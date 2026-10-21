"""`csd-agent install` wiring DeepSeek Harness the same way it wires Claude Code.

The code under test is stdlib-only and lives in `sdk/csd-agent`; this file lives in the backend
suite because that is the only Python runner in the repository (see CLAUDE.md). No DSH, Node,
or pnpm is needed: the fake launcher below implements exactly the two manifest edits
`dsh plugin add|remove` performs, so the test pins our command line and our verification rather
than pnpm's behaviour.

The rule under every test: **install adds one dependency and one bundle entry, and uninstall
removes exactly those**. The profile files belong to the user and may hold other plugins.
"""

from __future__ import annotations

import json
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

SDK = Path(__file__).resolve().parents[2] / "sdk" / "csd-agent"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from csd_agent import config, dsh  # noqa: E402  (the path insert has to come first)

PACKAGE = "dsh-csd-plugin"
OTHER = "someone-elses-plugin"


@pytest.fixture(autouse=True)
def scratch_csd_home(tmp_path: Path, monkeypatch) -> Path:
    """Every vendored copy and tarball goes under the test's own directory."""
    home = tmp_path / "csd"
    monkeypatch.setattr(config, "agent_home", lambda: home)
    return home


@pytest.fixture()
def dsh_home(tmp_path: Path) -> Path:
    home = tmp_path / "dsh"
    for name in ("web", "headless"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "package.json").write_text(
            json.dumps({
                "name": f"dsh-profile-{name}",
                "private": True,
                "dependencies": {OTHER: "file:./someone.tgz"},
                "dsh": {"profile": {
                    "bundles": ["@deepseek-ai/dsh-base", OTHER],
                    "patchReload": "startup",
                }},
            }, indent=2) + "\n",
            encoding="utf-8",
        )
    return home


@pytest.fixture()
def fake_cli(tmp_path: Path) -> list[str]:
    """A stand-in for the `dsh` launcher, implementing `plugin ... add|remove`."""
    script = tmp_path / "fake_dsh.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    return [sys.executable, str(script)]


FAKE_CLI = r'''
import json, os, shutil, sys, tarfile
from pathlib import Path

args = sys.argv[1:]
assert args[:1] == ["plugin"], args
assert args[1] == "--profile", args
name = args[2]
action, target = args[3], args[4]
profile = Path(os.environ["DSH_HOME"]) / "profiles" / name
manifest_path = profile / "package.json"
data = json.loads(manifest_path.read_text(encoding="utf-8"))

if action == "add":
    staging = profile / ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    with tarfile.open(target) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = member.name.split("/", 1)[1]
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(tar.extractfile(member).read())
    package = json.loads((staging / "package.json").read_text(encoding="utf-8"))
    pkg_name = package["name"]
    dest = profile / "node_modules" / pkg_name
    shutil.rmtree(dest, ignore_errors=True)
    shutil.move(str(staging), str(dest))
    data.setdefault("dependencies", {})[pkg_name] = f"file:{target}"
    bundles = data.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
    if pkg_name not in bundles:
        bundles.append(pkg_name)
elif action == "remove":
    data.get("dependencies", {}).pop(target, None)
    bundles = data.get("dsh", {}).get("profile", {}).get("bundles", [])
    data["dsh"]["profile"]["bundles"] = [b for b in bundles if b != target]
    shutil.rmtree(profile / "node_modules" / target, ignore_errors=True)
else:
    raise SystemExit(f"unsupported action {action}")

manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
'''


def manifest(home: Path, name: str) -> dict:
    return json.loads((home / "profiles" / name / "package.json").read_text(encoding="utf-8"))


def test_install_wires_every_existing_profile_as_a_copy(dsh_home, fake_cli) -> None:
    result = dsh.install(home=dsh_home, cli=fake_cli)

    assert result.ok, result.notes
    assert result.installed == ["web", "headless"]
    for name in result.installed:
        data = manifest(dsh_home, name)
        dependency = data["dependencies"][PACKAGE]
        # A tarball, not `link:<some checkout>`: moving the repository must not break DSH.
        assert dependency.startswith("file:") and dependency.endswith(".tgz")
        assert PACKAGE in data["dsh"]["profile"]["bundles"]
        installed = dsh_home / "profiles" / name / "node_modules" / PACKAGE
        assert (installed / "package.json").exists()
        assert not installed.is_symlink()
        # The profile's own plugins are untouched.
        assert data["dependencies"][OTHER] == "file:./someone.tgz"
        assert OTHER in data["dsh"]["profile"]["bundles"]


def test_the_reporter_version_comes_from_the_package_manifest(dsh_home, fake_cli) -> None:
    result = dsh.install(home=dsh_home, cli=fake_cli)
    assert result.version == dsh.plugin_version(dsh.source_dir(None))


def test_install_is_idempotent(dsh_home, fake_cli) -> None:
    dsh.install(home=dsh_home, cli=fake_cli)
    dsh.install(home=dsh_home, cli=fake_cli)

    for name in ("web", "headless"):
        data = manifest(dsh_home, name)
        assert data["dsh"]["profile"]["bundles"].count(PACKAGE) == 1
        assert list(data["dependencies"]).count(PACKAGE) == 1


def test_install_can_target_one_profile(dsh_home, fake_cli) -> None:
    result = dsh.install(home=dsh_home, cli=fake_cli, profiles_requested=["web"])

    assert result.installed == ["web"]
    assert PACKAGE in manifest(dsh_home, "web")["dsh"]["profile"]["bundles"]
    assert PACKAGE not in manifest(dsh_home, "headless")["dsh"]["profile"]["bundles"]


def test_a_host_without_dsh_is_skipped_not_failed(tmp_path, fake_cli) -> None:
    result = dsh.install(home=tmp_path / "no-dsh-here", cli=fake_cli)

    assert result.ok
    assert result.skipped
    assert "not found" in result.notes[0]


def test_a_missing_reporter_source_is_a_named_failure(tmp_path, fake_cli) -> None:
    home = tmp_path / "dsh"
    (home / "profiles" / "web").mkdir(parents=True)
    (home / "profiles" / "web" / "package.json").write_text("{}\n", encoding="utf-8")

    result = dsh.install(home=home, cli=fake_cli, pkg_source=tmp_path / "empty")

    assert not result.ok
    assert any("source not found" in note for note in result.notes)
    assert not (home / "profiles" / "web" / "node_modules" / PACKAGE).exists()


def test_pack_ships_the_manifest_patch_and_code(scratch_csd_home: Path) -> None:
    source = dsh.source_dir(None)
    assert source is not None
    vendored = dsh.vendor(source)
    assert not (vendored / "test").exists(), "tests are development-only"

    tarball = dsh.pack(vendored)

    with tarfile.open(tarball) as tar:
        names = set(tar.getnames())
    assert "package/package.json" in names
    assert "package/cordis.patch.yml" in names
    assert "package/lib/index.js" in names
    assert "package/README.md" in names


def test_uninstall_removes_only_our_entry_and_the_vendored_copy(
    dsh_home, fake_cli, scratch_csd_home: Path
) -> None:
    dsh.install(home=dsh_home, cli=fake_cli)
    assert dsh.profile_has_plugin(dsh_home, "web")

    code = dsh.uninstall(home=dsh_home, cli=fake_cli)

    assert code == 0
    for name in ("web", "headless"):
        data = manifest(dsh_home, name)
        assert PACKAGE not in data["dependencies"]
        assert PACKAGE not in data["dsh"]["profile"]["bundles"]
        assert not (dsh_home / "profiles" / name / "node_modules" / PACKAGE).exists()
        assert data["dependencies"][OTHER] == "file:./someone.tgz"
        assert OTHER in data["dsh"]["profile"]["bundles"]
    assert not (scratch_csd_home / PACKAGE).exists()
    assert not (scratch_csd_home / "dsh").exists()


def test_uninstall_without_a_launcher_edits_the_manifest_directly(
    dsh_home, fake_cli, monkeypatch
) -> None:
    dsh.install(home=dsh_home, cli=fake_cli)
    monkeypatch.setattr(dsh, "cli_for", lambda home=None: None)

    code = dsh.uninstall(home=dsh_home)

    assert code == 0
    assert not dsh.profile_has_plugin(dsh_home, "web")
    assert OTHER in manifest(dsh_home, "web")["dsh"]["profile"]["bundles"]


def test_uninstall_leaves_a_profile_that_never_had_the_reporter(dsh_home, fake_cli) -> None:
    before = (dsh_home / "profiles" / "web" / "package.json").read_text(encoding="utf-8")
    code = dsh.uninstall(home=dsh_home, cli=fake_cli)
    assert code == 0
    assert (dsh_home / "profiles" / "web" / "package.json").read_text(encoding="utf-8") == before


def test_the_main_installer_invokes_the_dsh_step(dsh_home, monkeypatch) -> None:
    """`csd-agent install` is the one command that wires both agents; this pins the join."""
    from csd_agent import install as installer_mod

    cfg = config.AgentConfig(cloud_host="127.0.0.1", local=True)
    installer = installer_mod.Installer(cfg, dsh_home=str(dsh_home))
    seen = {}

    def fake_install(**kwargs):
        seen.update(kwargs)
        return dsh.InstallResult(
            ok=True, installed=["web"], home=str(dsh_home), version="0.1.0"
        )

    monkeypatch.setattr(dsh, "install", fake_install)
    installer._install_dsh()

    assert seen["home"] == dsh_home
    assert cfg.dsh_home == str(dsh_home)
    assert cfg.dsh_profiles == ["web"]


def test_no_dsh_skips_the_step(dsh_home, monkeypatch) -> None:
    from csd_agent import install as installer_mod

    called = False

    def fake_install(**kwargs):
        nonlocal called
        called = True
        return dsh.InstallResult(ok=True)

    monkeypatch.setattr(dsh, "install", fake_install)
    inherited = config.AgentConfig(cloud_host="127.0.0.1", local=True)
    installer_mod.Installer(inherited, no_dsh=True, dsh_home=str(dsh_home))._install_dsh()
    assert called is False
    assert inherited.dsh_profiles == []


def test_a_host_without_dsh_does_not_fail_the_main_installer(tmp_path) -> None:
    from csd_agent import install as installer_mod

    cfg = config.AgentConfig(cloud_host="127.0.0.1", local=True)
    installer = installer_mod.Installer(cfg, dsh_home=str(tmp_path / "absent"))
    installer._install_dsh()
    assert installer.dsh_ok is True
    assert cfg.dsh_profiles == []
