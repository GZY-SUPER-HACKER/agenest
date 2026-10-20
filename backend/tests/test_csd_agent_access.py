"""The connector's half of host access: one line in `authorized_keys`, and only its own.

This file lives in the backend suite because that is the only Python runner this repository has
(see CLAUDE.md), and the code it exercises — `sdk/csd-agent` — is stdlib-only, so importing it
here costs nothing. What it pins is the part of the feature that touches somebody else's machine:
the operator's own keys are in that file too.

The rule under every test: **it may append its own line and delete its own line, and nothing
else**. A connector that rewrote the file would be a connector that could lock its user out.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SDK = Path(__file__).resolve().parents[2] / "sdk" / "csd-agent"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from csd_agent import access  # noqa: E402  (the path insert has to come first)

LAB = "lab-box"
OURS = "ssh-ed25519 AAAATESTKEY csd-agent:lab-box"
REPLACEMENT = "ssh-ed25519 AAAANEWKEY csd-agent:lab-box"
SOMEONE_ELSE = "ssh-ed25519 AAAAOTHER alice@laptop"


@pytest.fixture()
def keys(tmp_path: Path, monkeypatch):
    """Point the module at a scratch home. `authorized_keys` is the one file sshd reads."""
    path = tmp_path / ".ssh" / "authorized_keys"
    monkeypatch.setattr(access, "authorized_keys_path", lambda: path)
    return path


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_the_key_is_added_with_a_comment_we_can_find_again(keys: Path) -> None:
    assert "added the key" in access.install_key(OURS, LAB)
    assert lines(keys) == [OURS]


def test_the_dashboard_comment_does_not_get_appended_twice(keys: Path) -> None:
    """The key arrives with a comment of its own (`ssh-keygen -C`). Appending ours would leave a
    line sshd honours and nothing can match."""
    access.install_key(OURS, LAB)
    assert lines(keys)[0].count("csd-agent:lab-box") == 1


def test_installing_twice_is_one_line(keys: Path) -> None:
    """The heartbeat calls this every five minutes for the life of the machine."""
    access.install_key(OURS, LAB)
    access.install_key(OURS, LAB)
    assert lines(keys) == [OURS]


def test_somebody_elses_key_is_never_touched(keys: Path) -> None:
    """The operator's own keys live in this file. It is not this package's file to rewrite."""
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text(SOMEONE_ELSE + "\n", encoding="utf-8")

    access.install_key(OURS, LAB)

    assert SOMEONE_ELSE in lines(keys)
    assert OURS in lines(keys)


def test_a_new_key_replaces_ours_rather_than_piling_up(keys: Path) -> None:
    """Revoke-then-approve mints a new key. Appending would leave the revoked one authorized
    forever — which is the whole thing revocation is supposed to prevent."""
    access.install_key(OURS, LAB)
    assert "replaced the previous key" in access.install_key(REPLACEMENT, LAB)

    assert lines(keys) == [REPLACEMENT]


def test_a_key_already_present_under_another_comment_is_left_alone(keys: Path) -> None:
    """Same key, different comment: it is already in force, and rewriting the line would be
    editing something we did not write."""
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text("ssh-ed25519 AAAATESTKEY someone-else-added-this\n", encoding="utf-8")

    assert "under another comment" in access.install_key(OURS, LAB)
    assert lines(keys) == ["ssh-ed25519 AAAATESTKEY someone-else-added-this"]


def test_revoking_removes_only_our_line(keys: Path) -> None:
    access.install_key(OURS, LAB)
    keys.write_text(keys.read_text(encoding="utf-8") + SOMEONE_ELSE + "\n", encoding="utf-8")

    assert "removed the key" in access.remove_key(LAB)
    assert lines(keys) == [SOMEONE_ELSE]


def test_revoking_when_there_is_nothing_to_remove_is_not_an_error(keys: Path) -> None:
    """The connector runs on a timer and a revoke can arrive twice; neither may be an exception."""
    assert "no key of ours" in access.remove_key(LAB)
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text(SOMEONE_ELSE + "\n", encoding="utf-8")
    assert "no key of ours" in access.remove_key(LAB)


def test_a_malformed_key_is_refused_rather_than_written(keys: Path) -> None:
    with pytest.raises(ValueError):
        access.install_key("not-a-key", LAB)
    assert not keys.exists()


def test_the_key_file_is_written_with_the_modes_sshd_insists_on(keys: Path) -> None:
    """sshd *silently ignores* a group- or world-writable `authorized_keys`, and the symptom
    points nowhere near the cause. Skipped on Windows, where the mode bits are emulated."""
    import os
    import stat

    if os.name != "posix":  # pragma: no cover - Windows has no meaningful mode bits
        pytest.skip("mode bits are not meaningful on this platform")

    access.install_key(OURS, LAB)
    mode = stat.S_IMODE(keys.stat().st_mode)
    assert mode == 0o600
