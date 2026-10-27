"""What `csd-agent` does when its own report fails.

The rule the connector follows everywhere is "a failed report is logged and the work carries
on". These tests pin the second half that was missing: after a *failed* report the entry must
stay tracked, because the heartbeat is the only thing left that can close the session on the
server (docs/design.md §35). Dropping it on the failure path is how one PATCH that returned
500 left a row at `running` with nothing on the host that knew it existed.
"""

from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path

import pytest

SDK = Path(__file__).resolve().parents[2] / "sdk" / "csd-agent"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from csd_agent import hooks, payload, runtime, tracked  # noqa: E402  (the path insert has to come first)


def entry() -> tracked.Tracked:
    return tracked.Tracked(
        cc_session_id="cc-1",
        csd_session_id="s_abc",
        pid=1234,
        cwd="/work",
        started_at=0.0,
    )


@pytest.fixture(autouse=True)
def quiet_log(monkeypatch, tmp_path):
    """Keep `hook.log` and the session state out of the developer's real `~/.csd`."""
    lines: list[str] = []
    monkeypatch.setattr(hooks, "_log", lines.append)
    # Several handlers write the tracked entry for real; without this the fixture entry
    # `cc-1` lands in the machine's actual connector state (and in the map `cpctl task` reads).
    monkeypatch.setattr(tracked.config, "tracked_file", lambda: tmp_path / "state" / "tracked.json")
    return lines


def test_tracked_put_publishes_the_session_for_cpctl(tmp_path, monkeypatch) -> None:
    """The agent's `cpctl task` needs a path from its working directory to the session id.

    The connector runs in a process the agent cannot see, and Pi/DSH do the same thing in
    their own files; this is the csd-agent half of that convention.
    """
    path = tmp_path / "state" / "tracked.json"
    monkeypatch.setattr(tracked.config, "tracked_file", lambda: path)

    tracked.put(entry())

    map_path = path.parent / "sessions.d" / "csd-agent.json"
    data = json.loads(map_path.read_text(encoding="utf-8"))
    published = data["by_cwd"]["/work"][0]
    assert published["session_id"] == "s_abc"
    assert published["harness_id"] == "cc-1"

    tracked.drop("cc-1")

    data = json.loads(map_path.read_text(encoding="utf-8"))
    assert data["by_cwd"] == {}


def test_a_failed_session_end_keeps_the_entry_tracked(quiet_log, monkeypatch) -> None:
    dropped: list[str] = []
    monkeypatch.setattr(tracked, "get", lambda cc_id: entry() if cc_id == "cc-1" else None)
    monkeypatch.setattr(tracked, "drop", dropped.append)
    monkeypatch.setattr(
        hooks.runtime, "cpctl", lambda *args: runtime.Result(False, detail="dashboard unreachable")
    )

    hooks.on_session_end({"session_id": "cc-1", "reason": "other"})

    assert dropped == []


def test_a_reported_session_end_drops_the_entry(monkeypatch) -> None:
    dropped: list[str] = []
    monkeypatch.setattr(tracked, "get", lambda cc_id: entry() if cc_id == "cc-1" else None)
    monkeypatch.setattr(tracked, "drop", dropped.append)
    monkeypatch.setattr(hooks.runtime, "cpctl", lambda *args: runtime.Result(True, stdout="ok\n"))

    hooks.on_session_end({"session_id": "cc-1", "reason": "other"})

    assert dropped == ["cc-1"]


def test_stop_reports_a_turn_boundary_and_never_a_status(monkeypatch) -> None:
    """`Stop` is a per-reply signal, so it must not close the session (§15, §36).

    The event is what lets the page stop saying "running" while an editor keeps the process
    open between prompts. Sending `session finish` here would make the first reply terminal
    and, because terminal states are sticky, every later event would be discarded.
    """
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(tracked, "get", lambda cc_id: entry() if cc_id == "cc-1" else None)
    monkeypatch.setattr(
        hooks.runtime,
        "cpctl",
        lambda *args: (calls.append(args), runtime.Result(True, stdout="ok\n"))[1],
    )

    hooks.on_stop({"session_id": "cc-1", "stop_hook_active": False})

    assert len(calls) == 1
    kind, session_flag, session_id = calls[0][:3]
    assert (kind, session_flag, session_id) == ("event", "--session", "s_abc")
    assert "--type" in calls[0]
    assert calls[0][calls[0].index("--type") + 1] == "turn_completed"
    assert "finish" not in calls[0]


def test_a_prompt_reports_progress_and_bumps_the_turn(monkeypatch) -> None:
    """The prompt is the only "working on X" report between the two ends of a turn.

    With `PostToolUse` deliberately unwired, this is what gives the timeline a line per turn.
    The turn number is in the message because `cpctl` derives its idempotency key from it —
    two identical prompts ("continue") would otherwise be deduplicated into one row.
    """
    calls: list[tuple[str, ...]] = []
    stored: list[tracked.Tracked] = []
    monkeypatch.setattr(tracked, "get", lambda cc_id: entry() if cc_id == "cc-1" else None)
    monkeypatch.setattr(tracked, "put", stored.append)
    monkeypatch.setattr(
        hooks.runtime,
        "cpctl",
        lambda *args: (calls.append(args), runtime.Result(True, stdout="ok\n"))[1],
    )

    hooks.on_user_prompt({"session_id": "cc-1", "prompt": "fix the tests\nand the docs"})

    assert [c[:2] for c in calls] == [("session", "update"), ("event", "--session")]
    event_call = calls[1]
    assert event_call[event_call.index("--type") + 1] == "progress"
    assert "Turn 1: fix the tests" in event_call
    assert stored and stored[0].turn == 1


def test_a_system_message_never_becomes_the_session_title(monkeypatch) -> None:
    """`<task-notification>` arrives on `UserPromptSubmit`, but the user did not write it.

    Naming the session from every payload is how the deployed dashboard ended up with rows
    called `<task-notification>`. The turn still advances: `Stop` keys its boundary on the
    number, so reusing the previous one would deduplicate the boundary away.
    """
    calls: list[tuple[str, ...]] = []
    stored: list[tracked.Tracked] = []
    current = entry()
    current.turn = 2
    monkeypatch.setattr(tracked, "get", lambda cc_id: current if cc_id == "cc-1" else None)
    monkeypatch.setattr(tracked, "put", stored.append)
    monkeypatch.setattr(
        hooks.runtime,
        "cpctl",
        lambda *args: (calls.append(args), runtime.Result(True, stdout="ok\n"))[1],
    )

    hooks.on_user_prompt({
        "session_id": "cc-1",
        "prompt": "<task-notification>\n<task-id>abc</task-id>\n</task-notification>",
    })

    assert calls == [], "no title update and no 'Turn N:' line for a system message"
    assert stored[-1].turn == 3
    # The tag decides, not the words: a prompt that merely mentions one is still a prompt.
    assert hooks._is_system_message("explain <task-notification>") is False


def test_a_utf8_prompt_is_not_decoded_with_the_console_codec(monkeypatch) -> None:
    """The payload is UTF-8; `sys.stdin` on Windows is the ANSI code page + surrogateescape.

    Reading it through the text wrapper turned a Chinese prompt into mojibake, and — for UTF-8
    byte pairs that are not valid GBK — into lone surrogates. The API rejects those, and
    encoding its own 422 body raised, so the client saw a 500: every session on the affected
    host kept the `Claude Code · <project>` placeholder forever (docs/design.md §43).
    """
    raw = '{"session_id": "cc-1", "prompt": "你好世界"}'.encode("utf-8")
    # The Windows shape, reproduced exactly: GBK with surrogateescape over UTF-8 bytes.
    fake = io.TextIOWrapper(io.BytesIO(raw), encoding="gbk", errors="surrogateescape")
    monkeypatch.setattr(sys, "stdin", fake)

    assert payload.read_stdin() == '{"session_id": "cc-1", "prompt": "你好世界"}'


def test_a_chinese_prompt_names_the_session(monkeypatch) -> None:
    """The title that reaches `cpctl` must be text the API can store."""
    raw = '{"session_id": "cc-1", "prompt": "修复这个登录 bug"}'.encode("utf-8")
    fake = io.TextIOWrapper(io.BytesIO(raw), encoding="gbk", errors="surrogateescape")
    monkeypatch.setattr(sys, "stdin", fake)

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(tracked, "get", lambda cc_id: entry() if cc_id == "cc-1" else None)
    monkeypatch.setattr(tracked, "put", lambda _entry: None)
    monkeypatch.setattr(
        hooks.runtime,
        "cpctl",
        lambda *args: (calls.append(args), runtime.Result(True, stdout="ok\n"))[1],
    )

    assert hooks.run("UserPromptSubmit") == 0

    title_call, progress_call = calls[0], calls[1]
    assert title_call[title_call.index("--title") + 1] == "修复这个登录 bug"
    assert progress_call[progress_call.index("-m") + 1] == "Turn 1: 修复这个登录 bug"


def pending_entry() -> tracked.Tracked:
    pending = entry()
    pending.turn = 3
    pending.pending_text = "Now I'll add the cursor test"
    pending.pending_message_id = "msg-1"
    pending.pending_final = True
    return pending


def test_message_display_accumulates_but_does_not_report(monkeypatch) -> None:
    """`MessageDisplay` is display-time: reporting there would make every answer a progress line.

    The text is held in `tracked.json` until a later `PostToolBatch` proves a tool followed it.
    """
    calls: list[tuple[str, ...]] = []
    stored: list[tracked.Tracked] = []
    current = entry()
    # `tracked.get` re-reads the file in production; returning the same object here makes the
    # second `MessageDisplay` see the text the first one wrote.
    monkeypatch.setattr(tracked, "get", lambda cc_id: current if cc_id == "cc-1" else None)
    monkeypatch.setattr(tracked, "put", stored.append)
    monkeypatch.setattr(
        hooks.runtime,
        "cpctl",
        lambda *args: (calls.append(args), runtime.Result(True, stdout="ok\n"))[1],
    )

    hooks.on_message_display(
        {"session_id": "cc-1", "message_id": "msg-1", "delta": "Now I'll add", "final": False}
    )
    hooks.on_message_display(
        {"session_id": "cc-1", "message_id": "msg-1", "delta": " the cursor test", "final": True}
    )

    assert calls == []
    assert stored[-1].pending_text == "Now I'll add the cursor test"
    assert stored[-1].pending_message_id == "msg-1"
    assert stored[-1].pending_final is True


def test_post_tool_batch_reports_the_pending_message_as_a_checkpoint(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    stored: list[tracked.Tracked] = []
    monkeypatch.setattr(
        tracked, "get", lambda cc_id: pending_entry() if cc_id == "cc-1" else None
    )
    monkeypatch.setattr(tracked, "put", stored.append)
    monkeypatch.setattr(
        hooks.runtime,
        "cpctl",
        lambda *args: (calls.append(args), runtime.Result(True, stdout="ok\n"))[1],
    )

    hooks.on_post_tool_batch({"session_id": "cc-1", "tool_calls": [{"tool_name": "Edit"}]})

    assert len(calls) == 1
    call = calls[0]
    assert call[:3] == ("event", "--session", "s_abc")
    assert call[call.index("--type") + 1] == "checkpoint"
    assert call[call.index("-m") + 1] == "Now I'll add the cursor test"
    assert "msg-1" in call[call.index("--key") + 1]
    import json

    payload = json.loads(call[call.index("--payload") + 1])
    assert payload == {"source": "prose", "turn": 3, "message_id": "msg-1"}
    assert stored[-1].pending_text == ""
    assert stored[-1].pending_message_id == ""


def test_stop_discards_the_pending_message(monkeypatch) -> None:
    """The message with no tool after it is the answer, not a checkpoint (§37)."""
    calls: list[tuple[str, ...]] = []
    stored: list[tracked.Tracked] = []
    monkeypatch.setattr(
        tracked, "get", lambda cc_id: pending_entry() if cc_id == "cc-1" else None
    )
    monkeypatch.setattr(tracked, "put", stored.append)
    monkeypatch.setattr(
        hooks.runtime,
        "cpctl",
        lambda *args: (calls.append(args), runtime.Result(True, stdout="ok\n"))[1],
    )

    hooks.on_stop({"session_id": "cc-1", "stop_hook_active": False})

    assert [c[c.index("--type") + 1] for c in calls if "--type" in c] == ["turn_completed"]
    assert stored[-1].pending_text == ""
    assert stored[-1].pending_final is False


def test_a_failed_checkpoint_keeps_pending_for_the_next_batch(monkeypatch) -> None:
    """The message id is the idempotency key, so a retry cannot duplicate the row."""
    stored: list[tracked.Tracked] = []
    monkeypatch.setattr(
        tracked, "get", lambda cc_id: pending_entry() if cc_id == "cc-1" else None
    )
    monkeypatch.setattr(tracked, "put", stored.append)
    monkeypatch.setattr(
        hooks.runtime,
        "cpctl",
        lambda *args: runtime.Result(False, detail="dashboard unreachable"),
    )

    hooks.on_post_tool_batch({"session_id": "cc-1", "tool_calls": []})

    assert stored == [], "a failed report must not rewrite the pending state"


def test_a_long_checkpoint_stays_inside_the_message_cap(monkeypatch) -> None:
    """The backend rejects 501 characters with a 422; the ellipsis must fit inside 500."""
    calls: list[tuple[str, ...]] = []
    entry = pending_entry()
    entry.pending_text = "x" * 600
    monkeypatch.setattr(tracked, "get", lambda cc_id: entry if cc_id == "cc-1" else None)
    monkeypatch.setattr(tracked, "put", lambda _entry: None)
    monkeypatch.setattr(
        hooks.runtime,
        "cpctl",
        lambda *args: (calls.append(args), runtime.Result(True, stdout="ok\n"))[1],
    )

    hooks.on_post_tool_batch({"session_id": "cc-1", "tool_calls": []})

    message = calls[0][calls[0].index("-m") + 1]
    assert len(message) == 500
    assert message.endswith("…")


def test_concurrent_writers_leave_a_readable_tracked_file(tmp_path: Path, monkeypatch) -> None:
    """Closing one VS Code window fires a `SessionEnd` per open session at the same moment.

    A fixed `tracked.json.tmp` made one of them rename the file another was about to write,
    which raised `FileNotFoundError` out of `os.replace` (observed in a real hook log). The
    temporary name has to be unique per writer.
    """
    path = tmp_path / "state" / "tracked.json"
    monkeypatch.setattr(tracked.config, "tracked_file", lambda: path)

    errors: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for i in range(20):
                tracked.put(
                    tracked.Tracked(
                        cc_session_id=f"cc-{n}-{i}",
                        csd_session_id=f"s_{n}_{i}",
                        pid=n,
                        cwd="/work",
                        started_at=0.0,
                    )
                )
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    # Whatever interleaving won, the file a later hook reads must be a whole JSON object.
    assert isinstance(tracked.load(), dict)
