"""The OpenClaw adapter, against a stub Gateway.

No OpenClaw install and no network: `websockets.serve` runs a scripted stand-in inside the
test process, so what is pinned is *our* side of the Gateway protocol and our policy — not
OpenClaw's ability to run an agent, and not whether anyone has a Gateway today.

The cases here are the ones where guessing differently would cost something invisible:

- **The reply is assembled from `chat` deltas**, with a `replace` delta in the middle
  restarting the text. Appending it would render the answer twice, and waiting for a
  `message` frame (the nanobot habit) would record every reply as empty.
- **`tool-events` is advertised at connect.** The Gateway registers only clients that ask,
  and its refusal to send tool events is silent — so the absence of a tool row looks like
  "this agent has no tools" rather than "you forgot a capability".
- **The subscription is connection-scoped.** A Gateway restart drops it, and the failure
  mode when the client does not re-subscribe is silence, not an error.
- **`PAIRING_REQUIRED` is a human step, not a bug**, so the message has to name the command
  that resolves it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from pathlib import Path

import pytest
import websockets
from app.agent import store
from app.agent.backends import BackendUnavailable, Delta, Message, ToolCall
from app.agent.backends.openclaw import OpenClawBackend
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.schemas import AgentRole
from app.stream import AgentBroker, Broker
from fastapi.testclient import TestClient

ACCESS_TOKEN = "test-access-token"

#: What the scripted turn says once the deltas are assembled.
ASSEMBLED = "Hello there, friend."


class StubGateway:
    """A scripted OpenClaw Gateway.

    Same shape as the nanobot stub, for the same reason: the app under test is driven by
    `TestClient`, which owns *its* event loop, and two loops in one thread deadlocks. A real
    TCP connection is also a better test than mocking the client library — it exercises the
    URL, the handshake, the framing and the request/reply correlation.
    """

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.port: int = 0
        self._port: concurrent.futures.Future[int] = concurrent.futures.Future()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conns: set = set()
        self._counter = 0

        #: Knobs the tests turn.
        self.challenge_delay = 0.0
        self.replace_delta = True
        self.pairing_required = False
        self.send_tool_event = True
        #: Wall-clock gap between sending the challenge and receiving `connect`.
        self.challenge_to_connect: float | None = None

        #: Every key the Gateway hands out comes back namespaced under its agent. Recorded
        #: from a real 2026.10.4 Gateway: propose `csd-b-…`, get `agent:main:csd-b-…` back,
        #: and every `chat` / `agent` event then arrives under *that* spelling. A stub that
        #: echoed the proposed key unchanged would let a client store a conversation id no
        #: event ever mentions — the replies would be dropped without an error anywhere.
        self.key_prefix = "agent:main:"

        #: Which `stream: "tool"` phases the scripted turn emits. The real three, in order:
        #: `start` carries args, `update` carries a `partialResult`, `result` carries the
        #: outcome. `update` is the one that matters — it is neither a second start nor an
        #: end, and a client that reads only `result` as terminal starts the tool twice.
        self.tool_phases = ("start", "update", "result")
        #: Whether the scripted tool failed. The real field is `isError`, and the phase stays
        #: `"result"` either way — so failure is only visible if you read that field.
        self.tool_is_error = False

        #: What `chat.history` serves. Two rows, one carrying text *and* a tool call, so the
        #: block-splitting path is exercised rather than assumed. The envelope is the real
        #: one: row id under `__openclaw.id`, content blocks of `{"type": "text", …}`.
        self.transcript: list[dict] = [
            {"role": "user", "content": "hi", "timestamp": 1792341762000,
             "__openclaw": {"id": "entry-1", "seq": 1}},
            {"role": "assistant", "content": [
                {"type": "text", "text": ASSEMBLED},
                {"type": "tool_use", "id": "call-1", "name": "edit",
                 "input": {"path": "src/thing.py"}},
            ], "timestamp": 1792341763000,
                "__openclaw": {"id": "entry-2", "seq": 2}},
        ]
        #: `kind: "reset"` means the transcript was cleared — a different fact from "empty".
        self.history_reset = False
        #: Lowered by the paging test so a two-row transcript needs two pages.
        self.history_page_cap = 100

    # -- lifecycle ------------------------------------------------------ #
    def start(self) -> int:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.port = self._port.result(timeout=10)
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def force_disconnect(self) -> None:
        """Close every live socket from the server side, as a Gateway restart would."""
        loop = self._loop
        if loop is None:
            return
        for ws in list(self._conns):
            asyncio.run_coroutine_threadsafe(ws.close(), loop)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._main())

    async def _main(self) -> None:
        async with websockets.serve(self._handler, "127.0.0.1", 0) as server:
            self._port.set_result(server.sockets[0].getsockname()[1])
            while not self._stop.is_set():
                await asyncio.sleep(0.02)

    # -- protocol ------------------------------------------------------- #
    async def _res(self, rid, payload: dict, *, ok: bool = True, error: dict | None = None):
        frame = {"type": "res", "id": rid, "ok": ok}
        if ok:
            frame["payload"] = payload
        else:
            frame["error"] = error or {"code": "error", "message": "stub"}
        await self._send(frame)

    async def _event(self, name: str, payload: dict) -> None:
        await self._send({"type": "event", "event": name, "payload": payload})

    async def _send(self, frame: dict) -> None:
        for ws in list(self._conns):
            try:
                await ws.send(json.dumps(frame))
            except Exception:
                pass

    def _frames(self, method: str) -> list[dict]:
        return [f for f in self.received if f.get("method") == method]

    def _normalize(self, key: str | None) -> str:
        """Any key a client proposes, as the Gateway stores it."""
        key = key or "dashboard:%d" % (self._counter + 1)
        return key if key.startswith(self.key_prefix) else self.key_prefix + key

    async def _handler(self, ws) -> None:
        self._conns.add(ws)
        try:
            if self.challenge_delay:
                await asyncio.sleep(self.challenge_delay)
            await ws.send(json.dumps({
                "type": "event",
                "event": "connect.challenge",
                "payload": {"nonce": "stub-nonce", "ts": 1},
            }))
            challenged_at = time.monotonic()

            async for raw in ws:
                frame = json.loads(raw)
                self.received.append(frame)
                method = frame.get("method")
                rid = frame.get("id")
                params = frame.get("params") or {}

                if method == "connect":
                    self.challenge_to_connect = time.monotonic() - challenged_at
                    if self.pairing_required:
                        await self._res(rid, {}, ok=False, error={
                            "code": "PAIRING_REQUIRED",
                            "message": "this device is not paired",
                            "details": {"requestId": "req-42"},
                        })
                        continue
                    await self._res(rid, {
                        "type": "hello-ok",
                        "protocol": 4,
                        "server": {"version": "stub-1", "connId": "conn-1"},
                        "features": {"methods": [], "events": ["chat", "agent"]},
                        "snapshot": {},
                        "auth": {"role": "operator", "scopes": ["operator.read", "operator.write"]},
                        "policy": {"maxPayload": 1000000, "maxBufferedBytes": 1000000,
                                   "tickIntervalMs": 1000},
                    })
                elif method == "sessions.create":
                    # Faithful to the real Gateway: `idempotencyKey` here is rejected
                    # outright, because there is no authenticated principal or device
                    # identity behind a loopback `gateway-client` to make a replay safe.
                    # `chat.send` is a different matter and does require one.
                    if params.get("idempotencyKey"):
                        await self._res(rid, {}, ok=False, error={
                            "code": "INVALID_REQUEST",
                            "message": "idempotent session creation requires an "
                                       "authenticated principal or device identity",
                        })
                        continue
                    self._counter += 1
                    await self._res(rid, {
                        "ok": True,
                        "key": self._normalize(params.get("key")),
                        "sessionId": "sess-%d" % self._counter,
                        "entry": {"sessionId": "sess-%d" % self._counter},
                        "runStarted": False,
                    })
                elif method == "sessions.messages.subscribe":
                    # The subscription reports the key it actually holds, in its namespaced
                    # form. It is also create-or-adopt, so it succeeds for a key that was
                    # never created — which is why the adapter can survive a failed create.
                    await self._res(rid, {
                        "subscribed": True,
                        "key": self._normalize(params.get("key")),
                        "agentId": "main",
                    })
                elif method == "chat.history":
                    await self._history(rid, params)
                elif method == "chat.send":
                    # The real ack names the run and nothing else. No `messageId`, no
                    # `entry.id` — so a client cannot hand back the user's row id and must
                    # say so rather than invent one.
                    await self._res(rid, {"ok": True, "runId": "run-1", "status": "started"})
                    asyncio.create_task(self._turn(params.get("sessionKey")))
                elif method == "chat.abort":
                    await self._res(rid, {"ok": True})
                    await self._event("chat", {
                        "state": "aborted", "runId": params.get("runId") or "run-1",
                        "sessionKey": params.get("sessionKey"), "seq": 99,
                        "stopReason": "aborted",
                    })
                else:
                    await self._res(rid, {"ok": True})
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._conns.discard(ws)

    async def _history(self, rid, params: dict) -> None:
        """`chat.history` as OpenClaw documents it: newest page first, `hasMore` walks back.

        The stub serves pages backwards on purpose — a client that concatenated them in
        arrival order would show the conversation upside down, and that is exactly the bug
        worth pinning.
        """
        if self.history_reset:
            await self._res(rid, {"kind": "reset"})
            return
        rows = list(reversed(self.transcript))
        offset = int(params.get("offset") or 0)
        limit = min(int(params.get("limit") or 100), self.history_page_cap)
        page = rows[offset:offset + limit]
        # The real payload, field for field. Note `nextOffset` is *not* among them — a client
        # that only advanced on `nextOffset` would walk one page and stop.
        payload: dict = {
            "messages": page,
            "hasMore": offset + limit < len(rows),
            "totalMessages": len(rows),
            "pendingInputs": {"items": [], "total": 0, "queuedCount": 0},
            "deltaCursor": "cursor-1",
            "offset": offset,
            "defaults": {"modelProvider": "stub", "model": "stub"},
            "sessionInfo": {"key": self._normalize(params.get("sessionKey"))},
        }
        await self._res(rid, payload)

    async def _turn(self, session: str) -> None:
        """One scripted run, shaped like OpenClaw's `chat` and `agent` event families.

        The turn opens with a draft the model then restarts, and the *same* script is used
        with the restart flag on and off: a client that appends unconditionally keeps the
        discarded draft, and that difference is the whole point of `Delta.replace`.

        Two things here are faithful rather than convenient, and both are load-bearing:

        - **The session key is normalised on the way out.** The Gateway answers under the
          key it stores, not the one the client proposed, so a client that keeps its own
          spelling is emitting events under an id the supervisor has never heard of.
        - **`agent` frames carry `sessionKey`.** They do — along with `sessionId` — so the
          run id does not have to be resolved back to a conversation by hand.
        """
        session = self._normalize(session)
        base = {"runId": "run-1", "sessionKey": session, "agentId": "main"}
        await self._event("chat", dict(base, state="status", phase="preparing_context", seq=0))
        await self._event("chat", dict(base, state="delta", deltaText="Let me think. ", seq=1))
        restart = {"replace": True} if self.replace_delta else {}
        await self._event("chat", dict(base, state="delta", deltaText="Hello there, ",
                                       seq=2, **restart))

        if self.send_tool_event:
            await self._tool_turn(session)

        await self._event("chat", dict(base, state="delta", deltaText="friend.", seq=3))
        await self._event("chat", dict(base, state="final", seq=4, stopReason="stop"))

    async def _tool_turn(self, session: str) -> None:
        """The three `stream: "tool"` phases, as a real Gateway emits them.

        `update` is the one worth scripting: it carries `partialResult`, not `result`, so a
        client that treats "has a result" as "is finished" re-announces the tool's *start*
        here and the page shows the same tool twice. `result` is where it actually ends, and
        the outcome is in `isError` — the phase is `"result"` on failure too.
        """
        call_id = "call-1"
        seq = 5
        for phase in self.tool_phases:
            data: dict = {"phase": phase, "name": "edit", "toolCallId": call_id}
            if phase == "start":
                data["args"] = {"path": "src/thing.py"}
            elif phase == "update":
                data["partialResult"] = {"content": [{"type": "text", "text": "working…"}],
                                         "details": {"status": "running"}}
            else:
                data["isError"] = self.tool_is_error
                data["commandBearing"] = True
                data["result"] = {
                    "content": [{"type": "text", "text": "src/thing.py  +3 -1"}],
                    "details": {"status": "failed" if self.tool_is_error else "completed",
                                "aggregated": "src/thing.py  +3 -1"},
                }
            await self._event("agent", {
                "runId": "run-1", "seq": seq, "stream": "tool", "ts": seq,
                "sessionKey": session, "agentId": "main", "isHeartbeat": False,
                "data": data,
            })
            seq += 1


@pytest.fixture()
def gateway():
    stub = StubGateway()
    stub.start()
    yield stub
    stub.stop()


def _settings(
    tmp_path: Path, *, port: int, dashboard_token: str | None = ACCESS_TOKEN, pairing: bool = False
) -> Settings:
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
        agent_backend="openclaw",
        openclaw_ws_url=f"ws://127.0.0.1:{port}/",
        openclaw_token="gw-token" if not pairing else None,
        openclaw_caps=("tool-events",),
        pi_workspace=tmp_path,
    )


@pytest.fixture()
def app_client(tmp_path: Path, gateway: StubGateway):
    settings = _settings(tmp_path, port=gateway.port)
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        yield client, db, gateway
    db.close()


def _headers(token: str = ACCESS_TOKEN) -> dict[str, str]:
    return {"X-CPD-Token": token}


def _wait_for(predicate, *, timeout: float = 20.0, interval: float = 0.05):
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


def _thread_messages(client: TestClient, thread_id: str) -> list[dict]:
    """What the page gets: the thread route joins our metadata with *the agent's* history."""
    body = client.get(f"/api/agent/threads/{thread_id}", headers=_headers()).json()
    return body["messages"]


class Collected:
    """An `EventSink` that keeps whatever the adapter emitted.

    The core keeps no transcript any more — a backend that can replay supplies messages
    through `history()`, and one that cannot keeps its own — so the adapter's contract here
    is asserted on the events it produces, not on a row in a table that no longer exists.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def __call__(self, conversation_id: str, event: object) -> None:
        self.events.append((conversation_id, event))

    def of(self, kind):
        return [event for _, event in self.events if isinstance(event, kind)]


async def _wait_async(predicate, *, timeout: float = 20.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("condition was not met before the timeout")


def _drive_turn(port: int, sink: Collected, *, message: str = "hello"):
    """Run the adapter through one turn, on its own event loop.

    The stub lives in another thread with its own loop, which is why this is not driven
    through `TestClient`: what is under test here is the adapter's translation of frames
    into events, with no supervisor or database in the way.
    """

    async def run():
        backend = OpenClawBackend(
            url=f"ws://127.0.0.1:{port}/",
            token="gw-token",
            caps=("tool-events",),
            on_event=sink,
        )
        await backend.start()
        try:
            await _wait_async(lambda: backend.is_available() and backend._connected.is_set())
            key = await backend.new_conversation()
            sent = await backend.send(key, message)
            await _wait_async(lambda: sink.of(Message))
            await asyncio.sleep(0.15)  # let the frames after the first message land
            return key, sent
        finally:
            await backend.stop()

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
def test_status_reports_the_backend_and_its_connection(app_client) -> None:
    client, _db, _gw = app_client
    _wait_for(lambda: _connected(client))
    body = client.get("/api/agent/status", headers=_headers()).json()
    assert body["backend"] == "openclaw"
    assert body["connected"] is True
    # The agent's workspace is the agent's business, so this layer must not claim to know it.
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
    db.close()


def test_the_client_waits_for_the_challenge_before_connecting(tmp_path, monkeypatch) -> None:
    """The Gateway may send `connect.challenge` and expect the client to answer *that*.

    The grace period is widened to 30 s and the challenge delayed by 1 s, so connecting at
    all inside 15 s proves the client reacted to the challenge rather than timing out its
    way to the connect request. Without the patch the test would pass either way.
    """
    from app.agent.backends import openclaw as oc

    monkeypatch.setattr(oc, "CHALLENGE_GRACE", 30.0)

    stub = StubGateway()
    stub.challenge_delay = 1.0
    stub.start()
    try:
        settings = _settings(tmp_path, port=stub.port)
        db = Database(settings.db_path)
        app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
        with TestClient(app) as client:
            _wait_for(lambda: _connected(client), timeout=15.0)
        db.close()
    finally:
        stub.stop()

    assert stub.challenge_to_connect is not None, "the client never sent connect"
    assert stub.challenge_to_connect < 5.0, "the client waited out the grace period"


def test_connect_identifies_as_a_loopback_backend_client(app_client, gateway) -> None:
    """`client.id: gateway-client` + `mode: backend` is the documented loopback path that
    skips device pairing. Getting these wrong sends a user to approve a device that never
    needed approving."""
    client, _db, _gw = app_client
    _wait_for(lambda: _connected(client))
    connect = gateway._frames("connect")[0]
    params = connect["params"]
    assert params["client"]["id"] == "gateway-client"
    assert params["client"]["mode"] == "backend"
    assert params["minProtocol"] == params["maxProtocol"] == 4
    assert "operator.write" in params["scopes"]
    # Without `tool-events` the Gateway sends no tool events at all, and does not say so.
    assert "tool-events" in params["caps"]


def test_a_pairing_refusal_names_the_command_that_fixes_it(tmp_path: Path) -> None:
    """A pairing requirement is a human step on the Gateway host. If the message does not
    name `openclaw devices approve`, the page just says "not reachable" and the user
    restarts the Gateway thirty times."""
    stub = StubGateway()
    stub.pairing_required = True
    stub.start()
    try:
        settings = _settings(tmp_path, port=stub.port, pairing=True)
        db = Database(settings.db_path)
        app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
        with TestClient(app) as client:
            thread_id = _new_thread(client)
            response = _say(client, thread_id, "hello")
            assert response.status_code == 503, response.text
            detail = response.json()["detail"]
            assert "openclaw devices approve req-42" in detail, detail
        db.close()
    finally:
        stub.stop()


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #
def test_a_thread_is_only_opened_on_the_agent_when_it_is_used(app_client) -> None:
    """Creating a thread is a local record. Opening a session on the Gateway is a change
    there, and a thread someone made by accident should leave no trace of it."""
    client, db, gateway = app_client
    _wait_for(lambda: _connected(client))

    thread_id = _new_thread(client)
    assert store.get_thread(db, thread_id).conversation_id is None
    assert not gateway._frames("sessions.create")

    _say(client, thread_id, "hello")
    _wait_for(lambda: store.get_thread(db, thread_id).conversation_id is not None)
    assert gateway._frames("sessions.create")


def test_the_conversation_id_is_the_key_the_gateway_stored_not_the_one_we_proposed(
    app_client,
) -> None:
    """The Gateway namespaces every key under its agent: propose `csd-…`, store
    `agent:main:csd-…`, and answer every `chat` and `agent` event under *that*.

    Keeping the proposed spelling is a silent failure of the worst kind — the socket is up,
    nothing errors, and `supervisor._on_event` drops every frame because it has no thread
    for a conversation id it has never seen. Verified against a real Gateway.
    """
    client, db, gateway = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")

    conversation_id = _wait_for(lambda: store.get_thread(db, thread_id).conversation_id)
    assert conversation_id.startswith(gateway.key_prefix), (
        f"stored the proposed key {conversation_id!r} instead of the Gateway's"
    )
    assert conversation_id == gateway._normalize(
        gateway._frames("chat.send")[0]["params"]["sessionKey"]
    )


def test_session_creation_does_not_send_an_idempotency_key(app_client) -> None:
    """`sessions.create` rejects one outright on a loopback backend client: there is no
    authenticated principal to make a replay meaningful, so the Gateway answers
    `INVALID_REQUEST` and the conversation can never be opened.

    `chat.send` is the opposite and does require one, which the test below pins. The two
    methods genuinely differ, so the same instinct must not be applied to both.
    """
    client, _db, gateway = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")
    _wait_for(lambda: gateway._frames("sessions.create"))

    create = gateway._frames("sessions.create")[0]
    assert "idempotencyKey" not in create["params"], (
        "sessions.create carries an idempotencyKey; the real Gateway refuses that call"
    )
    assert create["params"]["key"], "the client stopped proposing a key of its own"


# --------------------------------------------------------------------------- #
# the streaming contract
# --------------------------------------------------------------------------- #
def test_the_reply_is_assembled_from_chat_deltas(gateway) -> None:
    """OpenClaw streams the assistant's prose as `chat` deltas and then a `final`. A client
    that waited for a `message` frame — the nanobot habit — would record every reply empty."""
    sink = Collected()
    _drive_turn(gateway.port, sink)

    said = [m for m in sink.of(Message) if m.role == AgentRole.assistant]
    assert [m.content for m in said] == [ASSEMBLED]
    assert any(d.replace for d in sink.of(Delta)), "the scripted turn carries a restart"


def test_a_replace_delta_restarts_the_text_rather_than_appending(gateway) -> None:
    """Same scripted turn with the restart turned off, so the difference between this test
    and the one above *is* the contract. The kept "Let me think. " is the discarded draft a
    client that appended unconditionally would leave on screen."""
    gateway.replace_delta = False
    sink = Collected()
    _drive_turn(gateway.port, sink)

    said = [m for m in sink.of(Message) if m.role == AgentRole.assistant]
    assert [m.content for m in said] == ["Let me think. Hello there, friend."]


def test_a_tool_event_becomes_a_tool_row(gateway) -> None:
    """`agent` events with `stream: "tool"` are the only place tool activity exists — which
    is the same reason this client is not built on OpenClaw's HTTP API.

    Verified against a real Gateway by running one: `stream: "tool"` carries `name`,
    `toolCallId`, `args` on the start, and a `result` whose text lives in `content[].text`.
    """
    sink = Collected()
    _drive_turn(gateway.port, sink)

    tools = [m for m in sink.of(Message) if m.role == AgentRole.tool]
    assert tools, "no tool row was emitted"
    assert tools[0].tool_name == "edit"
    assert "src/thing.py" in tools[0].content
    # The readable half, not a Python repr of the block structure it arrived in.
    assert "{'type'" not in tools[0].content
    assert any(c.phase == "end" and c.name == "edit" for c in sink.of(ToolCall))


def test_a_tool_update_is_not_a_second_start(gateway) -> None:
    """`phase: "update"` is neither a start nor an end.

    It carries `partialResult` rather than `result`, so a client reading "has a result" as
    "finished" falls through to its start branch and the page shows one tool call twice.
    Exactly one start and one end is the contract.
    """
    sink = Collected()
    _drive_turn(gateway.port, sink)

    starts = [c for c in sink.of(ToolCall) if c.phase == "start"]
    ends = [c for c in sink.of(ToolCall) if c.phase == "end"]
    assert len(starts) == 1, f"the tool was started {len(starts)} times"
    assert len(ends) == 1, f"the tool was ended {len(ends)} times"


def test_a_failed_tool_is_reported_as_failed(gateway) -> None:
    """The outcome is in `isError`, not in the phase — a failed tool still reports
    `phase: "result"`. Reading the phase alone reports every failure as a success, which is
    the same category of error as a dashboard inventing a session status."""
    gateway.tool_is_error = True
    sink = Collected()
    _drive_turn(gateway.port, sink)

    tools = [m for m in sink.of(Message) if m.role == AgentRole.tool]
    assert tools, "no tool row was emitted"
    assert tools[0].is_error is True, "a failed tool was reported as a success"
    assert any(c.phase == "end" and c.is_error for c in sink.of(ToolCall))


def test_chat_send_carries_an_idempotency_key(app_client) -> None:
    """`idempotencyKey` is required on this method. Sending without one is a protocol
    violation, and reusing one across a retry is how a doubled message is avoided."""
    client, db, gateway = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)

    _say(client, thread_id, "hello")
    _wait_for(lambda: gateway._frames("chat.send"))
    send = gateway._frames("chat.send")[0]
    assert send["params"]["idempotencyKey"]
    assert send["params"]["sessionKey"]
    assert send["params"]["message"] == "hello"


def test_send_says_nothing_because_the_gateway_names_no_id(gateway) -> None:
    """`chat.send`'s ack names the run and nothing else — verified against a real Gateway,
    whose answer is `{"runId": …, "status": "started"}` with no row id of any kind.

    So the honest return is `None`: the page reconciles on the next history read. Inventing
    an id here would guarantee the duplicate the interface exists to prevent, and reading a
    bare `id` out of an ack that also carries a `runId` would be reading the wrong one.
    """
    sink = Collected()
    _key, sent = _drive_turn(gateway.port, sink)

    assert sent is None, f"a message id was invented: {sent.id!r}"


# --------------------------------------------------------------------------- #
# reconnection
# --------------------------------------------------------------------------- #
def test_the_subscription_is_reestablished_after_a_reconnect(app_client) -> None:
    """A subscription is connection-scoped. When it is lost and not restored, replies stop
    arriving with no error anywhere — the failure this test exists to prevent."""
    client, db, gateway = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")
    _wait_for(lambda: gateway._frames("sessions.messages.subscribe"))

    before = len(gateway._frames("sessions.messages.subscribe"))
    gateway.force_disconnect()
    _wait_for(
        lambda: len(gateway._frames("sessions.messages.subscribe")) > before,
        timeout=30.0,  # the client backs off 1 s before its first reconnect
    )


# --------------------------------------------------------------------------- #
# history — read from the Gateway, never kept here
# --------------------------------------------------------------------------- #
def test_history_is_read_from_the_gateway(app_client) -> None:
    """The conversation comes from `chat.history`, oldest first, carrying the Gateway's own
    row ids. Nothing is stored on this side: the transcript lives with the agent."""
    client, _db, _gw = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")

    messages = _wait_for(lambda: _thread_messages(client, thread_id))
    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
    assert messages[0]["content"] == "hi"
    assert messages[1]["content"] == ASSEMBLED
    # The id the page dedups on across a refresh has to be the agent's, not ours.
    assert messages[1]["id"] == "entry-2"
    assert messages[2]["tool_name"] == "edit"
    # The time each message was sent, from the row — not the moment the page read it.
    # OpenClaw stamps epoch *milliseconds*, and reading those as seconds lands in the
    # year 58742, so the value is checked rather than just its presence.
    assert messages[0]["created_at"] == "2026-10-18T16:42:42.000Z", messages[0]["created_at"]
    assert messages[1]["created_at"] == "2026-10-18T16:42:43.000Z", messages[1]["created_at"]


def test_a_transcript_reset_reads_as_empty(app_client) -> None:
    """`kind: "reset"` means the Gateway cleared the transcript. Showing the old rows after
    that is as wrong as showing nothing before it."""
    client, _db, gateway = app_client
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")
    _wait_for(lambda: _thread_messages(client, thread_id))

    gateway.history_reset = True
    _wait_for(lambda: _thread_messages(client, thread_id) == [])


def test_history_paging_comes_back_oldest_first(app_client) -> None:
    """The Gateway pages backwards. Concatenating pages in arrival order would render the
    conversation upside down, which is a bug that looks like a rendering glitch."""
    client, _db, gateway = app_client
    gateway.history_page_cap = 1  # two rows, so the second page must be asked for
    _wait_for(lambda: _connected(client))
    thread_id = _new_thread(client)
    _say(client, thread_id, "hello")

    messages = _wait_for(lambda: _thread_messages(client, thread_id))
    assert [m["content"] for m in messages][:2] == ["hi", ASSEMBLED]
    assert any(f["params"].get("offset") == 1 for f in gateway._frames("chat.history")), \
        "the client never walked past the first page"


def test_history_without_a_connection_is_not_an_empty_conversation(gateway) -> None:
    """An empty list would assert "this conversation is empty" — a different fact from "I
    could not ask", and the page cannot tell them apart once it is a list."""
    backend = OpenClawBackend(
        url=f"ws://127.0.0.1:{gateway.port}/",
        token=None,
        caps=("tool-events",),
        on_event=Collected(),
    )
    with pytest.raises(BackendUnavailable):
        asyncio.run(backend.history("csd-never-opened"))
