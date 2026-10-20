"""The nanobot adapter, against a stub gateway.

No nanobot install and no network: `websockets.serve` runs a scripted stand-in inside the
test process, so what is pinned is *our* side of nanobot's channel protocol and our policy —
not nanobot's ability to talk to a model, and not whether anyone has a provider key today.

Three cases here come from reading nanobot's source rather than its documentation, and each
would be a silent bug if assumed differently:

- **`stream_end` ends a segment, and `resuming` says whether the turn continues.** A stage
  note arrives as `delta … stream_end{resuming:true}` followed by a *textless* `message` frame
  carrying `kind="progress"`. Treating the segment end as the answer files every checkpoint as
  a reply and announces the turn finished once per stage.
- **`user_message` reports other channels.** It exists so a websocket client can see the same
  conversation continued from Telegram. Our own sends can echo through it too, which is why
  the adapter suppresses a round trip but not genuine cross-channel traffic.
- **`attach` is connection-scoped.** A subscription disappears with the socket, so every
  conversation has to be re-attached after a reconnect or replies silently stop arriving.

And one about the door itself: the registered address is an *HTTP* one, and the socket token
is minted from `/webui/bootstrap` per connection (`nanobot/webui/ws_http.py`). That is what lets
an agent hand us a door it already had open, with no config edit and no gateway restart.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from nanobot_door import ASSEMBLED, CHECKPOINT, StubNanobot
from app.agent import endpoints, store
from app.agent.backends.base import Message, Status
from app.agent.backends.nanobot import NanobotBackend, NanobotTranscript, build as build_backend
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.schemas import AgentRole
from app.stream import AgentBroker, Broker
from fastapi.testclient import TestClient

ACCESS_TOKEN = "test-access-token"


@pytest.fixture(autouse=True)
def _no_registered_door():
    """The endpoint cache is process-local, so one test's registration would be another
    test's door. Every test here starts and ends with none."""
    endpoints.clear()
    yield
    endpoints.clear()


@pytest.fixture()
def gateway():
    stub = StubNanobot()
    stub.start()
    yield stub
    stub.stop()


def _settings(tmp_path: Path, *, port: int, dashboard_token: str | None = ACCESS_TOKEN) -> Settings:
    return Settings(
        db_path=tmp_path / "test.sqlite3",
        ingest_token="test-ingest-token",
        host="127.0.0.1",
        port=0,
        static_dir=None,
        stale_after_seconds=900,
        event_payload_max_chars=8000,
        cors_origins=(),
        dashboard_token=dashboard_token,
        agent_backend="nanobot",
        nanobot_ws_url=f"ws://127.0.0.1:{port}/",
        nanobot_client_id="test-dashboard",
        nanobot_token=None,
        pi_workspace=tmp_path,
    )


@pytest.fixture()
def app_client(tmp_path: Path, gateway: StubNanobot):
    settings = _settings(tmp_path, port=gateway.port)
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        yield client, db, gateway
    db.close()


def _headers(token: str = ACCESS_TOKEN) -> dict[str, str]:
    return {"X-CPD-Token": token}


def _wait_for(predicate, *, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not met before the timeout")


def _connected(client: TestClient) -> bool:
    return client.get("/api/agent/status", headers=_headers()).json()["connected"]


def _new_thread(client: TestClient) -> str:
    """The conversation the page is on — there is one, and this is the call that returns it."""
    response = client.post("/api/agent/session", headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _say(client: TestClient, thread_id: str, content: str):
    return client.post(
        f"/api/agent/threads/{thread_id}/messages",
        json={"content": content},
        headers=_headers(),
    )


def _messages(client: TestClient, thread_id: str) -> list[dict]:
    """The conversation, via the route that joins our metadata to the agent's messages.

    nanobot keeps its own transcript inside its own adapter now (docs/design.md §18) — the core
    has no message table — so the API is the only honest way to ask what a conversation holds.
    Reading the adapter's own file here would test its storage rather than the contract every
    backend is held to.
    """
    response = client.get(f"/api/agent/threads/{thread_id}", headers=_headers())
    assert response.status_code == 200, response.text
    return [
        {
            "role": m["role"],
            "content": m["content"],
            "tool_name": m["tool_name"],
            # Carried through rather than dropped: which rows are checkpoints and which is the
            # answer is part of what this contract promises (docs/design.md §23).
            "progress": m["progress"],
        }
        for m in response.json()["messages"]
    ]


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
def test_status_reports_the_backend_and_its_connection(app_client) -> None:
    client, _db, _gw = app_client
    _wait_for(lambda: _connected(client))
    body = client.get("/api/agent/status", headers=_headers()).json()
    assert body["backend"] == "nanobot"
    assert body["connected"] is True
    # nanobot's workspace is nanobot's business, so this layer must not claim to know it.
    assert body["workdir"] is None


def test_a_gateway_that_is_not_there_is_a_503(tmp_path: Path) -> None:
    """The most likely thing to be wrong on a new machine, and it must say so."""
    settings = _settings(tmp_path, port=9)  # discard port: nothing is listening
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        thread_id = _new_thread(client)
        response = _say(client, thread_id, "hello")
        assert response.status_code == 503, response.text
        assert "not reachable" in response.json()["detail"]
        assert "nanobot gateway" in response.json()["detail"]
    db.close()


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #
def test_a_thread_is_only_opened_on_the_agent_when_it_is_used(app_client) -> None:
    """Creating a thread is a local record. Opening a conversation on the agent costs
    something there, and a thread someone made by accident should leave no trace of it."""
    client, db, gateway = app_client
    _wait_for(lambda: _connected(client))

    thread_id = _new_thread(client)
    assert store.get_thread(db, thread_id).conversation_id is None
    assert not [f for f in gateway.received if f.get("type") == "new_chat"]

    _say(client, thread_id, "hello")
    _wait_for(lambda: store.get_thread(db, thread_id).conversation_id is not None)


# --------------------------------------------------------------------------- #
# the streaming contract
# --------------------------------------------------------------------------- #
def test_the_reply_is_assembled_from_deltas(app_client) -> None:
    """nanobot sends `delta` … `stream_end` and **no** `message` frame when streaming.

    This is the whole point of the adapter's buffer: a client waiting for `message` would
    record every reply as empty and look broken while working perfectly.
    """
    client, db, _gw = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")

    _wait_for(lambda: any(m["role"] == "assistant" for m in _messages(client, thread_id)))
    assistants = [m for m in _messages(client, thread_id) if m["role"] == "assistant"]
    assert [m["content"] for m in assistants] == [ASSEMBLED]


def test_history_can_omit_tool_rows_for_a_client_that_hides_them(app_client) -> None:
    """Tool rows are most of a long transcript's bytes, and the page's default view hides
    them. `?process=false` is the same history minus that machinery — the query parameter that
    keeps a switch from pulling hundreds of kilobytes over a phone link.
    """
    client, db, _gw = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")
    _wait_for(lambda: any(m["role"] == "tool" for m in _messages(client, thread_id)))

    full = client.get(f"/api/agent/threads/{thread_id}", headers=_headers()).json()["messages"]
    lean = client.get(
        f"/api/agent/threads/{thread_id}?process=false", headers=_headers()
    ).json()["messages"]
    assert any(m["role"] == "tool" for m in full)
    assert not any(m["role"] == "tool" for m in lean)
    assert lean == [m for m in full if m["role"] != "tool"]


def test_thinking_is_streamed_but_never_stored(app_client) -> None:
    """Reasoning is scaffolding for the answer, not part of the record."""
    client, db, _gw = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")
    _wait_for(lambda: any(m["role"] == "tool" for m in _messages(client, thread_id)))

    assert "hmm" not in [m["content"] for m in _messages(client, thread_id)]


def test_a_file_edit_becomes_a_tool_row(app_client) -> None:
    """`file_edit` is how tool activity reaches a websocket client — the WebUI's activity
    timeline is built from it."""
    client, db, _gw = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")

    _wait_for(lambda: any(m["role"] == "tool" for m in _messages(client, thread_id)))
    tools = [m for m in _messages(client, thread_id) if m["role"] == "tool"]
    assert tools[0]["tool_name"] == "edit"
    assert "src/thing.py" in tools[0]["content"]
    assert "+3 -1" in tools[0]["content"]


# --------------------------------------------------------------------------- #
# the user-message echo
# --------------------------------------------------------------------------- #
def test_our_own_message_is_not_stored_twice(app_client) -> None:
    """`user_message` fires for input from other channels — and can echo our own.

    An echo stored as a new message would put every question in the transcript twice, in a
    conversation where the user can see it happening.
    """
    client, db, _gw = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")
    _wait_for(lambda: any(m["role"] == "tool" for m in _messages(client, thread_id)))

    users = [m["content"] for m in _messages(client, thread_id) if m["role"] == "user"]
    assert users == ["hello"]


def test_input_from_another_channel_is_shown(app_client) -> None:
    """The same conversation continued from Telegram belongs in this transcript.

    That is the entire point of a shared session, and the reason `user_message` exists at all:
    suppressing the echo must not suppress the real thing.
    """
    client, db, gateway = app_client
    _wait_for(lambda: _connected(client))
    gateway._run_cross_channel = True

    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")

    _wait_for(
        lambda: "sent from telegram"
        in [m["content"] for m in _messages(client, thread_id) if m["role"] == "user"]
    )


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def test_abort_is_nanobots_stop_command(app_client) -> None:
    """There is no abort frame in the protocol. `/stop` is how a participant stops the
    current task, so that is what goes on the wire."""
    client, _db, gateway = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")
    _wait_for(lambda: store.get_thread(app_client[1], thread_id).conversation_id is not None)

    chat_id = store.get_thread(app_client[1], thread_id).conversation_id
    response = client.post(f"/api/agent/threads/{thread_id}/abort", headers=_headers())
    assert response.status_code == 202
    assert response.json()["aborted"] is True

    # Wait for the stub to *process* the frame: it runs in its own thread on its own event
    # loop, so a 202 only means our side handed the bytes to the socket.
    _wait_for(
        lambda: any(
            f.get("type") == "message" and f.get("content") == "/stop"
            for f in gateway.received
        )
    )
    sent = [f for f in gateway.received if f.get("type") == "message"]
    assert {"type": "message", "chat_id": chat_id, "content": "/stop"} in sent

    # Pressing Stop is not the user saying "/stop": the command goes on the wire, not into
    # the transcript.
    users = [m["content"] for m in _messages(client, thread_id) if m["role"] == "user"]
    assert users == ["hello"]


# --------------------------------------------------------------------------- #
# reconnect
# --------------------------------------------------------------------------- #
def test_conversations_are_re_attached_after_a_reconnect(app_client) -> None:
    """A subscription is connection-scoped, so losing the socket silently loses every
    conversation unless they are re-attached. The failure mode is "replies just stop"."""
    client, db, gateway = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")
    _wait_for(lambda: store.get_thread(db, thread_id).conversation_id is not None)
    chat_id = store.get_thread(db, thread_id).conversation_id

    # Drop the socket from the server side and wait for the adapter to come back.
    gateway.force_disconnect()
    _wait_for(lambda: _connected(client), timeout=30)

    attaches = [
        f for f in gateway.received if f.get("type") == "attach" and f.get("chat_id") == chat_id
    ]
    assert attaches, "the conversation was never re-attached after the reconnect"


# --------------------------------------------------------------------------- #
# the transcript's own storage
# --------------------------------------------------------------------------- #
def test_building_a_backend_nobody_uses_creates_no_transcript(tmp_path: Path) -> None:
    """P1 in HANDOFF-native.

    `build()` runs for every registered backend so the settings panel can list them, and the
    supervisor's contract is that construction opens nothing. Opening the transcript in
    `__init__` made a deployment running `native` create an empty database (plus a WAL) on
    every start — a side effect of drawing a menu.
    """
    settings = _settings(tmp_path, port=0)

    build_backend(settings, lambda *_: None)

    assert not (tmp_path / "nanobot-transcript.sqlite3").exists()


def test_the_transcript_is_opened_on_first_write_and_keeps_timestamps(tmp_path: Path) -> None:
    """Lazy opening must not cost the timestamp: a row restored from the old shared table
    carries its original time, and stamping "now" would make an old conversation look like it
    all arrived at upgrade time."""
    path = tmp_path / "nanobot-transcript.sqlite3"
    transcript = NanobotTranscript(path)
    assert not path.exists()

    stored = transcript.record(
        "chat-1",
        Message(
            role=AgentRole.user,
            content="hello",
            created_at="2026-01-02T03:04:05.000Z",
        ),
    )
    assert path.exists()
    assert stored.created_at == "2026-01-02T03:04:05.000Z"
    assert [m.created_at for m in transcript.messages("chat-1")] == [
        "2026-01-02T03:04:05.000Z"
    ]
    transcript.close()


def test_legacy_rows_are_imported_before_the_old_table_is_dropped(tmp_path: Path) -> None:
    """The two old nanobot threads were as stranded as the native ones.

    Their messages lived in the shared table; the adapter's new table started empty, so
    switching back to nanobot showed an empty conversation. The one-shot migration copies them
    before `agent_messages` goes away.
    """
    settings = _settings(tmp_path, port=0)
    db = Database(settings.db_path)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO agent_threads "
            "(id, title, backend, backend_conversation_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("t_nb", "an old chat", "nanobot", "chat-1",
             "2026-01-01T10:00:00.000Z", "2026-01-01T10:10:00.000Z"),
        )
        conn.execute(
            """
            CREATE TABLE agent_messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id    TEXT NOT NULL,
                role         TEXT NOT NULL,
                content      TEXT NOT NULL,
                tool_name    TEXT,
                tool_call_id TEXT,
                is_error     INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO agent_messages (thread_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t_nb", "user", "hello from the old table", "2026-01-01T10:00:02.000Z"),
        )

    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app):
        assert db.query_one("SELECT 1 FROM sqlite_master WHERE name = 'agent_messages'") is None

    transcript = NanobotTranscript(tmp_path / "nanobot-transcript.sqlite3")
    rows = transcript.messages("chat-1")
    assert [(m.role, m.content, m.created_at) for m in rows] == [
        (AgentRole.user, "hello from the old table", "2026-01-01T10:00:02.000Z")
    ]
    transcript.close()
    db.close()


def test_a_stage_note_is_a_checkpoint_and_a_tool_hint_is_not(app_client) -> None:
    """`kind` is nanobot's own line between trace and answer, and the adapter keeps it.

    A `progress` marker makes the segment a checkpoint row; a `tool_hint` — the per-call
    breadcrumb — is dropped, because one line per *stage* is the whole point and a per-call
    feed is the thing it was chosen over. The answer is unaffected: it arrives on the delta
    path, which carries no `kind` at all.
    """
    client, db, _gw = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello, with progress")

    _wait_for(lambda: any(m["progress"] for m in _messages(client, thread_id)))
    rows = _messages(client, thread_id)

    notes = [m for m in rows if m["progress"]]
    assert [m["content"] for m in notes] == [CHECKPOINT]
    assert notes[0]["role"] == "assistant"

    assert "editing a file" not in [m["content"] for m in rows], "a tool hint is not a checkpoint"
    assert ASSEMBLED in [m["content"] for m in rows], "the answer is still the answer"
    assert [m["content"] for m in rows if m["role"] == "assistant"] == [
        CHECKPOINT,
        ASSEMBLED,
    ], "one row per segment, in the order the agent produced them"


# --------------------------------------------------------------------------- #
# segment semantics (no socket: the frames are the contract)
# --------------------------------------------------------------------------- #
def _frames_backend():
    events: list[object] = []
    backend = NanobotBackend(
        url="ws://127.0.0.1:9/",
        client_id="test",
        token=None,
        on_event=lambda _chat, event: events.append(event),
    )
    return backend, events


def test_a_resuming_segment_end_is_a_checkpoint_not_the_end_of_the_turn() -> None:
    """`resuming: true` means the agent is about to call a tool and will keep talking.

    Reading every `stream_end` as the end of the turn made the page announce "finished" once
    per stage — which, with sticky terminal states, is exactly the signal the supervisor uses
    to close a run.
    """
    backend, events = _frames_backend()
    backend._handle({"event": "delta", "chat_id": "c1", "text": "looking at the file"})
    backend._handle({"event": "stream_end", "chat_id": "c1", "resuming": True})

    messages = [e for e in events if isinstance(e, Message)]
    assert [(m.content, m.progress) for m in messages] == [("looking at the file", True)]
    assert not [e for e in events if isinstance(e, Status) and e.streaming is False]
    assert backend.is_streaming("c1")


def test_the_progress_marker_after_a_streamed_segment_is_not_a_second_message() -> None:
    """The `message{kind=progress}` frame carries no text in streaming mode — it only says
    "that segment was a checkpoint". Storing it as a message records every stage twice."""
    backend, events = _frames_backend()
    backend._handle({"event": "delta", "chat_id": "c1", "text": "stage one"})
    backend._handle({"event": "stream_end", "chat_id": "c1", "resuming": True})
    backend._handle({"event": "message", "chat_id": "c1", "kind": "progress", "text": ""})

    assert [e.content for e in events if isinstance(e, Message)] == ["stage one"]


def test_a_progress_frame_that_does_carry_text_is_kept_once() -> None:
    """With the gateway's streaming off there are no deltas to assemble, so the same frame is
    the whole message — and it must not be dropped as a duplicate marker."""
    backend, events = _frames_backend()
    backend._handle({"event": "message", "chat_id": "c1", "kind": "progress", "text": "a stage"})

    assert [(e.content, e.progress) for e in events if isinstance(e, Message)] == [
        ("a stage", True)
    ]


def test_turn_end_is_what_finishes_the_turn() -> None:
    backend, events = _frames_backend()
    backend._handle({"event": "delta", "chat_id": "c1", "text": "the answer"})
    backend._handle({"event": "stream_end", "chat_id": "c1"})
    assert [e for e in events if isinstance(e, Status) and e.streaming is False]

    events.clear()
    backend._handle({"event": "turn_end", "chat_id": "c1"})
    assert [e for e in events if isinstance(e, Status) and e.streaming is False]
    assert not backend.is_streaming("c1")


def test_a_segment_that_never_closed_is_still_recorded_at_turn_end() -> None:
    """A failed run can end the turn without a `stream_end`. The text the user watched stream
    in is theirs; dropping it to keep the state machine tidy loses the visible half of a turn."""
    backend, events = _frames_backend()
    backend._handle({"event": "delta", "chat_id": "c1", "text": "half an ans"})
    backend._handle({"event": "turn_end", "chat_id": "c1"})

    assert [e.content for e in events if isinstance(e, Message)] == ["half an ans"]


# --------------------------------------------------------------------------- #
# the door: registration and the one-time token
# --------------------------------------------------------------------------- #
def test_the_registered_door_is_used_and_its_token_minted(tmp_path: Path) -> None:
    """The registration is the door, and the socket token comes from it per connection.

    Starting the service with a dead settings URL and registering a live door afterwards is
    exactly what "an agent connects itself" looks like — and it has to recover without a
    restart (§25).
    """
    gateway = StubNanobot(secret="issue-secret", require_token=True)
    gateway.start()
    try:
        settings = _settings(tmp_path, port=9)
        db = Database(settings.db_path)
        app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/backends",
                json={
                    "kind": "nanobot",
                    "url": gateway.http_url,
                    "token": "issue-secret",
                },
            )
            assert response.status_code == 201, response.text
            assert response.json()["last_error"] is None
            assert gateway.bootstrap_calls >= 1, "the probe actually knocked"
            assert gateway.bootstrap_auth[0] == "Bearer issue-secret"

            _wait_for(lambda: _connected(client), timeout=20)
            assert gateway.bootstrap_calls >= 2, "the socket minted its own token"
        db.close()
    finally:
        gateway.stop()
        from app.agent import endpoints

        endpoints.clear()


def test_a_wrong_secret_is_a_registration_error_not_a_silent_row(tmp_path: Path) -> None:
    gateway = StubNanobot(secret="issue-secret")
    gateway.start()
    try:
        settings = _settings(tmp_path, port=gateway.port)
        db = Database(settings.db_path)
        app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
        with TestClient(app) as client:
            body = client.post(
                "/api/agent/backends",
                json={"kind": "nanobot", "url": gateway.http_url, "token": "wrong"},
            ).json()
            assert "401" in (body["last_error"] or "")
            assert "tokenIssueSecret" in body["last_error"]
        db.close()
    finally:
        gateway.stop()
        from app.agent import endpoints

        endpoints.clear()


def test_the_adapters_own_token_is_never_taken_from_the_bootstrap_body(tmp_path: Path) -> None:
    """The bootstrap response also carries an `api_token` for the gateway's whole WebUI HTTP
    API. This backend needs one socket; holding that credential would be a second power."""
    gateway = StubNanobot()
    gateway.start()
    try:
        backend = NanobotBackend(
            url=gateway.http_url,
            client_id="test",
            token=None,
            on_event=lambda *_: None,
        )
        asyncio.run(backend.probe())
        assert gateway.bootstrap_calls == 1
        # Nothing about the response is retained: the next connect mints a new token.
        assert backend._last_error is None
    finally:
        gateway.stop()
