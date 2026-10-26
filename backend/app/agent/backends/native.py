"""The native backend: a Pi process this service runs itself.

Kept because a local agent with no gateway to install is genuinely useful, and because it
proves the seam is real — this and `nanobot.py` disagree about almost every mechanism (a
subprocess per conversation versus one socket multiplexing many) while agreeing completely on
what a conversation and an event are.

It is **not** the main event any more. The point of the product is to be a channel onto an
agent you already run somewhere; this is the option for when there isn't one.

The Pi transport itself lives in `../rpc.py` unchanged — it was written and tested as a
standalone protocol client, and it stays one. What moves here is the *policy*: which command
line to run, and how a Pi event becomes a normalized one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from ...config import Settings
from ...schemas import AgentRole, cap_string
from .. import attachments
from ..appendix import (
    PROGRESS_NOTES,
    appendix_files,
    appendix_text,
    attachments_section,
    registry_index,
)
from ..legacy import read_legacy_sessions
from ..memory import write_recall_file
from ..native_config import EMPTY as EMPTY_CONFIG
from ..native_config import (
    PROVIDER_CONFIG_ENV,
    PROVIDER_EXTENSION,
    PROVIDER_KEY_ENV,
    Effective,
    NativeConfig,
    provider_config_path,
    provider_payload,
)
from ..rpc import PiCommandError, PiProcess, PiTimeout, resolve_executable
from .base import (
    AgentBackend,
    BackendError,
    BackendStatus,
    BackendUnavailable,
    Delta,
    EventSink,
    Message,
    Notice,
    Status,
    ToolCall,
)


logger = logging.getLogger("csd.agent.native")

#: How long to wait for Pi to echo a prompt back before giving up on its message id.
#:
#: Generous, because exceeding it costs almost nothing — the prompt was already accepted, and the
#: only consequence is that the page does not draw the user's own message early. Compare
#: `pi_command_timeout`, which bounds *acceptance*: this bounds a step that happens immediately
#: after it, and a timeout here must never be mistaken for the prompt having failed.
ECHO_TIMEOUT = 10.0

#: How long Pi's manual `compact` may take before it is abandoned. Unlike an ordinary command,
#: this one only answers once the summarization model call has finished, so it is bounded like
#: Dream rather than like an acknowledgement.
COMPACT_TIMEOUT = 300.0

#: How long `--list-models` gets. It reads local catalogues and exits; a process still running
#: after this has found something to wait on, and a settings panel should not wait with it.
CHECK_TIMEOUT = 20.0

#: The Pi extension that re-reads the appendix on **every prompt** (`before_agent_start`), so a
#: Dream — or an edit to `MACHINES.md`, or a settings change — reaches the agent on its next
#: message instead of on its next process. Shipped in this package beside the backend that loads
#: it; the fallback when it is absent is the start-up-only `--append-system-prompt`.
APPENDIX_EXTENSION = Path(__file__).resolve().parent.parent / "pi" / "appendix.ts"

#: The registry tool: a Pi extension that answers "which hosts, and what is running on them"
#: on demand, by reading this service's database read-only from the agent's own machine
#: (docs/design.md §24). Separate from the appendix because it is a *tool*, not prompt text:
#: the appendix points at the generated file, and this is the live half the file cannot be.
REGISTRY_EXTENSION = Path(__file__).resolve().parent.parent / "pi" / "registry.ts"

#: The provider override (docs/design.md §26): the one thing `--provider`/`--model` cannot say
#: is "this provider lives at another address, or is one Pi has never heard of". Loaded only
#: when the stored configuration needs it, so a deployment that configures nothing here runs
#: exactly the command line it ran before this file existed. The path, the environment variable
#: names and the generated file's location live in `native_config`, which Dream shares.

#: The directory `app` resolves from — what the registry tool's subprocess needs as its cwd and
#: `PYTHONPATH`. Derived rather than configured: a setting for it would be a second answer to
#: "where is this package", and the wrong one is a tool that silently cannot import.
_BACKEND_DIR = Path(__file__).resolve().parents[3]


def _extract_text(content: Any) -> str:
    """Flatten Pi's `content` into plain text.

    A string for user messages, an array of typed blocks for assistant and tool messages
    (`{type: text|thinking|toolCall}`). Only text blocks carry prose: thinking is scaffolding for
    the answer rather than part of it, and a `toolCall` block is represented by the tool row that
    follows — its `arguments` are carried onto that row by `_entries_to_messages`, which is what
    stops the command line from being visible on screen and then lost.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)


def _has_tool_call(message: dict[str, Any]) -> bool:
    """Whether this assistant message carries tool calls — which makes its text a progress note.

    Pi records the assistant's prose and the calls it made in **one** entry: the text blocks and
    the `toolCall` blocks share a `content` array. So "is this a checkpoint rather than an
    answer" is not a guess about wording or length — it is whether that entry called a tool, and
    that is exactly the field nanobot points its own model at when it asks for one line per stage
    (`templates/agent/progress_notes.md`).

    The last assistant entry of a run has no tool calls, which is what makes the final answer
    distinguishable from the notes that came before it.
    """
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "toolCall" for block in content
    )


def _entry_time(entry: dict[str, Any]) -> str | None:
    """Pi's timestamp for a session entry, or `None` if it is missing or malformed.

    The entry's own `timestamp`, not `message.timestamp`: Pi writes both for a message, and the
    entry is the record of *when the transcript holds it*, which is what the page is showing.
    """
    stamp = entry.get("timestamp")
    return stamp if isinstance(stamp, str) and stamp else None


#: Entry types that are not conversation. Pi records model changes, thinking-level changes,
#: session metadata, usage and compaction summaries in the same tree; the transcript is the
#: `message` entries among them.
_NON_MESSAGE_ENTRIES = frozenset(
    {"session", "session_info", "model_change", "thinking_level_change", "usage", "compaction",
     "branch_summary", "custom", "label", "custom_message", "context_edit"}
)


def _entries_to_messages(entries: list[dict[str, Any]]) -> list[Message]:
    """Translate Pi's session entries into the backend-neutral `Message` list.

    Two passes, because the two halves of a tool call are recorded apart: the assistant message
    holds the `toolCall` block (with `arguments` — the command line) and a later `toolResult`
    entry holds the outcome. The first pass indexes arguments by call id; the second emits rows,
    so the tool row carries **both** what was run and what came back.

    That pairing is the point rather than a detail. Before this, arguments existed only on the
    live `ToolCall` frame, which is never recorded anywhere — so a re-read conversation showed
    that something failed without showing what it was, and memory consolidation learned from it.
    """
    args_by_call: dict[str, Any] = {}
    for entry in entries:
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        if message.get("role") != "assistant":
            continue
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "toolCall":
                call_id = block.get("id")
                if isinstance(call_id, str):
                    args_by_call[call_id] = block.get("arguments")

    messages: list[Message] = []
    for entry in entries:
        if entry.get("type") != "message":
            continue
        entry_id = entry.get("id")
        message = entry.get("message") or {}
        role = message.get("role")
        text = _extract_text(message.get("content"))

        if role == "user":
            if text.strip():
                messages.append(
                    Message(
                        role=AgentRole.user,
                        content=cap_string(text),
                        id=entry_id,
                        created_at=_entry_time(entry),
                    )
                )
        elif role == "assistant":
            # An assistant turn that was nothing but tool calls has no prose; its record is the
            # tool rows. Emitting an empty one would put a blank bubble in the transcript.
            if text.strip():
                messages.append(
                    Message(
                        role=AgentRole.assistant,
                        content=cap_string(text),
                        id=entry_id,
                        created_at=_entry_time(entry),
                        # Text that shared an entry with tool calls is a checkpoint, not the
                        # answer — the same fact the live message carries as `progress=True`, so a
                        # refreshed transcript renders it as the same kind of row.
                        progress=_has_tool_call(message),
                    )
                )
        elif role == "toolResult":
            call_id = message.get("toolCallId")
            messages.append(
                Message(
                    role=AgentRole.tool,
                    content=cap_string(text) if text.strip() else "(no output)",
                    id=entry_id,
                    tool_name=message.get("toolName"),
                    tool_call_id=call_id if isinstance(call_id, str) else None,
                    args=args_by_call.get(call_id) if isinstance(call_id, str) else None,
                    is_error=bool(message.get("isError")),
                    created_at=_entry_time(entry),
                )
            )
    return messages


def _session_id_of(path: Path) -> str | None:
    """The session id in a session file's header, or `None` if that is not what the file is.

    Every Pi session file opens with `{"type": "session", "id": ...}`. Reading exactly one line
    per candidate is what makes scanning the session directory affordable — which the reader now
    does rather than globbing on a name, because a conversation can be spread over several files
    (`_session_files_of`).
    """
    try:
        with path.open("rb") as handle:
            first = handle.readline()
    except OSError:
        return None
    try:
        header = json.loads(first.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(header, dict) and header.get("type") == "session":
        value = header.get("id")
        return value if isinstance(value, str) else None
    return None


def _read_session_entries(path: Path) -> list[dict[str, Any]]:
    """Every entry in a Pi session file, unparsed beyond being JSON objects.

    **Bytes, split on `b"\\n"` alone** — the discipline `rpc.py` keeps, for the same reason: a
    `\\r` is legal *inside* a JSON string (a shell transcript, a diff), and text mode would both
    translate it and split on it, corrupting the record.

    An unparsable record is skipped rather than raised. The ordinary cause is reading the file
    while Pi is appending to it, which tears the last line and nothing else; the entry will be
    there on the next read. Skipping is also right in the abnormal case, because a conversation
    with a hole in it still beats no conversation.
    """
    try:
        with path.open("rb") as handle:
            chunks = handle.read().split(b"\n")
    except OSError as exc:
        logger.warning("could not read the session file %s: %s", path, exc)
        return []

    entries: list[dict[str, Any]] = []
    for chunk in chunks:
        record = chunk.rstrip(b"\r").strip()
        if not record:
            continue
        try:
            parsed = json.loads(record.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.debug("skipping an unparsable record in %s", path)
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


class NativeBackend(AgentBackend):
    """One Pi process per conversation, started lazily on first use."""

    name = "native"

    def __init__(
        self,
        settings: Settings,
        on_event: EventSink,
        config: NativeConfig | None = None,
    ) -> None:
        self._settings = settings
        self._on_event = on_event
        self._config = config if config is not None else EMPTY_CONFIG
        self._procs: dict[str, PiProcess] = {}
        self._streaming: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        #: Conversations whose live process was built from a configuration that has since
        #: changed. Their next prompt replaces the process; a running turn is never interrupted
        #: (docs/design.md §26). Cleared per conversation as the replacement happens.
        self._restart_pending: set[str] = set()
        #: Set while a prompt is being sent, and resolved when Pi echoes the user's message
        #: back. That echo is how we learn the message has been *recorded*, which is what lets
        #: `send` return its id rather than guessing at one.
        self._user_echo: dict[str, asyncio.Future[None]] = {}
        #: Newest message id at the last idle-compaction attempt, per conversation. Pi refuses
        #: to compact a session that is small or already compacted; without this, an idle
        #: conversation would ask once a minute and log the same refusal forever.
        self._compact_attempted: dict[str, str] = {}
        # The file has to exist before the first process starts, and `build_argv`/`_child_env`
        # are pure reads. Constructed eagerly for that reason: the supervisor builds every
        # backend at startup, which is the one moment this file can be written without a race.
        self._write_provider_config()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        # Nothing to do: processes are per conversation and start on demand. A backend that
        # eagerly started something here would make merely opening the page spawn a process.
        return

    async def stop(self) -> None:
        procs = list(self._procs.values())
        self._procs.clear()
        self._streaming.clear()
        # Every process is going away anyway, so the replacement a config change asked for has
        # already happened by the time anything starts again.
        self._restart_pending.clear()
        # Fail anything still waiting on an echo. A `send` blocked on a process that is going
        # away would otherwise sit there for the full `ECHO_TIMEOUT` before giving up.
        for pending in self._user_echo.values():
            if not pending.done():
                pending.cancel()
        self._user_echo.clear()
        if procs:
            await asyncio.gather(*(p.stop() for p in procs), return_exceptions=True)

    async def refresh_context(self, conversation_id: str) -> None:
        """Make the running agent see the memory the Dream just wrote.

        Two mechanisms, and only one of them runs.

        **Normally: nothing to do.** The extension in `../pi/appendix.ts` re-reads the appendix on
        every prompt, so a process that is already running picks the change up on the next
        message — better than a restart, which would also have to wait for an idle moment.

        **Fallback: restart the process.** Without the extension the memory arrives through
        `--append-system-prompt`, which Pi reads once, when the process starts; the RPC surface
        has no reload command. A restart is then the only way to rebuild it, and that is the whole
        reason this method exists.

        The caller applies the idle guard — a process stopped mid-run abandons a turn — and this
        method repeats it for the window between that check and this call. The conversation
        itself is untouched: the process owns no state the session file does not, and the next
        prompt resumes the same `--session-id`.
        """
        if self._appendix_extension() is not None:
            return
        if conversation_id in self._streaming or conversation_id in self._user_echo:
            # A turn started in the gap. Its memory is one turn stale, not broken; the next
            # process start picks the update up.
            return
        proc = self._procs.pop(conversation_id, None)
        if proc is not None:
            await proc.stop()

    async def compact_when_idle(
        self, conversation_id: str, *, idle_seconds: float
    ) -> bool:
        """Ask Pi to compact an idle conversation, if there is anything to compact.

        The supervisor owns the clock (`CSD_AGENT_IDLE_COMPACT_MINUTES`); this method owns the
        Pi-specific rules, the same split as Dream's schedule versus its cursor rules:

        - **Never start a process for this.** Compacting a conversation nobody is using would
          spawn a Node process and a model call for a page that is not even open. Only a process
          that is already alive is compacted, which also means a quiet deployment pays nothing.
        - **Never interrupt a turn.** Pi's `compact` aborts the current operation first, so both
          the caller's idle check and this one have to see "not streaming".
        - **One attempt per new message.** Pi refuses with "Nothing to compact (session too
          small)" or "Already compacted"; without remembering which message the last attempt
          ended at, an idle conversation would ask every scan and log a refusal a minute.

        Returns whether Pi accepted the command. A refusal is not an error — it means there is
        nothing worth shrinking, which is the normal state of a short conversation.
        """
        if idle_seconds <= 0:
            return False
        proc = self._procs.get(conversation_id)
        if proc is None or not proc.alive:
            return False
        if conversation_id in self._streaming or conversation_id in self._user_echo:
            return False

        newest = self._newest_message_id(conversation_id)
        if newest is None or self._compact_attempted.get(conversation_id) == newest:
            return False
        # Recorded before the call: a failure must not become a retry loop, and a new message is
        # what re-arms it.
        self._compact_attempted[conversation_id] = newest

        try:
            await proc.request({"type": "compact"}, timeout=COMPACT_TIMEOUT)
        except PiCommandError as exc:
            logger.info("pi declined to compact %s: %s", conversation_id, exc)
            return False
        except (PiTimeout, RuntimeError, OSError) as exc:
            logger.warning("compacting %s failed: %s", conversation_id, exc)
            return False
        logger.info("pi compacted the idle conversation %s", conversation_id)
        return True

    def _newest_message_id(self, conversation_id: str) -> str | None:
        """Pi's id for the newest message entry, or `None` when nothing has been said.

        Read from the same files `history()` reads, so it costs one parse and no round trip. The
        id is used only as a change token — "has anything new happened since the last attempt" —
        which is why it does not need to be ordered or interpreted.
        """
        for entry in reversed(self._entries_on_disk(conversation_id)):
            if entry.get("type") != "message":
                continue
            entry_id = entry.get("id")
            return entry_id if isinstance(entry_id, str) else None
        return None

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        return self._locks.setdefault(conversation_id, asyncio.Lock())

    # ------------------------------------------------------------------ #
    # runtime configuration (docs/design.md §26)
    # ------------------------------------------------------------------ #
    def apply_config(self, config: NativeConfig) -> None:
        """Take a new configuration, and arrange for the next process to use it.

        **Nothing is stopped here.** Pi's `stop()` can wait out a 5 s grace period, and a save is
        a request the page is waiting on; more importantly, stopping a process mid-turn would
        abandon the answer being written. Each live conversation is marked instead, and `_ensure`
        replaces its process on the next prompt — the first moment a new command line could have
        mattered. See docs/design.md §26.
        """
        self._config = config
        self._write_provider_config()
        self._restart_pending.update(self._procs)

    def _effective(self) -> Effective:
        return self._config.effective(self._settings)

    def _provider_config_path(self) -> Path:
        return provider_config_path(self._settings)

    def _write_provider_config(self) -> None:
        """Rewrite the file the provider extension reads, or remove it when nothing needs it.

        Atomic (temp file + replace) because a Pi process may be reading it at this exact
        moment, and a half-written JSON file would look like a provider that vanished. The file
        contains no credential — see `native_config.provider_payload`.
        """
        path = self._provider_config_path()
        payload = provider_payload(self._effective(), PROVIDER_KEY_ENV)
        try:
            if payload is None:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(path)
        except OSError as exc:
            # Reported, not raised: a save that could not write this file leaves the agent on
            # its previous provider, which is the state the panel's next read will show.
            logger.warning("could not write the provider configuration %s: %s", path, exc)

    def _provider_extension(self) -> Path | None:
        """The provider extension, only when there is something for it to register."""
        if not self._effective().needs_provider_extension:
            return None
        return PROVIDER_EXTENSION if PROVIDER_EXTENSION.is_file() else None

    async def check_config(self) -> tuple[bool, str]:
        """Whether Pi resolves the configured provider and model, without spending a token.

        `--list-models <pattern>` builds the catalogue the next process would see and exits —
        the same code path `--provider`/`--model` resolution uses, so a name Pi does not know
        fails here rather than at the first message. `--offline` keeps it from reaching for a
        remote catalogue, which would make the answer depend on the network rather than on the
        configuration.

        **It says nothing about the credential.** A key is only exercised by a request; this
        route's job is to tell the two apart in its own wording.
        """
        s = self._settings
        effective = self._effective()
        pattern = effective.model or effective.provider or ""
        argv = [*s.pi_bin, "--offline", "--list-models"]
        if pattern:
            argv.append(pattern)
        provider = self._provider_extension()
        if provider is not None:
            argv += ["-e", str(provider)]

        try:
            proc = await asyncio.create_subprocess_exec(
                resolve_executable(argv[0]),
                *argv[1:],
                cwd=str(s.pi_workspace) if s.pi_workspace else None,
                env=self._child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return False, f"could not start Pi: {exc}"

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            # A hung `--list-models` is itself the answer: Pi found something to wait on, and
            # making the page wait with it helps nobody.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return False, f"Pi did not answer within {CHECK_TIMEOUT:.0f}s"

        text = out.decode("utf-8", "replace").strip()
        error = err.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            return False, error or text or f"Pi exited with status {proc.returncode}"
        if text.startswith("No models matching"):
            return False, text.splitlines()[0]
        lines = [line for line in text.splitlines() if line.strip()]
        # The first line is the table header, so a match is a row below it.
        found = len(lines) > 1
        return (
            found,
            f"Pi resolves {pattern}" if found else f"Pi knows nothing matching {pattern!r}",
        )

    def build_argv(self, *, session_id: str | None, title: str) -> list[str]:
        """The command line for one conversation's Pi.

        `--session-id`, not `--session`. The two look interchangeable and are not: `--session`
        resolves a **path or partial UUID** to an existing file, while `--session-id` takes an
        exact project session id and *creates one if missing* — which is precisely "resume this
        conversation, or start it". The old code passed `--session` a value it never received, so
        neither behaviour ever ran and every restart began a blank conversation.

        Pi owns the file; we own the identity. The id must satisfy Pi's own rules (alphanumeric
        plus `-_.`, starting and ending alphanumeric), which `new_conversation`'s `c_<hex>` does.

        Provider and model are passed only when configured. Pi's own default provider is
        `google`, which most people have no credential for — so leaving these unset produces
        a confusing upstream auth error rather than "no model configured". They come from the
        stored configuration with the environment as the fallback (docs/design.md §26), which
        is why a change made in the settings panel needs a new process to take effect rather
        than a new service: the command line is built here, once per process.

        **The memory rides on the command line, or on a per-prompt hook.** Pi reads
        `--append-system-prompt` once, when the process starts, which is why a running agent used
        to keep the memory it booted with. The extension in `../pi/recall.ts` re-reads it on every
        prompt instead, so a Dream lands on the next message; the backend regenerates the file it
        reads and names it in the child environment. When that file is missing, the fallback is
        the old start-up append — no duplication, because only one of the two ever runs.

        Either way the text arrives as a *system prompt*, never as a message: a message would make
        the agent's memory look like something the user said, and would be journaled as such. An
        operator who passes their own `--append-system-prompt` in `CSD_PI_ARGS` owns that decision
        and ours is skipped rather than stacked underneath it.

        **Skills are passed by path, not by content.** `memory/skills` is outside every location
        Pi scans, so Dream's fourth writable path would otherwise be invisible. `--skill` is
        explicit (not project-trust-gated), and Pi's progressive disclosure keeps only each
        skill's name and description in the prompt until the body is actually read — which is
        both cheaper and kinder to the prefix cache than inlining full instruction sets.
        """
        s = self._settings
        # The stored configuration first, the environment where it is silent — the same merge
        # the settings panel shows (docs/design.md §26).
        effective = self._effective()
        argv = [*s.pi_bin, "--mode", "rpc"]
        if s.pi_session_dir is not None:
            argv += ["--session-dir", str(s.pi_session_dir)]
        if session_id:
            argv += ["--session-id", session_id]
        argv += ["--name", title[:80]]
        if effective.provider:
            argv += ["--provider", effective.provider]
        if effective.model:
            argv += ["--model", effective.model]
        if effective.tools:
            argv += ["--tools", ",".join(effective.tools)]
        if effective.trust_project_files:
            # Only the positive flag: Pi's default in RPC mode is already "not trusted" unless
            # the workspace has nothing trust gates, and passing `--no-approve` would turn a
            # harmless default into a decision this form made on the operator's behalf.
            argv += ["--approve"]
        provider = self._provider_extension()
        if provider is not None:
            argv += ["-e", str(provider)]
        memory_dir = s.resolved_memory_dir
        if memory_dir is not None:
            skills_dir = memory_dir / "skills"
            if skills_dir.is_dir():
                argv += ["--skill", str(skills_dir)]
        # The appendix is **not** gated on having a memory: the progress instruction and the
        # machines file have nothing to do with `MEMORY.md`, and a deployment with no workspace
        # configured should still get both (docs/design.md §23).
        if self._appendix_extension() is not None:
            if memory_dir is not None:
                write_recall_file(memory_dir)
            argv += ["-e", str(APPENDIX_EXTENSION)]
        elif not any(arg.startswith("--append-system-prompt") for arg in s.pi_args):
            # The extension is missing (a broken deployment, not a configured one), so the memory
            # and the notes ride in on the command line instead. Both are snapshots from here on.
            # The outbox is still exact: the command line is built per process, and this process
            # is one conversation.
            outbox = None
            if session_id is not None:
                try:
                    outbox = attachments.outbox_dir(s, session_id)
                except OSError:
                    logger.warning(
                        "could not prepare an outbox for %s", session_id, exc_info=True
                    )
            appendix = appendix_text(s, outbox=outbox)
            if appendix:
                argv += ["--append-system-prompt", appendix]
        # Independent of the appendix on purpose: the tool answers a question the prompt has
        # nothing to do with, and a deployment that lost one extension file should not also
        # lose the other capability.
        registry = self._registry_extension()
        if registry is not None:
            argv += ["-e", str(registry)]
        argv += list(s.pi_args)
        return argv

    def _appendix_extension(self) -> Path | None:
        """The per-turn appendix extension, or `None` when it is not installed.

        Checked rather than assumed: a deployment that lost the file should fall back to the
        start-up append, not run an agent that is never told where it may work. Nothing else
        decides between the two paths, so this is the single place the choice is made.
        """
        return APPENDIX_EXTENSION if APPENDIX_EXTENSION.is_file() else None

    def _registry_extension(self) -> Path | None:
        """The registry tool extension, or `None` when it is not installed.

        Checked rather than assumed, like the appendix: a deployment missing this file should
        get an agent that still works, and the appendix index drops the sentence that promises
        the tool — a prompt naming a tool that does not exist is worse than a shorter prompt.
        """
        return REGISTRY_EXTENSION if REGISTRY_EXTENSION.is_file() else None

    def has_per_turn_appendix(self) -> bool:
        """Whether this agent re-reads its appendix on every prompt.

        Public because the layer above has one thing to do differently for such a backend:
        regenerate the appendix file when a run settles, so a hand-edit of `MEMORY.md` reaches
        the agent too. Optional on the seam — a backend that does not implement it is asked
        through `getattr` and treated as "no".
        """
        return self._appendix_extension() is not None

    def _child_env(self, conversation_id: str | None = None) -> dict[str, str]:
        """The environment the Pi process gets.

        Pi reads provider credentials from its own environment, so the question is whether
        *this service's* environment has them. Under systemd it does not unless the
        EnvironmentFile carries them — and a key that works in your shell but not in the unit
        produces "no API key found for deepseek" from a process you cannot see, which reads
        as a broken feature rather than a missing variable.

        Only the named keys are forwarded, and only when set: an inherited `os.environ` would
        hand Pi everything this service has, including `CSD_INGEST_TOKEN`.
        """
        env = dict(os.environ)
        for name in self._settings.pi_env_keys:
            value = os.environ.get(name)
            if value:
                env[name] = value
        effective = self._effective()
        if effective.api_key:
            # The one credential this service stores, handed over in the environment rather than
            # on the command line: env is readable only by the same user, argv by everyone.
            env[PROVIDER_KEY_ENV] = effective.api_key
        provider = self._provider_extension()
        if provider is not None:
            path = self._provider_config_path()
            if path.is_file():
                env[PROVIDER_CONFIG_ENV] = str(path)
            else:
                logger.warning(
                    "the provider configuration %s is missing; Pi will use its own providers",
                    path,
                )
        env.setdefault("CSD_AGENT", "1")
        # The ownership marker is scoped to this direct child. A `pi` run the personal agent
        # spawns inherits both values, but its ppid is the personal agent, not this backend —
        # so the reporter knows it is a coding run and reports it (§31).
        env.setdefault("CSD_AGENT_PARENT", str(os.getpid()))
        if self._appendix_extension() is not None:
            # Which files make up the appendix is *this backend's* business (the generated memory
            # appendix, the operator's machines file), so they are handed over rather than
            # re-derived in TypeScript — and as paths rather than text, because the extension
            # re-reads them per prompt and a snapshot in the environment could never notice an edit.
            files = appendix_files(self._settings)
            if files:
                env["CSD_AGENT_APPENDIX"] = ",".join(str(path) for path in files)
            if self._settings.agent_progress:
                # The one section that is a fixed string rather than a file: it never changes, so
                # there is nothing to re-read and nothing that can go stale.
                env["CSD_AGENT_PROGRESS_NOTES"] = PROGRESS_NOTES
            # The index into the generated registry file (§24). A string rather than a path,
            # because the section *is* one line and what it points at is written by us.
            index = registry_index(
                self._settings.resolved_registry_file,
                tool=self._registry_extension() is not None,
            )
            if index:
                env["CSD_AGENT_REGISTRY_NOTE"] = index
            if conversation_id is not None:
                # **Per conversation**, which is why this one is read here rather than in
                # `appendix_files`: the outbox belongs to the conversation the process is
                # serving, and a path shared by every thread would let two conversations
                # deliver each other's files (docs/design.md §29). `check_config` starts a
                # process that serves no conversation and leaves this unset.
                #
                # A directory that cannot be created omits the section rather than failing the
                # process: the agent still answers, it just has no way to hand a file over.
                try:
                    outbox = attachments.outbox_dir(self._settings, conversation_id)
                except OSError:
                    logger.warning(
                        "could not prepare an outbox for %s", conversation_id, exc_info=True
                    )
                else:
                    env["CSD_AGENT_ATTACHMENTS_NOTE"] = attachments_section(outbox)
        if self._registry_extension() is not None:
            # What the registry tool needs to answer on its own: the interpreter this service is
            # already running under, the database, and the directory `app` resolves from. Paths
            # rather than a token, because the tool reads a local file — nothing here is a
            # credential, and nothing here can write.
            env["CSD_AGENT_PYTHON"] = sys.executable
            env["CSD_AGENT_DB_PATH"] = str(self._settings.db_path)
            env["CSD_AGENT_BACKEND_DIR"] = str(_BACKEND_DIR)
        return env

    async def _ensure(self, conversation_id: str) -> PiProcess:
        """The Pi process for this conversation, started if it is not running.

        The conversation id **is** the Pi session id, so a restarted process resumes the same
        conversation rather than opening a blank one. That is the whole reason the id is a
        `--session-id` argument rather than something we track on the side.

        There is no "is the session file still there" check any more, and its absence is the
        point: the file is Pi's, found by Pi, from an id it validated. A path we held separately
        could go stale in ways the id cannot.
        """
        async with self._lock(conversation_id):
            proc = self._procs.get(conversation_id)
            if proc is not None and proc.alive:
                if conversation_id not in self._restart_pending:
                    return proc
                # A settings save changed what this process should have been started with.
                # Replace it here, inside the lock, so the prompt about to be sent cannot reach
                # the old command line. The transcript is Pi's session file, so nothing is lost.
                self._restart_pending.discard(conversation_id)
                await proc.stop()
                self._procs.pop(conversation_id, None)
            else:
                # A dead process is not holding a stale configuration; the next start reads the
                # current one anyway.
                self._restart_pending.discard(conversation_id)

            proc = PiProcess(
                self.build_argv(session_id=conversation_id, title="agenest"),
                cwd=self._settings.pi_workspace,
                env=self._child_env(conversation_id),
                on_event=lambda frame: self._handle(conversation_id, frame),
                command_timeout=self._settings.pi_command_timeout,
            )
            await proc.start()
            self._procs[conversation_id] = proc

            # Ask Pi what it actually settled on: a model pattern can resolve or fall back,
            # and the header should show the truth rather than what was requested.
            try:
                state = await proc.request({"type": "get_state"})
            except Exception:
                logger.exception("could not read pi state for %s", conversation_id)
            else:
                data = state.get("data") or {}
                model = data.get("model") or {}
                self._fire(
                    conversation_id,
                    Status(provider=model.get("provider"), model=model.get("id")),
                )
            return proc

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    async def new_conversation(self) -> str:
        """Mint the conversation id, which is also Pi's session id.

        Shaped `c_<hex>` for two reasons that happen to agree. The `c_` prefix keeps a
        conversation id apart from a thread id (`t_`) in logs, and the whole thing matches Pi's
        own rule for a session id — alphanumeric plus `-_.`, starting and ending alphanumeric —
        which is what lets us pass it straight to `--session-id` and have Pi find or create the
        transcript itself.

        Nothing is created here. Pi makes the session when the first prompt arrives, so opening a
        thread nobody has spoken to leaves no trace on the agent.
        """
        return f"c_{secrets.token_hex(12)}"

    async def attach(self, conversation_id: str) -> None:
        # Nothing to subscribe to. `_ensure` starts the process on the next send; starting it
        # here would spawn a process for a thread nobody has spoken to.
        return

    async def send(self, conversation_id: str, text: str) -> Message | None:
        """Deliver a prompt, and return the user's message as Pi recorded it.

        The return value exists so the caller can hand the page a **real id** for the message it
        just sent. The page draws that row as soon as the POST answers — which is what makes the
        composer feel instant — and then meets the same message again in a re-fetched history, so
        the two have to be recognisably one message. Pi owns that identity, so we ask Pi rather
        than inventing one and reconciling later.

        Returns `None` when the id could not be settled in time. That is **not** a failure: the
        prompt was accepted and the reply is on its way. The caller simply does not draw the
        user's row early, which is a worse experience and the honest one.
        """
        try:
            proc = await self._ensure(conversation_id)
        except (OSError, RuntimeError) as exc:
            # Spawning failed: nothing installed, or a binary this OS will not start.
            # `PiProcess.start` re-raises `OSError` as `RuntimeError` with an actionable
            # message, so both are caught. The same category as nanobot being down — the
            # agent is not there — so it maps to the same status and the same advice.
            raise BackendUnavailable(str(exc)) from None

        before = len(self._entries_on_disk(conversation_id))
        echo: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._user_echo[conversation_id] = echo

        command: dict[str, Any] = {"type": "prompt", "message": text}
        if self.is_streaming(conversation_id):
            # Pi refuses a bare `prompt` mid-run. `follow_up` means "when you are done", which
            # is what someone typing into a busy agent expects.
            command = {"type": "follow_up", "message": text}
        try:
            await proc.request(command)
            try:
                await asyncio.wait_for(echo, timeout=ECHO_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "pi did not echo the prompt for %s within %ss; its id is unknown",
                    conversation_id,
                    ECHO_TIMEOUT,
                )
                return None
            return self._user_message_since(conversation_id, before)
        except PiCommandError as exc:
            raise BackendError(str(exc.response.get("error") or exc)) from None
        except RuntimeError as exc:
            raise BackendError(str(exc)) from None
        finally:
            self._user_echo.pop(conversation_id, None)

    def _user_message_since(self, conversation_id: str, index: int) -> Message | None:
        """The user's entry, read back from the file so its id is Pi's rather than ours.

        Counting rather than matching on the text: the file is append-only, so everything past
        the length we saw before sending is new, and the user's message is the first of those
        that is one. Matching on content would mis-handle the second identical prompt.
        """
        entries = self._entries_on_disk(conversation_id)
        for entry in entries[index:]:
            if entry.get("type") != "message":
                continue
            message = entry.get("message") or {}
            if message.get("role") != "user":
                continue
            entry_id = entry.get("id")
            return Message(
                role=AgentRole.user,
                content=cap_string(_extract_text(message.get("content"))),
                id=entry_id if isinstance(entry_id, str) else None,
                created_at=_entry_time(entry),
            )
        return None

    async def history(self, conversation_id: str) -> list[Message]:
        """The conversation, read from Pi's session file on disk.

        **No process is started for this, and that is the point.** The transcript is a file Pi
        already wrote; asking a running Pi for it over `get_entries` would mean spawning a Node
        process — with whatever its extensions and MCP servers do on startup — purely to read
        something that is sitting on disk. Reading it directly also restores a property worth
        keeping: the page renders with the agent down, which is what a viewer should be able to
        do.

        The RPC path still exists and is still used for the *cursor* work (`_cursor_for`,
        `_user_message_after`), because those run in the middle of a live conversation where the
        process is necessarily up and its in-memory view is the consistent one.

        A conversation that predates `--session-id` lives in one or more files Pi named after a
        UUID of its own. `_session_files` includes them through the mapping written by
        `legacy.migrate_legacy_transcripts`, so the transcript is addressable even though Pi
        itself would not resume it. **This recovers the page and the journal, not Pi's own
        context**: the next prompt starts a session Pi can match to the thread's id, and Pi's
        model sees only what is in that file. Merging the old entries into it would break the
        parent chains Pi resumes from, so the limit is stated rather than papered over.
        """
        files = self._session_files(conversation_id)
        if not files:
            # Either nothing has been said yet — Pi creates the file with the first prompt — or
            # the session predates `--session-id` and no legacy map could attribute it. Both
            # are "there is no conversation to show", not errors.
            return []
        entries: list[dict[str, Any]] = []
        for path in files:
            entries.extend(_read_session_entries(path))
        return _entries_to_messages(entries)

    def _session_files(self, conversation_id: str) -> list[Path]:
        """Every file that belongs to this conversation, oldest first.

        **Two kinds of more-than-one, and both are ordinary.**

        Pi starts a *new file* whenever it continues a session it does not currently have open —
        every service restart, every process it loses — and each of those files carries the same
        `id` in its header. Reading only the first match is what made a live conversation
        invisible: on 2026-10-19 the deployment showed the file Pi wrote before a restart and
        never the one it was writing to now, so both a prompt and its answer were missing from
        the page while sitting on disk. `_session_files_of` is that half.

        The other kind is a conversation begun before `--session-id` existed: each service restart
        started a new automatically named session, and the one-time migration records those files
        in `pi-sessions/.legacy/<conversation id>.json`. They are read as a prefix, never renamed
        or merged — see the class comment in `agent/legacy.py`.
        """
        files = read_legacy_sessions(self._settings.pi_session_dir, conversation_id)
        files.extend(self._session_files_of(conversation_id))
        return files

    def _session_files_of(self, conversation_id: str) -> list[Path]:
        """Pi's own files for this conversation, oldest first.

        Sorted by **filename**, which Pi begins with an ISO timestamp — the only ordering it gives
        us, and the right one, since it is the order the files were created in.

        Every candidate is opened for its header rather than matched on the name, because a
        filename is a convention while the `id` inside is what Pi itself matches on; that is also
        why this scans the whole directory instead of globbing `*_{id}.jsonl` first. The cost is
        one line read per file in the session directory, and it buys a reader that cannot be
        fooled by a naming change — or by a file Pi named itself.
        """
        directory = self._settings.pi_session_dir
        if directory is None or not directory.is_dir():
            return []
        found = [
            candidate
            for candidate in directory.glob("*.jsonl")
            if _session_id_of(candidate) == conversation_id
        ]
        return sorted(found, key=lambda path: path.name)

    # -- reading Pi's session from disk --------------------------------- #
    def _entries_on_disk(self, conversation_id: str) -> list[dict[str, Any]]:
        """Every entry in this conversation's session files, or none if there is no file yet.

        The one way this backend reads a conversation, used by both `history` and the id lookup
        in `send`. Keeping it in one place is what stops the two drifting into reading the
        record two different ways — and keeps the legacy prefix present for both, so the
        entry count `send` takes before a prompt still lines up with the read that follows it.
        """
        entries: list[dict[str, Any]] = []
        for path in self._session_files(conversation_id):
            entries.extend(_read_session_entries(path))
        return entries

    async def abort(self, conversation_id: str) -> bool:
        proc = self._procs.get(conversation_id)
        if proc is None or not proc.alive:
            return False
        await proc.request({"type": "abort"})
        return True

    def is_live(self, conversation_id: str) -> bool:
        proc = self._procs.get(conversation_id)
        return proc is not None and proc.alive

    def is_streaming(self, conversation_id: str) -> bool:
        return conversation_id in self._streaming

    def is_available(self) -> bool:
        """Whether the Pi binary is actually on this machine.

        This is the check that makes the local agent honest about itself. "Not connected"
        would be the wrong word for a binary that was never installed — the fix is to install
        it, not to reconnect — so the settings panel can say which of the two it is.

        `shutil.which` only: no subprocess, because this runs while rendering a list.
        """
        import shutil

        binary = self._settings.pi_bin[0] if self._settings.pi_bin else "pi"
        return shutil.which(binary) is not None

    def status(self) -> BackendStatus:
        effective = self._effective()
        return BackendStatus(
            name=self.name,
            connected=True,  # local: there is no connection to be down
            detail=str(self._settings.pi_workspace) if self._settings.pi_workspace else None,
            conversations=sum(1 for p in self._procs.values() if p.alive),
            # The *configured* allowlist, not the process's: an empty list means Pi's default
            # (every built-in tool), which is the honest reading of "no --tools was passed".
            tools=list(effective.tools or ()),
            provider=effective.provider,
            model=effective.model,
        )

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def _handle(self, conversation_id: str, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        try:
            if kind == "message_update":
                self._on_message_update(conversation_id, frame)
            elif kind == "message_end":
                self._on_message_end(conversation_id, frame)
            elif kind == "tool_execution_start":
                self._fire(
                    conversation_id,
                    ToolCall(
                        phase="start",
                        name=frame.get("toolName"),
                        call_id=frame.get("toolCallId"),
                        args=frame.get("args"),
                    ),
                )
            elif kind == "tool_execution_end":
                self._on_tool_end(conversation_id, frame)
            elif kind == "agent_start":
                self._streaming.add(conversation_id)
                self._fire(conversation_id, Status(streaming=True))
            elif kind == "agent_settled":
                # `agent_end` is one low-level run; `agent_settled` is where Pi stops
                # continuing on its own. Only the latter means "nothing more is coming".
                self._streaming.discard(conversation_id)
                self._fire(conversation_id, Status(streaming=False))
            elif kind in ("compaction_start", "compaction_end"):
                reason = frame.get("reason")
                self._fire(
                    conversation_id,
                    Notice(
                        notice="compacting" if kind.endswith("start") else "compacted",
                        # Why it happened is worth showing: "manual" is the idle scan or the
                        # user, "threshold"/"overflow" is Pi rescuing itself.
                        detail=reason if isinstance(reason, str) else None,
                    ),
                )
            elif kind in ("auto_retry_start", "auto_retry_end"):
                self._fire(
                    conversation_id,
                    Notice(notice="retrying" if kind.endswith("start") else "retry-done"),
                )
            elif kind == "extension_error":
                self._fire(
                    conversation_id,
                    Notice(
                        notice="extension-error",
                        level="error",
                        detail=str(frame.get("error"))[:500],
                    ),
                )
        except Exception:
            # One malformed event must not kill the reader task and take the conversation
            # down with it.
            logger.exception("failed handling pi event %r", kind)

    def _on_message_update(self, conversation_id: str, frame: dict[str, Any]) -> None:
        delta = frame.get("assistantMessageEvent") or {}
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            channel = "text"
        elif delta_type == "thinking_delta":
            channel = "thinking"
        else:
            # text_start/text_end/toolcall_* carry no prose. The client assembles text from
            # the deltas and treats `message_end` as authoritative, as Pi documents.
            return
        self._fire(
            conversation_id,
            Delta(channel=channel, text=delta.get("delta", "")),
        )

    def _on_message_end(self, conversation_id: str, frame: dict[str, Any]) -> None:
        message = frame.get("message") or {}
        role = message.get("role")

        if role == "user":
            # Pi echoing the prompt back is how we learn it has been *recorded* — which is what
            # `send` is waiting for. The message itself is deliberately not published: the caller
            # that sent it gets it as the POST's answer, carrying the same id, so publishing it
            # too would be a second copy of the same row for no gain.
            pending = self._user_echo.get(conversation_id)
            if pending is not None and not pending.done():
                pending.set_result(None)
            return

        if role == "toolResult":
            # A tool result already arrives through `tool_execution_end`, which carries the
            # `is_error` flag and the call id together. Emitting it from both paths would put
            # two rows in the transcript for one command.
            return

        text = _extract_text(message.get("content"))
        if not text.strip():
            # A turn that was nothing but tool calls; the tool rows are the record.
            return

        if _has_tool_call(message):
            # Prose in the same entry as tool calls is a **checkpoint**, not the answer. Publishing
            # it as an ordinary message is what used to put half-sentences between the tool rows
            # and make the real answer indistinguishable from the narration before it — so it is
            # still a message (the same row `history()` re-reads, marked the same way), just not
            # one the page draws as a reply.
            self._fire(
                conversation_id,
                Message(
                    role=AgentRole.assistant,
                    content=cap_string(text),
                    progress=True,
                ),
            )
            return

        self._fire(
            conversation_id,
            Message(
                role=AgentRole.assistant,
                content=cap_string(text),
                tool_name="bash" if role == "bashExecution" else None,
            ),
        )

    def _on_tool_end(self, conversation_id: str, frame: dict[str, Any]) -> None:
        result = frame.get("result") or {}
        text = _extract_text(result.get("content"))
        is_error = bool(frame.get("isError"))
        self._fire(
            conversation_id,
            Message(
                role=AgentRole.tool,
                content=cap_string(text) if text else "(no output)",
                tool_name=frame.get("toolName"),
                tool_call_id=frame.get("toolCallId"),
                is_error=is_error,
            ),
        )
        self._fire(
            conversation_id,
            ToolCall(
                phase="end",
                name=frame.get("toolName"),
                call_id=frame.get("toolCallId"),
                is_error=is_error,
            ),
        )

    def _fire(self, conversation_id: str, event: Any) -> None:
        result = self._on_event(conversation_id, event)
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)  # noqa: RUF006 - deliberately not awaited


BACKEND_NAME = "native"

#: How this reads in the settings panel. `agenest` rather than `native` because that is
#: already the display name (`AgentPage.backendLabel`), and a panel row that disagreed with
#: the header above it would read as two different things.
BACKEND_LABEL = "agenest"

#: What connecting involves: nothing, but installing something. The distinction matters —
#: `is_available()` on this backend is a `shutil.which`, so the honest blurb names the install
#: rather than implying a reconnect would fix it (docs/design.md §17).
BACKEND_BLURB = (
    "A Pi process this service runs itself. Works with nothing but a provider key, "
    "once Pi is installed."
)


def build(settings: Settings, on_event: EventSink) -> NativeBackend:
    """Construct the native backend from settings.

    The registry's contract is one obvious function per module — see `backends/nanobot.py`.
    """
    return NativeBackend(settings, on_event)


__all__ = ["BACKEND_NAME", "NativeBackend", "build"]
