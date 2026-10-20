"""The OpenClaw backend: this service as an operator client of an OpenClaw Gateway.

OpenClaw runs the agent. This service connects to its Gateway over the WebSocket protocol
and renders what comes back — the same relationship the nanobot adapter has, over a very
different wire.

Everything below was read out of OpenClaw's published contract rather than guessed:

- `oc_schema.json` — the machine-readable protocol schema shipped in
  `@openclaw/gateway-protocol` (`protocol.schema.json`), which is the authoritative source
  for frame shapes and enums. Every field name quoted in this module is from there.
- `docs/gateway/clients.md` — "Build a third-party operator or WebChat client", the guide
  for exactly this case.

Five things in here would each be a silent bug if assumed differently:

1. **`connect` is a request, and it may wait on a challenge.** The Gateway can send a
   `connect.challenge` event before the client may connect; the reference client refuses to
   connect without one ("gateway connect challenge missing nonce"). We wait for a challenge
   or a short grace period, then connect either way, because gateways from before the
   challenge existed are still in the wild and the official guide says so.

2. **A trusted local backend client may omit device identity.** `client.id` has an enum
   value `gateway-client` and `client.mode` has `backend`; on a direct loopback connection
   authenticating with the Gateway's shared token, that pairing skips Ed25519 device signing
   and the whole pairing ceremony. Getting this wrong sends a user to `openclaw devices
   approve` for a connection that never needed it.

3. **`tool-events` is a capability, not an authorization.** The Gateway registers only
   connections that advertise it as recipients of a run's structured tool events, and
   *does not error* if you forget — you simply never see a tool row. So it is pinned on in
   `config.py`, where the omission would otherwise be invisible.

4. **`chat.send` requires an `idempotencyKey`.** OpenClaw forces idempotency on side-effect
   methods. nanobot has nothing like it; sending without one is a protocol violation, and
   sending the same one twice is how a retry avoids a doubled message.

5. **`replace` is real here.** A `chat` delta can carry `replace: true` and use `deltaText`
   as the *entire* text so far, which is why `base.Delta` grew a `replace` field before this
   adapter was written. A client that appended unconditionally would render a restarted
   answer twice — the exact failure that field exists to prevent.

**History is read, not kept.** `chat.history` returns the conversation as the Gateway has it,
with a stable `__openclaw.id` per row and cursor/offset paging — and OpenClaw's own guide
names it as the recovery path for a reconnecting third-party client. So this adapter
implements `history()` and stores nothing: the conversation lives in one place, and it is the
agent's.

That reverses an earlier decision in this file, and the reversal is worth recording. The
first version deliberately ignored `chat.history` to keep this page a *mirror* — "what this
channel has seen" — so that two backends whose pages look identical would not mean different
things. That reasoning depended on the core keeping one shared message table for every
backend, which it no longer does: the core keeps no transcript at all, our own Pi is read
directly, and each foreign agent supplies history through its own protocol if it has one.
A mirror is now the *fallback* for an agent that cannot replay, not a rule this one follows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ...schemas import AgentRole
from .base import (
    AgentBackend,
    BackendStatus,
    BackendUnavailable,
    Delta,
    EventSink,
    Message,
    Notice,
    Status,
    ToolCall,
)

logger = logging.getLogger("csd.agent.openclaw")

#: Wire version this client speaks. The guide: general operator and WebChat clients
#: "must negotiate the exact current version with minProtocol: 4 and maxProtocol: 4";
#: only authenticated node clients get the N-1 window. Pinned rather than negotiated
#: because a third-party client has no way to test an older wire it has never seen.
PROTOCOL_VERSION = 4

#: The identity that unlocks the loopback path (see the module docstring, point 2). Both
#: values are enum members of `ConnectParams.client.id` / `.mode`.
CLIENT_ID = "gateway-client"
CLIENT_MODE = "backend"

#: What we tell the Gateway we are. Only `minLength: 1` is required by the schema; a
#: version that tracks this service's own would be a lie, so it names the client instead.
CLIENT_VERSION = "csd-agenbot-1"

#: Scopes this client actually uses: read for history/roster and events, write for
#: `chat.send` and `chat.abort`. Asking for `operator.admin` to render a chat transcript
#: would be the same category of mistake as requiring a write token to read the dashboard.
SCOPES = ("operator.read", "operator.write")

#: How long to wait for a `connect.challenge` before connecting anyway. Short: a Gateway
#: that sends no challenge sends `hello-ok` as soon as it sees the connect request, and
#: waiting longer only delays the first paint.
CHALLENGE_GRACE = 3.0

#: Reconnect backoff ceiling, as in the nanobot adapter. A Gateway restart should be
#: invisible rather than a dead page.
MAX_BACKOFF = 30.0

#: Bound on any single RPC. `chat.send` returns as soon as the run is *accepted*, so this
#: bounds acceptance, not the whole turn.
REQUEST_TIMEOUT = 30.0

#: How much history to ask for per `chat.history` page, and how much of it we are willing to
#: walk. The page renders a conversation, not an archive, and an unbounded walk over a long
#: session would be a slow page with no visible cause.
HISTORY_PAGE = 100
HISTORY_MAX_ROWS = 500

#: The phases a `stream: "tool"` frame's `data` can carry, as a real Gateway emits them.
#: Taken from a live 2026.10.4 run rather than from the contract, which documents the
#: envelope but not this field:
#:
#:   {"phase": "start",  "name", "toolCallId", "args"}
#:   {"phase": "update", "name", "toolCallId", "partialResult"}
#:   {"phase": "result", "name", "toolCallId", "isError", "result"}
#:
#: `update` is the middle one and the one worth naming: it carries `partialResult`, so a
#: client reading "has a result" as "finished" treats it as a start and shows the tool
#: twice. The older spellings are kept alongside for gateways of other vintages.
_TOOL_UPDATE_PHASES = ("update", "updating", "progress", "running")
_TOOL_END_PHASES = ("end", "done", "complete", "completed", "error", "failed", "result")


class OpenClawBackend(AgentBackend):
    """A WebSocket client for one OpenClaw Gateway.

    One connection carries every conversation, keyed by `sessionKey` — so, like nanobot and
    unlike native, this is a single long-lived object rather than one per thread.
    """

    name = "openclaw"

    def __init__(
        self,
        *,
        url: str,
        token: str | None,
        caps: tuple[str, ...],
        on_event: EventSink,
        connect_timeout: float = 10.0,
    ) -> None:
        self.url = url
        self.token = token
        self.caps = tuple(caps)
        self._on_event = on_event
        self._connect_timeout = connect_timeout

        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._connected = asyncio.Event()
        self._hello: dict[str, Any] | None = None

        #: RPC bookkeeping: `id` -> future awaiting that `res`.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        #: Set once per connection so the handshake is answered exactly once.
        self._challenge: dict[str, Any] | None = None
        #: Flipped by a `connect.challenge`; initialised here so `_handle` is safe to call
        #: before a connection exists (tests do exactly that).
        self._challenge_event: asyncio.Event | None = None

        #: Conversations this process cares about, re-subscribed after any reconnect
        #: because a subscription is connection-scoped.
        self._sessions: set[str] = set()
        #: Text assembled from `chat` deltas, per session.
        self._buffers: dict[str, list[str]] = defaultdict(list)
        #: Sessions whose reply is still arriving.
        self._streaming: set[str] = set()
        #: Newest run id per session, for `chat.abort`.
        self._runs: dict[str, str] = {}
        #: Highest `seq` accepted per run. The guide: "Maintain the highest accepted
        #: sequence independently for each run, ignore an already-seen or lower sequence".
        self._seq: dict[str, int] = {}
        #: Last run phase rendered, so a phase is announced once rather than per frame.
        self._phase: dict[str, str] = {}
        self._ok_seen = 0
        #: Why the last connect attempt failed, kept so the *reason* can be shown instead of
        #: a generic "is it running?". A pairing requirement and a stopped Gateway look
        #: identical from outside and need opposite actions from the person reading it.
        self._last_error: str | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task is not None:
            return
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="openclaw-backend")

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._close_socket()
        self._connected.clear()

    async def _close_socket(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # connection
    # ------------------------------------------------------------------ #
    def _connect_params(self) -> dict[str, Any]:
        """The `connect` request body, shaped by `ConnectParams`.

        `caps` is what we are willing to *receive* (`tool-events`), not what we may do —
        the schema says so in as many words, and the Gateway silently withholds tool events
        from a client that does not advertise it.
        """
        params: dict[str, Any] = {
            "minProtocol": PROTOCOL_VERSION,
            "maxProtocol": PROTOCOL_VERSION,
            "client": {
                "id": CLIENT_ID,
                "version": CLIENT_VERSION,
                "platform": "python",
                "mode": CLIENT_MODE,
            },
            "role": "operator",
            "scopes": list(SCOPES),
            "caps": list(self.caps),
        }
        if self.token:
            params["auth"] = {"token": self.token}
        return params

    async def _run(self) -> None:
        import websockets

        backoff = 1.0
        while not self._closing:
            try:
                async with websockets.connect(
                    self.url, open_timeout=self._connect_timeout
                ) as ws:
                    self._ws = ws
                    self._challenge = None
                    self._challenge_event = asyncio.Event()
                    reader = asyncio.create_task(self._read_loop(ws), name="openclaw-reader")
                    try:
                        await self._handshake()
                        # The reader *is* the connection's lifetime: it runs until the socket
                        # closes. Cancelling it here instead would close the socket the moment
                        # the handshake finished, and every conversation would ride on a
                        # connection that flaps once a second.
                        await reader
                    finally:
                        reader.cancel()
                        try:
                            await reader
                        except (asyncio.CancelledError, Exception):
                            pass
                    backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Remember *why*: a pairing requirement, a bad token and a stopped Gateway
                # all surface here as "not connected", and they need different fixes.
                self._last_error = str(exc)
                if not self._closing:
                    logger.warning("openclaw connection lost: %s", exc)
            finally:
                self._ws = None
                self._connected.clear()
                self._hello = None
                self._fail_pending("disconnected")
                self._streaming.clear()
                self._buffers.clear()

            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                self._handle(json.loads(raw))
            except Exception:
                logger.exception("bad openclaw frame: %.300s", raw)

    async def _handshake(self) -> None:
        """Wait for a challenge (or not), send `connect`, require `hello-ok`.

        The challenge is where a device-signed client gets its `nonce`; a loopback backend
        client does not sign anything, but the reference client still refuses to connect
        before a challenge arrives, so we wait — briefly.
        """
        try:
            await asyncio.wait_for(self._challenge_event.wait(), timeout=CHALLENGE_GRACE)
        except asyncio.TimeoutError:
            logger.debug("no connect.challenge within %.1fs; connecting anyway", CHALLENGE_GRACE)

        payload = await self._request("connect", self._connect_params())
        kind = payload.get("type")
        if kind != "hello-ok":
            raise BackendUnavailable(f"openclaw answered connect with {kind!r}, not hello-ok")

        self._hello = payload
        auth = payload.get("auth") or {}
        self._ok_seen += 1
        self._connected.set()
        self._last_error = None
        logger.info(
            "connected to openclaw at %s (protocol=%s role=%s scopes=%s)",
            self.url, payload.get("protocol"), auth.get("role"), auth.get("scopes"),
        )
        await self._resubscribe()

    async def _resubscribe(self) -> None:
        """Re-attach every conversation after a (re)connect. Connection-scoped, like nanobot."""
        for key in list(self._sessions):
            try:
                await self._request("sessions.messages.subscribe", {"key": key})
            except Exception:
                logger.warning("could not re-subscribe to %s after reconnect", key)

    async def _request(
        self, method: str, params: dict[str, Any], timeout: float = REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        """One RPC. The `id` is ours; the Gateway echoes it on the `res`."""
        ws = self._ws
        if ws is None:
            raise BackendUnavailable(f"not connected to openclaw at {self.url}")

        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await ws.send(json.dumps({
                "type": "req", "id": request_id, "method": method, "params": params,
            }))
            reply = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise BackendUnavailable(
                f"openclaw did not answer {method} in {timeout:.0f}s"
            ) from None
        finally:
            self._pending.pop(request_id, None)

        if not reply.get("ok", False):
            raise BackendUnavailable(_describe_error(method, reply.get("error") or {}))
        return reply.get("payload") or {}

    def _fail_pending(self, reason: str) -> None:
        while self._pending:
            _, future = self._pending.popitem()
            if not future.done():
                future.set_exception(BackendUnavailable(f"openclaw {reason}"))

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    async def new_conversation(self) -> str:
        """Create a session, subscribe to it, and return **the key the Gateway stored**.

        Two things here are not what the shape of the protocol suggests, and both were
        settled by running one:

        - **There is no `idempotencyKey` on this call.** OpenClaw forces idempotency on
          side-effect methods and `chat.send` genuinely does require one, but a loopback
          `gateway-client` has no authenticated principal or device identity behind it, and
          `sessions.create` answers an idempotent call with `INVALID_REQUEST`
          ("idempotent session creation requires an authenticated principal or device
          identity"). Sending one does not make a retry safer; it makes the conversation
          impossible to open at all.
        - **The key that comes back is the Gateway's, not ours.** It namespaces every key
          under its agent — propose `csd-abc`, get `agent:main:csd-abc` — and every `chat`
          and `agent` event afterwards carries the namespaced spelling. Returning the key we
          proposed would put events on a conversation id the supervisor has never seen,
          which drops them silently and looks exactly like a working connection.
        """
        await self._require_connected()
        proposed = "csd-" + uuid.uuid4().hex[:12]
        payload = await self._request("sessions.create", {"key": proposed})
        # The Gateway may normalise the key, or name one itself when we pass none.
        stored = _first_str(payload, "key") or proposed
        self._sessions.add(stored)
        reported = await self._subscribe(stored)
        # `sessions.messages.subscribe` is create-or-adopt and reports the stored key too,
        # so it is a second opinion on the same fact — and the only one available on a
        # bucket where `sessions.create` was refused.
        self._remember(reported)
        return reported or stored

    async def attach(self, conversation_id: str) -> None:
        """Subscribe to an existing session so its messages reach us again."""
        await self._require_connected()
        self._sessions.add(conversation_id)
        self._remember(await self._subscribe(conversation_id))

    def _remember(self, key: Any) -> None:
        """Also answer to the spelling the Gateway reports.

        Both spellings are kept rather than swapped: `is_live()` is asked with the id that
        came out of the database, and a thread stored before this was understood would
        otherwise read as dead while its conversation is very much alive.
        """
        if isinstance(key, str) and key:
            self._sessions.add(key)

    async def _subscribe(self, key: str) -> str | None:
        """Subscribe, and hand back the key the Gateway says it stored, if it names one."""
        try:
            payload = await self._request("sessions.messages.subscribe", {"key": key})
        except Exception as exc:
            # Not fatal: the session is registered and re-subscribed on the next connect.
            # Failing the page load over a slow ack would be worse than the ack.
            logger.warning("could not subscribe to %s: %s", key, exc)
            return None
        return _first_str(payload, "key")

    async def history(self, conversation_id: str) -> list[Message]:
        """The conversation as the Gateway has it. Nothing is kept on this side.

        `chat.history` is the recovery path OpenClaw's own guide points a reconnecting
        third-party client at, so this is a read rather than a copy: the transcript lives in
        one place and it is the agent's (`base.AgentBackend.history`).

        Three details of that call are honoured because each would otherwise make the
        transcript quietly untrue:

        - **`kind: "reset"` means cleared, not empty.** A transcript that was reset and one
          that never existed look the same in an empty list, and only one of them should
          erase what the page is showing.
        - **Paging goes backwards.** The first page is the newest rows and `hasMore` /
          `nextOffset` walk into the past, so the pages are reversed before being returned
          oldest-first as the interface requires.
        - **`__openclaw.id` is the row's identity**, and it is what the page dedups on. A
          re-fetched conversation must recognise the messages it is already showing.
        """
        if not self._connected.is_set():
            # An empty list would assert "this conversation is empty", which is a different
            # fact from "I could not ask". The route turns this into a 503 instead.
            raise BackendUnavailable(self._last_error or f"not connected to OpenClaw at {self.url}")

        collected: list[dict] = []
        offset = 0
        while True:
            payload = await self._request(
                "chat.history",
                {"sessionKey": conversation_id, "limit": HISTORY_PAGE, "offset": offset},
            )
            if payload.get("kind") == "reset":
                return []
            page = payload.get("messages")
            page = page if isinstance(page, list) else []
            collected.extend(row for row in page if isinstance(row, dict))
            if not payload.get("hasMore") or not page or len(collected) >= HISTORY_MAX_ROWS:
                break
            next_offset = payload.get("nextOffset")
            offset = next_offset if isinstance(next_offset, int) else offset + len(page)

        messages: list[Message] = []
        for row in reversed(collected):  # the pages arrived newest-first
            messages.extend(_row_to_messages(row))
        return messages

    async def send(self, conversation_id: str, text: str) -> Message | None:
        """Deliver a user message, and hand back the row id the Gateway gave it when there is one.

        The id matters more than it looks. The page draws this message the moment the call
        returns, and a later refresh brings the *same* message back from `history()` — only
        the agent's own id lets those two be recognised as one, which is why a locally
        invented id would guarantee the duplicate it is meant to prevent.

        `chat.send`'s result fields are not pinned by the protocol schema, so this reads the
        names a row id would plausibly use and otherwise says nothing. A bare `id` is
        deliberately **not** accepted: a reply carrying both a run id and an id leaves which
        is which ambiguous, and a wrong id is worse than no id.
        """
        await self._require_connected()
        self._sessions.add(conversation_id)
        self._buffers.pop(conversation_id, None)
        payload = await self._request(
            "chat.send",
            {
                "sessionKey": conversation_id,
                "message": text,
                "idempotencyKey": uuid.uuid4().hex,
            },
        )
        entry = payload.get("entry") if isinstance(payload.get("entry"), dict) else {}
        echoed = _first_str(payload, "messageId", "message_id") or _first_str(entry, "id")
        if echoed:
            return Message(role=AgentRole.user, content=text, id=echoed)
        return None

    async def abort(self, conversation_id: str) -> bool:
        """Stop the current run, if there is one we know the id of.

        `runId` is optional in `chat.abort` — omitting it stops the session's current run,
        which is what the page's stop button means. We pass it when we have seen one, so a
        run that has since been replaced is not stopped by mistake.
        """
        if not self._connected.is_set() or conversation_id not in self._sessions:
            return False
        if conversation_id not in self._streaming:
            return False
        params: dict[str, Any] = {"sessionKey": conversation_id}
        run_id = self._runs.get(conversation_id)
        if run_id:
            params["runId"] = run_id
        try:
            await self._request("chat.abort", params)
        except Exception as exc:
            logger.warning("openclaw refused chat.abort: %s", exc)
            return False
        return True

    async def _require_connected(self) -> None:
        if not self._connected.is_set():
            # Prefer the last attempt's own reason. "Is the Gateway running?" is the wrong
            # advice for a device that is up and waiting to be approved, and the user has no
            # way to tell those apart from the outside.
            raise BackendUnavailable(
                self._last_error
                or f"not connected to OpenClaw at {self.url}. Is the Gateway running?"
            )

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #
    def is_live(self, conversation_id: str) -> bool:
        return self._connected.is_set() and conversation_id in self._sessions

    def is_streaming(self, conversation_id: str) -> bool:
        return conversation_id in self._streaming

    def is_available(self) -> bool:
        """A gateway backend is always *usable* — it may just not be up yet.

        Same reasoning as nanobot: there is nothing to install or configure on this side.
        Whether the Gateway is reachable is `status().connected`, and conflating the two
        would send someone to install a client for a server that is simply stopped.
        """
        return True

    def status(self) -> BackendStatus:
        # The settings row is the one place a person looks when the page is dead, so the
        # reason travels with the URL rather than only into the log.
        detail = self.url
        if not self._connected.is_set() and self._last_error:
            detail = f"{self.url} — {self._last_error}"
        return BackendStatus(
            name=self.name,
            connected=self._connected.is_set(),
            detail=detail,
            conversations=len(self._sessions),
        )

    # ------------------------------------------------------------------ #
    # frames
    # ------------------------------------------------------------------ #
    def _emit(self, conversation_id: str, event: Any) -> None:
        """Hand an event up without letting a slow consumer stall the socket reader."""
        try:
            result = self._on_event(conversation_id, event)
        except Exception:
            logger.exception("event sink failed")
            return
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)  # noqa: RUF006 - deliberately not awaited

    def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")

        if kind == "res":
            future = self._pending.pop(str(frame.get("id")), None)
            if future is not None and not future.done():
                future.set_result(frame)
            else:
                # A reply to a request we already timed out on. Nothing to do, but worth
                # saying: a growing pile of these means the timeout is too tight.
                logger.debug("openclaw reply with no waiter: %s", frame.get("id"))
            return

        if kind != "event":
            # Including `req`: server-initiated calls (approvals, exec) go to clients that
            # advertise the matching capability, which this one does not.
            logger.debug("ignoring openclaw frame type=%r", kind)
            return

        name = frame.get("event")
        payload = frame.get("payload") or {}

        if name == "connect.challenge":
            self._challenge = payload
            if self._challenge_event is not None:
                self._challenge_event.set()
            return

        if name == "hello-ok":
            # Not every Gateway answers `connect` with the payload as its `res`; accept a
            # broadcast hello-ok too, and treat it as the connection being usable.
            self._hello = payload
            self._connected.set()
            return

        if name == "chat":
            chat_key = payload.get("sessionKey")
            if isinstance(chat_key, str):
                self._on_chat(chat_key, payload)
            else:
                logger.debug("ignoring a chat event without a sessionKey")
            return

        if name == "agent":
            # `AgentEvent` carries `sessionKey` (and `sessionId`) alongside the `runId`, so
            # the conversation is named outright and the run id is only a fallback. An
            # earlier version of this file asserted the opposite — that these frames are
            # keyed by run alone — and resolving by run is wrong as soon as a second
            # conversation exists.
            key = payload.get("sessionKey")
            if not isinstance(key, str) or not key:
                key = self._session_for_run(payload.get("runId"))
            if key is None:
                logger.debug("agent event for an unknown run %r", payload.get("runId"))
                return
            self._on_agent(key, payload)
            return

        logger.debug("ignoring openclaw event %r", name)

    def _session_for_run(self, run_id: Any) -> str | None:
        """Which conversation a run belongs to.

        With one conversation open there is nothing to disambiguate, and attributing a tool
        event is better than dropping it; with several, an unknown run is left alone rather
        than attached to a conversation it may not belong to.
        """
        if isinstance(run_id, str):
            for session, known in self._runs.items():
                if known == run_id:
                    return session
        if len(self._sessions) == 1:
            return next(iter(self._sessions))
        return None

    def _on_chat(self, session: str, payload: dict[str, Any]) -> None:
        """One frame of the assistant's own stream: status / delta / final / aborted / error."""
        run_id = payload.get("runId")
        state = payload.get("state")
        seq = payload.get("seq")

        if isinstance(run_id, str):
            self._runs[session] = run_id
            if isinstance(seq, int):
                last = self._seq.get(run_id, -1)
                if seq <= last:
                    return  # already seen, or out of order behind a newer frame
                self._seq[run_id] = seq

        if state == "delta":
            text = payload.get("deltaText") or ""
            replace = bool(payload.get("replace"))
            if replace:
                # `deltaText` is the whole text now, not an addition to it. Appending here
                # is the bug `Delta.replace` exists to make impossible.
                self._buffers[session] = [text]
            else:
                self._buffers[session].append(text)
            self._streaming.add(session)
            self._emit(session, Delta(channel="text", text=text, replace=replace))
            return

        if state == "status":
            phase = payload.get("phase")
            if isinstance(phase, str) and self._phase.get(session) != phase:
                self._phase[session] = phase
                self._emit(session, Notice(notice=phase.replace("_", " ")))
            return

        if state == "final":
            self._flush(session)
            self._streaming.discard(session)
            self._phase.pop(session, None)
            self._emit(session, Status(streaming=False))
            return

        if state in ("aborted", "error"):
            # Whatever arrived before the stop is what the agent said; keep it and label it.
            self._flush(session, is_error=state == "error")
            self._streaming.discard(session)
            self._phase.pop(session, None)
            if state == "error":
                self._emit(session, Notice(
                    notice="openclaw run failed",
                    level="error",
                    detail=str(payload.get("errorMessage") or payload.get("stopReason") or "")[:500],
                ))
            else:
                self._emit(session, Notice(notice="stopped"))
            self._emit(session, Status(streaming=False))
            return

        logger.debug("ignoring openclaw chat state %r", state)

    def _flush(self, session: str, *, is_error: bool = False) -> None:
        """Write the buffered text down as one assistant message, if there is any."""
        text = "".join(self._buffers.pop(session, []))
        if text.strip():
            self._emit(session, Message(role=AgentRole.assistant, content=text, is_error=is_error))

    def _on_agent(self, session: str, payload: dict[str, Any]) -> None:
        """The run's *other* stream: plan, lifecycle, tool activity.

        Two rules from the guide decide this method's shape:

        - **One text projection per display.** `chat` deltas already carry the assistant's
          prose; accumulating text from this stream too would duplicate the answer on
          screen. So this method never produces `Delta`.
        - **Tool payloads are not part of the documented contract.** `AgentEvent.data` is an
          open record (`Type.Record(Type.String(), Type.Unknown())`); only `stream: "tool"`
          has been observed. So the fields below are read by looking for the names several
          plausible shapes would use, and a frame we cannot read degrades to a debug line —
          never to an exception that would take the reader task down.
        """
        if payload.get("isHeartbeat"):
            return
        if payload.get("stream") != "tool":
            logger.debug("ignoring openclaw agent stream %r", payload.get("stream"))
            return

        data = payload.get("data")
        if not isinstance(data, dict):
            logger.debug("openclaw tool event with no data: %.200s", payload)
            return

        try:
            name = _first_str(data, "name", "toolName", "tool", "title") or "tool"
            call_id = _first_str(data, "toolCallId", "callId", "id", "requestId")
            phase = (_first_str(data, "phase", "status", "state", "kind") or "").lower()
            args = data.get("args") or data.get("input") or data.get("arguments")
            result = data.get("result") or data.get("output") or data.get("error") or data.get("text")

            # A real Gateway emits three phases per tool, and the middle one is the trap:
            # `update` carries `partialResult`, not `result`, so a client that reads "has a
            # result" as "finished" falls through to its start branch and puts the same tool
            # on screen twice. There is nothing new to say yet, so nothing is said.
            if phase in _TOOL_UPDATE_PHASES:
                return

            finished = phase in _TOOL_END_PHASES or result is not None
            # The outcome is `isError` — a failed tool still reports `phase: "result"`, so
            # reading the phase alone reports every failure as a success.
            failed = bool(data.get("isError")) or phase in ("error", "failed")

            if finished:
                text = _tool_result_text(result)
                if text:
                    self._emit(session, Message(
                        role=AgentRole.tool,
                        tool_name=name,
                        tool_call_id=call_id,
                        content=text,
                        is_error=failed,
                    ))
                self._emit(session, ToolCall(
                    phase="end", name=name, call_id=call_id, is_error=failed,
                ))
            else:
                self._emit(session, ToolCall(phase="start", name=name, call_id=call_id, args=args))
        except Exception:
            # A tool row is not worth losing a reply over. See the docstring.
            logger.exception("unreadable openclaw tool event: %.300s", payload)


def _first_str(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _tool_result_text(result: Any) -> str:
    """The readable half of a tool's `result`.

    A real `result` is a block structure — `{"content": [{"type": "text", "text": …}],
    "details": {…}}` — so `str(result)` puts a Python repr of that on screen, quotes and
    braces and all. The prose is in the blocks; `details.aggregated` is the same text for
    the shell tools that fill it in, and is the fallback when the blocks are absent.
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result[:4000]
    if isinstance(result, dict):
        blocks = result.get("content")
        if isinstance(blocks, list):
            text = "".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if text.strip():
                return text[:4000]
        details = result.get("details")
        if isinstance(details, dict):
            aggregated = details.get("aggregated")
            if isinstance(aggregated, str) and aggregated.strip():
                return aggregated[:4000]
        if not result:
            return ""
    try:
        return json.dumps(result, ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        return str(result)[:4000]


def _row_to_messages(row: dict[str, Any]) -> list[Message]:
    """One `chat.history` row, as the messages this page renders.

    The *row* shape is not in the documented contract — the guide describes content blocks
    and the `__openclaw` metadata envelope, not the row around them — so this reads what is
    there and drops what it cannot explain. Dropping is deliberate: a placeholder row would
    be this client inventing something the agent never said, which is the same category of
    error as a dashboard inventing a session status.

    Tool rows carry no `id`. The row's `__openclaw.id` identifies the *transcript entry*, and
    several messages can come out of one entry — giving them all the same id would make the
    page's dedup collapse them into one.
    """
    meta = row.get("__openclaw")
    meta = meta if isinstance(meta, dict) else {}
    if meta.get("kind") in ("compaction", "reset"):
        return []  # a boundary marker, not something anyone said
    entry_id = meta.get("id") if isinstance(meta.get("id"), str) else None
    created_at = _row_timestamp(row)

    role = row.get("role")
    if role == "system":
        return []  # injected context, not a turn
    agent_role = {"user": AgentRole.user, "assistant": AgentRole.assistant}.get(role)
    if agent_role is None:
        logger.debug("openclaw history row with role=%r; skipping", role)
        return []

    content = row.get("content")
    if isinstance(content, str):
        if not content.strip():
            return []
        return [Message(role=agent_role, content=content, id=entry_id, created_at=created_at)]
    if not isinstance(content, list):
        return []

    out: list[Message] = []
    text: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            args = block.get("input") if isinstance(block.get("input"), dict) else None
            out.append(Message(
                role=AgentRole.tool,
                content=json.dumps(args, ensure_ascii=False)[:1000] if args else "",
                tool_name=block.get("name"),
                tool_call_id=block.get("id"),
                args=args,
            ))
        elif kind == "tool_result":
            result = block.get("content")
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False) if result is not None else ""
            out.append(Message(
                role=AgentRole.tool,
                content=result[:4000],
                tool_call_id=block.get("tool_use_id"),
                is_error=bool(block.get("is_error")),
            ))
        else:
            logger.debug("openclaw history block type %r dropped", kind)

    if "".join(text).strip():
        out.insert(0, Message(role=agent_role, content="".join(text), id=entry_id,
                              created_at=created_at))
    return out


def _row_timestamp(row: dict[str, Any]) -> str | None:
    """A history row's own time, as the ISO 8601 string this service speaks.

    OpenClaw stamps rows with epoch **milliseconds** under `timestamp` and repeats the same
    moment in the metadata envelope. Carried through, a re-fetched transcript keeps the times
    its messages were sent; dropped, the page stamps every one of them with the moment the
    page loaded, which reads as fact and is not one.
    """
    for raw in (row.get("timestamp"), (row.get("__openclaw") or {}).get("recordTimestampMs")
                if isinstance(row.get("__openclaw"), dict) else None):
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            continue
        try:
            # Milliseconds, not seconds: a 13-digit epoch read as seconds lands in the year
            # 58742 and every message would claim to be from the far future.
            seconds = float(raw) / 1000.0 if raw > 1e11 else float(raw)
            return (
                datetime.fromtimestamp(seconds, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            continue
    return None


def _describe_error(method: str, error: dict[str, Any]) -> str:
    """Turn a structured `res.error` into something worth putting in front of a person."""
    code = error.get("code") or "error"
    message = error.get("message") or "no message"
    detail = f"openclaw {method} failed: {message} ({code})"

    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    if code == "PAIRING_REQUIRED":
        # Not a bug and not something this client can fix: pairing is approved by a person
        # on the Gateway host. Saying so is the difference between a readable failure and
        # "it just does not connect".
        request_id = details.get("requestId") or details.get("id") or "<requestId>"
        detail += (
            ". This device needs pairing: run `openclaw devices list` on the Gateway host, "
            f"then `openclaw devices approve {request_id}`"
        )
    elif error.get("retryable"):
        after = error.get("retryAfterMs")
        detail += f". The Gateway says this is retryable{'' if not after else f' after {after} ms'}"

    next_step = details.get("recommendedNextStep")
    if isinstance(next_step, str) and next_step:
        detail += f". Suggested: {next_step}"
    return detail


BACKEND_NAME = "openclaw"

#: Shown in the settings panel. The name the product uses for itself.
BACKEND_LABEL = "OpenClaw"

#: What connecting involves, in the UI's voice. Both halves matter: the loopback case
#: needs nothing, and the other case needs one command run *on the Gateway host* — which
#: is exactly the kind of thing a user otherwise discovers by staring at a dead page.
BACKEND_BLURB = (
    "OpenClaw's Gateway WebSocket. On the same machine with the shared Gateway token, "
    "nothing to do; from elsewhere you approve this device on the Gateway host "
    "(`openclaw devices approve <requestId>`)."
)


def build(settings, on_event: EventSink) -> OpenClawBackend:
    """Construct the backend from settings, for the registry to call."""
    return OpenClawBackend(
        url=settings.openclaw_ws_url,
        token=settings.openclaw_token,
        caps=settings.openclaw_caps,
        on_event=on_event,
    )


__all__ = ["OpenClawBackend", "PROTOCOL_VERSION"]
