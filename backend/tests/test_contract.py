"""Tests for the rules that must never regress.

Each test names the spec requirement it guards, because these are the places where a
plausible-looking refactor quietly breaks the contract.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import Database
from helpers import DASH_TOKEN, INGEST, READ, TOKEN, report


def _make_session(c, *, title: str = "zombie") -> str:
    r = c.post("/api/sessions", json={"agent_type": "codex", "title": title}, headers=INGEST)
    assert r.status_code == 201, r.text
    return r.json()["id"]



# --------------------------------------------------------------------------- #
# auth (spec 9)
# --------------------------------------------------------------------------- #
def test_missing_token_is_rejected(client: TestClient) -> None:
    r = client.post("/api/sessions", json={"agent_type": "codex", "title": "x"})
    assert r.status_code == 401


def test_wrong_token_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/sessions",
        json={"agent_type": "codex", "title": "x"},
        headers={"X-CPD-Token": "not-the-token"},
    )
    assert r.status_code == 401


def test_bearer_form_is_accepted(client: TestClient) -> None:
    r = client.post(
        "/api/sessions",
        json={"agent_type": "codex", "title": "x"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 201


def test_reads_are_open_without_a_dashboard_token(client: TestClient) -> None:
    """Reads need no credential unless CSD_DASHBOARD_TOKEN is set."""
    assert client.get("/api/sessions").status_code == 200
    assert client.get(f"/api/sessions/anything").status_code in (200, 404)


def test_dashboard_token_is_enforced_when_set(settings, db, broker) -> None:
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token="must-match")
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        assert c.get("/api/sessions").status_code == 401
        assert c.get("/api/sessions", headers={"X-CPD-Token": "must-match"}).status_code == 200


def test_read_token_is_accepted_from_the_query_string(settings, db, broker) -> None:
    """`EventSource` cannot set headers, so the read token is also accepted in the URL.

    The exception is narrow and this test pins both edges of it. It is checked against a plain
    read rather than `/api/events` only because `TestClient` runs the SSE generator to
    completion, which for a stream that never completes means a hang rather than a test — the
    two endpoints share the dependency, so the read is what proves the behaviour.

    The second half is the one that matters more: the **ingest** token is never accepted from a
    query string. It can write, and a credential that can write must not end up in a URL, a
    browser history, or the server's own access log.
    """
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token="must-match")
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        assert c.get("/api/sessions?token=must-match").status_code == 200
        assert c.get("/api/sessions?token=wrong").status_code == 401

        assert (
            c.post(
                f"/api/sessions?token={TOKEN}",
                json={"agent_type": "codex", "title": "written with a URL credential"},
            ).status_code
            == 401
        )


def test_a_non_ascii_token_is_a_401_not_a_crash(settings, db, broker) -> None:
    """A pasted link with a sentence glued onto it is a bad credential, not a server error.

    `secrets.compare_digest` raises `TypeError` on non-ASCII `str`, which is exactly what a
    `?token=` carrying a copy-pasted URL plus its trailing prose produces. That request came
    back as a **500** on `/api/events` before the comparison moved to UTF-8 bytes — a 500 the
    reader cannot act on, on the one route where the token has to travel in the URL.
    """
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token="must-match")
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        assert c.get("/api/sessions?token=tok%E4%BD%A0%E5%A5%BD").status_code == 401


# --------------------------------------------------------------------------- #
# closing a session by hand (design.md §2, §3 and §22)
# --------------------------------------------------------------------------- #
def test_close_is_refused_when_no_access_token_is_configured(settings, db, broker) -> None:
    """An unconfigured access token leaves the acting routes disabled, not open.

    The opposite of the read path, and deliberately so: leaving reads open costs nothing on a
    private deployment, while leaving a *write* open on a public URL lets anyone edit the record.
    One token does not mean one rule — the credential is shared with reads, the *requirement* is
    not (design.md §22).
    """
    from app.main import create_app

    with TestClient(create_app(settings=settings, db=db, broker=broker)) as c:
        sid = _make_session(c)
        assert c.post(f"/api/sessions/{sid}/close", headers=INGEST).status_code == 403


def test_close_needs_the_access_token_and_the_ingest_token_cannot(settings, db, broker) -> None:
    """Neither direction may work: the ingest token cannot close, the page's token cannot write.

    If either did, the split between the credential the page uses and the one hosts report with
    would be decorative — and a machine permitted to report work would be permitted to drive an
    agent.
    """
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token=DASH_TOKEN)
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        sid = _make_session(c)

        assert c.post(f"/api/sessions/{sid}/close").status_code == 401
        assert c.post(f"/api/sessions/{sid}/close", headers=INGEST).status_code == 401

        # ...and the page's token cannot do the ingest token's job either.
        assert (
            c.post(
                "/api/sessions",
                json={"agent_type": "codex", "title": "made with the page credential"},
                headers=READ,
            ).status_code
            == 401
        )

        assert c.post(f"/api/sessions/{sid}/close", headers=READ).status_code == 200


def test_close_marks_a_running_session_completed_with_the_user_as_reason(
    settings, db, broker
) -> None:
    """`completed` + `stop_reason=user` — the status says it ended, the reason says who.

    It must not be `failed` (nothing went wrong) and it must not be a bare `completed` (the
    green check belongs to a reporter's outcome, not to a person tidying the board). The
    reason is what keeps those two apart without a sixth status (docs/design.md §44).
    """
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token=DASH_TOKEN)
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        sid = _make_session(c)

        body = c.post(f"/api/sessions/{sid}/close", headers=READ).json()
        assert body["status"] == "completed"
        assert body["stop_reason"] == "user"
        assert body["finished_at"] is not None
        assert "Closed from the dashboard" in body["summary"]

        # A human decision should be visible in the timeline, not only in the fields — and
        # the reason travels with it, so the timeline can say *who* ended the run.
        events = c.get(f"/api/sessions/{sid}/events", headers=READ).json()["events"]
        changed = [e for e in events if e["event_type"] == "status_changed"]
        assert changed and changed[0]["payload"]["stop_reason"] == "user"


def test_close_cannot_reopen_a_finished_session(settings, db, broker) -> None:
    """Terminal states are sticky and first-write-wins (spec 3, design.md §3).

    Closing is idempotent, so pressing the button twice — or on a session that finished
    normally — applies nothing rather than overwriting a real outcome.
    """
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token=DASH_TOKEN)
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        sid = _make_session(c)

        # The terminal state has to come from a real report first — an event, not a PATCH —
        # or the test proves nothing about stickiness.
        assert report(c, sid, "session_completed", "finished for real").status_code == 202

        body = c.post(f"/api/sessions/{sid}/close", headers=READ).json()
        assert body["status"] == "completed"


# --------------------------------------------------------------------------- #
# dismissing a session from needs-attention (design.md §42)
# --------------------------------------------------------------------------- #
def test_dismiss_is_refused_when_no_access_token_is_configured(settings, db, broker) -> None:
    """The same asymmetry as close: an unconfigured access token disables acting, not reads.

    The ingest token is refused as well — a machine permitted to report work must not be able
    to hide a row from the board.
    """
    from app.main import create_app

    with TestClient(create_app(settings=settings, db=db, broker=broker)) as c:
        sid = _make_session(c)
        assert c.post(f"/api/sessions/{sid}/dismiss", headers=INGEST).status_code == 403


def test_dismiss_needs_the_access_token_and_the_ingest_token_cannot(
    settings, db, broker
) -> None:
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token=DASH_TOKEN)
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        sid = _make_session(c)
        assert c.post(f"/api/sessions/{sid}/dismiss").status_code == 401
        assert c.post(f"/api/sessions/{sid}/dismiss", headers=INGEST).status_code == 401
        assert c.post(f"/api/sessions/{sid}/dismiss", headers=READ).status_code == 200


def test_dismiss_takes_an_ended_session_out_of_attention_without_moving_it(
    settings, db, broker
) -> None:
    """The exit needs-attention did not have: a `failed` row, or a verdict that lands after
    the run closed, has nothing to click.

    A `completed` row is not in the list at all (design.md §42); it is used here because it
    pins the generic rule — dismissal never rewrites the outcome, whatever that outcome is.

    Dismissing records that a person *read* it. It must not rewrite the outcome, and it must
    not move the activity clock either — acknowledging is not work, the same rule heartbeats
    follow. `updated_at` orders the rail, so touching it would teleport an old row to the top.
    """
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token=DASH_TOKEN)
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        sid = _make_session(c)
        assert report(c, sid, "progress", "halfway").status_code == 202
        before = c.patch(
            f"/api/sessions/{sid}",
            json={"status": "completed", "stop_reason": "crash"},
            headers=INGEST,
        ).json()
        assert before["dismissed"] is False

        body = c.post(f"/api/sessions/{sid}/dismiss", headers=READ).json()
        assert body["dismissed"] is True
        assert body["status"] == "completed"
        assert body["stop_reason"] == "crash"
        assert body["updated_at"] == before["updated_at"]
        assert body["last_activity_at"] == before["last_activity_at"]

        listed = c.get("/api/sessions", headers=READ).json()["sessions"]
        assert [s["dismissed"] for s in listed if s["id"] == sid] == [True]

        # One-way by design: there is no un-dismiss route. A newer report is the way back, and
        # `test_a_newer_report_puts_a_dismissed_session_back` is that half.
        assert c.delete(f"/api/sessions/{sid}/dismiss", headers=READ).status_code == 405


def test_a_newer_report_puts_a_dismissed_session_back(settings, db, broker) -> None:
    """The dismissal is a watermark, not a switch.

    A verifier reports `task_failed` *after* the run has closed (design.md §40). An
    acknowledgement recorded a minute earlier must not swallow it — that is precisely the
    report the reader needs.
    """
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token=DASH_TOKEN)
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        sid = _make_session(c)
        assert report(c, sid, "session_completed", "run ended").status_code == 202
        assert c.post(f"/api/sessions/{sid}/dismiss", headers=READ).json()["dismissed"] is True

        assert report(c, sid, "task_failed", "3 tests failed").status_code == 202
        body = c.get(f"/api/sessions/{sid}", headers=READ).json()
        assert body["dismissed"] is False
        assert body["last_verdict"]["event_type"] == "task_failed"


def test_a_heartbeat_does_not_reopen_a_dismissed_session(settings, db, broker) -> None:
    """A ping is a liveness fact, not a new report about the work.

    If silence-renewal could clear the acknowledgement, a process that is merely still running
    would put its own row back on the board every few minutes.
    """
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token=DASH_TOKEN)
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        sid = _make_session(c)
        assert report(c, sid, "session_failed", "agent crashed").status_code == 202
        assert c.post(f"/api/sessions/{sid}/dismiss", headers=READ).json()["dismissed"] is True

        assert report(c, sid, "heartbeat", "still here").status_code == 202
        body = c.get(f"/api/sessions/{sid}", headers=READ).json()
        assert body["dismissed"] is True
        assert body["status"] == "failed"

        # Repeating the action is idempotent in the sense that matters: still dismissed.
        assert c.post(f"/api/sessions/{sid}/dismiss", headers=READ).json()["dismissed"] is True


def test_dismiss_of_an_unknown_session_is_404(settings, db, broker) -> None:
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token=DASH_TOKEN)
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        assert c.post("/api/sessions/s_nope/dismiss", headers=READ).status_code == 404


# --------------------------------------------------------------------------- #
# validation (spec 6.4, 9)
# --------------------------------------------------------------------------- #
def test_unknown_field_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/sessions",
        json={"agent_type": "codex", "title": "x", "statuss": "running"},
        headers=INGEST,
    )
    assert r.status_code == 422, "a typo must not silently become a default"


def test_invalid_status_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/sessions",
        json={"agent_type": "codex", "title": "x", "status": "finished"},
        headers=INGEST,
    )
    assert r.status_code == 422


def test_invalid_event_type_is_rejected(session_id: str, client: TestClient) -> None:
    r = report(client, session_id, "agent_became_sentient", "hmm")
    assert r.status_code == 422


def test_cpctl_offers_every_event_type_the_server_accepts() -> None:
    """The CLI is a separate zero-dependency package, so the two lists are pinned together.

    A server event the CLI cannot send is a feature nobody can use; this is how the
    `checkpoint` type would have been silently unusable.
    """
    import sys
    from pathlib import Path

    from app.schemas import EventType

    cpctl = Path(__file__).resolve().parents[2] / "sdk" / "cpctl"
    if str(cpctl) not in sys.path:
        sys.path.insert(0, str(cpctl))
    from cpctl.cli import EVENT_TYPES

    assert set(EVENT_TYPES) == {event.value for event in EventType}


def test_cpctl_resolves_the_session_a_connector_published(
    tmp_path, monkeypatch
) -> None:
    """The agent's shell cannot see a connector's session id, so the connector writes it down.

    This pins the cross-package convention: one file per writer under
    `$CSD_AGENT_HOME/state/sessions.d/`, keyed by working directory. Without it,
    `cpctl task done` from inside an interactive session would have no id to name.
    """
    import json
    import sys
    from pathlib import Path

    cpctl = Path(__file__).resolve().parents[2] / "sdk" / "cpctl"
    if str(cpctl) not in sys.path:
        sys.path.insert(0, str(cpctl))
    from cpctl.cli import Client

    home = tmp_path / "home"
    work = tmp_path / "project"
    work.mkdir()
    map_dir = home / "state" / "sessions.d"
    map_dir.mkdir(parents=True)
    (map_dir / "csd-agent.json").write_text(
        json.dumps(
            {
                "writer": "csd-agent",
                "by_cwd": {
                    str(work): [
                        {"session_id": "s_older", "started_at": 1.0},
                        {"session_id": "s_newer", "started_at": 2.0},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("CSD_AGENT_HOME", str(home))
    monkeypatch.delenv("CSD_SESSION_ID", raising=False)
    monkeypatch.chdir(work)
    client = Client(state_dir=tmp_path / "empty-state")

    assert client._resolve_session(None) == "s_newer"
    assert client._resolve_session("s_explicit") == "s_explicit"

    # A launcher that owns the run exports the id; it wins over the map outright.
    monkeypatch.setenv("CSD_SESSION_ID", "s_from_wrapper")
    assert client._resolve_session(None) == "s_from_wrapper"


def test_server_generates_session_id(client: TestClient) -> None:
    r = client.post(
        "/api/sessions", json={"agent_type": "codex", "title": "x"}, headers=INGEST
    )
    body = r.json()
    assert body["id"].startswith("s_")
    assert len(body["id"]) > 10
    assert body["status"] == "running"


def test_titles_are_length_capped(client: TestClient) -> None:
    r = client.post(
        "/api/sessions",
        json={"agent_type": "codex", "title": "t" * 500},
        headers=INGEST,
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# terminal states (spec 4.2, 6.4; acceptance §12)
# --------------------------------------------------------------------------- #
def test_late_event_does_not_reopen_session(
    session_id: str, client: TestClient
) -> None:
    report(client, session_id, "session_completed", "done")
    r = report(client, session_id, "progress", "arrived late")
    assert r.status_code == 202
    assert client.get(f"/api/sessions/{session_id}", headers=READ).json()["status"] == "completed"


def test_late_patch_cannot_override_terminal_status(
    session_id: str, client: TestClient
) -> None:
    report(client, session_id, "session_failed", "boom")
    r = client.patch(
        f"/api/sessions/{session_id}", json={"status": "completed"}, headers=INGEST
    )
    assert r.status_code == 200
    assert r.json()["status"] == "failed"


def test_first_terminal_verdict_wins(session_id: str, client: TestClient) -> None:
    report(client, session_id, "session_failed", "boom")
    report(client, session_id, "session_completed", "wait it worked")
    assert client.get(f"/api/sessions/{session_id}", headers=READ).json()["status"] == "failed"


def test_metadata_patch_still_applies_after_terminal(
    session_id: str, client: TestClient
) -> None:
    """Refusing to reopen a session must not mean refusing to record its outcome."""
    report(client, session_id, "session_completed", "done")
    r = client.patch(
        f"/api/sessions/{session_id}",
        json={"summary": "all green", "exit_code": 0},
        headers=INGEST,
    )
    body = r.json()
    assert body["status"] == "completed"
    assert body["summary"] == "all green"
    assert body["exit_code"] == 0


def test_unknown_status_can_be_resumed(session_id: str, client: TestClient) -> None:
    r = client.patch(
        f"/api/sessions/{session_id}", json={"status": "unknown"}, headers=INGEST
    )
    assert r.json()["status"] == "unknown"
    r2 = client.patch(
        f"/api/sessions/{session_id}", json={"status": "running"}, headers=INGEST
    )
    assert r2.json()["status"] == "running"


def test_unknown_status_is_not_overwritten_by_automatic_event(
    session_id: str, client: TestClient
) -> None:
    client.patch(
        f"/api/sessions/{session_id}", json={"status": "unknown"}, headers=INGEST
    )
    report(client, session_id, "progress", "still going")
    assert client.get(f"/api/sessions/{session_id}", headers=READ).json()["status"] == "unknown"


# --------------------------------------------------------------------------- #
# idempotency (spec 8.3)
# --------------------------------------------------------------------------- #
def test_duplicate_event_key_inserts_one_row(
    session_id: str, client: TestClient
) -> None:
    first = report(client, session_id, "progress", "step", event_key="k-1").json()
    second = report(client, session_id, "progress", "step", event_key="k-1").json()
    assert first["event"]["id"] == second["event"]["id"]
    assert client.get(f"/api/sessions/{session_id}/events", headers=READ).json()["total"] == 2


def test_heartbeat_duplicate_does_not_double_count(
    session_id: str, client: TestClient
) -> None:
    report(client, session_id, "heartbeat", "ping", event_key="hb-1")
    report(client, session_id, "heartbeat", "ping", event_key="hb-1")
    detail = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert detail["heartbeat_count"] == 1


def test_event_for_unknown_session_is_404(client: TestClient) -> None:
    r = client.post(
        "/api/sessions/nope/events",
        json={"event_type": "note", "message": "x"},
        headers=INGEST,
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# reads (spec 6.1)
# --------------------------------------------------------------------------- #
def test_listing_orders_running_first(client: TestClient) -> None:
    for i in range(2):
        client.post(
            "/api/sessions",
            json={"agent_type": "codex", "title": f"run {i}"},
            headers=INGEST,
        )
    done = client.post(
        "/api/sessions",
        json={"agent_type": "codex", "title": "finished one"},
        headers=INGEST,
    ).json()["id"]
    report(client, done, "session_completed", "done")

    body = client.get("/api/sessions", headers=READ).json()
    statuses = [s["status"] for s in body["sessions"]]
    assert statuses[0] == "running"
    assert statuses.index("running") < statuses.index("completed")


def test_filter_by_status_and_agent(client: TestClient, session_id: str) -> None:
    assert session_id  # fixture guarantees one running session exists
    assert client.get("/api/sessions?status=running", headers=READ).json()["total"] >= 1
    assert client.get("/api/sessions?agent_type=nonexistent", headers=READ).json()["total"] == 0


def test_events_are_newest_first(session_id: str, client: TestClient) -> None:
    report(client, session_id, "note", "first")
    report(client, session_id, "note", "second")
    events = client.get(f"/api/sessions/{session_id}/events", headers=READ).json()["events"]
    assert events[0]["message"] == "second"
    assert events[-1]["message"].startswith("Session started")


def test_heartbeat_is_not_the_last_display_event(
    session_id: str, client: TestClient
) -> None:
    report(client, session_id, "progress", "real work")
    report(client, session_id, "heartbeat", "ping")
    detail = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert detail["events"][0]["event_type"] == "progress"


# --------------------------------------------------------------------------- #
# the quiet clock: `disconnected` is derived, and reversible (design.md §44)
# --------------------------------------------------------------------------- #
def test_a_quiet_session_is_disconnected_and_a_heartbeat_clears_it(
    session_id: str, client: TestClient, db
) -> None:
    """The acceptance core: the state is derived from silence, and it undoes itself.

    Nothing writes `disconnected`. It is computed per response from
    `last_heartbeat_at`, so one arriving heartbeat returns the row to `running` with no
    background job and no "un-disconnect" write — the same shape `possibly_stale` had, on the
    clock that actually answers "is the agent there?".
    """
    report(client, session_id, "heartbeat", "alive")
    db.execute(
        "UPDATE sessions SET last_heartbeat_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
        (session_id,),
    )

    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["disconnected"] is True
    assert body["possibly_stale"] is True  # deprecated alias, same value
    assert body["status"] == "running"

    assert report(client, session_id, "heartbeat", "alive again").status_code == 202
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["disconnected"] is False
    assert body["status"] == "running"


def test_a_reporter_without_heartbeats_falls_back_to_activity(
    session_id: str, client: TestClient, db
) -> None:
    """No heartbeat mechanism is not a stopped heartbeat.

    A hand-run `cpctl` session, and parts of the Pi/DSH reporting shapes, send no heartbeat
    at all. Judging those by `last_heartbeat_at` would turn "never had this feature" into
    "lost the agent" — every such row would go grey on the day this shipped.
    """
    report(client, session_id, "progress", "still working")
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["last_heartbeat_at"] is None
    assert body["disconnected"] is False

    # With no heartbeat to use, the activity clock is the honest one.
    db.execute(
        "UPDATE sessions SET last_activity_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
        (session_id,),
    )
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["disconnected"] is True
    assert body["status"] == "running"


def test_finished_session_is_never_disconnected(
    session_id: str, client: TestClient, db
) -> None:
    report(client, session_id, "session_completed", "done")
    db.execute(
        "UPDATE sessions SET last_heartbeat_at = '2020-01-01T00:00:00.000Z', "
        "last_activity_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
        (session_id,),
    )
    detail = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert detail["disconnected"] is False
    assert detail["possibly_stale"] is False
    assert detail["status"] == "completed"


# --------------------------------------------------------------------------- #
# legacy agents: `interrupted` is translated on the way in (§44)
# --------------------------------------------------------------------------- #
def test_a_legacy_interrupted_patch_is_accepted_and_translated(
    session_id: str, client: TestClient
) -> None:
    """An old `cpctl` still sends this. A 422 would make it retry forever."""
    r = client.patch(
        f"/api/sessions/{session_id}", json={"status": "interrupted"}, headers=INGEST
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["stop_reason"] == "unknown"


def test_a_legacy_session_interrupted_event_is_accepted(
    session_id: str, client: TestClient
) -> None:
    r = report(client, session_id, "session_interrupted", "process went away")
    assert r.status_code == 202
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["status"] == "completed"
    assert body["stop_reason"] == "unknown"


def test_a_legacy_interrupted_create_is_accepted(
    client: TestClient,
) -> None:
    r = client.post(
        "/api/sessions",
        json={"agent_type": "codex", "title": "late", "status": "interrupted"},
        headers=INGEST,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "completed"
    assert body["stop_reason"] == "unknown"


def test_stop_reason_is_first_write_wins(session_id: str, client: TestClient) -> None:
    """A late crash report must not overwrite the human who pressed close (§44)."""
    report(
        client,
        session_id,
        "session_completed",
        "closed",
        payload={"stop_reason": "user"},
    )
    body = client.patch(
        f"/api/sessions/{session_id}", json={"stop_reason": "crash"}, headers=INGEST
    ).json()
    assert body["status"] == "completed"
    assert body["stop_reason"] == "user"


def test_stop_reason_is_not_written_onto_a_running_row(
    session_id: str, client: TestClient
) -> None:
    """A reason describes an ending; a running row has not had one."""
    body = client.patch(
        f"/api/sessions/{session_id}", json={"stop_reason": "crash"}, headers=INGEST
    ).json()
    assert body["status"] == "running"
    assert body["stop_reason"] is None


def test_closing_does_not_count_as_a_heartbeat(
    settings, db, broker
) -> None:
    """Closing records an ending, not liveness — the clock must not move."""
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token=DASH_TOKEN)
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        sid = _make_session(c)
        report(c, sid, "heartbeat", "alive")
        before = c.get(f"/api/sessions/{sid}", headers=READ).json()["last_heartbeat_at"]

        body = c.post(f"/api/sessions/{sid}/close", headers=READ).json()
        assert body["status"] == "completed"
        assert body["stop_reason"] == "user"
        assert body["last_heartbeat_at"] == before


# --------------------------------------------------------------------------- #
# migration: additive, idempotent, and backfilled once (§44)
# --------------------------------------------------------------------------- #
def test_migration_adds_columns_and_backfills_last_heartbeat_once(tmp_path: Path) -> None:
    """A deployed database has the old shape; the two new columns must appear on boot.

    The backfill matters as much as the ALTER: without it every historical running row reads
    as `disconnected` on the first response, because NULL is indistinguishable from "the
    heartbeat stopped". `IS NULL` makes the second boot a no-op.
    """
    path = tmp_path / "legacy.sqlite3"
    first = Database(path)
    first.execute(
        "INSERT INTO sessions (id, agent_type, title, status, started_at, "
        "last_activity_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "s_legacy",
            "claude_code",
            "old",
            "running",
            "2026-01-01T00:00:00.000Z",
            "2026-01-01T00:00:00.000Z",
            "2026-01-01T00:00:00.000Z",
            "2026-01-01T00:00:00.000Z",
        ),
    )
    first.execute(
        "INSERT INTO session_events (session_id, event_type, message, occurred_at, "
        "received_at) VALUES (?, ?, ?, ?, ?)",
        (
            "s_legacy",
            "heartbeat",
            "alive",
            "2026-01-01T00:00:00.000Z",
            "2026-01-01T00:00:00.000Z",
        ),
    )
    first.close()

    # Make it look like the deployed database: the old table, without the new columns.
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE sessions DROP COLUMN last_heartbeat_at")
    conn.execute("ALTER TABLE sessions DROP COLUMN stop_reason")
    conn.commit()
    conn.close()

    second = Database(path)
    row = second.query_one(
        "SELECT last_heartbeat_at, stop_reason FROM sessions WHERE id = 's_legacy'"
    )
    assert row["last_heartbeat_at"] == "2026-01-01T00:00:00.000Z"
    assert row["stop_reason"] is None
    second.execute(
        "UPDATE sessions SET last_heartbeat_at = '2026-02-02T00:00:00.000Z' "
        "WHERE id = 's_legacy'"
    )
    second.close()

    third = Database(path)
    row = third.query_one(
        "SELECT last_heartbeat_at FROM sessions WHERE id = 's_legacy'"
    )
    # A non-NULL value is not re-derived, so a second boot cannot move it backward.
    assert row["last_heartbeat_at"] == "2026-02-02T00:00:00.000Z"
    third.close()


# --------------------------------------------------------------------------- #
# payload hygiene (spec 9, 8.9)
# --------------------------------------------------------------------------- #
def test_oversized_payload_string_is_truncated_not_dropped(
    session_id: str, client: TestClient
) -> None:
    r = report(client, session_id, "progress", "step", payload={"blob": "x" * 50_000})
    assert r.status_code == 202
    stored = r.json()["event"]["payload"]["blob"]
    assert len(stored) < 50_000
    assert stored.endswith("[truncated]")


def test_payload_defaults_to_nothing(session_id: str, client: TestClient) -> None:
    body = report(client, session_id, "progress", "step").json()
    assert body["event"]["payload"] is None


# --------------------------------------------------------------------------- #
# persistence (spec 4.4)
# --------------------------------------------------------------------------- #
def test_data_survives_restart(settings, db, broker) -> None:
    from app.database import Database
    from app.main import create_app

    with TestClient(create_app(settings=settings, db=db, broker=broker)) as c:
        sid = c.post(
            "/api/sessions",
            json={"agent_type": "codex", "title": "durable"},
            headers=INGEST,
        ).json()["id"]
        report(c, sid, "progress", "work")
    db.close()

    reopened = Database(settings.db_path)
    try:
        with TestClient(create_app(settings=settings, db=reopened, broker=broker)) as c:
            assert c.get(f"/api/sessions/{sid}", headers=READ).json()["title"] == "durable"
            assert c.get(f"/api/sessions/{sid}/events", headers=READ).json()["total"] >= 1
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# last_event: what the rail shows as "what it is doing"                      #
# --------------------------------------------------------------------------- #
def test_last_event_is_the_newest_real_activity(
    session_id: str, client: TestClient
) -> None:
    report(client, session_id, "progress", "reading the source")
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["last_event"]["message"] == "reading the source"


def test_last_event_ignores_heartbeats(
    session_id: str, client: TestClient
) -> None:
    """A liveness ping is not something the user did."""
    report(client, session_id, "progress", "running tests")
    report(client, session_id, "heartbeat", "ping")
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["last_event"]["message"] == "running tests"


def test_last_event_ignores_status_changed(
    session_id: str, client: TestClient
) -> None:
    """`status_changed` is written by the dashboard itself.

    Surfacing it as "what it is doing" is circular: the UI reports its own bookkeeping back
    as activity. The status is already on the session; the last real work is a different
    question.
    """
    report(client, session_id, "progress", "writing the patch")
    client.patch(
        f"/api/sessions/{session_id}", json={"status": "interrupted"}, headers=INGEST
    )
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    # `interrupted` is still accepted from a legacy `cpctl`, and translated on the way in
    # (docs/design.md §44): the row is closed, and nobody said why.
    assert body["status"] == "completed"
    assert body["stop_reason"] == "unknown"
    assert body["last_event"]["message"] == "writing the patch"


def test_a_checkpoint_is_activity_without_moving_status(
    session_id: str, client: TestClient
) -> None:
    """A checkpoint answers "what is it doing now", not "how did this end".

    It must behave like `progress`: surface on the card as the newest activity, and leave the
    status column alone. §2/§3 are not repealed by a richer event type.
    """
    r = report(
        client,
        session_id,
        "checkpoint",
        "Now I'll add the cursor test",
        payload={"source": "prose", "turn": 2, "message_id": "m-1"},
        event_key="cp-1",
    )
    assert r.status_code == 202
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["status"] == "running"
    assert body["last_event"]["event_type"] == "checkpoint"
    assert body["last_event"]["message"] == "Now I'll add the cursor test"
    assert body["last_event"]["payload"]["source"] == "prose"


def test_a_checkpoint_does_not_reopen_a_terminal_session(
    session_id: str, client: TestClient
) -> None:
    """Checkpoints arrive mid-work; one landing late must not resurrect a finished row."""
    report(client, session_id, "session_completed", "done", event_key="done-1")
    r = report(client, session_id, "checkpoint", "late sentence", event_key="cp-late")
    assert r.status_code == 202
    assert client.get(f"/api/sessions/{session_id}", headers=READ).json()["status"] == "completed"


def test_duplicate_checkpoint_key_inserts_one_row(
    session_id: str, client: TestClient
) -> None:
    """The connector keys a checkpoint on the agent's own message id.

    A retried `PostToolBatch` therefore re-sends the same sentence without stacking a second
    row — §7 applies to this event type like every other.
    """
    first = report(client, session_id, "checkpoint", "same sentence", event_key="cp-2").json()
    second = report(client, session_id, "checkpoint", "same sentence", event_key="cp-2").json()
    assert first["event"]["id"] == second["event"]["id"]
    assert client.get(f"/api/sessions/{session_id}/events", headers=READ).json()["total"] == 2


# --------------------------------------------------------------------------- #
# awaiting_user: blocked on a human, reported and never inferred              #
# --------------------------------------------------------------------------- #
def test_awaiting_user_leaves_the_status_alone(
    session_id: str, client: TestClient
) -> None:
    """Waiting is not an outcome, so it is an event and not a status.

    The overview groups a session as blocked by reading this event, and the grouping only
    survives because the event stays out of `status`: statuses are terminal and sticky, and
    a session that is merely waiting on an answer has to be able to come back from it.
    """
    r = report(client, session_id, "awaiting_user", "needs permission to run psql")
    assert r.status_code == 202
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["status"] == "running"
    assert body["finished_at"] is None
    assert body["last_event"]["event_type"] == "awaiting_user"


def test_awaiting_user_clears_on_the_next_event(
    session_id: str, client: TestClient
) -> None:
    """Self-clearing is the whole reason it is an event rather than a field.

    The dashboard reads the newest display event, so the answer — or any progress at all —
    supersedes it, with no flag to reset and no expiry to get wrong.
    """
    report(client, session_id, "awaiting_user", "needs permission to run psql")
    report(client, session_id, "progress", "permission granted, continuing")
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["last_event"]["event_type"] == "progress"


def test_late_awaiting_user_cannot_reopen_a_terminal_session(
    session_id: str, client: TestClient
) -> None:
    """A notification arriving after the session ended is recorded, and decides nothing.

    The event stays in the timeline — the record is append-only, and the dashboard does not
    rewrite history it was handed. It just cannot move the verdict, which is what the
    client's `running` gate reads before it will call anything blocked.
    """
    report(client, session_id, "session_completed", "done")
    report(client, session_id, "awaiting_user", "late notification")
    detail = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert detail["status"] == "completed"
    assert any(e["event_type"] == "awaiting_user" for e in detail["events"])


# --------------------------------------------------------------------------- #
# task verdicts: the work's outcome, beside the run's                         #
# --------------------------------------------------------------------------- #
def test_task_failed_lands_after_a_terminal_status_and_moves_no_status(
    session_id: str, client: TestClient
) -> None:
    """The ordinary order is `session_completed` and *then* the verdict.

    Verification runs when the agent exits, so a status gate — the one `awaiting_user` needs
    to keep a late notification off a finished session — would hide the event entirely. The
    session stays `completed`, because that is still how the *run* ended; the verdict is what
    says the *task* did not pass (docs/design.md §40).
    """
    report(client, session_id, "session_completed", "agent exited 0")
    r = report(client, session_id, "task_failed", "pytest: 3 failed")
    assert r.status_code == 202

    detail = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert detail["status"] == "completed"
    assert detail["last_event"]["event_type"] == "task_failed"
    assert detail["last_verdict"]["event_type"] == "task_failed"


def test_task_verdict_survives_the_turn_boundary(
    session_id: str, client: TestClient
) -> None:
    """The verdict is projected separately from `last_event`, and this is why.

    An agent reports its verdict at the end of a turn; the connector's `Stop` reports
    `turn_completed` immediately afterwards. Reading the verdict off `last_event` would make
    it visible for a few hundred milliseconds and then hide it behind the boundary that says
    nothing about the task. `last_verdict` keeps the fact; the client decides whether it still
    describes the current work.
    """
    report(client, session_id, "task_failed", "pytest: 3 failed")
    report(client, session_id, "turn_completed", "reply finished")

    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["last_event"]["event_type"] == "turn_completed"
    assert body["last_verdict"]["event_type"] == "task_failed"


def test_task_completed_is_the_positive_verdict(
    session_id: str, client: TestClient
) -> None:
    """Symmetric with `task_failed`, and it closes nothing either."""
    r = report(client, session_id, "task_completed", "12 tests pass")
    assert r.status_code == 202

    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["status"] == "running"
    assert body["finished_at"] is None
    assert body["last_verdict"]["event_type"] == "task_completed"


def test_a_newer_verdict_replaces_the_older_one(
    session_id: str, client: TestClient
) -> None:
    """One session, one current verdict: the last thing the agent said about the task."""
    report(client, session_id, "task_failed", "first attempt failed")
    report(client, session_id, "task_completed", "second attempt passed")

    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["last_verdict"]["event_type"] == "task_completed"


def test_a_later_progress_event_is_newer_than_the_verdict(
    session_id: str, client: TestClient
) -> None:
    """The verdict stays in the record; whether it still describes the work is display.

    A retry reports progress, and the client stops showing the old verdict — but nothing
    rewrites history, and the verdict remains the newest one reported.
    """
    report(client, session_id, "task_failed", "tests failed")
    report(client, session_id, "progress", "retrying with a different approach")

    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["last_event"]["event_type"] == "progress"
    assert body["last_verdict"]["event_type"] == "task_failed"


def test_every_event_type_is_either_terminal_or_descriptive() -> None:
    """A new event type must land on one side of the line.

    The server stores an unclassified type, but no reader would know whether it may close a
    session. This is the assertion the next person adding one meets, instead of a silent
    ambiguity about what the event does to `status`.
    """
    from app.schemas import NON_TERMINAL_EVENTS, TERMINAL_EVENTS, EventType

    assert set(EventType) == set(NON_TERMINAL_EVENTS) | set(TERMINAL_EVENTS)


# --------------------------------------------------------------------------- #
# turn_completed: a reply boundary, and it moves no status                    #
# --------------------------------------------------------------------------- #
def test_turn_completed_leaves_the_status_running(
    session_id: str, client: TestClient
) -> None:
    """A finished reply is not a finished session.

    An interactive editor keeps one Claude Code process alive for days, so `SessionEnd` does
    not fire between prompts. `Stop` reports the turn boundary as this event; the status stays
    `running`, because the process really is still open (docs/design.md §36).
    """
    r = report(client, session_id, "turn_completed", "Reply finished (turn 1)")
    assert r.status_code == 202
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["status"] == "running"
    assert body["finished_at"] is None
    assert body["last_event"]["event_type"] == "turn_completed"


def test_a_new_prompt_supersedes_turn_completed(
    session_id: str, client: TestClient
) -> None:
    """Idle clears itself exactly like waiting does: the next display event replaces it.

    The UI reads the newest event, so the prompt the user sends is what ends the idle state —
    no flag to reset, and no expiry that could leave a working session looking idle.
    """
    report(client, session_id, "turn_completed", "Reply finished (turn 1)")
    report(client, session_id, "progress", "Turn 2: continue")
    body = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert body["status"] == "running"
    assert body["last_event"]["event_type"] == "progress"


def test_late_turn_completed_cannot_reopen_a_terminal_session(
    session_id: str, client: TestClient
) -> None:
    """Sticky terminals outrank a turn boundary, like every other late event."""
    report(client, session_id, "session_completed", "done")
    report(client, session_id, "turn_completed", "late reply boundary")
    detail = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert detail["status"] == "completed"
    assert any(e["event_type"] == "turn_completed" for e in detail["events"])


def test_list_carries_last_event_for_every_session(client: TestClient) -> None:
    """The rail renders a whole page from one payload; it must not need N+1 requests."""
    ids = []
    for i in range(3):
        ids.append(
            client.post(
                "/api/sessions",
                json={"agent_type": "codex", "title": f"task {i}"},
                headers=INGEST,
            ).json()["id"]
        )
    report(client, ids[0], "progress", "compiling")

    sessions = client.get("/api/sessions", headers=READ).json()["sessions"]
    by_id = {s["id"]: s for s in sessions}
    assert by_id[ids[0]]["last_event"]["message"] == "compiling"
    # Sessions with only their opening event still carry one.
    for sid in ids[1:]:
        assert by_id[sid]["last_event"]["event_type"] == "session_started"


# --------------------------------------------------------------------------- #
# placeholder titles (§39)
# --------------------------------------------------------------------------- #
def _seed_session(
    conn: sqlite3.Connection,
    session_id: str,
    agent_type: str,
    title: str,
    project_name: str | None,
    workspace: str | None,
    updated_at: str,
) -> None:
    conn.execute(
        "INSERT INTO sessions (id, agent_type, title, project_name, workspace, status,"
        " started_at, last_activity_at, finished_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'completed', 't0', 't0', 't0', 't0', ?)",
        (session_id, agent_type, title, project_name, workspace, updated_at),
    )


def test_the_migration_renames_root_directory_placeholders(tmp_path: Path) -> None:
    """A root is not a project, so `Claude Code · /` becomes `New session` (§39).

    This is the deployed-box half of the rule: the connector stops writing the root as a
    project, but the rows it already wrote stay in the database. The match is deliberately
    narrow — the exact old placeholder *and* a root workspace — so `Claude Code · conference`
    and a prompt-derived name opened from `/` both survive. Renaming a placeholder is not
    activity, so the timestamps are untouched and the board keeps its order.
    """
    path = tmp_path / "old.sqlite3"
    Database(path).close()  # create the current schema

    conn = sqlite3.connect(path)
    _seed_session(conn, "s_posix", "claude_code", "Claude Code · /", "/", "/", "2026-01-01")
    _seed_session(conn, "s_windows", "claude_code", "Claude Code · D:\\", "D:\\", "D:\\",
                  "2026-01-02")
    _seed_session(conn, "s_codex", "codex", "Codex · /", "/", "/", "2026-01-03")
    _seed_session(conn, "s_named", "claude_code", "fix the flaky test", None, "/", "2026-01-04")
    _seed_session(conn, "s_project", "claude_code", "Claude Code · conference", "conference",
                  "/home/me/conference", "2026-01-05")
    _seed_session(conn, "s_already", "claude_code", "New session", None, "/", "2026-01-06")
    _seed_session(conn, "s_null", "claude_code", "Claude Code · /", "/", None, "2026-01-07")
    conn.commit()
    conn.close()

    migrated = Database(path)
    try:
        rows = {row["id"]: row for row in migrated.query("SELECT * FROM sessions")}
    finally:
        migrated.close()

    for session_id in ("s_posix", "s_windows", "s_codex"):
        assert rows[session_id]["title"] == "New session"
        assert rows[session_id]["project_name"] is None
    assert rows["s_posix"]["updated_at"] == "2026-01-01", "renaming is not activity"
    # The name the reader recognises is not a placeholder, and a real placeholder still says
    # which project the row belongs to.
    assert rows["s_named"]["title"] == "fix the flaky test"
    assert rows["s_project"]["title"] == "Claude Code · conference"
    assert rows["s_project"]["project_name"] == "conference"
    # Without a workspace there is nothing to prove the root, so the row is left alone.
    assert rows["s_null"]["title"] == "Claude Code · /"

    # Idempotent by shape: a second boot matches nothing, and already-renamed rows stay put.
    again = Database(path)
    try:
        count = again.query_one(
            "SELECT COUNT(*) AS n FROM sessions WHERE title = 'New session'"
        )["n"]
        assert count == 4
    finally:
        again.close()
