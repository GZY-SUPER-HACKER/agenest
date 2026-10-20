"""A `cpctl` reporting hook for nanobot.

nanobot exposes a documented lifecycle surface (`nanobot/agent/hook.py::AgentHook`) with
`before_run` / `after_run` / `on_error` / `on_finally`. This module implements it and reports
into Agenest — no upstream changes required.

The hook is attached at construction time:

```python
from nanobot.agent.loop import AgentLoop
from csd_nanobot import DashboardHook

loop = AgentLoop(config=..., hooks=[DashboardHook(agent_type="nanobot")], ...)
```

## What it reports, and why that and nothing more

`before_run` / `after_run` / `on_error` mark a session's start and end, and `on_finally` is
the net that catches every exit path. The **turn-level** hooks (`before_iteration`,
`after_execute_tool`, `on_stream`, …) are deliberately not wired: they see each internal step
of the agent's work, and the spec's central rule is 只展示实际收到的状态与事件，不虚构
Agent 内部进度 (§1.3, §5.3, §9). Reporting them would mean the dashboard claims to show agent
internals it inferred from a hook — exactly what the design says it does not do. If you want
per-tool events, that is a separate, deliberate decision, and it carries the
payload-sanitising implications with it.

`on_finally` exists because a crash between turns never reaches `after_run`. Without it a
failed run sits at `running` forever and only ever shows as "maybe offline" — which is honest
but useless, when the process exit code is right there.

## Failure policy

Every report is best-effort and never raises into the agent. `AgentHook.__init__(reraise=False)`
means `CompositeHook._for_each_hook_safe` logs a hook exception and carries on, so a dashboard
outage cannot stop nanobot. Both subprocess calls are capped at 5 s: the CLI's own timeout is
3 s, so this bound is a backstop for interpreter startup rather than the network.

Reporting runs in a subprocess rather than in-process deliberately. A hook is on the agent's
critical path; importing an HTTP client into it adds import cost and, worse, a shared event
loop that a slow write can stall. A short-lived `cpctl` process cannot stall anything.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.agent.hook import AgentHook, AgentRunHookContext

DEFAULT_BASE_URL = os.environ.get("CSD_BASE_URL", "http://127.0.0.1:8780")

#: How the hook finds `cpctl`:
#:   1. `CSD_CPCTL` — an explicit program (an installed entry point, or a path to one)
#:   2. `cpctl` on PATH
#:   3. this file's own tree: <repo>/sdk/cpctl, run as `python -m cpctl`
#:
#: The third case matters because the hook lives in `sdk/` next to the CLI. Without it, a
#: source checkout would need `pip install -e sdk/cpctl` or a hand-set PYTHONPATH before the
#: hook did anything — and a hook that silently does nothing is worse than a missing one.
_LOCAL_CPCTL = Path(__file__).resolve().parent / "cpctl"

#: A subprocess ceiling. The CLI's own timeout is 3 s, so this mainly bounds interpreter
#: startup — but a hook sits on the agent's critical path, so it gets an explicit bound.
_CALL_TIMEOUT = 5.0


def _probe(path: Path) -> list[str] | None:
    """Return the argv prefix for invoking cpctl from `path`, or None if it is not there."""
    if (path / "cpctl" / "__main__.py").is_file():
        return [sys.executable, "-m", "cpctl"]
    return None


def _resolve_command() -> tuple[list[str], dict[str, str]]:
    """Work out how to run cpctl, plus any extra env it needs.

    Returns `(argv_prefix, extra_env)`. The extra env is where a source checkout gets its
    `PYTHONPATH`, so a caller never has to know the hook found the CLI on disk.
    """
    explicit = os.environ.get("CSD_CPCTL")
    if explicit:
        return [explicit], {}

    if _LOCAL_CPCTL.is_dir() and _probe(_LOCAL_CPCTL):
        # Prepend rather than set, so an operator's own PYTHONPATH survives.
        existing = os.environ.get("PYTHONPATH", "")
        path = str(_LOCAL_CPCTL)
        merged = path if not existing else f"{path}{os.pathsep}{existing}"
        return [sys.executable, "-m", "cpctl"], {"PYTHONPATH": merged}

    return ["cpctl"], {}


class DashboardHook(AgentHook):
    """Report a nanobot run's lifecycle to Agenest."""

    def __init__(
        self,
        *,
        agent_type: str = "nanobot",
        title: str | None = None,
        project: str | None = None,
        workspace: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        # `reraise=False` is the whole safety policy in one argument.
        super().__init__(reraise=False)
        self._agent_type = agent_type
        self._title = title
        self._project = project
        self._workspace = workspace
        self._base_url = base_url
        self._session_id: str | None = None
        self._started_at: datetime | None = None
        self._closed = False

    # -------------------------------------------------------------- #
    # lifecycle
    # -------------------------------------------------------------- #
    async def before_run(self, context: AgentRunHookContext) -> None:
        args = [
            "session", "start",
            "--agent", self._agent_type,
            "--title", self._title or _guess_title(context),
            "--status", "running",
        ]
        # Only pass a flag when there is a real value. `--workspace ""` would be accepted by
        # argparse and stored as a literal empty path — a field that looks populated and says
        # nothing. (AgentRunHookContext has no `workspace` field at all, which is why this is
        # read defensively.)
        for flag, value in (
            ("--project", self._project or "nanobot"),
            ("--workspace", self._workspace),
        ):
            if value and value.strip():
                args += [flag, value]

        self._session_id = await self._cpctl(*args)
        if self._session_id:
            self._started_at = datetime.now(timezone.utc)

    async def after_run(self, context: AgentRunHookContext) -> None:
        await self._close(context, status="completed", summary=_summarise(context))

    async def on_error(self, context: AgentRunHookContext) -> None:
        error = context.error or (
            repr(context.exception) if context.exception else "agent run failed"
        )
        await self._close(context, status="failed", summary=error[:500])

    async def on_finally(self, context: AgentRunHookContext) -> None:
        """The net for every exit path `after_run` does not reach.

        Idempotent: if the session is already closed this is a no-op. (The server would
        refuse to reopen a terminal session anyway, so the guard is about not *trying*.)
        """
        if self._closed:
            return
        failed = bool(context.error) or context.stop_reason == "error"
        await self._close(
            context,
            status="failed" if failed else "completed",
            summary=_summarise(context),
        )

    # -------------------------------------------------------------- #
    # optional: an explicit progress signal, off by default
    # -------------------------------------------------------------- #
    async def note(self, message: str) -> None:
        """Report one free-text note. Call this from your own code if you want it.

        Not wired to any turn hook — see the module docstring.
        """
        if not self._session_id:
            return
        await self._cpctl_and_discard(
            "event", "--session", self._session_id, "--type", "note", "--message", message
        )

    # -------------------------------------------------------------- #
    # internals
    # -------------------------------------------------------------- #
    async def _close(
        self,
        context: AgentRunHookContext,
        *,
        status: str,
        summary: str | None,
    ) -> None:
        if self._closed or not self._session_id:
            return
        self._closed = True

        detail = (summary or "").strip()
        took = _duration(self._started_at)
        if took:
            detail = f"{detail} · {took}" if detail else took

        args = [
            "session", "finish",
            "--session", self._session_id,
            "--status", status,
        ]
        if detail:
            args += ["--summary", detail]
        await self._cpctl_and_discard(*args)

    async def _cpctl(self, *args: str) -> str | None:
        """Run cpctl and return the first line of stdout (a session id), or None on failure.

        Off the event loop: a hook is async, and blocking it for the length of a subprocess
        would stall the agent's own turn.
        """
        env = _env(self._base_url)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                _command(*args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_CALL_TIMEOUT,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            # Run unobserved. `cpctl` has already printed its own reason to stderr;
            # repeating it here would double every failure line in the log.
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip().splitlines()[0]

    async def _cpctl_and_discard(self, *args: str) -> None:
        try:
            await asyncio.to_thread(
                subprocess.run,
                _command(*args),
                capture_output=True,
                timeout=_CALL_TIMEOUT,
                env=_env(self._base_url),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            # The session stays `running` server-side, which the dashboard will label
            # "maybe offline" once the stale threshold passes. That is the honest outcome:
            # we reported the start and never got to report the end.
            pass


def _command(*args: str) -> list[str]:
    """Full argv for one cpctl call, with the resolved prefix already applied."""
    return [*_PREFIX, *args]


def _env(base_url: str) -> dict[str, str]:
    return {**os.environ, **_EXTRA_ENV, "CSD_BASE_URL": base_url}


_PREFIX, _EXTRA_ENV = _resolve_command()


def _guess_title(context: AgentRunHookContext) -> str:
    """The first user message, trimmed. No parsing, no summarising — just the text."""
    for message in context.messages or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip().replace("\n", " ")[:120]
    return "nanobot run"


def _summarise(context: AgentRunHookContext) -> str:
    tools = context.tools_used or []
    if tools:
        unique = list(dict.fromkeys(tools))
        shown = ", ".join(unique[:6])
        more = "…" if len(unique) > 6 else ""
        return f"tools: {shown}{more}"
    return "run finished"


def _duration(started: datetime | None) -> str:
    if started is None:
        return ""
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60}s"
