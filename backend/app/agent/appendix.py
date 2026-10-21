"""The per-turn appendix: what the agent is told on every prompt, beyond its own code.

Five sections ride here, in one order, and the order is the argument:

1. **Memory** — `.recall.md`, regenerated from the durable files by `memory.write_recall_file`.
2. **Machines** — where the agent may go and what lives there, from an operator-authored file.
3. **The registry index** — one paragraph pointing at the generated file listing every machine
   whose connector reported itself, and which of them the user has authorized (§24). The list
   itself is deliberately *not* here: it grows with the fleet, and the prompt is paid for on
   every message.
4. **Sending files** — the outbox, where the agent leaves a file for the user (§29).
5. **Progress notes** — how to report while working, last so the instruction is the freshest
   thing in the prompt.

**Why they travel as paths plus one string, rather than as assembled text.** Pi reads
`--append-system-prompt` once, when the process starts, so a snapshot assembled here would go stale
the moment anything changed. The extension (`pi/appendix.ts`) re-reads these on *every* prompt
through `before_agent_start`, which is what lets a Dream, a `MACHINES.md` edit, a newly authorized
host, or a settings change land on the next message instead of on the next process. The text is not
built in the extension either — two languages building one prompt is two answers to "what is the
agent told".

This is not memory and does not live in `memory.py`: memory is one section of it.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .memory import RECALL_FILE, recall_prompt

#: How the agent should report while it works — a **structural checkpoint**, not a feed.
#:
#: The wording follows nanobot's `templates/agent/progress_notes.md`, which the operator wrote
#: upstream: the same instruction means an agent behaves the same way on either side of the seam,
#: and the two facts it states are the two a model gets wrong unaided. Both are about *where* the
#: line goes, because that is what decides whether a person sees anything at all — nanobot's own
#: switch defaulted to true for a long time while delivering nothing, and the cause was a model
#: that had been told to be terse in one line of its contract and to plan out loud in another.
PROGRESS_NOTES = """\
## Progress notes

When a turn calls tools several times in a row, write one short line in the same message as that
stage's tool calls, then make the calls. Keep it brief, in the user's language — for example
"reading the deploy config" or "running the smoke test".

- Text written *after* the tool calls, or in a separate turn, never reaches the user. When the
  line is missing, the user watches a blank screen.
- This is not an answer. Never put results, conclusions or the final answer in a progress note —
  those belong in the final response.
"""

#: The preamble that turns an operator's prose into an instruction. The names and paths are theirs;
#: what the agent should *do* with them is the same on every deployment, so it is stated here
#: rather than demanded of every operator in every `MACHINES.md`.
MACHINES_PREAMBLE = """\
## Machines you can reach

You drive a coding agent on another machine the way your user does — `ssh <host> 'cd <repo> && \
claude -p "…"'` (or `codex`, whichever is installed there, per the notes below). Use the hosts named
here; never invent one, and say so plainly if what you need is not in this list.
"""

#: The index into the **generated** registry file (docs/design.md §24), which is where the
#: machines that reported themselves live — including which of them the user has authorized and
#: the exact `ssh` line for each.
#:
#: One short section rather than the file's contents, on purpose: the registry is as long as the
#: user's fleet, and the prompt is paid for on every message. The file is one `read` away, and the
#: one question it cannot answer — what is running *now* — is the tool's job.
REGISTRY_INDEX = """\
## Registered coding agents

The machines whose connectors reported to this dashboard are listed in `{path}` (generated — do \
not edit). Read it before driving a coding agent on another machine: it says which hosts the user \
has authorized and gives the `ssh` line for each.{tool_note}

A host that is not authorized is **a request to the user**, not a puzzle to solve: say so, and \
never invent a credential or an address.
"""

#: The half of the index that is only true when the tool is loaded — a sentence promising a tool
#: that is not there is worse than no sentence, because the agent will call it and fail.
REGISTRY_TOOL_NOTE = " For live host and running-session state, call the `csd_registry` tool."

#: Where the agent hands a file to the user (docs/design.md §29). The path is per conversation
#: and is formatted in when the process starts; what matters here is the *contract*: write into
#: the directory, and the file is delivered when the turn ends.
ATTACHMENTS_NOTE = """\
## Sending files to the user

The user sees this conversation, not your filesystem. To hand them a file — an image, a report,
a build artifact — write it into `{outbox}` (create the directory if it does not exist).
Everything there is delivered as an attachment when the turn ends, and the directory is then
emptied, so a file you want to send must be written afresh each time.

- The file in the outbox *is* the delivery. Do not paste its path into your reply instead.
- Images are shown inline; everything else arrives as a downloadable file.
- Only files you put there are sent. Scratch files elsewhere in your workspace stay yours.
"""

#: The same instruction, compressed to one line, for a backend whose agent has no per-turn
#: appendix: it rides at the end of each prompt and is stripped from the transcript on display.
ATTACHMENTS_CHANNEL_LINE = (
    "[channel] To send the user a file, write it into {outbox}; it is delivered when the turn "
    "ends."
)


def attachments_section(outbox: Path) -> str:
    return ATTACHMENTS_NOTE.format(outbox=outbox)


def attachments_channel_line(outbox: Path) -> str:
    return ATTACHMENTS_CHANNEL_LINE.format(outbox=outbox)


def registry_index(path: Path | None, *, tool: bool) -> str | None:
    """The index section, or `None` when there is no workspace to keep the file in."""
    if path is None:
        return None
    return REGISTRY_INDEX.format(
        path=path, tool_note=REGISTRY_TOOL_NOTE if tool else ""
    )


def machines_section(path: Path | None) -> str | None:
    """The operator's machine notes as a prompt section, or `None` when there are none.

    A missing or unreadable file is "the agent has no other machines", not a broken turn: the
    first run of any deployment has no notes, and an agent that can only work locally is still a
    working agent.
    """
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not text:
        return None
    return f"{MACHINES_PREAMBLE}\n{text}"


def appendix_files(settings: Settings) -> list[Path]:
    """The files the per-turn extension should read, in order.

    Listed **whether or not they exist yet**, deliberately: this list reaches the Pi process in its
    environment at spawn time, so a path omitted because the file was missing then would stay
    invisible until the process restarted — and the moment it appears (`MACHINES.md` written by
    hand, the first Dream filling the memory) is exactly the moment someone is watching. The
    extension treats an unreadable path as "nothing to say", which is what a missing file deserves
    anyway.
    """
    files: list[Path] = []
    memory_dir = settings.resolved_memory_dir
    if memory_dir is not None:
        files.append(memory_dir / RECALL_FILE)
    machines = settings.resolved_machines_file
    if machines is not None:
        files.append(machines)
    return files


def appendix_text(settings: Settings, *, outbox: Path | None = None) -> str | None:
    """The whole appendix as one string, or `None` when there is nothing to say.

    Only for the **fallback path**: a deployment whose extension file is missing still has to get
    its memory, so the backend passes this through `--append-system-prompt`. It is a start-up
    snapshot and always was — which is why it is the fallback and not the mechanism.

    `tool=False` for the registry index here: this path has no extension loaded, so there is no
    `csd_registry` to call. The file is still named, because the file is still there.

    `outbox` is per conversation and the command line is built per process, so this path can
    still name the exact directory even though it is a snapshot. Without one (a config check,
    which starts no turn) the section is omitted rather than promising a path that is not real.
    """
    parts: list[str] = []
    memory_dir = settings.resolved_memory_dir
    if memory_dir is not None:
        recalled = recall_prompt(memory_dir)
        if recalled:
            parts.append(recalled)
    machines = machines_section(settings.resolved_machines_file)
    if machines:
        parts.append(machines)
    index = registry_index(settings.resolved_registry_file, tool=False)
    if index:
        parts.append(index)
    if outbox is not None:
        parts.append(attachments_section(outbox))
    if settings.agent_progress:
        parts.append(PROGRESS_NOTES)
    return "\n\n".join(parts) if parts else None


__all__ = [
    "ATTACHMENTS_CHANNEL_LINE",
    "ATTACHMENTS_NOTE",
    "MACHINES_PREAMBLE",
    "PROGRESS_NOTES",
    "REGISTRY_INDEX",
    "appendix_files",
    "appendix_text",
    "attachments_channel_line",
    "attachments_section",
    "machines_section",
    "registry_index",
]
