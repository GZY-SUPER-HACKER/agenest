"""The contract for host access (docs/design.md §24).

Three rules here are the ones a plausible refactor breaks quietly, and each has a test below:

- **Declaring is not authorizing.** A connector's report — which rides in on the shared ingest
  token — must never be enough to hand out a shell. Only a row in `host_access`, which requires
  the acting credential, does that.
- **The private key never leaves this side.** No response on any route carries it, and the
  generated machines file names its path rather than its contents.
- **An approval is for one address.** A host that starts declaring a different one drops to
  `address_changed`, so an old command cannot be aimed at a machine that moved.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.main import create_app
from app.services import host_access_service as ha
from helpers import INGEST

HOSTS = "/api/hosts"
DASH = {"X-CPD-Token": "dash-token"}

requires_ssh_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed"
)


@pytest.fixture()
def access_app(settings, db, broker, tmp_path: Path):
    """An app with a token (so the acting routes exist), a workspace, and a key directory."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    locked = replace(
        settings,
        dashboard_token="dash-token",
        pi_workspace=workspace,
        agent_key_dir=tmp_path / "keys",
    )
    with TestClient(create_app(settings=locked, db=db, broker=broker)) as client:
        yield client, locked


def report(client, **extra):
    body = {"label": "lab-box", "agent_type": "claude_code", "connected": True}
    body.update(extra)
    return client.post(HOSTS, json=body, headers=INGEST)


def host(client, index: int = 0) -> dict:
    return client.get(HOSTS, headers=DASH).json()["hosts"][index]


# --------------------------------------------------------------------------- #
# the declaration
# --------------------------------------------------------------------------- #
def test_a_connector_declares_that_it_can_be_driven(client: TestClient) -> None:
    """The declaration is what puts a decision in front of the user — and nothing else."""
    r = report(client, wants_access=True, ssh_host="100.64.1.2", ssh_user="alice", ssh_port=2222)
    assert r.status_code == 200, r.text

    row = host(client)
    assert row["wants_access"] is True
    assert row["access_state"] == "requested", "declared, but nobody has authorized it"
    assert row["access_target"] is None, "no approved address yet"


def test_an_old_connector_reports_nothing_about_access(client: TestClient) -> None:
    """Reported-not-declared is a different fact from declared-and-not-authorized."""
    assert report(client).status_code == 200
    row = host(client)
    assert row["wants_access"] is False
    assert row["access_state"] is None


def test_a_declared_address_is_unverified_and_optional(client: TestClient) -> None:
    """A connector with no address to report must not leave a blank that reads like one."""
    assert report(client, wants_access=True, ssh_host="   ", ssh_user="").status_code == 200
    row = host(client)
    assert row["wants_access"] is True
    assert row["access_state"] == "requested"


def test_a_port_outside_the_range_is_rejected(client: TestClient) -> None:
    assert report(client, ssh_port=0).status_code == 422
    assert report(client, ssh_port=70000).status_code == 422


def test_a_rewritten_declaration_replaces_the_old_one(client: TestClient) -> None:
    """A machine that moves says so on the next heartbeat; the row is one row, not a history."""
    report(client, wants_access=True, ssh_host="old.example", ssh_user="alice")
    report(client, wants_access=True, ssh_host="new.example", ssh_user="alice")
    assert len(client.get(HOSTS, headers=DASH).json()["hosts"]) == 1
    assert host(client)["access_state"] == "requested"


# --------------------------------------------------------------------------- #
# authorization: the person's half
# --------------------------------------------------------------------------- #
def test_authorizing_requires_the_acting_credential(access_app) -> None:
    """Handing out a way in is an action, so the ingest token must not be enough — and neither
    is an unauthenticated request."""
    client, _ = access_app
    report(client, wants_access=True, ssh_host="box", ssh_user="alice")
    host_id = host(client)["id"]

    assert client.post(f"{HOSTS}/{host_id}/access", json={}).status_code == 401
    assert (
        client.post(f"{HOSTS}/{host_id}/access", json={}, headers=INGEST).status_code == 401
    )


def test_authorizing_is_refused_when_no_access_token_is_configured(
    settings, db, broker, tmp_path: Path
) -> None:
    """`access_header`, not `dashboard_header`: with no token set, reads stay open and acting is
    switched off rather than handed out."""
    open_settings = replace(settings, dashboard_token=None, pi_workspace=tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    with TestClient(create_app(settings=open_settings, db=db, broker=broker)) as client:
        report(client, wants_access=True, ssh_host="box", ssh_user="alice")
        host_id = client.get(HOSTS).json()["hosts"][0]["id"]
        assert client.post(f"{HOSTS}/{host_id}/access", json={}).status_code == 403


@requires_ssh_keygen
def test_authorizing_mints_a_key_that_stays_on_this_side(access_app) -> None:
    client, settings = access_app
    report(client, wants_access=True, ssh_host="100.64.1.2", ssh_user="alice")
    host_id = host(client)["id"]

    r = client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host"]["access_state"] == "pending", "the host installs it, not us"
    assert body["public_key"].startswith("ssh-ed25519 ")
    assert "PRIVATE KEY" not in r.text

    # The private half exists on disk, at 0600, under the configured directory.
    key_files = sorted((settings.agent_key_dir).glob("csd_*"))
    private = [p for p in key_files if p.suffix != ".pub"]
    assert len(private) == 1
    assert private[0].read_text(encoding="utf-8").startswith("-----BEGIN OPENSSH PRIVATE KEY")


@requires_ssh_keygen
def test_the_connector_is_told_the_key_but_never_the_path(access_app) -> None:
    """A connector has no use for where the dashboard keeps its private half, so it is not sent
    — a field that is never transmitted cannot be logged by accident."""
    client, _ = access_app
    report(client, wants_access=True, ssh_host="100.64.1.2", ssh_user="alice")
    host_id = host(client)["id"]
    client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)

    r = client.get(
        f"{HOSTS}/access?label=lab-box&agent_type=claude_code", headers=INGEST
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["state"] == "pending"
    assert payload["public_key"].startswith("ssh-ed25519 ")
    assert "key_path" not in payload
    assert "PRIVATE" not in r.text


def test_the_connector_routes_refuse_the_page_credential(access_app) -> None:
    """The other direction: what a *host* presents is not what the page presents."""
    client, _ = access_app
    report(client, wants_access=True, ssh_host="box", ssh_user="alice")
    assert (
        client.get(f"{HOSTS}/access?label=lab-box&agent_type=claude_code", headers=DASH).status_code
        == 401
    )
    assert (
        client.post(
            f"{HOSTS}/access/installed",
            json={"label": "lab-box", "agent_type": "claude_code"},
            headers=DASH,
        ).status_code
        == 401
    )


@requires_ssh_keygen
def test_the_host_reports_the_key_went_in_and_then_it_is_ready(access_app) -> None:
    client, _ = access_app
    report(client, wants_access=True, ssh_host="100.64.1.2", ssh_user="alice")
    host_id = host(client)["id"]
    client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)

    r = client.post(
        f"{HOSTS}/access/installed",
        json={"label": "lab-box", "agent_type": "claude_code", "installed": True},
        headers=INGEST,
    )
    assert r.status_code == 200, r.text
    row = host(client)
    assert row["access_state"] == "ready"
    assert row["access_target"] == "alice@100.64.1.2:22"


@requires_ssh_keygen
def test_a_failed_install_keeps_the_host_pending_and_says_why(access_app) -> None:
    """A state that flipped to `ready` on a report we did not get would be a lie the agent acts
    on, so the failure is recorded and the host stays where it was."""
    client, _ = access_app
    report(client, wants_access=True, ssh_host="box", ssh_user="alice")
    host_id = host(client)["id"]
    client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)

    client.post(
        f"{HOSTS}/access/installed",
        json={
            "label": "lab-box",
            "agent_type": "claude_code",
            "installed": False,
            "error": "authorized_keys is not writable",
        },
        headers=INGEST,
    )
    assert host(client)["access_state"] == "pending"


@requires_ssh_keygen
def test_a_host_that_starts_declaring_somewhere_else_loses_the_approval(access_app) -> None:
    """The approved command is for one address. A machine that moves must not keep it."""
    client, settings = access_app
    report(client, wants_access=True, ssh_host="old.example", ssh_user="alice")
    host_id = host(client)["id"]
    client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)
    client.post(
        f"{HOSTS}/access/installed",
        json={"label": "lab-box", "agent_type": "claude_code", "installed": True},
        headers=INGEST,
    )
    assert host(client)["access_state"] == "ready"

    report(client, wants_access=True, ssh_host="new.example", ssh_user="alice")
    changed = host(client)
    assert changed["access_state"] == "address_changed"
    assert changed["access_target"] == "alice@old.example:22", "the approved one, not the new one"

    # Re-approving settles it on the new address, and the key does not have to be reinstalled:
    # the host did not move, the address it *claims* did.
    db = Database(settings.db_path)
    try:
        key_before = ha.get_row_for_label(db, "lab-box", "claude_code")["key_path"]
    finally:
        db.close()

    r = client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)
    assert r.status_code == 200, r.text
    assert host(client)["access_state"] == "ready"
    assert host(client)["access_target"] == "alice@new.example:22"

    db = Database(settings.db_path)
    try:
        assert ha.get_row_for_label(db, "lab-box", "claude_code")["key_path"] == key_before
    finally:
        db.close()


@requires_ssh_keygen
def test_revoking_deletes_the_key_and_tells_the_host_to_remove_it(access_app) -> None:
    client, settings = access_app
    report(client, wants_access=True, ssh_host="box", ssh_user="alice")
    host_id = host(client)["id"]
    client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)
    key_path = Path(
        ha.get_row_for_label(Database(settings.db_path), "lab-box", "claude_code")["key_path"]
    )
    assert key_path.exists()

    r = client.delete(f"{HOSTS}/{host_id}/access", headers=DASH)
    assert r.status_code == 200, r.text
    assert r.json()["access_state"] == "disabled"
    assert not key_path.exists(), "the private half must not outlive the approval"
    assert not Path(f"{key_path}.pub").exists()

    payload = client.get(
        f"{HOSTS}/access?label=lab-box&agent_type=claude_code", headers=INGEST
    ).json()
    assert payload["state"] == "disabled"
    assert payload["public_key"] is None


# --------------------------------------------------------------------------- #
# the generated machines file the agent reads
# --------------------------------------------------------------------------- #
@requires_ssh_keygen
def test_the_generated_file_gives_the_agent_the_command_but_not_the_key(access_app) -> None:
    client, settings = access_app
    report(client, wants_access=True, ssh_host="100.64.1.2", ssh_user="alice")
    host_id = host(client)["id"]
    client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)
    client.post(
        f"{HOSTS}/access/installed",
        json={"label": "lab-box", "agent_type": "claude_code", "installed": True},
        headers=INGEST,
    )

    registry = settings.resolved_registry_file
    assert registry is not None and registry.is_file()
    text = registry.read_text(encoding="utf-8")
    assert "ssh -i " in text
    assert "alice@100.64.1.2" in text
    # The path, never the key: neither half of it belongs in a file the agent reads.
    assert "PRIVATE KEY" not in text
    assert "ssh-ed25519 " not in text


def test_the_generated_file_says_so_when_nothing_is_authorized(access_app) -> None:
    """The common case has to be a sentence, not a missing file: the appendix points at it."""
    client, settings = access_app
    report(client, wants_access=True, ssh_host="box")
    registry = settings.resolved_registry_file
    assert registry is not None
    text = registry.read_text(encoding="utf-8")
    assert "not authorized" in text
    assert "ssh -i" not in text, "no command for a host nobody approved"


def test_a_host_that_never_asked_is_listed_without_an_invitation(access_app) -> None:
    client, settings = access_app
    report(client)
    text = settings.resolved_registry_file.read_text(encoding="utf-8")
    assert "lab-box" in text
    assert "not requested" in text


# --------------------------------------------------------------------------- #
# the wire and the migration
# --------------------------------------------------------------------------- #
def test_host_frame_carries_the_access_fields() -> None:
    """Same rule as every other frame: a field omitted here is a field erased from the screen."""
    from app.schemas import HostOut
    from app.stream import host_frame

    payload = host_frame(
        HostOut(
            id="h_1",
            label="lab-box",
            agent_type="claude_code",
            connected_at="t",
            last_seen_at="t",
            wants_access=True,
            access_state="pending",
            access_target="alice@box:22",
            access_fingerprint="SHA256:abc",
        )
    )["host"]
    assert payload["wants_access"] is True
    assert payload["access_state"] == "pending"
    assert payload["access_target"] == "alice@box:22"
    assert payload["access_fingerprint"] == "SHA256:abc"


def test_an_old_database_gains_the_declaration_columns(tmp_path: Path) -> None:
    """The deploy in miniature: `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so
    a column added to it never appears — and the box with the old shape is the one that matters.
    """
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE agent_hosts (
            id              TEXT PRIMARY KEY,
            label           TEXT NOT NULL,
            agent_type      TEXT NOT NULL,
            connected_at    TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            disconnected_at TEXT,
            UNIQUE (label, agent_type)
        );
        """
    )
    old.execute(
        "INSERT INTO agent_hosts VALUES ('h1', 'lab-box', 'claude_code', 't', 't', NULL)"
    )
    old.commit()
    old.close()

    migrated = Database(path)
    try:
        columns = {row["name"] for row in migrated.query("PRAGMA table_info(agent_hosts)")}
        assert {"wants_access", "ssh_host", "ssh_user", "ssh_port"} <= columns
        row = migrated.query_one("SELECT * FROM agent_hosts WHERE id = 'h1'")
        # A row written by an older connector reads as "did not ask", not as "unknown".
        assert row["wants_access"] == 0
        assert row["ssh_port"] == 22
        assert migrated.query("PRAGMA table_info(host_access)"), "the new table exists too"
    finally:
        migrated.close()

    # Idempotent: a second boot must not try to add the columns again and fail.
    again = Database(path)
    try:
        assert "wants_access" in {
            row["name"] for row in again.query("PRAGMA table_info(agent_hosts)")
        }
    finally:
        again.close()


def test_no_ssh_keygen_is_a_readable_refusal(access_app, monkeypatch) -> None:
    """A deployment without OpenSSH should be told so, not handed a 500."""
    client, _ = access_app
    monkeypatch.setattr(ha.shutil, "which", lambda _name: None)
    report(client, wants_access=True, ssh_host="box", ssh_user="alice")
    host_id = host(client)["id"]
    r = client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)
    assert r.status_code == 400
    assert "ssh-keygen" in r.json()["detail"]


def test_a_host_that_declared_no_user_cannot_be_approved(access_app) -> None:
    """Refusing beats inventing: guessing `root` would aim a shell at an account nobody named."""
    client, _ = access_app
    report(client, wants_access=True, ssh_host="box")
    host_id = host(client)["id"]
    r = client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)
    assert r.status_code == 400
    assert "ssh user" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# the tool the agent calls on demand
# --------------------------------------------------------------------------- #
def _running_session(client, **extra):
    body = {
        "agent_type": "claude_code",
        "title": "refactor auth",
        "project_name": "app",
        "workspace": "/home/me/app",
        "host": "lab-box",
    }
    body.update(extra)
    r = client.post("/api/sessions", json=body, headers=INGEST)
    assert r.status_code == 201, r.text


def test_the_tool_lists_hosts_with_their_access_state(access_app) -> None:
    """The file is the standing answer; this is the one a model asks when it needs it *now*."""
    from app.agent import hosts_view

    client, settings = access_app
    report(client, wants_access=True, ssh_host="100.64.1.2", ssh_user="alice")

    text = hosts_view.render_tool_text(Database(settings.db_path), action="hosts")
    assert "lab-box" in text
    assert "requested" in text
    assert "ssh -i" not in text, "no command for a host nobody approved"


@requires_ssh_keygen
def test_the_tool_gives_the_command_once_access_is_ready(access_app) -> None:
    from app.agent import hosts_view

    client, settings = access_app
    report(client, wants_access=True, ssh_host="100.64.1.2", ssh_user="alice")
    host_id = host(client)["id"]
    client.post(f"{HOSTS}/{host_id}/access", json={}, headers=DASH)
    client.post(
        f"{HOSTS}/access/installed",
        json={"label": "lab-box", "agent_type": "claude_code", "installed": True},
        headers=INGEST,
    )

    text = hosts_view.render_tool_text(Database(settings.db_path), action="hosts")
    assert "ssh -i " in text
    assert "alice@100.64.1.2" in text
    assert "PRIVATE KEY" not in text


def test_the_tool_lists_what_is_running_and_where(access_app) -> None:
    """A session's workspace is what tells the agent where to `cd` on the machine."""
    from app.agent import hosts_view

    client, settings = access_app
    _running_session(client)

    text = hosts_view.render_tool_text(Database(settings.db_path), action="sessions")
    assert "refactor auth" in text
    assert "lab-box" in text
    assert "/home/me/app" in text


def test_the_tool_says_nothing_is_running_rather_than_nothing(access_app) -> None:
    from app.agent import hosts_view

    _, settings = access_app
    text = hosts_view.render_tool_text(Database(settings.db_path), action="sessions")
    assert "No running sessions" in text


def test_the_tool_reads_without_being_able_to_write(access_app, tmp_path: Path) -> None:
    """Read-only is the point: the agent's process must not be able to change the record, and it
    must not run the schema/migration on its way in either."""
    from app.agent import hosts_view

    client, settings = access_app
    report(client, wants_access=True, ssh_host="box", ssh_user="alice")

    before = settings.db_path.read_bytes()
    db = Database.open_read_only(settings.db_path)
    try:
        assert "lab-box" in hosts_view.render_tool_text(db, action="hosts")
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM agent_hosts")
    finally:
        db.close()
    assert settings.db_path.read_bytes() == before


def test_the_tool_cli_prints_a_sentence_instead_of_a_traceback(tmp_path: Path, capsys) -> None:
    """A model cannot act on a stack trace, and this runs inside a turn the user is waiting on."""
    from app.agent import hosts_view

    assert hosts_view.main(["--db", str(tmp_path / "nope.sqlite3")]) == 0
    assert "could not open" in capsys.readouterr().out


def test_the_user_can_override_the_declared_address(access_app, monkeypatch) -> None:
    """The declared address is unverified, so approving is where a person corrects it."""
    client, _ = access_app
    monkeypatch.setattr(ha, "_generate_keypair", lambda *a, **k: (Path("/tmp/k"), "ssh-ed25519 AAA k", ""))
    report(client, wants_access=True, ssh_host="wrong.example", ssh_user="alice")
    host_id = host(client)["id"]
    r = client.post(
        f"{HOSTS}/{host_id}/access",
        json={"ssh_host": "right.example", "ssh_port": 2222},
        headers=DASH,
    )
    assert r.status_code == 200, r.text
    row = host(client)
    assert row["access_target"] == "alice@right.example:2222"
    # Overriding is not the host moving, so the approval stands.
    assert row["access_state"] == "pending"
