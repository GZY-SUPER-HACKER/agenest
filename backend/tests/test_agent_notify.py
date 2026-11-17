"""Relaying a blocked session to the personal agent (design.md §45).

No network and no LLM: the same stub-Pi arrangement as `test_agent_contract.py`, reduced to
what this path needs. What is pinned here is *when* a relay happens and *what* the agent is
told — not the model's ability to phrase a sentence.

The tests deliberately read the conversation through `/api/agent/threads/{id}`, because that is
where the two halves meet and because the instruction half of the prompt must be absent from
the wire: a reader should see the facts, never the scaffolding.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

import pytest
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.stream import AgentBroker, Broker
from fastapi.testclient import TestClient

ACCESS_TOKEN = "test-access-token"
INGEST_TOKEN = "test-ingest-token"

#: A Pi stand-in that answers the RPC calls this path uses. It records what the backend sends
#: as the user's own message and replies with `echo: …`, which is enough to prove that the
#: prompt reached the agent and a turn ran.
FAKE_PI = r'''
import json, os, sys

def send(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()

def respond(cmd, **extra):
    send({"type": "response", "id": cmd.get("id"), "command": cmd.get("type"),
          "success": True, **extra})

argv = sys.argv[1:]
def arg(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default

session_dir = arg("--session-dir")
session_id = arg("--session-id", "stub-session")
session_file = None
if session_dir:
    os.makedirs(session_dir, exist_ok=True)
    session_file = os.path.join(session_dir, "2026-01-01T00-00-00-000Z_%s.jsonl" % session_id)
    with open(session_file, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "session", "version": 3, "id": session_id,
                                 "cwd": os.getcwd()}) + "\n")

def record(role, text, **extra):
    if session_file is None:
        return
    with open(session_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "message",
            "id": "e%d" % sum(1 for _ in open(session_file, encoding="utf-8")),
            "parentId": None, "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": role, "content": [{"type": "text", "text": text}],
                        "timestamp": "2026-01-01T00:00:00.000Z", **extra},
        }, ensure_ascii=False) + "\n")

buffer = b""
while True:
    chunk = sys.stdin.buffer.read(1)
    if not chunk:
        break
    buffer += chunk
    while b"\n" in buffer:
        raw, _, buffer = buffer.partition(b"\n")
        raw = raw.rstrip(b"\r").strip()
        if not raw:
            continue
        cmd = json.loads(raw.decode("utf-8"))
        kind = cmd.get("type")
        if kind == "get_state":
            respond(cmd, data={"model": {"provider": "fake", "id": "fake-1"},
                               "sessionFile": session_file, "sessionId": session_id,
                               "isStreaming": False})
        elif kind == "get_available_models":
            respond(cmd, data={"models": []})
        elif kind == "get_entries":
            respond(cmd, data={"entries": [], "leafId": None})
        elif kind == "abort":
            respond(cmd)
        elif kind in ("prompt", "follow_up"):
            text = cmd.get("message", "")
            respond(cmd)
            record("user", text)
            send({"type": "message_end",
                  "message": {"role": "user",
                              "content": [{"type": "text", "text": text}]}})
            send({"type": "agent_start"})
            send({"type": "message_update",
                  "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0,
                                            "delta": "echo: " + text}})
            record("assistant", "echo: " + text)
            send({"type": "message_end",
                  "message": {"role": "assistant",
                              "content": [{"type": "text", "text": "echo: " + text}]}})
            send({"type": "agent_settled"})
'''


def _settings(
    tmp_path: Path,
    *,
    dashboard_token: str | None = ACCESS_TOKEN,
    notify: bool = True,
) -> Settings:
    script = tmp_path / "fake_pi.py"
    script.write_text(FAKE_PI, encoding="utf-8")
    return Settings(
        db_path=tmp_path / "test.sqlite3",
        ingest_token=INGEST_TOKEN,
        host="127.0.0.1",
        port=0,
        static_dir=None,
        stale_after_seconds=900,
        event_payload_max_chars=8000,
        cors_origins=(),
        dashboard_token=dashboard_token,
        agent_backend="native",
        agent_notify_on_ask=notify,
        pi_bin=tuple(shlex.split(f'"{sys.executable}" "{script}"')),
        pi_workspace=tmp_path,
        pi_session_dir=tmp_path / "pi-sessions",
        pi_provider="fake",
        pi_model="fake-1",
    )


def _app(tmp_path: Path, **kwargs):
    settings = _settings(tmp_path, **kwargs)
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    return app, db


@pytest.fixture()
def notify_app(tmp_path: Path):
    app, db = _app(tmp_path)
    with TestClient(app) as client:
        yield client, db
    db.close()


def _headers(token: str = ACCESS_TOKEN) -> dict[str, str]:
    return {"X-CPD-Token": token}


def _new_session(client: TestClient) -> str:
    response = client.post(
        "/api/sessions",
        json={
            "agent_type": "Claude Code",
            "title": "Fix the login redirect",
            "project_name": "myproj",
            "host": "laptop-1",
        },
        headers=_headers(INGEST_TOKEN),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _report(client: TestClient, session_id: str, event_type: str, message: str, **extra):
    body = {"event_type": event_type, "message": message, **extra}
    response = client.post(
        f"/api/sessions/{session_id}/events", json=body, headers=_headers(INGEST_TOKEN)
    )
    assert response.status_code == 202, response.text
    return response.json()


def _thread(client: TestClient) -> str:
    response = client.post("/api/agent/session", headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _messages(client: TestClient, thread_id: str) -> list[dict]:
    response = client.get(f"/api/agent/threads/{thread_id}", headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def _notices(client: TestClient, thread_id: str) -> list[dict]:
    return [
        m
        for m in _messages(client, thread_id)
        if m["role"] == "user" and m["content"].startswith("[agenest]")
    ]


def _wait_for(predicate, *, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not met before the timeout")


def test_a_blocked_session_is_relayed_to_the_personal_agent(notify_app) -> None:
    """The facts reach the agent, the instructions do not reach the reader."""
    client, _db = notify_app
    session_id = _new_session(client)
    _report(
        client,
        session_id,
        "awaiting_user",
        "Claude needs your permission to use Bash",
        payload={"options": ["Allow once", "Always allow", "Deny"]},
    )

    thread_id = _thread(client)
    notice = _wait_for(lambda: _notices(client, thread_id))[0]

    assert "Project: myproj" in notice["content"]
    assert "Fix the login redirect" in notice["content"]
    assert "Claude Code on laptop-1" in notice["content"]
    assert "Claude needs your permission to use Bash" in notice["content"]
    assert "Allow once / Always allow / Deny" in notice["content"]
    # The agent must be told what to do; the reader must not be shown it as a bubble.
    assert "[agenest · instruction]" not in notice["content"]

    # A turn actually ran: the stub echoes the prompt as the answer.
    reply = _wait_for(
        lambda: [m for m in _messages(client, thread_id) if m["role"] == "assistant"]
    )
    assert reply[-1]["content"].startswith("echo: ")


def test_a_repeated_awaiting_user_is_not_relayed_again(notify_app) -> None:
    """Claude re-fires `Notification` while blocked; the user is told once.

    The second report is a different sentence, so `event_key` does not dedupe it — the gate is
    the timeline: the newest display event before it is already `awaiting_user`.
    """
    client, _db = notify_app
    session_id = _new_session(client)
    _report(client, session_id, "awaiting_user", "permission to run the tests")

    thread_id = _thread(client)
    _wait_for(lambda: _notices(client, thread_id))

    _report(client, session_id, "awaiting_user", "permission to write a file")
    time.sleep(0.6)
    assert len(_notices(client, thread_id)) == 1


def test_a_new_block_is_relayed_after_the_previous_one_cleared(notify_app) -> None:
    """Work reported between two asks is a new question, not a repeat."""
    client, _db = notify_app
    session_id = _new_session(client)
    _report(client, session_id, "awaiting_user", "permission to run the tests")

    thread_id = _thread(client)
    _wait_for(lambda: _notices(client, thread_id))

    _report(client, session_id, "progress", "approved; running the suite")
    _report(client, session_id, "awaiting_user", "permission to install a package")
    _wait_for(lambda: len(_notices(client, thread_id)) == 2)


def test_other_events_are_not_relayed(notify_app) -> None:
    client, _db = notify_app
    session_id = _new_session(client)
    _report(client, session_id, "progress", "reading the router")
    _report(client, session_id, "turn_completed", "reply ended")

    thread_id = _thread(client)
    time.sleep(0.6)
    assert _notices(client, thread_id) == []


def test_a_late_ask_on_a_closed_session_is_not_relayed(notify_app) -> None:
    """`awaiting_user` is gated on `running`, the same rule the board applies."""
    client, _db = notify_app
    session_id = _new_session(client)
    _report(client, session_id, "session_completed", "session ended")
    _report(client, session_id, "awaiting_user", "late notification")

    thread_id = _thread(client)
    time.sleep(0.6)
    assert _notices(client, thread_id) == []


def test_no_token_means_no_agent_and_no_relay(tmp_path: Path) -> None:
    """The agent routes are disabled without an access token, so there is no one to tell."""
    app, db = _app(tmp_path, dashboard_token=None)
    try:
        with TestClient(app) as client:
            session_id = _new_session(client)
            _report(client, session_id, "awaiting_user", "permission to run the tests")
            time.sleep(0.6)
            assert db.query_one("SELECT COUNT(*) AS n FROM agent_threads")["n"] == 0
    finally:
        db.close()


def test_the_switch_turns_the_relay_off(tmp_path: Path) -> None:
    app, db = _app(tmp_path, notify=False)
    try:
        with TestClient(app) as client:
            session_id = _new_session(client)
            _report(client, session_id, "awaiting_user", "permission to run the tests")
            time.sleep(0.6)
            assert db.query_one("SELECT COUNT(*) AS n FROM agent_threads")["n"] == 0
    finally:
        db.close()
