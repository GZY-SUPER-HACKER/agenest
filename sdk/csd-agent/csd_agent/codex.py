"""Wiring Codex's hooks into the dashboard.

Codex loads hooks from a `hooks.json` beside its config (`$CODEX_HOME/hooks.json`) and from
inline tables in `config.toml`. This installer uses the JSON file: it is a structured document
we can merge without rewriting the user's TOML, and Codex loads it without a config edit.

Two consequences worth stating:

- **The file belongs to the user.** Only matcher groups whose command carries our marker are
  removed on uninstall; every other group and every other key is preserved verbatim.
- **Codex does not trust a new hook automatically.** The entries appear as untrusted until the
  operator approves them in Codex, or a headless run passes `--dangerously-bypass-hook-trust`.
  `status` reports the file, not a trust decision this package cannot make for Codex.

The command is the same shape as Claude Code's: a short-lived wrapper around
`python -m csd_agent hook-codex`, with the event read from stdin.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config

#: Codex event names are PascalCase. These are the ones the reporter acts on.
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "Interrupt",
    "PermissionRequest",
    "SessionEnd",
)

#: The marker that identifies our entries in a file we share with the user's own hooks.
COMMAND_MARKER = "csd-hook-codex"

#: Codex's hook timeout, in seconds. The wrapper's cpctl budget is smaller; this is the
#: backstop for a wedged interpreter.
HOOK_TIMEOUT = 20


@dataclass
class InstallResult:
    ok: bool
    skipped: bool = False
    path: Path | None = None
    events: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def codex_home(home: Path | str | None = None) -> Path:
    """`$CODEX_HOME` when set, else `~/.codex` — the same rule Codex uses."""
    if home:
        return Path(home).expanduser()
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def hooks_path(home: Path) -> Path:
    return home / "hooks.json"


def _command() -> str:
    """The command Codex runs for one event.

    Windows has no shebang and Codex's hook shell is not necessarily bash, so the wrapper is
    invoked explicitly. Forward slashes because the program receiving the path is bash.
    """
    wrapper = config.bin_dir() / "csd-hook-codex"
    if os.name == "nt":
        return f'bash "{wrapper.as_posix()}"'
    return str(wrapper)


def _group() -> dict:
    command = _command()
    return {
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": command,
            # Codex's Windows override; harmless on other platforms.
            "commandWindows": command,
            "timeout": HOOK_TIMEOUT,
        }],
    }


def _is_ours(group: object) -> bool:
    """Does this matcher group carry our command? Unreadable is not ours: never delete blind."""
    if not isinstance(group, dict):
        return False
    entries = group.get("hooks")
    if not isinstance(entries, list):
        return False
    marker = COMMAND_MARKER
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("command", "commandWindows"):
            value = str(entry.get(key, "")).replace("\\", "/")
            if marker in value:
                return True
    return False


def _load(path: Path) -> tuple[dict, str | None]:
    """Read the hooks file. A malformed file is reported, never overwritten."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, str(exc)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"{path} is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return {}, f"{path} must contain a JSON object"
    hooks = data.get("hooks", {})
    if hooks is not None and not isinstance(hooks, dict):
        return {}, f'{path}: "hooks" must be an object'
    return data, None


def _save(path: Path, data: dict) -> None:
    """Write atomically. Two installs (or an install racing the user's editor) must not leave
    a half-written file, and the unique temporary name is what makes `os.replace` safe when
    two writers race — the same lesson as `tracked._save` (design.md §35)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=".hooks.", suffix=".json.tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def install(home: Path | str | None = None) -> InstallResult:
    root = codex_home(home)
    path = hooks_path(root)
    data, error = _load(path)
    if error:
        return InstallResult(False, path=path, notes=[error])

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks

    added: list[str] = []
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
        # Replace our own entries, keep every foreign one. A re-install must refresh the
        # command (a moved home, a different wrapper) without duplicating it.
        kept = [group for group in groups if not _is_ours(group)]
        kept.append(_group())
        hooks[event] = kept
        added.append(event)

    try:
        _save(path, data)
    except OSError as exc:
        return InstallResult(False, path=path, notes=[str(exc)])

    return InstallResult(
        True,
        path=path,
        events=added,
        notes=[
            f"Codex hooks written to {path}",
            "Codex marks new hooks untrusted until you approve them; a headless run can pass "
            "--dangerously-bypass-hook-trust",
        ],
    )


def installed_events(home: Path | str | None = None) -> list[str]:
    """Which of our hook events are present in the file. An unreadable file is "none"."""
    data, error = _load(hooks_path(codex_home(home)))
    if error:
        return []
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    return [
        event for event in HOOK_EVENTS
        if any(_is_ours(group) for group in hooks.get(event, []))
    ]


def status(home: Path | str | None = None) -> int:
    root = codex_home(home)
    path = hooks_path(root)
    data, error = _load(path)
    if error:
        print(f"codex hooks: {error}")
        return 1
    present = installed_events(root)
    if not present:
        print(f"codex hooks: {path} — not installed")
        return 0
    print(f"codex hooks: {path} — installed: {', '.join(present)}")
    print("            (Codex trusts them only after you approve them)")
    return 0


def uninstall(home: Path | str | None = None) -> int:
    root = codex_home(home)
    path = hooks_path(root)
    if not path.exists():
        print(f"codex hooks: {path} — nothing to remove")
        return 0
    data, error = _load(path)
    if error:
        print(f"codex hooks: {error}")
        return 1
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        print(f"codex hooks: {path} — nothing to remove")
        return 0

    removed: list[str] = []
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = [group for group in groups if not _is_ours(group)]
        if len(kept) != len(groups):
            removed.append(event)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]

    try:
        _save(path, data)
    except OSError as exc:
        print(f"codex hooks: {exc}")
        return 1
    print(f"codex hooks: removed {', '.join(removed) if removed else 'nothing'} from {path}")
    return 0
