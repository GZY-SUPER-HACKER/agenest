"""What the liveness probe must leave alone.

The heartbeat exists to close sessions that ended without saying so. An entry that *did* say
so — `SessionEnd` reported it, and `ended_at` records that — has nothing left to probe: the row
on the dashboard is already terminal. Probing it would "close" a closed row and then drop the
entry, and that entry is the only thing carrying a conversation's name across the editor's
process restart (docs/design.md §39).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SDK = Path(__file__).resolve().parents[2] / "sdk" / "csd-agent"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from csd_agent import heartbeat, tracked  # noqa: E402  (the path insert has to come first)


def ended_entry() -> tracked.Tracked:
    return tracked.Tracked(
        cc_session_id="cc-1",
        csd_session_id="s_abc",
        pid=1234,
        cwd="/work",
        started_at=0.0,
        title="fix the flaky test",
        ended_at=1.0,
    )


def test_an_ended_entry_is_not_probed_or_closed(monkeypatch) -> None:
    entries = {"cc-1": ended_entry()}
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(heartbeat.install, "repair_hooks", lambda note=None: False)
    monkeypatch.setattr(heartbeat, "report_host", lambda **_kwargs: None)
    monkeypatch.setattr(heartbeat.access, "sync", lambda **_kwargs: None)
    monkeypatch.setattr(heartbeat.tracked, "load", lambda: dict(entries))
    monkeypatch.setattr(
        heartbeat.tracked, "put", lambda entry: entries.__setitem__(entry.cc_session_id, entry)
    )
    monkeypatch.setattr(
        heartbeat.tracked,
        "drop",
        lambda cc_id: pytest.fail("an entry reported closed must be kept for its name"),
    )
    monkeypatch.setattr(heartbeat.tracked, "prune", lambda: entries)
    monkeypatch.setattr(heartbeat.tracked, "process_alive", lambda entry: False)
    monkeypatch.setattr(heartbeat.runtime, "cpctl", lambda *args: calls.append(args))

    assert heartbeat.run() == 0
    assert calls == [], "no heartbeat and no second close for a row SessionEnd already reported"
