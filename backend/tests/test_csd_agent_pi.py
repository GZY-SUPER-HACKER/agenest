"""`csd-agent install` wiring the standalone `pi` CLI the same way it wires Claude Code.

The code under test is stdlib-only and lives in `sdk/csd-agent`; this file lives in the backend
suite because that is the only Python runner in the repository (see CLAUDE.md). No Pi, Node, or
network is needed: Pi's discovery rule is "every `.js` in `<agent dir>/extensions/`", so the
fixtures are directories and files.

The rule under every test: **install copies one file into the extensions directory, and uninstall
removes exactly that file**. The directory belongs to the user and may hold their own extensions.

The extension's own behaviour is covered in JavaScript by `sdk/pi-csd-extension/test/`; nothing
here starts Pi.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SDK = Path(__file__).resolve().parents[2] / "sdk" / "csd-agent"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from csd_agent import config, pi  # noqa: E402  (the path insert has to come first)

FOREIGN = "someone-elses-extension.ts"


@pytest.fixture(autouse=True)
def scratch_csd_home(tmp_path: Path, monkeypatch) -> Path:
    """Every vendored copy goes under the test's own directory."""
    home = tmp_path / "csd"
    monkeypatch.setattr(config, "agent_home", lambda: home)
    return home


@pytest.fixture()
def agent_dir(tmp_path: Path) -> Path:
    """A Pi agent directory with no extensions directory yet, as a fresh Pi leaves it."""
    home = tmp_path / "pi-agent"
    home.mkdir()
    (home / "sessions").mkdir()
    (home / "auth.json").write_text("{}\n", encoding="utf-8")
    return home


def extensions(agent_dir: Path) -> Path:
    return agent_dir / "extensions"


def installed(agent_dir: Path) -> Path:
    return extensions(agent_dir) / pi.EXTENSION


def test_install_copies_the_extension_where_pi_discovers_it(agent_dir: Path) -> None:
    result = pi.install(home=agent_dir)

    assert result.ok, result.notes
    assert result.installed == [str(installed(agent_dir))]
    target = installed(agent_dir)
    assert target.exists()
    assert not target.is_symlink(), "a copy, not a link: moving the checkout must not break Pi"
    assert pi.MARKER in target.read_text(encoding="utf-8")
    assert target.read_text(encoding="utf-8") == (
        pi.source_dir(None) / pi.EXTENSION
    ).read_text(encoding="utf-8")
    assert any("restart" in note.lower() for note in result.notes)


def test_install_vendors_the_package_without_its_tests(agent_dir: Path, scratch_csd_home: Path) -> None:
    pi.install(home=agent_dir)

    vendored = scratch_csd_home / pi.PACKAGE
    assert (vendored / pi.EXTENSION).exists()
    assert (vendored / "package.json").exists()
    assert not (vendored / "test").exists(), "tests are development-only"


def test_install_is_idempotent(agent_dir: Path) -> None:
    pi.install(home=agent_dir)
    first = installed(agent_dir).read_text(encoding="utf-8")
    result = pi.install(home=agent_dir)

    assert result.ok, result.notes
    assert installed(agent_dir).read_text(encoding="utf-8") == first
    assert sorted(p.name for p in extensions(agent_dir).iterdir()) == [pi.EXTENSION]


def test_install_preserves_the_users_own_extensions(agent_dir: Path) -> None:
    extensions(agent_dir).mkdir(parents=True)
    own = extensions(agent_dir) / FOREIGN
    own.write_text("export default function () {}\n", encoding="utf-8")

    result = pi.install(home=agent_dir)

    assert result.ok, result.notes
    assert own.exists()
    assert own.read_text(encoding="utf-8") == "export default function () {}\n"
    assert installed(agent_dir).exists()


def test_install_refuses_to_replace_a_file_it_did_not_write(agent_dir: Path) -> None:
    extensions(agent_dir).mkdir(parents=True)
    target = installed(agent_dir)
    target.write_text("// a reporter the user wrote, with the same filename\n", encoding="utf-8")

    result = pi.install(home=agent_dir)

    assert not result.ok
    assert any("refusing to replace" in note for note in result.notes)
    assert "the user wrote" in target.read_text(encoding="utf-8"), "the file is untouched"


def test_a_host_without_pi_is_skipped_not_failed(tmp_path: Path) -> None:
    result = pi.install(home=tmp_path / "no-pi-here")

    assert result.ok
    assert result.skipped
    assert "not found" in result.notes[0]


def test_a_missing_reporter_source_is_a_named_failure(agent_dir: Path, tmp_path: Path) -> None:
    result = pi.install(home=agent_dir, pkg_source=tmp_path / "empty")

    assert not result.ok
    assert any("source not found" in note for note in result.notes)
    assert not installed(agent_dir).exists()


def test_uninstall_removes_only_our_file_and_the_vendored_copy(
    agent_dir: Path, scratch_csd_home: Path
) -> None:
    extensions(agent_dir).mkdir(parents=True)
    own = extensions(agent_dir) / FOREIGN
    own.write_text("export default function () {}\n", encoding="utf-8")
    pi.install(home=agent_dir)
    assert installed(agent_dir).exists()

    code = pi.uninstall(home=agent_dir)

    assert code == 0
    assert not installed(agent_dir).exists()
    assert own.exists()
    assert not (scratch_csd_home / pi.PACKAGE).exists()


def test_uninstall_leaves_a_file_it_did_not_write(agent_dir: Path) -> None:
    extensions(agent_dir).mkdir(parents=True)
    target = installed(agent_dir)
    target.write_text("// not ours\n", encoding="utf-8")

    code = pi.uninstall(home=agent_dir)

    assert code == 1
    assert target.exists()


def test_uninstall_with_nothing_installed_is_a_no_op(agent_dir: Path) -> None:
    assert pi.uninstall(home=agent_dir) == 0


def test_status_reports_installed_and_absent(agent_dir: Path, capsys) -> None:
    assert pi.status(agent_dir) == 1
    assert "not installed" in capsys.readouterr().out

    pi.install(home=agent_dir)
    assert pi.status(agent_dir) == 0
    assert "installed" in capsys.readouterr().out


def test_status_calls_an_unmarked_file_not_ours(agent_dir: Path, capsys) -> None:
    extensions(agent_dir).mkdir(parents=True)
    installed(agent_dir).write_text("// not ours\n", encoding="utf-8")

    assert pi.status(agent_dir) == 1
    assert "not ours" in capsys.readouterr().out


def test_pi_agent_dir_honours_the_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "from-env"))
    assert pi.pi_agent_dir() == tmp_path / "from-env"

    # An explicit argument wins, because `--pi-home` is the operator overriding the estimate.
    assert pi.pi_agent_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_the_main_installer_invokes_the_pi_step(agent_dir: Path, monkeypatch) -> None:
    """`csd-agent install` is the one command that wires every agent; this pins the join."""
    from csd_agent import install as installer_mod

    cfg = config.AgentConfig(cloud_host="127.0.0.1", local=True)
    installer = installer_mod.Installer(cfg, pi_home=str(agent_dir))
    seen = {}

    def fake_install(**kwargs):
        seen.update(kwargs)
        return pi.InstallResult(
            ok=True, installed=[str(installed(agent_dir))], home=str(agent_dir), version="0.1.0"
        )

    monkeypatch.setattr(pi, "install", fake_install)
    installer._install_pi()

    assert seen["home"] == pi.pi_agent_dir(str(agent_dir))
    assert cfg.pi_home == str(agent_dir)
    assert cfg.pi_extension == str(installed(agent_dir))


def test_no_pi_skips_the_step(agent_dir: Path, monkeypatch) -> None:
    from csd_agent import install as installer_mod

    called = False

    def fake_install(**kwargs):
        nonlocal called
        called = True
        return pi.InstallResult(ok=True)

    monkeypatch.setattr(pi, "install", fake_install)
    inherited = config.AgentConfig(cloud_host="127.0.0.1", local=True)
    installer_mod.Installer(inherited, no_pi=True, pi_home=str(agent_dir))._install_pi()

    assert called is False
    assert inherited.pi_extension == ""


def test_a_host_without_pi_does_not_fail_the_main_installer(tmp_path: Path) -> None:
    from csd_agent import install as installer_mod

    cfg = config.AgentConfig(cloud_host="127.0.0.1", local=True)
    installer = installer_mod.Installer(cfg, pi_home=str(tmp_path / "absent"))
    installer._install_pi()

    assert installer.pi_ok is True
    assert cfg.pi_extension == ""


def test_the_agent_config_round_trips_the_pi_fields(tmp_path: Path, scratch_csd_home: Path) -> None:
    cfg = config.AgentConfig(cloud_host="127.0.0.1", local=True)
    cfg.pi_home = "/home/user/.pi/agent"
    cfg.pi_extension = "/home/user/.pi/agent/extensions/csd-reporter.js"
    cfg.save()

    loaded = config.AgentConfig.load()
    assert loaded.pi_home == cfg.pi_home
    assert loaded.pi_extension == cfg.pi_extension

    # A config written by an older release has neither field and still loads.
    path = config.config_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("pi_home")
    data.pop("pi_extension")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert config.AgentConfig.load().pi_home == ""
