"""The one file the connector shares with a config manager.

`~/.claude/settings.json` has two writers with different semantics: `csd-agent install` merges
its entries in by marker, while a config manager (cc-switch) writes its own template over the
whole document on every provider switch. The second deletes every hook its template does not
carry — observed on this machine, where the sound hook survived (it is in that template) and
all seven of ours vanished.

`repair_hooks` is the half that notices, and the heartbeat is what calls it. These tests pin
the properties that make it safe to run from a timer: foreign entries survive, a partial loss
is repaired, and a document we cannot parse is left exactly as it was.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SDK = Path(__file__).resolve().parents[2] / "sdk" / "csd-agent"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from csd_agent import heartbeat, install  # noqa: E402


def write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_repair_restores_our_hooks_and_keeps_everyone_elses(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    write(settings, {
        "theme": "dark",
        "hooks": {
            "Stop": [{
                "matcher": "",
                "hooks": [{"type": "command", "command": "notify.wav"}],
            }],
        },
    })
    monkeypatch.setattr(install, "SETTINGS", settings)

    assert install.repair_hooks() is True

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["theme"] == "dark", "the rest of the document is untouched"
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "notify.wav"
    for event in install.HOOK_EVENTS:
        assert any(install._is_ours(group) for group in data["hooks"][event]), event
    assert install.repair_hooks() is False, "a file that already carries them is not rewritten"


def test_one_missing_event_is_enough_to_repair(tmp_path, monkeypatch) -> None:
    """The check is per event: a config manager that carries six of seven still lost one."""
    settings = tmp_path / "settings.json"
    write(settings, {})
    monkeypatch.setattr(install, "SETTINGS", settings)
    assert install.repair_hooks() is True

    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"].pop("SessionEnd")
    write(settings, data)

    assert install.repair_hooks() is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert any(install._is_ours(group) for group in data["hooks"]["SessionEnd"])


def test_repair_leaves_a_document_it_cannot_parse_alone(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(install, "SETTINGS", settings)

    assert install.repair_hooks() is False
    assert settings.read_text(encoding="utf-8") == "{ not json"


def test_repair_does_not_create_a_settings_file(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(install, "SETTINGS", settings)

    assert install.repair_hooks() is False
    assert not settings.exists()


def test_the_heartbeat_repairs_before_its_early_return(tmp_path, monkeypatch) -> None:
    """A host with no tracked sessions still runs the repair — that is the common case."""
    settings = tmp_path / "settings.json"
    write(settings, {"hooks": {}})
    monkeypatch.setattr(install, "SETTINGS", settings)
    monkeypatch.setattr(heartbeat, "report_host", lambda **_kwargs: None)
    monkeypatch.setattr(heartbeat.access, "sync", lambda **_kwargs: None)
    monkeypatch.setattr(heartbeat.tracked, "load", lambda: {})

    assert heartbeat.run() == 0

    data = json.loads(settings.read_text(encoding="utf-8"))
    for event in install.HOOK_EVENTS:
        assert any(install._is_ours(group) for group in data["hooks"][event]), event
