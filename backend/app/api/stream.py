"""`GET /api/events` — the SSE stream.

Wire protocol (spec 6.3):

```
: keep-alive

id: 412
event: session.event
data: {…}

id: 413
event: session.updated
data: {…}
```

Reconnect semantics:

- **No cursor** → replay the whole buffer, then go live. The client may be brand new, so the
  buffer is the cheapest way to avoid an immediate gap.
- **`?cursor=<id>`** → the client persisted the last event id it rendered, so the server
  resumes from genuinely after it. The cursor is a **database event id**, so it stays
  meaningful across service restarts — which a per-connection counter would not.
- **`?session_id=<id>`** → filter to one session. Other sessions' frames are still emitted,
  as `: skip <id>` comments, so a filtered client's cursor never lags and needs no special
  case on reconnect.

The boundary, stated because it is easy to over-promise: **this stream is not history.**
A client that was disconnected longer than the buffer (2048 frames) must re-fetch
`/api/sessions/{id}/events`. SSE carries the notification; SQLite is the record (spec 6.2).

The 15 s heartbeat is a comment line rather than a data frame: intermediate NAT and phone
proxies drop idle connections, and a comment keeps the socket warm at near-zero cost.
"""


import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from . import deps
from ..auth import dashboard_header
from ..stream import Broker, sse_message

router = APIRouter()

HEARTBEAT_SECONDS = 15.0

#: `X-Accel-Buffering` is read by nginx; `no-transform` stops some phone proxies from
#: buffering a stream they think is a normal text response.
STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.get("/api/events", dependencies=[Depends(dashboard_header)])
async def stream_events(
    request: Request,
    db: deps.DbDep,
    broker: deps.BrokerDep,
    session_id: Annotated[str | None, Query(max_length=64)] = None,
    cursor: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """Open the realtime stream. The response headers go out immediately; the generator
    runs replays and then the live loop."""
    return StreamingResponse(
        _event_stream(
            request=request,
            broker=broker,
            session_id=session_id,
            cursor=cursor,
        ),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )


async def _event_stream(
    *,
    request: Request,
    broker: Broker,
    session_id: str | None,
    cursor: int | None,
) -> AsyncIterator[str]:
    # Open with a comment frame. Without it the generator does not yield until the first
    # real event arrives, so response headers are never flushed and the client's `EventSource`
    # sits in CONNECTING with no "is this stream alive?" signal at all — a phone that opens
    # the dashboard during a quiet period gets no connection status to show.
    yield ": stream open\n\n"

    # Replay + subscribe as one step. Subscribing after replaying would drop any frame
    # published in between, and the client has no way to detect a frame it never got.
    replay, queue = broker.replay_and_subscribe(cursor or 0)
    try:
        for frame in replay:
            yield sse_message(frame, include=_matches(frame, session_id))

        while True:
            # A phone that backgrounds the tab still holds the socket open; `is_disconnected`
            # is the only signal that it went away. Polling it also bounds how long we hold a
            # dead connection.
            if await request.is_disconnected():
                break
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield sse_message(frame, include=_matches(frame, session_id))
    finally:
        broker.unsubscribe(queue)


def _matches(frame, session_id: str | None) -> bool:
    if session_id is None:
        return True
    payload = frame.payload
    if payload.get("session_id") == session_id:
        return True
    session = payload.get("session")
    return isinstance(session, dict) and session.get("id") == session_id


__all__ = ["router"]
