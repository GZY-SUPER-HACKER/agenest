"""Wiring DeepSeek Harness into the dashboard.

Claude Code has a hook configuration file; DSH has a plugin tree. The dashboard does not care
which, but the *operator* should: one command on the agent host is the promise `csd-agent`
makes, and `dsh plugin --profile web add ../some/checkout` is not that command. This module is
the difference, so `csd-agent install` wires DSH the same way it wires Claude Code's hooks.

Three decisions are worth keeping:

- **The package is vendored first, then installed from a tarball.** `pnpm add <directory>`
  records a `link:` to wherever that directory happens to be — in a development checkout, that
  is the checkout, and moving or deleting it silently breaks every future DSH launch. The
  vendored copy under `~/.csd/dsh-csd-plugin` is the artifact this package already deploys
  (cpctl and csd-agent are copied the same way), and a tarball is what pnpm installs as a real
  copy rather than a link.
- **DSH's own plugin manager does the install.** It is the only writer that keeps
  `package.json`'s dependency and the `dsh.profile.bundles` list consistent, so a later
  `dsh plugin update` reconciles the set instead of dropping the reporter.
- **Only our package is touched.** Install adds one dependency and one bundle entry; uninstall
  removes exactly those and nothing else. The profiles may hold the user's own plugins.

A machine with no `~/.dsh` is not an error: Claude Code is still a supported install target,
and a host that later gains DSH can re-run `csd-agent install` (or `csd-agent dsh install`).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config

PACKAGE = "dsh-csd-plugin"
#: The profiles `dsh` initializes from templates. Ordering is only for readable output; a new
#: profile is installed into as well when it exists, because the reporter should be there
#: whichever surface the operator launches.
KNOWN_PROFILES = ("web", "headless", "sdk", "sdk-minimal", "acp")
#: Files a DSH profile needs. `tests/` and `node_modules/` are development artifacts.
PACKAGE_FILES = ("package.json", "cordis.patch.yml", "README.md", "lib")


def dsh_home() -> Path:
    """`$DSH_HOME` when set, else `~/.dsh` — the same rule the DSH CLI uses."""
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh")).expanduser()


def profiles(home: Path | None = None) -> list[str]:
    """Profiles that exist (a directory with a `package.json`), in a stable order."""
    root = (home or dsh_home()) / "profiles"
    try:
        found = {
            entry.name
            for entry in root.iterdir()
            if entry.is_dir() and (entry / "package.json").exists()
        }
    except OSError:
        return []
    ordered = [name for name in KNOWN_PROFILES if name in found]
    return ordered + sorted(found - set(ordered))


def cli_for(home: Path | None = None) -> list[str] | None:
    """How to invoke the DSH launcher on this host, or `None` when it is not installed.

    `dsh` is often not on PATH (a source checkout, or an installation kept under `~/.dsh`), so
    the packaged entry point is the fallback — the same file `dsh`'s own bin points at.
    """
    found = shutil.which("dsh")
    if found:
        return [found]

    node = shutil.which("node")
    if not node:
        return None
    candidates = [
        (home or dsh_home()) / "profiles/node_modules/@deepseek-ai/dsh/lib/bin.js",
        Path.home() / ".dsh/profiles/node_modules/@deepseek-ai/dsh/lib/bin.js",
    ]
    for candidate in candidates:
        if candidate.exists():
            return [node, str(candidate)]
    return None


def source_dir(pkg_source: Path | None) -> Path | None:
    """Find the reporter package next to the `sdk/` directory being installed from.

    Accepts the `sdk/` directory or `sdk/csd-agent/`, and falls back to a previous vendored
    copy so `csd-agent dsh install` works without the checkout after the first run.
    """
    candidates: list[Path] = []
    if pkg_source:
        base = pkg_source.expanduser().resolve()
        candidates += [
            base / PACKAGE,
            base.parent / PACKAGE,
            base.parent / "sdk" / PACKAGE,
        ]
    else:
        # sdk/csd-agent/csd_agent/dsh.py -> sdk/
        candidates.append(Path(__file__).resolve().parents[2] / PACKAGE)
    candidates.append(config.agent_home() / PACKAGE)

    for candidate in candidates:
        if (candidate / "package.json").exists() and (candidate / "lib" / "index.js").exists():
            return candidate
    return None


def plugin_version(package_dir: Path) -> str:
    try:
        data = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "unknown")
    except (OSError, json.JSONDecodeError):
        return "unknown"


def vendor(package_dir: Path) -> Path:
    """Copy the package into `~/.csd`, refresh-on-rerun like the rest of this installer.

    Removed first rather than merged over: a file deleted from the package must not survive a
    refresh, because the loader resolves by name and would keep importing a module the current
    release no longer ships.
    """
    dest = config.agent_home() / PACKAGE
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("test", "node_modules", "__pycache__", "*.pyc", ".git")
    shutil.copytree(package_dir, dest, ignore=ignore)
    return dest


def pack(package_dir: Path) -> Path:
    """Build the npm-style tarball pnpm installs as a real copy.

    `npm pack` would do this, but npm is not a dependency of `csd-agent` and the archive is
    simple: a gzipped tar with every file under `package/`.
    """
    version = plugin_version(package_dir)
    out_dir = config.agent_home() / "dsh"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{PACKAGE}-{version}.tgz"

    files: list[Path] = []
    for name in PACKAGE_FILES:
        path = package_dir / name
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files += sorted(p for p in path.rglob("*") if p.is_file())

    with tarfile.open(out, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for path in files:
            relative = path.relative_to(package_dir).as_posix()
            data = path.read_bytes()
            info = tarfile.TarInfo(f"package/{relative}")
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(path.stat().st_mtime)
            tar.addfile(info, io.BytesIO(data))

    # One version is one artifact. A leftover tarball from an older version is dead weight,
    # and leaving it invites an operator to install the wrong one by hand.
    for stale in out_dir.glob(f"{PACKAGE}-*.tgz"):
        if stale != out:
            try:
                stale.unlink()
            except OSError:
                pass
    return out


def profile_has_plugin(home: Path, name: str) -> bool:
    profile = home / "profiles" / name
    if not (profile / "package.json").exists():
        return False
    if not (profile / "node_modules" / PACKAGE / "package.json").exists():
        return False
    try:
        data = json.loads((profile / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    bundles = ((data.get("dsh") or {}).get("profile") or {}).get("bundles") or []
    deps = data.get("dependencies") or {}
    return PACKAGE in bundles and PACKAGE in deps


@dataclass
class InstallResult:
    ok: bool
    installed: list[str] = field(default_factory=list)
    skipped: bool = False
    notes: list[str] = field(default_factory=list)
    home: str = ""
    version: str = ""


def _run_cli(cli: list[str], home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DSH_HOME"] = str(home)
    return subprocess.run([*cli, *args], capture_output=True, text=True, env=env)


def _detail(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or proc.stdout or "").strip().splitlines()
    return text[-1][:300] if text else f"exit {proc.returncode}"


def install(
    *,
    profiles_requested: list[str] | None = None,
    pkg_source: Path | None = None,
    home: Path | None = None,
    cli: list[str] | None = None,
) -> InstallResult:
    """Vendor, pack, and add the reporter to every requested DSH profile.

    Never raises for the ordinary cases — a host without DSH, a profile without pnpm — so the
    caller can report one line and still finish the rest of its work. A real failure is in
    `notes`, because the reason is what the operator needs.
    """
    home = (home or dsh_home()).expanduser()
    result = InstallResult(ok=True, home=str(home))

    available = profiles(home)
    if not available:
        result.skipped = True
        result.notes.append(
            f"DeepSeek Harness not found at {home} (no profiles); skipped. "
            "Re-run after DSH has been launched once."
        )
        return result

    wanted = list(profiles_requested or available)
    missing = [name for name in wanted if name not in available]
    if missing:
        result.ok = False
        result.notes.append(
            f"profile(s) not found: {', '.join(missing)}; available: {', '.join(available)}"
        )
        wanted = [name for name in wanted if name in available]
    if not wanted:
        return result

    cli = cli or cli_for(home)
    if not cli:
        result.ok = False
        result.notes.append(
            "DSH launcher not found (no `dsh` on PATH and no packaged bin under "
            f"{home}/profiles/node_modules)."
        )
        return result

    source = source_dir(pkg_source)
    if source is None:
        result.ok = False
        result.notes.append(
            f"{PACKAGE} source not found. Put it beside csd-agent (sdk/{PACKAGE}) or pass "
            "--source pointing at the sdk/ directory."
        )
        return result

    vendored = vendor(source)
    tarball = pack(vendored)
    result.version = plugin_version(vendored)
    result.notes.append(f"reporter {result.version} vendored at {vendored}")

    for name in wanted:
        proc = _run_cli(cli, home, "plugin", "--profile", name, "add", str(tarball))
        if proc.returncode != 0:
            result.ok = False
            result.notes.append(f"{name}: dsh plugin add failed — {_detail(proc)}")
            if "pnpm" in (proc.stderr or "").lower():
                result.notes.append(
                    "  DSH manages plugins through pnpm. Install it (`npm i -g pnpm`) and re-run."
                )
            continue
        if not profile_has_plugin(home, name):
            result.ok = False
            result.notes.append(f"{name}: command succeeded but the reporter is not mounted")
            continue
        result.installed.append(name)
        result.notes.append(f"{name}: reporter {result.version} installed")

    if result.installed:
        # Bundle membership is read when a profile boots. A DSH that is already running keeps
        # the old tree, and "installed but nothing reports" is the failure this line prevents.
        result.notes.append("restart any running DSH so the new bundle is loaded")

    return result


def uninstall(
    *,
    profiles_requested: list[str] | None = None,
    home: Path | None = None,
    cli: list[str] | None = None,
    label: str | None = None,
) -> int:
    """Remove only our dependency and bundle entry from the requested profiles.

    The DSH host row is told goodbye first (the same rule `csd-agent uninstall` follows for
    Claude Code): if the command fails, silence is rendered as "maybe offline", which is also
    true. The vendored package and tarball are removed after the profiles no longer reference
    them, so a failed remove does not leave a profile pointing at nothing.
    """
    home = (home or dsh_home()).expanduser()
    available = profiles(home)
    if not available:
        print(f"  DeepSeek Harness not found at {home}; nothing to remove")
        return 0

    wanted = list(profiles_requested or [p for p in available if profile_has_plugin(home, p)])
    if not wanted:
        print("  no profile has the reporter; nothing to remove")
        _remove_vendored()
        return 0

    if label:
        # Late import: `runtime` loads agent.json and may not exist on a DSH-only host.
        from . import runtime

        result = runtime.cpctl(
            "host", "report", "--label", label, "--agent-type", "dsh", "--disconnected"
        )
        print("  dsh host marked disconnected" if result.ok
              else f"  note: could not report the dsh disconnect ({result.detail})")

    cli = cli or cli_for(home)
    failures = 0
    for name in wanted:
        profile = home / "profiles" / name
        if not (profile / "package.json").exists():
            print(f"  {name}: profile is gone; skipping")
            continue
        if cli:
            proc = _run_cli(cli, home, "plugin", "--profile", name, "remove", PACKAGE)
            if proc.returncode != 0:
                print(f"  {name}: dsh plugin remove failed — {_detail(proc)}")
        if profile_has_plugin(home, name):
            # A missing CLI, or a pnpm failure, must not leave the reporter loaded. The
            # manifest is the loader's input; editing it is a last resort, not the path.
            if _remove_direct(profile):
                print(f"  {name}: removed directly from package.json")
            else:
                print(f"  {name}: FAILED to remove; check {profile / 'package.json'}")
                failures += 1
        else:
            # pnpm can leave the package directory after its manifest entry is gone.
            _remove_package_dir(profile / "node_modules" / PACKAGE)
            print(f"  {name}: reporter removed")

    _remove_vendored()
    return 0 if failures == 0 else 1


def _remove_direct(profile: Path) -> bool:
    """Fallback for a host with no usable `dsh` CLI: edit the manifest and drop the copy."""
    path = profile / "package.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    deps = data.get("dependencies") or {}
    deps.pop(PACKAGE, None)
    if deps:
        data["dependencies"] = deps
    else:
        data.pop("dependencies", None)
    profile_meta = (data.get("dsh") or {}).get("profile") or {}
    bundles = profile_meta.get("bundles")
    if isinstance(bundles, list):
        profile_meta["bundles"] = [name for name in bundles if name != PACKAGE]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    target = profile / "node_modules" / PACKAGE
    _remove_package_dir(target)
    return True


def _remove_package_dir(target: Path) -> None:
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
    except OSError:
        pass


def _remove_vendored() -> None:
    for path in (config.agent_home() / PACKAGE, config.agent_home() / "dsh"):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


def status(home: Path | None = None) -> int:
    """Print what the dashboard side would need to know, without changing anything."""
    home = (home or dsh_home()).expanduser()
    found = profiles(home)
    print(f"dsh home   : {home}")
    if not found:
        print("profiles   : none (DeepSeek Harness has not been launched here)")
        return 1 if (config.agent_home() / PACKAGE).exists() else 0
    print(f"profiles   : {', '.join(found)}")
    cli = cli_for(home)
    print(f"launcher   : {' '.join(cli) if cli else 'NOT FOUND'}")
    version = plugin_version(config.agent_home() / PACKAGE)
    print(f"reporter   : {version} (vendored at {config.agent_home() / PACKAGE})")
    ok = True
    for name in found:
        wired = profile_has_plugin(home, name)
        print(f"  {name:<12}: {'wired' if wired else 'not wired'}")
        ok = ok and wired
    return 0 if ok else 1
