#!/usr/bin/env bash
#
# run-agent.sh — report a coding agent's whole lifecycle, without changing how it runs.
#
# The point of this wrapper is that the agent stays the agent. `cpctl` sits *around* it:
# it opens a session before the agent starts, and closes it with the agent's real exit status
# afterwards. It never intercepts output, never parses a terminal, never guesses progress.
#
# It also does not decide whether the *work* succeeded — an agent that exits 0 can still have
# failed the task, and nothing in its exit status says which. `--verdict` is the caller's
# answer: one command whose own exit code is the task outcome, reported as `task_failed`
# without touching the agent's run (docs/design.md §40).
#
# Usage:
#   export CSD_BASE_URL=http://127.0.0.1:8780
#   export CSD_INGEST_TOKEN=...
#   ./run-agent.sh --title "Fix the flaky test" -- codex "fix tests/session/test_compaction.py"
#
# With a task verdict — the agent can run cleanly and still fail the task, which is the one
# outcome no agent runtime reports:
#   ./run-agent.sh --verdict "pytest -q" -- claude -p "fix the flaky test"
#
# Or let it guess the title from the command:
#   ./run-agent.sh codex "fix the flaky test"

set -uo pipefail   # NOT -e: a reporting failure must never abort the agent (spec 8, item 10)

: "${CSD_BASE_URL:=http://127.0.0.1:8780}"
: "${CSD_INGEST_TOKEN:=}"
: "${CSD_AGENT_TYPE:=claude_code}"
export CSD_BASE_URL
# The agent inherits this, so `cpctl task done|failed` from its own shell can report to the
# same dashboard. It is the ingest token the caller already gave this wrapper, not a new one.
export CSD_INGEST_TOKEN

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
CPCTL=(python -m cpctl)

# --- resolve cpctl's location ---------------------------------------------- #
# Prefer an installed cpctl on PATH; fall back to the source tree next to this script.
# The fallback sets PYTHONPATH because `python -m cpctl` resolves the package from the
# working directory, and this script is meant to be run from the project it wraps.
if command -v cpctl >/dev/null 2>&1; then
  CPCTL=(cpctl)
elif [ -x "$SELF_DIR/backend/.venv/Scripts/python.exe" ]; then
  CPCTL=(env PYTHONPATH="$SELF_DIR/sdk/cpctl" "$SELF_DIR/backend/.venv/Scripts/python.exe" -m cpctl)
elif [ -x "$SELF_DIR/backend/.venv/bin/python" ]; then
  CPCTL=(env PYTHONPATH="$SELF_DIR/sdk/cpctl" "$SELF_DIR/backend/.venv/bin/python" -m cpctl)
fi

# --- parse arguments ------------------------------------------------------- #
TITLE=""
VERDICT=""
while [ $# -gt 0 ]; do
  case "${1:-}" in
    --title)   TITLE="${2:-}"; shift 2 ;;
    --verdict) VERDICT="${2:-}"; shift 2 ;;
    --)        shift; break ;;
    *)         break ;;
  esac
done

if [ $# -eq 0 ]; then
  echo "usage: $0 [--title \"...\"] [--verdict \"command\"] <agent command...>" >&2
  exit 2
fi

PROJECT=$(basename "$PWD")
WORKSPACE="$PWD"
[ -z "$TITLE" ] && TITLE="$*"

warn() { echo "run-agent: $*" >&2; }

# --- 1. open the session --------------------------------------------------- #
# A failure here is reported, not fatal: the work continues unobserved.
if [ -z "$CSD_INGEST_TOKEN" ]; then
  warn "CSD_INGEST_TOKEN is unset — running the agent without reporting"
  SESSION=""
else
  if ! SESSION=$("${CPCTL[@]}" session start \
        --agent "$CSD_AGENT_TYPE" \
        --title "$TITLE" \
        --project "$PROJECT" \
        --workspace "$WORKSPACE" 2>&1); then
    warn "could not create a session: $SESSION"
    SESSION=""
  fi
fi

# The agent and every tool it runs inherit this, so `cpctl task done|failed` from inside the
# agent resolves to the session this wrapper owns. Without it the CLI would have to guess.
if [ -n "$SESSION" ]; then
  export CSD_SESSION_ID="$SESSION"
fi

# --- 2. report its own termination on a signal ----------------------------- #
# SIGINT/SIGTERM kill the agent too, so this trap runs before the process dies. Without it
# an interrupted session would sit at "running" forever and only ever be marked stale.
cleanup() {
  [ -n "$SESSION" ] || return 0
  "${CPCTL[@]}" session finish --session "$SESSION" \
    --status interrupted --summary "Interrupted by signal" >/dev/null 2>&1 || true
}
trap cleanup INT TERM

# --- 3. run the agent. Not piped, not captured, not parsed. ----------------- #
"$@"
STATUS=$?

# --- 4. ask the caller's verifier whether the work actually succeeded ------- #
# Only after a clean agent exit. A non-zero exit is already a failed run, and 130 is an
# interruption — verifying either would report a second, redundant verdict. The verifier's
# output is left on the terminal on purpose: it is the evidence for the verdict.
VERDICT_STATUS=""
if [ -n "$VERDICT" ] && [ "$STATUS" -eq 0 ]; then
  bash -c "$VERDICT"
  VERDICT_STATUS=$?
  if [ "$VERDICT_STATUS" -ne 0 ]; then
    warn "verdict failed (exit $VERDICT_STATUS): $VERDICT"
  fi
fi

# --- 5. close the session with the real outcome ---------------------------- #
if [ -n "$SESSION" ]; then
  if [ -n "$VERDICT_STATUS" ] && [ "$VERDICT_STATUS" -ne 0 ]; then
    # The agent ran; the work did not pass. `task_failed` is the event, not a second status:
    # it can land after the session has closed, and the dashboard reads it as needs-attention
    # while the status column still says how the run ended (docs/design.md §40).
    MSG="verification failed (exit $VERDICT_STATUS): $VERDICT"
    "${CPCTL[@]}" task failed --session "$SESSION" \
      -m "${MSG:0:400}" --source verifier \
      --payload "exit_code=$VERDICT_STATUS" >/dev/null 2>&1 \
      || warn "could not report the task verdict"
    "${CPCTL[@]}" session finish --session "$SESSION" \
      --status failed --exit-code "$VERDICT_STATUS" --summary "${MSG:0:800}" \
      >/dev/null 2>&1 || warn "could not report the verification failure"
  elif [ -n "$VERDICT_STATUS" ]; then
    # The verifier passed. Report that explicitly rather than leaving "no verdict": the
    # whole point of `--verdict` is to say the work was checked, not merely that the process
    # exited 0.
    "${CPCTL[@]}" task done --session "$SESSION" --source verifier \
      -m "verification passed: $VERDICT" >/dev/null 2>&1 \
      || warn "could not report the task verdict"
    "${CPCTL[@]}" session finish --session "$SESSION" \
      --status completed --exit-code "$STATUS" >/dev/null 2>&1 \
      || warn "could not report completion"
  else
    case $STATUS in
      0)
        "${CPCTL[@]}" session finish --session "$SESSION" \
          --status completed --exit-code "$STATUS" >/dev/null 2>&1 \
          || warn "could not report completion"
        ;;
      130)
        # The trap usually catches this first; this is the belt to its braces.
        "${CPCTL[@]}" session finish --session "$SESSION" \
          --status interrupted --exit-code "$STATUS" >/dev/null 2>&1 || true
        ;;
      *)
        "${CPCTL[@]}" session finish --session "$SESSION" \
          --status failed --exit-code "$STATUS" >/dev/null 2>&1 \
          || warn "could not report failure"
        ;;
    esac
  fi
fi

# --- 6. exit with the agent's status, always ------------------------------- #
# Never cpctl's, and never the verifier's: the caller's shell must see what the agent did.
# A failed verdict is a reported fact on the dashboard, not a change to this contract.
exit $STATUS
