"""Command line for `csd-agent`.

Four verbs, each a short-lived process. There is no daemon: the only things that stay
running are the two systemd user units the installer writes, and those run `ssh` and a
timer — not code from this package.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import config, dsh, heartbeat, hooks, host, install


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csd-agent",
        description="Put this machine's coding agents on Agenest.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ins = sub.add_parser("install", help="one-time setup on this machine")
    ins.add_argument("--cloud", default=None,
                     help="dashboard host, e.g. dashboard.example.com. Not needed with --local.")
    ins.add_argument("--local", action="store_true",
                     help="this machine runs the dashboard itself, so there is nothing to "
                          "tunnel to: no csd-tunnel.service, and the API is on loopback")
    ins.add_argument("--cloud-user", default="root")
    ins.add_argument("--cloud-ssh-port", type=int, default=22)
    ins.add_argument("--cloud-port", type=int, default=8780,
                     help="the dashboard's port on the cloud (default 8780)")
    ins.add_argument("--tunnel-port", type=int, default=18781,
                     help="local port the tunnel listens on (default 18781)")
    ins.add_argument("--python", default="python3",
                     help="interpreter for cpctl and csd-agent (default python3)")
    ins.add_argument("--ssh-key", default="~/.ssh/id_ed25519",
                     help="key used for the tunnel and to fetch the token")
    ins.add_argument("--label", default=None, help="name for this host (default: hostname)")
    ins.add_argument("--no-drive", action="store_true",
                     help="do not ask the dashboard's personal agent to be able to drive this "
                          "machine. Without this the host declares itself driveable, which "
                          "only means the user is offered a decision — nothing is reachable "
                          "until they approve it in the dashboard")
    ins.add_argument("--ssh-host", default=None,
                     help="the address the dashboard should reach this machine at (default: "
                          "its fqdn or a Tailscale IP when one is available). A declaration; "
                          "the user confirms it when approving")
    ins.add_argument("--ssh-user", default=None,
                     help="the account a key would be installed for (default: the user running "
                          "this installer). Only this account can be set up automatically — "
                          "the connector writes its own ~/.ssh/authorized_keys")
    ins.add_argument("--ssh-port", type=int, default=22, help="ssh port (default 22)")
    ins.add_argument("--heartbeat-minutes", type=int, default=5)
    ins.add_argument("--token", default=None,
                     help="ingest token; omit to read it from the cloud over SSH")
    ins.add_argument("--source", default=None,
                     help="path to the sdk/ directory holding cpctl/ and csd-agent/")
    ins.add_argument("--dry-run", action="store_true",
                     help="print what would be done, change nothing")
    ins.add_argument("--no-dsh", action="store_true",
                     help="do not wire DeepSeek Harness even when it is installed")
    ins.add_argument("--dsh-profile", action="append", dest="dsh_profiles",
                     help="DSH profile to wire (repeatable; default: every profile that exists)")
    ins.add_argument("--dsh-home", default=None,
                     help="DSH home (default: $DSH_HOME or ~/.dsh)")

    sub.add_parser("uninstall", help="stop the units and remove the hook entries")
    sub.add_parser("status", help="what this host believes, and whether it is connected")

    hb = sub.add_parser("heartbeat", help="probe tracked sessions and report liveness")
    hb.add_argument("-v", "--verbose", action="store_true")

    hk = sub.add_parser("hook", help="Claude Code hook entry point (reads JSON on stdin)")
    hk.add_argument("event", nargs="?", help="hook event name")
    hk.add_argument("--payload", help="payload as a string instead of on stdin (for testing)")

    # DSH is wired by `install` too. This verb exists for a host that runs DSH but not Claude
    # Code, and for re-wiring a single profile without touching anything else.
    dsh_cmd = sub.add_parser("dsh", help="DeepSeek Harness reporter only")
    dsh_sub = dsh_cmd.add_subparsers(dest="dsh_action", required=True)
    dsh_ins = dsh_sub.add_parser("install", help="install the reporter into DSH profiles")
    dsh_ins.add_argument("--profile", action="append", dest="dsh_profiles",
                         help="profile to wire (repeatable; default: every existing profile)")
    dsh_ins.add_argument("--source", default=None,
                         help="path to the sdk/ directory holding dsh-csd-plugin/")
    dsh_ins.add_argument("--home", default=None, help="DSH home (default: $DSH_HOME or ~/.dsh)")
    dsh_status = dsh_sub.add_parser("status", help="which profiles carry the reporter")
    dsh_status.add_argument("--home", default=None)
    dsh_un = dsh_sub.add_parser("uninstall", help="remove the reporter from DSH profiles")
    dsh_un.add_argument("--home", default=None)

    return parser


def _cmd_install(args: argparse.Namespace) -> int:
    if args.dry_run:
        print("dry run — nothing will be written")
        print(json.dumps(vars(args), indent=2, ensure_ascii=False))
        return 0

    if not args.local and not args.cloud:
        print(
            "csd-agent: say which dashboard this host reports to —\n"
            "  --cloud <host>   a dashboard on another machine (this host tunnels to it)\n"
            "  --local          the dashboard runs ON this machine (nothing to tunnel to)",
            file=sys.stderr,
        )
        return 2

    cfg = config.AgentConfig(
        # Unused when local, and never read then: `base_url` picks the dashboard's own port.
        # Left non-empty so a config file written by an older version stays loadable.
        cloud_host=args.cloud or "127.0.0.1",
        cloud_user=args.cloud_user,
        cloud_ssh_port=args.cloud_ssh_port,
        cloud_port=args.cloud_port,
        tunnel_port=args.tunnel_port,
        python=args.python,
        ssh_key=args.ssh_key,
        label=args.label or "",
        local=args.local,
    )
    if not cfg.label:
        import socket
        cfg.label = socket.gethostname()

    # What this host declares about being driven (design.md §24). Declared here and re-sent on
    # every heartbeat, so a machine that moves (a new Tailscale address) says so instead of
    # leaving the dashboard aiming an approved command at where it used to be.
    cfg.wants_access = not args.no_drive
    cfg.ssh_host = (args.ssh_host or host.default_ssh_host()).strip()
    cfg.ssh_user = (args.ssh_user or host.current_user()).strip()
    cfg.ssh_port = args.ssh_port

    source = Path(args.source).resolve() if args.source else None
    installer = install.Installer(
        cfg,
        heartbeat_minutes=args.heartbeat_minutes,
        token=args.token,
        pkg_source=source,
        no_dsh=args.no_dsh,
        dsh_profiles=args.dsh_profiles,
        dsh_home=args.dsh_home,
    )
    return installer.run()


def _cmd_status(_args: argparse.Namespace) -> int:
    try:
        cfg = config.AgentConfig.load()
    except FileNotFoundError as exc:
        print(f"not installed: {exc}")
        return 1

    print(f"agent home : {config.agent_home()}")
    print(f"label      : {cfg.label}")
    if cfg.local:
        print(f"dashboard  : this machine (local install, {cfg.base_url})")
    else:
        print(f"cloud      : {cfg.cloud_user}@{cfg.cloud_host}:{cfg.cloud_ssh_port}"
              f" (dashboard on {cfg.cloud_port})")
        print(f"tunnel     : 127.0.0.1:{cfg.tunnel_port} -> dashboard")
    print(f"python     : {cfg.python}")
    # What the dashboard was last told about this host. "never" is worth printing plainly:
    # it is the difference between "the dashboard knows about this machine" and "it does not",
    # which is the question this whole package exists to make answerable without SSH.
    print(f"host       : {host.AGENT_TYPE} on {cfg.label}"
          f" — last reported {_stamp(host.last_report_at())}")
    if cfg.wants_access:
        # Printed with the address, because "why can't the agent reach me?" is almost always
        # answered here: the dashboard shows the user whatever was declared, and this is it.
        print(f"drive      : declared — the dashboard may offer this as {cfg.ssh_user}"
              f"@{cfg.ssh_host}:{cfg.ssh_port}. Nothing is reachable until the user approves it")
    else:
        print("drive      : not declared (--no-drive)")

    values = config.read_env()
    print(f"env file   : {'present' if config.env_file().exists() else 'MISSING'}"
          f", token {'set' if values.get('CSD_INGEST_TOKEN') else 'MISSING'}")

    dsh_home = Path(cfg.dsh_home).expanduser() if cfg.dsh_home else dsh.dsh_home()
    dsh_found = dsh.profiles(dsh_home)
    if dsh_found:
        wired = [name for name in dsh_found if dsh.profile_has_plugin(dsh_home, name)]
        print(f"dsh        : {dsh_home} — "
              f"{', '.join(wired) if wired else 'no profile wired'}")
    else:
        print(f"dsh        : not detected at {dsh_home}")

    if shutil.which("systemctl"):
        units = ["csd-heartbeat.timer"] if cfg.local else [
            "csd-tunnel.service", "csd-heartbeat.timer",
        ]
        for unit in units:
            proc = subprocess.run(["systemctl", "--user", "is-active", unit],
                                  capture_output=True, text=True)
            print(f"{unit:<24}: {proc.stdout.strip()}")

    entries = tracked_entries()
    print(f"tracked    : {len(entries)} session(s)")
    for entry in entries.values():
        alive = "alive" if _alive(entry) else "GONE "
        print(f"  {alive}  csd={entry.csd_session_id}  cc={entry.cc_session_id[:8]}"
              f"  pid={entry.pid}  {entry.cwd}")

    if shutil.which("curl"):
        proc = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--noproxy", "*",
             "--max-time", "8", f"{cfg.base_url}/api/sessions"],
            capture_output=True, text=True,
        )
        print(f"reachable  : HTTP {proc.stdout.strip()} from {cfg.base_url}/api/sessions")
    return 0


def tracked_entries():
    from . import tracked
    return tracked.load()


def _stamp(epoch: float) -> str:
    """A wall-clock time for a stored epoch. "never" when the host has not reported."""
    if not epoch:
        return "never"
    from datetime import datetime
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _alive(entry) -> bool:
    from . import tracked
    return tracked.process_alive(entry)


def _cmd_dsh(args: argparse.Namespace) -> int:
    """The standalone DSH path, for a host that runs no Claude Code.

    `csd-agent install` calls the same module; this verb is not a second implementation. It
    records what it wired in `agent.json` when one exists, so the ordinary `uninstall` — and
    this verb's own `uninstall` — can reverse exactly that.
    """
    home = Path(args.home).expanduser() if getattr(args, "home", None) else None

    if args.dsh_action == "status":
        return dsh.status(home)

    if args.dsh_action == "install":
        source = Path(args.source).resolve() if args.source else None
        result = dsh.install(
            profiles_requested=args.dsh_profiles,
            pkg_source=source,
            home=home,
        )
        for line in result.notes:
            print(f"  {line}")
        if result.skipped:
            return 0
        if result.installed:
            try:
                cfg = config.AgentConfig.load()
            except FileNotFoundError:
                cfg = None
            if cfg is not None:
                cfg.dsh_home = result.home
                cfg.dsh_profiles = result.installed
                cfg.save()
            if not config.env_file().exists():
                print(f"  note: {config.env_file()} is missing; the reporter reads")
                print("        CSD_BASE_URL / CSD_INGEST_TOKEN from the environment until")
                print("        `csd-agent install` writes one.")
        return 0 if result.ok else 1

    if args.dsh_action == "uninstall":
        try:
            cfg = config.AgentConfig.load()
        except FileNotFoundError:
            cfg = None
        # With an explicit --home, discover inside that home rather than trusting a recorded
        # profile list that belongs to a different one (the tests do exactly this).
        recorded = None if home is not None else (cfg.dsh_profiles if cfg else None)
        target = home or (
            Path(cfg.dsh_home).expanduser() if cfg and cfg.dsh_home else None
        )
        return dsh.uninstall(
            profiles_requested=recorded or None,
            home=target,
            label=cfg.label if cfg else None,
        )

    print(f"unsupported dsh action: {args.dsh_action}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "install":
        return _cmd_install(args)
    if args.command == "uninstall":
        return install.uninstall()
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "heartbeat":
        return heartbeat.run(verbose=args.verbose)
    if args.command == "hook":
        # hooks.run never raises and always returns 0, by design.
        return hooks.run(args.event, args.payload)
    if args.command == "dsh":
        return _cmd_dsh(args)

    print(f"unsupported command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
