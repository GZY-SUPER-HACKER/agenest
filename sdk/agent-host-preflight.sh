#!/usr/bin/env bash
#
# Pre-flight for pointing an agent host (a build server, say) at the dashboard.
#
# Why this shape: the dashboard never dials out. docs/design.md §1 makes it observability
# only — no code path in the backend can execute anything, or even open a connection, on the
# machine the work runs on — and the cloud binds 127.0.0.1, so nothing on the internet
# reaches it either. So "connect this server's Claude Code to the dashboard" means *this
# server pushes out*, over a path it originates itself:
#
#   cpctl ──▶ 127.0.0.1:18780 ══ ssh -L ══▶ 127.0.0.1:8780 (cloud) ──▶ SQLite ──▶ SSE ──▶ phone
#
# Four legs have to hold for that to work. This script tests them in order and names the one
# that broke, so a failure is a diagnosis rather than a shrug. It is read-only: it opens a
# tunnel, does one GET, and closes it. Nothing is written to the dashboard unless --write.
#
# Usage:
#   CSD_CLOUD_HOST=1.2.3.4 ./agent-host-preflight.sh          # probe, write nothing
#   CSD_CLOUD_HOST=1.2.3.4 ./agent-host-preflight.sh --write  # ... then push one throwaway session
#   CSD_CLOUD_USER=csdtun CSD_TUNNEL_KEY=~/.ssh/csd_tunnel ./agent-host-preflight.sh
#
# Exit: 0 path usable · 1 a leg is missing (see RESULT) · 2 usage

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
hr() { printf '%s\n' "------------------------------------------------------------"; }

CLOUD_USER=${CSD_CLOUD_USER:-root}
CLOUD_SSH_PORT=${CSD_CLOUD_SSH_PORT:-22}
TUNNEL_PORT=${CSD_TUNNEL_PORT:-18780}

WRITE=0
for arg in "$@"; do
  case "$arg" in
    --write) WRITE=1 ;;
    -h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# No default host on purpose: a repository that ships someone else's server address is one
# where the first run probes a machine the reader does not own.
if [ -z "${CSD_CLOUD_HOST:-}" ]; then
  echo "CSD_CLOUD_HOST is required (the dashboard host, e.g. CSD_CLOUD_HOST=1.2.3.4)" >&2
  exit 2
fi
CLOUD_HOST=$CSD_CLOUD_HOST

fail=0
blockers=()
authed=0
KEY_OPT=()
SOCK="${TMPDIR:-/tmp}/csd-tunnel-$$.sock"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
          -p "$CLOUD_SSH_PORT")

cleanup() {
  [ -S "$SOCK" ] || return 0
  ssh -S "$SOCK" -O exit "$CLOUD_USER@$CLOUD_HOST" >/dev/null 2>&1 || true
  rm -f "$SOCK"
}
trap cleanup EXIT INT TERM

hr
if [ "$WRITE" = 1 ]; then
  echo "Agent-host pre-flight + one throwaway session"
else
  echo "Agent-host pre-flight (read-only)"
fi
echo "host:   $(hostname)  $(date -Is)"
echo "target: $CLOUD_USER@$CLOUD_HOST:$CLOUD_SSH_PORT  →  127.0.0.1:8780 via local port $TUNNEL_PORT"
hr

# --- 0. Local facts that cause silent weirdness ----------------------------- #
echo
echo "[0] Local prerequisites"

proxied=0
for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy; do
  if [ -n "${!v:-}" ]; then echo "  [warn] $v=${!v}"; proxied=1; fi
done
if [ "$proxied" = 1 ]; then
  echo "         cpctl talks through urllib, which honours these for *every* URL —"
  echo "         including 127.0.0.1. A loopback tunnel is not a proxy destination, so"
  echo "         the request leaves the box and dies. This script works around it for its"
  echo "         own calls; the CC hooks will need the same (no_proxy=127.0.0.1)."
else
  echo "  [ok] no proxy variables set"
fi

# Python is only needed to *report*, not to probe the path. So its absence is a warning in
# read-only mode and a blocker only under --write, where nothing can be sent without cpctl.
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done

if [ -n "$PY" ]; then
  echo "  [ok] $PY $("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
else
  if [ "$WRITE" = 1 ]; then
    echo "  [FAIL] no python >= 3.11 — --write needs cpctl, which needs one"
    fail=1; blockers+=("no python >= 3.11")
  else
    echo "  [warn] no python >= 3.11 on PATH. Harmless for this probe; cpctl needs one to"
    echo "         report, so --write and the CC hooks would both be blocked here."
  fi
fi

if command -v cpctl >/dev/null 2>&1; then
  echo "  [ok] cpctl on PATH: $(command -v cpctl)"
elif [ -n "$PY" ] && "$PY" -c 'import cpctl' >/dev/null 2>&1; then
  echo "  [ok] cpctl importable — invoke it as: $PY -m cpctl"
else
  echo "  [warn] cpctl not installed. The probe below still runs; --write and the CC hooks"
  echo "         need it:  scp -r sdk/cpctl <you>@<thishost>:~ && pip3 install --user ~/cpctl"
fi

if command -v claude >/dev/null 2>&1; then
  echo "  [ok] claude on PATH: $(command -v claude)"
else
  echo "  [warn] no \`claude\` on PATH — irrelevant to this probe, but the hooks need it"
fi

# --- 1. Can this host reach the cloud at all? -------------------------------- #
echo
echo "[1] Outbound reachability to the cloud"
reachable=0
if timeout 5 bash -c "echo > /dev/tcp/$CLOUD_HOST/$CLOUD_SSH_PORT" 2>/dev/null; then
  echo "  [ok] TCP $CLOUD_HOST:$CLOUD_SSH_PORT connects"
  reachable=1
else
  echo "  [FAIL] cannot open TCP to $CLOUD_HOST:$CLOUD_SSH_PORT from this host"
  echo "         Either the campus network restricts outbound SSH, or the cloud's security"
  echo "         group does not admit this host's egress IP. Every leg below depends on it."
  echo "         If outbound SSH is genuinely blocked, the fallback is to bind the cloud to"
  echo "         0.0.0.0 and admit only this host's egress IP in the security group — see"
  echo "         .env.example, CSD_HOST. That gives up the loopback default, so it is a"
  echo "         decision rather than a workaround."
  fail=1; blockers+=("outbound SSH to the cloud is blocked")
fi

# --- 2. If it is reachable, can this host authenticate? ---------------------- #
if [ "$reachable" = 1 ]; then
  echo
  echo "[2] SSH authentication"

  if [ -n "${CSD_TUNNEL_KEY:-}" ]; then
    candidates=("$CSD_TUNNEL_KEY")
  else
    candidates=("$HOME/.ssh/id_ed25519" "$HOME/.ssh/id_rsa" "")
  fi

  tried=""
  for k in "${candidates[@]}"; do
    if [ -n "$k" ]; then
      [ -f "$k" ] || { tried="$tried ${k}(absent)"; continue; }
      attempt=(-i "$k"); label="$k"
    else
      attempt=(); label="default keys / agent"
    fi
    if ssh "${SSH_OPTS[@]}" ${attempt[@]+"${attempt[@]}"} \
           "$CLOUD_USER@$CLOUD_HOST" true >/dev/null 2>&1; then
      echo "  [ok] authenticated as $CLOUD_USER using: $label"
      KEY_OPT=(${attempt[@]+"${attempt[@]}"})
      authed=1
      break
    fi
    tried="$tried $label"
  done

  if [ "$authed" = 0 ]; then
    echo "  [FAIL] no working credential. Tried:$tried"
    echo
    echo "         Give this host its own key, scoped to exactly one thing. On THIS host:"
    echo "           ssh-keygen -t ed25519 -f ~/.ssh/csd_tunnel -N '' -C \"csd-tunnel@\$(hostname)\""
    echo "           cat ~/.ssh/csd_tunnel.pub"
    echo
    echo "         Then, from a machine that already reaches the cloud, add it as a user that"
    echo "         can do nothing but forward to the dashboard:"
    echo "           useradd -r -m -s /usr/sbin/nologin csdtun"
    echo "           mkdir -p /home/csdtun/.ssh && chmod 700 /home/csdtun/.ssh"
    echo "           printf 'restrict,port-forwarding,permitopen=\"127.0.0.1:8780\" %s\\n' \\"
    echo "                  '<paste the pubkey>' > /home/csdtun/.ssh/authorized_keys"
    echo "           chmod 600 /home/csdtun/.ssh/authorized_keys"
    echo "           chown -R csdtun:csdtun /home/csdtun/.ssh"
    echo
    echo "         \`restrict\` turns everything off (pty, agent and X11 forwarding, -R) and"
    echo "         \`permitopen\` re-allows -L to that one port — so a stolen key buys an"
    echo "         attacker a tunnel to the dashboard and nothing else. It is the same effort"
    echo "         as adding a root key, which is why there is no reason not to."
    echo
    echo "         Re-run with:  CSD_CLOUD_USER=csdtun CSD_TUNNEL_KEY=~/.ssh/csd_tunnel $0"
    fail=1; blockers+=("no SSH credential from this host to the cloud")
  fi
fi

# --- 3. Is the service actually up on the far side? -------------------------- #
echo
echo "[3] Dashboard service on the cloud (informational)"
if [ "$authed" = 1 ]; then
  remote=$(ssh "${SSH_OPTS[@]}" ${KEY_OPT[@]+"${KEY_OPT[@]}"} "$CLOUD_USER@$CLOUD_HOST" \
      'printf "  service=%s\n  listeners=%s\n" \
              "$(systemctl is-active csd 2>/dev/null || echo unknown)" \
              "$(ss -ltn 2>/dev/null | grep -c "127.0.0.1:8780")"' 2>/dev/null)
  if [ -n "$remote" ]; then
    echo "$remote"
    echo "         (listeners=1 means it is bound where the tunnel will land)"
  else
    echo "  [skip] this user may not run commands — expected for the restricted tunnel user."
    echo "         Whether the service is up is answered by step [4] anyway."
  fi
else
  echo "  [skip] no credential"
fi

# --- 4. Does the tunnel carry the API? --------------------------------------- #
echo
echo "[4] Tunnel + read path"
code=""
if [ "$authed" = 1 ]; then
  if (echo > "/dev/tcp/127.0.0.1/$TUNNEL_PORT") 2>/dev/null; then
    echo "  [warn] local port $TUNNEL_PORT is already taken; pick another with CSD_TUNNEL_PORT"
  fi

  if ssh -f -N -M -S "$SOCK" "${SSH_OPTS[@]}" ${KEY_OPT[@]+"${KEY_OPT[@]}"} \
         -o ExitOnForwardFailure=yes \
         -L "$TUNNEL_PORT:127.0.0.1:8780" "$CLOUD_USER@$CLOUD_HOST" 2>/dev/null \
     && ssh -S "$SOCK" -O check "$CLOUD_USER@$CLOUD_HOST" >/dev/null 2>&1; then
    echo "  [ok] tunnel up: 127.0.0.1:$TUNNEL_PORT → cloud 127.0.0.1:8780"

    code=$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 10 \
             "http://127.0.0.1:$TUNNEL_PORT/api/sessions" 2>/dev/null || echo 000)
    case "$code" in
      200) echo "  [ok] GET /api/sessions → 200" ;;
      401) echo "  [ok] GET /api/sessions → 401"
           echo "         The path works; CSD_DASHBOARD_TOKEN is set, so reads need a token." ;;
      000|"") echo "  [FAIL] the tunnel is up but nothing answered on it."
              echo "         Most likely csd.service is not running on the cloud (step [3])."
              fail=1; blockers+=("tunnel up, API silent — check csd.service") ;;
      *)   echo "  [FAIL] GET /api/sessions → $code (expected 200, or 401 behind a read token)"
           fail=1; blockers+=("unexpected status $code from the API") ;;
    esac

    # --- 5. The write. Only with --write: it puts a row in the record. ------- #
    if [ "$WRITE" = 1 ] && { [ "$code" = 200 ] || [ "$code" = 401 ]; }; then
      echo
      echo "[5] Ingest path (--write)"
      TOKEN="${CSD_INGEST_TOKEN:-}"
      if [ -z "$TOKEN" ]; then
        TOKEN=$(ssh "${SSH_OPTS[@]}" ${KEY_OPT[@]+"${KEY_OPT[@]}"} "$CLOUD_USER@$CLOUD_HOST" \
                  'grep ^CSD_INGEST_TOKEN= /etc/csd/csd.env 2>/dev/null | cut -d= -f2-' 2>/dev/null)
      fi
      if [ -z "$TOKEN" ]; then
        echo "  [FAIL] no ingest token. A restricted tunnel user cannot read it either."
        echo "         Fetch it out of band and export CSD_INGEST_TOKEN before re-running."
        fail=1; blockers+=("no ingest token available")
      else
        if command -v cpctl >/dev/null 2>&1; then CPCTL=(cpctl); else CPCTL=("$PY" -m cpctl); fi
        # urllib would route 127.0.0.1 through any configured proxy; belt and braces.
        export CSD_BASE_URL="http://127.0.0.1:$TUNNEL_PORT"
        export CSD_INGEST_TOKEN="$TOKEN"
        export CSD_STATE_DIR="${CSD_STATE_DIR:-$HOME/.cpctl}"
        export no_proxy="127.0.0.1,localhost" NO_PROXY="127.0.0.1,localhost"
        unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
        mkdir -p "$CSD_STATE_DIR"

        if SID=$("${CPCTL[@]}" session start --agent claude_code \
                   --title "Path check from $(hostname)" --project preflight \
                   --workspace "$PWD" 2>/dev/null); then
          echo "  [ok] session started: $SID"
          if "${CPCTL[@]}" event -m "First event from the agent host" >/dev/null 2>&1; then
            echo "  [ok] event reported"
          else
            echo "  [warn] the first event did not report (exit $?)"
          fi
          if "${CPCTL[@]}" event -m "Second event" \
               --payload "stage=preflight,host=$(hostname)" >/dev/null 2>&1; then
            echo "  [ok] event with payload reported"
          fi
          if "${CPCTL[@]}" session finish --status completed \
               --summary "agent-host-preflight --write from $(hostname)" >/dev/null 2>&1; then
            echo "  [ok] session finished (completed)"
          fi
          echo "         Look for it in the dashboard, newest first."
        else
          echo "  [FAIL] cpctl could not create a session."
          echo "         Re-run without 2>/dev/null to see why: it exits 3 on a delivery"
          echo "         failure, and prints the 401 detail if the token is stale."
          fail=1; blockers+=("cpctl could not write")
        fi
      fi
    fi
  else
    echo "  [FAIL] could not establish the tunnel (ExitOnForwardFailure)."
    echo "         A local port collision is the common cause; try CSD_TUNNEL_PORT=18781."
    fail=1; blockers+=("tunnel would not come up")
  fi
else
  echo "  [skip] no credential — this is the leg that proves the path, so it is untested"
fi

# --- RESULT ------------------------------------------------------------------ #
echo
hr
if [ "$fail" -eq 0 ]; then
  printf '%b\n' "${GREEN}RESULT: the path holds.${NC}"
  echo
  if [ "$WRITE" = 1 ]; then
    echo "A session from this host is in the dashboard. Next: make it continuous — run the"
    echo "tunnel under systemd (Restart=always, or autossh), then wire CC's hooks to cpctl."
    echo "Note that CC's \`Stop\` hook fires after every reply, so it must NOT map to"
    echo "\`session finish\`; \`SessionEnd\` is the one that ends a session."
  else
    echo "Nothing was written. Re-run with --write to prove the ingest path end to end."
  fi
else
  printf '%b\n' "${RED}RESULT: the path is not usable yet.${NC}"
  echo
  for b in ${blockers[@]+"${blockers[@]}"}; do echo "  - $b"; done
  echo
  echo "The legs are ordered: each depends on the ones above it. Fix the first blocker and"
  echo "re-run — the later legs cannot be judged until it passes."
fi
hr

exit "$fail"
