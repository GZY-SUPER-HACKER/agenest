/**
 * Checks for the overview's bucketing rules — `npm run check:board`.
 *
 * Same shape and the same reason as `check-rail-tree.mjs`: there is no unit-test runner in
 * this package, and this is one pure module's worth of coverage, so esbuild compiles
 * `lib/format.ts` and node runs the assertions. A test framework would be a dependency and a
 * config file for a handful of `check()` calls.
 *
 * It exists because a wrong predicate does not crash. It renders a plausible board — every
 * session still has a card, every tile still has a number — and the error is only visible as a
 * feeling that the list is too long. The rule pinned here is the one that changed on
 * 2026-11-13 (docs/design.md §44): `completed` is a statement about how the run stopped, not
 * a request for a decision, so it must not enter needs-attention — and removing it must leave
 * the tile row a partition (running + idle + attention + finished = total).
 */
import {
  isAwaitingUser,
  isTaskFailed,
  needsAttention,
  sessionBucket,
  statusLabel,
} from "../node_modules/.cache/format.mjs";

let eventId = 0;
const ev = (event_type) => ({
  id: ++eventId,
  session_id: "s",
  event_type,
  message: event_type,
  payload: null,
  occurred_at: "t",
  received_at: "t",
});

let sessionId = 0;
const session = (o = {}) => ({
  id: o.id ?? `s${++sessionId}`,
  agent_type: "claude_code",
  title: "t",
  project_name: null,
  workspace: null,
  host: null,
  status: o.status ?? "running",
  started_at: "t",
  last_activity_at: "t",
  last_heartbeat_at: o.last_heartbeat_at ?? null,
  finished_at: null,
  summary: null,
  exit_code: null,
  stop_reason: o.stop_reason ?? null,
  created_at: "t",
  updated_at: "t",
  possibly_stale: false,
  disconnected: o.disconnected ?? false,
  last_event: o.last_event ?? null,
  last_verdict: o.last_verdict ?? null,
  dismissed: o.dismissed ?? false,
});

let failures = 0;
const check = (label, actual, expected) => {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failures++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got  ${a}\n         want ${e}`}`);
};

console.log("--- what asks for a decision ---");

// A run that reached an ending, however it ended. None of the three asks for a decision, and
// the pill must still say what happened.
const reported = session({ status: "completed" });
const crashed = session({ status: "completed", stop_reason: "crash" });
const closedByHand = session({ status: "completed", stop_reason: "user" });
check("a completed session does not ask for attention",
  [needsAttention(reported), needsAttention(crashed), needsAttention(closedByHand)],
  [false, false, false]);
check("...and it lands in Finished, not attention",
  [sessionBucket(reported), sessionBucket(crashed), sessionBucket(closedByHand)],
  ["finished", "finished", "finished"]);
check("...with a reported completion saying so",
  statusLabel(reported), "completed");
check("...and a hand-closed row saying who closed it",
  statusLabel(closedByHand), "closed by you");
check("...and no dismissal is needed to get it there (dismissed changes nothing)",
  [sessionBucket(session({ status: "completed", dismissed: true })), needsAttention(session({ status: "completed", dismissed: true }))],
  ["finished", false]);

// The report that *does* know the work did not pass. It may arrive after the run closed, which
// is exactly why it is projected separately from `last_event` (§40).
const killedButFailed = session({ status: "completed", last_verdict: ev("task_failed") });
check("a closed run with a task_failed verdict still asks",
  [needsAttention(killedButFailed), sessionBucket(killedButFailed)], [true, "attention"]);
check("...and dismissing it moves it to Finished",
  sessionBucket({ ...killedButFailed, dismissed: true }), "finished");

check("awaiting_user on a running session asks",
  sessionBucket(session({ last_event: ev("awaiting_user") })), "attention");
check("a failed run asks", sessionBucket(session({ status: "failed" })), "attention");
check("a completed run does not", sessionBucket(session({ status: "completed" })), "finished");
check("a plain running session is just running",
  sessionBucket(session({ last_event: ev("progress") })), "running");
check("a running session between replies is idle",
  sessionBucket(session({ last_event: ev("turn_completed") })), "idle");
check("a task_completed verdict does not change the bucket",
  sessionBucket(session({ last_event: ev("turn_completed"), last_verdict: ev("task_completed") })),
  "idle");

// `awaiting_user` must not outlive the process: a dead session cannot receive the answer, and
// a late event must not park a finished row at the top of the board (§16b).
check("a waiting event on a completed session is not waiting",
  [isAwaitingUser(session({ status: "completed", last_event: ev("awaiting_user") })),
   sessionBucket(session({ status: "completed", last_event: ev("awaiting_user") }))],
  [false, "finished"]);

// A dismissed request that is still running falls through to Running — it is still open, only
// the request was answered. One bucket, so the arithmetic holds (§42).
check("a dismissed awaiting session that is still open is Running",
  sessionBucket(session({ last_event: ev("awaiting_user"), dismissed: true })), "running");

// The verdict stops describing the current work once newer work is reported. A killed run that
// resumed is not still failed; a turn boundary or a note is not work and does not clear it.
const verdict = ev("task_failed");
const resumed = session({ last_verdict: verdict, last_event: ev("progress") });
check("a newer progress line clears an old task_failed verdict",
  [isTaskFailed(resumed), sessionBucket(resumed)], [false, "running"]);
const afterTurn = session({ last_verdict: verdict, last_event: ev("turn_completed") });
check("...but turn_completed does not",
  [isTaskFailed(afterTurn), sessionBucket(afterTurn)], [true, "attention"]);

console.log();
console.log("--- the tile row is still a partition ---");

const board = [
  session({ id: "running-1", last_event: ev("progress") }),
  session({ id: "idle-1", last_event: ev("turn_completed") }),
  session({ id: "blocked-1", last_event: ev("awaiting_user") }),
  session({ id: "dismissed-blocked-1", last_event: ev("awaiting_user"), dismissed: true }),
  session({ id: "failed-1", status: "failed" }),
  session({ id: "task-failed-1", status: "completed", last_verdict: ev("task_failed") }),
  session({ id: "completed-1", status: "completed" }),
  session({ id: "unknown-1", status: "unknown" }),
  ...Array.from({ length: 10 }, (_, i) => session({ id: `completed-${i}`, status: "completed" })),
];

const buckets = ["attention", "running", "idle", "finished"];
const counts = Object.fromEntries(
  buckets.map((b) => [b, board.filter((s) => sessionBucket(s) === b).length]),
);
const seen = board.reduce((n, s) => n + Number(buckets.includes(sessionBucket(s))), 0);
check("every session lands in exactly one bucket", seen, board.length);
check("the four tiles add up to the total",
  buckets.reduce((n, b) => n + counts[b], 0), board.length);
check("completed rows contribute to Finished alone",
  [counts.attention, counts.finished], [3, 12]);
check("the attention tile holds only the three reports",
  board.filter((s) => sessionBucket(s) === "attention").map((s) => s.id).sort(),
  ["blocked-1", "failed-1", "task-failed-1"]);

console.log();
console.log(failures === 0 ? "ALL CHECKS PASSED" : `${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
