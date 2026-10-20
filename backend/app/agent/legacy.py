"""One-time repair of transcripts that predate per-agent storage.

The service used to keep every conversation in a single `agent_messages` table. That table is
gone, for the reasons in docs/design.md §18: it was the lowest common denominator across agents
and lost the tool arguments, which is what memory consolidation reads. Two things replaced it —
`native` reads Pi's session file, `nanobot` keeps its own table inside its adapter — and neither
can see the old rows.

There is a second, quieter casualty. Before `native` passed `--session-id`, Pi named its session
files after a UUID of its own, so the files a thread's conversation was written to cannot be
found from the thread's id. The conversation is on disk; it is simply not addressable. Those
threads already exist on any upgraded deployment and their page shows an empty conversation.

This module runs once, before the agent page is served:

- Every legacy Pi session file that can be attributed to a native thread is recorded in a
  mapping file beside the sessions (`pi-sessions/.legacy/<conversation id>.json`). It is a
  mapping rather than a rename because several sessions can belong to one thread — every service
  restart used to start a new automatically named file — and merging them would break the parent
  chains Pi resumes from. The files stay read-only; only the reader learns about them.
- nanobot's old rows are copied into its adapter's own table, keyed by `chat_id`, preserving
  their timestamps.
- Only after both steps is `agent_messages` dropped.

**Attribution is deliberately conservative.** A file is considered only if its header `cwd` is
the configured workspace and its start time falls inside the thread's lifetime; while the old
rows are still present, a file must also contain a user message within a few seconds of one of
that thread's rows. A guess that put one conversation under another thread's title would be
worse than leaving it unread, so ambiguity leaves the file alone and the thread empty.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..database import Database
from ..schemas import AgentRole, utcnow
from .backends.base import Message

logger = logging.getLogger("csd.agent.legacy")

#: Where the read-only mapping lives, beside the sessions it describes. Keeping it here rather
#: than in `meta` makes it travel with the session directory, and keeps the native backend
#: from needing a database handle.
LEGACY_DIR = ".legacy"

#: The one-shot marker. Its presence means the table was dropped and the mapping written.
LEGACY_DONE_KEY = "migrated.legacy_transcripts"

#: The dead table. Interpolated only from this constant, never from user input.
LEGACY_TABLE = "agent_messages"

#: A session file starts a second or two after the thread row: the row is written when the page
#: opens, and Pi starts with the first prompt.
_START_SKEW_SECONDS = 10.0

#: The old rows recorded a user message when Pi echoed it, which can land a second or two after
#: the prompt itself. Five seconds matched every file in the upgrade this was written for.
_MESSAGE_MATCH_SECONDS = 5.0


# --------------------------------------------------------------------------- #
# the mapping, read by the native backend
# --------------------------------------------------------------------------- #
def legacy_mapping_path(session_dir: Path, conversation_id: str) -> Path:
    return session_dir / LEGACY_DIR / f"{conversation_id}.json"


def read_legacy_sessions(session_dir: Path | None, conversation_id: str) -> list[Path]:
    """Session files recorded for a conversation before `--session-id`, oldest first.

    The mapping stores filenames rather than absolute paths so the session directory can be
    moved or restored from a backup; a file listed but missing is skipped, because a mapping
    that outlived its file is not an error the page should show.
    """
    if session_dir is None:
        return []
    path = legacy_mapping_path(session_dir, conversation_id)
    try:
        names = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read the legacy session map %s: %s", path, exc)
        return []
    if not isinstance(names, list):
        return []
    out: list[Path] = []
    for name in names:
        if not isinstance(name, str):
            continue
        candidate = session_dir / name
        if candidate.is_file():
            out.append(candidate)
    return out


# --------------------------------------------------------------------------- #
# the one-shot migration
# --------------------------------------------------------------------------- #
def migrate_legacy_transcripts(
    db: Database,
    settings: Settings,
    backends: dict[str, Any],
) -> None:
    """Recover what the old shared table held, then drop it. Never raises.

    A failure leaves `agent_messages` in place and does not write the marker, so the next start
    retries. Leaving a dead table is the harmless outcome; deleting one whose contents were not
    carried anywhere is not.
    """
    if db.query_one("SELECT 1 FROM meta WHERE key = ?", (LEGACY_DONE_KEY,)) is not None:
        return
    if not _has_table(db, LEGACY_TABLE):
        # A deployment that never had the old schema. Mark it so every start does not repeat
        # the same `sqlite_master` lookup.
        _mark_done(db)
        return

    unhandled = _rows_this_migration_cannot_restore(db)
    if unhandled:
        logger.warning(
            "%s has %d row(s) this migration cannot restore (another backend, a missing "
            "thread, or a nanobot conversation that already has new rows); keeping the table",
            LEGACY_TABLE,
            unhandled,
        )
        return

    try:
        mapped = _map_native_sessions(db, settings)
        imported, deferred = _import_nanobot_rows(db, backends)
    except Exception:
        logger.exception(
            "legacy transcript migration failed; %s was kept and will be retried",
            LEGACY_TABLE,
        )
        return

    if deferred:
        # Copying rows into a conversation that already has some would either duplicate its
        # beginning or put the old rows after the new ones. Neither is a transcript; keep the
        # table so nothing is silently dropped, and let a human decide.
        logger.warning(
            "%d nanobot conversation(s) already have rows in the new transcript; keeping %s",
            deferred,
            LEGACY_TABLE,
        )
        return

    with db.write() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {LEGACY_TABLE}")
    _mark_done(db)
    logger.info(
        "legacy transcripts migrated: %d native session file(s) mapped, %d nanobot row(s) copied",
        mapped,
        imported,
    )


def _has_table(db: Database, name: str) -> bool:
    return (
        db.query_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        )
        is not None
    )


def _rows_this_migration_cannot_restore(db: Database) -> int:
    """Rows with nowhere to go: another backend, or a thread that no longer exists.

    The old table predates the gateway adapters, so this should be zero. It is checked rather
    than assumed because the alternative is deleting someone's only copy of a conversation to
    tidy up a table.
    """
    row = db.query_one(
        """
        SELECT COUNT(*) AS n
        FROM agent_messages m
        LEFT JOIN agent_threads t ON t.id = m.thread_id
        WHERE t.id IS NULL
           OR t.backend NOT IN ('native', 'nanobot')
           OR t.backend_conversation_id IS NULL
        """
    )
    return int(row["n"]) if row is not None else 0


def _mark_done(db: Database) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (LEGACY_DONE_KEY, utcnow()),
        )


def _map_native_sessions(db: Database, settings: Settings) -> int:
    """Write one mapping file per native thread whose old sessions can be identified."""
    session_dir = settings.pi_session_dir
    workspace = settings.pi_workspace
    if session_dir is None or workspace is None or not session_dir.is_dir():
        return 0

    threads = db.query(
        """
        SELECT id, backend_conversation_id, created_at, updated_at
        FROM agent_threads
        WHERE backend = 'native' AND backend_conversation_id IS NOT NULL
        """
    )
    if not threads:
        return 0

    # Files named after an id we already know belong to the current scheme. They are not
    # candidates, and mapping one to a thread would be a rename in disguise.
    known_ids = {
        row["backend_conversation_id"]
        for row in db.query(
            "SELECT backend_conversation_id FROM agent_threads "
            "WHERE backend_conversation_id IS NOT NULL"
        )
    }
    candidates = [
        path
        for path in sorted(session_dir.glob("*.jsonl"))
        if (header := _session_header(path)) is not None
        and header.get("id") not in known_ids
    ]
    if not candidates:
        return 0

    workspace_key = _path_key(str(workspace))
    used: set[Path] = set()
    mapped = 0

    for thread in threads:
        start = _instant(thread["created_at"])
        if start is None:
            continue
        end = _instant(thread["updated_at"]) or start
        if end < start:
            end = start

        rows = db.query(
            "SELECT created_at FROM agent_messages WHERE thread_id = ? ORDER BY id",
            (thread["id"],),
        )
        row_times = [t for t in (_instant(row["created_at"]) for row in rows) if t is not None]

        matched: list[tuple[float, Path]] = []
        for path in candidates:
            if path in used:
                continue
            header = _session_header(path)
            if header is None:
                continue
            if _path_key(header.get("cwd")) != workspace_key:
                continue
            started = _instant(header.get("timestamp"))
            if started is None:
                continue
            begin = start.timestamp() - _START_SKEW_SECONDS
            finish = end.timestamp() + _START_SKEW_SECONDS
            if not (begin <= started.timestamp() <= finish):
                continue
            # While the old rows exist they are the strongest evidence available: a file that
            # contains none of this thread's user messages is not this thread's file.
            if row_times and not _contains_user_message_near(path, row_times):
                continue
            matched.append((started.timestamp(), path))

        if not matched:
            continue

        matched.sort()
        conversation_id = thread["backend_conversation_id"]
        mapping = legacy_mapping_path(session_dir, conversation_id)
        mapping.parent.mkdir(parents=True, exist_ok=True)
        mapping.write_text(
            json.dumps([path.name for _, path in matched], indent=2),
            encoding="utf-8",
        )
        used.update(path for _, path in matched)
        mapped += len(matched)
        logger.info(
            "mapped %d legacy session file(s) to conversation %s",
            len(matched),
            conversation_id,
        )

    return mapped


def _import_nanobot_rows(db: Database, backends: dict[str, Any]) -> tuple[int, int]:
    """Copy the old rows into nanobot's own transcript, keyed by its `chat_id`.

    Returns `(copied, deferred)`. A deferred conversation is one that already has rows in the
    new table: the old rows cannot be merged without either duplicating the beginning or placing
    them after the new ones, so the caller keeps the old table rather than choose.
    """
    backend = backends.get("nanobot")
    transcript = getattr(backend, "_transcript", None)

    threads = db.query(
        """
        SELECT id, backend_conversation_id
        FROM agent_threads
        WHERE backend = 'nanobot' AND backend_conversation_id IS NOT NULL
        """
    )
    if transcript is None:
        # The adapter could not be constructed (a broken install). Its rows have nowhere to go,
        # so defer rather than drop them.
        deferred = sum(
            1
            for thread in threads
            if db.query_one(
                "SELECT 1 FROM agent_messages WHERE thread_id = ? LIMIT 1", (thread["id"],)
            )
            is not None
        )
        return 0, deferred

    copied = 0
    deferred = 0
    for thread in threads:
        chat_id = thread["backend_conversation_id"]
        # A thread that already has rows in the new table was continued after the change; its
        # old rows are earlier than them, not an extension of them.
        if transcript.messages(chat_id):
            deferred += 1
            continue
        rows = db.query(
            """
            SELECT role, content, tool_name, tool_call_id, is_error, created_at
            FROM agent_messages WHERE thread_id = ? ORDER BY id
            """,
            (thread["id"],),
        )
        for row in rows:
            try:
                role = AgentRole(row["role"])
            except ValueError:
                continue

            transcript.record(
                chat_id,
                Message(
                    role=role,
                    content=row["content"],
                    tool_name=row["tool_name"],
                    tool_call_id=row["tool_call_id"],
                    is_error=bool(row["is_error"]),
                    created_at=row["created_at"],
                ),
            )
            copied += 1
    return copied, deferred


# --------------------------------------------------------------------------- #
# reading Pi's files just enough to identify them
# --------------------------------------------------------------------------- #
def _session_header(path: Path) -> dict[str, Any] | None:
    """The `session` header, or `None` when this is not a Pi session file."""
    try:
        with path.open("rb") as handle:
            first = handle.readline()
    except OSError:
        return None
    try:
        header = json.loads(first.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return header if isinstance(header, dict) and header.get("type") == "session" else None


def _contains_user_message_near(path: Path, row_times: list[datetime]) -> bool:
    """Whether any user message in the file lands within the match window of an old row."""
    try:
        with path.open("rb") as handle:
            raw = handle.read()
    except OSError:
        return False
    for chunk in raw.split(b"\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            entry = json.loads(chunk.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        stamp = _instant(entry.get("timestamp") or message.get("timestamp"))
        if stamp is None:
            continue
        if any(abs(stamp.timestamp() - row.timestamp()) <= _MESSAGE_MATCH_SECONDS for row in row_times):
            return True
    return False


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _path_key(value: Any) -> str:
    """A comparison key for paths: case- and separator-insensitive, as Windows is."""
    if not isinstance(value, str) or not value:
        return ""
    return os.path.normcase(os.path.normpath(value))
