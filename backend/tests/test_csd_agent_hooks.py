"""What `csd-agent` does when its own report fails.

The rule the connector follows everywhere is "a failed report is logged and the work carries
on". These tests pin the second half that was missing: after a *failed* report the entry must
stay tracked, because the heartbeat is the only thing left that can close the session on the
server (docs/design.md §35). Dropping it on the failure path is how one PATCH that returned
500 left a row at `running` with nothing on the host that knew it existed.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

SDK = Path(__file__).resolve().parents[2] / "sdk" / "csd-agent"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from csd_agent import hooks, runtime, tracked  # noqa: E402  (the path insert has to come first)


def entry() -> tracked.Tracked:
    return tracked.Tracked(
        cc_session_id="cc-1",
        csd_session_id="s_abc",
        pid=1234,
        cwd="/work",
        started_at=0.0,
    )


@pytest.fixture(autouse=True)
def quiet_log(monkeypatch):
    """Keep `hook.log` out of the developer's real `~/.csd` while these tests run."""
    lines: list[str] = []
    monkeypatch.setattr(hooks, "_log", lines.append)
    return lines


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
