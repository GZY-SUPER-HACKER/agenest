"""Tests for the rules that must never regress.

Each test names the spec requirement it guards, because these are the places where a
plausible-looking refactor quietly breaks the contract.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

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


def test_close_marks_a_running_session_interrupted(settings, db, broker) -> None:
    """`interrupted`, never `completed` — the honest claim is "stopped, outcome unknown"."""
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token=DASH_TOKEN)
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        sid = _make_session(c)

        body = c.post(f"/api/sessions/{sid}/close", headers=READ).json()
        assert body["status"] == "interrupted"
        assert "Closed from the dashboard" in body["summary"]

        # A human decision should be visible in the timeline, not only in the status field.
        events = c.get(f"/api/sessions/{sid}/events", headers=READ).json()["events"]
        assert any(e["event_type"] == "status_changed" for e in events)


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
# staleness (spec 4.2, item 8)
# --------------------------------------------------------------------------- #
def test_stale_running_session_is_flagged(session_id: str, client: TestClient, db) -> None:
    """A quiet session is labelled, never silently marked failed."""
    db.execute("UPDATE sessions SET last_activity_at = '2020-01-01T00:00:00.000Z' WHERE id = ?", (session_id,))
    detail = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert detail["possibly_stale"] is True
    assert detail["status"] == "running"


def test_finished_session_is_never_stale(session_id: str, client: TestClient, db) -> None:
    report(client, session_id, "session_completed", "done")
    db.execute("UPDATE sessions SET last_activity_at = '2020-01-01T00:00:00.000Z' WHERE id = ?", (session_id,))
    detail = client.get(f"/api/sessions/{session_id}", headers=READ).json()
    assert detail["possibly_stale"] is False
    assert detail["status"] == "completed"


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
    assert body["status"] == "interrupted"
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
