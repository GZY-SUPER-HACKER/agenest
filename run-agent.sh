#!/usr/bin/env bash
#
# run-agent.sh — report a coding agent's whole lifecycle, without changing how it runs.
#
# The point of this wrapper is that the agent stays the agent. `cpctl` sits *around* it:
# it opens a session before the agent starts, and closes it with the agent's real exit status
# afterwards. It never intercepts output, never parses a terminal, never guesses progress.
#
# Usage:
#   export CSD_BASE_URL=http://127.0.0.1:8780
#   export CSD_INGEST_TOKEN=...
#   ./run-agent.sh --title "Fix the flaky test" -- codex "fix tests/session/test_compaction.py"
#
# Or let it guess the title from the command:
#   ./run-agent.sh codex "fix the flaky test"

set -uo pipefail   # NOT -e: a reporting failure must never abort the agent (spec 8, item 10)

: "${CSD_BASE_URL:=http://127.0.0.1:8780}"
: "${CSD_INGEST_TOKEN:=}"
: "${CSD_AGENT_TYPE:=claude_code}"
export CSD_BASE_URL

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
if [ "${1:-}" = "--title" ]; then
  TITLE="$2"
  shift 2
fi

if [ $# -eq 0 ]; then
  echo "usage: $0 [--title \"...\"] <agent command...>" >&2
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

# --- 4. close the session with the real outcome ---------------------------- #
if [ -n "$SESSION" ]; then
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

# --- 5. exit with the agent's status, always ------------------------------- #
# Never cpctl's. The caller's shell must see what the agent did, not what the reporter did.
exit $STATUS
