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
        token = self.token or config.read_env().get("CSD_INGEST_TOKEN", "")
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
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        cfg = self.cfg
        rendered = {
            "csd-heartbeat.service": HEARTBEAT_SERVICE.format(
                home=config.agent_home(),
                env=config.env_file(),
                pkg=config.agent_home() / "csd-agent",
                python=cfg.python,
                tunnel_deps=LOCAL_DEPS if cfg.local else TUNNEL_DEPS,
            ),
            "csd-heartbeat.timer": HEARTBEAT_TIMER.format(minutes=self.heartbeat_minutes),
        }
        # No tunnel unit for a local install: the API is already on this machine's loopback,
        # so an `ssh -L` to nowhere would be a unit whose only job is to fail and restart.
        if not cfg.local:
            rendered["csd-tunnel.service"] = TUNNEL_UNIT.format(
                home=config.agent_home(),
                ssh=shutil.which("ssh") or "/usr/bin/ssh",
                key=os.path.expanduser(cfg.ssh_key),
                tunnel_port=cfg.tunnel_port,
                cloud_port=cfg.cloud_port,
                cloud_user=cfg.cloud_user,
                cloud_host=cfg.cloud_host,
            )

        for name, text in rendered.items():
            (UNIT_DIR / name).write_text(text, encoding="utf-8")
        self.note(f"units written to {UNIT_DIR} ({', '.join(sorted(rendered))})")

        if cfg.local:
            # A previous non-local install on this machine would have left a tunnel unit
            # behind, still holding a port and still dialling out. Remove it as part of
            # switching, rather than leaving two answers to "how does this host reach the
            # dashboard" on disk at once.
            stale = UNIT_DIR / "csd-tunnel.service"
            if stale.exists():
                self._systemctl("disable", "--now", "csd-tunnel.service")
                try:
                    stale.unlink()
                    self.note("removed the leftover csd-tunnel.service (this install is local)")
                except OSError:
                    pass

        if shutil.which("systemctl"):
            self._systemctl("daemon-reload")
            units = ["csd-heartbeat.timer"] if cfg.local else [
                "csd-tunnel.service", "csd-heartbeat.timer",
            ]
            for unit in units:
                self._systemctl("enable", "--now", unit)
        else:
            self.note("WARNING: no systemctl; units written but not started")

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
                    "command": f"{HOOK_MARKER} {event}",
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

        if self.cfg.local:
            self.note("csd-tunnel.service: not used (--local install)")
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
            code = "000"
            for attempt in range(10):
                probe = subprocess.run(
                    [curl, "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "--noproxy", "*", "--max-time", "5",
                     f"{self.cfg.base_url}/api/sessions"],
                    capture_output=True, text=True,
                )
                code = probe.stdout.strip() or "000"
                if code in ("200", "401"):
                    break
                time.sleep(1.0 if attempt < 3 else 1.5)

            self.note(f"GET {self.cfg.base_url}/api/sessions -> {code}")
            if code not in ("200", "401"):
                ok = False
                if self.cfg.local:
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
        where = "journalctl --user -u csd-heartbeat" if self.cfg.local else (
            "journalctl --user -u csd-tunnel"
        )
        self.note(f"Done. Logs: {config.state_dir()}/hook.log, and {where}")
        return 0 if ok else 1


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


def _is_ours(group: object) -> bool:
    """Is this hook matcher-group one that a previous install wrote?"""
    if not isinstance(group, dict):
        return False
    entries = group.get("hooks")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if isinstance(entry, dict) and HOOK_MARKER in str(entry.get("command", "")):
            return True
    return False


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

    for unit in ("csd-heartbeat.timer", "csd-heartbeat.service", "csd-tunnel.service"):
        subprocess.run(["systemctl", "--user", "disable", "--now", unit],
                       capture_output=True, text=True)

    for name in ("csd-tunnel.service", "csd-heartbeat.service", "csd-heartbeat.timer"):
        try:
            (UNIT_DIR / name).unlink()
        except OSError:
            pass
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)

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
