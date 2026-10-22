"""Reporting this host, so the dashboard can show that it connected.

The gap this closes: `csd-agent` could be installed, tunnelled, and working perfectly while
the dashboard showed nothing at all. Every group in the Sessions list was derived from
sessions, and a host with nothing running has none — so the only way to answer "did it
connect?" was to SSH in and run `csd-agent status`, which is the errand this whole project
exists to remove. The first thing a fresh install now does is say hello.

Three rules, all inherited rather than invented here:

- **Reported, never inferred.** This module says "I am here" and nothing else. It never claims
  a coding agent is busy, and it never reports a *disconnection* it did not observe — a host
  that is switched off mid-heartbeat simply stops talking, and the dashboard renders that as
  "maybe offline", which is the honest reading of a question nobody can ask any more.
- **A failed report is never an error** (design.md §6). Everything returns a `Result`; the
  install does not fail and no hook is delayed because the dashboard is unreachable.
- **One connector speaks for one coding agent.** `AGENT_TYPE` names the agent this package
  bridges, and the dashboard keys a host on `(label, agent_type)`. A machine running two
  coding agents connects through two connectors and shows up as two facts, which is what they
  are — not one row whose agent type keeps changing.
"""

from __future__ import annotations

import getpass
import json
import shutil
import socket
import subprocess
import time
from typing import Any

from . import config, runtime

#: The coding agent these hooks belong to. Claude Code is the only one this package speaks to
#: today; a codex connector would report `codex` and the dashboard needs no change to show it.
AGENT_TYPE = "claude_code"


def state_file():
    return config.state_dir() / "host.json"


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict[str, Any]) -> None:
    """Best-effort bookkeeping. This file is a convenience for `status`, not the record —
    the dashboard holds the record, and the server dedupes on `(label, agent_type)`, so
    losing this loses nothing but a timestamp."""
    path = state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def last_report_at() -> float:
    """When this host last reported successfully, or `0.0` if it never has."""
    try:
        return float(_load_state().get("last_report_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def report(connected: bool = True) -> runtime.Result:
    """Say this host is here, or that it is leaving. Never raises."""
    try:
        cfg = config.AgentConfig.load()
    except FileNotFoundError as exc:
        return runtime.Result(False, detail=str(exc))

    args = ["host", "report", "--label", cfg.label, "--agent-type", AGENT_TYPE]
    if not connected:
        args.append("--disconnected")
    if connected:
        # The declaration half of access (design.md §24). Sent on every report rather than once
        # at install, so a host whose address changed (a new Tailscale IP, a new LAN) says so and
        # the dashboard can drop its approval to `address_changed` instead of aiming an old
        # command at a machine that moved.
        if cfg.wants_access:
            args.append("--wants-access")
            # Configured values win; the fallbacks are for a host installed before this feature
            # existed (empty fields in its `agent.json`) and are re-resolved on every report, so
            # a machine that joins a tailnet later starts declaring its tailnet address without
            # anyone re-running the installer.
            ssh_host = cfg.ssh_host or default_ssh_host()
            ssh_user = cfg.ssh_user or current_user()
            if ssh_host:
                args += ["--ssh-host", ssh_host]
            if ssh_user:
                args += ["--ssh-user", ssh_user]
            if cfg.ssh_port and cfg.ssh_port != 22:
                args += ["--ssh-port", str(cfg.ssh_port)]

    result = runtime.cpctl(*args)
    if result.ok:
        _save_state(
            {
                "label": cfg.label,
                "agent_type": AGENT_TYPE,
                "connected": connected,
                "last_report_at": time.time(),
            }
        )
    return result


def current_user() -> str:
    """The account this connector runs as — the only one it can install a key for.

    Used at install time to fill `ssh_user` in. `getpass` can raise when there is no
    controlling terminal and no passwd entry; the caller treats an empty answer as "not
    declared" rather than failing an install over a display field.
    """
    try:
        return getpass.getuser()
    except Exception:
        return ""


def default_ssh_host() -> str:
    """The best address this machine can offer for someone trying to reach it.

    Tailscale first, because it is the one that works from a dashboard on another network —
    which is the case this feature exists for. Then the fqdn, then the bare hostname, which may
    or may not resolve; the dashboard shows whatever this returns to the user, who can correct
    it when approving. Guessing is fine here precisely because guessing is not authorizing.
    """
    tailscale = _tailscale_ip()
    if tailscale:
        return tailscale
    try:
        fqdn = socket.getfqdn()
    except OSError:
        fqdn = ""
    return fqdn or socket.gethostname()


def _tailscale_ip() -> str:
    """This host's tailnet IPv4, or "" when Tailscale is not installed or not up."""
    executable = shutil.which("tailscale")
    if not executable:
        return ""
    try:
        proc = subprocess.run(
            [executable, "ip", "-4"], capture_output=True, text=True, timeout=5,
            **runtime.NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else ""


__all__ = [
    "AGENT_TYPE",
    "current_user",
    "default_ssh_host",
    "last_report_at",
    "report",
    "state_file",
]
