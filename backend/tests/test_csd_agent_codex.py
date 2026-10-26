"""The Codex reporter: a turn-level adapter over Codex's Claude-compatible hooks.

Two contracts are pinned here:

- **The hooks file belongs to the user.** Install merges our matcher groups into
  `$CODEX_HOME/hooks.json`, a re-install replaces ours instead of appending, and uninstall
  removes only groups whose command carries our marker.
- **The reporter never answers an approval.** `PermissionRequest` is report-only: Codex's
  request is broadcast and the first answer wins, so a deny here would race the human in the
  TUI. The tests assert the handler makes no decision output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SDK = Path(__file__).resolve().parents[2] / "sdk" / "csd-agent"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from csd_agent import codex, codex_hooks, config, tracked  # noqa: E402


@pytest.fixture(autouse=True)
def scratch_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "csd"
    monkeypatch.setattr(config, "agent_home", lambda: home)
    return home


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_merges_and_preserves_foreign_hooks(tmp_path: Path) -> None:
    path = codex.hooks_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "description": "mine",
        "hooks": {
            "PreToolUse": [{
                "matcher": "^Bash$",
                "hooks": [{"type": "command", "command": "my-scanner"}],
            }],
        },
    }), encoding="utf-8")

    first = codex.install(tmp_path)
    assert first.ok
    data = read(path)
    assert data["description"] == "mine", "the user's top-level keys survive"
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "my-scanner"
    for event in codex.HOOK_EVENTS:
        groups = data["hooks"][event]
        assert len(groups) == 1
        assert codex._is_ours(groups[0])

    second = codex.install(tmp_path)
    assert second.ok
    data = read(path)
    for event in codex.HOOK_EVENTS:
        assert len(data["hooks"][event]) == 1, "a re-install replaces, never duplicates"


def test_uninstall_removes_only_our_groups(tmp_path: Path) -> None:
    path = codex.hooks_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{
                "matcher": "^Bash$",
                "hooks": [{"type": "command", "command": "my-scanner"}],
            }],
        },
    }), encoding="utf-8")
    codex.install(tmp_path)

    assert codex.uninstall(tmp_path) == 0
    data = read(path)
    assert "PreToolUse" in data["hooks"]
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "my-scanner"
    for event in codex.HOOK_EVENTS:
        assert event not in data["hooks"], "an event with only our group is removed"


def test_a_malformed_file_is_reported_and_never_overwritten(tmp_path: Path) -> None:
    path = codex.hooks_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    result = codex.install(tmp_path)
    assert not result.ok
    assert path.read_text(encoding="utf-8") == "{ not json"


class FakeCalls:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.stored: list[tracked.Tracked] = []
        self.dropped: list[str] = []

    def cpctl(self, *args: str):
        from csd_agent import runtime
        self.calls.append(args)
        return runtime.Result(True, stdout="s_codex\n")

    def of(self, kind: str) -> list[tuple[str, ...]]:
        """Calls whose argv contains this exact literal, e.g. the `session start` verbs."""
        return [call for call in self.calls if kind in call]

    def events(self, event_type: str) -> list[tuple[str, ...]]:
        return [
            call for call in self.calls
            if "--type" in call and call[call.index("--type") + 1] == event_type
        ]


@pytest.fixture()
def calls(monkeypatch) -> FakeCalls:
    fake = FakeCalls()
    monkeypatch.setattr(codex_hooks.runtime, "cpctl", fake.cpctl)
    monkeypatch.setattr(codex_hooks, "_log", lambda _message: None)
    monkeypatch.setattr(codex_hooks, "_host_label", lambda: "box")
    monkeypatch.setattr(codex_hooks.tracked, "find_pid", lambda _prefixes: 0)
    monkeypatch.setattr(
        codex_hooks.host_report, "report", lambda **_kwargs: codex_hooks.runtime.Result(True)
    )
    return fake


def entry() -> tracked.Tracked:
    return tracked.Tracked(
        cc_session_id="codex-1",
        csd_session_id="s_codex",
        pid=0,
        cwd="/work/demo",
        started_at=1.0,
        last_seen_at=1.0,
    )


def test_session_start_creates_a_codex_session(monkeypatch, calls: FakeCalls) -> None:
    monkeypatch.setattr(codex_hooks.tracked, "get", lambda _sid: None)
    monkeypatch.setattr(codex_hooks.tracked, "put", calls.stored.append)

    codex_hooks.run(raw=json.dumps({
        "hook_event_name": "SessionStart",
        "session_id": "codex-1",
        "cwd": "/work/demo",
        "source": "startup",
    }))

    start = calls.of("session")[0]
    assert start[:2] == ("session", "start")
    assert start[start.index("--agent") + 1] == "codex"
    assert start[start.index("--host") + 1] == "box"
    assert calls.stored[-1].csd_session_id == "s_codex"


def test_prompt_and_stop_are_the_turn_vocabulary(monkeypatch, calls: FakeCalls) -> None:
    current = entry()
    monkeypatch.setattr(codex_hooks.tracked, "get", lambda _sid: current)
    monkeypatch.setattr(codex_hooks.tracked, "put", lambda _entry: None)

    codex_hooks.run(raw=json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "codex-1",
        "prompt": "Fix the flaky test\nmore detail",
        "turn_id": "turn-7",
    }))
    update = calls.of("update")[0]
    assert update[update.index("--title") + 1] == "Fix the flaky test"
    progress = calls.events("progress")[0]
    assert progress[progress.index("-m") + 1] == "Turn 1: Fix the flaky test"

    codex_hooks.run(raw=json.dumps({
        "hook_event_name": "Stop",
        "session_id": "codex-1",
        "turn_id": "turn-7",
        "last_assistant_message": "done",
    }))
    boundary = calls.events("turn_completed")[0]
    assert boundary[boundary.index("-m") + 1] == "Reply finished (turn 1)"
    assert "turn-7" in boundary[boundary.index("--key") + 1]


def test_permission_request_reports_but_returns_no_decision(
    monkeypatch, calls: FakeCalls, capsys
) -> None:
    monkeypatch.setattr(codex_hooks.tracked, "get", lambda _sid: entry())

    codex_hooks.run(raw=json.dumps({
        "hook_event_name": "PermissionRequest",
        "session_id": "codex-1",
        "turn_id": "turn-7",
        "tool_name": "shell",
        "tool_input": {"command": "rm -rf build"},
    }))

    awaiting = calls.events("awaiting_user")[0]
    assert awaiting[awaiting.index("-m") + 1] == "approval needed: shell"
    captured = capsys.readouterr()
    assert captured.out == "", "a hook that prints JSON would be answering the request"


def test_session_end_closes_and_drops(monkeypatch, calls: FakeCalls) -> None:
    monkeypatch.setattr(codex_hooks.tracked, "get", lambda _sid: entry())
    monkeypatch.setattr(codex_hooks.tracked, "drop", calls.dropped.append)

    codex_hooks.run(raw=json.dumps({
        "hook_event_name": "SessionEnd",
        "session_id": "codex-1",
    }))

    finish = calls.of("finish")[0]
    assert finish[finish.index("--status") + 1] == "completed"
    assert calls.dropped == ["codex-1"]
