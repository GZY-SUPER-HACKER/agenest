"""Codex hook handlers — the same reporting contract, for a different event vocabulary.

Codex's hooks are Claude Code-compatible in *shape*: a short-lived command, one JSON object on
stdin, the event name in `hook_event_name`. They are not the same vocabulary, and the
difference decides what this reporter can honestly say:

- There is no `MessageDisplay` and no `PostToolBatch`. The only assistant prose is
  `stop.last_assistant_message`, which is the answer; a checkpoint built from it would show
  every reply twice (design.md §37).
- `postModelRequest` carries status, error and token usage — not text. It proves a model call
  happened, not what it said.
- `PermissionRequest` is a **request**, not a notification: the hook may return allow/deny.
  This reporter never does. The request is broadcast to every connected client and the first
  answer wins, so a deny here would race the human in the TUI. Returning nothing lets Codex's
  normal approval flow decide.

So Codex is a turn-level reporter: session start/end, the prompt, the reply boundary, and
`awaiting_user` for an approval. The tool events exist and are listed in `IGNORED`; they are
per-call, carry the user's code, and are not progress.

The event names are PascalCase (`SessionStart`, not `sessionStart`), and the payloads come
from `codex-rs/hooks/schema/generated/*.command.input.schema.json`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import runtime, tracked
from . import host as host_report
from .hooks import _first_line, _host_label, _log, _now

#: The dashboard's `MAX_MESSAGE`. A summary line is capped here so a long prompt cannot make
#: the event a 422.
MAX_MESSAGE = 500
#: `MAX_EVENT_KEY` in backend/app/schemas.py.
MAX_KEY = 128


def _entry_for(payload: dict) -> tracked.Tracked | None:
    session_id = payload.get("session_id") or ""
    return tracked.get(session_id) if session_id else None


def _key(*parts: object) -> str:
    return ":".join(str(part) for part in parts if part not in (None, ""))[:MAX_KEY]


def _project_of(cwd: str) -> str:
    return Path(cwd).name if cwd else "codex"


def on_session_start(payload: dict) -> None:
    """Create the session, or reuse it when Codex resumed the same thread.

    `source` is Codex's own word (`startup`, `resume`, `clear`, `compact`, `fork`). A resume is
    the same conversation continuing, so creating a second row would show one conversation as
    two. A new source with a new `session_id` is a new row, which is why the id is the key.
    """
    session_id = payload.get("session_id") or ""
    if not session_id:
        return
    existing = tracked.get(session_id)
    if existing is not None:
        existing.last_seen_at = _now()
        tracked.put(existing)
        runtime.cpctl("event", "--session", existing.csd_session_id,
                      "--type", "heartbeat", "-m", "session resumed")
        _log(f"Codex SessionStart: reusing {existing.csd_session_id} "
             f"({payload.get('source') or 'resume'})")
        return

    cwd = payload.get("cwd") or ""
    project = _project_of(cwd)
    args = [
        "session", "start",
        "--agent", "codex",
        "--title", f"Codex · {project}",
        "--project", project,
    ]
    if cwd:
        args += ["--workspace", cwd]
    if label := _host_label():
        args += ["--host", label]

    result = runtime.cpctl(*args)
    if not result.ok:
        _log(f"Codex SessionStart: cpctl failed: {result.detail}")
        return

    now = _now()
    tracked.put(tracked.Tracked(
        cc_session_id=session_id,
        csd_session_id=result.value,
        # The hook process's ancestor is the Codex process. An unknown pid is an answer; the
        # heartbeat treats it as "cannot judge" rather than closing a live session.
        pid=tracked.find_pid(("codex",)),
        cwd=cwd,
        started_at=now,
        last_seen_at=now,
    ))
    _log(f"Codex SessionStart: created {result.value} (source={payload.get('source')})")
    # Best effort, and never on the hook's critical path: a dashboard that cannot record the
    # host row still has the session itself. The same `(label, agent_type)` rule as §20.
    host_report.report(agent_type="codex")


def on_user_prompt(payload: dict) -> None:
    """A human prompt starts a turn: name it, and report it as progress.

    Codex gives a `turn_id` but no turn number, so the counter lives in `tracked.json` exactly
    as it does for Claude Code. The id is part of the event key: two prompts with the same text
    ("continue") must still be two rows.
    """
    entry = _entry_for(payload)
    if entry is None:
        return
    title = _first_line(payload.get("prompt") or "")
    if not title:
        return
    runtime.cpctl("session", "update", "--session", entry.csd_session_id,
                  "--title", title)
    entry.turn += 1
    tracked.put(entry)
    key = _key("codex-prompt", entry.csd_session_id, payload.get("turn_id"), entry.turn)
    result = runtime.cpctl("event", "--session", entry.csd_session_id,
                           "--type", "progress",
                           "-m", f"Turn {entry.turn}: {title}",
                           "--key", key)
    _log(f"Codex UserPromptSubmit: turn {entry.turn}" if result.ok
         else f"Codex UserPromptSubmit: cpctl failed: {result.detail}")


def _turn_boundary(payload: dict, message: str) -> None:
    entry = _entry_for(payload)
    if entry is None:
        return
    turn = entry.turn or 1
    # `Stop` and `Interrupt` describe the same boundary. One key means whichever arrives first
    # writes the row; the other is deduplicated instead of stacking a second boundary.
    key = _key("codex-turn", entry.csd_session_id,
               payload.get("turn_id") or turn, "boundary")
    result = runtime.cpctl("event", "--session", entry.csd_session_id,
                           "--type", "turn_completed",
                           "-m", message.format(turn=turn),
                           "--key", key)
    _log(f"Codex turn {turn} finished" if result.ok
         else f"Codex {payload.get('hook_event_name')}: cpctl failed: {result.detail}")


def on_stop(payload: dict) -> None:
    """The reply ended. It is a turn boundary, never a session end (design.md §36)."""
    _turn_boundary(payload, "Reply finished (turn {turn})")


def on_interrupt(payload: dict) -> None:
    """The user stopped the turn. That is a reported boundary, not an inferred one."""
    _turn_boundary(payload, "Interrupted (turn {turn})")


def on_permission_request(payload: dict) -> None:
    """Report that Codex is blocked on a human, and return **no decision**.

    The absence of a decision is the whole safety property: Codex's own flow (the TUI, or the
    configured reviewer) still answers the request. A reporter that returned `Deny` here could
    beat the human to the callback — approvals are broadcast, first answer wins.
    """
    entry = _entry_for(payload)
    if entry is None:
        return
    tool = payload.get("tool_name") or "a tool"
    key = _key("codex-approval", entry.csd_session_id,
               payload.get("run_id_suffix") or payload.get("turn_id") or tool)
    result = runtime.cpctl("event", "--session", entry.csd_session_id,
                           "--type", "awaiting_user",
                           "-m", f"approval needed: {tool}",
                           "--key", key)
    _log(f"Codex PermissionRequest: {tool}" if result.ok
         else f"Codex PermissionRequest: cpctl failed: {result.detail}")


def on_session_end(payload: dict) -> None:
    """A clean Codex exit closes the row. A crash fires no hook; the heartbeat owns that."""
    session_id = payload.get("session_id") or ""
    entry = tracked.get(session_id)
    if entry is None:
        return
    result = runtime.cpctl("session", "finish", "--session", entry.csd_session_id,
                           "--status", "completed",
                           "--summary", "Codex session ended")
    if not result.ok:
        # Same rule as Claude Code's SessionEnd: a failed report keeps the entry tracked so
        # the heartbeat can close it later, rather than forgetting the only pointer to a row
        # the server still believes is running (design.md §35).
        _log(f"Codex SessionEnd: cpctl failed: {result.detail} "
             f"({entry.csd_session_id} left tracked)")
        return
    tracked.drop(session_id)
    _log(f"Codex SessionEnd: closed {entry.csd_session_id}")


HANDLERS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt,
    "Stop": on_stop,
    "Interrupt": on_interrupt,
    "PermissionRequest": on_permission_request,
    "SessionEnd": on_session_end,
}

#: Events we knowingly do nothing about, so "we forgot" and "we decided" read differently in
#: the log. The tool and model-request events are the finest Codex offers, but they are
#: machinery, they fire per call, and `tool_input`/`tool_response` carry the user's code.
IGNORED = {
    "PreToolUse": "per-tool volume; the payload is the user's code",
    "PostToolUse": "per-tool volume; the payload is the user's code",
    "PreModelRequest": "a model call happened; it carries no assistant text",
    "PostModelRequest": "status/usage only; no assistant text",
    "PreCompact": "compaction does not change what the user is doing",
    "PostCompact": "compaction does not change what the user is doing",
    "SubagentStart": "a subagent is part of the parent turn",
    "SubagentStop": "a subagent is part of the parent turn",
}


def run(event: str | None = None, raw: str | None = None) -> int:
    """Dispatch one Codex hook invocation. Always returns 0 — see the module docstring."""
    try:
        text = raw if raw is not None else sys.stdin.read()
        payload = json.loads(text) if text.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        _log(f"codex hook: unreadable payload ({exc}); ignored")
        return 0

    name = event or payload.get("hook_event_name") or ""
    handler = HANDLERS.get(name)
    if handler is None:
        if name and name not in IGNORED:
            _log(f"codex hook: {name} has no handler and is not a known-ignored event")
        return 0

    try:
        handler(payload)
    except Exception as exc:  # noqa: BLE001 — a hook must never take Codex down
        _log(f"codex hook {name}: handler raised {type(exc).__name__}: {exc}")
    return 0
