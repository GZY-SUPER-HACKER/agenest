"""A scripted stand-in for a nanobot gateway, for tests that must not need one installed.

Two real sockets in one server: the WebSocket channel the adapter speaks, and the
`/webui/bootstrap` HTTP route it mints a connection token from. Both are real because the
things worth pinning are the handshake, the token round trip and the framing — a mock of the
client library would pin none of them.

The frames follow `nanobot/channels/websocket/runtime.py` as of nanobot 0.3.5, which matters
in two places the older stub got wrong:

- a stage note is **streamed** (`delta` … `stream_end` with `resuming: true`) and then
  followed by a `message` frame with `kind="progress"` that carries **no text**. The frame is a
  marker; the text already arrived as deltas.
- `turn_end` is what says the turn is over. `stream_end` without `resuming` closes the last
  segment, and a client that reads it as the end of the turn marks a checkpoint as an answer.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

#: What the stub's final answer assembles to, from its deltas.
ASSEMBLED = "Hello there, friend."

#: What its stage note assembles to, from its deltas.
CHECKPOINT = "Working on it. "


class StubNanobot:
    def __init__(self, *, secret: str | None = None, require_token: bool = False) -> None:
        #: When set, `/webui/bootstrap` only answers a caller holding it.
        self.secret = secret
        #: When true, the websocket handshake only accepts a token this stub issued.
        self.require_token = require_token
        self.received: list[dict] = []
        self.bootstrap_calls = 0
        self.bootstrap_auth: list[str | None] = []
        self.issued: set[str] = set()
        self.port: int = 0
        self._port: concurrent.futures.Future[int] = concurrent.futures.Future()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._counter = 0
        self._run_cross_channel = False
        self._conns: set = set()

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

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/"

    def force_disconnect(self) -> None:
        """Close every live socket from the server side, as a gateway restart would."""
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
        async with websockets.serve(
            self._handler, "127.0.0.1", 0, process_request=self._process_request
        ) as server:
            self._port.set_result(server.sockets[0].getsockname()[1])
            while not self._stop.is_set():
                await asyncio.sleep(0.02)

    # -- HTTP: the token door ------------------------------------------- #
    def _process_request(self, connection, request) -> Response | None:
        path = urlsplit(request.path)
        if path.path != "/webui/bootstrap":
            if self.require_token:
                supplied = parse_qs(path.query).get("token", [None])[0]
                if supplied not in self.issued:
                    return Response(
                        401, "Unauthorized", Headers(), b"Unauthorized"
                    )
            return None

        self.bootstrap_calls += 1
        authorization = request.headers.get("Authorization")
        self.bootstrap_auth.append(authorization)
        if self.secret and authorization != f"Bearer {self.secret}":
            return Response(401, "Unauthorized", Headers(), b"Unauthorized")
        token = f"nbwt_stub_{self.bootstrap_calls}"
        self.issued.add(token)
        body = json.dumps(
            {
                "token": token,
                "ws_url": self.ws_url,
                "expires_in": 300,
                # Deliberately present: the adapter must ignore it rather than hold a
                # credential for the gateway's whole HTTP API.
                "api_token": "nbwt_stub_api_token",
            }
        ).encode()
        return Response(
            200,
            "OK",
            Headers([("Content-Type", "application/json")]),
            body,
        )

    # -- the websocket channel ------------------------------------------ #
    async def _handler(self, ws) -> None:
        self._conns.add(ws)
        await ws.send(
            json.dumps({"event": "ready", "chat_id": "default-chat", "client_id": "stub"})
        )
        try:
            async for raw in ws:
                frame = json.loads(raw)
                self.received.append(frame)
                kind = frame.get("type")
                if kind == "new_chat":
                    self._counter += 1
                    await ws.send(
                        json.dumps({"event": "attached", "chat_id": f"chat-{self._counter}"})
                    )
                elif kind == "attach":
                    await ws.send(
                        json.dumps({"event": "attached", "chat_id": frame.get("chat_id")})
                    )
                elif kind == "message":
                    await self._turn(ws, frame.get("chat_id"), frame.get("content") or "")
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._conns.discard(ws)

    async def _turn(self, ws, chat_id: str, content: str) -> None:
        """One scripted assistant turn, shaped like a streaming nanobot 0.3.5 run."""
        if content.startswith("/"):
            # In-chat commands produce no assistant turn at all.
            return

        if "progress" in content.lower():
            # A stage note: streamed, then a *resuming* segment end, then a textless marker.
            for chunk in ("Working ", "on it. "):
                await ws.send(
                    json.dumps(
                        {"event": "delta", "chat_id": chat_id, "text": chunk, "stream_id": "s1"}
                    )
                )
            await ws.send(
                json.dumps(
                    {
                        "event": "stream_end",
                        "chat_id": chat_id,
                        "stream_id": "s1",
                        "resuming": True,
                    }
                )
            )
            await ws.send(
                json.dumps(
                    {"event": "message", "chat_id": chat_id, "text": "", "kind": "progress"}
                )
            )
            # A per-call breadcrumb, which is not a checkpoint and must be dropped.
            await ws.send(
                json.dumps(
                    {
                        "event": "message",
                        "chat_id": chat_id,
                        "text": "editing a file",
                        "kind": "tool_hint",
                    }
                )
            )

        # Thinking arrives in its own channel and is never stored.
        await ws.send(
            json.dumps(
                {"event": "reasoning_delta", "chat_id": chat_id, "text": "hmm", "stream_id": "r1"}
            )
        )
        await ws.send(
            json.dumps({"event": "reasoning_end", "chat_id": chat_id, "stream_id": "r1"})
        )

        # A file edit, which is how tool activity reaches a websocket client.
        await ws.send(
            json.dumps(
                {
                    "event": "file_edit",
                    "chat_id": chat_id,
                    "edits": [{"path": "src/thing.py", "added": 3, "removed": 1}],
                }
            )
        )

        # The answer: deltas, a *non*-resuming segment end, and the turn's own end.
        for chunk in ("Hello ", "there, ", "friend."):
            await ws.send(
                json.dumps(
                    {"event": "delta", "chat_id": chat_id, "text": chunk, "stream_id": "s2"}
                )
            )
        await ws.send(
            json.dumps({"event": "stream_end", "chat_id": chat_id, "stream_id": "s2"})
        )
        await ws.send(json.dumps({"event": "turn_end", "chat_id": chat_id}))

        # The echo of what we just sent: must not become a second user message.
        await ws.send(
            json.dumps({"event": "user_message", "chat_id": chat_id, "text": content})
        )

        if self._run_cross_channel:
            await ws.send(
                json.dumps(
                    {"event": "user_message", "chat_id": chat_id, "text": "sent from telegram"}
                )
            )


__all__ = ["ASSEMBLED", "CHECKPOINT", "StubNanobot"]
