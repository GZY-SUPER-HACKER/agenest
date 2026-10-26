"""The agent-side verdict launcher and skill: the two files that make `cpctl task` usable."""

from __future__ import annotations

import sys
from pathlib import Path

SDK = Path(__file__).resolve().parents[2] / "sdk" / "csd-agent"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from csd_agent import skill  # noqa: E402  (the path insert has to come first)


def _install(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    skills = tmp_path / "skills"
    monkeypatch.setenv("CSD_AGENT_HOME", str(home))
    monkeypatch.setenv("CLAUDE_SKILLS_DIR", str(skills))
    notes = skill.install()
    return home, skills, notes


def test_install_writes_the_launcher_and_the_skill(tmp_path, monkeypatch) -> None:
    home, skills, notes = _install(tmp_path, monkeypatch)

    launcher = home / "bin" / "csd-cpctl"
    skill_md = skills / skill.SKILL_NAME / "SKILL.md"
    assert launcher.exists() and skill_md.exists()
    assert len(notes) == 2

    text = launcher.read_text(encoding="utf-8")
    # A CRLF shell script still runs through `bash`, but the `\r` can become part of an
    # argument — written LF on every platform for that reason.
    assert "\r\n" not in text
    assert "task done" in skill_md.read_text(encoding="utf-8")


def test_the_skill_invokes_the_launcher_through_bash(tmp_path, monkeypatch) -> None:
    """Windows is the reason: PowerShell cannot execute a bash script by path.

    An agent that runs the bare path gets no output and an empty exit code, which reads as
    success. The skill has to spell out `bash "<launcher>"`.
    """
    home, skills, _ = _install(tmp_path, monkeypatch)
    body = (skills / skill.SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    launcher = (home / "bin" / "csd-cpctl").as_posix()
    assert f'bash "{launcher}" task done' in body
    assert f'bash "{launcher}" task failed' in body


def test_uninstall_removes_only_files_carrying_our_marker(tmp_path, monkeypatch) -> None:
    home, skills, _ = _install(tmp_path, monkeypatch)
    skill_md = skills / skill.SKILL_NAME / "SKILL.md"
    # A same-named file we did not write must survive.
    skill_md.write_text("someone else's skill\n", encoding="utf-8")

    assert skill.uninstall() == 0

    assert not (home / "bin" / "csd-cpctl").exists()
    assert skill_md.exists()
