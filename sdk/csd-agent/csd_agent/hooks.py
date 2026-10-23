"""The Claude Code hook dispatcher.

Claude Code fires a hook by running a shell command with the event's payload as JSON on
stdin. This module turns those payloads into `cpctl` calls. It is the only piece of
`csd-agent` that runs inside someone else's critical path, so it obeys two rules absolutely:

1. **It always exits 0.** A dashboard problem must never become a Claude Code problem. Every
   failure path here logs and returns.
2. **It never invents a status.** Claude Code says what happened; we report that. Where the
   two disagree — the case that matters most is `Stop`, below — the design's rule wins
   (docs/design.md §2: status is reported, never inferred).

## Which events are wired, and which are deliberately not

| Event | What we do | Why |
|---|---|---|
| `SessionStart` | create the session (or reuse it, on resume) | the only place a session can begin |
| `UserPromptSubmit` | set the real title | `SessionStart` fires before the user has typed anything, so its title is a placeholder, and the title is what makes the list readable |
| `SessionEnd` | `finish --status completed` | a clean exit; a crash does not fire this at all |
| `Notification` | report an `awaiting_user` event | this is Claude Code saying it wants a human — the single most useful thing on a phone |
| `Stop` | **nothing** | see below |
| `PostToolUse`, `PreToolUse`, `SubagentStop`, `PreCompact` | nothing by default | volume, and the payload implications documented in the README |

### `Notification` reports an event, not a status

The signal is "this session is blocked on you", and the dashboard surfaces it under *needs
attention*. It is an **event** and not a status on purpose. A status is how a session *ended*
— its values are terminal and sticky, and once written they cannot be walked back. "Waiting
for an answer" is the opposite of that: it must vanish the moment the user answers.

As an event it clears itself. The dashboard shows the newest non-heartbeat event on a
session, so the next thing the session reports — the user's prompt, another progress line —
supersedes it with no bookkeeping and no clear-protocol to forget. It also cannot leak into
`status`, so the terminal-state rules in docs/design.md §3 stay untouched.

### `Stop` must not mean "finished"

`Stop` fires every time Claude Code finishes *a reply* — many times per session, the first
of them seconds after the session opens. Wiring it to `session finish` would make every
session terminal after its first answer, and worse than that it would be permanent: the
backend's terminal states are sticky and first-write-wins (design.md §3), so every event
after that would be silently discarded and the dashboard would show a wall of finished
sessions that are all still running. `SessionEnd` is the event that ends a session.

### `SessionEnd` is not guaranteed, which is why the pid exists

Claude Code killed with SIGKILL, or an SSH session that drops and takes the process with
it, never fires `SessionEnd`. The session stays `running` and only ever shows as "maybe
offline" — honest, but useless. So `SessionStart` records the Claude Code pid, and the
heartbeat checks it and reports the truth when the process is gone.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import config, runtime, tracked

#: Long enough for a real task description, short enough not to wrap on a phone.
MAX_TITLE = 68


def _log(message: str) -> None:
    """Append to the agent's log. Best-effort — a log write must not fail a hook."""
    try:
        path = config.state_dir() / "hook.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{_stamp()} {message}\n")
    except OSError:
        pass


def _stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _first_line(text: str, limit: int = MAX_TITLE) -> str:
    """Collapse a user prompt to one presentable line for the session title."""
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if line:
            return line[:limit].rstrip() + ("…" if len(line) > limit else "")
    return ""


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #

def on_session_start(payload: dict) -> None:
    cc_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    if not cc_id:
        _log("SessionStart: no session_id in payload; nothing to report")
        return

    existing = tracked.get(cc_id)
    if existing:
        # `--resume` / `--continue` re-fires SessionStart for a conversation that already
        # has a dashboard session. Creating a second one would split the timeline in two.
        # The pid is refreshed because a resumed session is a new process.
        pid = tracked.find_cc_pid() or existing.pid
        tracked.put(tracked.Tracked(
            cc_session_id=cc_id,
            csd_session_id=existing.csd_session_id,
            pid=pid,
            cwd=cwd,
            started_at=existing.started_at,
            last_seen_at=existing.last_seen_at,
        ))
        runtime.cpctl("event", "--session", existing.csd_session_id, "--type", "heartbeat",
                      "-m", "session resumed")
        _log(f"SessionStart: reusing {existing.csd_session_id} (resume)")
        return

    # A previous Claude Code in this same directory that died without SessionEnd is still
    # sitting at `running`. Close it now, before opening a new one, so the dashboard does
    # not accumulate sessions that look alive and are not. Bounded to this cwd on purpose:
    # another directory's dead session is the heartbeat's business, not this hook's.
    for stale in tracked.for_cwd(cwd):
        if not tracked.process_alive(stale):
            result = runtime.cpctl("session", "finish", "--session", stale.csd_session_id,
                                   "--status", "interrupted",
                                   "--summary", "Claude Code exited without reporting; "
                                                "closed on restart")
            if not result.ok:
                # Same rule as `SessionEnd` below: a failed report keeps the entry tracked so
                # the heartbeat can retry, rather than forgetting the only pointer to a row
                # the server still believes is running.
                _log(f"SessionStart: closing dead {stale.csd_session_id} failed: "
                     f"{result.detail} (left tracked)")
                continue
            tracked.drop(stale.cc_session_id)
            _log(f"SessionStart: closed dead {stale.csd_session_id} (pid {stale.pid} gone)")

    project = Path(cwd).name or cwd
    args = [
        "session", "start",
        "--agent", "claude_code",
        "--title", f"Claude Code · {project}",
        "--project", project,
        "--workspace", cwd,
    ]
    # Attribute the session to this machine, so the rail can nest it under the host it ran on.
    # Omitted rather than sent empty when the config is missing: the API treats a blank host
    # as "no host recorded", and a session reported with no host is grouped apart instead of
    # being placed under a name we made up.
    if label := _host_label():
        args += ["--host", label]

    result = runtime.cpctl(*args)
    if not result.ok:
        _log(f"SessionStart: cpctl failed: {result.detail}")
        return

    pid = tracked.find_cc_pid()
    now = _now()
    tracked.put(tracked.Tracked(
        cc_session_id=cc_id,
        csd_session_id=result.value,
        pid=pid,
        cwd=cwd,
        started_at=now,
        last_seen_at=now,
    ))
    _log(f"SessionStart: created {result.value} (pid {pid or 'unknown'})")


def on_user_prompt(payload: dict) -> None:
    entry = _entry_for(payload)
    if entry is None:
        return
    title = _first_line(payload.get("prompt") or "")
    if not title:
        return
    result = runtime.cpctl("session", "update", "--session", entry.csd_session_id,
                           "--title", title)
    _log(f"UserPromptSubmit: title -> {title!r}" if result.ok
         else f"UserPromptSubmit: cpctl failed: {result.detail}")


def on_notification(payload: dict) -> None:
    """Claude Code wants a human. Report that as its own event type.

    The type matters: the dashboard reads `awaiting_user` off the newest event to put the
    session under *needs attention*. Reporting it as a plain `progress` would file the most
    actionable thing on the board under "work in progress" and bury it.

    Repetition is expected — Claude Code re-notifies while it stays blocked — and is handled
    by `cpctl`'s derived `event_key`: the same message on the same session hashes to the same
    key, so the duplicate is ignored rather than stacking up rows.
    """
    entry = _entry_for(payload)
    if entry is None:
        return
    message = _first_line(payload.get("message") or "", 200) or "Claude Code needs your input"
    runtime.cpctl("event", "--session", entry.csd_session_id,
                  "--type", "awaiting_user", "-m", message)


def on_session_end(payload: dict) -> None:
    cc_id = payload.get("session_id") or ""
    entry = tracked.get(cc_id)
    if entry is None:
        return
    reason = payload.get("reason") or "exit"
    cwd = payload.get("cwd") or entry.cwd
    result = runtime.cpctl("session", "finish", "--session", entry.csd_session_id,
                           "--status", "completed",
                           "--summary", f"Claude Code session ended ({reason}) in {Path(cwd).name}")
    if not result.ok:
        # **Do not drop the entry.** Dropping it is how a failed report becomes permanent:
        # the dashboard keeps the session at `running` and the heartbeat, which is the only
        # thing left that could close it, no longer knows it exists. Leaving it tracked lets
        # the next probe see the process gone and report `interrupted` — the honest ending
        # for a session whose own report never landed (design.md §35).
        _log(f"SessionEnd: cpctl failed: {result.detail} "
             f"({entry.csd_session_id} left tracked for the heartbeat)")
        return
    tracked.drop(cc_id)
    _log(f"SessionEnd: closed {entry.csd_session_id} (reason={reason})")


def _entry_for(payload: dict) -> tracked.Tracked | None:
    cc_id = payload.get("session_id") or ""
    return tracked.get(cc_id) if cc_id else None


def _host_label() -> str:
    """This host's own name for itself, as `install` recorded it.

    A hook runs on Claude Code's critical path and may outlive a half-removed install, so a
    missing config is an answer ("no host") rather than an error. Reporting no host is
    honest; inventing one would put the session under a machine that may not exist.
    """
    try:
        return config.AgentConfig.load().label
    except (FileNotFoundError, ValueError, OSError):
        return ""


def _now() -> float:
    import time
    return time.time()


HANDLERS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt,
    "Notification": on_notification,
    "SessionEnd": on_session_end,
}

#: Events we knowingly do nothing about. Listed so that "we forgot" and "we decided" read
#: differently in a log — the whole point of writing the decision down.
IGNORED = {
    "Stop": "fires per reply, not per session; see the module docstring",
    "PreToolUse": "per-tool volume; enable deliberately if you want it",
    "PostToolUse": "per-tool volume; enable deliberately if you want it",
    "PostToolUseFailure": "per-tool volume; enable deliberately if you want it",
    "SubagentStop": "a subagent finishing is not the session finishing",
    "PreCompact": "compaction does not change what the user is doing",
}


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def run(event: str | None, raw: str | None = None) -> int:
    """Dispatch one hook invocation. Always returns 0 — see the module docstring."""
    try:
        text = raw if raw is not None else sys.stdin.read()
        payload = json.loads(text) if text.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        _log(f"{event}: unreadable payload ({exc}); ignored")
        return 0

    name = event or payload.get("hook_event_name") or ""
    handler = HANDLERS.get(name)
    if handler is None:
        if name and name not in IGNORED:
            _log(f"{name}: no handler and not a known-ignored event")
        return 0

    try:
        handler(payload)
    except Exception as exc:  # noqa: BLE001 — a hook must never take Claude Code down
        _log(f"{name}: handler raised {type(exc).__name__}: {exc}")
    return 0
