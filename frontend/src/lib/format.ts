/**
 * Presentation helpers: status → visual, and time formatting.
 *
 * The colour rule lives here and nowhere else: **only "alive" and "broken" get a bright
 * colour.** Everything finished recedes. A wall of green checkmarks is noise, and the one
 * thing you open this dashboard to answer is "what is running right now".
 */

import type { EventType, Session, SessionStatus } from "../types/api";

/** Kinds used for the timeline dot; collapsed from the nine event types. */
export type EventKind =
  | "started"
  | "completed"
  | "failed"
  | "interrupted"
  | "awaiting"
  | "turn"
  | "checkpoint"
  | "progress"
  | "note";

export function eventKind(type: EventType): EventKind {
  switch (type) {
    case "session_started":
      return "started";
    case "session_completed":
      return "completed";
    case "session_failed":
      return "failed";
    case "session_interrupted":
      return "interrupted";
    case "awaiting_user":
      return "awaiting";
    case "turn_completed":
      return "turn";
    case "checkpoint":
      return "checkpoint";
    case "task_failed":
      // Same red as a failed session in the timeline: it is an outcome, not punctuation.
      return "failed";
    case "task_completed":
      return "completed";
    default:
      return "progress";
  }
}

export const STATUS_LABEL: Record<SessionStatus, string> = {
  running: "running",
  completed: "completed",
  failed: "failed",
  interrupted: "interrupted",
  unknown: "unknown",
};

/** The one line that describes what a session is doing right now.
 *
 * Rules, in priority order — a failed session leads with its summary because that is what
 * you opened it for; a running one leads with the newest event because that is live
 * information; a finished one falls back to its summary, then to "no activity reported".
 *
 * The summary is only preferred for `failed`. For a completed session the summary usually
 * just restates the last event ("done"), so preferring it would replace the more informative
 * "all 419 tests passed" with a strictly worse line.
 */
export function doingLine(session: Session): string {
  if (session.status === "failed" && session.summary) return session.summary;
  // A verdict describes the last finished task and is more useful than the turn boundary that
  // followed it ("the reply ended"). Only a verdict that still describes the current work is
  // shown — see `visibleVerdict`.
  const verdict = visibleVerdict(session);
  if (verdict) return verdict.message;
  if (session.last_event) return session.last_event.message;
  if (session.summary) return session.summary;
  if (session.status === "running") return "no activity reported yet";
  return "no events reported";
}

/** True only for a *reported* alive state; staleness never counts as running. */
export function isBusy(session: Session): boolean {
  return session.status === "running";
}

/**
 * Is this session blocked on a human right now?
 *
 * The signal is an `awaiting_user` event at the head of the session's timeline — the agent
 * reported that it is waiting for an answer. It sits beside `isBusy` rather than inside
 * `status` on purpose: a status says how a session ended, and this says what it needs this
 * minute. Waiting is not an outcome, so it must not be sticky.
 *
 * Self-clearing by construction. `last_event` is the newest non-heartbeat event, so the
 * moment the session reports anything else — the answer, the next progress line — this is
 * false again, with no flag left behind to reset.
 *
 * Gated on `running` because a session that has already ended is waiting for nothing. That
 * also keeps a late notification from parking a finished session at the top of the board.
 */
export function isAwaitingUser(session: Session): boolean {
  return (
    session.status === "running" && session.last_event?.event_type === "awaiting_user"
  );
}

/**
 * Has the agent finished its last reply while the session is still open?
 *
 * A turn boundary is **reported**, not inferred: Claude Code fires `Stop` when a reply ends,
 * and an interactive editor keeps one process alive for days, so `SessionEnd` is not what
 * tells you the agent stopped working. Read off the newest display event, exactly like
 * `isAwaitingUser` — and that is what makes it clear itself. The next prompt reports a
 * `progress` event, which supersedes this one; no flag to reset, no expiry to tune.
 *
 * It is deliberately not a `SessionStatus`: the session *is* still running (the process is
 * open), it is just not working, which is the distinction the card needs and the status
 * column must not carry (docs/design.md §36).
 */
export function isIdle(session: Session): boolean {
  return (
    session.status === "running" && session.last_event?.event_type === "turn_completed"
  );
}

/**
 * The task verdict that still describes the current work, if any.
 *
 * Read off `last_verdict`, **not** `last_event`: an agent reports its verdict at the end of a
 * turn, and the `turn_completed` (or a trailing checkpoint) that follows would hide it if it
 * shared `last_event`. Keeping the projection separate is what makes "completed run, failed
 * task" visible at all (docs/design.md §40).
 *
 * It stops describing the current work once work resumes. A running session with a newer
 * `progress`/`checkpoint` is on a new task, so the old verdict is not shown; `turn_completed`
 * (idle), `awaiting_user` and `note` are not work and do not clear it. A closed session keeps
 * its last verdict — there is nothing after it to contradict it.
 */
function visibleVerdict(session: Session): Session["last_verdict"] {
  const verdict = session.last_verdict;
  if (!verdict) return null;
  if (!isBusy(session) || isIdle(session)) return verdict;

  const event = session.last_event;
  if (!event) return verdict;
  if (event.id <= verdict.id) return verdict;
  if (
    event.event_type === "turn_completed" ||
    event.event_type === "awaiting_user" ||
    event.event_type === "note"
  ) {
    return verdict;
  }
  return null;
}

/** Did the *work* fail, while the run itself may have ended fine? */
export function isTaskFailed(session: Session): boolean {
  return visibleVerdict(session)?.event_type === "task_failed";
}

/** Did the agent report that the work succeeded? */
export function isTaskCompleted(session: Session): boolean {
  return visibleVerdict(session)?.event_type === "task_completed";
}

/** The status a person should read. `idle` is a reported display state, not a status. */
export function statusLabel(session: Session): string {
  return isIdle(session) ? "idle" : STATUS_LABEL[session.status];
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "—";

  const seconds = Math.round((Date.now() - then.getTime()) / 1000);
  if (seconds < 45) return "now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

export function clockTime(iso: string): string {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "--:--:--";
  return then.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function absoluteTime(iso: string): string {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "—";
  return then.toLocaleString();
}

/**
 * Group sessions by harness, preserving the server's ordering (running first, then most
 * recently touched). Using a Map keeps insertion order, so no re-sort is needed.
 */
export interface HarnessGroup {
  agentType: string;
  sessions: Session[];
  running: number;
  failed: number;
}

export function groupByHarness(sessions: Session[]): HarnessGroup[] {
  const map = new Map<string, Session[]>();
  for (const session of sessions) {
    const bucket = map.get(session.agent_type);
    if (bucket) bucket.push(session);
    else map.set(session.agent_type, [session]);
  }
  return [...map.entries()].map(([agentType, list]) => ({
    agentType,
    sessions: list,
    running: list.filter((s) => isBusy(s)).length,
    failed: list.filter((s) => s.status === "failed").length,
  }));
}

/** The two-letter mark on a harness row, e.g. `claude_code` → `CC`. */
export function harnessMark(agentType: string): string {
  const cleaned = agentType.replace(/[^a-zA-Z0-9]/g, "");
  if (cleaned.length <= 2) return cleaned.toUpperCase();
  return cleaned.slice(0, 2).toUpperCase();
}

/** Last path segment of a workspace, for the card footer. */
export function workspaceTail(workspace: string | null): string {
  if (!workspace) return "";
  const parts = workspace.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? workspace;
}