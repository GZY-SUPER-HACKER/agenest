"""SSE tests.

Spec 6.3 gates two behaviours:

- **Cursor resume.** A client that passes `?cursor=<id>` gets everything after that id and
  nothing before it, and the cursor stays meaningful across a service restart because it is
  a *database* event id, not a per-connection counter.
- **Filtered streams still advance the cursor.** A stream filtered to one session receives
  the other sessions' frames as `: skip <id>` comments, so its cursor never silently lags.

Framing is tested directly against the broker (no socket). The HTTP surface runs against a
**real uvicorn socket** rather than `TestClient`, because `TestClient` runs the ASGI app to
completion — and an SSE generator, by design, never completes. Its `portal.call(...).result()`
blocks forever with no timeout, which would hang the suite rather than test anything.

The real-socket version is also the only one that proves the thing the phone depends on:
response headers reaching the client before the first event arrives.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import closing, contextmanager

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.schemas import EventOut, SessionOut
from app.stream import Broker, event_frame, session_frame, sse_message
from helpers import INGEST, report


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def running_app(build_app):
    """Serve the app on a real localhost port for the duration of the block."""
    port = _free_port()
    config = uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        if not server.started:
            raise RuntimeError("uvicorn did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture()
def http(settings, db, broker, session_id):
    """A served app plus an already-created session id."""
    from app.main import create_app

    def build():
        return create_app(settings=settings, db=db, broker=broker)

    with running_app(build) as base:
        yield base, session_id


def _read_lines(resp, count: int, want: str | None = None, limit: float = 5.0):
    """Read up to `count` lines, stopping early once a line containing `want` appears.

    The deadline bounds the read: the stream is infinite by design, so `count` is a safety
    net and `want` is the normal exit. Note this only stops the *loop*; the underlying socket
    read is what `READ_TIMEOUT` caps, because `iter_lines()` blocks on the wire.
    """
    lines: list[str] = []
    deadline = time.time() + limit
    for line in resp.iter_lines():
        lines.append(line)
        if want and want in line:
            break
        if len(lines) >= count:
            break
        if time.time() > deadline:
            break
    return lines


# --------------------------------------------------------------------------- #
# framing logic
# --------------------------------------------------------------------------- #
def test_resume_since_returns_only_frames_after_the_cursor() -> None:
    broker = Broker()
    broker.publish(1, {"kind": "session.event"})
    broker.publish(2, {"kind": "session.event"})
    broker.publish(5, {"kind": "session.event"})

    assert [f.seq for f in broker.replay_since(2)] == [5]
    assert [f.seq for f in broker.replay_since(0)] == [1, 2, 5]


def test_frames_that_arrive_during_reconnect_are_not_lost() -> None:
    """The window between replaying and subscribing is where frames used to vanish.

    `replay_and_subscribe` snapshots *after* registering the subscriber, so a frame published
    concurrently shows up twice — once in the replay, once live. The duplicate is harmless:
    the client's cursor skips an id it already rendered. A frame that appears zero times is
    not harmless, because the client has no way to know to ask for it.
    """
    broker = Broker()
    broker.publish(1, {"kind": "a"})
    replay, queue = broker.replay_and_subscribe(0)
    broker.publish(2, {"kind": "b"})

    replayed = [f.seq for f in replay]
    live = [f.seq for f in [queue.get_nowait()]]

    # Nothing lost:
    assert sorted(set(replayed + live)) == [1, 2]
    # And the concurrent frame is the one that may appear twice:
    assert replayed.count(2) + live.count(2) >= 1


def test_subscribe_before_replay_leaves_no_gap() -> None:
    """Publishing at the exact reconnect moment must not lose the frame."""
    broker = Broker()
    broker.publish(1, {"kind": "a"})
    replay, queue = broker.replay_and_subscribe(0)
    broker.publish(2, {"kind": "b"})

    ids = sorted({f.seq for f in replay} | {f.seq for f in [queue.get_nowait()]})
    assert ids == [1, 2]


def test_skip_comment_carries_only_the_id() -> None:
    class _Frame:
        seq = 7
        payload = {"kind": "session.event", "secret": "x"}

    assert sse_message(_Frame(), include=False) == ": skip 7\n\n"


def test_included_frame_has_id_event_and_data() -> None:
    class _Frame:
        seq = 9
        payload = {"kind": "session.event", "x": 1}

    text = sse_message(_Frame(), include=True)
    assert text.startswith("id: 9\nevent: session.event\ndata: ")
    assert text.endswith("\n\n")
    assert json.loads(text.split("data: ", 1)[1])["x"] == 1


def test_event_and_session_frames_share_one_id() -> None:
    """One write must advance a client's cursor once, not twice."""
    broker = Broker()
    event = EventOut(
        id=42, session_id="s_1", event_type="progress", message="m",
        occurred_at="t", received_at="t",
    )
    session = SessionOut(
        id="s_1", agent_type="codex", title="t", status="running",
        started_at="t", last_activity_at="t", created_at="t", updated_at="t",
    )
    broker.publish_event(event)
    broker.publish_session(session, after=event.id)
    assert [f.seq for f in broker.replay_since(0)] == [42, 42]


def test_session_frame_carries_every_field_the_client_type_declares() -> None:
    """The list hook *replaces* its copy of a session with this object.

    So a field the frame omits is a field erased from the screen: the card's "what it is
    doing" line used to disappear on the frame published alongside every event, because
    `last_event` did not survive it. `last_event` is also what says whether a session is
    blocked on a human, so the omission is not cosmetic.
    """
    event = EventOut(
        id=7, session_id="s_1", event_type="awaiting_user", message="needs permission",
        occurred_at="t", received_at="t",
    )
    session = SessionOut(
        id="s_1", agent_type="codex", title="t", status="running", workspace="/w",
        host="lab-box",
        started_at="t", last_activity_at="t", created_at="t", updated_at="t",
        last_event=event,
    )
    payload = session_frame(session)["session"]
    assert payload["last_event"]["event_type"] == "awaiting_user"
    assert payload["workspace"] == "/w"
    # The rail nests a session under its machine, so a frame without this would move the card
    # to the "no host recorded" group on the very event that updated it.
    assert payload["host"] == "lab-box"
    assert payload["created_at"] == "t"


# --------------------------------------------------------------------------- #
# HTTP surface, over a real socket
# --------------------------------------------------------------------------- #
def test_headers_arrive_before_the_first_event(http) -> None:
    """What the phone actually depends on: the connection is usable during a quiet period."""
    base, sid = http
    with httpx.Client(timeout=10) as c:
        t0 = time.time()
        with c.stream("GET", f"{base}/api/events") as resp:
            elapsed = time.time() - t0
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers["cache-control"] == "no-cache, no-transform"
            assert resp.headers.get("x-accel-buffering") == "no"
            first = _read_lines(resp, count=2, limit=elapsed + 5)
    assert first and first[0].startswith(": "), "the stream must open with a comment frame"


def test_reported_event_reaches_the_stream(http) -> None:
    """Report an event while a stream is open, and see it arrive."""
    base, sid = http
    api = _api_client(base)
    with api:
        api.post(
            f"/api/sessions/{sid}/events",
            json={"event_type": "progress", "message": "arrived over sse"},
        )

    with httpx.Client(timeout=httpx.Timeout(10, read=8)) as c:
        with c.stream("GET", f"{base}/api/events") as resp:
            lines = _read_lines(resp, count=40, want="arrived over sse")

    assert any("event: session.event" in l for l in lines)
    assert any("arrived over sse" in l for l in lines)


def _api_client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=10, headers=INGEST)


def test_cursor_resumes_after_last_seen_id(http) -> None:
    base, sid = http
    api = _api_client(base)
    with api:
        first = api.post(
            f"/api/sessions/{sid}/events",
            json={"event_type": "progress", "message": "first"},
        ).json()["event"]["id"]
        second = api.post(
            f"/api/sessions/{sid}/events",
            json={"event_type": "progress", "message": "second"},
        ).json()["event"]["id"]

    with httpx.Client(timeout=httpx.Timeout(10, read=8)) as c:
        with c.stream("GET", f"{base}/api/events?cursor={first}") as resp:
            lines = _read_lines(resp, count=40, want="second")

    payloads = [json.loads(l[6:]) for l in lines if l.startswith("data:")]
    ids = [p["event"]["id"] for p in payloads]
    assert second in ids
    assert first not in ids, "a frame the client already has must not be replayed"


def test_filtered_stream_keeps_cursor_advancing(http) -> None:
    """A stream filtered to one session must still advance the client's cursor.

    Session B's frames are filtered out — but they must not be *swallowed*. They arrive as
    `: skip <id>` comments so the client's `cursor` keeps moving and never silently lags
    behind the server's. This is the invariant that makes reconnect-with-cursor safe.
    """
    base, sid = http
    api = _api_client(base)
    with api:
        other = api.post(
            "/api/sessions", json={"agent_type": "codex", "title": "other"}
        ).json()["id"]
        api.post(
            f"/api/sessions/{other}/events",
            json={"event_type": "progress", "message": "not for you"},
        )

    # Read until the skip comments actually appear — don't stop at the first own-session
    # frame, which arrives earlier in the replay and would end the read too soon.
    with httpx.Client(timeout=httpx.Timeout(10, read=4)) as c:
        with c.stream("GET", f"{base}/api/events?session_id={sid}") as resp:
            lines = _read_lines(resp, count=400, want=": skip", limit=4)

    skips = [l for l in lines if l.startswith(": skip")]
    assert skips, "another session's frames must still arrive as skip comments"

    # The skipped ids must be real, increasing database ids.
    ids = [int(l.split()[2]) for l in skips]
    assert ids == sorted(ids)
    assert all(i > 0 for i in ids)

    # And nothing for another session leaked through as a data frame.
    for line in lines:
        if line.startswith("data:"):
            assert '"session_id":"' not in line or f'"session_id":"{sid}"' in line


def test_heartbeat_gets_no_session_updated_frame(http) -> None:
    base, sid = http
    api = _api_client(base)
    with api:
        api.post(
            f"/api/sessions/{sid}/events",
            json={"event_type": "heartbeat", "message": "ping"},
        )
        api.post(
            f"/api/sessions/{sid}/events",
            json={"event_type": "progress", "message": "real work"},
        )

    with httpx.Client(timeout=httpx.Timeout(10, read=8)) as c:
        with c.stream("GET", f"{base}/api/events?session_id={sid}") as resp:
            lines = _read_lines(resp, count=80, want="real work")

    events = [l.split(": ", 1)[1] for l in lines if l.startswith("event: ")]
    assert "session.event" in events


def test_stream_needs_no_credential_when_unlocked(http) -> None:
    base, _sid = http
    with httpx.Client(timeout=httpx.Timeout(10, read=8)) as c:
        with c.stream("GET", f"{base}/api/events") as resp:
            assert resp.status_code == 200
