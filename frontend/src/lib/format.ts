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