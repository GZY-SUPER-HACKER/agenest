#!/usr/bin/env bash
#
# release.sh — cut a version, and ship that version. Never the working tree.
#
# Why this exists (docs/design.md §30):
#
#   `deploy.sh` installs whatever is in the staging tree it is handed. The README's manual
#   flow built that tree with `tar` from the working directory, which means a half-finished
#   feature — or a `.env` someone forgot to exclude — could reach the one instance you use
#   every day. The separation that matters is not "dev machine vs cloud machine" (those were
#   already separate databases and tokens); it is **released commit vs working tree**.
#
#   So a release is an *artifact*: a tag, the frontend built from that tag, and a tarball
#   carrying both. `deploy` ships an artifact that already exists on disk, which also makes
#   rollback a re-upload rather than a rebuild of an old tree.
#
# Usage (from the repo root):
#
#   ./release.sh cut v0.2.0     # clean tree + tests + build -> releases/v0.2.0.tgz, tag v0.2.0
#   ./release.sh deploy v0.2.0  # ship that artifact to $CSD_DEPLOY_HOST
#   ./release.sh status         # what version is running there
#   ./release.sh list           # artifacts kept on this machine
#   ./release.sh rollback       # re-deploy the release before the current one
#
# `CSD_DEPLOY_HOST` is required for deploy/status/rollback, e.g.
#
#   export CSD_DEPLOY_HOST=root@<dashboard-host>
#
# `CSD_DEPLOY_SUDO` defaults to `sudo`; set it to the empty string on a host where the SSH
# user is already root and sudo is not installed.

set -uo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
RELEASES="$ROOT/releases"
HISTORY="$RELEASES/.history"
PY="$BACKEND/.venv/Scripts/python.exe"

blue()  { printf '\033[0;34m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }
yellow(){ printf '\033[0;33m%s\033[0m\n' "$*"; }
die()   { red "$*"; exit 1; }

# Same layout rule as dev.sh: Windows venvs use Scripts/, Linux uses bin/.
if [ ! -f "$PY" ]; then
  [ -f "$BACKEND/.venv/bin/python" ] && PY="$BACKEND/.venv/bin/python"
fi

usage() {
  cat <<'EOF'
usage:
  ./release.sh cut <tag>       clean tree + tests + build -> releases/<tag>.tgz
  ./release.sh deploy <tag>    ship an existing artifact to $CSD_DEPLOY_HOST
  ./release.sh status          print the version running on $CSD_DEPLOY_HOST
  ./release.sh list            list local artifacts
  ./release.sh rollback        re-deploy the release before the current one

environment:
  CSD_DEPLOY_HOST              required for deploy/status/rollback (user@host)
  CSD_DEPLOY_SUDO              command prefix for the installer (default: sudo;
                               set empty when the SSH user is root without sudo)
EOF
}

# A release has to be reproducible from the commit it names. That is only true when nothing
# is modified, staged, or untracked: an untracked source file would be silently absent from
# `git archive`, and the failure would show up as an ImportError on the server, not here.
need_clean_tree() {
  git -C "$ROOT" diff --quiet \
    || die "working tree has unstaged changes — commit or stash them first"
  git -C "$ROOT" diff --cached --quiet \
    || die "index has staged changes — commit them first"

  local untracked
  untracked="$(git -C "$ROOT" ls-files --others --exclude-standard)"
  if [ -n "$untracked" ]; then
    red "untracked files would be missing from the release:"
    printf '%s\n' "$untracked" | sed 's/^/    /' >&2
    die "commit them, or add them to .gitignore if they are local state"
  fi
}

run_tests() {
  [ -f "$PY" ] || die "no venv at backend/.venv — run ./dev.sh start once"
  blue "==> tests"
  ( cd "$BACKEND" && "$PY" -m pytest -q ) || die "tests failed — not cutting a release"
  green "    tests pass"
}

build_frontend() {
  blue "==> frontend build"
  [ -d "$FRONTEND/node_modules" ] || die "run npm install in frontend/ first"
  ( cd "$FRONTEND" && npm run build ) || die "frontend build failed"
  [ -f "$FRONTEND/dist/index.html" ] || die "build finished but dist/index.html is missing"
  green "    frontend built"
}

do_cut() {
  local tag="${1:-}"
  [ -n "$tag" ] || { usage; exit 1; }
  case "$tag" in
    v[0-9]*) ;;
    *) die "tag '$tag' should look like v0.2.0 — the version is what the page will report" ;;
  esac

  git -C "$ROOT" rev-parse -q --verify "refs/tags/$tag" >/dev/null \
    && die "tag $tag already exists — a release is immutable, bump the version"

  need_clean_tree
  run_tests
  build_frontend

  local tmp
  tmp="$(mktemp -d)"
  # No `trap ... EXIT` here: this function returns into the case statement, and an EXIT trap
  # would also fire on a later `die`. Every failure below is immediately fatal, so clean up
  # explicitly on the one path that succeeds.
  mkdir -p "$tmp/agenest"
  git -C "$ROOT" archive --format=tar HEAD | tar -x -C "$tmp/agenest" \
    || { rm -rf "$tmp"; die "git archive failed"; }

  # `frontend/dist` is ignored by git (it is a build product), so `git archive` cannot carry
  # it. Copy it in deliberately: the server has no Node, and the installer expects it.
  mkdir -p "$tmp/agenest/frontend/dist"
  cp -a "$FRONTEND/dist/." "$tmp/agenest/frontend/dist/"

  # The server records this file; `release.sh status` reads it back. Without it a deployment
  # is "some working tree, some Tuesday" — which is the thing this script exists to end.
  local commit built_at
  commit="$(git -C "$ROOT" rev-parse HEAD)"
  built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  cat > "$tmp/agenest/VERSION" <<EOF
version=$tag
commit=$commit
built_at=$built_at
EOF

  mkdir -p "$RELEASES"
  tar -czf "$RELEASES/$tag.tgz" -C "$tmp" agenest \
    || { rm -rf "$tmp"; die "could not write $RELEASES/$tag.tgz"; }
  rm -rf "$tmp"

  # Tag only after the artifact exists: a tag with no artifact is a version nobody can ship,
  # and `cut` refuses to touch an existing tag.
  git -C "$ROOT" tag -a "$tag" -m "release $tag ($(git -C "$ROOT" rev-parse --short HEAD))"

  green "=============================================="
  green " cut $tag"
  green "=============================================="
  echo "  artifact   $RELEASES/$tag.tgz"
  echo "  commit     $commit"
  echo "  built_at   $built_at"
  echo
  echo "  ship it:   CSD_DEPLOY_HOST=user@host ./release.sh deploy $tag"
}

# Not a `host="$(require_host)"` command substitution: `die` inside it would exit only the
# subshell, and the caller would carry on with an empty host. Validate, then read the variable.
require_deploy_host() {
  [ -n "${CSD_DEPLOY_HOST:-}" ] || die "CSD_DEPLOY_HOST is not set (e.g. root@your-dashboard-host)"
}

do_deploy() {
  local tag="${1:-}"
  [ -n "$tag" ] || { usage; exit 1; }

  local artifact="$RELEASES/$tag.tgz"
  [ -f "$artifact" ] || die "no artifact for $tag — run ./release.sh cut $tag first"

  require_deploy_host
  local host="$CSD_DEPLOY_HOST" remote sudo
  remote="/tmp/agenest-$tag.tgz"
  sudo="${CSD_DEPLOY_SUDO-sudo}"

  blue "==> shipping $tag to $host"
  scp -q "$artifact" "$host:$remote" || die "scp failed"

  # The staging tree is emptied first: `deploy.sh` rsyncs with --delete, and extracting a new
  # tarball over an old one leaves files the new version no longer has (§ design.md §30).
  ssh "$host" "set -e
    rm -rf /opt/csd
    mkdir -p /opt/csd
    tar -xzf '$remote' -C /opt/csd --strip-components=1
    rm -f '$remote'
    cd /opt/csd
    $sudo bash deploy/deploy.sh install" || die "deploy failed on $host — see the output above"

  local seen
  seen="$(ssh "$host" 'cat /srv/csd/VERSION 2>/dev/null || true')"
  if printf '%s' "$seen" | grep -qx "version=$tag"; then
    green "    verified: the server reports version=$tag"
  else
    red "    deployed, but /srv/csd/VERSION does not say version=$tag"
    red "    found: ${seen:-<missing — the installer ran without a VERSION file>}"
    die "refusing to record this as a successful deploy"
  fi

  mkdir -p "$RELEASES"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tag" >> "$HISTORY"
}

do_status() {
  require_deploy_host
  local host="$CSD_DEPLOY_HOST"
  blue "==> $host"
  ssh "$host" 'cat /srv/csd/VERSION 2>/dev/null || echo "unversioned (deployed from a working tree)"'
}

do_list() {
  if [ ! -d "$RELEASES" ] || ! ls "$RELEASES"/*.tgz >/dev/null 2>&1; then
    echo "no releases yet — ./release.sh cut v0.1.0"
    return
  fi
  printf '  %-20s %8s  %s\n' "VERSION" "SIZE" "BUILT"
  local f
  for f in "$RELEASES"/*.tgz; do
    printf '  %-20s %8s  %s\n' \
      "$(basename "$f" .tgz)" \
      "$(du -h "$f" | cut -f1)" \
      "$(date -r "$f" '+%Y-%m-%d %H:%M' 2>/dev/null || echo '?')"
  done
  if [ -f "$HISTORY" ]; then
    echo
    echo "  deployed (oldest first):"
    sed 's/^/    /' "$HISTORY"
  fi
}

do_rollback() {
  [ -f "$HISTORY" ] || die "no deploy history — nothing to roll back to"

  local current previous
  current="$(awk '{t=$2} END{print t}' "$HISTORY")"
  # The newest line that names a *different* version. A failed deploy is never appended, so
  # every line here corresponds to a server that came up and reported its version.
  previous="$(awk -v c="$current" '$2 != c {t=$2} END{print t}' "$HISTORY")"
  [ -n "$previous" ] || die "only $current has been deployed — no previous release"

  yellow "current: $current"
  yellow "rolling back to: $previous"
  do_deploy "$previous"
}

case "${1:-}" in
  cut)      shift; do_cut "${1:-}" ;;
  deploy)   shift; do_deploy "${1:-}" ;;
  status)   do_status ;;
  list)     do_list ;;
  rollback) do_rollback ;;
  -h|--help|help) usage ;;
  *)        usage; exit 1 ;;
esac
