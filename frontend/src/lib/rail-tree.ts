/**
 * The rail's tree: host → harness → project → session.
 *
 * Separated from the component because this is where the decisions are, and decisions written
 * inside a render function can only be checked by looking at a screen. The component below it
 * is layout; this is the part that can be wrong in a way nobody notices for a week — a host
 * that quietly swallows its sessions, a machine that only appears once it has work on it.
 *
 * The host level is the **union of two sources**, which is the whole subtlety: machines the
 * registry knows about (connected, quite possibly with nothing running on them) and machine
 * names that appear only on sessions. Both are legitimate:
 *
 *   - A host with no sessions is the state this entire level exists for. Before it, every
 *     group was derived from sessions, so a machine that had just connected looked exactly
 *     like one that had never been set up.
 *   - A session naming a host that never registered (`cpctl session start --host lab-box`)
 *     is a real thing that happens, and dropping it would lose the session from the tree.
 *     Such a node carries no connection marker, because none was reported.
 *
 * A session with no host at all collects under "no host recorded" — a bucket, not a guess.
 */

import { isBusy, isIdle } from "./format";
import type { Host, Session } from "../types/api";

/** The bucket for sessions that never said which machine they ran on. */
export const NO_HOST_KEY = "\u0000no-host";
export const NO_HOST_LABEL = "no host recorded";

export interface ProjectNode {
  key: string;
  name: string;
  sessions: Session[];
  running: number;
}

export interface HarnessNode {
  key: string;
  name: string;
  projects: ProjectNode[];
  sessions: Session[];
  running: number;
  failed: number;
  /** Known from the registry but with nothing on it — the state this level is for. */
  connectedOnly: boolean;
}

export interface HostNode {
  key: string;
  name: string;
  /** The registry row, when there is one. `null` means the name came only from sessions. */
  host: Host | null;
  harnesses: HarnessNode[];
  sessions: Session[];
  running: number;
  failed: number;
  /**
   * Some connector on this machine asked to be driven and the user has not authorized it yet —
   * the rail's one reminder about access (docs/design.md §24).
   *
   * Reduced over *all* of a machine's rows rather than read off the single `host` above, for
   * the same reason the node exists at all: a box with Claude Code authorized and codex not is
   * a box with something waiting, and asking only its most-live connector would hide that
   * behind which of the two happened to report last.
   */
  accessNeeds: boolean;
}

/** What a host's marker is allowed to claim. `unrecorded` claims nothing. */
export type HostState = "connected" | "stale" | "disconnected" | "unrecorded";

export const HOST_STATE_LABEL: Record<HostState, string> = {
  connected: "connected",
  stale: "maybe offline",
  disconnected: "disconnected",
  unrecorded: "",
};

export function hostState(host: Host | null): HostState {
  if (host === null) return "unrecorded";
  if (host.disconnected_at) return "disconnected";
  return host.possibly_stale ? "stale" : "connected";
}

/**
 * How many sessions under this node are *working* — running and not between replies. A node's
 * marker is the rail's at-a-glance "something is happening here", so an idle session must not
 * light it up: it is the same claim the Running filter and the session row make (§36).
 */
const workingCount = (rows: Session[]): number =>
  rows.filter((s) => isBusy(s) && !isIdle(s)).length;

function harnessesFor(agentTypes: string[], sessions: Session[]): HarnessNode[] {
  // Sessions first, so the order the server gave (running first, newest first) survives; a
  // reported-but-idle harness is appended, because it has no ordering of its own.
  const byHarness = new Map<string, Session[]>();
  for (const session of sessions) {
    const bucket = byHarness.get(session.agent_type);
    if (bucket) bucket.push(session);
    else byHarness.set(session.agent_type, [session]);
  }
  for (const agentType of agentTypes) {
    if (!byHarness.has(agentType)) byHarness.set(agentType, []);
  }

  return [...byHarness.entries()].map(([name, list]) => {
    const projects = new Map<string, Session[]>();
    for (const session of list) {
      const project = session.project_name || "unassigned";
      const bucket = projects.get(project);
      if (bucket) bucket.push(session);
      else projects.set(project, [session]);
    }
    const nodes: ProjectNode[] = [...projects.entries()].map(([pname, rows]) => ({
      key: `${name}/${pname}`,
      name: pname,
      sessions: rows,
      running: workingCount(rows),
    }));
    return {
      key: name,
      name,
      projects: nodes,
      sessions: list,
      running: workingCount(list),
      failed: list.filter((s) => s.status === "failed").length,
      connectedOnly: list.length === 0,
    };
  });
}

/** Group sessions and hosts into the four-level tree, preserving the server's ordering. */
export function buildTree(sessions: Session[], hosts: Host[]): HostNode[] {
  const byHost = new Map<string, Session[]>();
  const named = new Map<string, string>(); // host key → display name

  for (const session of sessions) {
    const key = session.host ? `h:${session.host}` : NO_HOST_KEY;
    if (session.host) named.set(key, session.host);
    const bucket = byHost.get(key);
    if (bucket) bucket.push(session);
    else byHost.set(key, [session]);
  }

  // One node per **machine**, not one per registry row. The registry can hold several rows
  // for one box — one connector per coding agent — and a node per row put every session on
  // that box under *each* of them, so a box running two agents appeared twice with identical
  // contents. Grouping by label is what makes the level mean "which machine" (see the module
  // docstring). The connector rows are then unioned into that node's harness list, which is
  // how a connected-but-idle agent still gets a group of its own.
  const rowsByLabel = new Map<string, Host[]>();
  const labelOrder: string[] = [];
  for (const host of hosts) {
    const bucket = rowsByLabel.get(host.label);
    if (bucket) bucket.push(host);
    else {
      rowsByLabel.set(host.label, [host]);
      labelOrder.push(host.label);
    }
  }

  const nodes: HostNode[] = [];
  const seen = new Set<string>();

  // Registered machines first, in the order the API returned them (most recently seen first):
  // this list is a liveness view, so the box that just checked in belongs at the top.
  for (const label of labelOrder) {
    const key = `h:${label}`;
    const registered = rowsByLabel.get(label)!;
    seen.add(key);
    const rows = byHost.get(key) ?? [];
    nodes.push({
      key,
      name: label,
      // The node's state, reduced from its connectors by `mostLive`. A machine cannot be
      // connected and not connected at once, so several rows have to become one answer.
      host: mostLive(registered),
      harnesses: harnessesFor(registered.map((h) => h.agent_type), rows),
      sessions: rows,
      running: workingCount(rows),
      failed: rows.filter((s) => s.status === "failed").length,
      accessNeeds: needsAccess(registered),
    });
  }

  // Then machines that only sessions mention. Same grouping, no connection claim.
  for (const [key, rows] of byHost) {
    if (seen.has(key)) continue;
    nodes.push({
      key,
      name: key === NO_HOST_KEY ? NO_HOST_LABEL : (named.get(key) ?? key),
      host: null,
      harnesses: harnessesFor([], rows),
      sessions: rows,
      running: workingCount(rows),
      failed: rows.filter((s) => s.status === "failed").length,
      // Nothing registered, so nothing to authorize: a machine nobody has connected from
      // cannot be asking to be driven.
      accessNeeds: false,
    });
  }

  // Unattributed last, always: it is the fallback pile, and it should not push a real machine
  // off the top of the list.
  const unattributed = nodes.findIndex((n) => n.key === NO_HOST_KEY);
  if (unattributed !== -1) nodes.push(nodes.splice(unattributed, 1)[0]);

  return nodes;
}

/**
 * The one connector that speaks for a machine: the most live one on it.
 *
 * A node carries a single `Host` and a machine can have several, so they have to be reduced.
 * Liveness wins in the order connected → stale → disconnected, which is the same
 * "how much does this need you" ordering the status scale is built on. Picking the first row
 * instead would let a connector that was uninstalled last week drag a perfectly healthy
 * machine down to "disconnected".
 */
function mostLive(rows: Host[]): Host {
  const rank = (h: Host) => (h.disconnected_at ? 2 : h.possibly_stale ? 1 : 0);
  return rows.reduce((best, candidate) => (rank(candidate) < rank(best) ? candidate : best));
}

/** Does any connector on this machine want access the user has not granted?
 *
 * `pending` counts — the key exists and the host has not installed it yet, which is still a state
 * a person may have to do something about, and the sheet says what. `disabled` does not: a revoked
 * host is a decision already made, and a milestone marker that kept asking would be nagging the
 * user about something they did. */
function needsAccess(rows: Host[]): boolean {
  return rows.some(
    (h) => h.wants_access && h.access_state !== "ready" && h.access_state !== "disabled",
  );
}
