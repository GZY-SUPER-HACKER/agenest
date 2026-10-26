"""`cpctl` — the reporting CLI/SDK.

Design constraint that drives everything: **reporting must never block or fail the work it
reports on.** A coding agent's job is to write code, not to keep a dashboard up to date
(spec 8, item 10). Every command therefore:

- has a short default timeout (spec 8.1),
- retries with exponential backoff a bounded number of times (spec 8.2),
- exits 0 on success and non-zero *only* on a genuine write failure, so a shell caller can
  distinguish "reported" from "not reported" — and deliberately does not abort the caller.

Idempotency (spec 8.3): every event carries an `event_key` derived deterministically from
(session id, event type, a caller-supplied discriminator). Retrying is therefore safe, and a
client that dies mid-request does not create a duplicate row.

Local state: the session id is written to `$CSD_STATE_DIR/<slug>.id` so a launcher script can
do `cpctl event --session "$(cat .cpctl-id)" progress "..."` without threading the id
through every call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROG = "cpctl"

#: Mirrors `backend/app/schemas.py`'s `EventType`. The CLI is a separate, zero-dependency
#: package and cannot import the server, so `test_cpctl_offers_every_event_type_the_server_accepts`
#: pins the two lists together: a server event the CLI cannot send is a feature nobody can use.
EVENT_TYPES = (
    "session_started",
    "status_changed",
    "progress",
    "note",
    "awaiting_user",
    "turn_completed",
    "checkpoint",
    "task_failed",
    "task_completed",
    "session_completed",
    "session_failed",
    "session_interrupted",
    "heartbeat",
)

#: Exit code for "the report did not reach the server". Deliberately non-zero but distinct
#: from usage errors (2) so a wrapper script can log it without masking a real CLI mistake.
EXIT_REPORT_FAILED = 3

DEFAULT_BASE_URL = os.environ.get("CSD_BASE_URL", "http://127.0.0.1:8780")
DEFAULT_TIMEOUT = float(os.environ.get("CSD_TIMEOUT", "3.0"))
DEFAULT_RETRIES = int(os.environ.get("CSD_RETRIES", "3"))
DEFAULT_TOKEN = os.environ.get("CSD_INGEST_TOKEN", "")


class ReportError(RuntimeError):
    """The event could not be delivered after all retries."""


@dataclass
class Client:
    base_url: str = DEFAULT_BASE_URL
    token: str = DEFAULT_TOKEN
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    state_dir: Path = Path(os.environ.get("CSD_STATE_DIR", ".cpctl"))

    # -------------------------------------------------------------- #
    # transport
    # -------------------------------------------------------------- #
    def _request(self, method: str, path: str, body: dict | None) -> dict:
        if not self.token:
            raise ReportError(
                "no ingest token: set CSD_INGEST_TOKEN or pass --token"
            )
        payload = (
            json.dumps(body).encode("utf-8") if body is not None else None
        )
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                self.base_url.rstrip("/") + path,
                data=payload,
                method=method,
                headers={
                    "Content-Type": "application/json",
                    "X-CPD-Token": self.token,
                    "User-Agent": f"{PROG}/0.1",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 4xx is the server's answer, not a transient failure: retrying a malformed
                # request just burns time. The one exception is 401, which is worth
                # surfacing loudly because it usually means a stale token.
                if 400 <= exc.code < 500 and exc.code != 408:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                    raise ReportError(f"{method} {path} -> {exc.code}: {detail}")
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc

            if attempt < self.retries:
                # Exponential backoff with jitter, capped, so a burst of retrying agents
                # does not synchronise into a thundering herd.
                time.sleep(min(0.5 * (2**attempt) + random.uniform(0, 0.25), 8.0))

        raise ReportError(
            f"{method} {path} failed after {self.retries + 1} attempts: {last_error}"
        )

    # -------------------------------------------------------------- #
    # commands
    # -------------------------------------------------------------- #
    def start(self, args: argparse.Namespace) -> int:
        body = {
            "agent_type": args.agent,
            "title": args.title,
            "project_name": args.project,
            "workspace": args.workspace,
            "host": args.host,
            "status": args.status,
        }
        if args.started_at:
            body["started_at"] = args.started_at

        session = self._request("POST", "/api/sessions", body)
        self._save_id(session["id"])
        print(session["id"])
        if args.echo_context:
            # Machine-readable echo for a launcher that wants to capture the id from stdout
            # without parsing it back out of the id-only line.
            print(json.dumps({"session_id": session["id"], "status": session["status"]}),
                  file=sys.stderr)
        return 0

    def event(self, args: argparse.Namespace) -> int:
        session_id = self._resolve_session(args.session)
        body = {
            "event_type": args.type,
            "message": args.message,
            "occurred_at": args.at,
            "event_key": args.key or self._auto_key(session_id, args.type, args.message),
        }
        if args.payload:
            body["payload"] = _parse_payload(args.payload)

        result = self._request("POST", f"/api/sessions/{session_id}/events", body)
        print(result["event"]["id"])
        return 0

    def task(self, args: argparse.Namespace) -> int:
        """Report the task verdict, not the run's lifecycle.

        `task done` / `task failed` is the one thing an agent can say that its runtime
        cannot: the run may have ended cleanly and the work still did not pass. It moves no
        status — the server treats both as display events — so it is safe on a session that
        is still running and on one that has already closed (docs/design.md §40).
        """
        session_id = self._resolve_session(args.session)
        event_type = "task_completed" if args.verdict == "done" else "task_failed"
        payload = _parse_payload(args.payload) if args.payload else {}
        payload.setdefault("source", args.source)
        body = {
            "event_type": event_type,
            "message": args.message,
            "event_key": args.key or self._auto_key(session_id, event_type, args.message),
            "payload": payload,
        }
        result = self._request("POST", f"/api/sessions/{session_id}/events", body)
        print(result["event"]["id"])
        return 0

    def finish(self, args: argparse.Namespace) -> int:
        session_id = self._resolve_session(args.session)
        body = {"status": args.status}
        if args.summary:
            body["summary"] = args.summary
        if args.exit_code is not None:
            body["exit_code"] = args.exit_code

        self._request("PATCH", f"/api/sessions/{session_id}", body)
        self._clear_id(session_id)
        return 0

    def update(self, args: argparse.Namespace) -> int:
        """Change a session's descriptive fields without touching its status.

        Separate from `finish` because only one of the two ends a session, and conflating
        them is how a reporter accidentally closes work that is still running.

        The caller that needs this is the Claude Code adapter: `SessionStart` fires before
        the user has typed anything, so the title it can report is a placeholder. The title
        is the single field that decides whether the dashboard is readable, and it is not
        knowable until the user's first prompt arrives.
        """
        session_id = self._resolve_session(args.session)
        body: dict[str, object] = {}
        if args.title:
            body["title"] = args.title
        if args.project:
            body["project_name"] = args.project
        if args.summary:
            body["summary"] = args.summary
        if not body:
            raise ReportError("nothing to update: pass --title, --project or --summary")

        self._request("PATCH", f"/api/sessions/{session_id}", body)
        return 0

    def show(self, args: argparse.Namespace) -> int:
        session_id = self._resolve_session(args.session)
        if session_id:
            print(json.dumps(self._request("GET", f"/api/sessions/{session_id}", None), indent=2))
            return 0

        query = []
        if args.status:
            query.append(f"status={args.status}")
        if args.limit != 100:
            query.append(f"limit={args.limit}")
        path = "/api/sessions" + (f"?{'&'.join(query)}" if query else "")
        listing = self._request("GET", path, None)
        for session in listing["sessions"]:
            print(_format_line(session))
        return 0

    # -------------------------------------------------------------- #
    # hosts
    # -------------------------------------------------------------- #
    def host_report(self, args: argparse.Namespace) -> int:
        """Tell the dashboard this connector is here, or that it is going away.

        Idempotent on the server side, keyed on `(label, agent_type)`, so a connector that
        calls this every few minutes still yields exactly one row. This is deliberately a
        *report* and not a registration protocol: there is no handshake, no id to keep, and
        nothing to unregister if the host never comes back.

        The declaration fields (`--wants-access` and the address) are what makes a host
        driveable: the dashboard shows them to the user, who decides (design.md §24). Saying
        "I can be driven" grants nothing on its own.
        """
        body: dict[str, object] = {
            "label": args.label,
            "agent_type": args.agent_type,
            "connected": not args.disconnected,
            "wants_access": args.wants_access,
        }
        if args.ssh_host:
            body["ssh_host"] = args.ssh_host
        if args.ssh_user:
            body["ssh_user"] = args.ssh_user
        if args.ssh_port:
            body["ssh_port"] = args.ssh_port
        result = self._request("POST", "/api/hosts", body)
        print(result["id"])
        return 0

    def host_list(self, args: argparse.Namespace) -> int:
        listing = self._request("GET", "/api/hosts", None)
        for host in listing["hosts"][: args.limit]:
            print(_format_host_line(host))
        return 0

    def host_access(self, args: argparse.Namespace) -> int:
        """Ask what this connector is supposed to do about its key.

        JSON on stdout, because the caller is `csd-agent`'s heartbeat and not a person: it has
        to decide between "install this key", "remove this line" and "do nothing", and parsing
        prose to decide that is how a feature works until someone rewords a sentence.
        """
        query = urllib.parse.urlencode(
            {"label": args.label, "agent_type": args.agent_type}
        )
        print(json.dumps(self._request("GET", f"/api/hosts/access?{query}", None)))
        return 0

    def host_access_report(self, args: argparse.Namespace) -> int:
        """Say whether the key went into this machine's `authorized_keys`.

        A failure is reported, not raised: the connector retries on its next heartbeat, and the
        dashboard shows the reason until it succeeds. Same rule as every other report (design.md
        §6) — reporting must never fail the work.
        """
        body: dict[str, object] = {
            "label": args.label,
            "agent_type": args.agent_type,
            "installed": not args.error,
        }
        if args.error:
            body["error"] = args.error
        print(json.dumps(self._request("POST", "/api/hosts/access/installed", body)))
        return 0

    # -------------------------------------------------------------- #
    # id bookkeeping
    # -------------------------------------------------------------- #
    def _slug(self) -> str:
        """A stable per-project key, so two projects on one machine don't collide."""
        root = Path.cwd().as_posix()
        digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:10]
        return f"{Path(root).name or 'root'}-{digest}"

    def _save_id(self, session_id: str) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / f"{self._slug()}.id").write_text(session_id, encoding="utf-8")
        except OSError:
            # Losing the cached id is survivable — the caller can still pass --session.
            pass

    def _load_id(self) -> str | None:
        try:
            path = self.state_dir / f"{self._slug()}.id"
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _clear_id(self, session_id: str) -> None:
        try:
            (self.state_dir / f"{self._slug()}.id").unlink(missing_ok=True)
        except OSError:
            pass

    def _resolve_session(self, explicit: str | None) -> str:
        if explicit:
            return explicit
        # A launcher that owns the run (run-agent.sh) exports this, so the agent can report a
        # verdict from its own shell without being told an id it cannot know.
        from_env = os.environ.get("CSD_SESSION_ID", "").strip()
        if from_env:
            return from_env
        cached = self._load_id()
        if cached:
            return cached
        # A connector created the session in a process the agent cannot see. It leaves the id
        # in a small per-writer map keyed by working directory; see csd_agent.tracked and the
        # Pi/DSH/nanobot reporters.
        mapped = _session_from_connector_map(Path.cwd())
        if mapped:
            return mapped
        raise ReportError(
            "no session id given and none cached. Run `cpctl session start` first, pass "
            "`--session <id>`, or let a connector (csd-agent / Pi / DSH) report the run."
        )

    @staticmethod
    def _auto_key(session_id: str, event_type: str, message: str) -> str:
        """Deterministic idempotency key when the caller does not supply one.

        Same session + same type + same message ⇒ same key, so a retried command is a no-op
        rather than a duplicate event.
        """
        digest = hashlib.sha1(
            f"{session_id}|{event_type}|{message}".encode("utf-8")
        ).hexdigest()[:16]
        return f"auto-{digest}"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _cwd_key(value: object) -> str:
    """One spelling per directory, across the Python and Node connectors.

    The agent's shell may hand us a Git-Bash path, the connector stored a native one, and
    Windows does not care about case. `abspath` + `normcase` is the smallest rule that makes
    `C:\\Repo`, `C:/Repo` and `c:\\repo` the same key on Windows, and is the identity on
    POSIX.
    """
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(value))))


def _session_from_connector_map(cwd) -> str | None:
    """Resolve the session a connector opened in this directory.

    Connectors run in a process the agent cannot see, so they cannot put an id in its
    environment. Each writer leaves its own file under `~/.csd/state/sessions.d/` instead —
    no shared file, no cross-writer lock. The newest live entry for this directory wins; a
    writer removes its entry when the run ends, so a stale one only survives a crash.
    """
    home = Path(os.environ.get("CSD_AGENT_HOME") or (Path.home() / ".csd"))
    directory = home / "state" / "sessions.d"
    try:
        paths = list(directory.glob("*.json"))
    except OSError:
        return None

    target = _cwd_key(cwd)
    best: tuple[float, str] | None = None
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        by_cwd = data.get("by_cwd") if isinstance(data, dict) else None
        if not isinstance(by_cwd, dict):
            continue
        for raw_cwd, entries in by_cwd.items():
            if not isinstance(raw_cwd, str) or _cwd_key(raw_cwd) != target:
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                session_id = entry.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    continue
                started = entry.get("started_at")
                score = float(started) if isinstance(started, (int, float)) else 0.0
                if best is None or score > best[0]:
                    best = (score, session_id)
    return best[1] if best else None


def _parse_payload(raw: str) -> dict:
    """`key=value,key=value` or a JSON object. Keeps shell quoting simple for the common
    `stage=testing` case without giving up structure."""
    text = raw.strip()
    if text.startswith("{"):
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise SystemExit(f"{PROG}: payload JSON must be an object")
        return parsed

    payload: dict[str, object] = {}
    for pair in text.split(","):
        if not pair.strip():
            continue
        if "=" not in pair:
            raise SystemExit(f"{PROG}: payload entry {pair!r} is not key=value")
        key, value = pair.split("=", 1)
        key, value = key.strip(), value.strip()
        payload[key] = _coerce(value)
    return payload


def _coerce(value: str):
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _format_host_line(host: dict) -> str:
    """One line per host. `?` is the same "we cannot ask it" mark the session list uses."""
    if host.get("disconnected_at"):
        state = "disconnected"
    elif host.get("possibly_stale"):
        state = "maybe offline"
    else:
        state = "connected"
    # The access state, when the host asked to be driven: the one thing about a host a person
    # reads this list to find out — `cpctl host list` is the non-browser view of the registry.
    access = host.get("access_state")
    suffix = f"  ssh: {access}" if access else ""
    return (
        f"{'●' if state == 'connected' else '○'} {state:<14} "
        f"{host['label']:<24} {host['agent_type']:<12} {host['last_seen_at']}{suffix}"
    )


def _format_line(session: dict) -> str:
    status = session["status"]
    marker = {"running": "▶", "completed": "✓", "failed": "✗", "interrupted": "■"}.get(
        status, "?"
    )
    stale = " [maybe offline]" if session.get("possibly_stale") else ""
    return (
        f"{marker} {status:<11} {session['agent_type']:<12} "
        f"{session['title'][:48]:<48} {session['updated_at']}{stale}"
    )


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Report coding-agent sessions to Agenest.",
        epilog=(
            "Reporting failures never abort the caller's work: a command that cannot reach "
            "the server exits 3 and leaves the agent running."
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    parser.add_argument("--token", default=DEFAULT_TOKEN, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=argparse.SUPPRESS)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)

    session = sub.add_parser("session", help="manage sessions")
    session_cmd = session.add_subparsers(dest="action", required=True)

    start = session_cmd.add_parser("start", help="create a session and print its id")
    start.add_argument("--agent", required=True, help="agent type, e.g. codex or claude_code")
    start.add_argument("--title", required=True, help="human-readable task name")
    start.add_argument("--project", dest="project", help="project name")
    start.add_argument("--workspace", help="working directory on this machine")
    start.add_argument(
        "--host",
        help="which machine this ran on, as the host names itself (csd-agent passes its "
             "label). Omit and the session is grouped apart from the named hosts rather "
             "than being attributed to a guess.",
    )
    start.add_argument(
        "--status",
        default="running",
        choices=["running", "completed", "failed", "interrupted", "unknown"],
        help="initial status (default: running)",
    )
    start.add_argument("--started-at", help="ISO-8601 start time, if reporting late")
    start.add_argument(
        "--json",
        dest="echo_context",
        action="store_true",
        help='also print {"session_id": ...} to stderr',
    )

    for name, helptext in (
        ("finish", "close a session with its final status"),
    ):
        finish = session_cmd.add_parser(name, help=helptext)
        finish.add_argument(
            "--session", help="session id (default: the cached id for this directory)"
        )
        finish.add_argument(
            "--status",
            default="completed",
            choices=["completed", "failed", "interrupted"],
        )
        finish.add_argument("--summary", help="final result summary")
        finish.add_argument("--exit-code", type=int, help="process exit code, if known")

    update = session_cmd.add_parser(name="update", help="change title / project / summary")
    update.add_argument(
        "--session", help="session id (default: the cached id for this directory)"
    )
    update.add_argument("--title", help="replace the session title")
    update.add_argument("--project", help="replace the project name")
    update.add_argument("--summary", help="replace the summary")

    event = sub.add_parser("event", help="report one event on a session")
    event.add_argument("--session", help="session id (default: the cached id)")
    event.add_argument(
        "--type",
        dest="type",
        default="progress",
        choices=list(EVENT_TYPES),
    )
    event.add_argument("--message", "-m", required=True, help="short human-readable message")
    event.add_argument("--key", help="idempotency key (default: derived from the message)")
    event.add_argument("--at", help="ISO-8601 time the event actually happened")
    event.add_argument("--payload", help='extra data: "stage=testing" or {"stage":"testing"}')

    # The verdict is not a status: `task done` / `task failed` says whether the *work*
    # succeeded, while `session finish` says how the *run* ended. An agent that exits 0 can
    # still fail the task, and the runtime has no way to say so (docs/design.md §40).
    task = sub.add_parser(
        "task", help="say whether the task succeeded, not how the run ended"
    )
    task_cmd = task.add_subparsers(dest="verdict", required=True)
    for name, helptext in (
        ("done", "the task the user asked for succeeded"),
        ("failed", "the task failed, or could not be completed"),
    ):
        verdict = task_cmd.add_parser(name, help=helptext)
        verdict.add_argument(
            "--session",
            help="session id (default: $CSD_SESSION_ID, the cached id, or the connector map)",
        )
        verdict.add_argument(
            "--message",
            "-m",
            required=True,
            help="one line: what you verified, or what is wrong / left",
        )
        verdict.add_argument(
            "--source",
            default="agent",
            help="who is making the claim: agent (default), verifier, orchestrator",
        )
        verdict.add_argument(
            "--payload", help='extra data: "tests=42" or {"tests":42}'
        )
        verdict.add_argument(
            "--key", help="idempotency key (default: derived from the message)"
        )

    sub.add_parser("status", help="show the cached session id for this directory")

    # A host is not a session, so it gets its own verb rather than a flag on `session`.
    # `report` is what a connector calls on every heartbeat and on uninstall; `list` is what
    # lets a human ask the dashboard what it thinks is connected, without SSH.
    host = sub.add_parser("host", help="the machines that run coding agents")
    host_cmd = host.add_subparsers(dest="action", required=True)

    host_report = host_cmd.add_parser(
        "report", help="say this connector is here, or that it is leaving"
    )
    host_report.add_argument(
        "--label", required=True,
        help="the machine's own name for itself (csd-agent uses its --label, default: hostname)",
    )
    host_report.add_argument(
        "--agent-type", required=True,
        help="the coding agent this connector speaks for, e.g. claude_code",
    )
    host_report.add_argument(
        "--disconnected", action="store_true",
        help="report a disconnect instead of a connection. Reported, never inferred: a host "
             "that simply stops talking stays connected and goes stale instead.",
    )
    host_report.add_argument(
        "--wants-access", action="store_true",
        help="declare that this machine may be driven by the dashboard's personal agent. A "
             "declaration, not a grant: the user still has to authorize it, and nothing is "
             "reachable until they do.",
    )
    host_report.add_argument(
        "--ssh-host", default=None,
        help="the address this connector believes reaches it (a fqdn, a LAN address, a "
             "Tailscale IP). Unverified by the dashboard — the user confirms it when approving.",
    )
    host_report.add_argument(
        "--ssh-user", default=None,
        help="the account a key would be installed for; the user this connector runs as.",
    )
    host_report.add_argument(
        "--ssh-port", type=int, default=None, help="ssh port (default 22)",
    )

    host_list = host_cmd.add_parser("list", help="print connected hosts as JSON")
    host_list.add_argument("--limit", type=int, default=100, help=argparse.SUPPRESS)

    host_access = host_cmd.add_parser(
        "access", help="ask what this host's access state is, and whether a key is waiting"
    )
    host_access.add_argument("--label", required=True)
    host_access.add_argument("--agent-type", required=True)

    host_access_report = host_cmd.add_parser(
        "access-report", help="say whether the dashboard's key is now in authorized_keys"
    )
    host_access_report.add_argument("--label", required=True)
    host_access_report.add_argument("--agent-type", required=True)
    host_access_report.add_argument(
        "--error", default=None,
        help="why the key could not be installed. Omitting it reports success.",
    )

    sub.add_parser("list", help="list recent sessions").add_argument(
        "--status", choices=["running", "completed", "failed", "interrupted", "unknown"]
    )
    sub.choices["list"].add_argument("--limit", type=int, default=100)

    show = sub.add_parser("show", help="print one session's detail as JSON")
    show.add_argument("--session", help="session id (default: the cached id)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = Client(
        base_url=args.base_url,
        token=args.token,
        timeout=args.timeout,
        retries=args.retries,
    )

    try:
        if args.command == "session":
            if args.action == "start":
                return client.start(args)
            if args.action == "finish":
                return client.finish(args)
            if args.action == "update":
                return client.update(args)
        if args.command == "event":
            return client.event(args)
        if args.command == "task":
            return client.task(args)
        if args.command == "host":
            if args.action == "report":
                return client.host_report(args)
            if args.action == "list":
                return client.host_list(args)
            if args.action == "access":
                return client.host_access(args)
            if args.action == "access-report":
                return client.host_access_report(args)
        if args.command == "status":
            cached = client._load_id()
            print(cached or "(no cached session in this directory)")
            return 0
        if args.command == "list":
            return client.show(args)
        if args.command == "show":
            return client.show(args)
    except ReportError as exc:
        # A reporting failure is a report about the report, not a failure of the work. It is
        # printed to stderr and does not raise, so `set -e` scripts that wrap the agent keep
        # running (spec 8, item 10).
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EXIT_REPORT_FAILED
    except KeyboardInterrupt:
        return 130

    parser.error(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
