"""Thread metadata for the agent.

**No conversation content lives here.** That is the rule this module now exists to keep, and it
replaced the opposite one. The transcript belongs to the agent — Pi writes a session file and
answers `get_entries` for it — so a second copy in this service would be a second answer to
"what was said", maintained by different code, drifting.

What is left is the part that is genuinely ours: *which* conversations exist, which agent each
belongs to, and what to call it. `agent_threads` is metadata about a conversation, not the
conversation, and it is the one thing no agent can tell us because it is our bookkeeping.

An agent that keeps no readable transcript (nanobot) stores its own, inside its own adapter. That
is the adapter's storage and the adapter's schema; nothing in this module knows about it.
"""

from __future__ import annotations

import secrets
import threading

from ..database import Database
from ..schemas import AgentThreadOut, utcnow


def new_thread_id() -> str:
    """Server-generated, like a session id. The `t_` prefix keeps the two apart in logs."""
    return f"t_{secrets.token_hex(12)}"


def _row_to_thread(row, *, running: bool = False, streaming: bool = False) -> AgentThreadOut:
    return AgentThreadOut(
        id=row["id"],
        title=row["title"],
        backend=row["backend"],
        conversation_id=row["backend_conversation_id"],
        provider=row["provider"],
        model=row["model"],
        workdir=row["workdir"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        running=running,
        streaming=streaming,
    )


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
def create_thread(
    db: Database,
    *,
    title: str | None = None,
    backend: str = "nanobot",
    provider: str | None = None,
    model: str | None = None,
    workdir: str | None = None,
) -> AgentThreadOut:
    now = utcnow()
    thread_id = new_thread_id()
    with db.write():
        db.execute(
            """
            INSERT INTO agent_threads
                (id, title, backend, backend_conversation_id, provider, model, workdir,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                title or "New conversation",
                backend,
                None,
                provider,
                model,
                workdir,
                now,
                now,
            ),
        )
    row = db.query_one("SELECT * FROM agent_threads WHERE id = ?", (thread_id,))
    assert row is not None
    return _row_to_thread(row)


def touch_thread(db: Database, thread_id: str) -> None:
    """Move a thread to the top of the list.

    Called when a prompt is sent rather than when a message is stored, because nothing is stored
    any more. `updated_at` is what orders the thread list, so it has to move on the event the user
    would call "activity" — and sending a prompt is that event.
    """
    with db.write():
        db.execute(
            "UPDATE agent_threads SET updated_at = ? WHERE id = ?", (utcnow(), thread_id)
        )


def set_thread_model(
    db: Database, thread_id: str, *, provider: str | None, model: str | None
) -> None:
    """Record the model actually in use. Called after Pi reports it, not from config.

    The distinction matters: Pi resolves `--model sonnet:high` to a concrete model and can
    fall back, so what was asked for and what is running are not always the same string.
    """
    with db.write():
        db.execute(
            "UPDATE agent_threads SET provider = ?, model = ? WHERE id = ?",
            (provider, model, thread_id),
        )


def set_thread_conversation(db: Database, thread_id: str, conversation_id: str) -> None:
    """Record the backend's own id for this conversation.

    Stored but never used as our primary key. The backend mints it (nanobot's `chat_id`), and
    a schema whose identity depends on someone else's id generation cannot be reasoned about
    locally — so this is a foreign reference, not an identity.
    """
    with db.write():
        db.execute(
            "UPDATE agent_threads SET backend_conversation_id = ? WHERE id = ?",
            (conversation_id, thread_id),
        )


def rename_thread(db: Database, thread_id: str, title: str) -> None:
    """Used once, to replace the placeholder title with the first thing the user asked."""
    with db.write():
        db.execute(
            "UPDATE agent_threads SET title = ? WHERE id = ?", (title[:200], thread_id)
        )


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def get_thread_row(db: Database, thread_id: str):
    return db.query_one("SELECT * FROM agent_threads WHERE id = ?", (thread_id,))


def get_thread(db: Database, thread_id: str) -> AgentThreadOut | None:
    row = db.query_one("SELECT * FROM agent_threads WHERE id = ?", (thread_id,))
    return _row_to_thread(row) if row else None


def latest_thread(db: Database, *, backend: str | None = None) -> AgentThreadOut | None:
    """The conversation this deployment is currently on, or None on a fresh install.

    There is exactly one session in the product (see docs/design.md §18), so "which thread"
    is a question the API answers once rather than something the client chooses. Ordering by
    `updated_at` is what makes an existing deployment keep the conversation it was last
    using instead of silently starting over.

    `backend` filters to one agent's conversations. Switching agents does not re-point an
    existing thread — §17 — so "the session" is scoped to whichever agent is now active.
    """
    if backend is None:
        row = db.query_one(
            "SELECT * FROM agent_threads ORDER BY updated_at DESC, id ASC LIMIT 1"
        )
    else:
        row = db.query_one(
            """
            SELECT * FROM agent_threads
             WHERE backend = ?
             ORDER BY updated_at DESC, id ASC
             LIMIT 1
            """,
            (backend,),
        )
    return _row_to_thread(row) if row else None


#: Serialises `get_or_create_session` below. See its docstring for why a lock is the right tool
#: here rather than something cleverer.
_session_lock = threading.Lock()


def get_or_create_session(db: Database, *, backend: str) -> AgentThreadOut:
    """The one conversation for `backend`, created if this is its first use.

    **Serialised, not merely checked.** A plain "read it, insert if missing" has a window
    between the two, and two clients arriving inside it both read None and both insert — which
    is precisely the second conversation this product does not have. That is not hypothetical:
    the page's own boot fired two overlapping calls the first time it ran (React's StrictMode
    double-mounts effects in development) and produced two rows.

    A lock rather than `INSERT ... WHERE NOT EXISTS` because the connection is shared with
    `check_same_thread=False`, so two threads driving it at once is a hazard in its own right —
    the same experiment raised `sqlite3.InterfaceError: bad parameter or other API misuse`.
    Serialising the whole operation fixes both, and it is cheap enough to be the right answer:
    this runs once per page load.
    """
    with _session_lock:
        existing = latest_thread(db, backend=backend)
        if existing is not None:
            return existing
        return create_thread(db, backend=backend)


__all__ = [
    "create_thread",
    "get_or_create_session",
    "get_thread",
    "get_thread_row",
    "latest_thread",
    "new_thread_id",
    "rename_thread",
    "set_thread_conversation",
    "set_thread_model",
    "touch_thread",
]
