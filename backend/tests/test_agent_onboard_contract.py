"""The onboarding contract: an agent connecting itself to the agent page (docs/design.md §25).

What these tests defend, in one sentence each:

- **The document is generated, not written.** A kind appears in it because its backend module
  declares how its door opens, so the instructions cannot drift from the adapter that speaks
  the protocol.
- **Only loopback can be registered.** That single rule is what makes an endpoint that the
  agent itself created safe to accept without inventing a second credential: there is no
  "somebody else's server" to point the page at.
- **A door that does not open is recorded, not rejected.** The registration succeeded; the
  other machine did not answer. Both facts have to survive into the row, or the user is left
  with an agent that "connected" and a page that shows nothing.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent import endpoints
from app.agent.backends import BackendUnavailable
from app.agent.backends.nanobot import NanobotBackend
from app.database import Database
from app.main import create_app
from app.stream import AgentBroker, Broker
from helpers import TOKEN
from nanobot_door import StubNanobot

ACCESS = "dash-token"
AUTH = {"X-CPD-Token": ACCESS}
BACKENDS = "/api/agent/backends"


def _settings(tmp_path: Path, **overrides):
    base = dict(
        db_path=tmp_path / "test.sqlite3",
        ingest_token=TOKEN,
        dashboard_token=ACCESS,
        host="127.0.0.1",
        port=0,
        static_dir=None,
        stale_after_seconds=900,
        event_payload_max_chars=8000,
        cors_origins=(),
        agent_backend="native",
        pi_workspace=tmp_path / "ws",
        pi_session_dir=tmp_path / "pi-sessions",
        pi_bin=("definitely-not-a-real-binary-xyz",),
    )
    base.update(overrides)
    return __import__("app.config", fromlist=["Settings"]).Settings(**base)


@pytest.fixture()
def app_client(tmp_path: Path):
    settings = _settings(tmp_path)
    (tmp_path / "ws").mkdir(exist_ok=True)
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        client.app_state_db = db  # type: ignore[attr-defined]
        yield client, db, settings
    db.close()
    endpoints.clear()


@contextlib.contextmanager
def _door(*, secret: str | None = None):
    """A live stand-in for a nanobot gateway: the websocket channel and its token door.

    A real socket rather than a monkeypatched probe, because the thing worth testing is that
    the probe actually knocks — a stub that answers "fine" would pass with the client never
    sending a request at all.
    """
    stub = StubNanobot(secret=secret)
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


# --------------------------------------------------------------------------- #
# the document
# --------------------------------------------------------------------------- #
def test_the_document_is_generated_from_the_backends(app_client) -> None:
    """Add a backend module that declares a door and the doc gains a section — no edit here."""
    client, _, _ = app_client
    response = client.get("/api/agent/onboard", headers=AUTH)
    assert response.status_code == 200, response.text
    text = response.text

    assert "nanobot" in text
    assert "127.0.0.1:8765" in text, "the door nanobot already serves, by its default address"
    assert "/webui/bootstrap" in text, "and the route the token comes from"
    assert "/api/agent/backends" in text
    assert "127.0.0.1" in text
    # The facts an agent gets wrong unaided: no token of the user's to find, and the switch is
    # the user's click rather than a call it makes.
    assert "no token" in text.lower(), "registration's credential must be stated"
    assert "Settings → Agent" in text, "say where the user performs the switch"
    assert "Do not" in text and "restart" in text


def test_the_document_is_also_machine_readable(app_client) -> None:
    client, _, _ = app_client
    payload = client.get("/api/agent/onboard.json", headers=AUTH).json()

    assert payload["version"] >= 1
    assert payload["endpoints"]["register"].endswith("/api/agent/backends")
    assert payload["auth"]["header"] == "X-CPD-Token"
    kinds = {entry["kind"] for entry in payload["kinds"]}
    assert "nanobot" in kinds
    entry = next(e for e in payload["kinds"] if e["kind"] == "nanobot")
    assert entry["register"] == {"kind": "nanobot", "url": "http://127.0.0.1:8765"}
    assert any(step.get("shell") for step in entry["steps"])
    json.dumps(payload)  # and it is JSON, which is the whole point of the second rendering


def test_the_document_and_registration_need_no_token(app_client, tmp_path) -> None:
    """An agent walks this path *before* it has any credential, so these two are open.

    Reading how to connect and saying where you are are declarations. The call that decides
    which agent receives the user's words is not, and is asserted below.
    """
    client, _, _ = app_client
    assert client.get("/api/agent/onboard").status_code == 200
    assert client.get("/api/agent/onboard.json").status_code == 200

    from dataclasses import replace

    from app.stream import AgentBroker as AB

    open_settings = replace(_settings(tmp_path), dashboard_token=None, pi_workspace=tmp_path / "ws")
    db = Database(open_settings.db_path)
    app = create_app(settings=open_settings, db=db, broker=Broker(), agent_broker=AB())
    with TestClient(app) as no_token:
        assert no_token.get("/api/agent/onboard").status_code == 200
        with _door() as door:
            assert no_token.post(
                BACKENDS, json={"kind": "nanobot", "url": door.http_url}
            ).status_code == 201
        # With no token configured, acting stays disabled rather than open.
        assert no_token.post(
            "/api/agent/backend", json={"backend": "nanobot"}
        ).status_code == 403
    db.close()


# --------------------------------------------------------------------------- #
# registering a door
# --------------------------------------------------------------------------- #
def test_registering_records_the_row_and_probes_it(app_client) -> None:
    client, db, _ = app_client
    with _door() as door:
        response = client.post(
            BACKENDS,
            json={"kind": "nanobot", "url": door.http_url, "label": "my nanobot"},
            headers=AUTH,
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "nanobot"
    assert body["last_error"] is None, "the probe answered; nothing to report"
    assert body["last_probe_at"] is not None
    assert body["has_token"] is False
    assert "token" not in body, "the token never comes back out"

    listed = client.get(BACKENDS, headers=AUTH).json()["endpoints"]
    assert [row["kind"] for row in listed] == ["nanobot"]


def test_a_shut_door_is_stored_with_the_reason_not_rejected(app_client) -> None:
    """The registration succeeded; the other machine did not answer. Both go in the row."""
    client, _, _ = app_client
    response = client.post(
        BACKENDS,
        json={"kind": "nanobot", "url": "http://127.0.0.1:1"},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["last_error"], "a shut door must say so"
    assert "nanobot gateway" in body["last_error"], "and name the likely cause"

    # Still usable as a choice, and it reports itself as not connected.
    status = client.get("/api/agent/status", headers=AUTH).json()
    row = next(b for b in status["backends"] if b["name"] == "nanobot")
    assert row["ready"] is True, "registered, so it is offered"
    assert row["connected"] is False
    assert row["registered"] is True


def test_re_registering_forgets_the_previous_verdict(app_client) -> None:
    """A new address is a new door; the old failure would read as a failure of this one."""
    client, _, _ = app_client
    client.post(BACKENDS, json={"kind": "nanobot", "url": "http://127.0.0.1:1"}, headers=AUTH)
    assert client.get(BACKENDS, headers=AUTH).json()["endpoints"][0]["last_error"]

    with _door() as door:
        response = client.post(
            BACKENDS, json={"kind": "nanobot", "url": door.http_url}, headers=AUTH
        )
    assert response.json()["last_error"] is None


def test_a_token_is_stored_but_never_returned(app_client) -> None:
    client, _, _ = app_client
    with _door() as door:
        body = client.post(
            BACKENDS,
            json={"kind": "nanobot", "url": door.http_url, "token": "agent-secret"},
            headers=AUTH,
        ).json()
    assert body["has_token"] is True
    assert "agent-secret" not in json.dumps(body)
    assert endpoints.get("nanobot").token == "agent-secret"


def test_an_unknown_kind_is_refused_with_the_list(app_client) -> None:
    client, _, _ = app_client
    response = client.post(
        BACKENDS, json={"kind": "nano bot", "url": "http://127.0.0.1:8900"}, headers=AUTH
    )
    assert response.status_code == 400
    assert "nanobot" in response.json()["detail"], "say what *is* offered"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8900",
        "http://10.0.0.5:8900",
        "https://my-agent.internal",
        "http://0.0.0.0:8900",
    ],
)
def test_only_this_machine_can_be_registered(app_client, url: str) -> None:
    """The one rule that makes a self-registered endpoint safe (§25)."""
    client, _, _ = app_client
    response = client.post(
        BACKENDS, json={"kind": "nanobot", "url": url}, headers=AUTH
    )
    assert response.status_code == 400
    assert "not this machine" in response.json()["detail"]
    assert client.get(BACKENDS, headers=AUTH).json()["endpoints"] == []


@pytest.mark.parametrize("url", ["", "127.0.0.1:8900", "ftp://127.0.0.1", "http://"])
def test_a_malformed_url_is_refused(app_client, url: str) -> None:
    """422 for a missing URL (the schema's business) and 400 for a present-but-wrong one (the
    loopback rule's) — the two are different mistakes and say so differently."""
    client, _, _ = app_client
    status = client.post(
        BACKENDS, json={"kind": "nanobot", "url": url}, headers=AUTH
    ).status_code
    assert status == (422 if url == "" else 400)


def test_loopback_in_all_its_spellings_is_accepted(tmp_path: Path) -> None:
    """`localhost`, `127.0.0.1` and `::1` are the same machine; a name that merely *resolves*
    to loopback is not, because that answer would change with the network."""
    assert endpoints.validate_url("http://127.0.0.1:8900/") == "http://127.0.0.1:8900"
    assert endpoints.validate_url("http://localhost:8900") == "http://localhost:8900"
    assert endpoints.validate_url("http://[::1]:8900") == "http://[::1]:8900"
    with pytest.raises(endpoints.EndpointError):
        endpoints.validate_url("http://mybox.internal:8900")


def test_registration_is_open_but_switching_needs_the_acting_credential(app_client) -> None:
    """Declaring a door is not authorizing it (§24's rule, applied to agent endpoints).

    Any process that can reach this port may say "there is a door at X"; only the page's
    credential may decide that the user's words go to it.
    """
    client, _, _ = app_client
    with _door() as door:
        assert client.post(
            BACKENDS, json={"kind": "nanobot", "url": door.http_url}
        ).status_code == 201
        assert client.post(
            "/api/agent/backend", json={"backend": "nanobot"}
        ).status_code in (401, 403)
        assert client.post(
            "/api/agent/backend",
            json={"backend": "nanobot"},
            headers={"X-CPD-Token": TOKEN},
        ).status_code in (401, 403), "the ingest token is not the page token"


def test_an_open_registration_cannot_move_the_active_door(app_client) -> None:
    """The hole the token used to cover, closed by a rule instead of by a credential.

    Registration is open, and the active backend reads its address per request — so re-declaring
    a *different* URL for the active kind would hand the user's next message to whoever owns that
    local port, with nothing changing on screen. Declaring the same address again stays allowed:
    that is an agent restarting on its port, not a move.
    """
    client, db, _ = app_client
    with _door() as door:
        client.post(BACKENDS, json={"kind": "nanobot", "url": door.http_url})
        assert client.post(
            "/api/agent/backend", json={"backend": "nanobot"}, headers=AUTH
        ).status_code == 200

        moved = client.post(BACKENDS, json={"kind": "nanobot", "url": "http://127.0.0.1:1"})
        assert moved.status_code == 409, moved.text
        assert "active" in moved.json()["detail"]
        assert endpoints.get("nanobot").url == door.http_url, "the address must not have moved"

        again = client.post(BACKENDS, json={"kind": "nanobot", "url": door.http_url})
        assert again.status_code == 201, again.text

        # And once it is not active any more, the move is allowed.
        client.post("/api/agent/backend", json={"backend": "native"}, headers=AUTH)
        assert client.post(
            BACKENDS, json={"kind": "nanobot", "url": "http://127.0.0.1:1"}
        ).status_code == 201


def test_nanobot_has_one_door(app_client) -> None:
    """One agent, one row. The text-only Chat Completions door is gone, not hidden.

    It used to sit next to the websocket one; two rows called "nanobot" read as two agents,
    and the API surface cannot carry a checkpoint. A deployment that still has that name
    selected falls back (the next test), so "removed" does not mean "silently in use".
    """
    client, _, _ = app_client
    names = [b["name"] for b in client.get("/api/agent/status", headers=AUTH).json()["backends"]]
    assert "nanobot" in names
    assert "nanobot_api" not in names, "the removed door must not still be offered"

    refused = client.post("/api/agent/backend", json={"backend": "nanobot_api"}, headers=AUTH)
    assert refused.status_code == 400, refused.text


def test_a_removed_choice_does_not_keep_the_page_on_it(tmp_path: Path) -> None:
    """A persisted selection of a door this build no longer has falls back — the one state
    where a backend that is not offered would be worse than switching away from it, because
    the panel cannot show the thing the page is using."""
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    with db.write():
        db.execute(
            "INSERT INTO meta (key, value) VALUES ('agent_backend', 'nanobot_api') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        assert client.get("/api/agent/status", headers=AUTH).json()["backend"] == "native"
    db.close()
    endpoints.clear()


def test_switching_and_getting_the_conversation_is_one_request(app_client) -> None:
    """The page's switch is a single round trip: switch + session + messages, composed.

    The three primitives remain — session needs the switch, messages need the session, so a
    client cannot overlap them — and on a phone link each round trip is about a second. This
    route is what keeps a tap from reading as "the transcript is slow to sync".
    """
    client, _, _ = app_client
    with _door() as door:
        client.post(BACKENDS, json={"kind": "nanobot", "url": door.http_url})
        response = client.post("/api/agent/switch", json={"backend": "nanobot"}, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["backend"] == "nanobot"
    assert body["thread"]["backend"] == "nanobot"
    assert body["thread"]["messages"] == []

    # The same conversation `/session` resolves — not a second one.
    assert client.post("/api/agent/session", headers=AUTH).json()["id"] == body["thread"]["id"]
    assert client.get("/api/agent/status", headers=AUTH).json()["backend"] == "nanobot"


def test_removing_an_endpoint_is_refused_while_it_is_active(app_client) -> None:
    """Deleting the address out from under the page would leave a broken screen instead of a
    message. Switching away first is one call, and the agent already has it."""
    client, _, _ = app_client
    with _door() as door:
        client.post(BACKENDS, json={"kind": "nanobot", "url": door.http_url}, headers=AUTH)
        switched = client.post("/api/agent/backend", json={"backend": "nanobot"}, headers=AUTH)
        assert switched.status_code == 200, switched.text
        assert switched.json()["backend"] == "nanobot"

        refused = client.delete(f"{BACKENDS}/nanobot", headers=AUTH)
        assert refused.status_code == 409
        assert "switch" in refused.json()["detail"]

        client.post("/api/agent/backend", json={"backend": "native"}, headers=AUTH)
        removed = client.delete(f"{BACKENDS}/nanobot", headers=AUTH)
        assert removed.status_code == 200
        assert removed.json()["endpoints"] == []


def test_removing_something_that_was_never_there_is_404(app_client) -> None:
    client, _, _ = app_client
    assert client.delete(f"{BACKENDS}/nanobot", headers=AUTH).status_code == 404


def test_switching_to_a_door_that_is_down_reports_offline_rather_than_failing(
    app_client,
) -> None:
    """A door that is not answering must not take the page down.

    The websocket backend has a configured address before anything is registered, and its
    socket is opened in the background, so the switch itself always succeeds — what fails is
    `connected`, which the page reports honestly. The first *message* is where it surfaces,
    and it says why.
    """
    client, _, _ = app_client
    response = client.post("/api/agent/backend", json={"backend": "nanobot"}, headers=AUTH)
    assert response.status_code == 200, response.text
    status_body = client.get("/api/agent/status", headers=AUTH).json()
    assert status_body["backend"] == "nanobot"
    row = next(b for b in status_body["backends"] if b["name"] == "nanobot")
    assert row["connected"] is False

    thread_id = client.post("/api/agent/session", headers=AUTH).json()["id"]
    said = client.post(
        f"/api/agent/threads/{thread_id}/messages",
        json={"content": "hello"},
        headers=AUTH,
    )
    assert said.status_code == 503
    assert "nanobot gateway" in said.json()["detail"]

    # Still a working page: switching back is one call and needs nothing from the agent.
    assert client.post("/api/agent/backend", json={"backend": "native"}, headers=AUTH).status_code == 200


# --------------------------------------------------------------------------- #
# the client itself
# --------------------------------------------------------------------------- #
def test_a_registered_door_that_is_shut_is_reported_at_probe_time(tmp_path: Path) -> None:
    """The probe is a real knock, and what it found survives into the row.

    A wrong secret and a shut port are different mistakes — one is a credential to fix, the
    other a process to start — and the messages have to say which.
    """
    import asyncio

    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    endpoints.put(
        db,
        endpoints.AgentEndpoint(kind="nanobot", url="http://127.0.0.1:1"),
    )
    backend = NanobotBackend(
        url=settings.nanobot_ws_url,
        client_id=settings.nanobot_client_id,
        token=None,
        on_event=lambda *_: None,
    )
    with pytest.raises(BackendUnavailable) as excinfo:
        asyncio.run(backend.probe())
    assert "nanobot gateway" in str(excinfo.value)
    assert backend.status().connected is False
    endpoints.clear()
    db.close()
