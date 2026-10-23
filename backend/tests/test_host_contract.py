"""The host registry's contract.

The rules here are the ones a plausible refactor breaks quietly. Two of them are load-bearing
enough to name up front:

- **A host is reported, never inferred.** Nothing derives a host from a session, and a host
  that stops talking is never marked disconnected — it goes `possibly_stale`, which is the
  honest rendering of a question nobody can ask any more.
- **The migration is the deploy.** `executescript(SCHEMA)` is all `CREATE TABLE IF NOT
  EXISTS`, so an existing database keeps its old `sessions` shape and silently has no `host`
  column. A fresh dev file always has it, which is precisely why the failure only ever shows
  up on the deployed box.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import Database
from app.schemas import HostOut
from app.stream import host_frame
from helpers import INGEST, READ, TOKEN

HOSTS = "/api/hosts"


def report_host(
    c: TestClient,
    *,
    label: str = "lab-box",
    agent_type: str = "claude_code",
    connected: bool = True,
):
    return c.post(
        HOSTS,
        json={"label": label, "agent_type": agent_type, "connected": connected},
        headers=INGEST,
    )


def list_hosts(c: TestClient, headers=READ):
    return c.get(HOSTS, headers=headers).json()["hosts"]


# --------------------------------------------------------------------------- #
# reporting and listing
# --------------------------------------------------------------------------- #
def test_a_reported_host_is_listed(client: TestClient) -> None:
    r = report_host(client)
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "lab-box"
    assert r.json()["agent_type"] == "claude_code"
    assert r.json()["disconnected_at"] is None

    hosts = list_hosts(client)
    assert len(hosts) == 1
    assert hosts[0]["label"] == "lab-box"


def test_reporting_again_is_one_row_not_two(client: TestClient) -> None:
    """The connector reports on every heartbeat. A row per ping would make "which hosts are
    connected" unanswerable, which is the only question the table exists for."""
    for _ in range(5):
        assert report_host(client).status_code == 200
    assert len(list_hosts(client)) == 1


def test_one_box_with_two_coding_agents_is_two_hosts(client: TestClient) -> None:
    """The key is `(label, agent_type)`, not `label`.

    A connector speaks for exactly one coding agent. A box running both Claude Code and codex
    connects twice, through two connectors, and conflating them would make the second
    connection look like a refresh of the first — and lose the fact that there are two.
    """
    report_host(client, agent_type="claude_code")
    report_host(client, agent_type="codex")
    hosts = list_hosts(client)
    assert {(h["label"], h["agent_type"]) for h in hosts} == {
        ("lab-box", "claude_code"),
        ("lab-box", "codex"),
    }


def test_two_boxes_are_two_hosts(client: TestClient) -> None:
    report_host(client, label="lab-box")
    report_host(client, label="gpu-02")
    assert {h["label"] for h in list_hosts(client)} == {"lab-box", "gpu-02"}


# --------------------------------------------------------------------------- #
# connected_at means "connected since", and only that
# --------------------------------------------------------------------------- #
def test_connected_at_does_not_move_on_a_ping(client: TestClient, db) -> None:
    report_host(client)
    db.execute(
        "UPDATE agent_hosts SET connected_at = '2020-01-01T00:00:00.000Z'"
    )

    report_host(client)  # an ordinary heartbeat from an already-connected host

    assert list_hosts(client)[0]["connected_at"] == "2020-01-01T00:00:00.000Z"


def test_connected_at_moves_on_a_real_reconnect(client: TestClient, db) -> None:
    """Disconnect then come back is a new connection, and the pill should say so."""
    report_host(client)
    db.execute(
        "UPDATE agent_hosts SET connected_at = '2020-01-01T00:00:00.000Z'"
    )

    report_host(client, connected=False)
    assert list_hosts(client)[0]["disconnected_at"] is not None

    report_host(client, connected=True)
    host = list_hosts(client)[0]
    assert host["disconnected_at"] is None
    assert host["connected_at"] != "2020-01-01T00:00:00.000Z"


def test_a_disconnected_host_says_so(client: TestClient) -> None:
    report_host(client)
    report_host(client, connected=False)
    host = list_hosts(client)[0]
    # Reported, not deleted: "this host left" is a fact worth keeping, and deleting the row
    # would make it indistinguishable from a host that was never here.
    assert host["disconnected_at"] is not None


# --------------------------------------------------------------------------- #
# staleness — the one inference, and where it is allowed to apply
# --------------------------------------------------------------------------- #
def test_a_quiet_host_goes_possibly_stale(client: TestClient, db) -> None:
    report_host(client)
    db.execute("UPDATE agent_hosts SET last_seen_at = '2020-01-01T00:00:00.000Z'")
    assert list_hosts(client)[0]["possibly_stale"] is True


def test_a_fresh_host_is_not_stale(client: TestClient) -> None:
    report_host(client)
    assert list_hosts(client)[0]["possibly_stale"] is False


def test_a_disconnected_host_is_never_stale(client: TestClient, db) -> None:
    """A reported state must not wear the rendering reserved for a guess.

    A host that said goodbye is not "maybe offline" — it is offline, and it said so. The same
    rule the dashboard applies to session statuses (design.md §2) applies at this scale.
    """
    report_host(client, connected=False)
    db.execute("UPDATE agent_hosts SET last_seen_at = '2020-01-01T00:00:00.000Z'")
    assert list_hosts(client)[0]["possibly_stale"] is False


# --------------------------------------------------------------------------- #
# auth and validation
# --------------------------------------------------------------------------- #
def test_reporting_requires_the_ingest_token(client: TestClient) -> None:
    r = client.post(
        HOSTS, json={"label": "lab-box", "agent_type": "claude_code"}
    )
    assert r.status_code == 401


def test_listing_requires_the_dashboard_token_when_one_is_set(
    settings, db, broker
) -> None:
    from dataclasses import replace

    from app.main import create_app

    locked = replace(settings, dashboard_token="must-match")
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as c:
        assert c.get(HOSTS).status_code == 401
        assert c.get(HOSTS, headers={"X-CPD-Token": "must-match"}).status_code == 200


def test_listing_is_open_without_a_dashboard_token(client: TestClient) -> None:
    assert client.get(HOSTS).status_code == 200


def test_a_blank_label_is_rejected(client: TestClient) -> None:
    """A host with no name could not be told apart from any other, and the UI groups on it."""
    assert report_host(client, label="   ").status_code == 422


def test_an_unknown_field_is_rejected(client: TestClient) -> None:
    r = client.post(
        HOSTS,
        json={"label": "lab-box", "agent_type": "claude_code", "stale": True},
        headers=INGEST,
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# the wire: frames and the session's own host field
# --------------------------------------------------------------------------- #
def test_host_frame_carries_every_field_the_client_type_declares() -> None:
    """Same failure the session frame had: the hook replaces its copy wholesale, so a field
    omitted from the frame is a field erased from the screen."""
    host = HostOut(
        id="h_1",
        label="lab-box",
        agent_type="claude_code",
        connected_at="t",
        last_seen_at="t",
    )
    payload = host_frame(host)["host"]
    assert payload["label"] == "lab-box"
    assert payload["agent_type"] == "claude_code"
    assert payload["connected_at"] == "t"
    assert payload["last_seen_at"] == "t"
    assert payload["disconnected_at"] is None
    assert payload["possibly_stale"] is False


def test_a_session_carries_the_host_it_ran_on(client: TestClient) -> None:
    r = client.post(
        "/api/sessions",
        json={"agent_type": "claude_code", "title": "t", "host": "lab-box"},
        headers=INGEST,
    )
    assert r.status_code == 201, r.text
    assert r.json()["host"] == "lab-box"
    listed = client.get("/api/sessions", headers=READ).json()["sessions"][0]
    assert listed["host"] == "lab-box"


def test_a_session_with_no_host_stays_null(client: TestClient, session_id: str) -> None:
    """Null is a real answer: a reporter that was never told a host must not invent one, and
    the UI groups those rows separately rather than attributing them to a guess."""
    listed = client.get("/api/sessions", headers=READ).json()["sessions"][0]
    assert listed["host"] is None


def test_old_sessions_keep_their_place_after_the_migration(tmp_path: Path) -> None:
    """An existing database gains the column with its rows intact and `host` empty.

    This is the deploy in miniature. A fresh dev database gets `host` from `CREATE TABLE`, so
    nothing local ever exercises the `ALTER TABLE` path — and on the deployed box it is the
    only path there is.
    """
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE sessions (
            id                TEXT PRIMARY KEY,
            agent_type        TEXT NOT NULL,
            title             TEXT NOT NULL,
            project_name      TEXT,
            workspace         TEXT,
            status            TEXT NOT NULL DEFAULT 'running',
            started_at        TEXT NOT NULL,
            last_activity_at  TEXT NOT NULL,
            finished_at       TEXT,
            summary           TEXT,
            exit_code         INTEGER,
            heartbeat_count   INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        """
    )
    old.execute(
        "INSERT INTO sessions (id, agent_type, title, started_at, last_activity_at,"
        " created_at, updated_at) VALUES ('s_old', 'codex', 'pre-host row', 't', 't', 't', 't')"
    )
    old.commit()
    old.close()

    migrated = Database(path)
    try:
        columns = {row["name"] for row in migrated.query("PRAGMA table_info(sessions)")}
        assert "host" in columns
        row = migrated.query_one("SELECT * FROM sessions WHERE id = 's_old'")
        assert row["host"] is None
        # The rest of the row is untouched: this migration adds, it does not rebuild.
        assert row["title"] == "pre-host row"
        assert row["agent_type"] == "codex"
    finally:
        migrated.close()

    # Idempotent — a second boot must not try to add the column again and fail.
    again = Database(path)
    try:
        assert "host" in {row["name"] for row in again.query("PRAGMA table_info(sessions)")}
    finally:
        again.close()
