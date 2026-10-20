"""Session persistence and the terminal-state rules.

Rules asserted in tests/test_sessions.py:

- **Terminal states are sticky.** A late event or patch cannot move a session out of
  completed/failed/interrupted (spec 6.4, acceptance §12).
- **`unknown` is a wall for automatic transitions, not a downgrade.** An operator who set
  `unknown` said "I do not know"; an automated event must not answer that for them.
  An *explicit* patch is still allowed, and `unknown → running` is allowed (resumed work).
- **Activity moves forward.** `last_activity_at` never goes backward, so a reporter with a
  skewed clock cannot make a session look abandoned.
- **Repeated reports do not accumulate status changes.** Same status + same `event_key`
  inserts nothing; the counter that matters (`heartbeat_count`) increments only on real
  heartbeats.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from ..database import Database
from ..schemas import (
    EventType,
    EventOut,
    SessionCreate,
    SessionDetailOut,
    SessionOut,
    SessionPatch,
    SessionStatus,
    TERMINAL_STATUSES,
    TERMINAL_STATUS_TO_EVENT,
    parse_any_timestamp,
    utcnow,
)

# --------------------------------------------------------------------------- #
# row mapping
# --------------------------------------------------------------------------- #
def _row_to_event(row) -> EventOut:
    payload = json.loads(row["payload_json"]) if row["payload_json"] else None
    return EventOut(
        id=row["id"],
        session_id=row["session_id"],
        event_type=row["event_type"],
        message=row["message"],
        payload=payload if isinstance(payload, dict) else None,
        occurred_at=row["occurred_at"],
        received_at=row["received_at"],
    )


def _row_to_session(row, *, possibly_stale: bool = False, last_event=None) -> SessionOut:
    return SessionOut(
        id=row["id"],
        agent_type=row["agent_type"],
        title=row["title"],
        project_name=row["project_name"],
        workspace=row["workspace"],
        host=row["host"],
        status=row["status"],
        started_at=row["started_at"],
        last_activity_at=row["last_activity_at"],
        finished_at=row["finished_at"],
        summary=row["summary"],
        exit_code=row["exit_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        possibly_stale=possibly_stale,
        last_event=last_event,
    )


def attach_last_events(db: Database, sessions: list[SessionOut]) -> None:
    """Populate `last_event` for a page of sessions in **one** query.

    A correlated subquery per session would be simpler to write and would mean N queries for
    an N-row page — which is exactly the shape a phone's sidebar produces, re-fetched on every
    SSE frame. One grouped query over the ids we already have is the difference between a
    dashboard that feels instant and one that doesn't.

    `heartbeat` and `status_changed` are excluded for the reason documented on
    `get_last_display_event`: a ping is not activity, and a status the dashboard wrote itself
    is not something the user should be shown back as "what it is doing".
    """
    if not sessions:
        return

    ids = [s.id for s in sessions]
    placeholders = ",".join("?" * len(ids))
    rows = db.query(
        f"""
        SELECT * FROM (
            SELECT session_id, MAX(id) AS newest
              FROM session_events
             WHERE session_id IN ({placeholders})
               AND event_type NOT IN ('heartbeat', 'status_changed')
             GROUP BY session_id
        ) AS newest_per_session
        JOIN session_events e ON e.id = newest_per_session.newest
        """,
        tuple(ids),
    )
    by_session = {row["session_id"]: _row_to_event(row) for row in rows}

    for session in sessions:
        session.last_event = by_session.get(session.id)


def _row_to_detail(row, *, possibly_stale: bool = False) -> SessionDetailOut:
    """A session plus its counters. Kept separate from `_row_to_session` because the list
    view has no use for `heartbeat_count` and should not pay to serialise it."""
    return SessionDetailOut(
        **_row_to_session(row, possibly_stale=possibly_stale).model_dump(),
        heartbeat_count=row["heartbeat_count"],
    )


def new_session_id() -> str:
    """Server-generated (spec 6.4); the prefix keeps `journalctl` lines readable."""
    return f"s_{secrets.token_hex(12)}"


def _normalise_ts(value: str | None, fallback: str) -> str:
    parsed = parse_any_timestamp(value) if value else None
    if parsed is None:
        return fallback
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _to_dt(value: str) -> datetime:
    return parse_any_timestamp(value)


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive input is treated as UTC (the storage zone)."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def get_session_row(db: Database, session_id: str):
    return db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))


def list_sessions(
    db: Database,
    *,
    status: SessionStatus | None,
    agent_type: str | None,
    limit: int,
    offset: int,
    stale_after_seconds: int,
) -> tuple[list[SessionOut], int]:
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status.value)
    if agent_type:
        clauses.append("agent_type = ?")
        params.append(agent_type)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    # Running first, then most recently touched. `id` is a stable tiebreaker so paging
    # does not reshuffle while the user scrolls.
    order = " ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, updated_at DESC, id ASC"

    total_row = db.query_one(f"SELECT COUNT(*) AS n FROM sessions{where}", tuple(params))
    total = int(total_row["n"]) if total_row else 0

    rows = db.query(
        f"SELECT * FROM sessions{where}{order} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    sessions = _with_staleness(rows, stale_after_seconds=stale_after_seconds)
    attach_last_events(db, sessions)
    return sessions, total


def get_session(
    db: Database, session_id: str, *, stale_after_seconds: int
) -> SessionOut | None:
    row = get_session_row(db, session_id)
    if row is None:
        return None
    return _with_staleness([row], stale_after_seconds=stale_after_seconds)[0]


def get_session_detail(
    db: Database, session_id: str, *, stale_after_seconds: int
) -> SessionDetailOut | None:
    row = get_session_row(db, session_id)
    if row is None:
        return None
    detail = _row_to_detail(row)
    if stale_after_seconds > 0 and detail.status is SessionStatus.running:
        quiet = (datetime.now(timezone.utc) - _to_dt(detail.last_activity_at)).total_seconds()
        detail.possibly_stale = quiet >= stale_after_seconds
    # Populate last_event here too, not just on the list path — the field is declared on
    # the shared schema, and a detail response that always had it null would be a lie about
    # the shape of the API.
    attach_last_events(db, [detail])
    return detail


def _with_staleness(rows, *, stale_after_seconds: int) -> list[SessionOut]:
    if not rows:
        return []
    sessions = [_row_to_session(r) for r in rows]
    if stale_after_seconds <= 0:
        return sessions
    now = datetime.now(timezone.utc)
    for session in sessions:
        if session.status is not SessionStatus.running:
            continue
        quiet = (now - _to_dt(session.last_activity_at)).total_seconds()
        session.possibly_stale = quiet >= stale_after_seconds
    return sessions


def list_events(
    db: Database, session_id: str, *, limit: int, offset: int
) -> tuple[list[EventOut], int]:
    # Descending timeline: newest first on a phone, so the latest is never below the fold.
    # `id` breaks ties between events sharing a millisecond.
    # `heartbeat` is excluded on purpose — it is a liveness ping, not something the user did,
    # and 40 of them per session would bury the timeline. `GET .../events?include=heartbeat`
    # brings them back for debugging.
    rows = db.query(
        """
        SELECT * FROM session_events
         WHERE session_id = ? AND event_type != 'heartbeat'
         ORDER BY occurred_at DESC, id DESC
         LIMIT ? OFFSET ?
        """,
        (session_id, limit, offset),
    )
    total = count_events(db, session_id)
    return [_row_to_event(r) for r in rows], total


def count_events(db: Database, session_id: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM session_events WHERE session_id = ?", (session_id,)
    )
    return int(row["n"]) if row else 0


def get_last_display_event(db: Database, session_id: str) -> EventOut | None:
    """The newest event a *person* would recognise as activity.

    Excludes two kinds that are bookkeeping rather than work:

    - `heartbeat` — a liveness ping, not something the user did.
    - `status_changed` — this is what the dashboard itself wrote when a session was patched.
      Surfacing it as "what it's doing now" is circular: the UI would report back to itself.
      The status is already on the session; the last real activity is a different question.
    """
    row = db.query_one(
        """
        SELECT * FROM session_events
         WHERE session_id = ?
           AND event_type NOT IN ('heartbeat', 'status_changed')
         ORDER BY received_at DESC, id DESC
         LIMIT 1
        """,
        (session_id,),
    )
    return _row_to_event(row) if row else None


def get_events_after(
    db: Database, session_id: str, *, last_event_id: int, limit: int
) -> list[EventOut]:
    """Events with `id > last_event_id`. Used by SSE reconnect to backfill (spec 6.3)."""
    rows = db.query(
        """
        SELECT * FROM session_events
         WHERE session_id = ? AND id > ?
         ORDER BY id ASC
         LIMIT ?
        """,
        (session_id, last_event_id, limit),
    )
    return [_row_to_event(r) for r in rows]


def get_last_event_row(db: Database, session_id: str):
    """The newest event row, or `None`. Used to anchor SSE frames that carry no event."""
    return db.query_one(
        "SELECT * FROM session_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    )


def latest_event_id(db: Database) -> int:
    """The newest event id in the whole database, or `-1` when there are none yet.

    The anchor for a frame that has no row of its own — a metadata-only patch, or a host
    update. A frame published at an id the client already holds is not lost, it is merely
    not replayed to that client; `-1` is the value for "there is nothing to anchor to", and
    it sorts before every real id so a first-time subscriber still receives the frame.
    """
    row = db.query_one("SELECT MAX(id) AS n FROM session_events")
    return int(row["n"]) if row is not None and row["n"] is not None else -1


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
class NotFoundError(KeyError):
    """No session with that id."""


def insert_event(
    db: Database,
    *,
    session_id: str,
    event_type: EventType,
    message: str,
    payload: dict | None,
    event_key: str | None,
    occurred_at: str,
    received_at: str,
) -> EventOut:
    """Insert one event and return the stored row (as `EventOut`).

    `INSERT OR IGNORE` on the unique partial index makes this idempotent: replaying the
    same `event_key` returns the *original* event rather than adding a second one. That is
    what lets the CLI retry a failed POST without corrupting the timeline (spec 8.3).
    """
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO session_events (
            session_id, event_type, message, payload_json, event_key, occurred_at, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            event_type.value,
            message,
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            if payload is not None
            else None,
            event_key,
            occurred_at,
            received_at,
        ),
    )
    row = (
        db.query_one("SELECT * FROM session_events WHERE id = ?", (cursor.lastrowid,))
        if cursor.lastrowid
        else db.query_one(
            "SELECT * FROM session_events WHERE event_key = ?", (event_key,)
        )
    )
    assert row is not None
    return _row_to_event(row)


def create_session(db: Database, data: SessionCreate) -> tuple[SessionOut, EventOut]:
    now = utcnow()
    started_at = _normalise_ts(data.started_at, now)
    session_id = new_session_id()

    # Creating a session already-terminal is legitimate (finished before the dashboard
    # ever saw it). Recording it as a real transition is what keeps the timeline honest.
    terminal = data.status in TERMINAL_STATUSES
    opening_status = data.status if terminal else SessionStatus.running
    last_activity = now if data.started_at is None else max(now, started_at)

    with db.write():
        db.execute(
            """
            INSERT INTO sessions (
                id, agent_type, title, project_name, workspace, host, status,
                started_at, last_activity_at, finished_at, summary, exit_code,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                data.agent_type,
                data.title,
                data.project_name,
                data.workspace,
                data.host,
                opening_status.value,
                started_at,
                last_activity,
                now if terminal else None,
                None,
                None,
                now,
                now,
            ),
        )
        start_row = insert_event(
            db,
            session_id=session_id,
            event_type=EventType.session_started,
            message=_start_message(data.title, backdated=data.started_at is not None),
            payload=None,
            event_key=None,
            occurred_at=last_activity,
            received_at=now,
        )
        if terminal:
            insert_event(
                db,
                session_id=session_id,
                event_type=TERMINAL_STATUS_TO_EVENT[data.status],
                message=f"Reported after the fact as {data.status.value}",
                payload={"status": data.status.value, "source": "create"},
                event_key=None,
                occurred_at=last_activity,
                received_at=now,
            )

    row = get_session_row(db, session_id)
    assert row is not None
    return _row_to_session(row), start_row


def _start_message(title: str | None, *, backdated: bool) -> str:
    text = "Session started"
    if title:
        text = f"Session started: {title}"
    return text + (" (reported late)" if backdated else "")


def patch_session(db: Database, session_id: str, data: SessionPatch):
    """Apply metadata/status fields.

    Returns `(session, changed)`. `changed=False` means "nothing was applied", which the
    caller must not treat as a failure — a no-op patch is a legitimate de-duplicated retry.
    """
    row = get_session_row(db, session_id)
    if row is None:
        return None, False

    current = SessionStatus(row["status"])
    now = utcnow()

    sets: list[str] = []
    params: list[object] = []
    transition: tuple[SessionStatus, SessionStatus] | None = None

    if data.status is not None and data.status is not current:
        if current in TERMINAL_STATUSES:
            # Published verdicts are not re-opened by a late request.
            return _row_to_session(row), False
        if (
            current is SessionStatus.unknown
            and data.status not in TERMINAL_STATUSES
            and data.status is not SessionStatus.running
        ):
            # `unknown` → anything other than a terminal state or a resume is not an
            # automatic transition, so it is refused rather than guessed.
            return _row_to_session(row), False
        transition = (current, data.status)
        sets.extend(["status = ?", "finished_at = ?"])
        params.extend(
            [data.status.value, now if data.status in TERMINAL_STATUSES else row["finished_at"]]
        )

    if data.title is not None:
        sets.append("title = ?")
        params.append(data.title)
    if data.project_name is not None:
        sets.append("project_name = ?")
        params.append(data.project_name)
    if data.summary is not None:
        sets.append("summary = ?")
        params.append(data.summary)
    if data.exit_code is not None:
        sets.append("exit_code = ?")
        params.append(data.exit_code)

    if not sets:
        return _row_to_session(row), False

    # Activity time only ever advances; monotonic updates keep a session from looking
    # abandoned because a different machine's clock ran behind (CPB §8 / spec 8).
    last_activity = max(row["last_activity_at"], now)
    sets.append("updated_at = ?")
    params.append(now)
    sets.append("last_activity_at = ?")
    params.append(last_activity)

    with db.write():
        db.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", (*params, session_id)
        )
        if transition is not None:
            _insert_event(
                db,
                session_id=session_id,
                event_type=EventType.status_changed,
                message=f"Status changed to {transition[1].value}",
                payload={"from": transition[0].value, "to": transition[1].value},
                event_key=None,
                occurred_at=now,
                received_at=now,
            )

    row = get_session_row(db, session_id)
    assert row is not None
    return _row_to_session(row), True


def _insert_event(
    db: Database,
    *,
    session_id: str,
    event_type: EventType,
    message: str,
    payload: dict | None,
    event_key: str | None,
    occurred_at: str,
    received_at: str,
):
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO session_events (
            session_id, event_type, message, payload_json, event_key, occurred_at, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            event_type.value,
            message,
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            if payload is not None
            else None,
            event_key,
            occurred_at,
            received_at,
        ),
    )
    if cursor.rowcount == 0:
        # Duplicate `event_key`: a retry of an already-recorded event.
        return db.query_one(
            "SELECT * FROM session_events WHERE event_key = ?", (event_key,)
        )
    return db.query_one(
        "SELECT * FROM session_events WHERE id = ?", (cursor.lastrowid,)
    )


__all__ = [
    "NotFoundError",
    "attach_last_events",
    "count_events",
    "create_session",
    "get_events_after",
    "get_last_display_event",
    "get_session",
    "get_session_detail",
    "get_session_row",
    "insert_event",
    "latest_event_id",
    "list_events",
    "list_sessions",
    "new_session_id",
    "parse_ts",
    "patch_session",
]
