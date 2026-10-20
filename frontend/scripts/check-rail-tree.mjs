/**
 * Checks for the rail's tree building — `npm run check:rail`.
 *
 * A plain node script, not a test framework. There is no unit-test runner in this package and
 * this is one pure function's worth of coverage, so adding vitest to get it would be a
 * dependency and a config file to maintain for a single file of assertions. `esbuild` is
 * already here as vite's bundler, so the npm script uses it to compile the TS module and node
 * runs the result.
 *
 * It exists because the rail's grouping is where a mistake hides. A host level that quietly
 * dropped a session, or a two-agent machine that listed its work twice, renders as a
 * plausible-looking tree — nobody sees it on a screen. The first version of this code did
 * exactly that: one node per *registry row* instead of per *machine* meant every session on a
 * box appeared under each of its connectors. The check below caught it, and the assertion
 * that "every session appears exactly once across the tree" is the one that generalises.
 */
import { buildTree, hostState, HOST_STATE_LABEL } from "../node_modules/.cache/rail-tree.mjs";

const session = (o) => ({
  id: o.id, agent_type: o.agent_type ?? "claude_code", title: o.title ?? "t",
  project_name: o.project_name ?? null, workspace: null, host: o.host ?? null,
  status: o.status ?? "running", started_at: "t", last_activity_at: "t",
  finished_at: null, summary: null, exit_code: null, created_at: "t", updated_at: "t",
  possibly_stale: false, last_event: null,
});
const host = (o) => ({
  id: o.id, label: o.label, agent_type: o.agent_type ?? "claude_code",
  connected_at: "t", last_seen_at: "t", disconnected_at: o.disconnected_at ?? null,
  possibly_stale: o.possibly_stale ?? false,
  wants_access: o.wants_access ?? false,
  access_state: o.access_state ?? null,
  access_target: o.access_target ?? null,
  access_fingerprint: o.access_fingerprint ?? null,
});

const sessions = [
  session({ id: "s1", host: "lab-box", project_name: "nanobot", title: "refactor compaction" }),
  session({ id: "s2", host: "lab-box", project_name: "nanobot", title: "issue report", status: "completed" }),
  session({ id: "s3", host: "lab-box", agent_type: "codex", project_name: "loopx", title: "bump CI" }),
  session({ id: "s4", host: "ghost-box", project_name: "mystery", title: "named but never registered" }),
  session({ id: "s5", title: "reported before hosts existed" }),
  session({ id: "s6", host: "gpu-02", project_name: "obelisk", title: "migration" }),
];

const hosts = [
  host({ id: "h1", label: "lab-box", agent_type: "claude_code" }),
  host({ id: "h2", label: "lab-box", agent_type: "codex" }),                 // same box, 2nd agent
  host({ id: "h3", label: "gpu-02", agent_type: "claude_code", possibly_stale: true,
         wants_access: true, access_state: "ready", access_target: "me@gpu-02:22" }),
  // Asks to be driven and has not been authorized — the rail's reminder case.
  host({ id: "h4", label: "idle-box", agent_type: "claude_code",
         wants_access: true, access_state: "requested" }),                     // connected, ZERO sessions
  host({ id: "h5", label: "gone-box", agent_type: "claude_code", disconnected_at: "t" }),
  host({ id: "h6", label: "mixed-box", agent_type: "claude_code" }),         // live...
  host({ id: "h7", label: "mixed-box", agent_type: "codex", disconnected_at: "t" }), // ...and dead
// One connector on this box is authorized, the other is not: the machine still waits.
  host({ id: "h8", label: "half-box", agent_type: "claude_code",
         wants_access: true, access_state: "ready" }),
  host({ id: "h9", label: "half-box", agent_type: "codex",
         wants_access: true, access_state: "pending" }),
  // Revoked: a decision already made, not an errand outstanding.
  host({ id: "h10", label: "revoked-box", agent_type: "claude_code",
         wants_access: true, access_state: "disabled" }),
];

const tree = buildTree(sessions, hosts);

let failures = 0;
const check = (label, actual, expected) => {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failures++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got  ${a}\n         want ${e}`}`);
};

console.log("--- the tree, as the rail would draw it ---");
for (const n of tree) {
  const st = hostState(n.host);
  console.log(`  ${n.name}  [${HOST_STATE_LABEL[st] || "no claim"}]  ${n.sessions.length} sessions`);
  for (const h of n.harnesses) {
    console.log(`      ${h.name}  (${h.sessions.length})${h.connectedOnly ? "  <- connected, empty" : ""}`);
    for (const p of h.projects) console.log(`          ${p.name}: ${p.sessions.map((s) => s.id).join(", ")}`);
  }
}
console.log();

check("one node per MACHINE, registered first, unattributed last",
  tree.map((n) => n.name),
  ["lab-box", "gpu-02", "idle-box", "gone-box", "mixed-box", "half-box", "revoked-box", "ghost-box",
   "no host recorded"]);

// The bug this suite was written for: a node per registry row put every session on a box
// under each of its connectors, so a two-agent machine listed its work twice over.
const total = tree.reduce((n, node) => n + node.sessions.length, 0);
check("every session appears exactly once across the tree", total, sessions.length);

const lab = tree.find((n) => n.name === "lab-box");
check("two connectors on one box are one machine with two harnesses",
  lab.harnesses.map((h) => h.name), ["claude_code", "codex"]);
check("...and each harness holds only its own agent's sessions",
  lab.harnesses.map((h) => h.sessions.map((s) => s.id)), [["s1", "s2"], ["s3"]]);

const idle = tree.find((n) => n.name === "idle-box");
check("a connected host with zero sessions still renders",
  [idle.harnesses.length, idle.harnesses[0].name, idle.harnesses[0].connectedOnly],
  [1, "claude_code", true]);

const ghost = tree.find((n) => n.name === "ghost-box");
check("a session naming an unregistered host still groups, with no claim",
  [ghost.sessions.map((s) => s.id), ghost.host, hostState(ghost.host)],
  [["s4"], null, "unrecorded"]);
check("...and its harness is not marked connected-only", ghost.harnesses[0].connectedOnly, false);

const orphan = tree.find((n) => n.name === "no host recorded");
check("sessions with no host land in their own bucket",
  orphan.sessions.map((s) => s.id), ["s5"]);
check("...which is last", tree[tree.length - 1].name, "no host recorded");

check("grouping into projects inside a harness",
  lab.harnesses[0].projects.map((p) => [p.name, p.sessions.length]), [["nanobot", 2]]);

check("a quiet host reads as maybe offline",
  HOST_STATE_LABEL[hostState(tree.find((n) => n.name === "gpu-02").host)], "maybe offline");
check("a disconnected host is reported, not inferred",
  HOST_STATE_LABEL[hostState(tree.find((n) => n.name === "gone-box").host)], "disconnected");
check("a box is live if ANY connector on it is live",
  HOST_STATE_LABEL[hostState(tree.find((n) => n.name === "mixed-box").host)], "connected");

// Access is a *machine-level* question, reduced over every connector on the box: a rail chip
// derived from `mostLive` would claim a two-agent machine was settled while one of its agents
// still waited on the user.
const needs = (name) => tree.find((n) => n.name === name).accessNeeds;
check("a host that asked and was not authorized needs the user",
  needs("idle-box"), true);
check("a host that never asked does not", needs("lab-box"), false);
check("an authorized host is settled", needs("gpu-02"), false);
check("...and so is one whose connector was never asked",
  needs("ghost-box"), false);
check("a box waits if any of its connectors still does",
  [needs("half-box"), needs("mixed-box")], [true, false]);
check("a revoked host is not an outstanding errand", needs("revoked-box"), false);

console.log();
console.log(failures === 0 ? "ALL CHECKS PASSED" : `${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
