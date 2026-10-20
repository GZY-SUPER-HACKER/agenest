"""The Hermes backend: this service as a client of the Hermes API server.

Hermes runs the agent — its memory, its skills, its tool backends, its model credentials.
This service carries messages over HTTP and renders the reply from its SSE stream.

**Which of Hermes' surfaces, and why this one.** Hermes exposes several things a client
could talk to, and the wrong ones are wrong for this page:

- The **Desktop app's canonical WebSocket** (`ws://127.0.0.1:7681/`) is gated by private
  `session-ticket` grants minted over a same-OS-user control socket, and its protocol is
  documented as the *desktop application's* inner wiring. Building a control plane on
  another product's private surface is a dependency that breaks the day that product
  reshuffles its front end — the same reasoning that kept this service off nanobot's WebUI
  route.
- The **`hermes acp` stdio server** speaks ACP over stdin/stdout, which would mean spawning
  a process per conversation. That is the `native` backend's shape, and it throws away the
  one thing Hermes has that Pi does not: a server that is already running.

So: the **API server** (`http://127.0.0.1:8642` by default, `API_SERVER_ENABLED = true`),
which is documented *as an integration surface for other front ends* — exactly our case.

**The Runs API, not Chat Completions**, and the difference is the stop button. Both are
OpenAI-ish SSE surfaces on that server; only `/v1/runs` produces a run that
`POST /v1/runs/{run_id}/stop` will accept. A Chat Completions turn is not a run: it names a
`chatcmpl-…` *completion*, and stopping that answers `404 run_not_found` — verified against a
live server, where a real `run_…` id stops cleanly. The first version of this adapter used
Chat Completions, so its stop button silently did nothing.

The move costs one thing and buys three. It costs `toolCallId` on the live tool frames —
runs carry `tool` and `preview` instead, so a start and its end pair by order rather than by
id, which is all the page ever used. It buys:

1. **A working stop.** `POST /v1/runs/{run_id}/stop` → `run.cancelled`.
2. **Tool outcome.** `tool.completed` carries `duration` and an `error` flag, so a failed
   tool is visible. Chat Completions' `hermes.tool.progress` carries no result at all, so
   every tool row there was a start and an end with nothing in between.
3. **Reasoning.** `reasoning.available` arrives as a `Delta(channel="thinking")`, which the
   page shows when the process toggle is on — Hermes had no reasoning channel before.

Three documented details drive the implementation, and each is a bug if assumed otherwise:

1. **The event name is a field, not an SSE header.** Every frame is `data: {"event": …}`.
   (`hermes.tool.progress`, on the Chat Completions path, was the opposite — unnamed in its
   body and named only on an `event:` line — which is why reading it went wrong for a whole
   release.)
2. **Every stream carries `: keepalive` comments** when nothing has been sent for thirty
   seconds, deliberately, so a long tool call does not trip a client's idle timeout. A parser
   that treats a `:` line as data breaks on the first slow tool.
3. **`Idempotency-Key` is honoured durably on run creation.** An identical retry returns the
   original `run_id` with HTTP 202, including after a restart; reusing a key with a different
   body is a 409. So this adapter sends one per message rather than hoping a retry was not a
   duplicate.

**History is read, not kept.** `GET /api/sessions/{id}/messages` returns the session's
transcript, so this adapter implements `history()` and stores nothing (`base.history`).
Verified: a turn run through `/v1/runs` with a `session_id` lands in that session's
transcript exactly as a Chat Completions turn did, so moving surfaces did not cost history.
Hermes keeps its own durable transcript for the same session the Desktop, the CLI and every
messaging channel are talking to — a second copy here would be a copy that can disagree.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
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

logger = logging.getLogger("csd.agent.hermes")

#: The model name Hermes documents for its own endpoint. It is a **sentinel**, not a model
#: id: it selects whatever provider Hermes is configured with. On the Runs API that intent is
#: expressed by *omitting* `model` entirely, so this value is never sent — `_run_request`
#: sends `model` only when someone has set something other than this.
DEFAULT_MODEL = "hermes-agent"

#: Connect timeout, and a separate read timeout of **None** for streams.
#:
#: This is not a detail. A turn can legitimately spend minutes inside one tool call, and
#: Hermes answers that with a keepalive comment rather than tokens. A read timeout here
#: would cut exactly the turns the keepalive exists to protect — the failure the OpenClaw
#: notes call out as "the stream died" with no error on either side.
CONNECT_TIMEOUT = 5.0

#: How long a non-streaming call may take (`/health`, a session create, a transcript read).
REQUEST_TIMEOUT = 20.0

#: How much of a transcript to read. The page shows a conversation, not an archive.
HISTORY_LIMIT = 500

#: What a run's terminal events mean. Read out of `api_server.py`'s run writer and confirmed
#: on a live server: `run.completed` carries the answer, `run.failed` carries an `error`,
#: `run.cancelled` is a stop, and `run.interrupted` is a gateway shutdown — the last of which
#: is *not* the agent's failure and must not be labelled as one.
_RUN_FAILED = "run.failed"
_RUN_TERMINAL = ("run.completed", _RUN_FAILED, "run.cancelled", "run.interrupted")

#: `tool.completed` reports failure through `error`, a boolean, not through the event name —
#: the name is `tool.completed` either way. `tool.failed` is handled too: it exists on the
#: session-chat stream, and treating a deployment that sends it as a success would be the
#: same mistake the Chat Completions path made by having no outcome field at all.
_TOOL_EVENTS = ("tool.started", "tool.completed", "tool.failed")


class HermesBackend(AgentBackend):
    """A client of one Hermes API server.

    Stateless over HTTP: unlike the two WebSocket backends there is no connection to keep
    subscribed, so "attached" only means "this session id is one we have verified".
    """

    name = "hermes"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str = DEFAULT_MODEL,
        on_event: EventSink,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._on_event = on_event

        self._client: Any = None
        self._connected = False
        self._last_error: str | None = None
        self._sessions: set[str] = set()
        self._streaming: set[str] = set()
        self._buffers: dict[str, list[str]] = {}
        #: Newest run id per session, for `POST /v1/runs/{run_id}/stop`.
        self._runs: dict[str, str] = {}
        #: Tasks driving live streams, so `stop()` can cancel them.
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._client is not None:
            return
        import httpx

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers(),
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT, read=None),
            # A personal agent is a local component, so this is loopback. Honouring a system
            # proxy would send 127.0.0.1 through it, and the proxy's own error would read as
            # "Hermes is down" on any developer machine that runs one.
            trust_env=False,
        )
        await self._probe()

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        self._connected = False

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _probe(self) -> None:
        """Decide `connected` in two steps, because one of them is not enough.

        `/health` is **unauthenticated** on the real server — verified against a live one,
        where a wrong key and no key at all both get a `200`. So it can only answer "is
        something listening"; on its own it reports a bad credential as a healthy
        connection, and the first thing the user meets is a message that fails for a reason
        the header called fine.

        The credential therefore gets its own check against an endpoint that does enforce
        it. The two failures stay distinct because they need opposite actions: start the
        server, or supply a working key.
        """
        try:
            response = await self._client.get("/health")
        except Exception as exc:
            self._last_error = f"no Hermes API server at {self.base_url} ({exc.__class__.__name__})"
            self._connected = False
            return
        if response.status_code >= 500:
            self._last_error = f"Hermes is up but unhealthy (HTTP {response.status_code})"
            self._connected = False
            return

        # Read-only and cheap, and the one that actually checks the key.
        try:
            check = await self._client.get("/api/sessions", params={"limit": 1})
        except Exception as exc:
            self._last_error = f"Hermes did not answer /api/sessions ({exc.__class__.__name__})"
            self._connected = False
            return
        if check.status_code in (401, 403):
            self._last_error = (
                "Hermes rejected the API key (set CSD_HERMES_API_KEY to API_SERVER_KEY)"
            )
            self._connected = False
            return
        self._connected = check.status_code < 500
        self._last_error = None

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    async def new_conversation(self) -> str:
        """Create a session on the API server, and return the id it gave it.

        `source` names what kind of client asked. Hermes enumerates them (`cli`, `tui`,
        `gui`, `acp`, `tool`, `oneshot`) and `tool` is the one that means "a third-party
        integration" — which is what this service is. Claiming to be a `cli` would put our
        conversations in the same bucket as someone's terminal. The server records its own
        `api_server` against the row either way; sending the field is about asking
        honestly, not about controlling what comes back.

        The answer is `201` with the id **nested under `session`** and no top-level `id`, so
        the nested read below is the one that actually fires.
        """
        client = await self._require_client()
        response = await client.post(
            "/api/sessions",
            json={"source": "tool"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        if response.status_code >= 400:
            raise BackendUnavailable(_describe_http("create a session", response))
        payload = _json_or_empty(response)
        session_id = _first_str(payload, "id", "session_id", "sessionId") or _first_str(
            payload.get("session") if isinstance(payload.get("session"), dict) else {}, "id"
        )
        if not session_id:
            raise BackendUnavailable("Hermes created a session but named no id")
        self._sessions.add(session_id)
        return session_id

    async def attach(self, conversation_id: str) -> None:
        """Confirm the session exists on the agent side, and remember it.

        A missing session is logged rather than raised: it means the thread outlived the
        conversation (reset, deleted, a different profile). Failing the page load over it
        would take the whole page down for one stale thread, while `send` will report the
        same fact at the moment it actually matters.
        """
        client = await self._require_client()
        try:
            response = await client.get(f"/api/sessions/{conversation_id}")
            if response.status_code == 404:
                logger.warning("Hermes has no session %s; it may have been reset", conversation_id)
            else:
                self._sessions.add(conversation_id)
        except Exception as exc:
            logger.warning("could not verify session %s: %s", conversation_id, exc)

    def _run_request(self, conversation_id: str, text: str) -> dict[str, Any]:
        """The `POST /v1/runs` body.

        `session_id` is what keeps this turn on *that* conversation: the run loads the
        session's own transcript and writes back to it, which is what `history()` reads
        afterwards. It has to name a session the API server itself created — one another
        surface owns (a CLI, the Desktop, a messaging channel) is refused with `409`, so
        this service cannot be pointed at someone else's chat by mistake.
        """
        body: dict[str, Any] = {"input": text, "session_id": conversation_id}
        if self.model and self.model != DEFAULT_MODEL:
            # `hermes-agent` is a sentinel meaning "whatever Hermes is configured with",
            # and on this surface that is spelled by leaving the field out. Sending it
            # literally would ask the server to route to a model by that name.
            body["model"] = self.model
        return body

    async def send(self, conversation_id: str, text: str) -> Message | None:
        """Start a run, then attach to its event stream; return once the run is *created*.

        Two calls rather than one, and the split is the point. `POST /v1/runs` answers
        immediately with a `run_id` and does not hold the connection open; the events stream
        is a separate request that can be attached — or re-attached — at any time, because
        the server buffers a run's events for a client that arrives late or went away.

        That is also the reason `abort()` can work at all, so the id is stored the moment it
        exists rather than being hunted for in a later frame.

        **Returns `None` on purpose.** Neither call names the user's own transcript row: a
        run id is a *run*, and the row is only readable afterwards through `history()`.
        Handing back a run id labelled as a message id would put a value on the page that
        never matches the one history returns.
        """
        client = await self._require_client()
        response = await client.post(
            "/v1/runs",
            json=self._run_request(conversation_id, text),
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        if response.status_code >= 400:
            detail = _describe_http("start a run", response, body=await response.aread())
            raise BackendUnavailable(detail)
        run_id = _first_str(_json_or_empty(response), "run_id", "id")
        if not run_id:
            raise BackendUnavailable("Hermes started a run but named no run id")

        self._sessions.add(conversation_id)
        self._runs[conversation_id] = run_id
        self._buffers[conversation_id] = []
        self._streaming.add(conversation_id)

        stream = await client.send(
            client.build_request(
                "GET",
                f"/v1/runs/{run_id}/events",
                headers={"Accept": "text/event-stream"},
            ),
            stream=True,
        )
        if stream.status_code >= 400:
            detail = _describe_http("read a run's events", stream, body=await stream.aread())
            await stream.aclose()
            self._streaming.discard(conversation_id)
            raise BackendUnavailable(detail)

        task = asyncio.create_task(
            self._consume(conversation_id, stream), name="hermes-run-events"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    async def abort(self, conversation_id: str) -> bool:
        """Stop the run behind this conversation.

        `POST /v1/runs/{run_id}/stop` is the only stop control Hermes has, and it names a
        *run* — which is the whole reason this adapter builds its turns through `/v1/runs`
        and keeps the id from the moment it is issued. Verified against a live server: the
        call answers `{"status": "stopping"}` and the run settles as `cancelled`, with a
        `run.cancelled` event on the stream.

        With no run id there is nothing to name, and this says so rather than cancelling the
        local stream and calling that stopped — which would tell the page a run had ended
        that is still running over there.
        """
        if conversation_id not in self._streaming:
            return False
        run_id = self._runs.get(conversation_id)
        if not run_id:
            logger.warning("no run id for %s; cannot stop it", conversation_id)
            return False
        client = await self._require_client()
        response = await client.post(f"/v1/runs/{run_id}/stop", json={})
        if response.status_code >= 400:
            logger.warning("Hermes refused to stop %s: %s", run_id, response.status_code)
            return False
        return True

    async def history(self, conversation_id: str) -> list[Message]:
        """The session's transcript, as Hermes has it. Nothing is stored on this side.

        The rows come back under **`data`**, with the session id beside them — verified
        against a real server, and not what the endpoint's name suggests. Reading `messages`
        instead returns an empty list for every conversation, forever, which reads on screen
        exactly like a conversation nobody has had.
        """
        client = await self._require_client()
        response = await client.get(f"/api/sessions/{conversation_id}/messages")
        if response.status_code == 404:
            # A conversation Hermes no longer has is an empty one, not an error the page
            # should shout about: the thread outlived the session.
            return []
        if response.status_code >= 400:
            raise BackendUnavailable(_describe_http("read the transcript", response))
        payload = _json_or_empty(response)
        if isinstance(payload, dict):
            rows = payload.get("data")
            if not isinstance(rows, list):
                rows = payload.get("messages")  # the spelling an earlier guess used
        else:
            rows = payload
        rows = rows if isinstance(rows, list) else []
        return [m for m in (_row_to_message(row) for row in rows[:HISTORY_LIMIT]) if m is not None]

    async def _require_client(self):
        if self._client is None:
            raise BackendUnavailable("the Hermes backend has not been started")
        if not self._connected:
            raise BackendUnavailable(
                self._last_error or f"not connected to the Hermes API server at {self.base_url}"
            )
        return self._client

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #
    def is_live(self, conversation_id: str) -> bool:
        return self._connected and conversation_id in self._sessions

    def is_streaming(self, conversation_id: str) -> bool:
        return conversation_id in self._streaming

    def is_available(self) -> bool:
        """Whether this backend could work at all, ignoring whether it is up right now.

        A missing API key is the one thing that cannot be fixed by starting something: every
        request would be a 401, so the settings row should say the credential is missing
        rather than invite someone to check whether the server is running.
        """
        return bool(self.api_key)

    def status(self) -> BackendStatus:
        detail = self.base_url
        if not self._connected and self._last_error:
            detail = f"{self.base_url} — {self._last_error}"
        return BackendStatus(
            name=self.name,
            connected=self._connected,
            detail=detail,
            conversations=len(self._sessions),
        )

    # ------------------------------------------------------------------ #
    # the stream
    # ------------------------------------------------------------------ #
    def _emit(self, conversation_id: str, event: Any) -> None:
        try:
            result = self._on_event(conversation_id, event)
        except Exception:
            logger.exception("event sink failed")
            return
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)  # noqa: RUF006 - deliberately not awaited

    async def _consume(self, conversation_id: str, response: Any) -> None:
        """Read the run's SSE stream until it ends, emitting as it goes.

        Every frame is `data: {"event": …}` — here the name is a *field*, not an SSE header.
        Its predecessor on the Chat Completions path, `hermes.tool.progress`, was the
        opposite: unnamed in its body and named only on an `event:` line, which is precisely
        why reading it went wrong for a whole release.
        """
        try:
            async for line in response.aiter_lines():
                if not line or line.startswith(":"):
                    # A blank line separates SSE blocks; `: keepalive` (every 30 s of quiet)
                    # and `: stream closed` are comments, not data. Hermes sends the keepalive
                    # so a long tool call does not look like a dead connection, and treating
                    # one as data would make every slow turn a parse error.
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    frame = json.loads(payload)
                except ValueError:
                    logger.debug("unparseable Hermes SSE frame: %.200s", payload)
                    continue
                if isinstance(frame, dict):
                    self._handle_frame(conversation_id, frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A stream that died mid-answer is worth saying out loud: the page can then show
            # the partial reply as an interrupted one rather than as a finished thought.
            self._emit(conversation_id, Notice(
                notice="hermes stream failed", level="error", detail=str(exc)[:300],
            ))
        finally:
            try:
                await response.aclose()
            except Exception:
                pass
            self._streaming.discard(conversation_id)
            # Flush whatever arrived: a stream that ended without a terminal frame still said
            # something, and dropping it would lose the visible half of the turn.
            self._flush(conversation_id)
            self._emit(conversation_id, Status(streaming=False))

    def _handle_frame(self, conversation_id: str, frame: dict[str, Any]) -> None:
        """One frame of a run's event stream, dispatched by its `event` name."""
        name = _first_str(frame, "event", "type")
        if not name:
            logger.debug("Hermes frame with no event name: %.200s", frame)
            return

        if name in _TOOL_EVENTS:
            self._on_tool_event(conversation_id, name, frame)
            return

        if name == "message.delta":
            text = frame.get("delta")
            if isinstance(text, str) and text:
                self._buffers.setdefault(conversation_id, []).append(text)
                self._emit(conversation_id, Delta(channel="text", text=text))
            return

        if name == "message.interim":
            # Mid-turn commentary: text the model writes alongside its tool calls rather
            # than the answer. `already_streamed` says the same text also went out as a
            # `message.delta`, and rendering it again would show the sentence twice.
            if frame.get("already_streamed"):
                return
            text = frame.get("text")
            if isinstance(text, str) and text:
                self._buffers.setdefault(conversation_id, []).append(text)
                self._emit(conversation_id, Delta(channel="text", text=text))
            return

        if name == "reasoning.available":
            # Hermes' own reasoning, arriving as one complete block. A `thinking` delta and
            # not prose: the page keeps it out of the transcript, and shows it only when the
            # process toggle is on.
            text = frame.get("text")
            if isinstance(text, str) and text.strip():
                self._emit(conversation_id, Delta(channel="thinking", text=text))
            return

        if name in _RUN_TERMINAL:
            self._on_run_terminal(conversation_id, name, frame)
            return

        if name == "run.stopping":
            self._emit(conversation_id, Notice(notice="stopping"))
            return

        # run.started, message.started, approval.*, and whatever is added next. Logged
        # rather than guessed at, so a new frame shows up in a log rather than on screen.
        logger.debug("ignoring Hermes run event %r", name)

    def _on_run_terminal(
        self, conversation_id: str, name: str, frame: dict[str, Any]
    ) -> None:
        """The end of a run, where "it ended" and "it worked" are different questions.

        Four terminal events, and collapsing them would misreport three of them: a run the
        user stopped, a run the agent failed, and a run a *gateway restart* interrupted are
        not completed answers. `run.interrupted` is deliberately not an error — nothing the
        agent did went wrong — but it is not a success either.
        """
        failed = name == _RUN_FAILED
        self._flush(conversation_id, is_error=failed)

        if failed:
            self._emit(conversation_id, Notice(
                notice="hermes run failed",
                level="error",
                detail=(_first_str(frame, "error") or "")[:500] or None,
            ))
        elif name == "run.cancelled":
            self._emit(conversation_id, Notice(notice="stopped"))
        elif name == "run.interrupted":
            self._emit(conversation_id, Notice(
                notice="interrupted", detail="the Hermes gateway stopped mid-turn",
            ))

        self._streaming.discard(conversation_id)
        self._emit(conversation_id, Status(streaming=False))

    def _on_tool_event(
        self, conversation_id: str, name: str, frame: dict[str, Any]
    ) -> None:
        """A tool starting or finishing, in the two shapes this stream really sends:

            {"event": "tool.started",   "tool": "read_file", "preview": "activate.sh"}
            {"event": "tool.completed", "tool": "read_file", "duration": 1.821,
             "error": false}

        `error` is the reason this adapter moved onto this surface at all. The Chat
        Completions event that used to carry tool activity had **no result field**, so a tool
        that failed and a tool that worked looked identical on screen.

        Neither frame names the tool *call*, so no `call_id` is set. `base.ToolCall` allows
        one and the page does not use it, and inventing one from the tool's name would
        correlate two concurrent calls to the same tool that are not the same call.

        `preview` is Hermes' own rendering of the arguments — a display string, not the
        arguments — so it is not passed as `args`. The page renders `tool_name`, and the real
        arguments reach it through `history()`.
        """
        try:
            tool = _first_str(frame, "tool", "tool_name", "name") or "tool"
            if name == "tool.started":
                self._emit(conversation_id, ToolCall(
                    phase="start", name=tool, call_id=_tool_call_id(frame),
                    args=_tool_args(frame),
                ))
            else:
                self._emit(conversation_id, ToolCall(
                    phase="end", name=tool, call_id=_tool_call_id(frame),
                    is_error=name == "tool.failed" or bool(frame.get("error")),
                ))
        except Exception:
            # A tool row is not worth losing a reply over.
            logger.exception("unreadable Hermes tool event: %.300s", frame)

    def _flush(self, conversation_id: str, *, is_error: bool = False) -> None:
        text = "".join(self._buffers.pop(conversation_id, []))
        if text.strip():
            self._emit(conversation_id, Message(
                role=AgentRole.assistant, content=text, is_error=is_error,
            ))


# --------------------------------------------------------------------------- #
# payload readers
#
# Kept local rather than shared with the other adapters on purpose: a backend is a module
# that translates one agent's protocol, and a helper shared between two translations is the
# first thread of the coupling the seam exists to prevent.
# --------------------------------------------------------------------------- #
def _first_str(mapping: Any, *keys: str) -> str | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _id_str(mapping: Any, *keys: str) -> str | None:
    """A row id as a string, whether the server sends a string or a whole number.

    Hermes numbers its transcript rows — the real ones are `"id": 6`, `"id": 7` — and a
    reader that only accepted strings dropped every one of them. The id is what the page
    dedups on when a refresh brings back a message it is already showing, so losing it means
    every refresh can duplicate the conversation.
    """
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _tool_call_id(frame: dict[str, Any]) -> str | None:
    """A tool call's id, when the frame names one.

    The runs stream does not: `tool.started` and `tool.completed` carry `tool`, `preview`,
    `duration` and `error` and no correlation id at all. Read defensively so a deployment
    that adds one is picked up, and `None` otherwise rather than a value invented from the
    tool's name — which would tie two concurrent calls to the same tool together as if they
    were one.
    """
    return _id_str(frame, "toolCallId", "tool_call_id", "call_id")


def _tool_args(frame: dict[str, Any]) -> dict[str, Any] | None:
    """A tool's arguments, when the frame carries them as a mapping.

    The runs stream sends `preview`, a display string Hermes builds *from* the arguments.
    Wrapping that in a dict would put something in `args` that is not what it says it is.
    The page renders `tool_name`; the real arguments reach it through `history()`.
    """
    for key in ("args", "arguments", "input"):
        value = frame.get(key)
        if isinstance(value, dict) and value:
            return value
    return None


def _epoch_to_iso(value: Any) -> str | None:
    """A row's own timestamp, as the ISO 8601 string the rest of this service speaks.

    Hermes stores float seconds. Carried through, a re-fetched transcript keeps the times
    the messages were actually sent; dropped, every message on the page is stamped with the
    moment the page loaded, which is a quiet falsehood rather than a missing field.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return (
            datetime.fromtimestamp(float(value), tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _json_or_empty(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return {}


def _describe_http(action: str, response: Any, body: bytes | None = None) -> str:
    """One line a person can act on, naming the status and what the server said."""
    detail = f"hermes could not {action}: HTTP {response.status_code}"
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else ""
    text = text.strip() or (getattr(response, "text", "") or "").strip()
    if text:
        detail += f" — {text[:300]}"
    if response.status_code in (401, 403):
        detail += " (check CSD_HERMES_API_KEY against API_SERVER_KEY)"
    return detail


def _row_to_message(row: Any) -> Message | None:
    """One transcript row as a `Message`, or `None` for rows this page does not render.

    Hermes' own transcript includes system turns and injected context, which are not things
    either party *said* — showing them as messages would put words in the agent's mouth.
    """
    if not isinstance(row, dict):
        return None
    role = row.get("role")
    if role == "system":
        return None
    agent_role = {
        "user": AgentRole.user,
        "assistant": AgentRole.assistant,
        "tool": AgentRole.tool,
    }.get(role)
    if agent_role is None:
        logger.debug("hermes transcript row with role=%r; skipping", role)
        return None

    content = row.get("content")
    if isinstance(content, list):
        # A text-block projection, so a row stored in the richer form still renders.
        content = "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)

    if agent_role is not AgentRole.tool and not content.strip():
        return None
    return Message(
        role=agent_role,
        content=content,
        # Hermes' own row id, which is what the page dedups on across a refresh. It arrives
        # as an **integer** — `_first_str` rejected it, so every id was silently dropped and
        # a refreshed history could not recognise the messages already on screen.
        id=_id_str(row, "id", "message_id"),
        tool_name=row.get("tool_name") or row.get("name"),
        tool_call_id=_id_str(row, "tool_call_id", "call_id"),
        created_at=_epoch_to_iso(row.get("timestamp")),
    )


BACKEND_NAME = "hermes"

BACKEND_LABEL = "Hermes"

#: What connecting involves: one setting on the Hermes side and one value shared with it.
BACKEND_BLURB = (
    "Hermes' API server (HTTP + SSE), not the Desktop app's private socket. Set "
    "API_SERVER_ENABLED = true there and give this service its API_SERVER_KEY."
)


def build(settings, on_event: EventSink) -> HermesBackend:
    """Construct the backend from settings, for the registry to call."""
    return HermesBackend(
        base_url=settings.hermes_api_url,
        api_key=settings.hermes_api_key,
        model=settings.hermes_model,
        on_event=on_event,
    )


__all__ = ["HermesBackend", "DEFAULT_MODEL"]
