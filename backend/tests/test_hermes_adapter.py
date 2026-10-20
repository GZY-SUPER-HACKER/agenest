"""The Hermes adapter, against a stub API server.

No Hermes install and no network: a real HTTP server on a real socket, scripted inside the
test process, so what is pinned is *our* side of the API server's contract and our policy —
not Hermes' ability to run an agent, and not whether anyone has a key today.

The cases here are the ones where guessing differently would cost something invisible:

- **The SSE stream carries `: keepalive` comments.** Hermes emits one whenever a turn has
  said nothing for ten seconds, on purpose, so a slow tool does not look like a dead
  connection. A parser that treats a comment line as data breaks on the first slow tool.
- **`hermes.tool.progress` names itself on the SSE `event:` line, not inside the JSON.**
  Its payload is `{"tool", "toolCallId", "status"}` with no `type` field at all, so a client
  dispatching on `data.type` drops every tool event without an error anywhere.
- **`finish_reason: "cancelled"` is not success.** Hermes reports `cancelled` for an
  interrupted turn and `error` for a failure, and `stop` only for a real completion.
- **The session is named in a header** (`X-Hermes-Session-Id`), so the request continues
  *that* conversation and Hermes answers from its own history.
- **The transcript comes back under `data`, and its row ids are integers.** Both were
  assumed the other way round; the first made `history()` return empty forever and the
  second would have silently dropped every id the page dedups on.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from app.agent.backends import BackendUnavailable, Delta, Message, Notice, ToolCall
from app.agent.backends.hermes import HermesBackend
from app.schemas import AgentRole

API_KEY = "test-api-key"

#: What the scripted turn says once its chunks are assembled.
ASSEMBLED = "Hello there, friend."

#: The real 401 body, word for word. Its shape is worth keeping: the adapter lifts the
#: message out of it and puts it in front of a person.
_INVALID_KEY = {"error": {"message": "Invalid API key", "type": "invalid_request_error",
                          "code": "invalid_api_key"}}


class StubHermes:
    """A scripted Hermes API server, on its own thread and a real socket.

    `ThreadingHTTPServer` rather than a mocked transport for the same reason the other
    adapters use a real socket: the URL, the headers, the status codes and the SSE framing
    are all part of what is under test.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.port: int = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        #: Knobs the tests turn.
        self.require_key = True
        #: Which terminal event the scripted run ends with. The four the server really
        #: sends, and the whole point of having them separately: a stopped run and a failed
        #: run are not a completed answer.
        self.terminal = "run.completed"
        self.terminal_error = "the model refused"
        self.send_tool_event = True
        #: The `error` flag on `tool.completed`. The event name is `tool.completed` either
        #: way — a failed tool is only visible through this field.
        self.tool_error = False
        self.send_reasoning = True
        self.send_keepalive = True
        self.missing_session = False
        self.missing_transcript = False
        #: Seconds between frames, so a test can act while a turn is still streaming.
        self.stream_delay = 0.0
        #: The transcript as `GET /api/sessions/{id}/messages` really returns it: rows carry
        #: an **integer** `id` and a nullable `tool_call_id`, and the list lives under `data`.
        #: Every field the real rows have is here, including the ones nothing reads, so that
        #: a client relying on a field that is not actually sent fails here rather than in
        #: production.
        self.transcript: list[dict] = [
            {"id": 1, "role": "system", "content": "You are Hermes.", "tool_call_id": None,
             "tool_calls": None, "tool_name": None, "timestamp": 1.0, "token_count": None,
             "finish_reason": None, "reasoning": None, "reasoning_content": None},
            {"id": 2, "role": "user", "content": "hi", "tool_call_id": None, "tool_calls": None,
             "tool_name": None, "timestamp": 1792341762.0, "token_count": None,
             "finish_reason": None, "reasoning": None, "reasoning_content": None},
            {"id": 3, "role": "assistant", "content": "Hello there, friend.",
             "tool_call_id": None, "tool_calls": None, "tool_name": None,
             "timestamp": 1792341763.0, "token_count": None, "finish_reason": "stop",
             "reasoning": None, "reasoning_content": None},
        ]
        self.stop_calls: list[str] = []
        #: Runs created through `POST /v1/runs`, and what the adapter asked for in each.
        #: The body is where `session_id` and the model live, so the tests read it rather
        #: than trusting that the adapter sent what it says it sends.
        self.created_runs: list[dict] = []
        self.run_counter = 0
        #: What `GET /v1/runs/{id}` reports once a run is over.
        self.run_status: dict[str, str] = {}

    # -- lifecycle ------------------------------------------------------ #
    def start(self) -> int:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _handler_class(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep the test output clean
                pass

            # -- helpers ------------------------------------------------ #
            def _record(self, method: str) -> None:
                stub.requests.append({
                    "method": method,
                    "path": urlparse(self.path).path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self._read_body(),
                })

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return {}

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorised(self) -> bool:
                if not stub.require_key:
                    return True
                return self.headers.get("Authorization") == f"Bearer {API_KEY}"

            # -- routes ------------------------------------------------- #
            def do_GET(self) -> None:  # noqa: N802 - stdlib spelling
                self._record("GET")
                path = urlparse(self.path).path
                if path == "/health":
                    # Unauthenticated on the real server: a bad key still gets a 200 here.
                    # Pinned because it means this endpoint cannot be used to check a
                    # credential, and a client that assumes otherwise reports a wrong key as
                    # a healthy connection.
                    self._json(200, {"status": "ok", "platform": "hermes-agent",
                                     "version": "0.18.2"})
                    return
                if path == "/api/sessions":
                    # The endpoint that *does* check the key, which is why the adapter probes
                    # it rather than trusting the open `/health`.
                    if not self._authorised():
                        self._json(401, _INVALID_KEY)
                        return
                    self._json(200, {"object": "list", "data": []})
                    return
                if path.startswith("/v1/runs/") and path.endswith("/events"):
                    if not self._authorised():
                        self._json(401, _INVALID_KEY)
                        return
                    self._run_events()
                    return
                if path.startswith("/v1/runs/"):
                    if not self._authorised():
                        self._json(401, _INVALID_KEY)
                        return
                    run_id = path.split("/")[3]
                    self._json(200, {
                        "object": "hermes.run",
                        "run_id": run_id,
                        "status": stub.run_status.get(run_id, "running"),
                    })
                    return
                if path.startswith("/api/sessions/") and path.endswith("/messages"):
                    if not self._authorised():
                        self._json(401, _INVALID_KEY)
                        return
                    self._transcript()
                    return
                if path.startswith("/api/sessions/"):
                    if not self._authorised():
                        self._json(401, _INVALID_KEY)
                        return
                    if stub.missing_session:
                        self._json(404, {"error": "no such session"})
                        return
                    self._json(200, {"object": "hermes.session", "session": {
                        "id": path.rsplit("/", 1)[-1], "source": "api_server"}})
                    return
                self._json(404, {"error": "no such route"})

            def do_POST(self) -> None:  # noqa: N802 - stdlib spelling
                self._record("POST")
                path = urlparse(self.path).path
                if path == "/api/sessions":
                    if not self._authorised():
                        self._json(401, _INVALID_KEY)
                        return
                    # The real answer: 201, the id nested under `session`, and no top-level
                    # `id` at all. `source` is the server's own — it records `api_server`
                    # whatever the caller asked for.
                    self._json(201, {"object": "hermes.session", "session": {
                        "id": "api_1792341918_2c6ef3d0", "source": "api_server",
                        "model": "hermes-agent", "message_count": 0}})
                    return
                if path == "/v1/runs":
                    if not self._authorised():
                        self._json(401, _INVALID_KEY)
                        return
                    # The real answer: HTTP **202** with a `run_id` and nothing else. The
                    # adapter needs that id to stop the run, so it comes from here rather
                    # than being read out of a later frame.
                    stub.created_runs.append(stub.requests[-1]["body"])
                    stub.run_counter += 1
                    run_id = f"run_{stub.run_counter:04d}"
                    stub.run_status[run_id] = "running"
                    self._json(202, {"run_id": run_id, "status": "started"})
                    return
                if path.startswith("/v1/runs/") and path.endswith("/stop"):
                    run_id = path.split("/")[3]
                    stub.stop_calls.append(run_id)
                    # Verified against a live server: the call answers `stopping` and the run
                    # settles as `cancelled`.
                    stub.run_status[run_id] = "cancelled"
                    self._json(200, {"run_id": run_id, "status": "stopping"})
                    return
                self._json(404, {"error": "no such route"})

            # -- bodies ------------------------------------------------- #
            def _transcript(self) -> None:
                if stub.missing_transcript:
                    self._json(404, {"error": "no such session"})
                    return
                # The rows are under `data`, not `messages` — and that one word was the
                # difference between a working transcript and a permanently empty one.
                self._json(200, {
                    "object": "list",
                    "session_id": "sess-1",
                    "data": stub.transcript,
                })

            def _run_events(self) -> None:
                """`GET /v1/runs/{run_id}/events`, frame for frame as the server sends it.

                Every frame is `data: {"event": …}` — the name is a *field* here, unlike
                `hermes.tool.progress` whose name lived on an SSE `event:` line. That
                difference is the whole reason this adapter reads the field.
                """
                run_id = urlparse(self.path).path.split("/")[3]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                if stub.send_keepalive:
                    # Sent when a turn has been quiet, and not data. A parser that reads it
                    # as data corrupts the reply; the exact-text assertions are what catch it.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()

                def emit(name: str, **fields) -> None:
                    self._data({"event": name, "run_id": run_id,
                                "timestamp": time.time(), **fields})

                emit("run.started", user_message={"role": "user", "content": "hello"})
                emit("message.started", message={"id": "m-1", "role": "assistant"})
                for chunk in ("Hello ", "there, "):
                    emit("message.delta", delta=chunk)
                if stub.send_tool_event:
                    emit("tool.started", tool="read_file", preview="activate.sh")
                    emit("tool.completed", tool="read_file", duration=1.821,
                         error=stub.tool_error)
                if stub.send_reasoning:
                    emit("reasoning.available", text="The user wants a greeting.")
                emit("message.delta", delta="friend.")

                extra: dict = {}
                if stub.terminal == "run.completed":
                    extra["output"] = ASSEMBLED
                    extra["usage"] = {"input_tokens": 10, "output_tokens": 4}
                elif stub.terminal == "run.failed":
                    # The only terminal event that carries a reason.
                    extra["error"] = stub.terminal_error
                emit(stub.terminal, **extra)
                self.wfile.write(b": stream closed\n\n")
                self.wfile.flush()

            def _data(self, payload: dict) -> None:
                self._sse(json.dumps(payload))

            def _sse(self, payload: str) -> None:
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                time.sleep(stub.stream_delay or 0.01)

        return Handler


def _backend(stub: StubHermes, sink) -> HermesBackend:
    return HermesBackend(
        base_url=stub.base_url(),
        api_key=API_KEY,
        on_event=sink,
    )


class Collected:
    """An `EventSink` that keeps whatever the adapter emitted."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def __call__(self, conversation_id: str, event: object) -> None:
        self.events.append((conversation_id, event))

    def of(self, kind):
        return [event for _, event in self.events if isinstance(event, kind)]


def _drive_turn(stub: StubHermes, sink: Collected, *, message: str = "hello"):
    """Run the adapter through one turn on its own event loop, and return the session id."""

    async def run():
        backend = _backend(stub, sink)
        await backend.start()
        try:
            assert backend.status().connected, backend.status().detail
            session = await backend.new_conversation()
            sent = await backend.send(session, message)
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and not sink.of(Message):
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)  # let the frames after the reply land
            return session, sent
        finally:
            await backend.stop()

    return asyncio.run(run())


@pytest.fixture()
def server():
    stub = StubHermes()
    stub.start()
    yield stub
    stub.stop()


# --------------------------------------------------------------------------- #
# the streaming contract
# --------------------------------------------------------------------------- #
def test_the_reply_is_assembled_from_sse_chunks(server) -> None:
    """Chunks in, one message out — and the leading `: keepalive` comment contributed
    nothing, which is what the exact-text assertion is defending: a parser that treated a
    comment line as data would fold it into the reply."""
    sink = Collected()
    _drive_turn(server, sink)

    said = [m for m in sink.of(Message) if m.role == AgentRole.assistant]
    assert [m.content for m in said] == [ASSEMBLED]
    assert any(isinstance(d, Delta) for d in sink.of(Delta)), "no deltas were emitted"


def test_a_cancelled_turn_is_not_reported_as_finished(server) -> None:
    """A stopped run ends with `run.cancelled`, and showing that as a finished answer would
    present a run someone stopped as a completed one."""
    server.terminal = "run.cancelled"
    sink = Collected()
    _drive_turn(server, sink)

    said = [m for m in sink.of(Message) if m.role == AgentRole.assistant]
    assert said, "the part that arrived before the stop was dropped"
    assert said[0].is_error is False, "a user's own stop is not an agent failure"
    notices = sink.of(Notice)
    assert any(n.notice == "stopped" for n in notices), [n.notice for n in notices]


def test_a_failed_run_is_reported_as_a_failure(server) -> None:
    """`run.failed` carries the reason and must not read as an answer. Reading only "a
    terminal event arrived" as success is how a failure gets shown as a reply."""
    server.terminal = "run.failed"
    sink = Collected()
    _drive_turn(server, sink)

    said = [m for m in sink.of(Message) if m.role == AgentRole.assistant]
    assert said and said[0].is_error is True, "a failed run was recorded as a good answer"
    errors = [n for n in sink.of(Notice) if n.level == "error"]
    assert errors, "a failed run said nothing about why"
    assert "refused" in (errors[0].detail or ""), errors[0].detail


def test_an_interrupted_run_is_not_called_a_failure(server) -> None:
    """`run.interrupted` means the *gateway* went away mid-turn — nothing the agent did went
    wrong. Labelling it an agent error would blame the model for a restart."""
    server.terminal = "run.interrupted"
    sink = Collected()
    _drive_turn(server, sink)

    said = [m for m in sink.of(Message) if m.role == AgentRole.assistant]
    assert said and said[0].is_error is False
    assert not [n for n in sink.of(Notice) if n.level == "error"], "a restart blamed on the agent"
    assert any(n.notice == "interrupted" for n in sink.of(Notice)), [n.notice for n in sink.of(Notice)]


def test_a_successful_turn_is_reported_as_success(server) -> None:
    """`run.completed` is the one terminal event that is an answer."""
    server.terminal = "run.completed"
    sink = Collected()
    _drive_turn(server, sink)

    said = [m for m in sink.of(Message) if m.role == AgentRole.assistant]
    assert [m.content for m in said] == [ASSEMBLED]
    assert said[0].is_error is False
    assert not [n for n in sink.of(Notice) if n.level == "error"]


def test_a_tool_becomes_a_start_and_an_end(server) -> None:
    """`tool.started` / `tool.completed` are what let the page show that the agent is
    working, and not merely that it is talking."""
    sink = Collected()
    _drive_turn(server, sink)

    calls = [c for c in sink.of(ToolCall) if c.name == "read_file"]
    assert [c.phase for c in calls] == ["start", "end"], [c.phase for c in calls]
    # No correlation id on this surface, and none invented from the tool's name.
    assert all(c.call_id is None for c in calls), [c.call_id for c in calls]
    # No result either, so nothing is written into the transcript on this side.
    assert not [m for m in sink.of(Message) if m.role == AgentRole.tool]


def test_a_failed_tool_is_reported_as_failed(server) -> None:
    """The outcome is `error` on `tool.completed` — the event name is `tool.completed`
    whether the tool worked or not.

    This is the field the adapter moved surfaces to get. The Chat Completions event that
    used to carry tool activity had none, so a tool that failed and one that worked were
    identical on screen.
    """
    server.tool_error = True
    sink = Collected()
    _drive_turn(server, sink)

    ends = [c for c in sink.of(ToolCall) if c.phase == "end"]
    assert ends and ends[0].is_error is True, "a failed tool was reported as a success"


def test_reasoning_arrives_as_a_thinking_delta(server) -> None:
    """`reasoning.available` is Hermes' own reasoning, and it goes out on the `thinking`
    channel — never as prose. Folding it into the reply would put the agent's working-out
    in the transcript as something it said."""
    sink = Collected()
    _drive_turn(server, sink)

    thinking = [d for d in sink.of(Delta) if d.channel == "thinking"]
    assert thinking, "no reasoning was emitted"
    assert "greeting" in thinking[0].text
    assert all(t.channel == "thinking" for t in thinking)
    # And the answer is the answer, with no reasoning folded into it.
    said = [m for m in sink.of(Message) if m.role == AgentRole.assistant]
    assert [m.content for m in said] == [ASSEMBLED]


def test_send_creates_a_run_against_the_session_and_carries_an_idempotency_key(server) -> None:
    """`session_id` is what keeps the turn on this conversation — it is what makes Hermes
    load and write back *that* transcript — and the idempotency key is what makes a retry
    not a second run."""
    sink = Collected()
    session, _sent = _drive_turn(server, sink)

    assert server.created_runs, "no run was created"
    body = server.created_runs[0]
    assert body["session_id"] == session
    assert body["input"]
    # `hermes-agent` is a sentinel meaning "Hermes' own configured provider", which on this
    # surface is spelled by leaving the field out.
    assert "model" not in body, body

    starts = [r for r in server.requests if r["path"] == "/v1/runs" and r["method"] == "POST"]
    assert starts[0]["headers"].get("idempotency-key")
    assert starts[0]["headers"].get("authorization") == f"Bearer {API_KEY}"
    # The events stream is a *second* request, and it is the one that carries the SSE.
    events = [r for r in server.requests if r["path"].endswith("/events")]
    assert events, "the adapter never attached to the run's event stream"


def test_send_does_not_invent_a_message_id(server) -> None:
    """A run id names a *run*, not the user's transcript row, and the row is only readable
    afterwards. Handing the run id back as a message id would put a value on the page that
    never matches what history returns — a duplicate wearing a wrong label."""
    sink = Collected()
    _session, sent = _drive_turn(server, sink)

    assert sent is None


def test_abort_stops_the_run_the_turn_created(server) -> None:
    """The stop button, which is the reason this adapter is on `/v1/runs` at all: a Chat
    Completions turn produced no id that `POST /v1/runs/{id}/stop` would accept."""
    server.stream_delay = 0.2
    sink = Collected()

    async def run():
        backend = _backend(server, sink)
        await backend.start()
        try:
            session = await backend.new_conversation()
            await backend.send(session, "hello")
            for _ in range(100):
                if session in backend._streaming:
                    break
                await asyncio.sleep(0.05)
            return session, await backend.abort(session)
        finally:
            await backend.stop()

    session, stopped = asyncio.run(run())
    assert stopped is True
    # The id the stub issued for this test's single run — number 1, since the fixture is
    # fresh per test.
    assert server.stop_calls == ["run_0001"], server.stop_calls
    assert session  # the session id came back from the create call


def test_abort_with_nothing_running_is_false(server) -> None:
    sink = Collected()

    async def run():
        backend = _backend(server, sink)
        await backend.start()
        try:
            return await backend.abort("sess-never-used")
        finally:
            await backend.stop()

    assert asyncio.run(run()) is False


# --------------------------------------------------------------------------- #
# history — read from the agent, never kept here
# --------------------------------------------------------------------------- #
def test_history_comes_from_the_agents_own_transcript(server) -> None:
    """A backend whose agent keeps a readable transcript returns it and stores nothing.
    System rows are dropped on the way: they are not things either party said.

    Two shape facts are pinned here, and getting either wrong made this method useless in a
    different way: the rows arrive under **`data`** (reading `messages` returned an empty
    list forever), and their ids are **integers** (a string-only reader dropped every id the
    page dedups on).
    """
    sink = Collected()

    async def run():
        backend = _backend(server, sink)
        await backend.start()
        try:
            return await backend.history("sess-1")
        finally:
            await backend.stop()

    messages = asyncio.run(run())
    assert [m.role for m in messages] == [AgentRole.user, AgentRole.assistant]
    assert messages[0].content == "hi"
    # The id the page dedups on has to be the agent's, not one we minted.
    assert [m.id for m in messages] == ["2", "3"], [m.id for m in messages]
    # And the time each message was actually sent, not the time the page asked.
    assert [m.created_at for m in messages] == [
        "2026-10-18T16:42:42.000Z", "2026-10-18T16:42:43.000Z",
    ], [m.created_at for m in messages]


def test_a_transcript_hermes_no_longer_has_is_empty_not_an_error(server) -> None:
    """A thread that outlived its session is an empty conversation, not a page-level
    failure: the alternative takes the whole thread down over a stale id."""
    server.missing_transcript = True
    sink = Collected()

    async def run():
        backend = _backend(server, sink)
        await backend.start()
        try:
            return await backend.history("sess-gone")
        finally:
            await backend.stop()

    assert asyncio.run(run()) == []


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def test_a_missing_api_key_is_reported_as_missing_not_as_down(server, monkeypatch) -> None:
    """Without the key every request is a 401, so "is it running?" sends someone to check
    the wrong thing. `is_available` is what the settings row shows for that."""
    sink = Collected()

    async def run():
        backend = HermesBackend(base_url=server.base_url(), api_key=None, on_event=sink)
        assert backend.is_available() is False
        await backend.start()
        try:
            assert backend.status().connected is False
            assert "API key" in (backend.status().detail or "")
            with pytest.raises(BackendUnavailable):
                await backend.new_conversation()
        finally:
            await backend.stop()

    asyncio.run(run())


def test_a_bad_api_key_is_caught_at_startup_not_at_the_first_message(server) -> None:
    """`/health` is **unauthenticated** — the real server answers 200 to a wrong key and to
    no key at all. A client that treats a healthy `/health` as "the credential is fine"
    shows a working connection and then fails on the first thing the user types.

    So `start()` checks the key against an endpoint that does enforce it, and the settings
    row can say which of the two problems it is.
    """
    sink = Collected()

    async def run():
        backend = HermesBackend(base_url=server.base_url(), api_key="wrong-key", on_event=sink)
        assert backend.is_available() is True  # a key *is* configured; it is just wrong
        await backend.start()
        try:
            assert backend.status().connected is False
            assert "API key" in (backend.status().detail or ""), backend.status().detail
            with pytest.raises(BackendUnavailable):
                await backend.new_conversation()
        finally:
            await backend.stop()

    asyncio.run(run())


def test_a_server_that_is_not_there_is_not_connected(server) -> None:
    """Starting must not raise: the page has to come up and *say* the server is missing."""
    sink = Collected()

    async def run():
        backend = HermesBackend(base_url="http://127.0.0.1:9", api_key=API_KEY, on_event=sink)
        await backend.start()
        try:
            assert backend.status().connected is False
            with pytest.raises(BackendUnavailable):
                await backend.send("sess-1", "hello")
        finally:
            await backend.stop()

    asyncio.run(run())
