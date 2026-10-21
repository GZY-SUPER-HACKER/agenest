"""Files carried by the conversation (docs/design.md §29).

Two directions, and they are different problems:

- **The user sends a file.** The bytes arrive over HTTP and are ours to keep, under
  `settings.resolved_attachments_dir`. The agent is a process on this machine with file tools
  and a protocol with no media frame, so what crosses the seam is a **path in the message
  text**. That line is also what puts the file back on the message after a refresh: it carries
  the attachment's id, so stripping it is a lookup in this table rather than a guess about
  which words were a filename.
- **The agent sends a file.** It writes into an *outbox* the appendix names, and the
  supervisor drains it when the turn settles. Drain means **move**: once delivered, a file is
  no longer in the queue, so a second drain cannot deliver it twice. This is the same rule the
  dashboard applies to status — a delivery is recorded when it happens, never inferred from
  what happens to be lying around the workspace.

**Why not render every file the agent touches?** Because "the agent wrote this" is not "the
agent meant to send this". A workspace is full of build output, caches and scratch files, and
picking them out by extension or recency would be §2's inference applied to attachments. The
outbox is the one place where the intent is explicit.

Nothing in this module talks to an agent. It stores, moves, and renders markers; the
supervisor decides when a drain happens, and the backend decides how the agent is told where
the outbox is.
"""

from __future__ import annotations

import contextlib
import logging
import mimetypes
import os
import re
import secrets
from pathlib import Path

from ..config import Settings
from ..database import Database
from ..schemas import AgentAttachmentOut, utcnow
from .backends.base import Attachment

logger = logging.getLogger("csd.agent.attachments")


class AttachmentError(ValueError):
    """The message names a file this conversation cannot carry.

    A distinct type rather than a bare `ValueError`, so the route can turn *this* refusal into a
    400 without also turning a backend's unrelated `ValueError` into "your attachment is bad".
    """


#: `a_` keeps an attachment id apart from a thread id (`t_`) and a conversation id (`c_`) in
#: logs. The hex body is what makes a marker unique enough to recognise in message text.
ID_PREFIX = "a_"
_ID_BODY_BYTES = 12
ID_RE = re.compile(r"^a_[0-9a-f]{24}$")

#: A marker line is `[attachment a_<hex>] <name>: <absolute path>`. It is the instruction the
#: agent reads ("here is a file") and the identity the transcript needs ("this message carried
#: it"), in one line. The name may contain anything except a newline; the id is what is parsed.
_MARKER = re.compile(r"^\[attachment (a_[0-9a-f]{24})\] ")

#: The one-line channel note appended for an agent that has no per-turn appendix. Stripped from
#: the transcript the same way a marker is: it is addressed to the agent, not to the reader.
_CHANNEL = re.compile(r"^\[channel\] ")

#: A MIME type we are willing to echo back. Anything else falls back to what the name says (or
#: octet-stream): the client is a phone, and its `Content-Type` for an upload is a guess too.
_MIME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$"
)

#: Long enough for a descriptive filename, short enough to keep the card and the marker readable.
_NAME_LIMIT = 120

#: Characters that a path cannot contain (or that mean something else) on at least one platform
#: this service runs on. `:` is the one worth naming: on Windows it starts an alternate data
#: stream, so `report.txt:hidden` would write somewhere other than the file we then serve.
_RESERVED = '<>:"/\\|?*'


def new_id() -> str:
    return ID_PREFIX + secrets.token_hex(_ID_BODY_BYTES)


def safe_name(raw: str) -> str:
    """The filename as we will store and show it: one path component, printable, bounded.

    The input is whatever a client sent — a header on the upload route — so it is treated as
    hostile. Only the basename survives, reserved characters are replaced, and the extension is
    preserved when trimming because it is the half that tells the phone which app to open.
    """
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        ch
        if ch.isprintable() and ch not in "\r\n\t" and ch not in _RESERVED
        else "_"
        for ch in name
    ).strip().strip(".")
    if not name:
        return "file"
    stem, dot, suffix = name.rpartition(".")
    if dot and 1 <= len(suffix) <= 12:
        head = stem[: max(1, _NAME_LIMIT - len(suffix) - 1)] or "file"
        return f"{head}.{suffix}"
    return name[:_NAME_LIMIT] or "file"


def mime_for(name: str, declared: str | None) -> str:
    """The type to store and serve. The client's guess first, the name's second, octet last.

    `application/octet-stream` is treated as *no guess*: browsers send it for files whose type
    they do not know, which includes a `.png` picked from a desktop file dialog on some
    platforms. Taking it at face value would store a perfectly good image as an opaque blob and
    cost it its inline rendering.
    """
    if declared:
        value = declared.split(";", 1)[0].strip().lower()
        if _MIME_RE.match(value) and value != "application/octet-stream":
            return value
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _component(value: str) -> str:
    """A directory name derived from an id. Ids are ours, but this stays defensive because a
    conversation id comes from somebody else's generator."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value or "")[:80]
    return cleaned or "unknown"


def _private(directory: Path) -> Path:
    """Create a directory that only this user can read, and say so where the OS supports it."""
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    return directory


def incoming_dir(settings: Settings, thread_id: str) -> Path:
    return _private(settings.resolved_attachments_dir / "in" / _component(thread_id))


def outbox_dir(settings: Settings, conversation_id: str) -> Path:
    """Where the agent writes files to hand the user.

    Named by *conversation* rather than thread, because the agent is told this path and the
    only id a backend necessarily knows is its own. The supervisor maps it back.
    """
    return _private(
        settings.resolved_attachments_dir / "outbox" / _component(conversation_id)
    )


def delivered_dir(settings: Settings, conversation_id: str) -> Path:
    """Where drained files go. A sibling of the outbox, so the queue is emptiable by `ls`."""
    return _private(
        settings.resolved_attachments_dir / "delivered" / _component(conversation_id)
    )


def incoming_target(settings: Settings, thread_id: str, name: str) -> tuple[str, Path]:
    """An id and a path to stream one upload into. The file does not exist yet."""
    attachment_id = new_id()
    target = incoming_dir(settings, thread_id) / f"{attachment_id}-{safe_name(name)}"
    return attachment_id, target


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
_COLUMNS = "id, thread_id, conversation_id, direction, name, mime, size, path, created_at"


def record(
    db: Database,
    *,
    attachment_id: str,
    thread_id: str,
    conversation_id: str | None,
    direction: str,
    name: str,
    mime: str,
    size: int,
    path: Path,
) -> object:
    """Write one row and return it. The caller has already put the bytes in place."""
    with db.write():
        db.execute(
            f"INSERT INTO agent_attachments ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attachment_id,
                thread_id,
                conversation_id,
                direction,
                name,
                mime,
                size,
                str(path),
                utcnow(),
            ),
        )
    row = db.query_one(
        "SELECT * FROM agent_attachments WHERE id = ?", (attachment_id,)
    )
    assert row is not None
    return row


def get(db: Database, attachment_id: str) -> object | None:
    if not ID_RE.match(attachment_id):
        return None
    return db.query_one("SELECT * FROM agent_attachments WHERE id = ?", (attachment_id,))


def get_many(
    db: Database, thread_id: str, ids: list[str], *, direction: str | None = None
) -> list[object]:
    """Rows for `ids`, in the order asked, scoped to one thread.

    Scoped on purpose: an attachment id from another conversation is not a file this message
    may carry, and a lookup that ignored the thread would let anybody with the page token
    attach somebody else's file by guessing an id.
    """
    wanted = [value for value in dict.fromkeys(ids) if ID_RE.match(value)]
    if not wanted:
        return []
    placeholders = ", ".join("?" for _ in wanted)
    params: list[object] = [thread_id, *wanted]
    clause = f"thread_id = ? AND id IN ({placeholders})"
    if direction is not None:
        clause += " AND direction = ?"
        params.append(direction)
    rows = db.query(f"SELECT * FROM agent_attachments WHERE {clause}", tuple(params))
    by_id = {row["id"]: row for row in rows}
    return [by_id[value] for value in wanted if value in by_id]


def list_for_thread(
    db: Database, thread_id: str, *, direction: str | None = None
) -> list[object]:
    if direction is None:
        return db.query(
            "SELECT * FROM agent_attachments WHERE thread_id = ? "
            "ORDER BY created_at, rowid",
            (thread_id,),
        )
    return db.query(
        "SELECT * FROM agent_attachments WHERE thread_id = ? AND direction = ? "
        "ORDER BY created_at, rowid",
        (thread_id, direction),
    )


def delete(db: Database, attachment_id: str) -> object | None:
    """Forget one attachment and its bytes. Used when an upload never reached a message."""
    row = get(db, attachment_id)
    if row is None:
        return None
    with db.write():
        db.execute("DELETE FROM agent_attachments WHERE id = ?", (attachment_id,))
    with contextlib.suppress(OSError):
        Path(row["path"]).unlink()
    return row


def drain(
    db: Database, settings: Settings, thread_id: str, conversation_id: str
) -> list[object]:
    """Deliver everything in this conversation's outbox, and return the rows written.

    **Move, then record.** A file still in the outbox after a failure is retried by the next
    drain; a file recorded twice would show up as two attachments. If the row cannot be
    written after the move, the file is put back so neither state is lost.
    """
    source = outbox_dir(settings, conversation_id)
    if not source.is_dir():
        return []
    destination = delivered_dir(settings, conversation_id)

    rows: list[object] = []
    for path in sorted(source.iterdir(), key=lambda candidate: candidate.name):
        try:
            if path.is_symlink() or not path.is_file() or path.name.startswith("."):
                # A symlink could point anywhere on the host; hidden names are how editors and
                # transfer tools mark a file that is still being written.
                continue
            name = safe_name(path.name)
            attachment_id = new_id()
            target = destination / f"{attachment_id}-{name}"
            path.replace(target)
            size = target.stat().st_size
        except OSError as exc:
            logger.warning("could not move %s out of the outbox: %s", path, exc)
            continue
        try:
            row = record(
                db,
                attachment_id=attachment_id,
                thread_id=thread_id,
                conversation_id=conversation_id,
                direction="out",
                name=name,
                mime=mime_for(name, None),
                size=size,
                path=target,
            )
        except Exception:
            logger.exception("could not record the delivered file %s", target)
            with contextlib.suppress(OSError):
                target.replace(path)
            continue
        # A zero-byte delivery is legal (an empty report); it gets the same mode as any other.
        with contextlib.suppress(OSError):
            os.chmod(target, 0o600)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# the wire shape
# --------------------------------------------------------------------------- #
def as_attachment(row) -> Attachment:
    return Attachment(
        id=row["id"],
        name=row["name"],
        mime=row["mime"],
        size=row["size"],
        direction=row["direction"],
        created_at=row["created_at"],
    )


def to_out(value) -> AgentAttachmentOut:
    """The wire shape for either a database row or a normalized `Attachment`.

    Two shapes meet in the supervisor: marker-resolved files arrive as rows, and an outgoing
    attachment is created as a normalized event before it ever needs a row-shaped accessor.
    """
    if isinstance(value, Attachment):
        return AgentAttachmentOut(
            id=value.id,
            name=value.name,
            mime=value.mime,
            size=value.size,
            direction=value.direction,
            created_at=value.created_at,
        )
    return AgentAttachmentOut(
        id=value["id"],
        name=value["name"],
        mime=value["mime"],
        size=value["size"],
        direction=value["direction"],
        created_at=value["created_at"],
    )


# --------------------------------------------------------------------------- #
# the marker
# --------------------------------------------------------------------------- #
def compose_prompt(text: str, rows: list[object]) -> str:
    """The message the agent receives: one line per file, then the user's words.

    The path is absolute and the line is prose a model reads naturally: "here is a file, and
    here is where it is". Nothing binary crosses the channel, which is what lets the same
    message work for every backend — the agent opens the file with the tools it already has.

    **The markers come first, and that is load-bearing.** A backend caps a stored user message
    (`cap_string`, 2000 chars) and the cap truncates the *tail*; markers at the end would be the
    first thing lost when someone sends a long prompt with a file, and a re-read transcript
    would silently lose the card for a file the agent did receive. At the front, a truncation
    costs the end of the user's prose instead — a visible loss — and the delivery survives.
    """
    parts: list[str] = []
    markers = [f"[attachment {row['id']}] {row['name']}: {row['path']}" for row in rows]
    if markers:
        parts.append("\n".join(markers))
    body = text.strip()
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def marker_ids(text: str) -> list[str]:
    """Every attachment id named by a marker line, in order, without consulting the database."""
    found: list[str] = []
    for line in text.split("\n"):
        match = _MARKER.match(line)
        if match:
            found.append(match.group(1))
    return found


def strip(text: str, known: set[str]) -> str:
    """Remove marker and channel lines whose id we resolved, leaving the user's words.

    Only *known* ids are removed. An attachment id that does not resolve — a transcript carried
    over from another deployment, or a model quoting the line back — stays as text, because
    dropping it would delete words on the strength of looking like a marker.

    When nothing is removed, the text is returned **untouched**. That matters: a backend's
    transcript is the agent's record, and trimming whitespace here used to make a checkpoint
    that ended in a space come back without it — a difference the adapter tests pin, and the
    right rule is that this function only edits what it was asked to edit.
    """
    kept: list[str] = []
    removed = False
    for line in text.split("\n"):
        match = _MARKER.match(line)
        if match and match.group(1) in known:
            removed = True
            continue
        if _CHANNEL.match(line):
            removed = True
            continue
        kept.append(line)
    if not removed:
        return text
    clean = "\n".join(kept)
    # The markers were composed as their own block, so removing them leaves the separator
    # behind — at the front, because that is where they are. Collapsed only when we changed
    # something, for the reason above.
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip("\n")
    return clean


__all__ = [
    "AttachmentError",
    "ID_RE",
    "as_attachment",
    "compose_prompt",
    "delete",
    "delivered_dir",
    "drain",
    "get",
    "get_many",
    "incoming_dir",
    "incoming_target",
    "list_for_thread",
    "marker_ids",
    "mime_for",
    "new_id",
    "outbox_dir",
    "record",
    "safe_name",
    "strip",
    "to_out",
]
