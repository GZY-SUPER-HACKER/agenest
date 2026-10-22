"""Where `csd-agent` keeps its state, and what it knows about this host.

Everything lives under one directory (`$CSD_AGENT_HOME`, default `~/.csd`) so that
uninstall is "stop the units, delete one directory" and nothing is scattered. On a shared
lab machine that matters: the footprint has to be obvious to whoever finds it later,
including you in six months.

Layout:

    ~/.csd/
      agent.json     what this host was told at install time (cloud, ports, interpreter)
      env            CSD_BASE_URL / CSD_INGEST_TOKEN, mode 600 — the only secret
      cpctl/         a vendored copy of the reporting CLI's source
      bin/csd-hook   the wrapper Claude Code's hooks point at
      state/
        tracked.json which CC sessions this host believes are live
        *.id         cpctl's own per-directory session cache

Nothing here is a daemon. Every command is a short-lived process; on Linux the only
long-lived things are the two systemd user units the installer writes, and those run `ssh`
and a timer — not code of ours. On Windows the equivalents are a Startup entry supervising
`ssh` and a Task Scheduler task.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path


def agent_home() -> Path:
    return Path(os.environ.get("CSD_AGENT_HOME") or (Path.home() / ".csd"))


def is_loopback_url(url: str) -> bool:
    """Is this URL on this machine? The proxy rules differ, so the caller needs to know.

    Loopback must bypass the system proxy: urllib honours `http_proxy` for 127.0.0.1 too,
    and a proxy that answers a tunnel request looks exactly like "the dashboard is down".
    A published URL (Tailscale Funnel, for instance) must *not* be stripped of the proxy:
    a machine that needs one to reach the internet would otherwise stop reporting.
    """
    return url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))


def env_file() -> Path:
    return agent_home() / "env"


def config_file() -> Path:
    return agent_home() / "agent.json"


def state_dir() -> Path:
    return agent_home() / "state"


def cpctl_dir() -> Path:
    return agent_home() / "cpctl"


def bin_dir() -> Path:
    return agent_home() / "bin"


def tracked_file() -> Path:
    return state_dir() / "tracked.json"


@dataclass
class AgentConfig:
    """What the installer learned, so every later command can stay argument-free.

    The units and the hook wrapper both read this indirectly (the units get their values
    baked in at install time, the hook reads it at run time), which is why it lives on disk
    rather than in the environment: a hook fired by Claude Code has none of your shell.
    """

    cloud_host: str
    cloud_user: str = "root"
    cloud_ssh_port: int = 22
    cloud_port: int = 8780
    tunnel_port: int = 18781
    python: str = "python3"
    ssh_key: str = "~/.ssh/id_ed25519"
    label: str = field(default_factory=lambda: socket.gethostname())
    #: This agent host *is* the machine the dashboard runs on, so there is nothing to tunnel
    #: to: the API is already on loopback. A flag rather than a check for `cloud_host` being
    #: `127.0.0.1`, because what differs is which units get written — and a remote host could
    #: legitimately address its dashboard over loopback through some other arrangement.
    local: bool = False
    #: A dashboard URL this host can reach directly — typically a Tailscale Funnel or Serve
    #: address. When set there is nothing to tunnel to: every report goes straight to this
    #: URL and the installer writes no tunnel unit. This is the only remote shape that works
    #: on a platform without systemd (Windows), because a hook is a short-lived process and
    #: needs no daemon — the heartbeat is the only thing that still needs a scheduler.
    direct_url: str = ""
    #: Whether this host declares itself driveable by the dashboard's personal agent
    #: (docs/design.md §24). On by default: the declaration is a *request* that puts a prompt in
    #: front of the user, and a machine that never asks can never be approved. `--no-drive`
    #: turns it off for a host that should stay unlisted.
    wants_access: bool = True
    #: The address this connector believes reaches it, and the account a key would be installed
    #: for. Both are declarations, unverified by the dashboard — the user confirms the address
    #: when they approve. Empty means "not declared", and the dashboard falls back to the label.
    ssh_host: str = ""
    ssh_user: str = ""
    ssh_port: int = 22
    #: The DSH home and the profiles the reporter was installed into. Empty means this host
    #: had no DeepSeek Harness at install time (or the operator passed `--no-dsh`). Recorded
    #: rather than rediscovered so `uninstall` removes exactly what `install` added.
    dsh_profiles: list[str] = field(default_factory=list)
    dsh_home: str = ""
    #: The Pi agent directory the extension was copied into, and the file it copied. Recorded
    #: rather than rediscovered so `uninstall` removes exactly what `install` added — an agent
    #: started with `PI_CODING_AGENT_DIR` would otherwise point uninstall somewhere else.
    pi_home: str = ""
    pi_extension: str = ""

    @property
    def base_url(self) -> str:
        """Where the API is, from this host.

        A direct URL wins: the dashboard is already published at an address this host can
        reach, so nothing needs forwarding. Otherwise local talks to the dashboard's own
        port and a remote host talks to the near end of the tunnel.
        """
        if self.direct_url:
            return self.direct_url.rstrip("/")
        port = self.cloud_port if self.local else self.tunnel_port
        return f"http://127.0.0.1:{port}"

    def save(self) -> None:
        path = config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls) -> "AgentConfig":
        path = config_file()
        if not path.exists():
            raise FileNotFoundError(
                f"{path} does not exist — run `csd-agent install` first"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def read_env() -> dict[str, str]:
    """Parse `~/.csd/env` and return its KEY=value pairs.

    Deliberately not `source`d by the units: systemd's `EnvironmentFile` and a shell's
    `source` disagree about quoting and expansion, and one file read two ways is how a
    token ends up subtly different in the two paths. The units use `EnvironmentFile`; the
    hook wrapper sources it. This function serves the in-process callers.
    """
    path = env_file()
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values
