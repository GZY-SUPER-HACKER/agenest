"""Attachments: the contract of the files a conversation carries (docs/design.md §29).

No agent process and no network. The backend here is a stub that records what it is sent and
replays it, because what needs pinning is this service's side: that an upload lands on disk with
a safe name, that the agent is handed a path rather than a binary, that the marker disappears
from what the page reads, that the outbox delivers a file exactly once, and that every byte is
behind the same credential as the agent itself.

The one thing these tests cannot prove is that a real agent reads the file. That is the agent's
half of the contract, and the path is the whole interface.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from app.agent import attachments as attachment_store
from app.agent import store as thread_store
from app.agent.backends.base import BackendStatus, Message
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.schemas import AgentRole
from app.stream import AgentBroker, Broker
from fastapi.testclient import TestClient

ACCESS_TOKEN = "test-access-token"
HEADERS = {"X-CPD-Token": ACCESS_TOKEN}
MB = 1024 * 1024


class StubBackend:
    """An agent that remembers. Everything the supervisor asks of a backend, and nothing else."""

    name = "stub"

    def __init__(self) -> None:
        self.conversation = "c_stub"
        self.sent: list[tuple[str, str]] = []
        self.messages: dict[str, list[Message]] = {}

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def new_conversation(self) -> str:
        return self.conversation

    async def attach(self, conversation_id: str) -> None:
        return

    async def send(self, conversation_id: str, text: str) -> Message:
        self.sent.append((conversation_id, text))
        message = Message(
            role=AgentRole.user,
            content=text,
            id=f"u_{len(self.sent)}",
            created_at="2026-01-01T00:00:00.000Z",
        )
        self.messages.setdefault(conversation_id, []).append(message)
        return message

    async def history(self, conversation_id: str) -> list[Message]:
        return list(self.messages.get(conversation_id, []))

    async def abort(self, conversation_id: str) -> bool:
        return False

    def is_live(self, conversation_id: str) -> bool:
        return True

    def is_streaming(self, conversation_id: str) -> bool:
        return False

    def is_available(self) -> bool:
        return True

    def status(self) -> BackendStatus:
        return BackendStatus(name=self.name, connected=True)


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        db_path=tmp_path / "test.sqlite3",
        ingest_token="test-ingest-token",
        dashboard_token=ACCESS_TOKEN,
        host="127.0.0.1",
        port=0,
        static_dir=None,
        stale_after_seconds=900,
        event_payload_max_chars=8000,
        cors_origins=(),
        agent_backend="stub",
        attachment_max_bytes=25 * MB,
    )
    values.update(overrides)
    return Settings(**values)


def _open(tmp_path: Path, **overrides):
    settings = _settings(tmp_path, **overrides)
    db = Database(settings.db_path)
    backend = StubBackend()
    app = create_app(
        settings=settings,
        db=db,
        broker=Broker(),
        agent_broker=AgentBroker(),
        backend=backend,
    )
    return settings, db, backend, app


@pytest.fixture()
def attachment_app(tmp_path: Path):
    settings, db, backend, app = _open(tmp_path)
    with TestClient(app) as client:
        yield client, db, settings, backend, app
    db.close()


def _thread(client: TestClient) -> str:
    response = client.post("/api/agent/session", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _upload(
    client: TestClient,
    thread_id: str,
    name: str,
    data: bytes,
    mime: str = "application/octet-stream",
    token: str | None = ACCESS_TOKEN,
):
    headers = {"Content-Type": mime, "X-CSD-Filename": quote(name, safe="")}
    if token is not None:
        headers["X-CPD-Token"] = token
    return client.post(
        f"/api/agent/threads/{thread_id}/attachments", content=data, headers=headers
    )


def _send(client: TestClient, thread_id: str, content: str, ids: list[str]):
    return client.post(
        f"/api/agent/threads/{thread_id}/messages",
        json={"content": content, "attachment_ids": ids},
        headers=HEADERS,
    )


def _conversation(client: TestClient, thread_id: str) -> list[dict]:
    response = client.get(f"/api/agent/threads/{thread_id}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["messages"]


# --------------------------------------------------------------------------- #
# upload
# --------------------------------------------------------------------------- #
def test_an_upload_is_stored_with_its_metadata_and_a_safe_name(attachment_app, tmp_path):
    client, _db, settings, _backend, _app = attachment_app
    thread_id = _thread(client)

    response = _upload(client, thread_id, "报告 final.pdf", b"pdf-bytes", "application/pdf")
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["id"].startswith("a_")
    assert body["name"] == "报告 final.pdf"
    assert body["mime"] == "application/pdf"
    assert body["size"] == len(b"pdf-bytes")
    assert body["direction"] == "in"

    row = attachment_store.get(_db, body["id"])
    assert row is not None
    stored = Path(row["path"])
    assert stored.is_file()
    assert stored.read_bytes() == b"pdf-bytes"
    # The id prefixes the filename on disk, so two files called `notes.txt` cannot collide.
    assert stored.name.endswith("-报告 final.pdf")
    assert settings.resolved_attachments_dir in stored.parents


def test_a_hostile_filename_cannot_leave_the_attachment_directory(attachment_app):
    client, _db, settings, _backend, _app = attachment_app
    thread_id = _thread(client)

    response = _upload(client, thread_id, "../../etc/passwd", b"x")
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "passwd"
    row = attachment_store.get(_db, response.json()["id"])
    stored = Path(row["path"]).resolve()
    assert settings.resolved_attachments_dir.resolve() in stored.parents

    # Windows separators and reserved characters are treated the same way: the basename
    # survives, everything that could mean "another directory" or "another stream" does not.
    response = _upload(client, thread_id, "..\\..\\evil:name.py", b"x")
    assert response.status_code == 201, response.text
    assert "/" not in response.json()["name"]
    assert "\\" not in response.json()["name"]
    assert ":" not in response.json()["name"]


def test_an_upload_needs_the_access_token(attachment_app):
    client, _db, _settings_, _backend, _app = attachment_app
    thread_id = _thread(client)

    assert _upload(client, thread_id, "a.txt", b"x", token=None).status_code == 401
    assert _upload(client, thread_id, "a.txt", b"x", token="wrong").status_code == 401


def test_acting_is_403_when_the_server_has_no_token(tmp_path: Path):
    _settings_, db, _backend, app = _open(tmp_path, dashboard_token=None)
    with TestClient(app) as client:
        response = _upload(client, "t_whatever", "a.txt", b"x", token=None)
        assert response.status_code == 403
        assert "no access token" in response.json()["detail"]
    db.close()


def test_an_upload_over_the_limit_is_refused_and_leaves_nothing_behind(tmp_path: Path):
    settings, db, _backend, app = _open(tmp_path, attachment_max_bytes=1024)
    with TestClient(app) as client:
        thread_id = _thread(client)
        response = _upload(client, thread_id, "big.bin", b"x" * 2048)
        assert response.status_code == 413, response.text
        assert "larger than" in response.json()["detail"]

        incoming = settings.resolved_attachments_dir / "in"
        leftovers = list(incoming.rglob("*")) if incoming.is_dir() else []
        assert [path for path in leftovers if path.is_file()] == []
    db.close()


def test_an_upload_for_an_unknown_thread_is_404(attachment_app):
    client, _db, _settings_, _backend, _app = attachment_app
    assert _upload(client, "t_missing", "a.txt", b"x").status_code == 404


# --------------------------------------------------------------------------- #
# sending
# --------------------------------------------------------------------------- #
def test_a_file_travels_as_a_path_and_comes_back_as_a_card(attachment_app):
    """The agent gets a path in the text; the page gets a card and clean words.

    This is the whole mechanism in one test: the marker is the instruction, the row is the
    identity, and neither leaking into the other's half is the contract.
    """
    client, _db, _settings_, backend, _app = attachment_app
    thread_id = _thread(client)
    uploaded = _upload(client, thread_id, "shot.png", b"png", "image/png").json()

    response = _send(client, thread_id, "look at this", [uploaded["id"]])
    assert response.status_code == 202, response.text
    row = response.json()
    assert row["content"] == "look at this"
    assert [a["id"] for a in row["attachments"]] == [uploaded["id"]]

    _conversation_id, text = backend.sent[-1]
    # Markers first: a backend's 2000-char cap truncates the tail, and a long prompt must not
    # be able to truncate away the file the agent was handed (see `compose_prompt`).
    assert text.startswith(f"[attachment {uploaded['id']}] shot.png: ")
    assert "\n\nlook at this\n\n" in text
    assert _settings_.resolved_attachments_dir.name in text

    history = _conversation(client, thread_id)
    mine = [m for m in history if m["role"] == "user"]
    assert len(mine) == 1
    assert mine[0]["content"] == "look at this", "the marker leaked into the transcript"
    assert [a["name"] for a in mine[0]["attachments"]] == ["shot.png"]


def test_an_agent_quoting_a_marker_keeps_its_words(attachment_app):
    """Markers are read only on user messages.

    An agent discussing the file may quote the line. The id resolves — it is our id — but the
    reply is not the message that carried the file, so attaching a card to it and deleting its
    sentence would be a misattribution.
    """
    client, _db, _settings_, backend, _app = attachment_app
    thread_id = _thread(client)
    uploaded = _upload(client, thread_id, "shot.png", b"png", "image/png").json()
    assert _send(client, thread_id, "look", [uploaded["id"]]).status_code == 202

    marker = f"[attachment {uploaded['id']}] shot.png: /tmp/shot.png"
    backend.messages[backend.conversation].append(
        Message(
            role=AgentRole.assistant,
            content=f"I received:\n\n{marker}",
            id="a_quote",
            created_at="2026-01-01T00:01:00.000Z",
        )
    )

    reply = [m for m in _conversation(client, thread_id) if m["id"] == "a_quote"][0]
    assert marker in reply["content"]
    assert reply["attachments"] == []


def test_a_file_with_no_caption_is_a_message(attachment_app):
    client, _db, _settings_, backend, _app = attachment_app
    thread_id = _thread(client)
    uploaded = _upload(client, thread_id, "photo.jpg", b"jpg", "image/jpeg").json()

    response = _send(client, thread_id, "", [uploaded["id"]])
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["content"] == ""
    assert [a["id"] for a in body["attachments"]] == [uploaded["id"]]
    # A captionless message still names the file to the agent; the path is the message.
    assert f"[attachment {uploaded['id']}]" in backend.sent[-1][1]


def test_an_attachment_from_another_conversation_is_refused(attachment_app):
    client, db, _settings_, _backend, _app = attachment_app
    mine = _thread(client)
    uploaded = _upload(client, mine, "a.txt", b"x").json()

    other = thread_store.create_thread(db, backend="stub")
    response = _send(client, other.id, "here", [uploaded["id"]])
    assert response.status_code == 400, response.text
    assert "does not belong" in response.json()["detail"]


def test_an_unknown_attachment_id_is_refused(attachment_app):
    client, _db, _settings_, backend, _app = attachment_app
    thread_id = _thread(client)
    response = _send(client, thread_id, "here", ["a_" + "0" * 24])
    assert response.status_code == 400, response.text
    assert backend.sent == [], "the agent was sent a message with a file it cannot read"


def test_an_empty_message_is_still_422(attachment_app):
    client, _db, _settings_, _backend, _app = attachment_app
    thread_id = _thread(client)
    assert _send(client, thread_id, "   ", []).status_code == 422


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def test_download_serves_the_bytes_as_a_named_attachment(attachment_app):
    client, _db, _settings_, _backend, _app = attachment_app
    thread_id = _thread(client)
    uploaded = _upload(client, thread_id, "notes.txt", b"the bytes", "text/plain").json()

    response = client.get(
        f"/api/agent/attachments/{uploaded['id']}", headers=HEADERS
    )
    assert response.status_code == 200
    assert response.content == b"the bytes"
    assert response.headers["content-type"].startswith("text/plain")
    # Never rendered in this origin: these bytes came from a phone or an agent.
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"

    assert (
        client.get(f"/api/agent/attachments/{uploaded['id']}").status_code == 401
    )
    assert client.get("/api/agent/attachments/a_" + "0" * 24, headers=HEADERS).status_code == 404


def test_deleting_an_upload_removes_its_bytes(attachment_app):
    client, db, _settings_, _backend, _app = attachment_app
    thread_id = _thread(client)
    uploaded = _upload(client, thread_id, "a.txt", b"x").json()
    path = Path(attachment_store.get(db, uploaded["id"])["path"])

    assert (
        client.delete(f"/api/agent/attachments/{uploaded['id']}", headers=HEADERS).status_code
        == 204
    )
    assert attachment_store.get(db, uploaded["id"]) is None
    assert not path.exists()
    assert (
        client.delete(f"/api/agent/attachments/{uploaded['id']}", headers=HEADERS).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# the outbox — the agent's half
# --------------------------------------------------------------------------- #
def _deliver(client, app, thread_id: str, backend: StubBackend, name: str, data: bytes):
    """Do what the settling event does: put a file in the outbox and drain it."""
    settings = app.state.settings
    outbox = attachment_store.outbox_dir(settings, backend.conversation)
    (outbox / name).write_bytes(data)
    app.state.supervisor._drain_outbox(thread_id, publish=True)


def test_the_outbox_becomes_a_message_exactly_once(attachment_app):
    client, _db, _settings_, backend, app = attachment_app
    thread_id = _thread(client)
    assert _send(client, thread_id, "make me a report", []).status_code == 202

    _deliver(client, app, thread_id, backend, "report.csv", b"a,b\n1,2\n")

    history = _conversation(client, thread_id)
    delivered = [m for m in history if m["id"] and m["id"].startswith("outbox:")]
    assert len(delivered) == 1
    assert delivered[0]["role"] == "assistant"
    assert delivered[0]["content"] == ""
    assert delivered[0]["attachments"][0]["name"] == "report.csv"
    assert delivered[0]["attachments"][0]["direction"] == "out"

    # The queue is emptied by delivery. A second drain is a no-op, which is what makes a
    # duplicate impossible rather than "unlikely".
    app.state.supervisor._drain_outbox(thread_id, publish=True)
    history = _conversation(client, thread_id)
    assert len([m for m in history if m["id"] and m["id"].startswith("outbox:")]) == 1

    outbox = attachment_store.outbox_dir(_settings_, backend.conversation)
    assert list(outbox.iterdir()) == []


def test_history_delivers_a_file_whose_settling_event_was_missed(attachment_app):
    """A restart between "the turn ended" and "drain" must not lose the file.

    The read path drains too. It publishes nothing — the client asking for the transcript is
    about to receive its rows anyway.
    """
    client, _db, settings, backend, _app = attachment_app
    thread_id = _thread(client)
    assert _send(client, thread_id, "make me a report", []).status_code == 202

    outbox = attachment_store.outbox_dir(settings, backend.conversation)
    (outbox / "late.txt").write_bytes(b"late")
    # No drain call: the read itself has to repair the missed event.

    history = _conversation(client, thread_id)
    names = [
        attachment["name"]
        for message in history
        for attachment in message["attachments"]
    ]
    assert names == ["late.txt"]


def test_the_live_frame_carries_the_attachment_too(attachment_app):
    """The client renders the frame, not the history, while it is connected.

    A field omitted from the frame is a card erased from the screen until the next refresh —
    the same rule the dashboard's session frame follows.
    """
    client, _db, _settings_, backend, app = attachment_app
    thread_id = _thread(client)
    assert _send(client, thread_id, "make me a report", []).status_code == 202

    broker = app.state.agent_broker
    queue = broker.subscribe()
    try:
        _deliver(client, app, thread_id, backend, "chart.png", b"png")
        frames = []
        while not queue.empty():
            frames.append(queue.get_nowait().payload)
    finally:
        broker.unsubscribe(queue)

    messages = [frame["message"] for frame in frames if frame["kind"] == "agent.message"]
    assert messages, "delivery published no message frame"
    assert messages[-1]["attachments"][0]["name"] == "chart.png"
    assert messages[-1]["attachments"][0]["direction"] == "out"


# --------------------------------------------------------------------------- #
# the marker itself
# --------------------------------------------------------------------------- #
def test_only_resolved_markers_are_stripped():
    """An id we cannot resolve is words, and deleting words on the strength of their shape is
    how a transcript loses a sentence a model quoted back."""
    text = "hello\n\n[attachment a_" + "1" * 24 + "] x.png: /tmp/x.png"
    assert attachment_store.strip(text, set()) == text
    assert attachment_store.strip(text, {"a_" + "1" * 24}) == "hello"


def test_the_channel_note_is_never_shown():
    text = "hi\n\n[channel] To send the user a file, write it into /tmp/outbox."
    assert attachment_store.strip(text, set()) == "hi"


def test_plain_text_with_no_marker_is_untouched():
    text = "Working on it. "
    assert attachment_store.strip(text, set()) == text


def test_a_generic_declared_type_defers_to_the_name():
    """`application/octet-stream` is the absence of a guess, not a guess — otherwise a `.png`
    picked from a desktop dialog loses its inline rendering."""
    assert attachment_store.mime_for("shot.png", "application/octet-stream") == "image/png"
    assert attachment_store.mime_for("x.png", "image/jpeg") == "image/jpeg"
    assert (
        attachment_store.mime_for("thing.bin", "application/octet-stream")
        == "application/octet-stream"
    )
