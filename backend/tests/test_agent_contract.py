"""The agent's contract, exercised against a stub Pi process.

No network and no LLM: `CSD_PI_BIN` points at a script written by these tests, which speaks
the JSONL RPC protocol and nothing more. That is the point — what needs pinning here is our
side of the protocol and our policy (auth, what gets stored, what happens when Pi dies), not
Pi's ability to talk to a model.

The framing case in `test_frame_with_carriage_return_and_line_separator_is_intact` is the one
worth keeping honest. Pi's RPC documentation warns that clients must split on `\\n` alone
because `U+2028` is legal inside a JSON string; Python has the mirror hazard, since text-mode
pipes apply universal-newline translation and split on `\\r` too. A test that only exercised
tidy ASCII would pass against a reader that corrupts both.

The awkward characters are built with escapes and `chr(0x2028)` rather than pasted in
literally, because an invisible separator in a source file is indistinguishable from a space
to everyone who reads it afterwards — including whoever next has to edit this.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path

import pytest
from app.agent import store
from app.agent.backends.native import NativeBackend
from app.agent.memory import DreamResult
from app.agent.rpc import resolve_executable
from app.agent.supervisor import Supervisor
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.stream import AgentBroker, Broker
from fastapi.testclient import TestClient

ACCESS_TOKEN = "test-access-token"

#: A Pi stand-in. Speaks LF-delimited JSON on stdin/stdout; echoes each prompt, then reports
#: one tool call. Reads and writes **bytes**, splitting on `b"\n"` — the same discipline the
#: real client must follow, and the reason the separator test is meaningful.
FAKE_PI = r'''
import json, os, sys

def send(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()

def respond(cmd, **extra):
    send({"type": "response", "id": cmd.get("id"), "command": cmd.get("type"),
          "success": True, **extra})

# The session file, written the way the real Pi writes one: to disk, named `{stamp}_{id}.jsonl`,
# with a header carrying the id. Reading the conversation is a file read (docs/design.md §18), so
# a stub that only remembered its turns in memory would let `history` pass while reading nothing
# — and would hide exactly the thing these tests are about.
argv = sys.argv[1:]
def arg(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default

session_dir = arg("--session-dir")
session_id = arg("--session-id", "stub-session")
if session_dir:
    os.makedirs(session_dir, exist_ok=True)
    session_file = os.path.join(session_dir, "2026-01-01T00-00-00-000Z_%s.jsonl" % session_id)
    with open(session_file, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "session", "version": 3, "id": session_id,
                                 "cwd": os.getcwd()}) + "\n")
else:
    session_file = None

def record(role, content, **extra):
    if session_file is None:
        return
    with open(session_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "message", "id": "e%s" % (sum(1 for _ in open(session_file, encoding="utf-8"))),
            "parentId": None, "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": role, "content": content,
                        "timestamp": "2026-01-01T00:00:00.000Z", **extra},
        }, ensure_ascii=False) + "\n")

def text_block(text):
    return [{"type": "text", "text": text}]

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
            respond(cmd, data={"models": [{"provider": "fake", "id": "fake-1", "name": "Fake One"}]})
        elif kind == "get_entries":
            respond(cmd, data={"entries": [], "leafId": None})
        elif kind == "abort":
            respond(cmd)
        elif kind in ("prompt", "follow_up"):
            text = cmd.get("message", "")
            if "CRASH" in text:
                sys.exit(3)
            respond(cmd)
            # The user's message is recorded before the reply, and is what the client waits for
            # to learn its id.
            record("user", text_block(text))
            send({"type": "message_end",
                  "message": {"role": "user", "content": text_block(text)}})
            echo = "echo: " + text
            send({"type": "agent_start"})
            send({"type": "message_update",
                  "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": echo}})
            # A `toolCall` block, because that is where the real Pi puts the arguments — and the
            # arguments are the half of a tool call a reader cannot reconstruct from the result.
            record("assistant", text_block(echo) + [
                {"type": "toolCall", "id": "c1", "name": "bash",
                 "arguments": {"command": "ls"}}
            ])
            send({"type": "message_end",
                  "message": {"role": "assistant", "content": text_block(echo)}})
            send({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
                  "args": {"command": "ls"}})
            tricky = "a\r\nb" + chr(0x2028) + "c"
            record("toolResult", text_block(tricky), toolName="bash", toolCallId="c1",
                   isError=False)
            send({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
                  "isError": False,
                  "result": {"content": text_block(tricky)}})
            send({"type": "agent_settled"})
'''

#: What the tool call above must come back as: a carriage return (which a text-mode reader
#: would translate and split on) and a U+2028 line separator (which a naive line reader would
#: split on). Both are legal inside a JSON string; neither is a record delimiter.
TRICKY_TOOL_OUTPUT = "a\r\nb" + chr(0x2028) + "c"


def _settings(tmp_path: Path, *, dashboard_token: str | None = ACCESS_TOKEN, pi_bin: str | None = None) -> Settings:
    script = tmp_path / "fake_pi.py"
    script.write_text(FAKE_PI, encoding="utf-8")
    # Quoted so a path containing spaces stays one argument — `Settings.from_env` parses this
    # with shlex for the same reason.
    command = pi_bin or f'"{sys.executable}" "{script}"'

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
        # These tests exercise the `native` backend against a stub Pi process: the protocol
        # and the policy are what is being pinned, and neither needs a network. The nanobot
        # backend has its own file (`test_nanobot_adapter.py`) with its own stub server.
        agent_backend="native",
        pi_bin=tuple(shlex.split(command)),
        pi_workspace=tmp_path,
        pi_session_dir=tmp_path / "pi-sessions",
        pi_provider="fake",
        pi_model="fake-1",
    )


@pytest.fixture()
def agent_app(tmp_path: Path):
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        yield client, db, settings
    db.close()


def _headers(token: str = ACCESS_TOKEN) -> dict[str, str]:
    return {"X-CPD-Token": token}


def _wait_for(predicate, *, timeout: float = 10.0, interval: float = 0.05):
    """Poll until `predicate` is truthy. The agent runs on the app's event loop, so a test
    observing its effects has to wait for it rather than assume it already happened."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not met before the timeout")


def _new_thread(client: TestClient) -> str:
    """The conversation the page is on.

    Asks for *the* session rather than creating one: there is exactly one and no route that
    makes a second (docs/design.md §18), so this is the same call the page makes on boot.
    """
    response = client.post("/api/agent/session", headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _say(client: TestClient, thread_id: str, content: str):
    return client.post(
        f"/api/agent/threads/{thread_id}/messages",
        json={"content": content},
        headers=_headers(),
    )


# --------------------------------------------------------------------------- #
# auth — the whole security story of this feature
# --------------------------------------------------------------------------- #
def test_every_agent_route_is_disabled_without_a_token(tmp_path: Path) -> None:
    """Unset means **disabled**, not open. The agent runs shell commands on this host, so a
    default that left it reachable would be the worst default in the project."""
    settings = _settings(tmp_path, dashboard_token=None)
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        for method, path, body in [
            ("get", "/api/agent/status", None),
            ("post", "/api/agent/session", None),
            ("get", "/api/agent/threads/t_anything", None),
            ("post", "/api/agent/threads/t_anything/messages", {"content": "hi"}),
            ("get", "/api/agent/stream", None),
        ]:
            # Bodies are valid on purpose: this asserts the *gate*, and a 422 from a malformed
            # body would pass the assertion for the wrong reason if the gate ever moved.
            call = getattr(client, method)
            response = call(path, **({"json": body} if body is not None else {}))
            assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
    db.close()


def test_wrong_token_is_401_and_right_token_works(agent_app) -> None:
    client, _db, _s = agent_app
    assert client.get("/api/agent/status", headers=_headers("nope")).status_code == 401
    assert client.get("/api/agent/status", headers=_headers()).status_code == 200


def test_dashboard_token_is_not_an_ingest_token(agent_app) -> None:
    """The split has to hold in the other direction too: holding the agent credential must
    not grant write access to the dashboard's records."""
    client, _db, _s = agent_app
    response = client.post(
        "/api/sessions", json={"agent_type": "x", "title": "y"}, headers=_headers()
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# framing
# --------------------------------------------------------------------------- #
def _conversation(client: TestClient, thread_id: str) -> list[dict]:
    """The conversation, as the API reports it.

    Read through the route rather than the database, because the database no longer holds a
    conversation: the transcript belongs to the agent (docs/design.md §18) and this route is
    where the two halves — our thread metadata and the agent's own messages — are joined. A test
    that reached into a table here would be testing storage that is deliberately not ours.
    """
    response = client.get(f"/api/agent/threads/{thread_id}", headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def _settled(client: TestClient, thread_id: str) -> list[dict]:
    """The conversation once the run has finished — a tool row means the run got all the way."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        messages = _conversation(client, thread_id)
        if any(m["role"] == "tool" for m in messages):
            return messages
        time.sleep(0.05)
    raise AssertionError("the run never settled")


def test_frame_with_carriage_return_and_line_separator_is_intact(agent_app) -> None:
    """A `\\r` and a `U+2028` inside tool output must survive the transport untouched.

    A text-mode reader translates `\\r\\n` to `\\n` and splits there; a reader that treats the
    Unicode separators as newlines splits mid-string. Either way this output arrives as two
    mangled records instead of one honest one.

    It has to survive two paths now rather than one: the live frame, and the `get_entries` round
    trip that produces the stored conversation.
    """
    client, _db, _s = agent_app
    thread_id = _new_thread(client)
    assert _say(client, thread_id, "hello").status_code == 202

    tool = [m for m in _settled(client, thread_id) if m["role"] == "tool"]
    assert tool[0]["content"] == TRICKY_TOOL_OUTPUT


# --------------------------------------------------------------------------- #
# what the conversation holds
# --------------------------------------------------------------------------- #
def test_the_prompt_is_recorded_exactly_once(agent_app) -> None:
    """The conversation holds the prompt once, not twice.

    Pi echoes the user's own message back, and that echo is what `send` waits for — so the
    temptation is to let both the echo and the sent copy through. The echo is a signal, not a
    second message, and the count is what proves it was treated that way.
    """
    client, _db, _s = agent_app
    thread_id = _new_thread(client)
    _say(client, thread_id, "count me")

    users = [m for m in _settled(client, thread_id) if m["role"] == "user"]
    assert [m["content"] for m in users] == ["count me"]


def test_only_completed_units_reach_the_conversation(agent_app) -> None:
    """Deltas stream; the conversation holds units. One entry per thing said, not per token."""
    client, _db, _s = agent_app
    thread_id = _new_thread(client)
    _say(client, thread_id, "hi")

    messages = _settled(client, thread_id)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
    assert messages[1]["content"] == "echo: hi"
    assert messages[2]["content"] == TRICKY_TOOL_OUTPUT


def test_a_message_carries_the_agents_own_id(agent_app) -> None:
    """The id the page dedups on comes from the agent, not from this service.

    It has to be the same value across a refresh, a reconnect and a restart, because the page
    draws a message early from the POST response and meets it again in a fetched history. The
    `e1`/`e2` shape here is the stub Pi's session-entry id, which is exactly what the real one
    supplies.
    """
    client, _db, _s = agent_app
    thread_id = _new_thread(client)
    sent = _say(client, thread_id, "hi").json()

    assert sent["id"], "the POST answers with the agent's id for the message just sent"
    messages = _settled(client, thread_id)
    assert sent["id"] in [m["id"] for m in messages], "and the history agrees with it"


def test_a_tool_row_carries_what_was_run_as_well_as_what_came_back(agent_app) -> None:
    """The command line, not only its output.

    Pi records these apart — the assistant's `toolCall` block holds the arguments and a later
    `toolResult` holds the outcome — and pairing them is what makes a re-read conversation
    actionable. Without it a reader learns that something failed, not what.
    """
    client, _db, _s = agent_app
    thread_id = _new_thread(client)
    _say(client, thread_id, "hi")

    tool = [m for m in _settled(client, thread_id) if m["role"] == "tool"][0]
    assert tool["tool_name"] == "bash"
    assert tool["args"] == {"command": "ls"}



def test_first_prompt_replaces_the_placeholder_title(agent_app) -> None:
    client, db, _s = agent_app
    thread_id = _new_thread(client)
    assert store.get_thread(db, thread_id).title == "New conversation"

    _say(client, thread_id, "what is a monad")
    assert _wait_for(lambda: store.get_thread(db, thread_id).title == "what is a monad")


def test_an_explicit_title_is_never_overwritten(agent_app) -> None:
    """The rename is a convenience for the placeholder, not a policy.

    A title the user typed is a decision, and the first prompt silently replacing it would be
    the same category of mistake as the dashboard inventing a session status (§2).
    """
    client, db, _s = agent_app
    # Made through the store rather than the API: the session became singular and its route
    # stopped accepting a title, and what is under test here is the rename policy, not a route.
    thread_id = store.create_thread(db, title="Batch import bug", backend="native").id
    _say(client, thread_id, "something else entirely")

    _settled(client, thread_id)
    assert store.get_thread(db, thread_id).title == "Batch import bug"


def test_model_reported_by_pi_is_what_is_stored(agent_app) -> None:
    """Not what config asked for. A model pattern can resolve or fall back, and the UI should
    show what is actually running."""
    client, db, _s = agent_app
    thread_id = _new_thread(client)
    _say(client, thread_id, "hi")
    assert _wait_for(lambda: store.get_thread(db, thread_id).model == "fake-1")


def test_concurrent_session_requests_still_produce_one_conversation(tmp_path: Path) -> None:
    """Idempotent under concurrency, not merely in sequence.

    "Read it, insert if missing" has a window between the two, and the page's own boot lands in
    it: React's StrictMode double-mounts effects in development, so two calls leave together.
    Without serialising, that window yields the second conversation this product does not have
    — which is exactly how it was found, in two rows three milliseconds apart.
    """
    db = Database(tmp_path / "race.sqlite3")
    ids: list[str] = []
    collected = threading.Lock()

    def boot() -> None:
        session_id = store.get_or_create_session(db, backend="native").id
        with collected:
            ids.append(session_id)

    threads = [threading.Thread(target=boot) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(ids)) == 1, ids
    assert len(list(db.query("SELECT id FROM agent_threads"))) == 1
    db.close()


# --------------------------------------------------------------------------- #
# the stream
# --------------------------------------------------------------------------- #
def test_the_agent_stream_has_no_id_field() -> None:
    """No `id:` means no cursor — deliberate, because most frames are deltas that are never
    stored and so cannot be resumed from. An SSE id that looks resumable and isn't would be
    worse than none."""
    from app.stream import _Frame, agent_sse_message

    text = agent_sse_message(_Frame(7, {"kind": "agent.delta", "delta": "x"}))
    assert "id:" not in text
    assert text.startswith("event: agent.delta\n")


def test_the_dashboard_stream_still_has_one() -> None:
    """The contrast that makes the decision above legible: the dashboard's frames are
    database rows, so its cursor survives a restart and it keeps `id:`."""
    from app.stream import _Frame, sse_message

    text = sse_message(_Frame(7, {"kind": "session.updated"}), include=True)
    assert text.startswith("id: 7\n")


# --------------------------------------------------------------------------- #
# failure
# --------------------------------------------------------------------------- #
def test_a_crashed_pi_is_reported_and_the_thread_survives(agent_app) -> None:
    """A dead agent must not take the conversation with it.

    What "survives" means has changed, and in a way worth noting: it used to mean "our copy in
    SQLite still has the messages". There is no copy — the transcript belongs to the agent — so
    what survives is the **thread**: the conversation is still addressable, still readable, and
    still usable, because the next prompt starts a fresh process against the same session id.

    The stub Pi keeps its session in memory, so a restarted stub legitimately has an empty
    transcript. The real one resumes from its session file, which is the behaviour `--session-id`
    exists to get.
    """
    client, _db, _s = agent_app
    thread_id = _new_thread(client)

    # The agent dies mid-prompt: reported as "the agent refused this", not as a broken thread.
    assert _say(client, thread_id, "CRASH").status_code == 502

    response = client.get(f"/api/agent/threads/{thread_id}", headers=_headers())
    assert response.status_code == 200, "the conversation is still addressable"
    assert response.json()["messages"] == []

    # And it is still usable: the next prompt starts a process and the run completes.
    assert _say(client, thread_id, "try again").status_code == 202
    assert [m["role"] for m in _settled(client, thread_id)] == ["user", "assistant", "tool"]


def test_history_reads_the_session_file_without_starting_pi(tmp_path: Path) -> None:
    """Reading the conversation must not require a process.

    The transcript is a file Pi already wrote. Asking a *running* Pi for it would mean spawning a
    Node process — and running whatever its extensions and MCP servers do on startup — to read
    something sitting on disk, and it would make the page depend on the agent being up just to
    render. This test is decisive about which path was taken: `pi_bin` here cannot be executed,
    so any attempt to start Pi answers 503 instead of the conversation.
    """
    settings = _settings(tmp_path, pi_bin="definitely-not-a-real-binary-xyz")
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        thread_id = _new_thread(client)

        # A conversation Pi has already recorded: the thread points at it, and the session file
        # exists with the shape Pi writes — a header carrying the id, then message entries.
        session_dir = settings.pi_session_dir
        assert session_dir is not None
        session_dir.mkdir(parents=True, exist_ok=True)
        store.set_thread_conversation(db, thread_id, "c_written_by_pi")
        (session_dir / "2026-01-01T00-00-00-000Z_c_written_by_pi.jsonl").write_text(
            "\n".join(
                json.dumps(entry)
                for entry in [
                    {"type": "session", "version": 3, "id": "c_written_by_pi", "cwd": str(tmp_path)},
                    {"type": "thinking_level_change", "id": "e1", "thinkingLevel": "medium"},
                    {
                        "type": "message",
                        "id": "e2",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "what did we decide"}],
                        },
                    },
                    {
                        "type": "message",
                        "id": "e3",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "not part of the answer"},
                                {"type": "text", "text": "the dashboard is read-only"},
                            ],
                        },
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        response = client.get(f"/api/agent/threads/{thread_id}", headers=_headers())
        assert response.status_code == 200, response.text
        messages = response.json()["messages"]

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "the dashboard is read-only"
    # Ids are Pi's, and the bookkeeping entries are not messages.
    assert [m["id"] for m in messages] == ["e2", "e3"]
    db.close()


def test_history_carries_pi_timestamps_rather_than_the_read_time(tmp_path: Path) -> None:
    """A re-read conversation must show when it happened, not when it was fetched.

    The old shared table carried `created_at`. Deleting it without putting the field on
    `Message` would leave the only stamp in `Supervisor._to_out`, which uses `utcnow()` — a
    shape that passes every role-and-content assertion while making the whole conversation
    look like it arrived at page load, and collapsing day grouping onto today.
    """
    settings = _settings(tmp_path, pi_bin="definitely-not-a-real-binary-xyz")
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        thread_id = _new_thread(client)
        session_dir = settings.pi_session_dir
        assert session_dir is not None
        session_dir.mkdir(parents=True, exist_ok=True)
        store.set_thread_conversation(db, thread_id, "c_stamped")
        (session_dir / "2026-01-01T00-00-00-000Z_c_stamped.jsonl").write_text(
            "\n".join(
                json.dumps(entry)
                for entry in [
                    {"type": "session", "version": 3, "id": "c_stamped", "cwd": str(tmp_path)},
                    {
                        "type": "message",
                        "id": "e1",
                        "timestamp": "2026-01-02T03:04:05.000Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                    },
                    {
                        "type": "message",
                        "id": "e2",
                        "timestamp": "2026-01-02T03:04:07.000Z",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "hi"}],
                        },
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        messages = client.get(
            f"/api/agent/threads/{thread_id}", headers=_headers()
        ).json()["messages"]

    assert [m["created_at"] for m in messages] == [
        "2026-01-02T03:04:05.000Z",
        "2026-01-02T03:04:07.000Z",
    ]
    db.close()


def test_a_conversation_from_before_session_ids_is_still_addressable(tmp_path: Path) -> None:
    """P2 in HANDOFF-native: the old code never passed `--session-id`.

    Pi therefore named the session file after a UUID of its own, and the thread's id cannot
    find it. The upgrade has to do better than showing an empty conversation beside a title
    that says otherwise. It records the file in a read-only map beside the sessions, and drops
    the old shared table only after that has worked.
    """
    settings = _settings(tmp_path, pi_bin="definitely-not-a-real-binary-xyz")
    db = Database(settings.db_path)
    session_dir = tmp_path / "pi-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "2026-01-01T10-00-01-000Z_01legacy.jsonl").write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {
                    "type": "session",
                    "version": 3,
                    "id": "01legacy",
                    "cwd": str(tmp_path),
                    "timestamp": "2026-01-01T10:00:01.000Z",
                },
                {
                    "type": "message",
                    "id": "l1",
                    "timestamp": "2026-01-01T10:00:02.400Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "what did we decide"}],
                    },
                },
                {
                    "type": "message",
                    "id": "l2",
                    "timestamp": "2026-01-01T10:00:03.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "sqlite is the record"}],
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with db.write() as conn:
        conn.execute(
            "INSERT INTO agent_threads "
            "(id, title, backend, backend_conversation_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "t_legacy",
                "an old conversation",
                "native",
                "c_after_upgrade",
                "2026-01-01T10:00:00.000Z",
                "2026-01-01T10:10:00.000Z",
            ),
        )
        # The schema the old release created. Dropping it is the point; losing its rows is not.
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
            ("t_legacy", "user", "what did we decide", "2026-01-01T10:00:02.000Z"),
        )

    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        response = client.get("/api/agent/threads/t_legacy", headers=_headers())
        assert response.status_code == 200, response.text
        messages = response.json()["messages"]

        # The old table is gone once the conversation is addressable, and the marker means the
        # next start does not repeat a migration whose input it has dropped. Asserted before the
        # client shuts down, because the app closes the database with its lifespan.
        assert db.query_one("SELECT 1 FROM sqlite_master WHERE name = 'agent_messages'") is None
        assert (
            db.query_one("SELECT 1 FROM meta WHERE key = 'migrated.legacy_transcripts'")
            is not None
        )

    assert [m["content"] for m in messages] == ["what did we decide", "sqlite is the record"]
    assert [m["created_at"] for m in messages] == [
        "2026-01-01T10:00:02.400Z",
        "2026-01-01T10:00:03.000Z",
    ]
    db.close()


def test_the_memory_is_appended_when_the_extension_is_missing(tmp_path: Path, monkeypatch) -> None:
    """The fallback path: no per-turn appendix extension, so the memory is a start-up snapshot.

    Pi reads `--append-system-prompt` once, when the process starts. The per-turn extension
    replaced that (see the test below) and this stays for a deployment that lost the extension
    file — a worse agent than a per-turn one, and a better one than an agent with no memory.

    The registry tool is a *separate* extension file and stays loaded here: losing one does not
    lose the other, and the `-e` assertions below are what keeps them independent.
    """
    monkeypatch.setattr("app.agent.backends.native.APPENDIX_EXTENSION", tmp_path / "missing.ts")
    settings = _settings(tmp_path)
    memory_dir = settings.resolved_memory_dir
    assert memory_dir is not None
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("# Memory\n\nSQLite is the record.", encoding="utf-8")
    (memory_dir / "USER.md").write_text("Prefers Chinese.", encoding="utf-8")
    (memory_dir / "SOUL.md").write_text("Be terse.", encoding="utf-8")
    (memory_dir / "history.jsonl").write_text('{"cursor": 1}\n', encoding="utf-8")

    argv = NativeBackend(settings, lambda *_: None).build_argv(session_id="c_x", title="t")

    loaded = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
    assert not any(path.endswith("appendix.ts") for path in loaded), "the appendix is missing"
    assert any(path.endswith("registry.ts") for path in loaded), "the tool is a different file"
    assert "--append-system-prompt" in argv
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "SQLite is the record." in prompt
    assert "Prefers Chinese." in prompt
    assert "Be terse." in prompt
    assert "history.jsonl" in prompt
    assert "the user wins" in prompt


def test_the_memory_is_re_read_on_every_prompt_not_just_at_startup(tmp_path: Path) -> None:
    """The per-turn path, and the reason the Dream restart could be dropped.

    Pi's `before_agent_start` hook fires for every prompt, so the extension re-reads the appendix
    each time. The backend's job is to load that extension, regenerate the file it reads and say
    where it is — the text itself stays in Python, where the suite can see it.
    """
    settings = _settings(tmp_path)
    memory_dir = settings.resolved_memory_dir
    assert memory_dir is not None
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("SQLite is the record.", encoding="utf-8")
    (memory_dir / "USER.md").write_text("Prefers Chinese.", encoding="utf-8")

    backend = NativeBackend(settings, lambda *_: None)
    argv = backend.build_argv(session_id="c_x", title="t")

    assert "-e" in argv
    assert argv[argv.index("-e") + 1].endswith("appendix.ts")
    assert "--append-system-prompt" not in argv, "one mechanism, not both"

    appendix = memory_dir / ".recall.md"
    assert backend._child_env()["CSD_AGENT_APPENDIX"].split(",")[0] == str(appendix)
    text = appendix.read_text(encoding="utf-8")
    assert "SQLite is the record." in text
    assert "Prefers Chinese." in text

    # The appendix tracks the files it is derived from: this is what a Dream regenerates, and
    # what makes a hand-edit of MEMORY.md reach a running agent too.
    (memory_dir / "MEMORY.md").write_text("SQLite is the log.", encoding="utf-8")
    backend.build_argv(session_id="c_x", title="t")
    assert "SQLite is the log." in appendix.read_text(encoding="utf-8")


def test_the_memory_skills_directory_is_passed_to_pi(tmp_path: Path) -> None:
    """Dream's fourth writable path lives outside every location Pi scans.

    `memory/skills/<name>/SKILL.md` is not `.pi/skills` or `.agents/skills`, so without an
    explicit `--skill` the agent never learns the skill exists. Passing the directory (not a
    file) keeps Pi's progressive disclosure: names and descriptions in the prompt, the body
    only when the agent reads it.
    """
    settings = _settings(tmp_path)
    memory_dir = settings.resolved_memory_dir
    assert memory_dir is not None
    skill = memory_dir / "skills" / "deploy" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: deploy\ndescription: ship it\n---\n", encoding="utf-8")

    argv = NativeBackend(settings, lambda *_: None).build_argv(session_id="c_x", title="t")

    assert "--skill" in argv
    assert argv[argv.index("--skill") + 1] == str(memory_dir / "skills")


def test_an_operators_append_system_prompt_is_not_stacked_on(tmp_path: Path, monkeypatch) -> None:
    """Two `--append-system-prompt` arguments make the winner depend on Pi's parser.

    An operator who passed one in `CSD_PI_ARGS` has made the decision; ours is skipped rather
    than appended underneath it. Checked on the fallback path — the only one that passes the flag
    at all, since the per-turn extension supersedes both.
    """
    monkeypatch.setattr("app.agent.backends.native.APPENDIX_EXTENSION", tmp_path / "missing.ts")
    settings = _settings(tmp_path)
    memory_dir = settings.resolved_memory_dir
    assert memory_dir is not None
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("a fact", encoding="utf-8")
    settings = dataclasses.replace(
        settings, pi_args=("--append-system-prompt", "operator text")
    )

    argv = NativeBackend(settings, lambda *_: None).build_argv(session_id=None, title="t")

    assert argv.count("--append-system-prompt") == 1
    assert "operator text" in argv


def test_the_extension_needs_no_restart(tmp_path: Path) -> None:
    """What the per-turn extension buys: `refresh_context` becomes a no-op.

    The restart existed only because Pi reads the appendix when the process starts. With the
    extension re-reading it every prompt, stopping a live process would cost a spawn and buy
    nothing — and would still have had to wait for an idle moment.
    """

    class _Proc:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    backend = NativeBackend(_settings(tmp_path), lambda *_: None)
    proc = _Proc()
    backend._procs["c_1"] = proc

    asyncio.run(backend.refresh_context("c_1"))

    assert not proc.stopped
    assert "c_1" in backend._procs


def test_refreshing_the_context_stops_an_idle_process_on_the_fallback_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Without the extension a long-lived Pi keeps the memory it started with, and only a
    restart reloads it."""
    monkeypatch.setattr("app.agent.backends.native.APPENDIX_EXTENSION", tmp_path / "missing.ts")

    class _Proc:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    backend = NativeBackend(_settings(tmp_path), lambda *_: None)
    proc = _Proc()
    backend._procs["c_1"] = proc

    asyncio.run(backend.refresh_context("c_1"))

    assert proc.stopped
    assert "c_1" not in backend._procs


def test_refreshing_the_context_never_interrupts_a_turn(tmp_path: Path, monkeypatch) -> None:
    """The supervisor checks, and the backend checks again for the gap between the two.

    Stopping a process mid-turn would abandon work the user is waiting on. A one-turn-stale
    memory is the worse-looking and better-behaved failure.
    """
    monkeypatch.setattr("app.agent.backends.native.APPENDIX_EXTENSION", tmp_path / "missing.ts")

    class _Proc:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    backend = NativeBackend(_settings(tmp_path), lambda *_: None)
    proc = _Proc()
    backend._procs["c_1"] = proc
    backend._streaming.add("c_1")

    asyncio.run(backend.refresh_context("c_1"))

    assert not proc.stopped
    assert "c_1" in backend._procs


def test_a_dream_that_changed_the_memory_refreshes_the_agent(tmp_path: Path) -> None:
    """The supervisor wires the two halves together: Dream changed files, so restart Pi.

    Without this line the memory layer is correct on disk and stale in the only process that
    reads it. A Dream that changed nothing must not restart anything — that would be churn
    with a process kill.
    """

    class _Backend:
        name = "native"

        def __init__(self) -> None:
            self.refreshed: list[str] = []

        async def refresh_context(self, conversation_id: str) -> None:
            self.refreshed.append(conversation_id)

        def is_streaming(self, conversation_id: str) -> bool:
            return False

    class _Dream:
        def __init__(self, changed: bool) -> None:
            self.changed = changed

        async def run(self) -> DreamResult:
            return DreamResult(ran=True, changed=self.changed, advanced=True)

    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    backend = _Backend()
    supervisor = Supervisor(settings, db, AgentBroker(), backend=backend)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO agent_threads "
            "(id, title, backend, backend_conversation_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("t_1", "the conversation", "native", "c_1", "2026-01-01T00:00:00.000Z",
             "2026-01-01T00:00:00.000Z"),
        )

    supervisor._dream = _Dream(changed=True)  # type: ignore[assignment]
    asyncio.run(supervisor.run_dream())
    assert backend.refreshed == ["c_1"]

    supervisor._dream = _Dream(changed=False)  # type: ignore[assignment]
    asyncio.run(supervisor.run_dream())
    assert backend.refreshed == ["c_1"], "a Dream that changed nothing restarts nothing"
    db.close()


def test_idle_compaction_is_on_by_default() -> None:
    """The agent has one conversation that never ends (§19), so its context only grows.

    Pi compacts on its own when the window is nearly full; this is the proactive half, and it is
    on for the same reason Dream is: maintenance that only happens when someone remembers to ask
    is maintenance that does not happen. Zero turns it off.
    """
    from app.config import Settings

    assert Settings.__dataclass_fields__["agent_idle_compact_minutes"].default == 15.0


def test_compact_when_idle_needs_a_live_process(tmp_path: Path) -> None:
    """Never spawn a process to compact. A conversation nobody is using costs nothing, and
    spawning Node plus a model call for a closed page is the opposite of maintenance."""
    backend = NativeBackend(_settings(tmp_path), lambda *_: None)

    assert asyncio.run(backend.compact_when_idle("c_1", idle_seconds=3600)) is False
    assert backend._procs == {}


def test_compact_when_idle_is_one_attempt_per_new_message(tmp_path: Path) -> None:
    """Pi refuses a session that is too small or already compacted. Remembering the newest
    message id is what stops an idle conversation from asking — and logging — every scan."""

    class _Proc:
        alive = True

        def __init__(self) -> None:
            self.requests: list[dict] = []

        async def request(self, command: dict, *, timeout: float | None = None) -> dict:
            self.requests.append({"command": command, "timeout": timeout})
            return {"success": True}

    backend = NativeBackend(_settings(tmp_path), lambda *_: None)
    proc = _Proc()
    backend._procs["c_1"] = proc
    entries = [
        {"type": "session", "id": "s"},
        {"type": "message", "id": "m1"},
    ]
    backend._entries_on_disk = lambda conversation_id: entries  # type: ignore[method-assign]

    assert asyncio.run(backend.compact_when_idle("c_1", idle_seconds=900)) is True
    assert proc.requests[0]["command"] == {"type": "compact"}
    # The summary is a model call, so this request is bounded like Dream, not like an ack.
    assert proc.requests[0]["timeout"] == 300.0

    # Same newest message, same idle conversation: nothing to do, and no second request.
    assert asyncio.run(backend.compact_when_idle("c_1", idle_seconds=1200)) is False
    assert len(proc.requests) == 1

    # A new message re-arms it.
    entries.append({"type": "message", "id": "m2"})
    assert asyncio.run(backend.compact_when_idle("c_1", idle_seconds=900)) is True
    assert len(proc.requests) == 2


def test_compact_when_idle_never_interrupts_a_turn(tmp_path: Path) -> None:
    """Pi's manual `compact` aborts the current operation first, so a streaming conversation
    must never be asked to compact."""

    class _Proc:
        alive = True

        def __init__(self) -> None:
            self.requests = 0

        async def request(self, command: dict, *, timeout: float | None = None) -> dict:
            self.requests += 1
            return {"success": True}

    backend = NativeBackend(_settings(tmp_path), lambda *_: None)
    proc = _Proc()
    backend._procs["c_1"] = proc
    backend._streaming.add("c_1")
    backend._entries_on_disk = lambda conversation_id: [{"type": "message", "id": "m1"}]

    assert asyncio.run(backend.compact_when_idle("c_1", idle_seconds=3600)) is False
    assert proc.requests == 0


def test_a_refused_compaction_is_not_an_error(tmp_path: Path) -> None:
    """Pi answers "Nothing to compact (session too small)" for a short conversation. That is a
    result — there is nothing worth shrinking — not a failure to retry or to surface."""
    from app.agent.rpc import PiCommandError

    class _Proc:
        alive = True

        async def request(self, command: dict, *, timeout: float | None = None) -> dict:
            raise PiCommandError("compact", {"error": "Nothing to compact (session too small)"})

    backend = NativeBackend(_settings(tmp_path), lambda *_: None)
    backend._procs["c_1"] = _Proc()
    backend._entries_on_disk = lambda conversation_id: [{"type": "message", "id": "m1"}]

    assert asyncio.run(backend.compact_when_idle("c_1", idle_seconds=900)) is False


def test_the_supervisor_only_compacts_threads_that_have_been_idle(tmp_path: Path) -> None:
    """The clock is the supervisor's; the rules are the backend's."""

    class _Backend:
        name = "native"

        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        async def compact_when_idle(self, conversation_id: str, *, idle_seconds: float) -> bool:
            self.calls.append((conversation_id, idle_seconds))
            return True

    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    backend = _Backend()
    supervisor = Supervisor(settings, db, AgentBroker(), backend=backend)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO agent_threads "
            "(id, title, backend, backend_conversation_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("t_old", "old", "native", "c_old",
             "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
        )
        conn.execute(
            "INSERT INTO agent_threads "
            "(id, title, backend, backend_conversation_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("t_now", "now", "native", "c_now", "2026-01-01T00:00:00.000Z", "2099-01-01T00:00:00.000Z"),
        )

    asyncio.run(supervisor.compact_idle_now(900))

    assert [call[0] for call in backend.calls] == ["c_old"]
    db.close()


def test_switching_does_not_wait_for_the_previous_backends_teardown(tmp_path: Path) -> None:
    """The switch response is what the page waits on before it can load the new transcript.

    Pi's `stop()` may sit out its full 5 s grace period, and awaiting it here made switching
    look like "the messages sync slowly". The switch is recorded before the teardown starts,
    so the stop can finish — and fail — on its own.
    """

    class _Backend:
        def __init__(self, name: str) -> None:
            self.name = name
            self.stop_started = asyncio.Event()
            self.stop_finished = False
            self.release = asyncio.Event()

        async def start(self) -> None:
            return

        async def stop(self) -> None:
            self.stop_started.set()
            await self.release.wait()
            self.stop_finished = True

    async def scenario() -> None:
        settings = _settings(tmp_path)
        db = Database(settings.db_path)
        supervisor = Supervisor(settings, db, AgentBroker())
        previous, target = _Backend("native"), _Backend("openclaw")
        supervisor._backends = {"native": previous, "openclaw": target}
        supervisor._active = "native"

        switched = await asyncio.wait_for(supervisor.switch_backend("openclaw"), timeout=1)
        assert switched == "openclaw"
        # The teardown is a task, not a call, so give it its first slice before looking.
        await asyncio.sleep(0)
        assert previous.stop_started.is_set(), "the teardown did begin"
        assert not previous.stop_finished, "and the caller did not wait for it"

        previous.release.set()
        await asyncio.gather(*supervisor._stops)
        assert previous.stop_finished
        db.close()

    asyncio.run(scenario())


def test_unknown_thread_is_404(agent_app) -> None:
    client, _db, _s = agent_app
    assert client.get("/api/agent/threads/t_nope", headers=_headers()).status_code == 404
    assert (
        client.post(
            "/api/agent/threads/t_nope/messages", json={"content": "hi"}, headers=_headers()
        ).status_code
        == 404
    )


def test_blank_message_is_rejected(agent_app) -> None:
    client, _db, _s = agent_app
    thread_id = _new_thread(client)
    assert _say(client, thread_id, "   ").status_code == 422


# --------------------------------------------------------------------------- #
# finding the executable
# --------------------------------------------------------------------------- #
def test_resolve_executable_prefers_one_this_os_can_launch(tmp_path, monkeypatch) -> None:
    """npm installs a CLI as three files side by side, and only some of them can be started.

    On Windows `shutil.which("pi")` returns the extensionless POSIX shell script, which
    `CreateProcess` rejects with "not a valid Win32 application" — the real bug this guards.
    The runnable `.cmd` has to win.
    """
    (tmp_path / "thing").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (tmp_path / "thing.cmd").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path))

    resolved = resolve_executable("thing")
    if os.name == "nt":
        assert resolved.lower().endswith(".cmd"), resolved
    else:
        # POSIX has no such ambiguity: the first hit is the runnable one.
        assert resolved.endswith("thing"), resolved


def test_resolve_executable_passes_through_an_explicit_path(tmp_path) -> None:
    """An absolute path in CSD_PI_BIN must be used as given, not searched for again."""
    script = tmp_path / "fake_pi.py"
    script.write_text("", encoding="utf-8")
    assert resolve_executable(sys.executable) == sys.executable
    assert resolve_executable(str(script)) == str(script)


def test_a_missing_binary_is_a_503_with_a_usable_message(tmp_path: Path) -> None:
    """The first thing a new user hits. It must name the fix, not return an errno.

    Built with its own settings rather than mutating the fixture's: `Settings` is frozen
    precisely so a test cannot reach in and change the configuration under a running app.
    """
    settings = _settings(tmp_path, pi_bin="definitely-not-a-real-binary-xyz")
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        thread_id = _new_thread(client)
        response = _say(client, thread_id, "hi")

        # 503, not 502: "the agent is not there" and "the agent refused this" are the two
        # failures a user has to tell apart.
        assert response.status_code == 503, response.text
        assert "not reachable" in response.json()["detail"]
        assert "definitely-not-a-real-binary-xyz" in response.json()["detail"]
    db.close()


# --------------------------------------------------------------------------- #
# progress notes: a checkpoint is not an answer (design.md §23)
#
# The distinction is Pi's own record, not a guess about wording: an assistant entry whose
# `content` holds tool calls *and* prose is a stage note, and the last entry of a run — the one
# with no tool calls — is the answer. nanobot's channel draws the same line on the wire
# (`kind: "progress"`), which is why this is a frame on the seam rather than a native quirk.
# --------------------------------------------------------------------------- #
def _stage_entry(text: str, *, call: bool = True) -> dict:
    content: list[dict] = [{"type": "text", "text": text}]
    if call:
        content.append({"type": "toolCall", "id": "call-1", "arguments": {"command": "pytest"}})
    return {
        "type": "message",
        "id": "e1",
        "timestamp": "2026-10-19T00:00:00Z",
        "message": {"role": "assistant", "content": content},
    }


def test_a_pi_stage_note_arrives_as_a_checkpoint_not_as_a_reply(tmp_path: Path) -> None:
    """The failure this prevents: half-sentences interleaved with tool rows, and a final answer
    that looks exactly like them.

    A checkpoint is still a `Message` — one shape, so the page has one rendering path and the row
    dedups like any other — but it carries `progress=True`, which is what tells the page to draw
    it as a stage note rather than as the reply (`docs/design.md` §23).
    """
    from app.agent.backends.base import Message

    events: list = []
    backend = NativeBackend(_settings(tmp_path), lambda _cid, event: events.append(event))
    backend._handle("c_1", {"type": "message_end", "message": _stage_entry("running the tests")["message"]})

    assert [type(event).__name__ for event in events] == ["Message"]
    assert isinstance(events[0], Message)
    assert (events[0].progress, events[0].content) == (True, "running the tests")


def test_the_runs_last_message_is_the_answer_and_stays_one(tmp_path: Path) -> None:
    from app.agent.backends.base import Message

    events: list = []
    backend = NativeBackend(_settings(tmp_path), lambda _cid, event: events.append(event))
    backend._handle("c_1", {"type": "message_end", "message": _stage_entry("all green", call=False)["message"]})

    assert [type(event).__name__ for event in events] == ["Message"]
    assert events[0].progress is False


def test_a_checkpoint_survives_a_re_read_as_a_checkpoint() -> None:
    """The same words must not render as a checkpoint while streaming and as a reply after a
    refresh. Pi's entry is the single source for both, so the flag is derived there too."""
    from app.agent.backends.native import _entries_to_messages

    messages = _entries_to_messages([_stage_entry("running the tests"), _stage_entry("all green", call=False)])

    assert [m.progress for m in messages] == [True, False]
    assert [m.content for m in messages] == ["running the tests", "all green"]


def test_progress_notes_ride_in_the_child_environment(tmp_path: Path) -> None:
    """Pi reads the appendix per prompt through the extension, so its two halves travel in the
    environment: the files by path, the fixed instruction by text."""
    backend = NativeBackend(_settings(tmp_path), lambda *_: None)

    env = backend._child_env()

    assert "Progress notes" in env["CSD_AGENT_PROGRESS_NOTES"]
    # The machines file is listed even before it exists: the list is fixed at spawn, so a path
    # omitted now would stay invisible until the process restarted.
    assert env["CSD_AGENT_APPENDIX"].split(",")[-1].endswith("MACHINES.md")


def test_the_registry_index_points_at_the_file_without_carrying_it(tmp_path: Path) -> None:
    """The registry holds every machine's details; the prompt holds one paragraph.

    That split is the reason the file exists at all (docs/design.md §24): the appendix is paid
    for on every message, and a fleet-sized list of hosts is the one thing on this page that
    grows without bound.
    """
    from app.agent.appendix import appendix_text, registry_index

    settings = _settings(tmp_path)
    path = settings.resolved_registry_file
    assert path is not None
    assert path.name == "MACHINES.registered.md"

    # The fallback path (no extension file) still names the file — but promises no tool, because
    # on that path there is nothing to call.
    text = appendix_text(settings)
    assert text is not None
    assert "MACHINES.registered.md" in text
    assert "csd_registry" not in text

    with_tool = registry_index(path, tool=True)
    assert with_tool is not None and "csd_registry" in with_tool


def test_the_registry_tool_travels_in_the_child_environment(tmp_path: Path) -> None:
    """The tool answers by reading the database itself, from the agent's own machine, so what it
    needs in the environment is paths — no token, and nothing that can write."""
    backend = NativeBackend(_settings(tmp_path), lambda *_: None)

    env = backend._child_env()

    assert env["CSD_AGENT_DB_PATH"].endswith("test.sqlite3")
    assert env["CSD_AGENT_PYTHON"] == sys.executable
    assert env["CSD_AGENT_BACKEND_DIR"].endswith("backend")
    assert "csd_registry" in env["CSD_AGENT_REGISTRY_NOTE"]


def test_the_registry_index_drops_the_tool_when_its_extension_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """A prompt naming a tool that is not loaded is worse than a shorter prompt: the agent
    calls it, fails, and blames the machine it was trying to reach."""
    monkeypatch.setattr("app.agent.backends.native.REGISTRY_EXTENSION", tmp_path / "missing.ts")

    backend = NativeBackend(_settings(tmp_path), lambda *_: None)

    env = backend._child_env()
    assert "csd_registry" not in env["CSD_AGENT_REGISTRY_NOTE"]
    assert "MACHINES.registered.md" in env["CSD_AGENT_REGISTRY_NOTE"]
    assert "CSD_AGENT_DB_PATH" not in env
    assert "-e" in backend.build_argv(session_id="c_1", title="t")


def test_progress_notes_can_be_turned_off(tmp_path: Path, monkeypatch) -> None:
    """An operator who does not want the agent narrating gets silence, on both paths."""
    monkeypatch.setattr("app.agent.backends.native.APPENDIX_EXTENSION", tmp_path / "missing.ts")
    settings = dataclasses.replace(_settings(tmp_path), agent_progress=False)
    memory_dir = settings.resolved_memory_dir
    assert memory_dir is not None
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("SQLite is the record.", encoding="utf-8")

    backend = NativeBackend(settings, lambda *_: None)
    assert "CSD_AGENT_PROGRESS_NOTES" not in backend._child_env()

    # The fallback path still carries the memory — it is the *notes* that are switched off, not
    # the appendix.
    argv = backend.build_argv(session_id=None, title="t")
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "SQLite is the record." in prompt
    assert "Progress notes" not in prompt


def test_the_machines_file_is_what_tells_the_agent_where_it_may_go(tmp_path: Path) -> None:
    """Driving a remote coding agent is `ssh` from the agent's own shell, so what it needs is the
    knowledge a person would have — not a tool. It is a file because it is prose about somebody's
    infrastructure, and it is read at prompt time so an edit lands on the next message."""
    from app.agent.appendix import appendix_text, machines_section

    settings = _settings(tmp_path)
    workspace = settings.pi_workspace
    assert workspace is not None
    (workspace / "MACHINES.md").write_text("lab-box — `ssh lab-box`, repo at /srv/code", encoding="utf-8")

    text = appendix_text(settings)
    assert text is not None
    assert "lab-box" in text
    assert "never invent one" in text

    # No file, no section — and no complaint: an agent that can only work locally still works.
    (workspace / "MACHINES.md").unlink()
    assert machines_section(settings.resolved_machines_file) is None


def test_a_conversation_spread_over_two_files_reads_as_one(tmp_path: Path) -> None:
    """Pi starts a **new session file** each time it continues a session it does not have open.

    That is every service restart and every process it loses, and each of those files carries the
    same `id` in its header. Reading only the first match is how a live conversation went
    invisible on 2026-10-19: the deployment showed the file Pi wrote *before* the restart and
    never the one it was writing to now, so a prompt and its answer sat on disk unseen. The order
    is the filenames' — Pi begins each with the ISO timestamp it was created at.
    """
    settings = _settings(tmp_path, pi_bin="definitely-not-a-real-binary-xyz")
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        thread_id = _new_thread(client)
        session_dir = settings.pi_session_dir
        assert session_dir is not None
        session_dir.mkdir(parents=True, exist_ok=True)
        store.set_thread_conversation(db, thread_id, "c_split")

        def write(name: str, *entries: dict) -> None:
            header = {"type": "session", "version": 3, "id": "c_split", "cwd": str(tmp_path)}
            (session_dir / name).write_text(
                "\n".join(json.dumps(e) for e in (header, *entries)) + "\n", encoding="utf-8"
            )

        write(
            "2026-01-01T00-00-00-000Z_c_split.jsonl",
            {
                "type": "message",
                "id": "a1",
                "message": {"role": "user", "content": [{"type": "text", "text": "before"}]},
            },
        )
        write(
            "2026-01-02T00-00-00-000Z_c_split.jsonl",
            {
                "type": "message",
                "id": "b1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "running the tests"},
                        {"type": "toolCall", "id": "call-1", "arguments": {"command": "pytest"}},
                    ],
                },
            },
        )

        response = client.get(f"/api/agent/threads/{thread_id}", headers=_headers())
        assert response.status_code == 200, response.text
        messages = response.json()["messages"]

    assert [m["id"] for m in messages] == ["a1", "b1"], "oldest file first"
    assert messages[1]["progress"] is True
    db.close()
