#!/usr/bin/env bash
#
# deploy.sh — install / upgrade Agenest on this box.
#
# Written to be re-runnable. Every step either succeeds or stops the script, so a partial
# install is visible rather than half-working.
#
# Assumptions this script encodes (each one is a default, not a requirement):
#   - a Debian/Ubuntu-style host with python3, python3-venv, rsync and systemd
#   - no node/npm on the server → the frontend is built locally, artifacts only
#   - bind loopback; reach it through a tunnel or a private network
#   - a pip mirror may exist only in root's config → CSD_PIP_INDEX_URL passes one explicitly
#   - /srv must stay owned by the service user → a root sed -i silently changes ownership
#
# Usage (from the repo root, on the server):
#   ./deploy.sh install          first install
#   ./deploy.sh install --token-file /path/to/token   (avoids the token in shell history)
#
set -euo pipefail

ACTION="${1:-install}"
SERVICE_USER=csd
SERVICE_ROOT=/srv/csd
VENV="$SERVICE_ROOT/venv"
DB_DIR="$SERVICE_ROOT/data"
STATIC_DIR="$SERVICE_ROOT/static"
ENV_FILE=/etc/csd/csd.env
UNIT=/etc/systemd/system/csd.service

PYTHON_SRC="$(dirname "$(readlink -f "$0")")/../backend"

blue()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }
die()   { red "FAILED: $*"; exit 1; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root (sudo ./deploy.sh $ACTION)"
}

# --------------------------------------------------------------------------- #
step_venv() {
  blue "==> 1/6  virtualenv"
  if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" || die "python3-venv missing: apt install python3-venv"
  fi
  # If the host uses a mirror that is configured only for root, pass it explicitly; a
  # non-root pip would otherwise fall back to the default index and appear to hang.
  if [ -n "${CSD_PIP_INDEX_URL:-}" ]; then
    "$VENV/bin/pip" install --quiet --upgrade pip -i "$CSD_PIP_INDEX_URL"
    "$VENV/bin/pip" install --quiet fastapi 'uvicorn[standard]' httpx websockets \
      -i "$CSD_PIP_INDEX_URL"
  else
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet fastapi 'uvicorn[standard]' httpx websockets
  fi
  # This list — not `pyproject.toml` — is what a deployment actually installs, so it has to
  # cover every runtime import. `httpx` and `websockets` are not optional: the HTTP-backed
  # backends and nanobot's channel import them at runtime, and leaving one out is invisible
  # until someone selects that backend, at which point the page blames the agent's door.
  # Verify by *importing*, not by grepping `pip list`. Two reasons, both learned the hard
  # way: `grep -q` closes the pipe on first match, so under `set -o pipefail` a successful
  # match still reports failure via SIGPIPE; and `pip list` column layout is not a stable
  # thing to grep. Importing is the thing we actually care about.
  "$VENV/bin/python" -c 'import fastapi, httpx, uvicorn, websockets' \
    || die "fastapi/uvicorn/httpx/websockets did not import — check the install output above"
  green "    venv ready: fastapi $("$VENV/bin/python" -c 'import fastapi; print(fastapi.__version__)')"
}

step_user() {
  blue "==> 2/6  service user and directories"
  id -u "$SERVICE_USER" >/dev/null 2>&1 || adduser --system --group --home "$SERVICE_ROOT" "$SERVICE_USER"
  # -o csd:csd is the whole point. root-owned files under /srv make the service unwritable
  # and git complain about "dubious ownership".
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$DB_DIR" "$STATIC_DIR"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$SERVICE_ROOT/backend" 2>/dev/null || true
  green "    user $SERVICE_USER owns $SERVICE_ROOT/{data,static}"
}

step_code() {
  blue "==> 3/6  backend code"
  [ -d "$PYTHON_SRC/app" ] || die "backend/app not found next to this script (got $PYTHON_SRC)"
  # Install to a versioned dir and symlink, so a failed upgrade can roll back by flipping
  # one symlink rather than re-uploading.
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$SERVICE_ROOT/backend"
  rsync -a --delete \
    --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
    --exclude 'tests' --exclude 'data' \
    "$PYTHON_SRC"/ "$SERVICE_ROOT/backend"/
  chown -R "$SERVICE_USER:$SERVICE_USER" "$SERVICE_ROOT/backend"
  green "    code in $SERVICE_ROOT/backend/app"
}

step_static() {
  blue "==> 4/6  frontend artifacts"
  DIST="$(dirname "$(readlink -f "$0")")/../frontend/dist"
  if [ -f "$DIST/index.html" ]; then
    # Built on the developer's machine — this box has no node and npm would OOM here.
    rsync -a --delete "$DIST"/ "$STATIC_DIR"/
    chown -R "$SERVICE_USER:$SERVICE_USER" "$STATIC_DIR"
    green "    static copied from $DIST"
  else
    red "    no frontend/dist found — serving the API only"
    red "    (build it locally: cd frontend && npm install && npm run build)"
  fi
}

step_env() {
  blue "==> 5/6  configuration"
  install -d -m 750 -o root -g "$SERVICE_USER" /etc/csd

  if [ -f "$ENV_FILE" ]; then
    green "    $ENV_FILE exists — leaving it alone (token preserved)"
    return
  fi

  local token=""
  if [ -n "${CSD_INGEST_TOKEN:-}" ]; then
    token="$CSD_INGEST_TOKEN"
    green "    using CSD_INGEST_TOKEN from the environment (not printed)"
  elif [ "${2:-}" = "--token-file" ] && [ -n "${3:-}" ] && [ -f "${3:-}" ]; then
    token="$(tr -d '[:space:]' < "$3")"
    green "    using the token from $3 (not printed)"
  else
    token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    # Deliberately NOT printing the token. It goes to stdout, which means terminal scrollback,
    # `script` logs, and CI output. Printing a credential to make it convenient is how tokens
    # end up in places they should not be; the file is the only place it belongs.
    red "    generated a NEW token. To read it (only if you need it):"
    echo "        sudo grep CSD_INGEST_TOKEN $ENV_FILE"
    echo
    red "    If you lose it, rotate: delete that line, re-run this script, restart."
    echo
  fi

  if [ ${#token} -lt 20 ]; then
    die "token looks wrong (${#token} chars) — refusing to write a weak credential"
  fi

  # 640 root:csd — systemd reads this as root before dropping privileges; the service user
  # only needs it for the group bit. Never 644: this file is the whole auth story.
  cat > "$ENV_FILE" <<EOF
# Agenest — created by deploy.sh
# The token below is the only credential. Do not copy this file anywhere.
CSD_INGEST_TOKEN=$token

# Loopback by default: the host has no firewall, so the security group is the only gate.
CSD_HOST=127.0.0.1
CSD_PORT=8780

CSD_DB_PATH=$DB_DIR/csd.sqlite3
CSD_STATIC_DIR=$STATIC_DIR
CSD_STALE_AFTER_SECONDS=900
CSD_PAYLOAD_MAX_CHARS=8000
EOF
  chmod 640 "$ENV_FILE"
  chown root:"$SERVICE_USER" "$ENV_FILE"
  green "    wrote $ENV_FILE (0600/0640 root:$SERVICE_USER)"
}

step_unit() {
  blue "==> 6/6  systemd"
  UNIT_SRC="$(dirname "$(readlink -f "$0")")/systemd/csd.service"
  [ -f "$UNIT_SRC" ] || die "unit file not found at $UNIT_SRC"
  install -m 644 -o root -g root "$UNIT_SRC" "$UNIT"
  systemctl daemon-reload
  systemctl enable csd >/dev/null 2>&1 || true
  systemctl restart csd
  sleep 2
  systemctl is-active --quiet csd || {
    red "--- csd failed to start ---"
    journalctl -u csd -n 40 --no-pager
    die "see journal above"
  }
  green "    csd.service active"
}

step_verify() {
  blue ""
  blue "==> verification"
  local port
  port="$(grep -E '^CSD_PORT=' "$ENV_FILE" | cut -d= -f2)"
  port="${port:-8780}"
  local host
  host="$(grep -E '^CSD_HOST=' "$ENV_FILE" | cut -d= -f2)"
  host="${host:-127.0.0.1}"

  # The service listens on the configured address; talk to it through that same address so
  # the check proves what is actually bound, not what we hope is bound.
  local target="$host"
  [ "$host" = "0.0.0.0" ] && target="127.0.0.1"

  # "Is it answering?" is the question, and on a deployment with CSD_DASHBOARD_TOKEN set the
  # honest answer to an *unauthenticated* GET is 401 — which is what this check used to call a
  # failure. It read 200 only because nothing had set the read token yet, so the assertion could
  # never hold on the one deployment shape that needs it most (a Funnel-published box, where the
  # read token stops being optional). Accept either, then prove reads actually work by asking
  # again with the token when there is one.
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://$target:$port/api/sessions" || echo 000)
  case "$code" in
    200) green "    GET /api/sessions (no token)  -> 200 (reads are open)" ;;
    401) green "    GET /api/sessions (no token)  -> 401 (read token enforced)" ;;
    *)   die "API not answering on $target:$port (got $code)" ;;
  esac

  local dash_token
  dash_token="$(grep -E '^CSD_DASHBOARD_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
  if [ -n "$dash_token" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' \
           -H "X-CPD-Token: $dash_token" "http://$target:$port/api/sessions" || echo 000)
    [ "$code" = "200" ] \
      && green "    GET /api/sessions (token)     -> 200" \
      || die "reads rejected with CSD_DASHBOARD_TOKEN (got $code) — the token in $ENV_FILE does not match the service"
  fi

  code=$(curl -s -o /dev/null -w '%{http_code}' "http://$target:$port/api/sessions" \
         -X POST -H 'Content-Type: application/json' \
         -d '{"agent_type":"x","title":"y"}' || echo 000)
  if [ "$code" = "401" ]; then
    green "    POST without a token          -> 401 (auth is enforced)"
  else
    die "SECURITY: unauthenticated POST returned $code, expected 401"
  fi

  code=$(curl -s -o /dev/null -w '%{http_code}' "http://$target:$port/" || echo 000)
  [ "$code" = "200" ] \
    && green "    GET /  (dashboard)            -> 200" \
    || red "    GET /  (dashboard)            -> $code  (API-only; build frontend/dist)"

  # `grep -q` here would be a trap: it exits on first match, closing the pipe, and under
  # `set -o pipefail` the SIGPIPE (141) that `ss` then gets is reported as overall failure.
  # `ss -H -tln` drops the header so a literal comparison is safe.
  if ! ss -H -tln 2>/dev/null | grep -F ":$port" >/dev/null; then
    red "    nothing appears to be listening on $port"
  else
    green "    listening on $target:$port"
  fi

  echo
  green "======================================================"
  green " installed and verified"
  green "======================================================"
  echo
  echo "  Next:"
  echo "   1. Read the token:  sudo grep CSD_INGEST_TOKEN $ENV_FILE"
  echo "   2. Reach the UI. It is bound to $host:$port, so it is NOT public — use a tunnel:"
  echo
  echo "        ssh -i ~/.ssh/id_ed25519 -L $port:$host:$port root@<this-server>"
  echo
  echo "      then open http://127.0.0.1:$port"
  echo
  echo "   3. Report from your dev machine (keep the tunnel open):"
  echo
  echo "        export CSD_BASE_URL=http://127.0.0.1:$port"
  echo "        export CSD_INGEST_TOKEN=\$(sudo grep CSD_INGEST_TOKEN $ENV_FILE | cut -d= -f2)"
  echo "        cpctl session start --agent codex --title 'hello'"
  echo
}

case "$ACTION" in
  install|upgrade)
    need_root
    step_venv
    step_user
    step_code
    step_static
    step_env
    step_unit
    step_verify
    ;;
  verify)
    step_verify
    ;;
  uninstall)
    need_root
    blue "==> uninstalling (the database in $DB_DIR is left alone)"
    systemctl disable --now csd 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    green "removed. Data still in $DB_DIR — delete it manually if you want it gone."
    ;;
  *)
    die "usage: $0 {install|upgrade|verify|uninstall}"
    ;;
esac