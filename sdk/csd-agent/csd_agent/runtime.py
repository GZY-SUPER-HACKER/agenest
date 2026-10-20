"""Running `cpctl`, and the one rule that governs it: a failed report is never an error.

`csd-agent` sits in the same seat as `cpctl` — on the agent's critical path — so every
function here returns a result instead of raising. Callers decide what to do about a
failure, and in every caller in this package the answer is "log it and carry on", because
a dashboard that is down must not become a Claude Code session that will not start.

The one thing worth knowing about the invocation: `cpctl` is run from its vendored source
via `PYTHONPATH`, not from an installed entry point. That is on purpose. The lab machine
has no sudo, a Python older than cpctl's declared floor (3.11 — the floor is wrong, see
the README), and a conda install that belongs to someone's research. Putting three small
stdlib-only files in `~/.csd/cpctl` and pointing `PYTHONPATH` at them touches none of it,
and uninstalling is `rm -rf`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from . import config

#: cpctl's own timeout is 3 s and it retries 3 times with backoff, so a healthy failure
#: still returns in well under this. The cap exists for the pathological case where the
#: interpreter itself will not start — without it a hook could hang Claude Code.
SUBPROCESS_TIMEOUT = 25.0


@dataclass
class Result:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    detail: str = ""

    @property
    def value(self) -> str:
        """First line of stdout, stripped — how the subcommands return an id."""
        return self.stdout.strip().splitlines()[0].strip() if self.stdout.strip() else ""


def environment() -> dict[str, str]:
    """The environment `cpctl` needs, built fresh so a hook inherits nothing stale."""
    cfg = config.AgentConfig.load()
    values = config.read_env()

    env = dict(os.environ)
    env["CSD_BASE_URL"] = values.get("CSD_BASE_URL", cfg.base_url)
    if token := values.get("CSD_INGEST_TOKEN"):
        env["CSD_INGEST_TOKEN"] = token
    env["CSD_STATE_DIR"] = str(config.state_dir())
    env["PYTHONPATH"] = str(config.cpctl_dir())

    # cpctl runs on urllib, which honours http_proxy for *every* URL including 127.0.0.1.
    # On a host behind an egress proxy the tunnel is then routed to the proxy and dies —
    # and the failure looks like "the dashboard is down", not "the proxy ate loopback".
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "all_proxy"):
        env.pop(var, None)
    return env


def cpctl(*args: str) -> Result:
    """Run one `cpctl` command. Never raises."""
    try:
        cfg = config.AgentConfig.load()
    except FileNotFoundError as exc:
        return Result(False, detail=str(exc))

    if not (config.cpctl_dir() / "cpctl" / "cli.py").exists():
        return Result(False, detail=f"cpctl source missing at {config.cpctl_dir()}")

    try:
        proc = subprocess.run(
            [cfg.python, "-m", "cpctl", *args],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            env=environment(),
            cwd=str(config.agent_home()),
        )
    except subprocess.TimeoutExpired:
        return Result(False, detail=f"cpctl {' '.join(args[:2])} timed out")
    except OSError as exc:
        return Result(False, detail=f"could not start {cfg.python}: {exc}")

    if proc.returncode == 0:
        return Result(True, proc.stdout, proc.stderr)

    # Exit 3 is cpctl's "the report did not reach the server" — distinct from 2 (a usage
    # mistake in this package). Both are reported, neither is raised.
    detail = (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
    return Result(False, proc.stdout, proc.stderr, detail=detail.splitlines()[0][:200])
