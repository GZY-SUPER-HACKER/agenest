#!/usr/bin/env bash
#
# Enable the agent page (agenest, the `native` backend) on an already-deployed host.
#
# A separate script from `deploy.sh` on purpose. `deploy.sh` refuses to write `/etc/csd/csd.env`
# once it exists, and the agent deliberately ships **off**: `auth.access_header` answers 403 for
# `/api/agent/*` until an access token exists. Turning it on installs a Node runtime and hands the
# agent a shell on this host, which is not something a fresh install should decide for the
# operator. So this is the second, explicit step — idempotent, so re-running it is the repair path.
#
# There is **no agent token**. One access token (`CSD_DASHBOARD_TOKEN`) gates the page, and the
# agent — like every route that acts — simply requires it to exist (docs/design.md §22). This
# script mints nothing; it reads what `deploy.sh` already wrote.
#
# Why the pieces are what they are (docs/design.md §21–§23):
#   - Pi is pinned to the version of the `pi-mono` checkout the repo treats as the authority,
#     because `rpc.py` parses its JSONL frames. `latest` is not a version for a protocol peer.
#   - The unit's `MemoryDenyWriteExecute` must be off: V8 JITs into W+X pages and the service
#     cannot start Pi at all while it is on.
#   - The agent's workspace is `/srv/code`, added to the unit's `ReadWritePaths`. An agent that
#     cannot write code is not the agent this page is for, and the boundary is a line someone
#     chose rather than "wherever the sandbox happened to allow".
#   - Its HOME is `/srv/csd/data/agent-home`, so `ssh` reads `$HOME/.ssh/{config,id_*,known_hosts}`
#     from a path the unit leaves writable.
#
# Usage:  sudo DEEPSEEK_API_KEY=sk-... ./enable-agent.sh
#
set -euo pipefail

NODE_VERSION="${CSD_NODE_VERSION:-22.23.3}"
PI_VERSION="${CSD_PI_VERSION:-0.84.1}"
PI_BIN=/usr/local/bin/pi
SERVICE_USER=csd
DATA_DIR=/srv/csd/data
WORKSPACE=/srv/code
UNIT=/etc/systemd/system/csd.service
ENV_FILE=/etc/csd/csd.env
NPM_REGISTRY="${CSD_NPM_REGISTRY:-https://registry.npmmirror.com}"

blue()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[0;33m%s\033[0m\n' "$*"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }
die()   { red "FAILED: $*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (sudo DEEPSEEK_API_KEY=... ./enable-agent.sh)"
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found — run deploy.sh install first"
grep -q '^CSD_DASHBOARD_TOKEN=' "$ENV_FILE" \
  || die "$ENV_FILE has no CSD_DASHBOARD_TOKEN. The agent requires the access token to exist
    (that is the one rule one token does not relax), so set one there first:
        echo CSD_DASHBOARD_TOKEN=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') >> $ENV_FILE"

# --------------------------------------------------------------------------- #
# 1/6  Node
# --------------------------------------------------------------------------- #
step_node() {
  blue "==> 1/6  Node"
  # Pi requires >=22.19.0, so Ubuntu 24.04's own nodejs package (18.x) is not an option. The
  # check is on the *major.minor* floor, not on `node -v` matching our pin: an operator who
  # installed a newer Node by hand should not have it replaced under them.
  if command -v node >/dev/null 2>&1; then
    local have; have="$(node -v)"
    if [ "$(printf '%s\n22.19.0\n' "${have#v}" | sort -V | head -1)" = "22.19.0" ]; then
      green "    node $have already satisfies Pi (>=22.19.0) — leaving it"
      return
    fi
    yellow "    node $have is below Pi's floor (>=22.19.0) — installing $NODE_VERSION"
  fi

  cd /tmp
  # The box cannot reach GitHub; nodejs.org is reachable and fast (≈1.3 MB/s measured). The
  # checksum is not ceremony: this is a runtime that will execute an agent's shell commands.
  curl -fsSL -O "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz"
  curl -fsSL -O "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt"
  grep "node-v$NODE_VERSION-linux-x64.tar.xz" SHASUMS256.txt | sha256sum -c - \
    || die "node tarball checksum mismatch — refusing to install"
  tar -xJf "node-v$NODE_VERSION-linux-x64.tar.xz" -C /usr/local --strip-components=1 \
      --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md
  rm -f "node-v$NODE_VERSION-linux-x64.tar.xz" SHASUMS256.txt
  green "    installed $(/usr/local/bin/node -v) into /usr/local"
}

# --------------------------------------------------------------------------- #
# 2/6  Pi, pinned
# --------------------------------------------------------------------------- #
step_pi() {
  blue "==> 2/6  Pi @ $PI_VERSION"
  local have=""
  [ -x "$PI_BIN" ] && have="$(node -e "
    try { console.log(require('/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/package.json').version) }
    catch { process.exit(0) }" 2>/dev/null)"

  if [ "$have" = "$PI_VERSION" ]; then
    green "    $PI_BIN is already $PI_VERSION"
    return
  fi
  [ -n "$have" ] && yellow "    found $have — pinning to $PI_VERSION"
  # An exact version, never `latest`: see the header. npm's own registry measured 9.8 s against
  # npmmirror's 0.32 s from this box.
  npm install -g --registry="$NPM_REGISTRY" \
      "@earendil-works/pi-coding-agent@$PI_VERSION" >/dev/null
  green "    installed $(node -e "console.log(require('/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/package.json').version)")"
}

# --------------------------------------------------------------------------- #
# 3/6  Writable paths
# --------------------------------------------------------------------------- #
step_dirs() {
  blue "==> 3/6  directories"
  # `agent-home` is Pi's HOME: the `csd` account's passwd home is /srv/csd, which
  # ProtectSystem=strict leaves read-only, and Pi writes ~/.pi/agent/models-store.json on its
  # first run. `.ssh` is created here because this is where ssh will look for keys — an agent
  # that drives a remote coding agent does it by running `ssh` itself (§23).
  install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" \
      "$DATA_DIR/agent-home" "$DATA_DIR/pi-sessions" "$WORKSPACE"
  install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR/agent-home/.ssh"
  green "    $DATA_DIR/agent-home (+ .ssh), $DATA_DIR/pi-sessions, $WORKSPACE"
}

# --------------------------------------------------------------------------- #
# 4/6  Environment
# --------------------------------------------------------------------------- #
step_env() {
  blue "==> 4/6  $ENV_FILE"
  if grep -q '^CSD_PI_WORKSPACE=' "$ENV_FILE"; then
    green "    agent section already present — leaving it alone"
    return
  fi
  [ -n "${DEEPSEEK_API_KEY:-}" ] || die "DEEPSEEK_API_KEY is empty. Pi resolves the provider key
    from *this service's* environment, so it has to be in $ENV_FILE, not in your shell:
        sudo DEEPSEEK_API_KEY=sk-... $0"

  {
    echo ""
    echo "# --- agenest: the agent page (Pi, the \`native\` backend) -------------------"
    echo "# No token of its own: \`/api/agent/*\` requires the one access token to *exist*."
    echo "CSD_AGENT_BACKEND=native"
    echo ""
    echo "# Pi reads its own provider credentials from the environment, and the environment it"
    echo "# gets is this service's — so the key belongs here."
    echo "CSD_PI_BIN=$PI_BIN"
    echo "CSD_PI_PROVIDER=deepseek"
    echo "CSD_PI_MODEL=deepseek/deepseek-chat"
    echo "DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY"
    echo ""
    echo "# Where the agent writes code. Never the deployed source, which is where Pi's cwd would"
    echo "# otherwise land — and the unit adds this path to ReadWritePaths so it is a boundary"
    echo "# someone chose, not an accident."
    echo "CSD_PI_WORKSPACE=$WORKSPACE"
    echo ""
    echo "# One short line per stage while it works, in the message that carries that stage's tool"
    echo "# calls. Default is on; 0 turns the narration off (docs/design.md §23)."
    echo "CSD_AGENT_PROGRESS=1"
    echo ""
    echo "# Which machines the agent may ssh to is prose, so it lives in a file the operator"
    echo "# writes: \$CSD_PI_WORKSPACE/MACHINES.md. Uncomment to keep it somewhere else."
    echo "# CSD_AGENT_MACHINES_FILE=$WORKSPACE/MACHINES.md"
    echo ""
    echo "# This host has no swap, so an unbounded V8 heap means the OOM killer chooses, and the"
    echo "# largest process here is somebody else's. Cap it: a runaway agent should die as itself."
    echo "NODE_OPTIONS=--max-old-space-size=384"
  } >> "$ENV_FILE"
  chmod 640 "$ENV_FILE"; chown root:"$SERVICE_USER" "$ENV_FILE"
  green "    wrote the agent section"
}

# --------------------------------------------------------------------------- #
# 5/6  Unit
# --------------------------------------------------------------------------- #
step_unit() {
  blue "==> 5/6  $UNIT"
  local changed=no
  # The repo's own unit already carries these; this is for a host whose unit predates them, so
  # that the script converges rather than assuming a redeploy happened.
  if grep -q '^MemoryDenyWriteExecute=true' "$UNIT"; then
    sed -i 's/^MemoryDenyWriteExecute=true/MemoryDenyWriteExecute=false/' "$UNIT"
    green "    MemoryDenyWriteExecute → false (V8 JIT needs W+X pages)"
    changed=yes
  fi
  if ! grep -q '^Environment=HOME=' "$UNIT"; then
    sed -i "/^EnvironmentFile=/a Environment=HOME=$DATA_DIR/agent-home" "$UNIT"
    green "    HOME → $DATA_DIR/agent-home"
    changed=yes
  fi
  if ! grep -q "^ReadWritePaths=.*$WORKSPACE" "$UNIT"; then
    sed -i "s|^ReadWritePaths=.*|& $WORKSPACE|" "$UNIT"
    green "    ReadWritePaths += $WORKSPACE (the agent's workspace)"
    changed=yes
  fi
  [ "$changed" = no ] && green "    already configured"

  # Without swap the failure mode is the OOM killer, not slowness.
  if ! swapon --show | grep -q .; then
    fallocate -l 1G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
    chmod 600 /swapfile; mkswap /swapfile >/dev/null; swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    green "    added a 1G swapfile"
  else
    green "    swap already present"
  fi

  systemctl daemon-reload
  systemctl restart csd
  sleep 3
}

# --------------------------------------------------------------------------- #
# 6/6  Verify
# --------------------------------------------------------------------------- #
step_verify() {
  blue "==> 6/6  verify"
  systemctl is-active --quiet csd || { journalctl -u csd -n 20 --no-pager; die "csd is not running"; }
  local token; token="$(grep -m1 '^CSD_DASHBOARD_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"

  # The credential's own failure mode first: an unauthenticated request to `/api/agent/*` must be
  # refused, or "enabled" would mean "open".
  local anon; anon="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8780/api/agent/status)"
  [ "$anon" = "401" ] || [ "$anon" = "403" ] || die "/api/agent/status answered $anon without a token — expected 401/403"
  green "    no token → $anon"

  local out; out="$(curl -s -H "X-CPD-Token: $token" http://127.0.0.1:8780/api/agent/status)"
  case "$out" in
    *'"active":true'*) green "    with the access token → 200, native active" ;;
    *) red "    unexpected status: $out"; exit 1 ;;
  esac

  # The workspace has to be writable *by the service user under the unit's sandbox*, which is not
  # something the directory's mode alone tells you — so it is checked as that user.
  if runuser -u "$SERVICE_USER" -- test -w "$WORKSPACE"; then
    green "    the agent can write $WORKSPACE"
  else
    yellow "    WARNING: $WORKSPACE is not writable by $SERVICE_USER — the agent cannot write code"
  fi

  echo
  green "The agent page is on. It uses the same token as the dashboard:"
  echo "    page:  <your-url>/#/agent"
  echo "    token: sudo grep CSD_DASHBOARD_TOKEN $ENV_FILE"
  echo
  yellow "To let it drive a coding agent on another machine, give it ssh:"
  echo "    install -m 600 -o $SERVICE_USER -g $SERVICE_USER ~/.ssh/id_ed25519 \\"
  echo "        $DATA_DIR/agent-home/.ssh/id_ed25519"
  echo "    then write $WORKSPACE/MACHINES.md — host names, how to reach them, where the code is."
  echo "    It is appended to the system prompt on every message."
  echo
  yellow "Pi starts as a resident process on the first prompt (not on page load) — that is"
  yellow "expected, and it is why idle compaction exists (§19)."
}

step_node; step_pi; step_dirs; step_env; step_unit; step_verify
