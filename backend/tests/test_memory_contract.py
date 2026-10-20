"""The memory layer's contract, exercised without a model and without git.

docs/design.md §18 is the spec. Everything here tests a rule that the design chapter states as
load-bearing, and the rules cluster into three groups:

- **The journal is a view.** Its cursor points into the agent's transcript, so the tests assert
  that re-running consolidation converges rather than duplicating.
- **The cursor advances on completion, not on change.** This is the rule most likely to be
  "simplified" later by someone who reasons that a Dream that changed nothing should be retried.
  `test_a_dream_that_changed_nothing_still_advances` is the one to keep.
- **Dream's edits are restricted.** `test_dreams_tools_exclude_bash` and
  `test_only_the_memory_files_are_ever_committed` pin the boundary that keeps a curation process
  from becoming a second agent with shell access.

No network, no LLM, no subprocess: `FakeRunner` stands in for the Pi process, and git is used only
where a real repository is needed to observe a commit.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import pytest

from app.agent.backends.base import Message
from app.agent.memory import (
    CURSOR_DREAM,
    RECALL_MAX_CHARS,
    Consolidator,
    Dream,
    DreamResult,
    Journal,
    JournalEntry,
    MemoryGit,
    PiDreamRunner,
    _read_cursor,
    _read_id_cursor,
    _write_cursor,
    journal_cursor_key,
    recall_prompt,
    write_recall_file,
)
from app.schemas import AgentRole
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.stream import AgentBroker, Broker
from fastapi.testclient import TestClient

TOKEN = "test-ingest-token"

#: The one credential this service's page uses — reads, closing a session, and the agent
#: (docs/design.md §22). `_settings` sets it and `_headers` presents it, so the two agree.
ACCESS_TOKEN = "access-token"


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        db_path=tmp_path / "test.sqlite3",
        ingest_token=TOKEN,
        host="127.0.0.1",
        port=0,
        static_dir=None,
        stale_after_seconds=900,
        event_payload_max_chars=8000,
        cors_origins=(),
        dashboard_token=ACCESS_TOKEN,
        pi_workspace=tmp_path,
        # Where Pi keeps its sessions, and therefore where this backend *reads the conversation*
        # from. `config.py` always sets it in production (defaulting to a directory beside the
        # database), so a test that left it unset would be exercising a configuration that does
        # not exist — and the stub Pi, given no `--session-dir`, would record nothing to read.
        pi_session_dir=tmp_path / "pi-sessions",
        pi_provider="fake",
        pi_model="fake-1",
        memory_dir=tmp_path / "memory",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


def _convos() -> dict[str, list[Message]]:
    """Conversations keyed by thread — what `AgentBackend.history()` hands back.

    Built here rather than through a backend, because the consolidator's subject is what it does
    with a conversation, not how one was obtained. That the conversation now *arrives* as an
    argument instead of being queried out of a table is the change these tests exist to pin: the
    transcript belongs to the agent, and this layer no longer has storage of its own.
    """
    return defaultdict(list)


def _say(convos, thread_id: str, role: str, content: str, **extra) -> str:
    """Append one message to a conversation, and return the id the agent would have given it."""
    message = Message(
        role=AgentRole(role),
        content=content,
        id=f"{thread_id}-e{len(convos[thread_id]) + 1}",
        **extra,
    )
    convos[thread_id].append(message)
    assert message.id is not None
    return message.id


def _long(text: str = "x") -> str:
    """Content over the journalling threshold, so a test that cares about the entry does not
    also have to care about the threshold."""
    return text * 500


# --------------------------------------------------------------------------- #
# cursors — a book, not a state
# --------------------------------------------------------------------------- #
def test_a_missing_cursor_reads_as_zero(db: Database) -> None:
    """Starting from the beginning is the correct recovery. The journal can be rebuilt from the
    transcript, so a lost cursor costs a re-read and never costs history."""
    assert _read_cursor(db, journal_cursor_key("t_test")) == 0


def test_each_conversation_gets_its_own_journal_cursor(db: Database, tmp_path: Path) -> None:
    """The journal is one ordered file but a conversation is its own, so the cursor is per thread.

    A single global cursor would let a newer conversation advance past an older one's unread
    messages and lose them. Pinned because the failure is silent and delayed: it needs a second
    conversation to exist, and the symptom is a missing journal entry nobody knows to look for.

    The cursor is now an id the agent minted (`t_one-e1` below), which is why nothing here
    compares it as a number — see `_after`.
    """
    convos = _convos()
    journal = Journal(tmp_path / "history.jsonl")
    consolidator = Consolidator(db, journal)

    _say(convos, "t_one", "user", _long("a"))
    consolidator.consolidate("t_one", convos["t_one"])

    # The second conversation is journalled from *its* beginning, not from wherever the first
    # one's cursor happens to be.
    _say(convos, "t_two", "user", _long("b"))
    entry = consolidator.consolidate("t_two", convos["t_two"])

    assert entry is not None
    assert _read_id_cursor(db, journal_cursor_key("t_one")) != _read_id_cursor(
        db, journal_cursor_key("t_two")
    )
    assert len(journal.read()) == 2


def test_a_corrupt_cursor_reads_as_zero_rather_than_raising(db: Database) -> None:
    """Someone editing the `meta` table by hand must not take the service down."""
    _write_cursor(db, CURSOR_DREAM, 7)
    with db.write():
        db.execute("UPDATE meta SET value = 'not a number' WHERE key = ?", (CURSOR_DREAM,))
    assert _read_cursor(db, CURSOR_DREAM) == 0


def test_cursors_round_trip(db: Database) -> None:
    _write_cursor(db, CURSOR_DREAM, 42)
    assert _read_cursor(db, CURSOR_DREAM) == 42
    _write_cursor(db, CURSOR_DREAM, 43)
    assert _read_cursor(db, CURSOR_DREAM) == 43


# --------------------------------------------------------------------------- #
# the journal
# --------------------------------------------------------------------------- #
def test_a_journal_that_does_not_exist_reads_as_empty(tmp_path: Path) -> None:
    assert Journal(tmp_path / "history.jsonl").read() == []


def test_a_malformed_line_is_skipped_and_the_rest_survives(tmp_path: Path) -> None:
    """The journal is an optimisation over the transcript. A bad line costs Dream a little
    context; it must not cost the whole file."""
    path = tmp_path / "history.jsonl"
    path.write_text(
        json.dumps({"index": 1, "timestamp": "t", "content": "first"}) + "\n"
        + "{ this is not json\n"
        + json.dumps({"index": 2, "timestamp": "t", "content": "second"}) + "\n",
        encoding="utf-8",
    )
    entries = Journal(path).read()
    assert [e.index for e in entries] == [1, 2]


def test_an_entry_without_content_is_not_an_entry(tmp_path: Path) -> None:
    """A record with no content is a shape error, not a short entry — and keeping it would put a
    blank line in the prompt Dream reads."""
    path = tmp_path / "history.jsonl"
    path.write_text(
        json.dumps({"index": 1, "timestamp": "t"}) + "\n"
        + json.dumps({"index": 2, "timestamp": "t", "content": "kept"}) + "\n",
        encoding="utf-8",
    )
    assert [e.content for e in Journal(path).read()] == ["kept"]


def test_unprocessed_returns_only_entries_after_the_cursor(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "history.jsonl")
    for index in (1, 2, 3):
        journal.append(JournalEntry(index=index, timestamp="t", content=f"e{index}"))
    assert [e.index for e in journal.unprocessed(1)] == [2, 3]
    assert journal.unprocessed(3) == []


def test_compaction_never_drops_an_unconsumed_entry(tmp_path: Path) -> None:
    """An oversized journal costs tokens. A journal missing entries Dream has not read costs
    memory, and memory is the thing this module exists to keep."""
    journal = Journal(tmp_path / "history.jsonl")
    for index in range(1, 11):
        journal.append(JournalEntry(index=index, timestamp="t", content=f"e{index}"))

    dropped = journal.compact(keep=2, before_index=8)
    assert dropped == 8  # 10 entries, keep 2, but nothing at or before cursor 8 may go
    assert [e.index for e in journal.read()] == [9, 10]


def test_compaction_drops_the_oldest_when_the_cursor_is_caught_up(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "history.jsonl")
    for index in range(1, 11):
        journal.append(JournalEntry(index=index, timestamp="t", content=f"e{index}"))
    assert journal.compact(keep=3, before_index=10) == 7
    assert [e.index for e in journal.read()] == [8, 9, 10]


# --------------------------------------------------------------------------- #
# the consolidator — agent_messages -> journal
# --------------------------------------------------------------------------- #
def test_a_short_run_is_not_journalled_but_the_cursor_still_moves(db: Database, tmp_path: Path) -> None:
    """A greeting is not memory.

    The cursor must move anyway: leaving it would make the threshold a floor that nothing can
    ever cross, because every later run would re-read this one and stop at the same place.
    """
    convos = _convos()
    thread = "t_test"
    _say(convos, thread, "user", "hi")
    _say(convos, thread, "assistant", "hello")

    journal = Journal(tmp_path / "history.jsonl")
    consolidator = Consolidator(db, journal, min_chars=400)

    assert consolidator.consolidate(thread, convos[thread]) is None
    assert journal.read() == []
    # A string id the agent minted, so "did it move" is a question about presence, not size.
    assert _read_id_cursor(db, journal_cursor_key(thread)) == "t_test-e2"


def test_a_substantial_run_becomes_one_entry(db: Database, tmp_path: Path) -> None:
    convos = _convos()
    thread = "t_test"
    _say(convos, thread, "user", _long("q"))
    _say(convos, thread, "assistant", _long("a"))

    journal = Journal(tmp_path / "history.jsonl")
    entry = Consolidator(db, journal).consolidate(thread, convos[thread])

    assert entry is not None
    assert entry.index == 1
    assert "USER:" in entry.content and "ASSISTANT:" in entry.content
    assert len(journal.read()) == 1


def test_consolidating_twice_does_not_duplicate_the_entry(db: Database, tmp_path: Path) -> None:
    """The whole reason the journal is a view rather than an append log.

    The trigger is `agent_settled`, which can fire more than once for one run, and a service can
    restart between the write and anything else. Both must converge on one entry.
    """
    convos = _convos()
    thread = "t_test"
    _say(convos, thread, "user", _long("q"))
    _say(convos, thread, "assistant", _long("a"))

    journal = Journal(tmp_path / "history.jsonl")
    consolidator = Consolidator(db, journal)

    first = consolidator.consolidate(thread, convos[thread])
    second = consolidator.consolidate(thread, convos[thread])

    assert first is not None
    assert second is None, "a second pass with nothing new must not write a second entry"
    assert len(journal.read()) == 1


def test_a_new_run_appends_a_second_entry(db: Database, tmp_path: Path) -> None:
    convos = _convos()
    thread = "t_test"
    _say(convos, thread, "user", _long("q"))
    journal = Journal(tmp_path / "history.jsonl")
    consolidator = Consolidator(db, journal)
    consolidator.consolidate(thread, convos[thread])

    _say(convos, thread, "assistant", _long("a"))
    second = consolidator.consolidate(thread, convos[thread])

    assert second is not None and second.index == 2
    assert [e.index for e in journal.read()] == [1, 2]


def test_a_failed_tool_is_labelled_in_words(db: Database, tmp_path: Path) -> None:
    """The same rule the agent page applies to tool rows: an outcome is a word, not a colour.

    A journal that recorded a failure as mere output would teach Dream a false history — and a
    journal that recorded the output *without the command* would leave it unable to tell an
    ordinary error from a real finding. Both halves are asserted, because the second is the one
    that used to be missing.
    """
    convos = _convos()
    thread = "t_test"
    _say(
        convos,
        thread,
        "tool",
        _long("boom"),
        tool_name="bash",
        args={"command": "pytest tests/"},
        is_error=True,
    )

    journal = Journal(tmp_path / "history.jsonl")
    entry = Consolidator(db, journal).consolidate(thread, convos[thread])

    assert entry is not None
    assert "bash [failed]" in entry.content
    assert "pytest tests/" in entry.content, "the command is what makes the failure actionable"


# --------------------------------------------------------------------------- #
# dream's cursor rules — the part most likely to be "simplified" wrongly
# --------------------------------------------------------------------------- #
class FakeRunner:
    """Stands in for the Pi process that runs Dream.

    Records being called, and edits the files according to a scripted outcome, so every cursor
    rule can be tested without a model. `fail` raises the way a crashed Pi would.
    """

    def __init__(self, *, writes: str | None = None, fail: bool = False) -> None:
        self.writes = writes
        self.fail = fail
        self.calls = 0

    async def run(self, prompt: str, *, directory: Path, cwd: Path | None = None) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("pi exited with code 3")
        if self.writes is not None:
            (directory / "MEMORY.md").write_text(self.writes, encoding="utf-8")
        return "done"


def _dream(db: Database, tmp_path: Path, runner, *, batch: int = 20) -> Dream:
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    journal = Journal(directory / "history.jsonl")
    return Dream(
        directory=directory,
        journal=journal,
        db=db,
        max_entries=batch,
        runner=runner,
    )


def _dream_run(dream: Dream) -> DreamResult:
    """Run one Dream pass to completion, synchronously.

    `asyncio.run` rather than `pytest-asyncio`: this project carries no async test plugin, and
    adding one for seven tests is a dependency bought with a decorator. `Dream.run` is async
    because the runner it drives is, not because anything here needs to await concurrently.
    """
    return asyncio.run(dream.run())


def _require_git() -> None:
    """Skip when the git *binary* is missing — not when the directory has no repository.

    The distinction matters: `MemoryGit.available` asks "is this a repository", which is false
    for every memory directory until `Dream.run` initialises one. Skipping on that would skip
    the test on every machine, including the ones the test exists for.
    """
    try:
        probe = subprocess.run(
            ["git", "--version"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git is not installed")
    if probe.returncode != 0:
        pytest.skip("git is not usable")


def _seed_journal(directory: Path, count: int = 1) -> Journal:
    journal = Journal(directory / "history.jsonl")
    for index in range(1, count + 1):
        journal.append(JournalEntry(index=index, timestamp="t", content=f"entry {index}"))
    return journal


def test_a_dream_with_nothing_to_do_does_not_start_a_process(db: Database, tmp_path: Path) -> None:
    """The common case, and the cheap one: no journal entries means no subprocess and no model
    call. A Dream that ran anyway would spend the operator's budget to read nothing."""
    runner = FakeRunner()
    dream = _dream(db, tmp_path, runner)
    result = _dream_run(dream)

    assert result == DreamResult(ran=False, detail="nothing to consolidate")
    assert runner.calls == 0


def test_a_dream_that_changed_nothing_still_advances(db: Database, tmp_path: Path) -> None:
    """**"No update needed" is a result, not a failure.**

    If the cursor only moved when files changed, a journal Dream had nothing to add to would be
    re-read on every run, forever, and each read would cost a model call. The distinction the
    cursor follows is *completed*, not *changed*.
    """
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    _seed_journal(directory, 3)

    runner = FakeRunner(writes=None)  # completes, edits nothing
    result = _dream_run(_dream(db, tmp_path, runner))

    assert result.ran and result.advanced and not result.changed
    assert _read_cursor(db, CURSOR_DREAM) == 3


def test_a_dream_that_only_added_a_skill_is_committed(db: Database, tmp_path: Path) -> None:
    """A skill is a *new directory*, and two things conspire to hide it.

    `git diff` ignores untracked files, so the commit subject would have no file to name; and
    if `changed` is computed from the three markdown files only, the run reports "no update
    needed", advances the cursor, and neither commits nor surfaces the skill. Both are pinned
    here because either one alone leaves the feature silently broken.
    """
    _require_git()
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    _seed_journal(directory, 1)

    class SkillRunner:
        async def run(self, prompt: str, *, directory: Path, cwd: Path | None = None) -> str:
            path = directory / "skills" / "deploy" / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "---\nname: deploy\ndescription: ship it\n---\n\nrun the deploy script\n",
                encoding="utf-8",
            )
            return "created a skill"

    result = _dream_run(_dream(db, tmp_path, SkillRunner()))

    assert result.ran and result.changed and result.advanced
    log = MemoryGit(directory).log()
    assert log and "skills/deploy/SKILL.md" in log[0]["subject"]
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=directory, capture_output=True, text=True, check=True
    ).stdout
    assert "skills/deploy/SKILL.md" in tracked


def test_a_dream_that_changed_memory_advances_and_records_a_commit(db: Database, tmp_path: Path) -> None:
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    _seed_journal(directory, 2)

    runner = FakeRunner(writes="# Memory\n\n- the dashboard is read-only\n")
    result = _dream_run(_dream(db, tmp_path, runner))

    assert result.ran and result.advanced and result.changed
    assert _read_cursor(db, CURSOR_DREAM) == 2
    assert (directory / "MEMORY.md").read_text(encoding="utf-8").startswith("# Memory")


def test_a_failed_dream_does_not_advance_the_cursor(db: Database, tmp_path: Path) -> None:
    """A crashed Dream must be retried, so the batch it did not finish stays pending.

    Retrying is safe because Dream's edits are idempotent given unchanged inputs, and the inputs
    have not changed — the journal has not moved.
    """
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    _seed_journal(directory, 3)

    runner = FakeRunner(fail=True)
    result = _dream_run(_dream(db, tmp_path, runner))

    assert result.ran and not result.advanced
    assert _read_cursor(db, CURSOR_DREAM) == 0, "the same batch must still be pending"


def test_a_dream_that_raises_does_not_propagate(db: Database, tmp_path: Path) -> None:
    """A failed Dream is a missed consolidation, not a broken service. `run` swallowing this is
    what lets the supervisor call it from a background loop without a try of its own."""
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    _seed_journal(directory, 1)

    # No runner at all: `_invoke` raises RuntimeError, which is the same path a crashed Pi takes.
    dream = Dream(directory=directory, journal=Journal(directory / "history.jsonl"), db=db)
    result = _dream_run(dream)

    assert result.ran and not result.advanced


def test_the_batch_is_bounded_and_the_rest_stays_pending(db: Database, tmp_path: Path) -> None:
    """One run reads at most `max_entries`. A long journal is consumed over several runs rather
    than in one unbounded prompt."""
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    _seed_journal(directory, 10)

    result = _dream_run(_dream(db, tmp_path, FakeRunner(writes=None), batch=4))

    assert result.ran
    assert _read_cursor(db, CURSOR_DREAM) == 4, "only the batch it read may be marked consumed"
    assert len(Journal(directory / "history.jsonl").unprocessed(4)) == 6


def test_the_commit_subject_comes_from_the_diff_not_the_model(db: Database, tmp_path: Path) -> None:
    """The audit record must reflect the filesystem, not the model's account of itself.

    The runner here reports nothing at all — the subject still names the file that moved, because
    it is derived from `git diff --stat` rather than from anything the model said.
    """
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    _seed_journal(directory, 1)

    runner = FakeRunner(writes="# Memory\n\n- a fact\n")
    dream = _dream(db, tmp_path, runner)
    _require_git()
    result = _dream_run(dream)

    assert result.commit, "a changed file must produce a commit"
    log = dream.git.log()
    assert "MEMORY.md" in log[0]["subject"]
    assert log[0]["subject"].startswith("dream:")


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
def test_only_the_memory_files_are_ever_committed(tmp_path: Path) -> None:
    """The memory repository must not become a shadow of the workspace.

    The agent works in `CSD_PI_WORKSPACE`; the memory directory is inside it by default. A
    `git add -A` here would version every file the agent touches — including whatever it is
    working on, and any secret it happened to read.
    """
    directory = tmp_path / "memory"
    directory.mkdir()
    git = MemoryGit(directory)
    if not git.available and subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git is not installed")

    git.initialize()
    (directory / "MEMORY.md").write_text("a fact\n", encoding="utf-8")
    (directory / "NOT_MEMORY.txt").write_text("should not be committed\n", encoding="utf-8")

    sha = git.commit("dream: update MEMORY.md")
    assert sha
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=directory, capture_output=True, text=True, check=True
    ).stdout
    assert "MEMORY.md" in tracked
    assert "NOT_MEMORY.txt" not in tracked


def test_nothing_to_commit_returns_none(tmp_path: Path) -> None:
    """The expected result when Dream decided memory is already current. `None` rather than an
    error, because it is not a failure."""
    directory = tmp_path / "memory"
    directory.mkdir()
    git = MemoryGit(directory)
    git.initialize()
    assert git.commit("dream: nothing") is None


def test_a_missing_git_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    """A host without git still gets memory; it just does not get history. Raising here would
    make the audit trail a prerequisite for the feature."""
    directory = tmp_path / "memory"
    directory.mkdir()
    git = MemoryGit(directory)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))

    assert git.available is False
    assert git.commit("dream: nothing") is None
    assert git.log() == []
    assert git.describe_changes() == ""


# --------------------------------------------------------------------------- #
# dream's prompt and tool boundary
# --------------------------------------------------------------------------- #
def test_the_routing_table_is_in_the_prompt(tmp_path: Path) -> None:
    """The routing table is what stops four files from becoming one long file — the single
    failure mode that makes a memory store useless. If it is not in the prompt, it is not in
    effect."""
    from app.agent.memory import DREAM_PROMPT

    for name in ("MEMORY.md", "USER.md", "SOUL.md", "SKILL.md"):
        assert name in DREAM_PROMPT


def test_dreams_tools_exclude_bash(tmp_path: Path) -> None:
    """Dream curates notes. A Dream that can run shell commands is a second agent with the
    operator's full privileges, and the four-file restriction in the prompt would be decorative.
    """
    settings = _settings(tmp_path)
    argv = PiDreamRunner(settings).build_argv(directory=tmp_path)

    assert "--tools" in argv
    tools = argv[argv.index("--tools") + 1]
    assert "bash" not in tools
    assert set(tools.split(",")) == {"read", "edit", "write"}


def test_the_real_runner_can_reach_its_transport(tmp_path: Path) -> None:
    """`PiDreamRunner` must import `PiProcess` from *this* package's `rpc`, not the parent's.

    Every other test in this file injects a fake runner, so the one line that loads the real
    transport is never executed by them — and a relative import with one dot too few
    (`..rpc` instead of `.rpc`) resolves to `app.rpc`, which does not exist. The failure is
    invisible: `Dream.run` catches it, the cursor does not advance, and the run reports
    "dream failed; will retry" forever while every test stays green.

    That is not hypothetical. It was the state of this module until a real end-to-end run
    against a real Pi process found it. This test is the cheap stand-in for that run: point the
    runner at a binary that cannot exist, and assert the failure is *the spawn* rather than an
    import — which means everything up to the spawn worked.
    """
    settings = _settings(tmp_path, pi_bin=("definitely-not-a-real-binary-xyz",))
    runner = PiDreamRunner(settings)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(runner.run("prompt", directory=tmp_path))

    message = str(raised.value)
    # `PiProcess.start` re-raises the OSError as a RuntimeError naming the fix; a ModuleNotFound
    # or AttributeError here would mean the import path is wrong, and that is what this asserts.
    assert "definitely-not-a-real-binary-xyz" in message, message
    assert not isinstance(raised.value, (ModuleNotFoundError, ImportError))


def test_dreams_tools_are_not_inherited_from_the_agent(tmp_path: Path) -> None:
    """The agent's allowlist is a statement about what the *agent* may do. Dream is a different
    actor with a smaller brief, so it must not simply reuse `CSD_PI_TOOLS`."""
    settings = _settings(tmp_path, pi_tools=("read", "grep", "find", "ls"))
    argv = PiDreamRunner(settings).build_argv(directory=tmp_path)
    tools = argv[argv.index("--tools") + 1]
    assert "grep" not in tools and "ls" not in tools


def test_the_dream_model_defaults_to_the_agents_own(tmp_path: Path) -> None:
    """Dream reads the same files it curates. A different model would read a different dialect of
    them, so the default is the agent's own model and an override is explicit."""
    settings = _settings(tmp_path)
    argv = PiDreamRunner(settings).build_argv(directory=tmp_path)
    assert argv[argv.index("--model") + 1] == "fake-1"

    overridden = _settings(tmp_path, memory_model="other-model")
    argv = PiDreamRunner(overridden).build_argv(directory=tmp_path)
    assert argv[argv.index("--model") + 1] == "other-model"


# --------------------------------------------------------------------------- #
# configuration defaults
# --------------------------------------------------------------------------- #
def test_dream_runs_on_a_schedule_by_default() -> None:
    """A memory layer nobody switches on is a layer that does not exist.

    The first version defaulted this to off, on the argument that a channel must not spend the
    operator's model budget on its own initiative (docs/design.md §18 keeps the reversal). Once
    the agent re-reads its memory on every prompt, an unconsolidated journal is a visible hole in
    every answer, and nanobot — which also ships Dream enabled, every 2 hours — is the comparison
    that matters rather than a hypothetical cheaper deployment.
    """
    import os

    from app.config import Settings

    assert Settings.__dataclass_fields__["memory_interval_h"].default == 2.0
    # And the environment parser agrees, since that is the path production takes.
    os.environ.pop("CSD_MEMORY_INTERVAL_H", None)
    assert Settings.from_env(require_token=False).memory_interval_h == 2.0


# --------------------------------------------------------------------------- #
# the wiring: the hook, and the two routes
# --------------------------------------------------------------------------- #
# The tests above exercise the memory layer directly. These exercise the path production
# actually takes — a settled run arriving from a backend, through the supervisor, into the
# journal — because the interesting failures live in the wiring rather than in the parts:
# a hook attached to the wrong frame journals nothing and looks exactly like a working system.

#: A Pi stand-in that echoes the prompt and settles. Enough for the supervisor to treat the run
#: as finished, which is the event the journal hook is attached to.
FAKE_PI = r'''
import json, os, sys

def send(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()

def respond(cmd, **extra):
    send({"type": "response", "id": cmd.get("id"), "command": cmd.get("type"),
          "success": True, **extra})

# The transcript goes to disk, the way the real Pi writes it: `{stamp}_{id}.jsonl`, with a header
# carrying the id. The journal is derived from that file, which is this whole file's subject — a
# stub that recorded only in memory would make every journal test here vacuous, and would have
# kept them green straight through the change that moved this read from a table to a file.
argv = sys.argv[1:]
def arg(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default

session_dir = arg("--session-dir")
session_id = arg("--session-id", "stub-session")
session_file = None
if session_dir:
    os.makedirs(session_dir, exist_ok=True)
    session_file = os.path.join(session_dir, "2026-01-01T00-00-00-000Z_%s.jsonl" % session_id)
    with open(session_file, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "session", "version": 3, "id": session_id,
                                 "cwd": os.getcwd()}) + "\n")

def record(role, text, **extra):
    if session_file is None:
        return
    with open(session_file, encoding="utf-8") as handle:
        lines = sum(1 for _ in handle)
    with open(session_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "message", "id": "e%d" % lines, "parentId": None,
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": role, "content": [{"type": "text", "text": text}],
                        "timestamp": "2026-01-01T00:00:00.000Z", **extra},
        }, ensure_ascii=False) + "\n")

buffer = b""
while True:
    chunk = sys.stdin.buffer.read(1)
    if not chunk:
        break
    buffer += chunk
    while b"\n" in buffer:
        raw, _, buffer = buffer.partition(b"\n")
        raw = raw.rstrip(b"\r").strip()
        if not raw:
            continue
        cmd = json.loads(raw.decode("utf-8"))
        kind = cmd.get("type")
        if kind == "get_state":
            respond(cmd, data={"model": {"provider": "fake", "id": "fake-1"},
                               "sessionFile": session_file, "sessionId": session_id,
                               "isStreaming": False})
        elif kind == "get_entries":
            # Nothing reads entries over RPC any more — the conversation is read from the file
            # above — so this answers emptily rather than modelling a protocol path no caller
            # takes. If something starts asking again, an empty answer here is a visible failure
            # rather than a silent one.
            respond(cmd, data={"entries": [], "leafId": None})
        elif kind in ("prompt", "follow_up"):
            text = cmd.get("message", "")
            respond(cmd)
            record("user", text)
            send({"type": "message_end",
                  "message": {"role": "user",
                              "content": [{"type": "text", "text": text}]}})
            echo = "echo: " + text
            send({"type": "agent_start"})
            send({"type": "message_update",
                  "assistantMessageEvent": {"type": "text_delta", "delta": echo}})
            record("assistant", echo)
            send({"type": "message_end",
                  "message": {"role": "assistant",
                              "content": [{"type": "text", "text": echo}]}})
            send({"type": "agent_settled"})
'''


def _live_settings(tmp_path: Path, **overrides) -> Settings:
    script = tmp_path / "fake_pi.py"
    script.write_text(FAKE_PI, encoding="utf-8")
    return _settings(
        tmp_path,
        pi_bin=tuple(shlex.split(f'"{sys.executable}" "{script}"')),
        **overrides,
    )


@pytest.fixture()
def agent_app(tmp_path: Path):
    settings = _live_settings(tmp_path)
    db = Database(settings.db_path)
    app = create_app(
        settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker()
    )
    with TestClient(app) as client:
        yield client, db, settings
    db.close()


def _headers(token: str = ACCESS_TOKEN) -> dict[str, str]:
    return {"X-CPD-Token": token}


def _wait_for(predicate, *, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not met before the timeout")


def test_a_settled_run_reaches_the_journal(agent_app, tmp_path: Path) -> None:
    """The end-to-end wiring, and the only test that would catch a hook on the wrong frame.

    `agent_settled` is what "the run is over" means (docs/design.md §18), delivered as
    `Status(streaming=False)`. A hook attached to a delta, or to `agent_end` — which fires once
    per low-level run rather than once per conversation turn — would journal the wrong amount at
    the wrong time, and every unit test above would still pass.
    """
    client, _db, _settings_ = agent_app
    thread_id = client.post("/api/agent/session", headers=_headers()).json()["id"]
    response = client.post(
        f"/api/agent/threads/{thread_id}/messages",
        json={"content": "x" * 600},
        headers=_headers(),
    )
    assert response.status_code == 202, response.text

    journal = Journal(tmp_path / "memory" / "history.jsonl")
    entry = _wait_for(lambda: (journal.read() or [None])[0])

    assert entry is not None
    assert "echo: " in entry.content
    assert entry.index == 1


def test_the_memory_route_names_the_directory_and_the_files(agent_app, tmp_path: Path) -> None:
    client, _db, _settings_ = agent_app
    body = client.get("/api/agent/memory", headers=_headers()).json()

    assert body["directory"] == str(tmp_path / "memory")
    assert {f["name"] for f in body["files"]} == {"MEMORY.md", "USER.md", "SOUL.md"}
    assert body["interval_hours"] == 2.0


def test_the_memory_route_does_not_ship_file_contents(agent_app, tmp_path: Path) -> None:
    """These files are the agent's working memory of the user. A status route is not where that
    gets sent to a phone that only asked whether memory is on."""
    client, _db, _settings_ = agent_app
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "USER.md").write_text("works on a secret project", encoding="utf-8")

    body = client.get("/api/agent/memory", headers=_headers()).json()
    assert "secret" not in json.dumps(body)
    sizes = {f["name"]: f for f in body["files"]}
    assert sizes["USER.md"]["present"] and sizes["USER.md"]["chars"] > 0


def test_the_memory_route_is_404_without_a_memory_directory(tmp_path: Path) -> None:
    """No workspace and no `CSD_MEMORY_DIR` means there is nowhere to put memory — and the route
    says so rather than inventing a location."""
    settings = _settings(
        tmp_path, pi_workspace=None, memory_dir=None, dashboard_token=ACCESS_TOKEN
    )
    assert settings.resolved_memory_dir is None
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        assert client.get("/api/agent/memory", headers=_headers()).status_code == 404
        assert client.post("/api/agent/memory/dream", headers=_headers()).status_code == 404
    db.close()


def test_every_memory_route_is_gated_by_the_dashboard_token(agent_app) -> None:
    """The memory routes sit behind the same credential as everything else under `/api/agent`.

    They are, on their face, the least dangerous routes on the page: one reads a few file sizes,
    the other asks the agent to tidy its notes. But `/memory/dream` *starts a process and spends
    a model call*, and "unset means disabled" has to cover it for the reason §17 gives for the
    rest — the direction of the default is what matters, not the size of the action.

    This is the *wrong token* half: the server has one configured, so the answer is 401.
    """
    client, _db, _s = agent_app
    assert client.get("/api/agent/memory").status_code == 401
    assert client.post("/api/agent/memory/dream").status_code == 401
    assert client.get("/api/agent/memory", headers=_headers("wrong")).status_code == 401


def test_memory_routes_are_disabled_not_open_when_no_token_is_configured(tmp_path: Path) -> None:
    """The other half, and the one that matters: a server with no `CSD_ACCESS_TOKEN` answers 403.

    `401` and `403` are different problems and the page tells them apart — "I need your token"
    versus "this is switched off here". Collapsing them would send someone hunting for a token
    that does not exist. `/memory/dream` is why this route group cannot be the exception: it
    spawns a Pi process, so an open default would be remote process execution.
    """
    settings = _settings(tmp_path, dashboard_token=None)
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    with TestClient(app) as client:
        assert client.get("/api/agent/memory").status_code == 403
        assert client.post("/api/agent/memory/dream").status_code == 403
    db.close()


def test_dream_completes_against_a_real_subprocess(agent_app, tmp_path: Path) -> None:
    """The whole slow path with a real child process: spawn, prompt, settle, report.

    Every other Dream test injects a fake runner, which means the transport itself — the argv,
    the event loop's reader task, and above all **what the run waits for** — is never exercised.
    That gap hid two real bugs, both found only by running Dream for real:

    1. `PiDreamRunner` imported `PiProcess` from `..rpc`, one dot too many, so the run died with
       `ModuleNotFoundError` inside `Dream.run`'s `except`; the cursor never advanced and every
       test stayed green.
    2. The run awaited `proc.wait()`. Pi in RPC mode is a long-lived server that goes on reading
       stdin after a prompt finishes, so that never returns — Dream hung until its timeout and
       then reported a failure it had not had.

    Both are invisible to a fake. This test uses the real runner and the stub Pi process, and
    asserts the shape the fixed version produces: the run completes, changes nothing (the stub
    edits no files), and **advances the cursor anyway** — "already current" is a result.
    """
    client, _db, _settings_ = agent_app
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    Journal(directory / "history.jsonl").append(
        JournalEntry(index=1, timestamp="t", content="something to consolidate")
    )

    response = client.post("/api/agent/memory/dream", headers=_headers())
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["ran"] is True, body
    assert body["changed"] is False, "the stub Pi edits nothing"
    assert body["advanced"] is True, "a completed run advances, changed or not"
    assert body["commit"] is None, "nothing moved, so there is nothing to commit"

    # And the API agrees with the database that the batch is consumed.
    assert client.get("/api/agent/memory", headers=_headers()).json()["pending_entries"] == 0


def test_a_dream_whose_agent_is_missing_reports_it_and_keeps_the_batch(tmp_path: Path) -> None:
    """The failure path, end to end: no Pi binary means Dream raises, `run` swallows it, and the
    cursor stays put so the same batch is retried.

    Answers 200 rather than 5xx on purpose. The request succeeded in doing what it asked — run
    Dream — and `advanced: false` is a more useful answer than a status code that would collapse
    "nothing to do", "already current" and "the process would not start" into one failure.
    """
    settings = _settings(
        tmp_path, pi_bin=("definitely-not-a-real-binary-xyz",), dashboard_token=ACCESS_TOKEN
    )
    db = Database(settings.db_path)
    app = create_app(settings=settings, db=db, broker=Broker(), agent_broker=AgentBroker())
    directory = settings.resolved_memory_dir
    assert directory is not None
    directory.mkdir(parents=True, exist_ok=True)
    Journal(directory / "history.jsonl").append(
        JournalEntry(index=1, timestamp="t", content="something to consolidate")
    )

    with TestClient(app) as client:
        response = client.post("/api/agent/memory/dream", headers=_headers())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ran"] is True
        assert body["advanced"] is False
        assert body["commit"] is None

        # And the batch is still pending, because nothing consumed it.
        assert (
            client.get("/api/agent/memory", headers=_headers()).json()["pending_entries"] == 1
        )
    db.close()


# --------------------------------------------------------------------------- #
# recall — the half that makes written memory actually memory
# --------------------------------------------------------------------------- #
def test_recall_is_none_when_there_is_nothing_to_recall(tmp_path: Path) -> None:
    assert recall_prompt(tmp_path) is None


def test_recall_carries_all_three_files_and_the_journal(tmp_path: Path) -> None:
    (tmp_path / "USER.md").write_text("prefers Chinese", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("sqlite is the record", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("be terse", encoding="utf-8")
    (tmp_path / "history.jsonl").write_text('{"cursor": 1}\n', encoding="utf-8")

    prompt = recall_prompt(tmp_path)

    assert prompt is not None
    assert "prefers Chinese" in prompt
    assert "sqlite is the record" in prompt
    assert "be terse" in prompt
    assert str(tmp_path / "history.jsonl") in prompt
    # It is context, not an instruction that outranks the user.
    assert "the user wins" in prompt


def test_recall_is_capped_to_fit_in_an_argument(tmp_path: Path) -> None:
    """It travels as one argv element; Windows caps a command line at 32,767 characters."""
    (tmp_path / "MEMORY.md").write_text("x" * (RECALL_MAX_CHARS * 2), encoding="utf-8")

    prompt = recall_prompt(tmp_path)

    assert prompt is not None
    assert len(prompt) <= RECALL_MAX_CHARS + 100
    assert "memory truncated" in prompt


def test_the_recall_file_is_derived_and_cleared_when_the_memory_is_empty(tmp_path: Path) -> None:
    """The file the Pi extension reads is a view of the three files, not a fourth memory.

    Clearing it matters more than writing it: a stale appendix surviving an emptied memory is the
    one failure a reader cannot see from the outside.
    """
    path = write_recall_file(tmp_path)
    assert path.read_text(encoding="utf-8") == ""

    (tmp_path / "MEMORY.md").write_text("a fact", encoding="utf-8")
    write_recall_file(tmp_path)
    assert "a fact" in path.read_text(encoding="utf-8")

    (tmp_path / "MEMORY.md").unlink()
    write_recall_file(tmp_path)
    assert path.read_text(encoding="utf-8") == ""
