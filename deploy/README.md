# Deploying

One long-lived process on a Linux host with systemd, bound to loopback by default. No
container runtime, no broker, no database server: the durable state is one SQLite file.

## What the host needs

- Linux with systemd, `python3` (3.11+), `python3-venv`, `rsync`, `tar`
- **No Node** on the server — build the frontend where npm exists and ship `frontend/dist`
- A way to reach it: an SSH tunnel, a Tailscale address, or a reverse proxy you trust

`deploy/deploy.sh` is the installer. It creates a `csd` service user, a venv at
`/srv/csd/venv`, rsyncs the backend, copies the static build, writes `/etc/csd/csd.env` **only
if absent**, installs `deploy/systemd/csd.service`, restarts, and then verifies. Re-running it
is the upgrade path.

## The short version

```bash
# 0. build the frontend on a machine that has npm
cd frontend && npm ci && npm run build && cd ..

# 1. ship the repo — without local state or secrets. Keep frontend/dist: the installer
#    copies it to /srv/csd/static, and the server has no Node to rebuild it.
tar --exclude='.git' --exclude='.venv' --exclude='node_modules' \
    --exclude='__pycache__' \
    --exclude='.env' --exclude='data' --exclude='backend/data' \
    -czf /tmp/agenest.tgz -C .. agenest

# 2. install
scp /tmp/agenest.tgz <user>@<dashboard-host>:/tmp/
ssh <user>@<dashboard-host> \
  'rm -rf /opt/csd && mkdir -p /opt/csd && \
   tar -xzf /tmp/agenest.tgz -C /opt/csd --strip-components=1 && \
   cd /opt/csd && sudo bash deploy/deploy.sh install'
```

**The `--exclude` list and the `rm -rf /opt/csd` are both load-bearing.**

- Without the excludes, a tarball made from a working tree carries `.env` (your provider key),
  the SQLite database, and Pi's session transcripts. The installer rsyncs `backend/` and would
  place them on the server; not shipping them at all is the safer rule.
- `/opt/csd` is the *staging tree* the installer rsyncs from. Extracting a new tarball over an
  old one leaves files the new version no longer has, and `rsync --delete` faithfully deploys
  them — a module removed weeks earlier can reappear as a feature. Empty the staging tree first.

## Releasing: production runs an artifact, not a working tree

The manual flow above ships whatever is in your working directory. That is fine for a one-off,
but on the dashboard you actually use every day it is the wrong default: a half-finished feature,
or an untracked file that was never committed, can reach production and there is nothing on the
server to tell you which code is running.

`release.sh` closes both gaps. A release is a **tag plus a tarball**; deploy ships the tarball
and records its version on the server.

```bash
export CSD_DEPLOY_HOST=root@<dashboard-host>

./release.sh cut v0.2.0     # refuses a dirty tree; runs tests; builds dist; tags; writes releases/v0.2.0.tgz
./release.sh deploy v0.2.0  # scp + install; verifies /srv/csd/VERSION reports v0.2.0
./release.sh status         # what version the server is actually running
./release.sh list           # artifacts kept locally, and the deploy history
./release.sh rollback       # re-deploy the release before the current one
```

What makes it safe, in order:

- **`cut` refuses a dirty tree** — unstaged, staged, or untracked files. `git archive` only
  carries tracked files, so an untracked module would be silently missing from the tarball and
  fail on the server instead of here.
- **The frontend is built from the same commit** and copied into the tarball. The server has no
  Node; a `dist/` from a different tree is how the UI and the API drift apart.
- **The version is verified, not assumed.** `deploy.sh` writes `/srv/csd/VERSION` from the
  tarball, and `release.sh deploy` reads it back over ssh. A deploy that does not report the tag
  it shipped is recorded as a failure.
- **Rollback re-uploads.** `releases/` keeps every artifact (it is gitignored), so going back is
  not a rebuild of an old checkout — it is shipping the file you already verified.

The manual `tar` flow still works; the installer prints `no VERSION file — this is a
working-tree build, not a release` and `release.sh status` will say the same.

## What the installer does, in order

| Step | Action |
|---|---|
| 1 | venv at `/srv/csd/venv`; installs `fastapi`, `uvicorn[standard]`, `httpx` |
| 2 | creates the `csd` system user and `/srv/csd/{data,static}`, owned by it |
| 3 | rsyncs `backend/` to `/srv/csd/backend` (excludes tests, data, caches) |
| 4 | copies `frontend/dist` to `/srv/csd/static` (warns if it was never built) |
| 5 | writes `/etc/csd/csd.env`, mode 640 root:csd, with a generated ingest token |
| 6 | installs and restarts `csd.service`, then verifies |

The pip index is configurable with `CSD_PIP_INDEX_URL` (useful on hosts where the default
index is slow or blocked). Ownership of `/srv/csd` must stay with the service user; a root
`sed -i` rebuilds a file as root and the service can no longer write it.

The verification step checks four things and aborts on the first failure:
`GET /api/sessions` answers 200 (or 401 when a dashboard token is set), `POST` without a
token answers **401** (anything else is a real security finding, not a quirk),
`GET /` serves the built app, and the listening socket is what the config says it should be.

## Reaching it

The default bind is `127.0.0.1:8780`, so use an SSH tunnel:

```bash
ssh -L 8780:127.0.0.1:8780 <user>@<dashboard-host>
# open http://127.0.0.1:8780
```

For a phone, Tailscale is the usual answer: `tailscale serve` for the tailnet, `tailscale
funnel` for public access. Funnel is the one deployment where `CSD_DASHBOARD_TOKEN` is
mandatory — the app shell stays public (it is a static bundle) but every `/api` read answers
401 until the token is entered.

> **If the host is an Aliyun ECS**, read `deploy/scripts/tailscale-prefly.sh` before installing
> Tailscale. Its `ts-input` chain drops `100.64.0.0/10` unless it arrives via `tailscale0`,
> and Aliyun's metadata (`100.100.100.200`), internal MQTT and **DHCP-supplied DNS resolvers**
> live in that range. The failure mode is DNS and metadata disappearing, not "Tailscale is
> down". The script treats reachability as the verdict; rule inspection alone once reported
> `[ok]` through an outage.

## Turning on the agent page

The agent page ships **off**: with no access token configured, every route that drives the
agent answers 403 while reads stay open. That is deliberate — a missing token on a route that
can act would hand out a shell.

`deploy/scripts/enable-agent.sh` is the second, explicit step. It installs Node, pins Pi,
creates the agent's workspace and `HOME`, adds the environment the unit needs, and adjusts the
sandbox (`MemoryDenyWriteExecute=false`, because V8 JIT-compiles into W+X pages; a 1 GB
swapfile, so the OOM killer does not pick the largest process).

```bash
scp deploy/scripts/enable-agent.sh <user>@<dashboard-host>:/tmp/
ssh <user>@<dashboard-host> 'DEEPSEEK_API_KEY=... bash /tmp/enable-agent.sh'
```

The key has to be given here so that it lands in `/etc/csd/csd.env`. Pi resolves credentials
from the environment it is handed; a key that works in your shell but not in the unit produces
`no API key found` from a process you cannot see.

Re-running it is the repair path. It ends by checking both directions of the credential:
`/api/agent/status` without a token must be refused, and with it must report `native` active —
and that the workspace is writable *as the service user*, because a directory's mode alone does
not tell you what the sandbox does with it.

## Letting the agent drive another machine

The agent does that the way you would: `ssh box 'cd repo && claude -p "…"'`, from its own
shell. The service is not involved and opens no outbound connection (§1, §13).

The supported path is the dashboard's own authorization (§24): the host connector declares it
can be driven, the machine appears in the rail, and you approve it once in **Settings → Host
access**. The service mints an ed25519 key under the agent's `$HOME/.ssh`; the connector
installs the public half on its next heartbeat. What the dashboard cannot check is that the
approved address is reachable *from the dashboard's host* — a machine behind NAT can dial out
and still not be dialable.

Two files live in the workspace:

- `MACHINES.md` — **yours.** Prose about paths and which agent is installed where; appended to
  the system prompt on every message. Move it with `CSD_AGENT_MACHINES_FILE`.
- `MACHINES.registered.md` — **generated.** What the registry knows and which hosts you have
  authorized. Rewritten whenever either changes; do not edit.

`~/.ssh/config` and `known_hosts` work normally because the unit points `HOME` into the
writable tree (`/srv/csd/data/agent-home`). If you move the workspace, add the new path to the
unit's `ReadWritePaths`; `ProtectSystem=strict` leaves everything else read-only.

## Rollback

```bash
cd /opt/csd && sudo bash deploy/deploy.sh uninstall    # keeps /srv/csd/data
```

Nothing else on the host depends on this service.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Service crash-loops, `No module named app` | `WorkingDirectory` must be the directory *containing* `app/` (`/srv/csd/backend`) |
| Script hangs during the venv step | pip fell back to a blocked default index; set `CSD_PIP_INDEX_URL` |
| `csd` cannot write under `/srv/csd` | ownership is root, not `csd` |
| `GET /` is 404 | `frontend/dist` was never built, or `CSD_STATIC_DIR` is unset |
| `cpctl` exits 3 immediately | `CSD_BASE_URL` points at the wrong loopback, or the token does not match |
| Installer aborts at "SECURITY: unauthenticated POST" | genuine finding — the write API is reachable without a token. Do not work around it |
| The agent page answers 403 | no `CSD_DASHBOARD_TOKEN` is configured — the designed default for a route that acts |
| Pi dies at once: `Check failed: 12 == (*__errno_location())` | `MemoryDenyWriteExecute=true` is still set; V8 cannot allocate its JIT range |
| Pi cannot write `~/.pi/agent/...` (`Read-only file system`) | the unit's `Environment=HOME=` is missing or outside `/srv/csd/data` |
| Pi reports `no API key found` | the key is in your shell, not in `/etc/csd/csd.env`; the unit does not inherit your environment |
| Everything works, then DNS/metadata vanish on Aliyun | Tailscale's `ts-input` rule. See above |

## What is verified

The backend suite (308 tests) runs without a network, an API key or a model: Pi and nanobot are
stubbed inside the tests. `tsc --build` and `vite build` are clean, and `npm run check:rail`
pins the rail's grouping.

On a real Linux host, the installer has been run twice in a row (the second run converged with
no changes); the credential pair has been checked in both directions; a Pi prompt has returned
with a tool row carrying its command; the session file has been observed under
`/srv/csd/data/pi-sessions/`; and the agent has written a file into its workspace. Ways of
running systemd hardening differ across kernel versions — `enable-agent.sh` prints what it
found rather than assuming.
