"""Install the agent-side way to report a task verdict.

The protocol is an event (`task_completed` / `task_failed`), and the agent is the party that
knows when a task ended. Two small pieces make that usable from inside an agent:

- a **launcher** at `~/.csd/bin/csd-cpctl`, because the agent's shell has neither
  `PYTHONPATH` nor the ingest token that a hook gets from this install;
- a **skill** under `~/.claude/skills/csd-task-verdict/`, which tells the model when a verdict
  is wanted and how to send it.

Claude Code discovers the skill automatically. The same launcher works for Pi/DSH/nanobot;
those harnesses have their own instruction mechanisms, so the skill is not forced on them.

Nothing here is required for a wrapper-driven run (`run-agent.sh --verdict` runs the
verification itself) or for the connectors' automatic lifecycle reporting. It is the path for
"the agent finished a task and knows it".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import config

SKILL_NAME = "csd-task-verdict"

#: Recognises our files on a re-run and on uninstall. Never delete a file without it.
LAUNCHER_MARKER = "csd-agent skill install"
SKILL_MARKER = "csd-agent task-verdict skill"

LAUNCHER = """#!/usr/bin/env bash
# csd-cpctl — the reporting CLI with the connector's environment. Written by `csd-agent skill install`.
#
# The hook wrapper gets PYTHONPATH and the token because it is spawned by this install. The
# agent's own shell does not, so this does the same setup for a command the agent runs itself.
set -uo pipefail
export CSD_AGENT_HOME="${CSD_AGENT_HOME:-__HOME__}"
export CSD_STATE_DIR="${CSD_STATE_DIR:-$CSD_AGENT_HOME/state}"
export PYTHONPATH="$CSD_AGENT_HOME/cpctl${PYTHONPATH:+:$PYTHONPATH}"
if [ -f "$CSD_AGENT_HOME/env" ]; then
  set -a
  . "$CSD_AGENT_HOME/env"
  set +a
fi
exec __PYTHON__ -m cpctl "$@"
"""

SKILL = """---
name: csd-task-verdict
description: Report whether a substantial coding task succeeded or failed to the Agenest dashboard. Use when you finish a task the user asked you to do, not for a casual question or a partial step.
---

<!-- __MARKER__ -->

# Report the task verdict

When you finish a substantial task — code changed, a fix applied, work done — report the
verdict **once**, at the end:

```bash
bash "__LAUNCHER__" task done -m "one line: what you verified"
```

If the work did not succeed:

```bash
bash "__LAUNCHER__" task failed -m "one line: what is wrong, or what is left"
```

Rules:

- **Only with evidence.** Report `done` when you have checked the result — tests, a build, a
  command's output. Do not call it because you ran out of things to say.
- **Keep the message short and plain.** One sentence, roughly under 120 characters, with no
  parentheses, backticks or semicolons: it travels through the agent's shell, and a long quoted
  message is exactly what gets truncated there.
- **Once per task.** Do not report after every reply. A session can span many turns; only the
  answer that ends a piece of work gets a verdict.
- **Never for a question or a partial step.** Reading a file or explaining code is not a task.
  If you are still working, say so instead.
- **Reporting is not the work.** If the command fails (dashboard unreachable), continue. The
  same rule applies to the hooks: reporting must never block or fail what you were asked to do.
"""


def _launcher_path() -> Path:
    return config.bin_dir() / "csd-cpctl"


def _skill_dir() -> Path:
    # Claude Code's personal skills directory. `CLAUDE_SKILLS_DIR` is an escape hatch for a
    # non-default install (and what the tests use).
    base = os.environ.get("CLAUDE_SKILLS_DIR")
    return (Path(base) if base else Path.home() / ".claude" / "skills") / SKILL_NAME


def install() -> list[str]:
    """Write both files. Returns one line per thing done, for the caller to print."""
    notes: list[str] = []

    launcher = _launcher_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        LAUNCHER.replace("__HOME__", config.agent_home().as_posix()).replace(
            "__PYTHON__", Path(sys.executable).as_posix()
        ),
        encoding="utf-8",
        # LF, not the platform default: a CRLF bash script still runs when invoked through
        # `bash`, but a stray `\r` turns into part of an argument in the worst place.
        newline="\n",
    )
    try:
        launcher.chmod(0o755)
    except OSError:
        # Windows: the file is invoked through bash, which does not need the bit.
        pass
    notes.append(f"launcher: {launcher}")

    directory = _skill_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        SKILL.replace("__LAUNCHER__", launcher.as_posix()).replace(
            "__MARKER__", SKILL_MARKER
        ),
        encoding="utf-8",
        newline="\n",
    )
    notes.append(f"skill:    {path}")
    return notes


def status() -> int:
    launcher = _launcher_path()
    skill = _skill_dir() / "SKILL.md"
    for label, path, marker in (
        ("launcher", launcher, LAUNCHER_MARKER),
        ("skill", skill, SKILL_MARKER),
    ):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            print(f"  {label:<8} missing  {path}")
            continue
        state = "ok" if marker in text else "not ours"
        print(f"  {label:<8} {state:<8} {path}")
    return 0


def uninstall() -> int:
    """Remove only files carrying our marker; leave a same-named file we did not write."""
    for path, marker in (
        (_launcher_path(), LAUNCHER_MARKER),
        (_skill_dir() / "SKILL.md", SKILL_MARKER),
    ):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if marker not in text:
            print(f"  left {path} (not written by csd-agent)")
            continue
        try:
            path.unlink()
            print(f"  removed {path}")
        except OSError as exc:
            print(f"  could not remove {path}: {exc}")
    return 0
