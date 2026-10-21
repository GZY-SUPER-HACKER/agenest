"""Wiring standalone Pi into the dashboard.

Claude Code has a hooks file and DSH has a plugin manager; Pi has neither. It discovers
`*.js` and `*.ts` in `<agent dir>/extensions/` at startup and loads each one, so this install is
a **copy** — and the only real question is what may be overwritten. One marker string decides:
the file carries `csd-pi-reporter`, and anything else in that directory is somebody's own work.

Three decisions worth keeping:

- **The global extensions directory, never `.pi/extensions`.** Project-local extensions load only
  after Pi trusts the project, so a reporter there would go silent the first time it runs
  somewhere new — a dashboard that stops reporting for a reason the operator cannot see.
- **A copy, never a link.** The vendored package under `~/.csd/pi-csd-extension` is the artifact;
  pointing Pi at a development checkout breaks it the day the checkout moves.
- **A host with no Pi is not an error.** Claude Code and DSH are still supported targets, and a
  machine that later gains Pi can re-run `csd-agent install` (or `csd-agent pi install`).

The extension itself is inert when `CSD_AGENT=1` (a Pi child the dashboard's Agent tab owns) or
`CSD_PI_REPORTER=0`, so installing it on a machine that also runs the personal agent does not
make that agent report to its own dashboard.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config

PACKAGE = "pi-csd-extension"
#: The single file Pi loads. It is one file on purpose: Pi's loader treats every `.js` in the
#: extensions directory as an independent extension, so a reporter split across modules would
#: be loaded twice.
EXTENSION = "csd-reporter.js"
#: The marker that says a file in `extensions/` is ours. Uninstall deletes only a file carrying
#: it: the directory belongs to the user, and their own extension may be called anything.
MARKER = "csd-pi-reporter"
#: What the vendored copy keeps. `test/` is development-only.
PACKAGE_FILES = ("package.json", "csd-reporter.js", "README.md")


def pi_agent_dir(home: Path | str | None = None) -> Path:
    """`$PI_CODING_AGENT_DIR` when set, else `~/.pi/agent` — the same rule Pi uses."""
    if home:
        return Path(home).expanduser()
    return Path(
        os.environ.get("PI_CODING_AGENT_DIR") or (Path.home() / ".pi" / "agent")
    ).expanduser()


def extensions_dir(agent_dir: Path) -> Path:
    return agent_dir / "extensions"


def installed_extension(agent_dir: Path) -> Path:
    return extensions_dir(agent_dir) / EXTENSION


def source_dir(pkg_source: Path | None) -> Path | None:
    """Find the reporter package next to the `sdk/` directory being installed from.

    Accepts the `sdk/` directory or `sdk/csd-agent/`, and falls back to a previous vendored copy
    so `csd-agent pi install` works without the checkout after the first run.
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
        # sdk/csd-agent/csd_agent/pi.py -> sdk/
        candidates.append(Path(__file__).resolve().parents[2] / PACKAGE)
    candidates.append(config.agent_home() / PACKAGE)

    for candidate in candidates:
        if (candidate / "package.json").exists() and (candidate / EXTENSION).exists():
            return candidate
    return None


def package_version(package_dir: Path) -> str:
    try:
        data = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "unknown")
    except (OSError, json.JSONDecodeError):
        return "unknown"


def vendor(package_dir: Path) -> Path:
    """Copy the package into `~/.csd`, refresh-on-rerun like the rest of this installer.

    Removed first rather than merged over: a file deleted from the package must not survive a
    refresh, because Pi would keep loading a module the current release no longer ships.
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


def is_ours(path: Path) -> bool:
    """Does this file carry our marker? Unreadable is not ours: never delete blind."""
    try:
        return MARKER in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


@dataclass
class InstallResult:
    ok: bool
    installed: list[str] = field(default_factory=list)
    skipped: bool = False
    notes: list[str] = field(default_factory=list)
    home: str = ""
    version: str = ""


def install(
    *,
    pkg_source: Path | None = None,
    home: Path | str | None = None,
) -> InstallResult:
    """Vendor the package and copy the extension where Pi will discover it.

    Never raises for the ordinary cases — a host without Pi, a missing source — so the caller
    can report one line and still finish the rest of its work. A real failure is in `notes`,
    because the reason is what the operator needs.
    """
    agent_dir = pi_agent_dir(home)
    result = InstallResult(ok=True, home=str(agent_dir))

    if not agent_dir.is_dir():
        result.skipped = True
        result.notes.append(
            f"Pi not found at {agent_dir} (no agent directory); skipped. "
            "Re-run after Pi has been launched once."
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
    result.version = package_version(vendored)
    result.notes.append(f"reporter {result.version} vendored at {vendored}")

    source_file = vendored / EXTENSION
    if not source_file.exists():
        result.ok = False
        result.notes.append(f"{PACKAGE} is missing {EXTENSION}; reinstall the package")
        return result

    target_dir = extensions_dir(agent_dir)
    target = target_dir / EXTENSION
    if target.exists() and not is_ours(target):
        # Overwriting here would silently replace an extension the user wrote; leaving ours out
        # is the reversible mistake, and the note says exactly which file blocked it.
        result.ok = False
        result.notes.append(
            f"refusing to replace {target}: it does not carry the '{MARKER}' marker. "
            "Move it aside and re-run."
        )
        return result

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
        target.chmod(0o644)
    except OSError as exc:
        result.ok = False
        result.notes.append(f"could not write {target}: {exc}")
        return result

    result.installed.append(str(target))
    result.notes.append(f"extension copied to {target}")
    # Pi reads the extensions directory once at startup. Saying so is the difference between
    # "installed" and "installed but nothing reports", which reads as a broken dashboard.
    result.notes.append("restart any running Pi so the extension is loaded")
    return result


def uninstall(*, home: Path | str | None = None, label: str | None = None) -> int:
    """Remove only our extension file, and tell the dashboard the host is leaving.

    The goodbye goes first (the same rule `csd-agent uninstall` follows for Claude Code): if it
    fails, silence is rendered as "maybe offline", which is also true.
    """
    agent_dir = pi_agent_dir(home)
    target = installed_extension(agent_dir)

    if label:
        # Late import: `runtime` loads agent.json and may not exist on a Pi-only host.
        from . import runtime

        result = runtime.cpctl(
            "host", "report", "--label", label, "--agent-type", "pi", "--disconnected"
        )
        print("  pi host marked disconnected" if result.ok
              else f"  note: could not report the pi disconnect ({result.detail})")

    failures = 0
    if not target.exists():
        print(f"  no reporter at {target}; nothing to remove")
    elif not is_ours(target):
        print(f"  {target} does not carry the '{MARKER}' marker; left alone")
        failures += 1
    else:
        try:
            target.unlink()
            print(f"  removed {target}")
            print("  restart any running Pi so the extension is unloaded")
        except OSError as exc:
            print(f"  FAILED to remove {target}: {exc}")
            failures += 1

    _remove_vendored()
    return 0 if failures == 0 else 1


def _remove_vendored() -> None:
    path = config.agent_home() / PACKAGE
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    except OSError:
        pass


def status(home: Path | str | None = None) -> int:
    """Print what the dashboard side would need to know, without changing anything."""
    agent_dir = pi_agent_dir(home)
    target = installed_extension(agent_dir)
    print(f"pi agent   : {agent_dir}")
    if not agent_dir.is_dir():
        print("agent dir  : MISSING (Pi has not been run here)")
        return 1 if (config.agent_home() / PACKAGE).exists() else 0

    if not target.exists():
        print(f"extension  : not installed ({target})")
        return 1
    if not is_ours(target):
        print(f"extension  : {target} exists but is not ours (no '{MARKER}' marker)")
        return 1

    vendored = config.agent_home() / PACKAGE
    print(f"extension  : installed ({target})")
    print(f"reporter   : {package_version(vendored)} (vendored at {vendored})")
    return 0
