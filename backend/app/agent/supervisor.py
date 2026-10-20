"""Owns threads, and translates backend events into records and UI frames.

The layer that decides *what is worth saying*, now that it no longer decides *which agent is
saying it*. A backend emits normalized events against a conversation id; this maps that onto
one of our threads, writes down the parts that are the record, and publishes the parts that
are the notification.

The split that survives every change of backend:

- **Deltas are forwarded, never stored.** They are superseded microseconds later and have no
  identity; a row per token would be unbounded and worthless. This is why the agent stream
  has no cursor (`stream.AgentBroker`).
- **Completed units are stored.** A message, a tool result — what a client re-fetches after a
  reconnect, and the entire reason the stream can be cursorless.
- **A user message is stored once, by us.** Every backend echoes user input back in some
  form; each adapter is responsible for not reporting that echo as a new message, because
  only it knows what its own echo looks like.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..database import Database
from ..schemas import AgentMessageOut, AgentRole, AgentThreadDetailOut, cap_string, utcnow
from ..stream import AgentBroker, agent_frame
from . import endpoints, native_config, store
from .backends import (
    AgentBackend,
    BackendError,
    BackendUnavailable,
    Delta,
    Message,
    Notice,
    Status,
    ToolCall,
    available,
    build_backend,
    default_backend,
    is_offered,
)
from .legacy import migrate_legacy_transcripts
from .memory import Consolidator, Dream, DreamResult, Journal, PiDreamRunner, write_recall_file
from ..config import Settings

logger = logging.getLogger("csd.agent")

#: How much of the first prompt becomes the thread title.
TITLE_FROM_PROMPT = 60

#: The backend this service runs itself. It is the only one whose configuration we own: a
#: gateway agent keeps its own model and credentials on its own host, so offering to edit them
#: here would be a form that changes nothing (docs/design.md §26).
LOCAL_BACKEND = "native"

#: The backend whose conversations the memory layer belongs to. Not "whichever is active":
#: this memory is the local agent's, and a gateway agent has its own on its own host.
MEMORY_BACKEND = LOCAL_BACKEND


def _seconds_since(stamp: str | None, now: datetime) -> float | None:
    """Seconds between an ISO timestamp from `agent_threads` and now, or `None` if unreadable.

    Unreadable counts as "not idle": compaction is optional maintenance, and a parse failure
    must never be the reason a conversation gets aborted for a summary.
    """
    if not stamp:
        return None
    text = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds()


class Supervisor:
    """Thread bookkeeping plus event translation. Nothing here touches HTTP.

    Holds **every configured backend**, with one of them active. Threads remember which
    backend they belong to, so switching agents changes which one new conversations go to
    without re-pointing — or breaking — the conversations already on the page.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        broker: AgentBroker,
        backend: AgentBackend | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._broker = broker

        #: Every backend this deployment offers, by name. Built eagerly and cheaply —
        #: construction opens nothing, so listing them costs nothing. Only the active one is
        #: started. Names come from the registry, so a newly added backend module appears
        #: here without this class learning about it.
        self._backends: dict[str, AgentBackend] = {}
        for name in available():
            try:
                self._backends[name] = backend if (backend and backend.name == name) else build_backend(settings, self._on_event, name=name)
            except Exception:
                logger.exception("could not construct the %s backend", name)

        if backend is not None and backend.name not in self._backends:
            # An injected backend (tests) is authoritative even if its name is unusual.
            self._backends[backend.name] = backend

        # The local agent's stored configuration (docs/design.md §26). Loaded here, before
        # anything can build a command line, and handed to the backend that builds it — read in
        # this layer because the database is this layer's, and a backend has no handle of its
        # own. A backend that cannot be configured simply has no `apply_config`.
        self._native_config = native_config.load(db)
        self._dream_runner: PiDreamRunner | None = None
        apply_config = getattr(self._backends.get(LOCAL_BACKEND), "apply_config", None)
        if apply_config is not None:
            apply_config(self._native_config)

        # One-shot upgrade from the shared transcript table (agent/legacy.py): recover the
        # conversations it held, then drop it. It runs here, before anything serves the page,
        # because the page is how someone would notice it had not run. It never raises — a
        # failure leaves the table in place and retries on the next start.
        migrate_legacy_transcripts(self._db, settings, self._backends)

        self._active: str = self._resolve_initial_active(backend)
        #: conversation id -> thread id. Rebuilt from the store on demand, so it survives
        #: nothing and needs to survive nothing.
        self._by_conversation: dict[str, str] = {}
        #: Last published header state per thread, so an unchanged status is not re-sent.
        self._last_status: dict[str, dict] = {}
        #: Serialises switches, so two settings changes cannot interleave starts and stops.
        self._switch_lock = asyncio.Lock()
        #: Stops still draining from previous switches. Tracked so a rapid switch back to the
        #: same backend cannot start it while its old process is still being torn down.
        self._stops: set[asyncio.Task[None]] = set()

        # -- memory (docs/design.md §18) ---------------------------------- #
        # Built eagerly, like the backends: construction opens nothing, so a deployment with
        # memory switched off pays a couple of attributes and no I/O.
        #
        # `memory.intervals` deliberately does **not** live in Settings: the *schedule* is a
        # runtime concern (it starts and stops with the service), while the *configuration* is
        # not. The distinction decides where a bug is reported, and the schedule's bug report is
        # a log line, not a startup failure.
        memory_dir = settings.resolved_memory_dir
        self._journal: Journal | None = None
        self._consolidator: Consolidator | None = None
        self._dream: Dream | None = None
        self._dream_task: asyncio.Task[None] | None = None
        self._compact_task: asyncio.Task[None] | None = None
        if memory_dir is not None:
            self._journal = Journal(memory_dir / "history.jsonl")
            self._consolidator = Consolidator(
                db,
                self._journal,
                min_chars=settings.memory_journal_min_chars,
            )
            # Only the `native` backend can run Dream: it is a Pi process, and a gateway agent
            # keeps its own memory on its own host. A deployment talking to nanobot therefore has
            # a journal that nothing consolidates — which is the honest state, and better than
            # spawning a local model to curate a conversation it cannot see.
            if MEMORY_BACKEND in self._backends:
                # Kept on the supervisor as well as inside Dream: a configuration change has to
                # reach the process Dream spawns next, and Dream owns no config of its own.
                self._dream_runner = PiDreamRunner(settings, config=self._native_config)
                self._dream = Dream(
                    directory=memory_dir,
                    journal=self._journal,
                    db=db,
                    max_entries=settings.memory_dream_batch,
                    runner=self._dream_runner,
                )

    def _resolve_initial_active(self, injected: AgentBackend | None) -> str:
        """Which backend is live at startup.

        A persisted choice wins over the configured default: someone who switched to a local
        agent and restarted the service meant it, and silently reverting them to a gateway on
        every deploy would look like the setting does not stick.

        **Except when that choice is no longer offered.** A door that was retired after the user
        selected it should not keep the page on a backend the settings panel no longer lists —
        that is the one state where "hidden but still in use" would be worse than falling back.
        An explicit `CSD_AGENT_BACKEND` is still honoured, because that is the operator saying
        it on purpose.
        """
        if injected is not None:
            return injected.name
        stored = self._db.query_one("SELECT value FROM meta WHERE key = 'agent_backend'")
        if (
            stored is not None
            and stored["value"] in self._backends
            and is_offered(stored["value"])
        ):
            return stored["value"]
        configured = (self._settings.agent_backend or default_backend()).strip().lower()
        if configured in self._backends:
            return configured
        # Configured name is not one we can build. Fall back rather than refuse to boot — but
        # say so loudly, because someone typed it and expects it.
        fallback = (
            default_backend() if default_backend() in self._backends else next(iter(self._backends))
        )
        logger.warning(
            "CSD_AGENT_BACKEND=%r is not available; using %r", configured, fallback
        )
        return fallback

    # ------------------------------------------------------------------ #
    # backends
    # ------------------------------------------------------------------ #
    @property
    def backend(self) -> AgentBackend:
        """The active backend. Everything below goes through here."""
        return self._backends[self._active]

    @property
    def backend_name(self) -> str:
        return self._active

    def available_backends(self) -> list[dict]:
        """Every backend the page offers, its state, and whether it could be used right now.

        This is what turns "nanobot is not reachable" from a dead end into a choice: the page
        can offer the backends that *are* ready instead of only reporting the one that isn't.

        A retired door (`BACKEND_OFFERED = False`) is not a choice and is left out — **unless
        it is the active one**, which can only happen when the operator named it explicitly in
        `CSD_AGENT_BACKEND`. Hiding the backend the page is actually talking to would be a lie
        of exactly the kind §2 forbids.
        """
        out: list[dict] = []
        registered = set(endpoints.all())
        for name, backend in self._backends.items():
            if not is_offered(name) and name != self._active:
                continue
            status = backend.status()
            out.append(
                {
                    "name": name,
                    "active": name == self._active,
                    "connected": status.connected,
                    "ready": backend.is_available(),
                    "detail": status.detail,
                    # Whether the *agent itself* told us where to knock (§25) — the one
                    # thing the settings panel needs to know to offer a forget action
                    # without inviting somebody to erase an env-configured backend.
                    "registered": name in registered,
                    # Whether the page may offer a gear on this row. A backend that has nothing
                    # to configure refuses the config routes, so the panel must not draw the
                    # gear for it — a settings button that opens an empty form is worse than
                    # no button (docs/design.md §26).
                    "configurable": getattr(backend, "apply_config", None) is not None,
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # the local agent's configuration (docs/design.md §26)
    # ------------------------------------------------------------------ #
    def native_config_public(self) -> dict:
        """The local agent's configuration, as the page may see it. Never includes the key."""
        return native_config.public(self._native_config, self._settings)

    def update_native_config(self, patch: dict) -> dict:
        """Persist a change and make the next Pi process use it.

        Raises `native_config.ConfigError` when a value cannot be used — the API turns that into
        a 400 with the sentence intact, because "a key needs a provider" is more useful than
        "validation error".
        """
        config = native_config.save(self._db, patch, self._settings)
        self._native_config = config
        apply_config = getattr(self._backends.get(LOCAL_BACKEND), "apply_config", None)
        if apply_config is not None:
            apply_config(config)
        if self._dream_runner is not None:
            self._dream_runner.set_config(config)
        return native_config.public(config, self._settings)

    def native_backend(self) -> AgentBackend | None:
        """The local backend, or `None` when this build does not have one."""
        return self._backends.get(LOCAL_BACKEND)

    async def probe_backend(self, name: str) -> tuple[bool, str | None]:
        """Knock on one backend's door and report what happened, without switching to it.

        Optional on the seam — a backend with nothing to knock on answers "fine" — which is
        how the register route can give an agent an immediate verdict instead of letting it
        discover a shut door the next time somebody switches the page.
        """
        backend = self._backends.get(name)
        if backend is None:
            return False, f"unknown backend {name!r}"
        probe = getattr(backend, "probe", None)
        if probe is None:
            return True, None
        try:
            await probe()
        except BackendUnavailable as exc:
            return False, str(exc)
        except Exception as exc:  # a probe must not be able to 500 the register route
            return False, f"{exc.__class__.__name__}: {exc}"[:300]
        return True, None

    async def switch_backend(self, name: str) -> str:
        """Make `name` the active backend, starting it and stopping the previous one.

        Conversations already recorded keep their own backend, so this changes where *new*
        conversations go. It does not re-point old ones, which would either fail or, worse,
        hand a thread's history to a different agent.
        """
        if name not in self._backends:
            raise ValueError(f"unknown backend {name!r}; have {', '.join(self._backends)}")
        if name == self._active:
            return name
        if not is_offered(name):
            # Retirement is a removal from the choices, not a hidden row that still works:
            # the only way in is an explicit CSD_AGENT_BACKEND at startup.
            choices = ", ".join(sorted(n for n in self._backends if is_offered(n))) or "none"
            raise ValueError(f"backend {name!r} is no longer offered; choose one of {choices}")

        async with self._switch_lock:
            previous = self._backends[self._active]
            target = self._backends[name]

            # A stop from an earlier switch may still be draining. Wait for it before starting
            # anything, or a `stop()` could clear state underneath a process that just started.
            # Usually the set is empty and this costs nothing.
            if self._stops:
                await asyncio.gather(*self._stops, return_exceptions=True)

            await target.start()
            self._active = name
            with self._db.write():
                self._db.execute(
                    "INSERT INTO meta (key, value) VALUES ('agent_backend', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (name,),
                )
            # Stopping the previous backend is **not** part of the switch's answer. Its `stop()`
            # can wait out Pi's 5 s grace period, and the caller is waiting to render a
            # transcript; the switch is already recorded above, so the teardown can finish on
            # its own and report its own failure.
            task = asyncio.create_task(self._stop_quietly(previous))
            self._stops.add(task)
            task.add_done_callback(self._stops.discard)

            logger.info("agent backend switched: %s -> %s", previous.name, name)
            return name

    async def _stop_quietly(self, backend: AgentBackend) -> None:
        """Stop one backend without letting its failure — or its latency — reach the switch."""
        try:
            await backend.stop()
        except Exception:
            logger.exception("stopping the previous backend %s failed", backend.name)

    def backend_for(self, thread_id: str) -> AgentBackend:
        """The backend a given thread belongs to.

        Threads carry their backend, so a thread created against a local agent keeps working
        after someone switches to a gateway — and vice versa. Resolving per thread rather
        than per request is what makes switching non-destructive.
        """
        row = store.get_thread_row(self._db, thread_id)
        if row is not None:
            name = row["backend"]
            if name in self._backends:
                return self._backends[name]
        return self.backend

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        try:
            await self.backend.start()
        except Exception:
            # A backend that cannot start must not take the dashboard down: the sessions
            # page has nothing to do with the agent and should keep working.
            logger.exception("agent backend %s failed to start", self.backend.name)
        self._start_dream_clock()
        self._start_idle_compaction_clock()

    async def shutdown(self) -> None:
        # Drain stops a switch left running, so the loop below does not race a teardown that
        # is already in progress on the same backend.
        if self._stops:
            await asyncio.gather(*self._stops, return_exceptions=True)
        for attribute in ("_dream_task", "_compact_task"):
            task = getattr(self, attribute)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(self, attribute, None)
        for name, backend in self._backends.items():
            try:
                await backend.stop()
            except Exception:
                logger.exception("agent backend %s failed to stop", name)

    # ------------------------------------------------------------------ #
    # memory (docs/design.md §18)
    # ------------------------------------------------------------------ #
    @property
    def memory(self) -> Dream | None:
        """The Dream, for the API to offer a manual run. `None` when memory is switched off."""
        return self._dream

    @property
    def journal(self) -> Journal | None:
        """The run log. `None` when memory is switched off."""
        return self._journal

    @property
    def memory_directory(self) -> Path | None:
        """Where the memory files live, for the page to name the directory rather than imply it
        is somewhere only the service knows."""
        return self._settings.resolved_memory_dir

    @property
    def memory_interval_hours(self) -> float:
        return self._settings.memory_interval_h

    def _start_dream_clock(self) -> None:
        """Begin the slow layer's schedule, if one was configured.

        **On by default** (`memory_interval_h` = 2, nanobot's own interval). The first version of
        this defaulted to off, on the argument that a channel must not spend the operator's model
        budget on its own initiative; that was reversed when the agent started re-reading its
        memory on every prompt, because a layer nobody turns on is a layer that does not exist
        (docs/design.md §18). The interval is the knob; zero turns the clock off.
        """
        hours = self._settings.memory_interval_h
        if self._dream is None or hours <= 0:
            logger.debug(
                "dream clock is off (interval=%sh, dream=%s)", hours, self._dream is not None
            )
            return
        self._dream_task = asyncio.create_task(
            self._dream_loop(hours * 3600), name="csd-dream"
        )

    async def _dream_loop(self, interval: float) -> None:
        """Run Dream on its own clock, forever.

        `sleep` first, then run: a service restart should not immediately spend a model call
        consolidating a journal that was consolidated minutes before the deploy.
        """
        while True:
            try:
                await asyncio.sleep(interval)
                await self.run_dream()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed Dream is a missed consolidation. The cursor did not advance, so the
                # next tick retries the same batch — which is the whole reason the clock can be
                # this simple.
                logger.exception("dream run failed; it will be retried on the next tick")

    # ------------------------------------------------------------------ #
    # mid-conversation cost control (docs/design.md §19)
    # ------------------------------------------------------------------ #
    def _start_idle_compaction_clock(self) -> None:
        """Begin the idle-compaction scan, if one was configured.

        Separate from Dream and from Pi's own threshold: this is the proactive half of
        compaction, run when nobody is waiting. Zero turns it off.
        """
        minutes = self._settings.agent_idle_compact_minutes
        if minutes <= 0:
            logger.debug("idle compaction is off (minutes=%s)", minutes)
            return
        self._compact_task = asyncio.create_task(
            self._idle_compaction_loop(minutes * 60.0), name="csd-idle-compact"
        )

    async def _idle_compaction_loop(self, idle_after: float) -> None:
        while True:
            try:
                await asyncio.sleep(self._settings.agent_idle_compact_check_seconds)
                await self.compact_idle_now(idle_after)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A scan that fails is a missed opportunity, not a broken page; the next tick
                # asks again.
                logger.exception("idle compaction scan failed")

    async def compact_idle_now(self, idle_after: float) -> None:
        """Ask every backend that supports it to compact conversations idle this long.

        The clock is the supervisor's; whether a conversation is *worth* compacting and how to
        ask are the backend's (`NativeBackend.compact_when_idle`). Backends with no notion of a
        context window — a gateway agent owns its own — simply do not implement the method.
        """
        rows = self._db.query(
            """
            SELECT id, backend, backend_conversation_id, updated_at
            FROM agent_threads WHERE backend_conversation_id IS NOT NULL
            """
        )
        now = datetime.now(timezone.utc)
        for row in rows:
            backend = self._backends.get(row["backend"])
            compact = getattr(backend, "compact_when_idle", None)
            if compact is None:
                continue
            idle = _seconds_since(row["updated_at"], now)
            if idle is None or idle < idle_after:
                continue
            try:
                await compact(row["backend_conversation_id"], idle_seconds=idle)
            except Exception:
                logger.exception("could not compact the idle thread %s", row["id"])

    async def run_dream(self) -> DreamResult:
        """Run one Dream pass now. Never raises — see `Dream.run`."""
        if self._dream is None:
            return DreamResult(ran=False, detail="memory is not enabled on this deployment")
        result = await self._dream.run()
        if result.changed:
            # Regenerate the appendix a running agent re-reads, and — on the fallback path only —
            # restart that agent. See `NativeBackend.refresh_context`.
            await self._refresh_memory_context()
        return result

    async def _refresh_memory_context(self) -> None:
        """Restart idle native agents so their next prompt sees the memory Dream just wrote.

        Guarded on **streaming**, not on `is_running`: a Pi process is long-lived, so "running"
        is true whenever it exists — including while it sits idle between turns, which is
        exactly when a restart is safe and needed. The guard exists because stopping a turn the
        user is waiting on would abandon it; a thread that is busy keeps the old memory and is
        refreshed on its next process start. Best-effort by design, not by accident.
        """
        backend = self._backends.get(MEMORY_BACKEND)
        refresh = getattr(backend, "refresh_context", None)
        if refresh is None:
            return
        rows = self._db.query(
            "SELECT id, backend_conversation_id FROM agent_threads WHERE backend = ?",
            (MEMORY_BACKEND,),
        )
        for row in rows:
            conversation_id = row["backend_conversation_id"]
            if not conversation_id:
                continue
            if self.is_streaming(row["id"]):
                logger.info(
                    "memory changed; thread %s is mid-turn and will refresh on its next start",
                    row["id"],
                )
                continue
            try:
                await refresh(conversation_id)
            except Exception:
                logger.exception("could not refresh the agent context for %s", row["id"])

    async def _journal_now(self, thread_id: str) -> None:
        """Write the settled run into the journal.

        Async because `history()` is on the backend interface, and it asks the agent for the
        conversation rather than reading a table, because there is no table: the transcript
        belongs to the agent, and `history()` is the only way to it. For `native` that is a file
        read — the run just settled, so Pi has written it — and it comes back with the tool
        arguments that the copy this used to read had dropped.
        """
        if self._consolidator is None:
            return
        row = store.get_thread_row(self._db, thread_id)
        if row is None or row["backend"] != MEMORY_BACKEND:
            return
        conversation_id = row["backend_conversation_id"]
        if not conversation_id:
            # Nothing has been said yet, so there is nothing to remember. Not an error.
            return
        try:
            messages = await self.backend_for(thread_id).history(conversation_id)
            entry = self._consolidator.consolidate(thread_id, messages)
        except Exception:
            # A journal that cannot be written is a missed memory, not a broken conversation.
            logger.exception("could not journal the run in %s", thread_id)
            return
        if entry is not None:
            logger.info("journalled %s as entry %d", thread_id, entry.index)

    def _regenerate_recall(self, thread_id: str) -> None:
        """Re-derive the appendix an agent re-reads, after a run settles.

        Dream is not the only writer a person might be: `MEMORY.md` is a file, deliberately, and
        someone who edits it by hand expects the agent to notice. Regenerating at the same moment
        the journal is written makes that true without a restart. A backend whose agent reads the
        appendix once at start-up has nothing to regenerate and is skipped — which is also why
        this is `getattr`: a per-turn appendix is optional on the seam, not required of every
        backend.

        Only the *memory* half is regenerated here: the machines file is the operator's own prose
        (there is nothing to derive), and the progress instruction is a fixed string that rides in
        the process environment.
        """
        row = store.get_thread_row(self._db, thread_id)
        if row is None or row["backend"] != MEMORY_BACKEND:
            return
        backend = self._backends.get(MEMORY_BACKEND)
        if backend is None or not getattr(backend, "has_per_turn_appendix", lambda: False)():
            return
        directory = self._settings.resolved_memory_dir
        if directory is None:
            return
        write_recall_file(directory)

    # ------------------------------------------------------------------ #
    # conversation mapping
    # ------------------------------------------------------------------ #
    def _thread_for(self, conversation_id: str) -> str | None:
        thread_id = self._by_conversation.get(conversation_id)
        if thread_id is not None:
            return thread_id
        # A conversation could belong to a thread created before this process started, so the
        # mapping is looked up once from the store rather than assumed to be in memory.
        row = self._db.query_one(
            "SELECT id FROM agent_threads WHERE backend_conversation_id = ?",
            (conversation_id,),
        )
        if row is None:
            return None
        self._by_conversation[conversation_id] = row["id"]
        return row["id"]

    async def _conversation_for(self, thread_id: str) -> str:
        """The backend conversation behind a thread, creating one if this is its first use.

        Creation is lazy on purpose: opening a thread should not spend an agent conversation
        on it, and every backend mints identity at a different moment (nanobot on
        `new_chat`, native on first send).

        Goes through `backend_for`, not the active backend: a thread is bound to whichever
        agent it was created against, and stays there across a switch.
        """
        row = store.get_thread_row(self._db, thread_id)
        if row is None:
            raise KeyError(thread_id)

        conversation_id = row["backend_conversation_id"]
        if conversation_id:
            self._by_conversation[conversation_id] = thread_id
            return conversation_id

        backend = self.backend_for(thread_id)
        try:
            conversation_id = await backend.new_conversation()
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(str(exc)) from None

        store.set_thread_conversation(self._db, thread_id, conversation_id)
        self._by_conversation[conversation_id] = thread_id
        return conversation_id

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    async def prompt(self, thread_id: str, content: str) -> AgentMessageOut:
        """Send a prompt and answer with the user's message **as the agent recorded it**.

        The ordering here is the whole design. Nothing is written down before the agent accepts
        it: the row returned to the caller is the agent's own, carrying the agent's own id, so
        the copy the page draws and the copy it later re-reads are recognisably one message.

        `send` returns `None` when it could not settle an id in time. That is not an error — the
        prompt was accepted — so we answer with the text and no id, and the page lets the
        history provide the row instead. The alternative, minting an id here, is what would
        force the client into reconciling two ids for one message.
        """
        backend = self.backend_for(thread_id)
        conversation_id = await self._conversation_for(thread_id)
        recorded = await backend.send(conversation_id, content)

        message = recorded or Message(role=AgentRole.user, content=cap_string(content))
        out = self._to_out(thread_id, message)
        self._publish_message(thread_id, out)

        row = store.get_thread_row(self._db, thread_id)
        if row is not None:
            if row["title"] == "New conversation":
                store.rename_thread(self._db, thread_id, content[:TITLE_FROM_PROMPT])
                self._emit_status(thread_id)
            # Nothing else writes to `agent_threads`, so activity has to be recorded explicitly:
            # `updated_at` is what orders the conversation list.
            store.touch_thread(self._db, thread_id)
        return out

    async def history(self, thread_id: str) -> list[AgentMessageOut]:
        """The conversation, from whichever place the backend keeps it."""
        row = store.get_thread_row(self._db, thread_id)
        if row is None:
            raise KeyError(thread_id)
        conversation_id = row["backend_conversation_id"]
        if not conversation_id:
            return []
        messages = await self.backend_for(thread_id).history(conversation_id)
        return [self._to_out(thread_id, m) for m in messages]

    async def thread_detail(self, thread_id: str) -> AgentThreadDetailOut:
        """The thread's metadata joined to its conversation.

        The join happens here, at the top, because the two halves come from different places and
        nothing below should have to know that: `agent_threads` holds what is ours about the
        conversation, and the messages come from the agent.
        """
        thread = store.get_thread(self._db, thread_id)
        if thread is None:
            raise KeyError(thread_id)
        return AgentThreadDetailOut(
            **thread.model_dump(),
            messages=await self.history(thread_id),
        )

    async def abort(self, thread_id: str) -> bool:
        row = store.get_thread_row(self._db, thread_id)
        if row is None:
            raise KeyError(thread_id)
        conversation_id = row["backend_conversation_id"]
        if not conversation_id:
            return False
        return await self.backend_for(thread_id).abort(conversation_id)

    def is_running(self, thread_id: str) -> bool:
        row = store.get_thread_row(self._db, thread_id)
        if row is None or not row["backend_conversation_id"]:
            return False
        return self.backend_for(thread_id).is_live(row["backend_conversation_id"])

    def is_streaming(self, thread_id: str) -> bool:
        row = store.get_thread_row(self._db, thread_id)
        if row is None or not row["backend_conversation_id"]:
            return False
        return self.backend_for(thread_id).is_streaming(row["backend_conversation_id"])

    def running_count(self) -> int:
        return self.backend.status().conversations

    def status(self):
        return self.backend.status()

    # ------------------------------------------------------------------ #
    # backend events
    # ------------------------------------------------------------------ #
    async def _on_event(self, conversation_id: str, event) -> None:
        """One normalized event from the backend, for one conversation."""
        thread_id = self._thread_for(conversation_id)
        if thread_id is None:
            # A conversation nobody here opened — another client's chat, or one from a thread
            # that was deleted. Not an error, and specifically not a reason to create a
            # thread: inventing one would put a conversation on the board that the user
            # never started here.
            logger.debug("event for unknown conversation %s", conversation_id)
            return

        try:
            if isinstance(event, Delta):
                self._broker.publish_agent(
                    agent_frame(
                        "agent.delta",
                        thread_id=thread_id,
                        channel=event.channel,
                        delta=event.text,
                        replace=event.replace,
                    )
                )
            elif isinstance(event, Message):
                # Published, not written. The message is already in the agent's own record — it
                # has to be, or the frame could not carry its id — so storing it here would
                # recreate exactly the second copy this design removed.
                self._publish_message(thread_id, self._to_out(thread_id, event))
            elif isinstance(event, ToolCall):
                self._broker.publish_agent(
                    agent_frame(
                        "agent.tool",
                        thread_id=thread_id,
                        phase=event.phase,
                        tool_name=event.name,
                        tool_call_id=event.call_id,
                        args=event.args,
                        is_error=event.is_error,
                    )
                )
            elif isinstance(event, Status):
                self._emit_status(
                    thread_id,
                    streaming=event.streaming,
                    provider=event.provider,
                    model=event.model,
                )
                if event.streaming is False:
                    # "Pi has stopped continuing on its own" — the one moment a run is finished
                    # and its transcript is complete. The backend has already normalized five
                    # different upstream frames into this, which is why the hook belongs here
                    # rather than in each adapter: what a settled run *is* is exactly what the
                    # seam was drawn to define, and what to do about it is not the backend's job.
                    #
                    # Awaited rather than fired off, because this handler already runs as its own
                    # task — every backend detaches the coroutine it gets from the sink — so
                    # awaiting here does not hold up the reader that produced the frame.
                    await self._journal_now(thread_id)
                    self._regenerate_recall(thread_id)
            elif isinstance(event, Notice):
                self._broker.publish_agent(
                    agent_frame(
                        "agent.notice",
                        thread_id=thread_id,
                        notice=event.notice,
                        level=event.level,
                        detail=event.detail,
                    )
                )
        except Exception:
            # A failure translating one event must not poison the reader that produced it.
            logger.exception("failed to handle backend event %r", type(event).__name__)

    # ------------------------------------------------------------------ #
    # frames
    # ------------------------------------------------------------------ #
    def _to_out(self, thread_id: str, message: Message) -> AgentMessageOut:
        """A backend `Message` as the wire model.

        The only translation left in this layer, and it is a shape change rather than a policy
        one: `id`, `args` and `created_at` pass straight through because they belong to the
        agent. The timestamp fallback is for backends whose protocol does not carry one — a
        live frame, where "now" *is* when the message arrived. History must not borrow it:
        stamping a re-read conversation with the moment it was read is a timestamp that is
        wrong on every refresh, which is exactly what this field was added to stop.
        """
        return AgentMessageOut(
            id=message.id,
            thread_id=thread_id,
            role=message.role,
            content=cap_string(message.content),
            tool_name=message.tool_name,
            tool_call_id=message.tool_call_id,
            args=message.args,
            is_error=message.is_error,
            created_at=message.created_at or utcnow(),
            progress=message.progress,
        )

    def _publish_message(self, thread_id: str, message: AgentMessageOut) -> None:
        self._broker.publish_agent(
            agent_frame(
                "agent.message",
                thread_id=thread_id,
                message=message.model_dump(mode="json"),
            )
        )

    def _emit_status(
        self,
        thread_id: str,
        *,
        streaming: bool | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        thread = store.get_thread(self._db, thread_id)
        if provider or model:
            store.set_thread_model(self._db, thread_id, provider=provider, model=model)
            thread = store.get_thread(self._db, thread_id)

        backend = self.backend_for(thread_id)
        payload = {
            "thread_id": thread_id,
            "running": self.is_running(thread_id),
            "streaming": self.is_streaming(thread_id) if streaming is None else streaming,
            "provider": thread.provider if thread else None,
            "model": thread.model if thread else None,
            "backend": backend.name,
        }
        # Only publish a change. Gateways emit several bookkeeping events per turn
        # (`session_updated`, `turn_model_updated`, `runtime_model_updated`) that mostly say
        # nothing new, and every one of them would otherwise become a frame the phone has to
        # receive and re-render. Same information, a third of the traffic.
        if self._last_status.get(thread_id) == payload:
            return
        self._last_status[thread_id] = payload
        self._broker.publish_agent(agent_frame("agent.status", **payload))


__all__ = ["Supervisor"]
