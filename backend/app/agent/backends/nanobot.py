"""The nanobot backend: this service as a client of a nanobot gateway.

nanobot runs the agent — its memory, its model credentials, its tools, its workspace. This
service carries messages to it and renders what comes back. That is the whole relationship,
and it is the one nanobot's own channel protocol was designed for: a websocket client that
speaks JSON frames and is welcome to render `_agent_ui` payloads that other channels ignore.

Protocol reference: `nanobot/docs/websocket.md`, and `nanobot/docs/guides/ai-agent-webui.md`
for the same shape of client. Verified against `nanobot/channels/websocket/runtime.py`.

**The door is the gateway's own WebUI socket, and the agent can point us at it itself.** The
websocket channel listens on `127.0.0.1:8765` by default and `GET /webui/bootstrap` hands a
local caller a one-time token (`nanobot/webui/ws_http.py`). That is why §25 can offer this as
an onboarding kind now: opening the *rich* door needs no config edit and no gateway restart —
the token is minted per connection, from a secret the agent already has. The token-issue
secret, when one is configured, is the `token` a registration carries; when it is empty the
bootstrap route accepts any loopback caller.

Five behaviours here come from reading that source rather than guessing, and each would be a
subtle bug if assumed:

1. **`stream_end` marks the end of a segment, not of the turn.** With `resuming: true` the
   agent continues (it is about to call a tool), and the text just assembled is that stage's
   checkpoint; without it, that was the answer. A client that treats every `stream_end` as
   the end of the reply files each checkpoint as a final answer and marks the turn finished
   four times.
2. **A checkpoint's text arrives as deltas, and the `message` frame that follows is only a
   marker.** nanobot streams the text, then sends `message` with `kind="progress"` and no
   text. Reading the marker as the message would duplicate every checkpoint; storing only the
   marker would lose it.
3. **`attach` does not replay history.** It subscribes a connection to a `chat_id` and nothing
   more, and nanobot exposes no history endpoint. So this service is a *mirror of what it has
   seen*, which is exactly what an IM channel can honestly claim to be. See docs/design.md §17.
4. **Stopping is a chat command, not a protocol verb.** There is no `abort` frame; `/stop` is
   how a participant stops the current task, so that is what this sends.
5. **`user_message` reports input from *other* channels.** It exists so a WebSocket client can
   see that the same conversation was continued from Telegram. That is worth showing — it is
   the entire point of a shared session — but it also means our own sends may echo back, so
   `_just_sent` suppresses the round trip without suppressing genuine cross-channel traffic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import threading
from collections import defaultdict, deque
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ...schemas import AgentRole, utcnow
from .. import endpoints
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

logger = logging.getLogger("csd.agent.nanobot")

#: Reconnect backoff ceiling. A gateway restart should be invisible, not a dead page.
MAX_BACKOFF = 30.0

#: How long to wait for an `attached` reply before giving up on a conversation.
ATTACH_TIMEOUT = 20.0

#: How long a call waits for the background handshake before answering "not connected".
#: Short on purpose: it covers a switch or a gateway blip, not an agent that is simply off.
CONNECT_WAIT = 5.0

#: Where a registered (http) door mints a one-time websocket token.
BOOTSTRAP_PATH = "/webui/bootstrap"

#: Token fetching is a local HTTP round trip; the read side of a turn is unbounded, this is not.
BOOTSTRAP_TIMEOUT = 10.0


def _describe_edits(edits: Any) -> str:
    """Render nanobot's `file_edit` payload as text a transcript can show.

    Defensive on purpose: the edit dictionaries come from nanobot's tool layer and are not
    part of its documented wire contract, so this reads whatever is there and degrades to a
    JSON dump rather than raising. A tool row is not worth losing a message over.
    """
    if not isinstance(edits, list) or not edits:
        return "(no changes reported)"
    lines: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        path = edit.get("path") or edit.get("file") or edit.get("file_path") or "?"
        added = edit.get("added") or edit.get("added_lines")
        removed = edit.get("removed") or edit.get("removed_lines")
        summary = str(path)
        if added is not None or removed is not None:
            summary += f"  +{added or 0} -{removed or 0}"
        diff = edit.get("diff")
        if isinstance(diff, str) and diff.strip():
            summary += "\n" + diff.strip()
        lines.append(summary)
    return "\n".join(lines) if lines else json.dumps(edits, ensure_ascii=False)[:2000]


class NanobotTranscript:
    """nanobot's own copy of what this channel has seen. Its storage, its schema, its problem.

    **This exists for exactly one reason, and the reason is not nanobot.** nanobot's channel
    cannot replay: `attach` subscribes a connection to a `chat_id` and nothing more, and the
    channel carries no history frame. So without a copy here, a page refresh would show an empty
    conversation. Every *other* agent this service can talk to — Pi, OpenClaw — exposes its
    transcript on the protocol it already speaks, implements `history()`, and keeps no table at
    all. That is the rule (see `base.py`), and this class is the case it exists for.

    It deliberately does **not** live in the core schema. The shape below is nanobot's shape:
    it can grow a column for a nanobot event without a migration touching anyone else, and no
    other adapter inherits its vocabulary. The old `agent_messages` was the opposite — one
    table every backend wrote to, shaped as the lowest common denominator, which is why it could
    hold neither Pi's tool arguments nor its per-message model.

    Two things worth knowing about the implementation:

    - **The id it hands back is stable and its own.** The page draws a message optimistically
      from the POST and meets it again in a re-fetched history; the two are recognised as one by
      id, so the id has to survive a refresh and a restart. A rowid does; a counter would not.
    - **It is a mirror, and says so.** nanobot keeps its own sessions and this is only what came
      over the wire. A conversation continued from Telegram appears from that point on and is not
      retroactively filled in. `sqlite3` directly rather than the core `Database`, because this
      is the adapter's file to open.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS nanobot_messages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id      TEXT NOT NULL,
        role         TEXT NOT NULL,
        content      TEXT NOT NULL,
        tool_name    TEXT,
        tool_call_id TEXT,
        args_json    TEXT,
        is_error     INTEGER NOT NULL DEFAULT 0,
        progress     INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_nanobot_messages_chat
        ON nanobot_messages (chat_id, id);
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        #: **Nothing is opened here.** `build()` constructs every registered backend so the
        #: settings panel can list them, and the supervisor's contract is that construction
        #: costs nothing. Opening the file in `__init__` made a deployment running `native`
        #: create an empty `nanobot-transcript.sqlite3` (plus a WAL) on every start — a
        #: side effect of looking at a menu. The connection is made on first use instead.
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _connection(self) -> sqlite3.Connection:
        """Open and prepare the database on first use, once."""
        if self._conn is not None:
            return self._conn
        with self._lock:
            if self._conn is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.path, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(self.SCHEMA)
                self._migrate(conn)
                conn.commit()
                self._conn = conn
        return self._conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add columns an older copy of this table is missing.

        Same reasoning as the core's `Database._migrate`: `CREATE TABLE IF NOT EXISTS` leaves an
        existing table alone, so a column added to the schema never appears in a deployment that
        already has the file — and the failure shows up at query time rather than at boot. The
        column's own presence is the marker, so there is nothing to keep in sync.
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(nanobot_messages)")}
        if "progress" not in columns:
            conn.execute(
                "ALTER TABLE nanobot_messages ADD COLUMN progress INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def record(self, chat_id: str, message: Message) -> Message:
        """Store a message and return it carrying the id this store assigned.

        Returning a copy rather than mutating is what keeps the call sites honest: a `Message`
        that came from the wire has no id, and one that has been recorded does.
        """
        # A message from the wire has no timestamp; a row restored from the old shared table
        # does, and losing it would make the restored conversation look like it all arrived at
        # upgrade time.
        created_at = message.created_at or utcnow()
        cursor = self._connection().execute(
            """
            INSERT INTO nanobot_messages
                (chat_id, role, content, tool_name, tool_call_id, args_json, is_error,
                 progress, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                message.role.value,
                message.content,
                message.tool_name,
                message.tool_call_id,
                json.dumps(message.args, ensure_ascii=False) if message.args else None,
                int(message.is_error),
                int(message.progress),
                created_at,
            ),
        )
        self._connection().commit()
        return Message(
            role=message.role,
            content=message.content,
            id=f"nb_{cursor.lastrowid}",
            tool_name=message.tool_name,
            tool_call_id=message.tool_call_id,
            args=message.args,
            is_error=message.is_error,
            created_at=created_at,
            progress=message.progress,
        )

    def messages(self, chat_id: str) -> list[Message]:
        """Everything recorded for one conversation, oldest first."""
        rows = self._connection().execute(
            "SELECT * FROM nanobot_messages WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        out: list[Message] = []
        for row in rows:
            args: dict | None = None
            if row["args_json"]:
                with suppress(json.JSONDecodeError, TypeError):
                    args = json.loads(row["args_json"])
            out.append(
                Message(
                    role=AgentRole(row["role"]),
                    content=row["content"],
                    id=f"nb_{row['id']}",
                    tool_name=row["tool_name"],
                    tool_call_id=row["tool_call_id"],
                    args=args,
                    is_error=bool(row["is_error"]),
                    created_at=row["created_at"],
                    progress=bool(row["progress"]),
                )
            )
        return out


class NanobotBackend(AgentBackend):
    """A websocket client for one nanobot gateway.

    One connection multiplexes every conversation (`chat_id`), which is why this is a
    single long-lived object rather than one per thread — the opposite of the native backend,
    and precisely the kind of difference the `AgentBackend` seam exists to absorb.
    """

    name = "nanobot"

    def __init__(
        self,
        *,
        url: str,
        client_id: str,
        token: str | None,
        on_event: EventSink,
        transcript: NanobotTranscript | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        #: Where the *settings* say the door is (`CSD_NANOBOT_WS_URL`, `CSD_NANOBOT_TOKEN`).
        #: Only a fallback: an agent that registered a door of its own wins, and is re-read on
        #: every connect — a registration that arrives after startup has to be honoured without
        #: bouncing the service, which is the whole point of §25's onboarding flow.
        self._settings_url = url
        self._settings_token = token
        #: What the last connect attempt dialled, for status and error messages.
        self.url = url
        self.client_id = client_id
        self._on_event = on_event
        #: Optional so the backend can be exercised without a database — the adapter tests do
        #: that — and `None` is a real state rather than a test artefact: with no transcript,
        #: `history()` honestly answers "nothing", which is what a channel that cannot replay
        #: has to say when nobody is keeping a copy.
        self._transcript = transcript
        self._connect_timeout = connect_timeout

        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._connected = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Why the last connect attempt failed, for the settings row. Cleared on success.
        self._last_error: str | None = None

        #: Conversations this process cares about. Re-attached after any reconnect, because a
        #: subscription is connection-scoped and silently disappears with the socket.
        self._conversations: set[str] = set()
        #: Conversations whose reply is still arriving.
        self._streaming: set[str] = set()
        #: Assistant text assembled from `delta` frames, per conversation.
        self._buffers: dict[str, list[str]] = defaultdict(list)
        #: The segment text last flushed at `stream_end`, so the textless (or duplicate) marker
        #: frame that follows it is not stored a second time.
        self._last_segment: dict[str, str] = {}
        #: Reply text we sent, so the echo of our own message is not stored twice.
        self._just_sent: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=8))
        #: Waiters for `attached` replies, oldest first.
        self._attach_waiters: deque[asyncio.Future[str]] = deque()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="nanobot-backend")

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        self._connected.clear()
        if self._transcript is not None:
            # Closed here rather than left to the garbage collector: `Supervisor.shutdown`
            # runs before the process exits, and a WAL file left mid-write is a transcript
            # that has to be recovered on the next start rather than one that is simply there.
            self._transcript.close()
            self._transcript = None

    def _door(self) -> tuple[str, str | None]:
        """Where to knock, and what to knock with — the registration first, settings after.

        Read on every connect rather than captured at construction, because the registration
        is what an agent does *after* the service is already running: a door that appeared a
        minute ago has to be used without a restart, or onboarding would need one.
        """
        endpoint = endpoints.get(BACKEND_NAME)
        if endpoint is not None:
            return endpoint.url, endpoint.token
        return self._settings_url, self._settings_token

    def _build_url(self, base: str, token: str | None) -> str:
        # The token goes in the query string because that is the protocol nanobot defines —
        # *its* websocket channel, not ours. Unlike our own agent stream (which uses a header
        # precisely to keep a dangerous credential out of URLs), this credential is nanobot's
        # and scoped to nanobot; it does not execute anything on this host. It is also
        # short-lived: the bootstrap route mints one per connection.
        parts = urlsplit(base if "://" in base else f"ws://{base}")
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "token"
        ]
        query.append(("client_id", self.client_id))
        if token:
            query.append(("token", token))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path or "/", urlencode(query), "")
        )

    async def _bootstrap(self, base: str, secret: str | None) -> tuple[str, str]:
        """Trade the registered address for a one-time socket token.

        `/webui/bootstrap` is the gateway's own route for this (`nanobot/webui/ws_http.py`): a
        local caller gets `{token, ws_url}`, and with a `tokenIssueSecret` configured the
        caller has to present it. That secret is what a registration carries as `token`.

        The response also contains an `api_token` that drives the gateway's whole WebUI HTTP
        API. It is deliberately ignored: this backend needs one socket, and a credential for
        somebody else's control surface is not something to hold.
        """
        import httpx

        headers = {"Accept": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        url = base.rstrip("/") + BOOTSTRAP_PATH
        # Loopback: a system proxy (on Windows, from the registry) would otherwise intercept
        # this and its answer would look like "the agent's door is down".
        async with httpx.AsyncClient(trust_env=False, timeout=BOOTSTRAP_TIMEOUT) as client:
            try:
                response = await client.get(url, headers=headers)
            except Exception as exc:
                raise BackendUnavailable(
                    f"nothing answering at {url} ({exc.__class__.__name__}). Is the nanobot "
                    "gateway running?"
                ) from exc
        if response.status_code == 401:
            raise BackendUnavailable(
                f"{url} answered 401 — the registered token is not this gateway's "
                "channels.websocket.tokenIssueSecret"
            )
        if response.status_code >= 400:
            raise BackendUnavailable(f"{url} answered {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendUnavailable(f"{url} did not answer JSON") from exc
        token = payload.get("token") if isinstance(payload, dict) else None
        ws_url = payload.get("ws_url") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token or not isinstance(ws_url, str) or not ws_url:
            raise BackendUnavailable(f"{url} answered without a token or a ws_url")
        return ws_url, token

    async def probe(self) -> None:
        """Knock on the door without switching to it (`POST /api/agent/backends` calls this).

        For a registered address that means asking for a token and throwing it away: it proves
        the route exists *and* that the credential is right, without opening a socket and
        without consuming the token (it expires unused). A raw `ws://` URL from settings has no
        HTTP route to knock on, so there the probe is the handshake itself.
        """
        url, token = self._door()
        if url.startswith(("ws://", "wss://")):
            import websockets

            try:
                async with websockets.connect(
                    self._build_url(url, token), open_timeout=self._connect_timeout
                ):
                    return
            except websockets.exceptions.InvalidStatus as exc:
                status = getattr(exc.response, "status_code", None)
                raise BackendUnavailable(
                    f"{url} refused the handshake ({status}) — this door wants a token. "
                    "Register the gateway's HTTP address instead and agenest will mint one."
                ) from exc
            except Exception as exc:
                raise BackendUnavailable(
                    f"nothing answering at {url} ({exc.__class__.__name__}). Is the nanobot "
                    "gateway running?"
                ) from exc
        await self._bootstrap(url, token)

    async def _run(self) -> None:
        import websockets

        backoff = 1.0
        while not self._closing:
            try:
                door, secret = self._door()
                self.url = door
                if door.startswith(("ws://", "wss://")):
                    ws_url, ws_token = door, secret
                else:
                    ws_url, ws_token = await self._bootstrap(door, secret)
                async with websockets.connect(
                    self._build_url(ws_url, ws_token), open_timeout=self._connect_timeout
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    self._last_error = None
                    logger.info("connected to nanobot at %s", self.url)
                    self._connected.set()
                    await self._resubscribe()
                    async for raw in ws:
                        try:
                            self._handle(json.loads(raw))
                        except Exception:
                            logger.exception("bad nanobot frame: %.300s", raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Kept for the panel, not just the log: "the door did not open" and *why* is
                # the difference between a user checking nanobot's unit and one checking a
                # credential. The next success clears it.
                self._last_error = str(exc)[:300]
                if not self._closing:
                    logger.warning("nanobot connection lost: %s", exc)
            finally:
                self._ws = None
                self._connected.clear()
                self._fail_attach_waiters("disconnected")

            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _resubscribe(self) -> None:
        """Re-attach every conversation we know about after a (re)connect."""
        for chat_id in list(self._conversations):
            await self._write({"type": "attach", "chat_id": chat_id})

    def _fail_attach_waiters(self, reason: str) -> None:
        while self._attach_waiters:
            waiter = self._attach_waiters.popleft()
            if not waiter.done():
                waiter.set_exception(BackendUnavailable(f"nanobot {reason}"))

    async def _write(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise BackendUnavailable("not connected to nanobot")
        await ws.send(json.dumps(payload, ensure_ascii=False))

    async def _await_attach(self, timeout: float = ATTACH_TIMEOUT) -> str:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._attach_waiters.append(future)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._attach_waiters.remove(future)
            raise BackendUnavailable(
                "nanobot did not confirm the conversation in time"
            ) from None

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    async def new_conversation(self) -> str:
        await self._require_connected()
        waiter = asyncio.get_running_loop().create_future()
        self._attach_waiters.append(waiter)
        await self._write({"type": "new_chat"})
        try:
            chat_id = await asyncio.wait_for(waiter, timeout=ATTACH_TIMEOUT)
        except asyncio.TimeoutError:
            if waiter in self._attach_waiters:
                self._attach_waiters.remove(waiter)
            raise BackendUnavailable("nanobot did not return a new conversation") from None
        self._conversations.add(chat_id)
        return chat_id

    async def attach(self, conversation_id: str) -> None:
        await self._require_connected()
        self._conversations.add(conversation_id)
        waiter: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._attach_waiters.append(waiter)
        await self._write({"type": "attach", "chat_id": conversation_id})
        try:
            await asyncio.wait_for(waiter, timeout=ATTACH_TIMEOUT)
        except asyncio.TimeoutError:
            if waiter in self._attach_waiters:
                self._attach_waiters.remove(waiter)
            # Not fatal: the conversation is registered and will be re-attached on the next
            # connect. Failing the page load over a slow ack would be worse than the ack.
            logger.warning("no `attached` ack for %s; continuing", conversation_id)

    async def send(self, conversation_id: str, text: str) -> Message | None:
        """Deliver a user message, and answer with the row this channel recorded for it.

        Recorded here rather than in the layer above, because here is where the echo is known
        about: nanobot sends our own message back as `user_message`, and `_just_sent` is what
        tells that echo apart from genuine input from another channel. Storing the message at the
        moment we send it — and returning the stored row — means the page's early copy and the
        history's copy carry one id, with no reconciliation.
        """
        await self._require_connected()
        self._conversations.add(conversation_id)
        message = Message(role=AgentRole.user, content=text)
        if self._transcript is not None:
            message = self._transcript.record(conversation_id, message)
        self._just_sent[conversation_id].append(text)
        await self._write({"type": "message", "chat_id": conversation_id, "content": text})
        return message

    async def history(self, conversation_id: str) -> list[Message]:
        """What this channel has seen, from the copy it is forced to keep.

        Empty when there is no transcript configured, which is the honest answer: an agent whose
        protocol cannot replay and whose adapter keeps nothing has no history to give, and
        inventing one would be worse than saying so.
        """
        if self._transcript is None:
            return []
        return self._transcript.messages(conversation_id)

    async def abort(self, conversation_id: str) -> bool:
        if not self.is_live(conversation_id):
            return False
        # `/stop` is nanobot's in-chat stop command — see the module docstring, point 4. It is
        # written to the socket directly rather than through `send()`, which would record it as
        # a message the *user* said: pressing Stop must not put "/stop" in the transcript.
        await self._require_connected()
        self._just_sent[conversation_id].append("/stop")
        await self._write({"type": "message", "chat_id": conversation_id, "content": "/stop"})
        return True

    async def _require_connected(self) -> None:
        # A short wait rather than an instant failure: the socket is opened in the background,
        # so the first call after switching the page to this agent — or after a gateway
        # restart — races the handshake by a few milliseconds. Failing that race would make
        # the switch look broken exactly once, which is the worst kind of bug to report.
        if not self._connected.is_set():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._connected.wait(), timeout=CONNECT_WAIT)
        if not self._connected.is_set():
            reason = f" ({self._last_error})" if self._last_error else ""
            raise BackendUnavailable(
                f"not connected to nanobot at {self.url}. Is `nanobot gateway` running?{reason}"
            )

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #
    def is_live(self, conversation_id: str) -> bool:
        return self._connected.is_set() and conversation_id in self._conversations

    def is_streaming(self, conversation_id: str) -> bool:
        return conversation_id in self._streaming

    def is_available(self) -> bool:
        """A gateway backend is always *usable* — it just may not be up yet.

        There is nothing to install or configure on this side: point it at a URL and start
        the gateway. So this is always true, and `status().connected` carries the part that
        actually varies.
        """
        return True

    def status(self) -> BackendStatus:
        # The registered door rather than the one last dialled, so a row that appeared since
        # startup shows up in the panel before anything reconnects.
        door, _ = self._door()
        detail = door
        if not self._connected.is_set() and self._last_error:
            detail = f"{door} — {self._last_error}"
        return BackendStatus(
            name=self.name,
            connected=self._connected.is_set(),
            detail=detail,
            conversations=len(self._conversations),
        )

    # ------------------------------------------------------------------ #
    # frames
    # ------------------------------------------------------------------ #
    def _emit(self, conversation_id: str, event: Any) -> None:
        """Hand an event up, without letting a slow consumer stall the socket reader.

        A completed `Message` is recorded on the way through, which is the only place it needs to
        happen: every message this backend reports — assistant text, a file edit — passes here
        exactly once, so there is one recording site rather than one per event type. The copy
        that comes back carries the id the store assigned, and that is what the page dedups on.
        """
        if isinstance(event, Message) and self._transcript is not None:
            try:
                event = self._transcript.record(conversation_id, event)
            except Exception:
                # A message that cannot be written down is still a message. Reporting it without
                # an id degrades the page's dedup; swallowing it would lose it from the screen
                # entirely, which is worse.
                logger.exception("could not record a message for %s", conversation_id)
        try:
            result = self._on_event(conversation_id, event)
        except Exception:
            logger.exception("event sink failed")
            return
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)  # noqa: RUF006 - deliberately not awaited

    def _handle(self, frame: dict[str, Any]) -> None:
        event = frame.get("event")
        chat_id = frame.get("chat_id")

        if event == "ready":
            # The connection's default chat. Registered so it is re-attached like any other,
            # but not created as a thread: merely connecting should not invent a conversation.
            if isinstance(chat_id, str):
                self._conversations.add(chat_id)
            return

        if event == "attached":
            if isinstance(chat_id, str):
                self._conversations.add(chat_id)
                if self._attach_waiters:
                    waiter = self._attach_waiters.popleft()
                    if not waiter.done():
                        waiter.set_result(chat_id)
            return

        if not isinstance(chat_id, str):
            return

        if event == "delta":
            text = frame.get("text") or ""
            self._buffers[chat_id].append(text)
            self._streaming.add(chat_id)
            self._emit(chat_id, Delta(channel="text", text=text))
            return

        if event == "reasoning_delta":
            # Thinking is streamed but never stored, same as the native backend: it is
            # scaffolding for the answer, not part of the record.
            self._emit(chat_id, Delta(channel="thinking", text=frame.get("text") or ""))
            return

        if event == "reasoning_end":
            return

        if event == "stream_end":
            # The end of a *segment*, not of the turn. `resuming: true` means the agent is
            # about to call a tool and will keep talking, so the text assembled so far is that
            # stage's checkpoint; without it, this was the answer. Treating both as an answer
            # filed every checkpoint as a reply and announced the turn finished once per stage
            # — which the page then rendered as a stream of "finished" bubbles.
            text = "".join(self._buffers.pop(chat_id, []))
            resuming = bool(frame.get("resuming"))
            if text.strip():
                self._last_segment[chat_id] = text
                self._emit(
                    chat_id,
                    Message(role=AgentRole.assistant, content=text, progress=resuming),
                )
            if not resuming:
                self._streaming.discard(chat_id)
                self._emit(chat_id, Status(streaming=False))
            return

        if event == "turn_end":
            # The turn is over: whatever was unfinished is not coming. A segment that never
            # closed (`stream_end` missing because the run failed) is still text the user saw
            # stream in, so it is recorded rather than dropped in the name of tidiness.
            self._streaming.discard(chat_id)
            text = "".join(self._buffers.pop(chat_id, []))
            if text.strip():
                self._emit(chat_id, Message(role=AgentRole.assistant, content=text))
            self._last_segment.pop(chat_id, None)
            self._emit(chat_id, Status(streaming=False))
            return

        if event == "message":
            # `kind` is how nanobot marks a message as *subordinate trace* rather than an
            # answer: its websocket channel sets `kind="progress"` on the stage notes and
            # `kind="tool_hint"` on tool breadcrumbs (`channels/websocket/runtime.py`,
            # `send_projected_message`).
            #
            # A tool hint is a *per-call* breadcrumb — precisely the thing the progress design
            # rejects in favour of one line per stage — so it is dropped rather than promoted
            # to a checkpoint.
            #
            # **With streaming on, the progress frame is a marker, not the text.** The stage
            # note already arrived as `delta … stream_end{resuming}`, so this frame carries the
            # same text (or none at all) purely to say "that segment was a checkpoint". It is
            # matched against the segment just flushed and never stored twice. Text only starts
            # here when the gateway has streaming off, where there were no deltas to assemble.
            text = frame.get("text") or ""
            kind = frame.get("kind")
            if kind == "progress":
                if not text.strip():
                    return
                if self._last_segment.pop(chat_id, None) == text:
                    return
                self._emit(
                    chat_id,
                    Message(role=AgentRole.assistant, content=text, progress=True),
                )
                return
            if text.strip() and kind != "tool_hint":
                self._streaming.discard(chat_id)
                self._emit(chat_id, Message(role=AgentRole.assistant, content=text))
                self._emit(chat_id, Status(streaming=False))
            return

        if event == "file_edit":
            self._emit(
                chat_id,
                Message(
                    role=AgentRole.tool,
                    tool_name="edit",
                    content=_describe_edits(frame.get("edits")),
                ),
            )
            self._emit(chat_id, ToolCall(phase="end", name="edit"))
            return

        if event == "user_message":
            text = frame.get("text") or ""
            pending = self._just_sent.get(chat_id)
            if pending and text in pending:
                # Our own message coming back. Drop the record of it rather than the frame —
                # if the same words are sent twice, the second is a real second message.
                pending.remove(text)
                return
            if text.strip():
                # Genuine cross-channel input: the same conversation continued from Telegram,
                # say. Worth showing, and the reason this event exists at all.
                self._emit(chat_id, Message(role=AgentRole.user, content=text))
            return

        if event in ("session_updated", "turn_model_updated", "runtime_model_updated"):
            model = frame.get("model_name") or frame.get("model")
            self._emit(chat_id, Status(model=model) if model else Status())
            return

        if event == "error":
            self._emit(
                chat_id,
                Notice(notice="nanobot error", level="error", detail=frame.get("detail")),
            )
            return

        logger.debug("ignoring nanobot event %r", event)


BACKEND_NAME = "nanobot"

#: How this reads in the settings panel, in the UI's voice rather than the config's.
BACKEND_LABEL = "nanobot"

#: One line for the panel: what the row is, and that it needs nothing done to it.
BACKEND_BLURB = (
    "Your nanobot gateway's own channel, on loopback. It carries the agent's checkpoints, "
    "tool rows and reasoning; nothing to configure and nothing to restart."
)

#: How this kind opens its door, and what it costs — read by `agent/onboard.py` and rendered
#: into the doc the agent reads. Kept here, beside the protocol code that knows it, for the
#: same reason `BACKEND_BLURB` is: the module that speaks a protocol is the module that can
#: explain how to switch it on, and splitting the two is how they drift (§25).
#:
#: This door is the one *the agent already has open*: nanobot serves its WebUI socket on
#: loopback by default, and `/webui/bootstrap` hands a local caller a one-time token. So the
#: onboarding step is a registration and nothing else — no config edit, no gateway restart,
#: and nothing that an update can overwrite.
BACKEND_ONBOARD = {
    "kind": BACKEND_NAME,
    "summary": (
        "You already serve this door: the gateway's WebSocket channel listens on "
        "127.0.0.1:8765 by default, and `GET /webui/bootstrap` mints a short-lived connection "
        "token for a local caller. Nothing to open and nothing to restart."
    ),
    "cost": "None. No config change, no gateway restart — agenest asks for its own token.",
    "steps": [
        {
            "title": "Check the door (it is on by default)",
            "shell": (
                "curl -sS -o /dev/null -w '%{http_code}\\n' "
                "http://127.0.0.1:8765/webui/bootstrap"
            ),
            "note": (
                "`200` and `401` both mean the channel is listening. `401` means your config "
                "sets `channels.websocket.tokenIssueSecret`: read that value and send it as "
                "the `token` when you register. If the call does not connect at all, your "
                "gateway is on another host or `channels.websocket.enabled` is false — tell "
                "your user, and do not edit the config or restart the gateway to fix it. "
                "A different `host`/`port` in that section is the address to register instead."
            ),
        },
    ],
    "health": BOOTSTRAP_PATH,
    "register": {"kind": BACKEND_NAME, "url": "http://127.0.0.1:8765"},
}


def build(settings, on_event: EventSink) -> NanobotBackend:
    """Construct the nanobot backend.

    Split out from the class so `backends/registry.py` can discover this module without
    importing `Settings` itself, and so the registry's contract is one obvious function.

    The url and token passed here are the *settings* answer, and only a fallback: the door an
    agent registered (`agent/endpoints.py`) is read on every connect, because that is the one
    an agent can open for itself after the service has already started.

    The transcript lives beside the main database rather than inside it: it is this adapter's
    storage, opened by this adapter, with a schema this adapter owns. Sharing the file would
    make every nanobot schema change a migration of the service's own database, which is the
    coupling that moving it out of the core was meant to end.
    """
    transcript = NanobotTranscript(
        Path(settings.db_path).parent / "nanobot-transcript.sqlite3"
    )
    return NanobotBackend(
        url=settings.nanobot_ws_url,
        client_id=settings.nanobot_client_id,
        token=settings.nanobot_token,
        on_event=on_event,
        transcript=transcript,
    )


def new_client_id(prefix: str = "csd-dashboard") -> str:
    """A stable-ish client id for `allowFrom` allow-listing.

    nanobot truncates to 128 chars and auto-generates `anon-…` if omitted, which would make
    it impossible to allow-list this dashboard specifically. A random suffix avoids every
    instance of this service sharing one identity.
    """
    return f"{prefix}-{secrets.token_hex(4)}"


__all__ = ["NanobotBackend", "new_client_id"]
