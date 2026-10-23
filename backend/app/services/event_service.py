"""Event ingestion and the status rules that follow from it.

Order of operations matters and is asserted in tests/test_events.py:

1. Verify the session exists. An event naming an unknown session is a client bug;
   auto-creating the session would invent data (spec 6.2).
2. Persist the event, idempotently (`event_key`).
3. Apply session side effects — status transition, `last_activity_at`, heartbeat count.
4. Publish to SSE. The caller does this only after the transaction commits, so a client is
   never told about something that could still be rolled back (spec 6.2).

What this module deliberately does **not** do: infer completion from event count, elapsed
time, or terminal-output parsing (spec 1.3, 4.2, 5.3). Status changes come from reports.

Two rules that are easy to get wrong, and why:

- **A terminal status is sticky and first-write-wins.** If `session_failed` arrives and then
  a duplicate `session_completed`, the session stays `failed`. Letting the later arrival win
  would make the final verdict depend on network ordering.
- **`last_activity_at` never goes backward.** A session that reports a heartbeat after a
  quiet period must not reset `updated_at`, or it would jump to the top of "recently
  active" and mask sessions that actually did something.
"""

from __future__ import annotations




from ..schemas import (
    EventCreate,
    EventOut,
    EventType,
    SessionStatus,
    TERMINAL_EVENTS,
    TERMINAL_STATUSES,
    cap_string,
    utcnow,
)
from . import session_service as ss


class UnknownSessionError(KeyError):
    """The event references a session the server has never seen."""


class IngestResult:
    __slots__ = ("event", "status", "session_updated", "duplicate")

    def __init__(
        self,
        *,
        event: EventOut,
        status: SessionStatus,
        session_updated: bool,
        duplicate: bool,
    ) -> None:
        self.event = event
        self.status = status
        self.session_updated = session_updated
        self.duplicate = duplicate


def ingest_event(
    db, session_id: str, data: EventCreate, *, now: str | None = None
) -> IngestResult:
    received_at = now or utcnow()
    occurred_at = normalise_occurred(data.occurred_at, fallback=received_at)

    with db.write():
        # Read inside the transaction, after `BEGIN IMMEDIATE` has the write lock. Reading
        # before it is how a session completed by a concurrent request was written back as
        # `running`: the row this code held was the pre-completion snapshot (design.md §35).
        row = ss.get_session_row(db, session_id)
        if row is None:
            raise UnknownSessionError(session_id)

        existing = (
            db.query_one("SELECT * FROM session_events WHERE event_key = ?", (data.event_key,))
            if data.event_key
            else None
        )

        event = ss.insert_event(
            db,
            session_id=session_id,
            event_type=data.event_type,
            message=data.message,
            payload=sanitise_payload(data.payload),
            event_key=data.event_key,
            occurred_at=occurred_at,
            received_at=received_at,
        )
        duplicate = existing is not None and existing["id"] == event.id

        if duplicate:
            # Already recorded: apply no side effects at all. Re-applying them would
            # double-count heartbeats or re-emit a status change (spec 6.4).
            return IngestResult(
                event=event,
                status=SessionStatus(row["status"]),
                session_updated=False,
                duplicate=True,
            )

        status, session_updated = _apply_session_effects(
            db,
            session_id=session_id,
            row=row,
            event_type=data.event_type,
            received_at=received_at,
        )

    return IngestResult(
        event=event,
        status=status,
        session_updated=session_updated,
        duplicate=False,
    )


def _apply_session_effects(
    db,
    *,
    session_id: str,
    row,
    event_type: EventType,
    received_at: str,
) -> tuple[SessionStatus, bool]:
    current = SessionStatus(row["status"])
    sets: list[str] = []
    params: list[object] = []
    status = current
    changed_status = False

    terminal_target = TERMINAL_EVENTS.get(event_type)

    if terminal_target is not None and current not in TERMINAL_STATUSES:
        status = terminal_target
        changed_status = True
        sets.extend(["status = ?", "finished_at = ?"])
        params.extend([status.value, received_at])
    # Otherwise `status` is absent from the UPDATE on purpose. Writing `current` back looks
    # like a no-op, but it restates a value read before this transaction — and when that value
    # was already stale it reopened a completed session (docs/design.md §35). An event either
    # moves the session to a terminal state or does not write the status at all.

    if event_type is EventType.heartbeat:
        # Renews the *quiet* clock only. `updated_at` stays put, so a session that is
        # merely alive does not outrank one that actually reported work (see module docstring).
        sets.append("heartbeat_count = heartbeat_count + 1")
        sets.append("last_activity_at = ?")
        params.append(max(row["last_activity_at"], received_at))
    elif current is SessionStatus.running:
        sets.append("last_activity_at = ?")
        params.append(max(row["last_activity_at"], received_at))
        sets.append("updated_at = ?")
        params.append(received_at)
    else:
        sets.append("last_activity_at = ?")
        params.append(max(row["last_activity_at"], received_at))

    sets.append("updated_at = ?")
    params.append(received_at)

    db.execute(
        f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", (*params, session_id)
    )
    return status, changed_status


def sanitise_payload(payload: dict | None) -> dict | None:
    """Cap strings and drop exotic shapes instead of dropping the whole event.

    A `progress` line must survive a reporter that pasted a stack trace or a long path;
    unbounded payloads are what fills the disk (spec 8.9).
    """
    if not payload:
        return None
    out: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            out[key] = cap_string(value)
        elif isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, list):
            out[key] = [
                cap_string(v) if isinstance(v, str) else v for v in value[:100]
            ]
    return out or None


def normalise_occurred(value: str | None, *, fallback: str) -> str:
    """Store `occurred_at` in the server's canonical form; fall back if unparseable.

    The reporter's clock is recorded, not trusted: only `received_at` orders "what is the
    latest event" (spec 4.3). A reporter with a badly skewed clock therefore cannot push a
    session to the top of the list, because list ordering uses `updated_at`/`received_at`.
    """
    if not value:
        return fallback
    try:
        parsed = ss.parse_ts(value)
    except ValueError:
        return fallback
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = ["IngestResult", "UnknownSessionError", "ingest_event", "sanitise_payload"]
