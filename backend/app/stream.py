"""The SSE broker.

Spec 6.3 asks for the `id:` field so a reconnecting client can resume. The honest version
of that needs a cursor that means something beyond one connection's lifetime, so the
sequence number clients see is the **database event id**, not a per-process counter.
A phone that reconnects after a tunnel drop, a service restart, or an hour in a pocket
passes `?cursor=<last id it rendered>` and the server replays from genuinely after it.

Design decisions worth defending:

- **The buffer holds ids, not copies of history.** A client that was away longer than the
  buffer must re-fetch `/api/sessions/{id}/events`; the stream carries notifications, the
  database is the record (spec 6.2). This is stated in the API contract, not papered over.
- **One instance per process.** The spec forbids Redis in MVP, so there is no shared bus.
  Scaling horizontally would require routing by `session_id`; the db-id cursor already
  survives that, which is why it was chosen over a counter.
- **Asyncio, not threads.** An SSE client is long-lived and idle, so a hundred phone
  listeners cost much less as async tasks than as threadpool workers.
- **Filtered streams still advance the cursor.** A detail page told to listen to one
  session receives the other sessions' ids as comments, so its `cursor` never falls behind
  and needs no special-casing on reconnect.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque

from .schemas import EventOut, HostOut, SessionOut, utcnow

#: How many recent writes are replayed to a client that reconnects without a cursor.
REPLAY_CAPACITY = 2048


class _Frame:
    __slots__ = ("seq", "payload")

    def __init__(self, seq: int, payload: dict) -> None:
        self.seq = seq
        self.payload = payload


def session_frame(session: SessionOut) -> dict:
    """The fields the dashboard needs to re-render a card, and nothing else.

    "Nothing else" means everything the client's `Session` type declares, and no more: the
    list hook replaces its copy of a session with this object, so a field omitted here is a
    field erased from the screen — which is how a card's "what it is doing" line used to
    vanish on the frame that arrived alongside every event. `last_event` in particular is
    load-bearing: it is what distinguishes a session blocked on the user from a busy one,
    and `last_verdict` is what keeps a task verdict visible after the turn boundary that
    follows it.
    """
    return {
        "kind": "session.updated",
        "at": utcnow(),
        "session": {
            "id": session.id,
            "agent_type": session.agent_type,
            "title": session.title,
            "project_name": session.project_name,
            "workspace": session.workspace,
            "host": session.host,
            "status": session.status.value,
            "started_at": session.started_at,
            "last_activity_at": session.last_activity_at,
            "finished_at": session.finished_at,
            "summary": session.summary,
            "exit_code": session.exit_code,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "possibly_stale": session.possibly_stale,
            "last_event": (
                session.last_event.model_dump(mode="json")
                if session.last_event is not None
                else None
            ),
            "last_verdict": (
                session.last_verdict.model_dump(mode="json")
                if session.last_verdict is not None
                else None
            ),
            # A projection, not a stored flag (docs/design.md §42): the frame has to carry it
            # or the list hook's wholesale replacement puts a dismissed row back on the board
            # on the very frame that announced the dismissal.
            "dismissed": session.dismissed,
        },
    }


def event_frame(session_id: str, event: EventOut) -> dict:
    return {
        "kind": "session.event",
        "at": utcnow(),
        "session_id": session_id,
        "event": event.model_dump(mode="json"),
    }


def host_frame(host: HostOut) -> dict:
    """The fields the rail needs to render a connected host.

    Every field the client's `Host` type declares, for the reason spelled out on
    `session_frame`: the hook replaces its copy wholesale, so an omission here is a field
    erased from the screen.
    """
    return {
        "kind": "host.updated",
        "at": utcnow(),
        "host": {
            "id": host.id,
            "label": host.label,
            "agent_type": host.agent_type,
            "connected_at": host.connected_at,
            "last_seen_at": host.last_seen_at,
            "disconnected_at": host.disconnected_at,
            "possibly_stale": host.possibly_stale,
            "wants_access": host.wants_access,
            "access_state": host.access_state,
            "access_target": host.access_target,
            "access_fingerprint": host.access_fingerprint,
        },
    }


class Broker:
    def __init__(self, capacity: int = REPLAY_CAPACITY) -> None:
        self._frames: deque[_Frame] = deque(maxlen=capacity)
        self._subscribers: set[asyncio.Queue] = set()

    # ------------------------------------------------------------------ #
    # write side — called after a transaction has committed
    # ------------------------------------------------------------------ #
    def publish(self, seq: int, payload: dict) -> None:
        """Queue `payload` under database event id `seq` and fan out to subscribers."""
        if seq <= 0:
            return
        frame = _Frame(seq, payload)
        self._frames.append(frame)
        for queue in list(self._subscribers):
            try:
                # `put_nowait` cannot block. A slow client drops a frame, which is safe
                # because its cursor still advances and it backfills from the database.
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------ #
    # read side
    # ------------------------------------------------------------------ #
    def replay_since(self, cursor: int) -> list[_Frame]:
        """Buffered writes with a database id above `cursor`, oldest first."""
        return [f for f in self._frames if f.seq > cursor]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def replay_and_subscribe(self, cursor: int) -> tuple[list[_Frame], asyncio.Queue]:
        """Atomically snapshot `frames > cursor` and start listening.

        The order matters: replaying *first* and subscribing *after* leaves a window in
        which a frame is published but delivered to nobody — the client misses it and, having
        no way to know, never asks for it again. Taking the replay snapshot *after*
        subscribing makes that window disappear, at the cost of possibly re-sending a frame
        the client already has. Re-sends are harmless (the client's cursor skips them);
        missing frames are not.

        No `await` between the two steps: asyncio is single-threaded, so nothing can
        interleave here.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return self.replay_since(cursor), queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    # ------------------------------------------------------------------ #
    # convenience wrappers for the frames the API actually emits
    # ------------------------------------------------------------------ #
    def publish_event(self, event: EventOut) -> None:
        self.publish(event.id, event_frame(event.session_id, event))

    def publish_session(self, session: SessionOut, *, after: int) -> None:
        """Announce a session change *after* the event that caused it.

        Both frames carry the same id — the event's row id. They are two views of one
        write, not two writes, so a client's cursor advances once.
        """
        self.publish(after, session_frame(session))

    def publish_host(self, host: HostOut, *, after: int) -> None:
        """Announce a host, anchored to an event id the client already holds.

        A host has no row in `session_events`, so it cannot mint a cursor id of its own and
        borrowing the newest one is the only alternative to a second sequence space. §4's
        promise — that the `id:` you resume from is a database event id — is what makes a
        cursor survive a service restart, and two numbering spaces would quietly break it.

        This is the same move `patch_session` already makes for a patch that writes no event
        of its own. It costs a replayed frame rather than a lost one, which is safe here
        because a host frame is a whole replacement of the client's copy: replaying it is
        idempotent, and the cursor skips an id it has already rendered.
        """
        self.publish(after, host_frame(host))


_broker: Broker | None = None


def init_broker(capacity: int = REPLAY_CAPACITY) -> Broker:
    global _broker
    _broker = Broker(capacity=capacity)
    return _broker


def get_broker() -> Broker:
    if _broker is None:
        raise RuntimeError("broker is not initialised; call init_broker() at startup")
    return _broker


def sse_message(frame: _Frame, *, include: bool) -> str:
    """Serialise one frame.

    `include=False` renders a comment carrying only the id, so a filtered subscription
    keeps advancing its cursor without receiving a payload it would render.
    """
    if not include:
        return f": skip {frame.seq}\n\n"
    body = json.dumps(frame.payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {frame.seq}\nevent: {frame.payload['kind']}\ndata: {body}\n\n"


# --------------------------------------------------------------------------- #
# the agent stream
# --------------------------------------------------------------------------- #
#: Frames kept for a client that connects mid-run. Shorter than the dashboard's buffer
#: because there is no cursor here: this only has to cover the gap between "the client
#: asked for the stream" and "the client re-fetched the thread", and the client re-fetches
#: on every connect.
AGENT_REPLAY_CAPACITY = 256


class AgentBroker(Broker):
    """The agent's notification channel. Same fan-out, deliberately **no cursor**.

    The dashboard's stream carries `id: <database event id>` so a reconnecting phone resumes
    from genuinely after the last frame it rendered (§4). That works because every dashboard
    frame *is* a database row.

    This stream is not. Its frames are mostly streaming deltas — one per token of the
    reply — and writing a row per token to buy resumability would be absurd for a value that
    is superseded microseconds later, and would grow without bound. So the rule is drawn the
    honest way instead: **deltas are never recorded at all; completed messages are.** A
    client that reconnects re-fetches `/api/agent/threads/{id}` and gets the whole
    conversation from SQLite.

    Consequently these frames are emitted *without* an `id:` field. The sequence number still
    exists internally (the replay buffer indexes on it), but publishing it would invite a
    client to persist a cursor it cannot rely on — an SSE id that looks resumable and isn't
    is worse than none.
    """

    def __init__(self, capacity: int = AGENT_REPLAY_CAPACITY) -> None:
        super().__init__(capacity=capacity)
        self._seq = 0

    def publish_agent(self, payload: dict) -> None:
        """Publish one agent frame under a per-process counter."""
        self._seq += 1
        self.publish(self._seq, payload)


def agent_frame(kind: str, **fields) -> dict:
    """Build a frame with its kind and timestamp filled in."""
    return {"kind": kind, "at": utcnow(), **fields}


def agent_sse_message(frame: _Frame) -> str:
    """Serialise an agent frame. Note the absence of `id:` — see `AgentBroker`."""
    body = json.dumps(frame.payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {frame.payload['kind']}\ndata: {body}\n\n"
