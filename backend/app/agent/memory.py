"""The agent's long-term memory: a journal, a Dream, and a git history.

The design is docs/design.md §18. Three pieces, and the shape of each is dictated by a constraint
worth restating here because it is what makes this module small:

**The journal is derived from the agent's own record, not from a copy kept here.** For `native`
that record is Pi's session file, reached through `AgentBackend.history()` — Pi is the kernel of
this service's agent, so the conversation belongs to Pi and a second copy in SQLite would be a
second answer to "what was said", written by different code, drifting. Deriving from it also
means the journal gets what the copy used to drop: the tool arguments, which is the difference
between "something failed" and "this command failed".

One consequence is worth stating: the journal is idempotent by construction, because it is
re-derived from the messages after the cursor, so a crash between the write and the cursor update
converges on the same file instead of duplicating an entry.

(An earlier version of this docstring claimed Pi's session file *could not* be replayed. That was
wrong, and the mistake mattered — it turned a design decision into a false constraint, which is
the kind of thing that stops being questioned. §18 keeps the correction.)

**Dream is a Pi process with a restricted tool set**, run on its own clock, which edits the
markdown files under `memory/` and nothing else. It is the same idea as nanobot's Dream and a
different implementation, because nanobot's agent *is* the product and ours is a channel onto one.

**The commit message is derived from the diff, not from the model.** The model that edited the
files is not a reliable witness to what it edited, so the subject comes from `git diff --stat` and
the model's account goes in the body. See `MemoryGit.describe_changes`.

Everything here is deliberately free of HTTP. The one async method is `Dream.run`, and only
because the Pi process it drives is async — not because anything here awaits concurrently. The
supervisor owns the clock; this module owns the rules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..schemas import AgentRole, utcnow
from .native_config import (
    EMPTY as EMPTY_CONFIG,
    PROVIDER_CONFIG_ENV,
    PROVIDER_EXTENSION,
    PROVIDER_KEY_ENV,
    Effective,
    NativeConfig,
    provider_config_path,
)
from .backends.base import Message

logger = logging.getLogger("csd.agent.memory")

#: The durable memory files, relative to the memory directory. `MemoryGit._tracked()` is what
#: turns these into a staged set; a file outside it is not memory, and committing one would turn
#: the memory repository into a shadow of whatever else lives in the workspace.
#:
#: `skills/<name>/SKILL.md` is the fourth thing Dream may write and is deliberately **not** named
#: here: it is a glob, not a path, and the two places that need the set (`_tracked`, and the
#: prompt's routing table) are honest about their own form rather than sharing a name for two
#: different shapes.
MEMORY_FILE = "MEMORY.md"
USER_FILE = "USER.md"
SOUL_FILE = "SOUL.md"

#: The generated appendix the Pi extension re-reads on every prompt. Derived, never written by
#: hand and never committed — the memory repository's `.gitignore` keeps it out, because it
#: would otherwise be a second copy of the three files, committed under a different shape.
RECALL_FILE = ".recall.md"

#: Above this, one entry is almost certainly a model echoing its input back rather than
#: summarising it. nanobot carries the same guard (`_HISTORY_ENTRY_HARD_CAP`, 64k) for the same
#: reason, and logs once rather than per occurrence.
ENTRY_HARD_CAP = 64_000

#: Entries older than this are dropped when the journal is compacted. Dream is meant to have
#: consumed them by now; keeping them is how a journal grows until it is unusable.
JOURNAL_MAX_ENTRIES = 1000

#: How long one Dream run may take before it is abandoned.
#:
#: Not derived from `CSD_PI_COMMAND_TIMEOUT`, because the two bound different things: that one
#: bounds a command's *acceptance* (Pi answers a `prompt` immediately) and this one bounds a
#: model call that reads four files and writes some of them back. Fifteen minutes is generous
#: for that and still short enough that a wedged process is noticed the same day.
DREAM_RUN_TIMEOUT = 900.0

#: How much memory may ride in one system-prompt appendix. Bounded by the *argument* limit,
#: not by what a model can read: the text travels as one argv element, and Windows caps a
#: command line at 32,767 characters. 16k leaves room for Pi's own arguments while still being
#: far more memory than three curated files need.
RECALL_MAX_CHARS = 16_000


# --------------------------------------------------------------------------- #
# recall
# --------------------------------------------------------------------------- #
def recall_prompt(directory: Path) -> str | None:
    """The durable memory as a system-prompt appendix, or `None` when there is none.

    nanobot rebuilds this from disk on every turn. We cannot: Pi reads instructions when its
    process starts (`AGENTS.md` / `CLAUDE.md`, and `--append-system-prompt`), so the appendix
    is attached at spawn time and the process is restarted after a Dream that changed
    something (`NativeBackend.refresh_context`). That is docs/design.md §18's recall half —
    without it the memory is written and consolidated but never read by the agent.

    The files are *context*, not a transcript. The text says so because a model handed a
    "what remains true" section is otherwise entitled to treat it as instructions, and a
    memory that argues with the user is worse than no memory.
    """
    sections: list[str] = []
    for name, title in (
        (USER_FILE, "Who you are talking to"),
        (MEMORY_FILE, "What remains true about the work"),
        (SOUL_FILE, "How you should sound"),
    ):
        path = directory / name
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("could not read %s for recall: %s", path, exc)
            continue
        if text:
            sections.append(f"## {title}\n\n{text}")

    if not sections:
        return None

    journal = directory / "history.jsonl"
    if journal.exists():
        sections.append(
            "## Past conversations\n\n"
            f"The append-only journal of earlier conversations is `{journal}`. Read or grep "
            "it when the summary above is not enough; it holds the detail this summary was "
            "compressed from."
        )

    text = (
        "# Long-term memory\n\n"
        "This was consolidated from your earlier conversations with this user. It is "
        "background, not a fresh instruction: if it conflicts with what the user says now, "
        "the user wins, and you should say so rather than quietly keeping the old fact.\n\n"
        + "\n\n".join(sections)
    )
    if len(text) > RECALL_MAX_CHARS:
        text = (
            text[:RECALL_MAX_CHARS].rstrip()
            + "\n\n[memory truncated here; read the files for the rest]"
        )
    return text


def write_recall_file(directory: Path) -> Path:
    """Regenerate the appendix file the Pi extension reads every turn.

    Called wherever the memory can have changed: after a Dream that wrote something, before a Pi
    process starts, and when a run settles (so a hand-edit of `MEMORY.md` reaches the agent
    without a restart). Idempotent — the extension compares nothing, it just reads — so calling
    it more often than needed costs one small write and no correctness.

    Clearing the file when there is nothing to recall is deliberate: a stale appendix surviving
    an emptied memory is the one failure a reader cannot see.
    """
    path = directory / RECALL_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(recall_prompt(directory) or "", encoding="utf-8")
    except OSError as exc:
        # The extension falls back to "no memory" on an unreadable file, which is worse than
        # stale but better than a broken agent; the log is where the difference shows up.
        logger.warning("could not write the recall appendix %s: %s", path, exc)
    return path


# --------------------------------------------------------------------------- #
# cursors
# --------------------------------------------------------------------------- #
# Two cursors, stored in `meta`, and they are different kinds of thing.
#
# `journal_cursor` is an **id the agent minted** for the last message already journalled, and its
# key carries the thread id. It is opaque — Pi chose it — so the only thing that can be asked of
# it is whether it appears in the conversation, which is why `_after` compares by position rather
# than by value. Per-thread because the journal is one ordered file while a thread's conversation
# is its own: a single global cursor would let the newer conversation advance past the older
# one's unread messages.
#
# `dream_cursor` is the **index of the last journal entry Dream has consumed** — a number, and a
# number we own. It points into a file, so it cannot be recovered, which is why it is kept and
# why losing it costs a re-read rather than history. Global, because the journal is one file.

CURSOR_JOURNAL = "memory.journal_cursor"


def journal_cursor_key(thread_id: str) -> str:
    """The `meta` key holding one conversation's journal cursor."""
    return f"{CURSOR_JOURNAL}.{thread_id}"


CURSOR_DREAM = "memory.dream_cursor"


def _read_cursor(db: Any, key: str) -> int:
    """The persisted cursor, or 0. A missing or unparsable value means "start from the
    beginning" — the same recovery nanobot's `_read_cursor_counter` makes, and for the same
    reason: a corrupted book is worth less than the history it points into."""
    try:
        row = db.query_one("SELECT value FROM meta WHERE key = ?", (key,))
    except Exception:  # pragma: no cover - a closed database is the caller's bug, not ours
        return 0
    if row is None:
        return 0
    try:
        value = int(str(row["value"]).strip())
    except (TypeError, ValueError):
        logger.warning("cursor %s is not an integer (%r); restarting from 0", key, row["value"])
        return 0
    return value if value > 0 else 0


def _write_cursor(db: Any, key: str, value: int) -> None:
    with db.write():
        db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def _read_id_cursor(db: Any, key: str) -> str | None:
    """A cursor holding an **agent-minted id**, which is opaque and not a number.

    Separate from the integer cursors because the two mean different things: the Dream cursor is
    an index into a file we own and can compare with `>`, while this one is a value Pi chose and
    the only thing that can be said about it is whether it appears in the conversation.
    """
    try:
        row = db.query_one("SELECT value FROM meta WHERE key = ?", (key,))
    except Exception:  # pragma: no cover - a closed database is the caller's bug, not ours
        return None
    if row is None:
        return None
    value = str(row["value"]).strip()
    return value or None


def _write_id_cursor(db: Any, key: str, value: str | None) -> None:
    if not value:
        return
    with db.write():
        db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _after(messages: list[Message], cursor_id: str | None) -> list[Message]:
    """Everything after `cursor_id`, or everything when the cursor cannot be found.

    Position, not comparison — see `_read_id_cursor`. A cursor that is absent from the
    conversation means the transcript was replaced or the cursor came from a session that no
    longer exists, and the safe reading of that is "start over": re-reading costs tokens, while
    silently journalling nothing costs the memory this whole module exists to keep.
    """
    if cursor_id is None:
        return list(messages)
    for index, message in enumerate(messages):
        if message.id == cursor_id:
            return list(messages[index + 1 :])
    return list(messages)


def _describe_args(args: dict | None) -> str:
    """Render a tool call's arguments as a short, greppable phrase.

    `bash` gets its command line bare, because that is the fact worth having — a journal entry
    reading `bash: <command>` is something a person can act on, and `bash: {'command': '...'}`
    is the same information wearing a dictionary. Anything else falls back to the arguments
    themselves, truncated: an unfamiliar tool's shape is still worth more than nothing.
    """
    if not args:
        return ""
    if isinstance(args.get("command"), str):
        return _one_line(args["command"])
    try:
        rendered = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    return _one_line(rendered)


def _one_line(text: str, limit: int = 200) -> str:
    """Collapse to a single line and bound it.

    A journal entry is one line per message by construction, because the file is grepped and a
    command containing a newline would otherwise break the one-record-per-line shape everything
    downstream assumes.
    """
    flat = " ".join(str(text).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


# --------------------------------------------------------------------------- #
# the journal
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class JournalEntry:
    """One journalled run: what was said, bounded, in the shape Dream expects."""

    index: int
    timestamp: str
    content: str

    def to_record(self) -> dict[str, Any]:
        return {"index": self.index, "timestamp": self.timestamp, "content": self.content}


def _entry_content(text: str) -> str:
    """Bound one entry and drop the scaffold a model sometimes leaves behind.

    `strip_think` in nanobot exists for the same reason: a model that begins a summary with an
    unclosed `<think` tag, or that echoes `<channel|>` markers, pollutes every later reader of
    the file — including Dream, which reads the journal as its primary input.
    """
    content = text.strip()
    for marker in ("<think", "</think>", "<channel|>", "<|channel|>"):
        content = content.replace(marker, "")
    return content[:ENTRY_HARD_CAP].strip()


class Journal:
    """The append-only run log, derived from the agent's own conversation record.

    Not a log in the dashboard's sense: it is not a record of what an agent did, it is the
    *material* from which long-term memory is shaped. nanobot's `history.jsonl` and this are the
    same idea with a different producer — see the module docstring.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    # -- reading ------------------------------------------------------------ #
    def read(self) -> list[JournalEntry]:
        """Every entry, oldest first. A malformed line is skipped, not fatal: the journal is an
        optimisation over the transcript, so losing one line costs Dream a little context and
        costs the user nothing."""
        if not self.path.is_file():
            return []
        entries: list[JournalEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping malformed journal line: %.80r", line)
                continue
            if not isinstance(parsed, dict):
                continue
            index = parsed.get("index")
            content = parsed.get("content")
            # Both must be present and sane. An entry with no content is not an entry.
            if not isinstance(index, int) or isinstance(index, bool) or index < 1:
                continue
            if not isinstance(content, str):
                continue
            entries.append(
                JournalEntry(
                    index=index,
                    timestamp=parsed.get("timestamp") or "",
                    content=content,
                )
            )
        return entries

    def unprocessed(self, since_index: int) -> list[JournalEntry]:
        return [e for e in self.read() if e.index > since_index]

    def last_index(self) -> int:
        entries = self.read()
        return entries[-1].index if entries else 0

    # -- writing ------------------------------------------------------------ #
    def append(self, entry: JournalEntry) -> None:
        """Append one entry. Called only by `consolidator`, which has already checked that the
        index is the next one — so an index collision is a bug, not a condition to handle."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_record(), ensure_ascii=False) + "\n")

    def rewrite(self, entries: list[JournalEntry]) -> None:
        """Replace the whole file. Used by compaction, which is the only operation that is
        allowed to remove an entry — because Dream has already consumed everything before the
        cut, and the transcript in SQLite still has the original."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry.to_record(), ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def compact(self, *, keep: int = JOURNAL_MAX_ENTRIES, before_index: int = 0) -> int:
        """Drop the oldest entries beyond `keep`, never touching anything at or after
        `before_index` — the Dream cursor, which marks what has not been consumed yet.

        Returns how many were dropped. A journal that must retain more than `keep` because the
        cursor is behind is left oversized and logged, rather than dropping entries Dream has
        not read: an oversized journal costs tokens, a dropped one costs memory.
        """
        if keep <= 0:
            return 0
        entries = self.read()
        if len(entries) <= keep:
            return 0
        first_unprocessed = next(
            (i for i, e in enumerate(entries) if e.index > before_index),
            len(entries),
        )
        drop = min(len(entries) - keep, first_unprocessed)
        if drop <= 0:
            logger.warning(
                "journal holds %d entries, over the %d limit, but the dream cursor is at %d — "
                "keeping them until Dream catches up",
                len(entries),
                keep,
                before_index,
            )
            return 0
        self.rewrite(entries[drop:])
        return drop


# --------------------------------------------------------------------------- #
# consolidator: the agent's conversation -> journal
# --------------------------------------------------------------------------- #
class Consolidator:
    """Turns a settled run into one journal entry.

    It is **handed** the conversation rather than reading it, because this service no longer
    keeps one: the transcript belongs to the agent, and `AgentBackend.history()` is how it is
    asked for. Two things follow. There is no shared sqlite connection to race any more, so this
    no longer has to run on the event loop to be safe. And the journal now inherits whatever the
    agent's own record holds — for Pi, that includes the tool arguments a copy here used to drop,
    which is the difference between "something failed" and "this command failed".

    One entry per run, not per turn. A run is what the user experiences — "I asked, it worked" —
    and per-turn entries would bury the outcome under the dialogue that produced it.
    """

    def __init__(
        self,
        db: Any,
        journal: Journal,
        *,
        min_chars: int = 400,
        max_messages: int = 1000,
    ) -> None:
        self._db = db
        self._journal = journal
        self._min_chars = min_chars
        #: Messages read per pass. A bound rather than a batch size: a conversation with a long
        #: history drains over successive runs, and nothing is lost because the cursor advances
        #: only over messages actually read.
        self._max_messages = max(1, max_messages)

    def consolidate(self, thread_id: str, messages: list[Message]) -> JournalEntry | None:
        """Journal everything after the cursor.

        The cursor is an id **the agent minted**, so it is opaque: there is nothing to compare it
        with except position in the ordered conversation the agent just returned. That is also
        the honest recovery when it cannot be found — a replaced session, or a cursor from a
        transcript that no longer exists — because re-reading a conversation costs tokens and
        losing one costs memory.
        """
        key = journal_cursor_key(thread_id)
        fresh = _after(messages, _read_id_cursor(self._db, key))
        if not fresh:
            return None
        batch = fresh[: self._max_messages]
        if len(fresh) > len(batch):
            logger.info(
                "%s has %d messages past the cursor; journalling the first %d, the rest next run",
                thread_id,
                len(fresh),
                len(batch),
            )

        last_id = batch[-1].id
        chars = sum(len(m.content or "") for m in batch)
        if chars < self._min_chars:
            logger.debug(
                "run in %s produced %d chars, under the %d threshold; not journalling",
                thread_id,
                chars,
                self._min_chars,
            )
            # The cursor still advances. Otherwise every later run re-reads this one and the
            # threshold becomes a permanent floor that nothing can cross.
            _write_id_cursor(self._db, key, last_id)
            return None

        entry = JournalEntry(
            index=self._journal.last_index() + 1,
            timestamp=utcnow(),
            content=_entry_content(self._format(batch)),
        )
        self._journal.append(entry)
        # Written after the append, and that order is load-bearing: a crash between the two
        # leaves an entry the cursor does not cover, so the same messages are journalled again
        # as a second entry — visible and recoverable. The reverse order would drop them
        # silently, which is the one outcome with no way back.
        _write_id_cursor(self._db, key, last_id)
        return entry

    @staticmethod
    def _format(messages: list[Message]) -> str:
        """Render a run as the model-readable text Dream will consume.

        Deliberately not Markdown: the journal is read by a model and grepped by a human, and
        both are better served by `ROLE: text` than by headings that imply structure the entries
        do not have.

        A tool row now carries **what was run** as well as what came back. That pairing is the
        whole reason this formatter was rewritten: with only the output, a journal entry can say
        a command failed without saying which one, and a consolidation pass reading it has no way
        to tell an ordinary error from a real discovery.
        """
        lines: list[str] = []
        for message in messages:
            text = (message.content or "").strip()
            if not text:
                continue
            role = message.role.value.upper()
            if message.role is AgentRole.tool:
                # `failed` is a word, not a colour — the same rule the agent page applies to
                # tool rows, and for the same reason: the journal may be read by a model that
                # cannot see one.
                outcome = " [failed]" if message.is_error else ""
                name = message.tool_name or "tool"
                command = _describe_args(message.args)
                head = f"{name}{outcome}" + (f" {command}" if command else "")
                lines.append(f"{role} {head}: {text}")
            else:
                lines.append(f"{role}: {text}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Dream's prompt
# --------------------------------------------------------------------------- #
#: What Dream is told to do. The file-routing table is the load-bearing part: it is what stops
#: four files from becoming one long file, and it is the part most worth editing per deployment.
DREAM_PROMPT = """You are running Dream: consolidating a conversation journal into long-term memory.

## File routing

Store each fact in one canonical location. Merge duplicates and overlapping sections rather than
adding a second copy.

| Path | Content |
|------|---------|
| `MEMORY.md` | Project goals, architecture, strategic decisions, dead ends already ruled out |
| `USER.md` | Who the user is: language, expertise, how they want answers framed |
| `SOUL.md` | How the agent should sound and behave |
| `skills/<name>/SKILL.md` | A workflow that has appeared at least twice, with concrete steps |

Write atomic facts ("prefers replies in Chinese", "the dashboard's status is reported, never
inferred"), not descriptions of the conversation that produced them.

## What to remove

Remove superseded facts, stale task state, resolved incidents, and one-off debugging details
unlikely to recur. Prefer deleting a single bullet over rewriting a whole section. Compress
verbose entries.

## Editing

Read the current target files first, then make focused edits. Create a missing canonical file
when it has content to hold. Batch related changes.

If the retained memory is already current, leave it unchanged and say so — rewriting a file to
say the same thing is churn, and it costs the reader a diff to discover nothing moved.

## Conversation history

The unprocessed journal entries follow. Each is `[timestamp] ROLE: text`; `TOOL [failed]:` is a
tool result and its outcome.
"""


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
class MemoryGit:
    """Version control for the memory files, by shelling out to `git`.

    nanobot uses dulwich. We do not, for one reason: the memory repository is read by humans with
    `git log` and `git diff`, and requiring a Python git library to read its own history is a
    worse trade than one subprocess. `git` is already how the agent's workspace is versioned.

    Every method is deliberately tolerant. A memory repository that cannot be committed is a
    missed audit trail, not a failed Dream — the files are already on disk, which is what the
    agent reads. So failures are logged and return `None`; they never raise into the caller.
    """

    #: Commits are attributed to the service, not to the model. The model's account of what it
    #: did is in the commit *body*; the author is whoever ran the process.
    AUTHOR = "csd-memory <csd-memory@localhost>"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    # -- internals ---------------------------------------------------------- #
    def _git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=str(self.directory),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=check,
            )
        except FileNotFoundError:
            # No git on this host. Memory still works; it just has no history.
            logger.warning("git is not installed; memory will not be versioned")
            return None
        except subprocess.TimeoutExpired:
            logger.warning("`git %s` timed out", args[0] if args else "")
            return None
        except OSError as exc:
            logger.warning("could not run git: %s", exc)
            return None

    @property
    def available(self) -> bool:
        result = self._git("rev-parse", "--git-dir")
        return result is not None and result.returncode == 0

    def _pathspecs(self) -> list[str]:
        """The paths to stage, filtered to what actually exists.

        `git add` aborts the whole command on a pathspec that matches nothing — including the
        legitimate case of a memory directory with no skills yet — and an aborted `add` stages
        nothing, so a commit would silently find no changes. Hence the filter.

        `skills` is added as a directory rather than as `skills/*/SKILL.md` for the same reason:
        a glob with no matches is a fatal pathspec. The `.gitignore` written by `initialize()`
        is what keeps the directory's non-SKILL.md contents out.
        """
        paths: list[str] = []
        for name in (MEMORY_FILE, USER_FILE, SOUL_FILE):
            if (self.directory / name).exists():
                paths.append(name)
        if (self.directory / "skills").is_dir():
            paths.append("skills")
        return paths

    def _add(self) -> None:
        paths = self._pathspecs()
        if paths:
            self._git("add", "-A", "--", *paths)

    # -- lifecycle ---------------------------------------------------------- #
    def initialize(self) -> bool:
        """Create the repository, if the directory is not already inside one.

        Refuses to nest: a `.git` inside the agent workspace would put every file the agent
        touches under version control, which is not what was asked for and is a good way to
        commit a secret.
        """
        if self.available:
            return False
        probe = self._git("rev-parse", "--show-toplevel")
        if probe is not None and probe.returncode == 0:
            logger.info(
                "memory directory %s is already inside a git repository (%s); not nesting one",
                self.directory,
                (probe.stdout or "").strip(),
            )
            return False

        self.directory.mkdir(parents=True, exist_ok=True)
        if self._git("init", check=False) is None:
            return False
        # Identity is set locally, not globally: a service that reconfigures the operator's
        # global git config has done something they did not ask for.
        self._git("config", "user.name", "csd-memory")
        self._git("config", "user.email", "csd-memory@localhost")

        gitignore = self.directory / ".gitignore"
        if not gitignore.exists():
            # Ignore everything, then re-include the memory files. `.gitignore` itself is kept
            # so the rule travels with the repository rather than living only in this function.
            #
            # Correctness does not actually depend on this file — `_pathspecs` decides what is
            # staged — but a `git status` full of the agent's scratch files would make the
            # repository useless to read.
            gitignore.write_text(
                "# Ignore everything except the memory files.\n"
                "/*\n"
                "!/.gitignore\n"
                + "".join(f"!/{path}\n" for path in (MEMORY_FILE, USER_FILE, SOUL_FILE))
                + "!/skills/\n",
                encoding="utf-8",
            )

        # The initial commit exists so the first real change has something to diff against.
        # Without it `git diff` against an unborn branch is not a diff.
        for name in (MEMORY_FILE, USER_FILE, SOUL_FILE):
            target = self.directory / name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("", encoding="utf-8")
        self._git("add", ".gitignore")
        self._add()
        self._git(
            "commit", "-m", "init: agent memory store", "--author", self.AUTHOR, check=False
        )
        return True

    # -- diffing ------------------------------------------------------------ #
    def describe_changes(self) -> str:
        """Which files changed and by how much — the ground truth for a commit subject.

        This is `summarize_working_tree` in nanobot's GitStore, and the reason it exists is the
        same: the model that edited the files is not a reliable witness to what it edited. A
        diff cannot say "I consolidated memory" when it truncated one line.

        **Stages first.** `git diff` ignores untracked files, and Dream's fourth writable path is
        a *new directory* (`skills/<name>/SKILL.md`). Without staging, a skill Dream just
        created would be absent from the diff — and the commit that follows would miss it.
        """
        self._add()
        result = self._git("diff", "--cached", "--stat", "--", *self._tracked())
        if result is None or not (result.stdout or "").strip():
            return ""
        # `--stat` truncates long paths and appends a total line; the total is noise here
        # because it counts a file the diff does not show.
        return "\n".join(
            line for line in result.stdout.strip().splitlines() if "|" in line
        ).strip()

    def _tracked(self) -> list[str]:
        """Every path the memory repository is allowed to care about.

        `.dream_cursor` is deliberately absent. In nanobot the Dream cursor is a file inside the
        tracked set, and its own comment explains why it is then excluded from the diff summary:
        progress bookkeeping must not read as a durable-memory edit. Here the cursor lives in the
        `meta` table, so there is no file to exclude — the equivalent mistake is not possible.
        """
        return [MEMORY_FILE, USER_FILE, SOUL_FILE, "skills"]

    def staged_diff(self) -> str:
        """The hunks themselves, for the commit body. Bounded: a Dream that rewrote a file is
        worth a summary, not a transcript.

        Staged rather than working-tree: `describe_changes` stages so an untracked skill appears,
        and the same set is exactly what `commit` will write. Reading the working tree after
        staging would show nothing at all.
        """
        self._add()
        result = self._git(
            "diff", "--cached", "--unified=1", "--", *self._tracked()
        )
        if result is None:
            return ""
        return (result.stdout or "")[:8000].strip()

    def commit(self, subject: str, body: str = "") -> str | None:
        """Commit the memory files. Returns the short SHA, or `None` if there was nothing to
        commit — which is the expected result when Dream decided nothing needed changing."""
        if not self.available:
            return None
        self._add()
        status = self._git("diff", "--cached", "--quiet")
        if status is not None and status.returncode == 0:
            return None
        message = f"{subject}\n\n{body}".strip()
        result = self._git(
            "commit", "-m", message, "--author", self.AUTHOR, check=False
        )
        if result is None or result.returncode != 0:
            logger.warning("committing memory failed: %s", (result.stderr or "").strip()[:200])
            return None
        head = self._git("rev-parse", "--short", "HEAD")
        return (head.stdout or "").strip() if head is not None else None

    def log(self, limit: int = 10) -> list[dict[str, str]]:
        """Recent commits, newest first. Used by the API to render the audit trail."""
        result = self._git(
            "log",
            f"-{max(1, limit)}",
            "--pretty=format:%h%x1f%ad%x1f%s",
            "--date=short",
            "--",
            *self._tracked(),
        )
        if result is None or not (result.stdout or "").strip():
            return []
        out: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\x1f")
            if len(parts) != 3:
                continue
            sha, date, subject = parts
            out.append({"sha": sha, "date": date, "subject": subject})
        return out


# --------------------------------------------------------------------------- #
# Dream
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class DreamResult:
    """What one Dream run did. `advanced` is the field the cursor is set from, and it is
    deliberately not the same as `changed` — see docs/design.md §18."""

    ran: bool = False
    changed: bool = False
    advanced: bool = False
    detail: str = ""
    commit: str | None = None


class Dream:
    """The slow layer: read the unprocessed journal, edit the memory files, commit.

    `run` is async because the process it drives is (`PiDreamRunner` uses `asyncio`
    subprocesses), not because anything here needs to await concurrently. The file I/O inside is
    blocking and small; the model call is the part that takes minutes, and it happens in a child
    process rather than on the event loop.
    """

    def __init__(
        self,
        *,
        directory: Path,
        journal: Journal,
        db: Any,
        prompt: str = DREAM_PROMPT,
        max_entries: int = 20,
        runner: Any = None,
    ) -> None:
        self.directory = directory
        self.journal = journal
        self._db = db
        self._prompt = prompt
        self._max_entries = max_entries
        #: The thing that turns a prompt into edited files. Injected rather than constructed
        #: here so the rest of this class — the cursor rules, the diff, the compaction — is
        #: testable without a subprocess and a model key.
        self._runner = runner
        self.git = MemoryGit(directory)

    def build_prompt(self) -> tuple[str, int] | None:
        """The Dream prompt and the journal index it covers, or `None` when there is nothing
        unprocessed. `None` is the common case and the cheap one: no process, no model call."""
        cursor = _read_cursor(self._db, CURSOR_DREAM)
        pending = self.journal.unprocessed(cursor)
        if not pending:
            return None
        batch = pending[: self._max_entries]
        history = "\n\n".join(
            f"[{e.timestamp[:19]}] {e.content}" for e in batch
        )
        return f"{self._prompt}\n\n## Conversation history\n\n{history}", batch[-1].index

    def pending_count(self) -> int:
        """How many journal entries Dream has not consumed.

        The one number that says whether the slow layer is keeping up: a value that only grows
        means Dream is not running, and that is worth being able to see without reading a log.
        """
        return len(self.journal.unprocessed(_read_cursor(self._db, CURSOR_DREAM)))

    async def run(self) -> DreamResult:
        """Run one Dream pass. Never raises: a failed Dream must not take the service down, and
        a failed Dream that left the cursor alone will simply be retried."""
        try:
            built = self.build_prompt()
            if built is None:
                return DreamResult(ran=False, detail="nothing to consolidate")
            prompt, last_index = built

            if not self.git.available:
                self.git.initialize()

            # The files are read *before* the model runs, so the diff afterwards is a real diff
            # rather than a comparison against whatever was on disk when the process started.
            before = self._snapshot()

            await self._invoke(prompt)

            after = self._snapshot()
            changed = before != after

            if not changed:
                # Completed, nothing moved. The cursor still advances — see §18: otherwise the
                # journal is re-read forever. This is a result, not a failure.
                _write_cursor(self._db, CURSOR_DREAM, last_index)
                return DreamResult(
                    ran=True,
                    changed=False,
                    advanced=True,
                    detail="memory is already current; no changes",
                )

            # Staged, so a skill Dream created in a brand-new directory is in the diff. Both
            # readers stage, and the set they see is the set `commit` will write.
            diff = self.git.describe_changes()
            body = self.git.staged_diff()

            subject = self._subject(diff)
            sha = self.git.commit(subject, body)
            _write_cursor(self._db, CURSOR_DREAM, last_index)
            dropped = self.journal.compact(before_index=last_index)
            if dropped:
                logger.info("journal compacted: dropped %d entries", dropped)
            # The agent reads this file, not the three the model just edited (design.md §18).
            # Regenerating it here is what makes "Dream changed the memory" true for a Pi process
            # that is already running: the extension re-reads it on the next prompt.
            write_recall_file(self.directory)
            return DreamResult(
                ran=True,
                changed=True,
                advanced=True,
                detail=diff or "memory updated",
                commit=sha,
            )
        except Exception:
            # The cursor is deliberately left alone: the same batch is retried, which is safe
            # because Dream's edits are idempotent given unchanged inputs.
            logger.exception("dream failed; the cursor was not advanced")
            return DreamResult(ran=True, detail="dream failed; will retry")

    # -- internals ---------------------------------------------------------- #
    def _snapshot(self) -> dict[str, str]:
        """Content of every editable file, so a change is a dict comparison rather than a stat
        of mtimes — a model that rewrites a file with identical content has not changed
        anything, and mtime would say it had.

        Only the three durable files *and every skill Dream may have created*. Skills were
        absent from this map at first, which made a skill-only Dream look like "no update
        needed": the cursor advanced, no commit was written, and the new file was never
        surfaced to the agent. A directory Dream can write is part of what "changed" means.

        The Dream cursor is a row in `meta`, so it cannot be mistaken for a memory edit — which
        is the failure nanobot has to guard against by excluding its own `.dream_cursor` from
        the diff summary.
        """
        state: dict[str, str] = {}
        for name in (MEMORY_FILE, USER_FILE, SOUL_FILE):
            path = self.directory / name
            try:
                state[name] = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                state[name] = ""
            except OSError as exc:
                logger.warning("could not read %s: %s", path, exc)
                state[name] = ""
        skills = self.directory / "skills"
        if skills.is_dir():
            for path in sorted(skills.rglob("SKILL.md")):
                try:
                    state[str(path.relative_to(self.directory))] = path.read_text(
                        encoding="utf-8"
                    )
                except OSError as exc:
                    logger.warning("could not read %s: %s", path, exc)
        return state

    @staticmethod
    def _subject(diff: str) -> str:
        """A commit subject derived from the diff.

        Not from the model: it is the same model that edited the files, and a self-report is an
        audit performed by the auditee.
        """
        if not diff:
            return "dream: consolidate memory"
        files = [
            line.split("|")[0].strip()
            for line in diff.splitlines()
            if "|" in line
        ]
        names = ", ".join(files[:3])
        if len(files) > 3:
            names += f" and {len(files) - 3} more"
        return f"dream: update {names}"

    async def _invoke(self, prompt: str) -> None:
        """Hand the prompt to the injected runner.

        A method rather than a call in `run` so a test can substitute a fake that edits the
        files directly — which is how the cursor rules are tested without a subprocess.
        """
        if self._runner is None:
            raise RuntimeError(
                "Dream has no runner; pass one (PiDreamRunner in production, a fake in tests)"
            )
        await self._runner.run(prompt, directory=self.directory)


# --------------------------------------------------------------------------- #
# the Pi process that runs Dream
# --------------------------------------------------------------------------- #
class PiDreamRunner:
    """Runs Dream in a Pi subprocess, with a tool allowlist that can only edit memory files.

    `CSD_PI_TOOLS` is reused as the allowlist, and that is a deliberate collision: Dream needs
    `read`, `edit` and `write`, and the operator who configured a read-only agent has already said
    what those words mean on this host. Dream is *more* restricted than the agent it serves —
    the agent can act on the workspace, Dream can only touch four files.
    """

    #: What Dream needs. `bash` is absent on purpose: a Dream that can run shell commands is a
    #: Dream that can delete the workspace it is supposed to be curating notes about.
    DREAM_TOOLS = ("read", "edit", "write")

    def __init__(
        self,
        settings: Any,
        on_frame: Any = None,
        config: NativeConfig | None = None,
    ) -> None:
        self._settings = settings
        self._on_frame = on_frame
        #: The same runtime configuration the agent backend uses (docs/design.md §26). Dream is a
        #: Pi process too, so a provider the user moved to in the settings panel has to reach it
        #: as well — otherwise the first Dream after a switch fails an auth check from a process
        #: nobody is watching.
        self._config = config

    def set_config(self, config: NativeConfig | None) -> None:
        """Take the current configuration. Dream spawns per run, so this needs no restart."""
        self._config = config

    def _effective(self) -> Effective:
        return (self._config or EMPTY_CONFIG).effective(self._settings)

    def build_argv(self, *, directory: Path, model: str | None = None) -> list[str]:
        s = self._settings
        effective = self._effective()
        argv = [*s.pi_bin, "--mode", "rpc"]
        if s.pi_session_dir is not None:
            argv += ["--session-dir", str(s.pi_session_dir)]
        # Provider *and* model, mirroring the agent backend — and for the same reason it gives:
        # Pi's built-in default provider is `google`, which most people have no credential for,
        # so omitting these turns "Dream used the agent's model" into an auth error from a
        # process nobody can see.
        if effective.provider:
            argv += ["--provider", effective.provider]
        chosen = model or s.memory_model or effective.model
        if chosen:
            argv += ["--model", chosen]
        # `--tools` is explicit rather than inherited: the agent's allowlist is a statement
        # about what the *agent* may do, and Dream is a different actor with a smaller brief.
        argv += ["--tools", ",".join(self.DREAM_TOOLS)]
        if effective.needs_provider_extension and PROVIDER_EXTENSION.is_file():
            argv += ["-e", str(PROVIDER_EXTENSION)]
        return argv

    async def run(
        self,
        prompt: str,
        *,
        directory: Path,
        cwd: Path | None = None,
        timeout: float = DREAM_RUN_TIMEOUT,
    ) -> str:
        """Send the prompt and return the assembled assistant text.

        Returns when Pi reports that it has settled, or raises `asyncio.TimeoutError` past
        `timeout`. Raising on failure rather than returning a sentinel: `Dream.run` catches
        everything and leaves the cursor alone, so the only decision made here is whether to
        raise at all.

        The prompt goes to a Pi process running **in the memory directory**, so the relative
        paths in its instructions (`MEMORY.md`, `USER.md`) resolve against the files they name
        rather than against the agent's workspace. That is the whole reason Dream gets a process
        of its own instead of being a second conversation in the agent's.
        """
        from .rpc import PiProcess

        collected: list[str] = []
        #: Set when Pi says it has stopped continuing on its own.
        settled = asyncio.Event()

        def on_event(frame: dict[str, Any]) -> None:
            kind = frame.get("type")
            if kind == "message_update":
                delta = frame.get("assistantMessageEvent") or {}
                if delta.get("type") == "text_delta":
                    collected.append(delta.get("delta", ""))
            elif kind == "agent_settled":
                # The same frame the supervisor journals on, and for the same reason: it is the
                # one that means "nothing more is coming". `agent_end` fires per low-level run
                # and would return mid-conversation.
                settled.set()

        proc = PiProcess(
            self.build_argv(directory=directory),
            cwd=cwd or directory,
            env=self._child_env(),
            # Bounds *acceptance* of the prompt, matching the agent backend's meaning. The run
            # itself is bounded by `timeout` below, which is a different and much larger number.
            command_timeout=max(self._settings.pi_command_timeout, 300.0),
            on_event=on_event,
        )
        await proc.start()
        try:
            await proc.request({"type": "prompt", "message": prompt})
            # Wait for the *settled event*, not for the process to exit. Pi in RPC mode is a
            # long-lived server that keeps reading stdin after a run finishes, so `proc.wait()`
            # never returns — it hangs until the timeout kills the process, and Dream reports a
            # failure it never actually had. Found by running it for real; no fake-based test
            # could have.
            await asyncio.wait_for(settled.wait(), timeout=timeout)
        finally:
            # Always stopped, including on a timeout: a Dream process left running would hold
            # the memory files open and the next run's diff would be against a moving target.
            await proc.stop()
        return "".join(collected)

    def _child_env(self) -> dict[str, str]:
        """The same forwarding rule as the agent backend: only the named keys, only when set.

        Reimplemented rather than shared because the native backend's copy is private to it and
        extracting it would couple the two for the sake of ten lines.
        """
        import os

        env = dict(os.environ)
        for name in self._settings.pi_env_keys:
            value = os.environ.get(name)
            if value:
                env[name] = value
        effective = self._effective()
        if effective.api_key:
            # Same rule as the agent backend: the credential travels in the environment, never
            # on the command line.
            env[PROVIDER_KEY_ENV] = effective.api_key
        if effective.needs_provider_extension and PROVIDER_EXTENSION.is_file():
            path = provider_config_path(self._settings)
            if path.is_file():
                env[PROVIDER_CONFIG_ENV] = str(path)
        env.setdefault("CSD_AGENT", "1")
        env.setdefault("CSD_AGENT_PARENT", str(os.getpid()))
        env["CSD_DREAM"] = "1"
        return env


__all__ = [
    "Consolidator",
    "CURSOR_DREAM",
    "CURSOR_JOURNAL",
    "Dream",
    "DreamResult",
    "JOURNAL_MAX_ENTRIES",
    "Journal",
    "JournalEntry",
    "MemoryGit",
    "PiDreamRunner",
    "journal_cursor_key",
    "utcnow",
]
