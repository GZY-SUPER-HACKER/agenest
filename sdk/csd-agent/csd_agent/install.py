"""One command to put this host on the dashboard, and one to take it off.

The installer exists because the manual version is nine steps, four of which have a failure
mode that is silent or misleading (documented at each step below). Doing them by hand once
is how you learn what they are; doing them by hand on every machine is how you end up with
a host that reports nothing and no idea why.

What it touches, exhaustively:

    ~/.csd/                       created (agent.json, env, cpctl/, csd-agent/, bin/, state/)
    ~/.config/systemd/user/       three units: tunnel, heartbeat service, heartbeat timer
    ~/.claude/settings.json       four hook entries merged in, never overwritten

And what it deliberately does not touch: no sudo anywhere, no pip, no conda environment, no
package manager, nothing outside your home directory. On a shared lab machine that is the
difference between "I can undo this" and "I have to ask someone".
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import access, config, dsh, host, pi

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
SETTINGS = Path.home() / ".claude" / "settings.json"

#: Where a `--local` install reads the ingest token from: this machine *is* the dashboard, so
#: the token is already on disk here. `deploy.sh` writes it 0640 root:csd, so this works as
#: root or for anyone in the `csd` group — otherwise `--token` is the way in.
LOCAL_ENV_FILE = Path("/etc/csd/csd.env")

#: Marker used to recognise *our* hook entries on a re-run, so install is idempotent and
#: `uninstall` can remove exactly what it added. It is the wrapper's path, which is unique
#: to this install and appears in no hand-written hook.
HOOK_MARKER = str(config.bin_dir() / "csd-hook")

#: Which Claude Code events we wire. `Stop` is absent on purpose — it fires per reply, not
#: per session, and wiring it to a session end would make every session terminal after its
#: first answer (see csd_agent/hooks.py for the full argument).
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Notification", "SessionEnd")

#: Seconds Claude Code waits for a hook before giving up. cpctl is capped at 2 s per attempt
#: and one retry by the wrapper, so this is a backstop for a wedged interpreter.
HOOK_TIMEOUT = 20

#: Windows has no systemd user units. Task Scheduler is the equivalent for the one job that
#: must outlive a hook: the five-minute liveness probe. The tunnel is deliberately absent —
#: on Windows the supported remote shape is `--url` (a published dashboard), because a hook
#: is a short-lived process and needs no daemon at all.
IS_WINDOWS = os.name == "nt"
HEARTBEAT_TASK = "CSD-Agent Heartbeat"

#: Per-user startup folder. The tunnel is a *long-running* process, and Task Scheduler gives
#: a task a 72-hour execution limit by default — a tunnel that silently stops on day four is
#: worse than no tunnel. A startup entry has no such limit, and the launcher restarts ssh in
#: a loop, so a dropped connection is not an outage.
STARTUP_DIR = (
    Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
)
TUNNEL_STARTUP = "csd-agent-tunnel.vbs"

#: The tunnel keeper. `while True` around ssh is this platform's `Restart=always`; the pid
#: file is how `uninstall` stops the one process Windows has no service manager for.
TUNNEL_LAUNCHER = '''\
"""Keep an SSH tunnel to the dashboard alive. Written by `csd-agent install`."""
import os
import socket
import subprocess
import time
from pathlib import Path

home = Path(__file__).resolve().parent.parent
state = home / "state"
state.mkdir(parents=True, exist_ok=True)

CMD = {command!r}
CREATE_NO_WINDOW = 0x08000000


def port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", {tunnel_port}), timeout=0.5):
            return True
    except OSError:
        return False


# A second logon (RDP, fast user switching) must not start a keeper that fights the first
# for the local port. If a tunnel is already up, the keeper that owns it is the right one.
if port_open():
    raise SystemExit(0)

(state / "tunnel.pid").write_text(str(os.getpid()), encoding="utf-8")

try:
    while True:
        if port_open():
            time.sleep(5)
            continue
        try:
            subprocess.call(CMD, creationflags=CREATE_NO_WINDOW)
        except OSError:
            pass
        # A tunnel that just failed (network down, server rebooting) is retried, not fatal.
        time.sleep(5)
finally:
    try:
        (state / "tunnel.pid").unlink()
    except OSError:
        pass
'''

#: Run by Task Scheduler through `pythonw.exe`, so no console appears every five minutes.
#: It resolves the vendored package from its own location: a scheduled task starts with
#: neither PYTHONPATH nor this package's directory, and baking paths here keeps the task
#: definition free of environment variables. The token and base URL come from `~/.csd/env`
#: inside `runtime.environment()`, exactly as they do for a hook.
HEARTBEAT_LAUNCHER = '''\
"""Run one CSD heartbeat with no console window. Written by `csd-agent install`."""
import os
import sys
import traceback
from pathlib import Path

home = Path(__file__).resolve().parent.parent
os.environ.setdefault("CSD_AGENT_HOME", str(home))
sys.path.insert(0, str(home / "csd-agent"))

try:
    from csd_agent.cli import main
    code = main(["heartbeat"])
except Exception:
    # A scheduled task has no terminal and no journal. Without this file a crash is silent,
    # and "the host went stale" is the only symptom anyone would ever see.
    (home / "state").mkdir(parents=True, exist_ok=True)
    with (home / "state" / "heartbeat.log").open("a", encoding="utf-8") as handle:
        traceback.print_exc(file=handle)
    code = 1
raise SystemExit(code)
'''

TUNNEL_UNIT = """\
[Unit]
Description=CSD tunnel to the coding-session dashboard
Documentation=file://{home}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# `ExitOnForwardFailure` matters more than it looks: without it ssh logs a warning, keeps
# running, and forwards nothing, so the unit reports healthy while every report fails.
# `ServerAlive*` is the half that detects a connection the network has already dropped —
# without it the socket goes half-open, ssh never exits, and Restart=always never fires
# because from systemd's point of view nothing went wrong.
ExecStart={ssh} -N \\
    -o BatchMode=yes \\
    -o ExitOnForwardFailure=yes \\
    -o ServerAliveInterval=30 \\
    -o ServerAliveCountMax=3 \\
    -o StrictHostKeyChecking=accept-new \\
    -i {key} \\
    -p {cloud_ssh_port} \\
    -L {tunnel_port}:127.0.0.1:{cloud_port} \\
    {cloud_user}@{cloud_host}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""

HEARTBEAT_SERVICE = """\
[Unit]
Description=CSD heartbeat — report which Claude Code sessions are still alive
Documentation=file://{home}/README.md
{tunnel_deps}
[Service]
Type=oneshot
EnvironmentFile={env}
Environment=CSD_AGENT_HOME={home}
Environment=PYTHONPATH={pkg}
ExecStart={python} -m csd_agent heartbeat
"""

#: Substituted into HEARTBEAT_SERVICE. A local install has no tunnel, and a unit that waited
#: on one that does not exist would be a puzzle for whoever next reads `systemctl --user
#: list-units` — systemd tolerates a `Wants` for a missing unit, which is exactly why nobody
#: would notice the mistake.
TUNNEL_DEPS = """\
# The heartbeat talks through the tunnel, so there is no point starting before it. `Wants`
# rather than `Requires`: a heartbeat that arrives before the tunnel is a wasted run, not a
# failed one, and the timer will be back in five minutes either way.
Wants=csd-tunnel.service
After=csd-tunnel.service
"""

LOCAL_DEPS = """\
# Local install (--local): the dashboard runs on this machine, so there is no tunnel to wait
# for and no tunnel unit at all.
"""

HEARTBEAT_TIMER = """\
[Unit]
Description=Run the CSD heartbeat every {minutes} minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec={minutes}min
# Jitter, so that a fleet of these never synchronises onto the same instant.
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
"""

HOOK_WRAPPER = """\
#!/usr/bin/env bash
# csd-hook — the command Claude Code's hooks invoke. Written by `csd-agent install`.
#
# Two details are load-bearing:
#
#   `exec` — the interpreter replaces this shell rather than being spawned by it. That keeps
#   the process's ancestry intact, which is how csd_agent.tracked.find_cc_pid() walks up to
#   find the Claude Code process and records its pid for the heartbeat to check later.
#   Backgrounding the work instead would reparent it to init and lose that chain.
#
#   `CSD_RETRIES`/`CSD_TIMEOUT` — cpctl's defaults (3 s, three retries with backoff) are
#   right for a launcher script and wrong here: a hook is on the critical path of someone's
#   Claude Code session. When the tunnel is down the failure is "connection refused", which
#   is instant, so a long retry ladder buys nothing but latency the user feels.
set -uo pipefail

export CSD_AGENT_HOME="${{CSD_AGENT_HOME:-{home}}}"
export CSD_RETRIES="${{CSD_RETRIES:-1}}"
export CSD_TIMEOUT="${{CSD_TIMEOUT:-2}}"
export PYTHONPATH="$CSD_AGENT_HOME/csd-agent"

exec {python} -m csd_agent hook "$@"
"""


class Installer:
    def __init__(self, cfg: config.AgentConfig, heartbeat_minutes: int = 5,
                 token: str | None = None, pkg_source: Path | None = None,
                 no_dsh: bool = False, dsh_profiles: list[str] | None = None,
                 dsh_home: str | None = None, no_pi: bool = False,
                 pi_home: str | None = None) -> None:
        self.cfg = cfg
        self.heartbeat_minutes = heartbeat_minutes
        self.token = token
        self.pkg_source = pkg_source
        self.no_dsh = no_dsh
        self.dsh_profiles = dsh_profiles
        self.dsh_home = dsh_home
        self.dsh_ok = True
        self.no_pi = no_pi
        self.pi_home = pi_home
        self.pi_ok = True
        self.notes: list[str] = []

    # ------------------------------------------------------------------ #
    def note(self, message: str) -> None:
        self.notes.append(message)
        print(f"  {message}")

    def step(self, message: str) -> None:
        print(f"\n== {message}")

    # ------------------------------------------------------------------ #
    def run(self) -> int:
        self.step("1/8  vendoring cpctl and csd-agent")
        self._vendor()
        self.step("2/8  resolving the ingest token")
        if not self._write_env():
            return 1
        self.step("3/8  writing config and the hook wrapper")
        self._write_config_and_wrapper()
        self.step("4/8  systemd user units")
        self._write_units()
        self.step("5/8  Claude Code hooks")
        self._merge_hooks()
        self.step("6/8  DeepSeek Harness reporter")
        self._install_dsh()
        self.step("7/8  Pi reporter")
        self._install_pi()
        self.step("8/8  starting and verifying")
        return self._verify()

    # ------------------------------------------------------------------ #
    def _locate(self) -> tuple[Path | None, Path | None]:
        """Find the cpctl and csd_agent packages next to wherever `--source` points.

        Accepts either the `sdk/` directory or this package's own `sdk/csd-agent/`, because
        the two are one path segment apart and getting it wrong produces a copy step that
        silently finds nothing — the install then "succeeds" with an unusable vendored tree.
        Checking for the marker file rather than trusting the layout is the difference
        between a clear error and a mysterious one.
        """
        roots = []
        if self.pkg_source:
            roots = [self.pkg_source, self.pkg_source.parent, self.pkg_source.parent.parent]
        else:
            here = Path(__file__).resolve().parent
            roots = [here.parent, here.parent.parent, here.parent.parent.parent]

        cpctl = pkg = None
        for root in roots:
            for candidate in (root / "cpctl", root / "sdk" / "cpctl"):
                if not cpctl and (candidate / "cpctl" / "cli.py").exists():
                    cpctl = candidate
            for candidate in (root / "csd_agent", root / "sdk" / "csd-agent" / "csd_agent"):
                if not pkg and (candidate / "hooks.py").exists():
                    pkg = candidate
            if cpctl and pkg:
                break

        if self.pkg_source and self.pkg_source.name == "csd-agent":
            fallback = self.pkg_source / "csd_agent"
            pkg = pkg or (fallback if (fallback / "hooks.py").exists() else None)
        return cpctl, pkg

    def _vendor(self) -> None:
        """Copy cpctl and csd-agent into ~/.csd.

        Copied rather than installed because the alternative on this class of machine is a
        pip install into whichever interpreter happens to be first on PATH — and here that
        is a system Python older than cpctl's declared floor, with no sudo to change it, next
        to a conda install that belongs to someone else's research. Three small stdlib-only
        packages under the user's own home touch none of that.
        """
        root = config.agent_home()
        root.mkdir(parents=True, exist_ok=True)
        source_cpctl, source_pkg = self._locate()

        cpctl_dest = config.cpctl_dir() / "cpctl"
        if source_cpctl is not None:
            # Always refresh, never skip-if-present: the vendored copy is the deployed
            # artifact, and a fix that never reaches it is a fix that does not exist.
            existed = (cpctl_dest / "cli.py").exists()
            shutil.copytree(source_cpctl / "cpctl", cpctl_dest, dirs_exist_ok=True)
            self.note(f"cpctl {'refreshed' if existed else 'copied'} from {source_cpctl}")
        else:
            self.note(f"ERROR: cpctl source not found (looked near {self.pkg_source})")
            self.note(f"       expected cpctl/cli.py; place a copy at {config.cpctl_dir()}")
            return

        agent_dest = root / "csd-agent" / "csd_agent"
        if source_pkg is not None:
            shutil.copytree(source_pkg, agent_dest, dirs_exist_ok=True)
            self.note(f"csd-agent copied to {root / 'csd-agent'}")
        else:
            self.note("WARNING: csd_agent source not found; the units will not start")

        config.state_dir().mkdir(parents=True, exist_ok=True)
        config.bin_dir().mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    def _fetch_token(self) -> str:
        """Read the ingest token off the cloud over SSH, so it never passes through a shell.

        Defaulting to this instead of asking for `--token` is not laziness: the alternative
        is a secret in your shell history, in your terminal scrollback, and in whatever
        records hooks you have installed on this machine. The cloud is already reachable
        with a key you already have.
        """
        remote = f"{self.cfg.cloud_user}@{self.cfg.cloud_host}"
        command = "grep ^CSD_INGEST_TOKEN= /etc/csd/csd.env | cut -d= -f2-"
        try:
            proc = subprocess.run(
                ["ssh", "-i", os.path.expanduser(self.cfg.ssh_key),
                 "-p", str(self.cfg.cloud_ssh_port),
                 "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 "-o", "StrictHostKeyChecking=accept-new",
                 remote, command],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _read_local_token(self) -> str:
        """Read the ingest token from this machine's own deployment.

        Parsed rather than sourced, for the reason `config.read_env` gives about `~/.csd/env`:
        one file read two ways is how a token ends up subtly different in the two paths.
        """
        try:
            text = LOCAL_ENV_FILE.read_text(encoding="utf-8")
        except OSError:
            return ""
        for line in text.splitlines():
            if line.startswith("CSD_INGEST_TOKEN="):
                return line.partition("=")[2].strip()
        return ""

    def _write_env(self) -> bool:
        existing = config.read_env()
        token = self.token or ""

        # Order matters, and this one was learned the hard way: an explicit `--cloud` makes
        # that host the authority for the credential. Reusing whatever was in the env file
        # first kept a local dev token across a switch to a published dashboard, and every
        # report answered 401 — which reads as "the token is broken", not "the token is from
        # a different install".
        if not token and self.cfg.cloud_host and self.cfg.cloud_host != "127.0.0.1":
            token = self._fetch_token()
            self.note("token fetched from the cloud over SSH" if token
                      else "could not read the token from the cloud")
        if not token and existing.get("CSD_BASE_URL", "").rstrip("/") == self.cfg.base_url:
            token = existing.get("CSD_INGEST_TOKEN", "")
            if token:
                self.note("reusing the token already in the env file (same dashboard)")
        elif not token and existing.get("CSD_INGEST_TOKEN"):
            self.note(
                f"old env pointed at {existing.get('CSD_BASE_URL')}; not reusing its token"
            )
        if not token and self.cfg.local:
            token = self._read_local_token()
            self.note(
                f"token read from {LOCAL_ENV_FILE}" if token
                else f"could not read {LOCAL_ENV_FILE} (0640 root:csd — try sudo, or --token)"
            )
        if not token and not self.cfg.local:
            token = self._fetch_token()
            self.note("token fetched from the cloud over SSH" if token
                      else "could not read the token from the cloud")
        if not token:
            self.note("ERROR: no ingest token. Pass --token, or make sure this host can read")
            if self.cfg.local:
                self.note(f"       {LOCAL_ENV_FILE} on this machine.")
            else:
                self.note(f"       /etc/csd/csd.env on "
                          f"{self.cfg.cloud_user}@{self.cfg.cloud_host}.")
            return False

        path = config.env_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 600 before anything is written into it, not after.
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(
            "# Written by `csd-agent install`. Contains the ingest token: keep it 0600.\n"
            "# The systemd units read this via EnvironmentFile; the hook wrapper sources it.\n"
            f"CSD_BASE_URL={self.cfg.base_url}\n"
            f"CSD_INGEST_TOKEN={token}\n"
            f"CSD_STATE_DIR={config.state_dir()}\n"
            "no_proxy=127.0.0.1,localhost\n",
            encoding="utf-8",
        )
        self.note(f"env written to {path} (mode 600, token {len(token)} chars)")
        return True

    # ------------------------------------------------------------------ #
    def _write_config_and_wrapper(self) -> None:
        self.cfg.save()
        self.note(f"config written to {config.config_file()}")

        wrapper = config.bin_dir() / "csd-hook"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(
            HOOK_WRAPPER.format(home=config.agent_home(), python=self.cfg.python),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        self.note(f"hook wrapper written to {wrapper}")

    # ------------------------------------------------------------------ #
    def _write_units(self) -> None:
        if IS_WINDOWS:
            self._write_windows_tasks()
            return

        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        cfg = self.cfg
        # A direct URL needs no tunnel on any platform. `tunnel_deps` matters for the
        # ordering only; with no tunnel unit, a `Wants=` for it would name a unit nobody
        # wrote, and systemd tolerates that silently — which is exactly why it is removed
        # here rather than left to be noticed by nobody.
        direct = bool(cfg.direct_url)
        rendered = {
            "csd-heartbeat.service": HEARTBEAT_SERVICE.format(
                home=config.agent_home(),
                env=config.env_file(),
                pkg=config.agent_home() / "csd-agent",
                python=cfg.python,
                tunnel_deps=LOCAL_DEPS if (cfg.local or direct) else TUNNEL_DEPS,
            ),
            "csd-heartbeat.timer": HEARTBEAT_TIMER.format(minutes=self.heartbeat_minutes),
        }
        # No tunnel unit for a local or direct install: in the first case the API is already
        # on this machine's loopback, in the second it is already published. An `ssh -L` to
        # nowhere would be a unit whose only job is to fail and restart.
        if not cfg.local and not direct:
            rendered["csd-tunnel.service"] = TUNNEL_UNIT.format(
                home=config.agent_home(),
                ssh=shutil.which("ssh") or "/usr/bin/ssh",
                key=os.path.expanduser(cfg.ssh_key),
                tunnel_port=cfg.tunnel_port,
                cloud_port=cfg.cloud_port,
                cloud_ssh_port=cfg.cloud_ssh_port,
                cloud_user=cfg.cloud_user,
                cloud_host=cfg.cloud_host,
            )

        for name, text in rendered.items():
            (UNIT_DIR / name).write_text(text, encoding="utf-8")
        self.note(f"units written to {UNIT_DIR} ({', '.join(sorted(rendered))})")

        if cfg.local or direct:
            # A previous non-local install on this machine would have left a tunnel unit
            # behind, still holding a port and still dialling out. Remove it as part of
            # switching, rather than leaving two answers to "how does this host reach the
            # dashboard" on disk at once.
            stale = UNIT_DIR / "csd-tunnel.service"
            if stale.exists():
                self._systemctl("disable", "--now", "csd-tunnel.service")
                try:
                    stale.unlink()
                    self.note("removed the leftover csd-tunnel.service "
                              "(this install does not use a tunnel)")
                except OSError:
                    pass

        if shutil.which("systemctl"):
            self._systemctl("daemon-reload")
            units = ["csd-heartbeat.timer"] if (cfg.local or direct) else [
                "csd-tunnel.service", "csd-heartbeat.timer",
            ]
            for unit in units:
                self._systemctl("enable", "--now", unit)
        else:
            self.note("WARNING: no systemctl; units written but not started")

    def _write_windows_tasks(self) -> None:
        """Install the two long-lived things Windows needs, because there is no systemd.

        A hook fires and exits; only these have to survive a reboot:

        - **the tunnel**, unless the dashboard is reached directly (`--url`). It is a
          long-running `ssh`, so it goes in the Startup folder rather than Task Scheduler —
          a scheduled task is killed after 72 hours by default, and a tunnel that quietly
          stops on day four is worse than no tunnel. The launcher restarts ssh in a loop.
        - **the heartbeat**, a five-minute job, which Task Scheduler fits exactly.

        `pythonw.exe` for both: a console window at logon and every five minutes is how a
        background helper gets removed by the person it was installed for.
        """
        cfg = self.cfg
        pythonw = Path(cfg.python).with_name("pythonw.exe")
        if not pythonw.exists():
            pythonw = Path(cfg.python)
            self.note("note: pythonw.exe not found next to the interpreter; a console window")
            self.note("      may appear for each scheduled run")

        if cfg.direct_url:
            self.note(f"no tunnel: reports go directly to {cfg.direct_url}")
        elif not cfg.local:
            self._write_windows_tunnel(pythonw)

        launcher = config.bin_dir() / "csd-heartbeat.pyw"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(HEARTBEAT_LAUNCHER, encoding="utf-8")
        self.note(f"heartbeat launcher written to {launcher}")

        command = f'"{pythonw.as_posix()}" "{launcher.as_posix()}"'
        if self._schtasks(
            "/Create", "/F", "/TN", HEARTBEAT_TASK,
            "/SC", "MINUTE", "/MO", str(self.heartbeat_minutes),
            "/TR", command,
        ):
            self.note(f"scheduled task {HEARTBEAT_TASK!r} every {self.heartbeat_minutes} min")
        else:
            self.note(f"WARNING: could not create scheduled task {HEARTBEAT_TASK!r}")
            self.note("         create it by hand in Task Scheduler with the command above")

    def _write_windows_tunnel(self, pythonw: Path) -> None:
        """Write the tunnel keeper and arrange for it to start at logon."""
        cfg = self.cfg
        command = [
            shutil.which("ssh") or "ssh", "-N",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-i", Path(os.path.expanduser(cfg.ssh_key)).as_posix(),
            "-p", str(cfg.cloud_ssh_port),
            "-L", f"{cfg.tunnel_port}:127.0.0.1:{cfg.cloud_port}",
            f"{cfg.cloud_user}@{cfg.cloud_host}",
        ]
        launcher = config.bin_dir() / "csd-tunnel.pyw"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(
            TUNNEL_LAUNCHER.format(command=command, tunnel_port=cfg.tunnel_port),
            encoding="utf-8",
        )
        self.note(f"tunnel launcher written to {launcher}")

        STARTUP_DIR.mkdir(parents=True, exist_ok=True)
        startup = STARTUP_DIR / TUNNEL_STARTUP
        # A `.vbs` runner is the hidden one: a startup `.cmd` flashes a console at every
        # logon, and the first thing a user does with a flashing helper is disable it.
        startup.write_text(
            'Set sh = CreateObject("WScript.Shell")\r\n'
            f'sh.Run """{pythonw.as_posix()}"" ""{launcher.as_posix()}""", 0, False\r\n',
            encoding="utf-8",
        )
        self.note(f"tunnel starts at logon: {startup}")

        # Start it now, so verification and the first heartbeat have a tunnel. A re-install
        # must not spawn a second keeper: it would fight the first for the local port, and
        # the loser would restart forever.
        if _port_open("127.0.0.1", cfg.tunnel_port):
            self.note(f"tunnel already listening on 127.0.0.1:{cfg.tunnel_port}")
            return
        try:
            subprocess.Popen(
                [str(pythonw), str(launcher)],
                creationflags=0x08000000,
                close_fds=True,
            )
            self.note(f"tunnel starting on 127.0.0.1:{cfg.tunnel_port}")
        except OSError as exc:
            self.note(f"WARNING: could not start the tunnel ({exc}); log on again to start it")

    def _schtasks(self, *args: str) -> bool:
        """Run schtasks and turn a non-zero exit into a note, never an exception."""
        try:
            proc = subprocess.run(["schtasks", *args], capture_output=True, text=True)
        except OSError as exc:
            self.note(f"schtasks failed: {exc}")
            return False
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            if detail:
                self.note(detail.splitlines()[0][:200])
            return False
        return True

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True)

    # ------------------------------------------------------------------ #
    def _merge_hooks(self) -> None:
        """Add our hook entries to ~/.claude/settings.json, touching nothing else.

        A merge rather than a write, because this file is the user's: it already holds their
        statusline, their permissions, and their own hooks. On the dev machine it is also
        rewritten wholesale by a config manager on every launch — which is why re-running
        install is the supported way to recover, and why the hardening here is "identify our
        own entries by a marker and replace only those" rather than "compare and skip".
        """
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        if SETTINGS.exists():
            backup = SETTINGS.with_suffix(".json.bak-csd-agent")
            shutil.copy2(SETTINGS, backup)
            self.note(f"backed up {SETTINGS.name} -> {backup.name}")
            try:
                data = json.loads(SETTINGS.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                self.note(f"ERROR: {SETTINGS} is not valid JSON ({exc}); not touching it")
                return
        else:
            data = {}

        if not isinstance(data, dict):
            self.note(f"ERROR: {SETTINGS} is not a JSON object; not touching it")
            return

        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}

        added = []
        for event in HOOK_EVENTS:
            groups = hooks.get(event)
            groups = groups if isinstance(groups, list) else []

            # Drop any earlier entry of ours, keep everyone else's verbatim.
            kept = [
                group for group in groups
                if not _is_ours(group)
            ]
            kept.append({
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": _hook_command(event),
                    "timeout": HOOK_TIMEOUT,
                }],
            })
            hooks[event] = kept
            added.append(event)

        data["hooks"] = hooks
        SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        self.note(f"hooks merged: {', '.join(added)}")

    # ------------------------------------------------------------------ #
    def _install_dsh(self) -> None:
        """Wire DSH with the same one command that wires Claude Code's hooks.

        A host with no DSH is left alone — this package still works for Claude Code alone. A
        host that *has* DSH is a host whose operator expects it on the dashboard, so a failure
        here is reported and fails verification rather than being filed as a note.
        """
        if self.no_dsh:
            self.note("skipped (--no-dsh)")
            return

        home = Path(self.dsh_home).expanduser() if self.dsh_home else dsh.dsh_home()
        if not dsh.profiles(home):
            self.note(f"DeepSeek Harness not found at {home}; skipped")
            self.note("  (launch DSH once, then re-run install, if it is installed elsewhere)")
            return

        result = dsh.install(
            profiles_requested=self.dsh_profiles,
            pkg_source=self.pkg_source,
            home=home,
        )
        for line in result.notes:
            self.note(line)
        self.dsh_ok = result.ok or result.skipped
        if result.installed:
            self.cfg.dsh_home = result.home
            self.cfg.dsh_profiles = result.installed
            self.cfg.save()
        if not self.dsh_ok:
            self.note("PROBLEM: the DSH reporter is not fully wired; see above")

    # ------------------------------------------------------------------ #
    def _install_pi(self) -> None:
        """Copy the Pi extension where the standalone `pi` CLI will discover it.

        A host with no Pi is left alone: this package still works for Claude Code and DSH. A
        host that *has* Pi is a host whose operator expects it on the dashboard, so a failure is
        reported and fails verification rather than being filed as a note.

        The dashboard's own personal agent (`native`) is the same binary, and the extension is
        inert under `CSD_AGENT=1`. Installing it here is therefore not "make the Agent tab a
        coding session" — it is what lets a human's own `pi` command show up.
        """
        if self.no_pi:
            self.note("skipped (--no-pi)")
            return

        home = pi.pi_agent_dir(self.pi_home)
        if not home.is_dir():
            self.note(f"Pi not found at {home}; skipped")
            self.note("  (launch Pi once, then re-run install, if it is installed elsewhere)")
            return

        result = pi.install(pkg_source=self.pkg_source, home=home)
        for line in result.notes:
            self.note(line)
        self.pi_ok = result.ok or result.skipped
        if result.installed:
            self.cfg.pi_home = result.home
            self.cfg.pi_extension = result.installed[0]
            self.cfg.save()
        if not self.pi_ok:
            self.note("PROBLEM: the Pi reporter is not fully wired; see above")

    # ------------------------------------------------------------------ #
    def _verify(self) -> int:
        ok = True

        # Linux is the supported deployment, but the DSH reporter works on any OS and testing
        # it on a developer machine should not require logind. Missing tools are a skipped
        # check, not a failed install.
        if shutil.which("loginctl"):
            linger = subprocess.run(
                ["loginctl", "show-user", _current_user(), "-p", "Linger"],
                capture_output=True, text=True,
            )
            if "Linger=yes" not in linger.stdout:
                ok = False
                self.note("PROBLEM: lingering is off, so these units die when you log out.")
                self.note("         Fix with:  loginctl enable-linger $USER")
            else:
                self.note("linger is on — units survive logout")
        else:
            self.note("loginctl not available; skipped the linger check")

        if IS_WINDOWS:
            # Task Scheduler is the equivalent of the timer. The tunnel check is a *port*
            # check here: there is no unit to ask, and "is the near end open" is the only
            # question that matters to every report the hooks will make.
            scheduled = self._schtasks("/Query", "/TN", HEARTBEAT_TASK)
            self.note(f"{HEARTBEAT_TASK}: {'scheduled' if scheduled else 'MISSING'}")
            if not scheduled:
                ok = False

            if self.cfg.direct_url:
                self.note(f"tunnel: not used (direct {self.cfg.direct_url})")
            elif self.cfg.local:
                self.note("tunnel: not used (--local)")
            else:
                open_ = _port_open("127.0.0.1", self.cfg.tunnel_port)
                self.note(f"tunnel: 127.0.0.1:{self.cfg.tunnel_port} "
                          f"{'open' if open_ else 'NOT open'}")
                if not open_:
                    ok = False
                    self.note("         the tunnel did not come up. Check:")
                    self.note("           Task Manager for pythonw.exe (csd-tunnel.pyw)")
                    self.note(f"           {config.state_dir()}/tunnel.pid")
        else:
            if self.cfg.local or self.cfg.direct_url:
                self.note("csd-tunnel.service: not used (no tunnel in this install)")
            else:
                state = self._systemctl("is-active", "csd-tunnel.service").stdout.strip()
                self.note(f"csd-tunnel.service: {state}")
                if state != "active":
                    ok = False
                    self.note("         check: journalctl --user -u csd-tunnel -n 30")

            timer = self._systemctl("is-active", "csd-heartbeat.timer").stdout.strip()
            self.note(f"csd-heartbeat.timer: {timer}")

        # The only test that matters: can this host actually reach the API?
        #
        # Retried, because `systemctl --user enable --now` returns as soon as the unit is
        # *started*, and ssh still has to connect and authenticate before it binds the local
        # port. Checking once reports "000" on a perfectly healthy install, which is worse
        # than no check at all — it teaches you to ignore the line.
        curl = shutil.which("curl")
        if curl:
            # `/dev/null` is not a filename on Windows, and `--noproxy` belongs on a loopback
            # URL only: stripping the proxy from a published address breaks a host that needs
            # one to reach the internet.
            null = "NUL" if IS_WINDOWS else "/dev/null"
            probe_args = [curl, "-s", "-o", null, "-w", "%{http_code}", "--max-time", "5"]
            if config.is_loopback_url(self.cfg.base_url):
                probe_args += ["--noproxy", "*"]
            probe_args.append(f"{self.cfg.base_url}/api/sessions")

            code = "000"
            for attempt in range(10):
                probe = subprocess.run(probe_args, capture_output=True, text=True)
                code = probe.stdout.strip() or "000"
                if code in ("200", "401"):
                    break
                time.sleep(1.0 if attempt < 3 else 1.5)

            self.note(f"GET {self.cfg.base_url}/api/sessions -> {code}")
            if code not in ("200", "401"):
                ok = False
                if self.cfg.direct_url:
                    self.note(f"         {self.cfg.base_url} did not answer from this machine.")
                    self.note("         Check the address from a browser on this host.")
                elif self.cfg.local:
                    self.note("         the dashboard is not answering on this machine. Check:")
                    self.note("           systemctl --user status csd-heartbeat.service")
                else:
                    self.note("         the tunnel is not carrying traffic. Check with:")
                    self.note("           systemctl --user status csd-tunnel.service")
                    self.note("           journalctl --user -u csd-tunnel -n 30")

        # Say hello here as well as on the heartbeat timer, so the moment this command
        # reports success is the moment the host appears on the dashboard. A failure is
        # noted and not fatal: the install worked, and the timer will try again shortly.
        if ok:
            result = host.report(connected=True)
            if result.ok:
                self.note("host registered — it should be on the dashboard now")
            else:
                self.note(f"note: could not register this host yet ({result.detail})")
                self.note("      the heartbeat timer will report it on its next run")

            # A key may already be waiting: the user can approve a host the moment it appears,
            # and on a re-install the approval from before is still on file. One sync here means
            # the first heartbeat is not the first time it could have worked.
            access.sync()

            if self.cfg.wants_access:
                # The remote half of "tell the user what to do at connect time" (design.md §24).
                # Nothing about this install is blocked on it, which is why it is a note and not
                # a prompt: the dashboard is where the decision belongs, and the user may not be
                # at it right now.
                self.note("")
                self.note("This host declared that the dashboard's personal agent may drive it:")
                self.note(f"  {self.cfg.ssh_user}@{self.cfg.ssh_host}:{self.cfg.ssh_port}")
                self.note("Nothing is reachable yet. The dashboard shows it as *not authorized*")
                self.note("until you approve it there: Settings → Host access. Approving mints a")
                self.note("key, and this host installs it into ~/.ssh/authorized_keys on its next")
                self.note("heartbeat (within the timer interval).")

        # DSH is additive: its reporter reports the `dsh` host row itself, so there is no
        # second heartbeat here. What this check owns is the install the operator asked for —
        # the package must still be mounted in every profile it was put in.
        if not self.dsh_ok:
            ok = False
        if self.cfg.dsh_profiles:
            home = Path(self.cfg.dsh_home) if self.cfg.dsh_home else dsh.dsh_home()
            broken = [p for p in self.cfg.dsh_profiles if not dsh.profile_has_plugin(home, p)]
            version = dsh.plugin_version(config.agent_home() / dsh.PACKAGE)
            self.note(f"dsh reporter: {', '.join(self.cfg.dsh_profiles)} (v{version})")
            if broken:
                ok = False
                self.note(f"PROBLEM: reporter missing from {', '.join(broken)}; re-run install")
        elif not self.no_dsh:
            self.note("dsh reporter: not present (DeepSeek Harness not installed here)")

        # Pi is additive in the same way: its extension reports the `pi` host row itself, so
        # there is no second heartbeat here. What this check owns is the install the operator
        # asked for — the file must still be where Pi looks.
        if not self.pi_ok:
            ok = False
        if self.cfg.pi_home or self.cfg.pi_extension:
            home = Path(self.cfg.pi_home).expanduser() if self.cfg.pi_home else pi.pi_agent_dir(self.pi_home)
            installed = pi.installed_extension(home)
            version = pi.package_version(config.agent_home() / pi.PACKAGE)
            self.note(f"pi reporter: {installed} (v{version})")
            if not (installed.exists() and pi.is_ours(installed)):
                ok = False
                self.note("PROBLEM: the Pi extension is missing; re-run install")
        elif not self.no_pi:
            self.note("pi reporter: not present (Pi not installed here)")

        self.note("")
        if IS_WINDOWS:
            where = f"Task Scheduler ({HEARTBEAT_TASK}) and {config.state_dir()}/heartbeat.log"
        elif self.cfg.local or self.cfg.direct_url:
            where = "journalctl --user -u csd-heartbeat"
        else:
            where = "journalctl --user -u csd-tunnel"
        self.note(f"Done. Logs: {config.state_dir()}/hook.log, and {where}")
        return 0 if ok else 1


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _current_user() -> str:
    """The invoking user's name, robustly.

    `$USER` is unset in plenty of non-interactive SSH sessions, and the failure it causes
    here is silent: `loginctl show-user ''` fails, the check reports lingering as off, and
    the installer tells you to fix something that is already correct.
    """
    import getpass
    for candidate in (os.environ.get("USER"), os.environ.get("LOGNAME")):
        if candidate:
            return candidate
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return str(os.getuid())


def _hook_command(event: str) -> str:
    """The command Claude Code runs for one event.

    On POSIX the wrapper is executable and carries a shebang, so its path is the command.
    Windows has no shebang and Claude Code's hook shell is not necessarily bash, so the
    wrapper is invoked explicitly. Forward slashes because the program receiving this path
    is bash, not cmd — and because the same string is what `_is_ours` must recognise on a
    re-install.
    """
    if IS_WINDOWS:
        return f'bash "{Path(HOOK_MARKER).as_posix()}" {event}'
    return f"{HOOK_MARKER} {event}"


def _is_ours(group: object) -> bool:
    """Is this hook matcher-group one that a previous install wrote?"""
    if not isinstance(group, dict):
        return False
    entries = group.get("hooks")
    if not isinstance(entries, list):
        return False
    # Compare with separators normalised: the Windows command carries forward slashes while
    # the marker was built from a Path with backslashes. Without this, re-install appends a
    # second entry and uninstall cannot remove either.
    marker = HOOK_MARKER.replace("\\", "/")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        command = str(entry.get("command", "")).replace("\\", "/")
        if marker in command:
            return True
    return False


def _stop_windows_tunnel() -> None:
    """Kill the tunnel keeper and its ssh child, if one is recorded.

    `/T` is load-bearing: the launcher's child is ssh, and killing only the parent leaves the
    local port held and the tunnel half-alive. A missing pid file is a no-op — the process
    may already be gone, and uninstall must stay re-runnable.
    """
    path = config.state_dir() / "tunnel.pid"
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, text=True)
    try:
        path.unlink()
    except OSError:
        pass


def uninstall() -> int:
    """Stop the units, remove our hook entries, and say what was left behind."""
    try:
        cfg = config.AgentConfig.load()
    except FileNotFoundError:
        cfg = None

    # Before the units go down, because for a remote host the tunnel is what carries this
    # report — saying goodbye after stopping it would be talking into a closed socket. If it
    # fails anyway there is nothing to repair: the host simply stops being heard from and the
    # dashboard renders that as "maybe offline", which is what actually happened.
    result = host.report(connected=False)
    if result.ok:
        print("  host marked disconnected — it stays in the list, greyed")
    else:
        print(f"  note: could not report the disconnect ({result.detail})")
        print("        the host will read as 'maybe offline' instead, which is also true")

    # DSH first, for the same reason: its host row is told goodbye over the same tunnel. A
    # host that never had DSH is skipped, so this stays a no-op for a Claude Code install.
    # No agent.json is still a reason to look: `csd-agent dsh install` can be used on its own.
    dsh_home = (
        Path(cfg.dsh_home).expanduser() if cfg and cfg.dsh_home else dsh.dsh_home()
    )
    if (cfg and cfg.dsh_profiles) or dsh.profiles(dsh_home):
        print("\n== DeepSeek Harness")
        dsh.uninstall(
            profiles_requested=(cfg.dsh_profiles if cfg else None) or None,
            home=dsh_home,
            label=cfg.label if cfg else None,
        )

    # Pi's reporter is a copy in the user's extensions directory, so there is no manifest to
    # clean up — just our file, identified by its marker. The goodbye goes through
    # `pi.uninstall` for the same reason DSH's does: on a remote host the tunnel is what
    # carries it, and it is still up at this point.
    # No agent.json is still a reason to look: `csd-agent pi install` can be used on its own.
    pi_home = (
        Path(cfg.pi_home).expanduser() if cfg and cfg.pi_home else pi.pi_agent_dir()
    )
    pi_target = pi.installed_extension(pi_home)
    if (cfg and cfg.pi_extension) or pi_target.exists():
        print("\n== Pi")
        pi.uninstall(home=pi_home, label=cfg.label if cfg else None)

    if IS_WINDOWS:
        # Task Scheduler has no "disable and remove"; deleting the task is both. A task that
        # does not exist makes this a no-op, which is what makes uninstall re-runnable.
        _stop_windows_tunnel()
        try:
            (STARTUP_DIR / TUNNEL_STARTUP).unlink()
        except OSError:
            pass
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", HEARTBEAT_TASK],
                       capture_output=True, text=True)
    else:
        for unit in ("csd-heartbeat.timer", "csd-heartbeat.service", "csd-tunnel.service"):
            subprocess.run(["systemctl", "--user", "disable", "--now", unit],
                           capture_output=True, text=True)

        for name in ("csd-tunnel.service", "csd-heartbeat.service", "csd-heartbeat.timer"):
            try:
                (UNIT_DIR / name).unlink()
            except OSError:
                pass
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, text=True)

    if SETTINGS.exists():
        try:
            data = json.loads(SETTINGS.read_text(encoding="utf-8"))
            hooks = data.get("hooks")
            if isinstance(hooks, dict):
                for event in list(hooks):
                    groups = hooks.get(event)
                    if isinstance(groups, list):
                        kept = [g for g in groups if not _is_ours(g)]
                        if kept:
                            hooks[event] = kept
                        else:
                            hooks.pop(event, None)
                if not hooks:
                    data.pop("hooks", None)
                else:
                    data["hooks"] = hooks
                SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8")
                print("hook entries removed from settings.json")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"WARNING: could not edit {SETTINGS}: {exc}")

    home = config.agent_home()
    print(f"\nUnits stopped and removed. Left in place: {home}")
    print("That directory holds the vendored code, the env file (with the token), and the")
    print("tracked-session state. Remove it yourself once you are sure:")
    print(f"    rm -rf {home}")
    return 0
