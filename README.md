# Agenest

English · [简体中文](README.zh-CN.md)

![Agenest](docs/social-preview.png)

**One page that watches coding agents, and one page that talks to yours:**

```
agent runs → cpctl reports → cloud persists → SSE pushes → phone shows

you type   → the agent page → your personal agent → it writes code, and may ssh out to drive others
```

The first answers exactly one question — *what are my coding agents doing right now, without
SSH-ing into the box* — and deliberately answers nothing else: no orchestration, no remote
commands, no inferred agent state.

The second is not observability and is not described as such. It is a channel onto a personal
agent that runs beside this service; that agent does the work, with your keys. Agenest never
dials a remote machine — its one client connection is to the personal agent beside it.
[docs/design.md](docs/design.md) records why each boundary sits where it does, and what was
deliberately left out.

## Screenshots

<table>
  <tr>
    <td align="center"><img src="images/performance1.jpg" width="260" alt="Overview"><br><sub>Overview — running, needs attention, finished</sub></td>
    <td align="center"><img src="images/performance2.jpg" width="260" alt="Sessions"><br><sub>Sessions grouped by host → harness → project</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="images/performance3.jpg" width="260" alt="Session detail"><br><sub>Session detail — outcome, workspace, timeline</sub></td>
    <td align="center"><img src="images/performance4.jpg" width="260" alt="Agent"><br><sub>Agent — a working conversation with your personal agent</sub></td>
  </tr>
</table>

## Why Agenest

People now drive several sessions — often several coding agents — across several machines, and
those sessions belong to different tasks. Checking on them by walking back to a desk and
polling each one is an expensive tax on attention.

The abstraction I wanted is two layers: **a coding agent is responsible to its project, and a
personal agent is responsible to you.** You should be able to see every coding agent's state
from wherever you are and direct them through one personal agent, instead of visiting each
session and talking to each coding agent face to face.

The usual home for a personal agent is an IM platform. That is convenient for collaborative
work, but a poor fit for supervising coding agents: an IM client cannot give their state the
interface it needs.

Agenest is that interface. Deploy it next to your personal agent, connect your coding agents,
reach it over Tailscale or however you prefer, and watch all of them from your phone. If you
already run a personal agent, connect it; if you do not, Agenest ships one (`native`, a local Pi
process) that needs nothing but a provider key.

## Supported agents

| Layer | What works today |
|---|---|
| Coding agents (observed and driven) | Claude Code (hooks), Codex (hooks), DeepSeek Harness / `dsh`, and standalone Pi — reporters wired by the same `csd-agent install` — or anything that can call `cpctl` from a wrapper |
| Personal agents (the channel) | `native` — the built-in personal agent, **built on Pi** (`@earendil-works/pi-coding-agent`); nanobot; OpenClaw; Hermes Agent |

The built-in `native` agent is a Pi process this service starts itself. **Pi is the agent loop;
Agenest is the dashboard and the channel around it** — not a replacement for Pi, and not an
agent implementation of its own.

A new coding agent needs a reporter (`cpctl`, a wrapper, or an agent-native plugin — `sdk/dsh-csd-plugin`
is the DSH example), not a change here. A new personal agent is a backend module in
`backend/app/agent/backends/` — the registry finds it by scan.

---

## Status

Working, and used daily on a private deployment. The backend's contract suite (308 tests)
passes; the frontend typechecks and builds clean; each of the four agent backends has been
exercised against a real instance (a Pi process, a nanobot gateway, OpenClaw Gateway 2026.10.4,
Hermes API server v0.18.2). The wire-level findings are in
[docs/agent-onboarding.md](docs/agent-onboarding.md).

The second page, the **Agent** tab, is the newest part and the most likely to change. What is
deliberately not built yet is recorded at the end of [docs/design.md](docs/design.md).

## Requirements

| | |
|---|---|
| Backend | Python 3.11+ — FastAPI, uvicorn, SQLite, no database server, no broker |
| Frontend | Node 18+ and npm; builds to static files the backend serves |
| `sdk/cpctl`, `sdk/csd-agent` | Python 3.10+, **zero runtime dependencies** |
| Agent tab (`native`, the default) | a `pi` binary (`npm i -g @earendil-works/pi-coding-agent`, Node 22.19+) and one provider key |
| Agent tab (`nanobot` / `openclaw` / `hermes`) | an instance you already run |

Everything except the optional agent can be run locally with one script; there is no container
image and no external service to stand up.


---

## Quick start

Everything happens on your machine. The cloud instance is a deployment target, not a
dependency — `./dev.sh` never touches it, and nothing you do locally can reach it (different
database, different token, different host).

```bash
./dev.sh start     # backend on :8780, frontend on :5173 with hot reload
./dev.sh status    # is it up?
./dev.sh logs      # tail the backend
./dev.sh stop
./dev.sh restart   # data survives
./dev.sh token     # the ingest token, for cpctl
./dev.sh access-token  # the page token, for the Agent tab
./dev.sh reset     # wipe local data (asks first)
```

First run creates the venv, installs deps, and generates `backend/data/dev-token` and
`backend/data/dev-access-token`. Subsequent runs reuse all of it.

The Agent tab needs an agent to talk to. By default that is a local Pi process, which needs
`npm i -g @earendil-works/pi-coding-agent` and a provider key in `.env` — `dev.sh` says so if
it cannot find one. To use a nanobot instead, ask it to connect itself (§25): its gateway
already serves the door on `127.0.0.1:8765`, and nothing on its side needs configuring or
restarting. It appears in the settings panel as `nanobot`.

The local agent gets an empty scratch directory unless you point `CSD_AGENT_WORKSPACE` at real
code — deliberately, since it can run commands.

| | |
|---|---|
| UI (hot reload) | http://127.0.0.1:5173 |
| API | http://127.0.0.1:8780 |
| API docs | http://127.0.0.1:8780/api/docs |
| Database | `backend/data/csd.sqlite3` |
| Logs / pids | `backend/data/run/` |

Open **:5173**, not :8780 — the Vite dev server reloads on save and proxies `/api` to the
backend, so the browser never needs CORS and you never hardcode an API address.

## Install on your phone

The page is a PWA: same URL, installable, standalone, and updated on the next launch. No app
store, no build per platform.

1. **Deploy it somewhere HTTPS.** The local dev server is not enough — service workers (and
   Android's install prompt) require a secure context. The supported path is Tailscale; see
   [Deployment](#deployment) for the full install, then:
   ```bash
   sudo tailscale serve --bg 8780            # tailnet only
   # or: sudo tailscale funnel --bg 8780     # public; set CSD_DASHBOARD_TOKEN first
   ```
2. **Open the `https://<host>.<tailnet>.ts.net/` URL on the phone.** Enter the access token
   once, or use `https://<host>.<tailnet>.ts.net/#token=<token>`.
3. **Install it.** iOS: Safari → Share → **Add to Home Screen**. Android: Chrome → ⋮ →
   **Install app**.
4. **Use it.** Close and reopen after a deploy to pick up the new build; the data itself is
   never cached, so what you see is always what the server last reported.

The installed app has its own storage container: a token entered in a browser tab does not
necessarily carry over, so enter it once inside the app. A raw `http://<LAN-IP>:8780` opens the
page but cannot install or work offline.

### Driving it from the CLI

```bash
export CSD_BASE_URL=http://127.0.0.1:8780
export CSD_INGEST_TOKEN=$(cat backend/data/dev-token)
cd sdk/cpctl
PYTHONPATH=. ../backend/.venv/Scripts/python -m cpctl session start   --agent claude_code --title "Fix the flaky test" --project nanobot
```

### When you're happy with it

Deploy is a separate, explicit step — see [Deployment](#deployment).

## Using `cpctl`

`cpctl` reports; it never runs the agent. Every command is a standalone process, so it drops
into any launcher script, Makefile, or CI step.

### The lifecycle

```bash
# 1. Start — prints the session id, caches it for this directory
cpctl session start --agent codex --title "Implement auth" --project myproj

# 2. Report as work happens
cpctl event -m "Reading auth.py" --payload "stage=reading"
cpctl event --type heartbeat --message "ping"
cpctl event -m "Tests green" --payload "stage=testing,tests=42"

# Blocked on a human? Say so — this is what puts the session under "needs attention",
# and the next event of any kind clears it again.
cpctl event --type awaiting_user -m "needs approval to run the migration"

# A sentence the agent wrote while working — not a status, not a plan. `source` says where
# it came from, so the page can never draw a machine fact as the agent's own words.
cpctl event --type checkpoint -m "Now I'll add the cursor test" \
  --payload '{"source":"prose","turn":3}'

# The *work* succeeded or failed, whatever the run did. `task done` / `task failed` are
# verdicts, not statuses: an agent that exits 0 can still have failed the task, and a run that
# is already closed can still receive one. The newest verdict wins; no verdict means nobody
# claimed either way.
cpctl task done   -m "12 tests pass"
cpctl task failed -m "pytest -q: 3 failed"

# 4. Finish with the real outcome
cpctl session finish --status completed --summary "12 tests pass" --exit-code 0
cpctl session finish --status failed     --summary "blocked on upstream" --exit-code 1
```

`cpctl event` and `cpctl task` use the cached session id, so the common case is just `-m`.
When a connector (csd-agent / Pi / DSH / nanobot) created the session, `cpctl` resolves it from
`~/.csd/state/sessions.d/` instead — the agent does not have to know an id. `csd-agent skill
install` writes the launcher and the Claude Code skill that tell an agent when to call this.

### Wrapping an actual agent

```bash
#!/usr/bin/env bash
# run-agent.sh — report an agent's whole lifecycle without changing how it runs.
set -uo pipefail   # deliberately not -e: reporting must never abort the agent

export CSD_BASE_URL="http://127.0.0.1:8780"
export CSD_INGEST_TOKEN="${CSD_INGEST_TOKEN:?set the ingest token}"

SESSION=$(cpctl session start --agent codex --title "$*" --project "$(basename "$PWD")") || {
  echo "cpctl: could not create a session; running the agent anyway" >&2
  SESSION=""
}

if [ -n "$SESSION" ]; then
  trap 'cpctl session finish --session "$SESSION" --status interrupted' INT TERM
fi

codex "$@"
STATUS=$?

if [ -n "$SESSION" ]; then
  case $STATUS in
    0)   cpctl session finish --session "$SESSION" --status completed --exit-code "$STATUS" ;;
    130) cpctl session finish --session "$SESSION" --status interrupted --exit-code "$STATUS" ;;
    *)   cpctl session finish --session "$SESSION" --status failed --exit-code "$STATUS" ;;
  esac
fi
exit "$STATUS"
```

`run-agent.sh` in this repo is that script, ready to copy.

### The rules it follows

- **It cannot block the agent.** Short timeout (3 s), bounded exponential-backoff retries
  with jitter, then a non-zero exit. A failed report never cancels the work.
- **It is idempotent.** Every event carries a key derived from (session, type, message), so
  a retry after a timeout inserts nothing rather than a duplicate row.
- **It reports what it knows.** No terminal-output parsing, no inferred progress. If the
  agent did not say it, `cpctl` does not.

Full flag reference: `cpctl --help`, `cpctl session start --help`.

---

## Connecting a real agent

### First: prove the path exists

The dashboard never dials out ([design.md](docs/design.md) §1) and the cloud binds loopback,
so the *agent host* has to originate the connection. `sdk/agent-host-preflight.sh` tests each
leg of that path in order — reachability, credential, remote service, tunnel + read, and
optionally the write — and names the one that broke instead of failing vaguely.

```bash
scp sdk/agent-host-preflight.sh you@agent-host:~/
ssh you@agent-host '~/agent-host-preflight.sh'           # probe only, writes nothing
ssh you@agent-host '~/agent-host-preflight.sh --write'   # ... and push one session
```

Run it **on the agent host**, not on your laptop: that is where the path starts, and the
laptop's path to the cloud says nothing about the agent host's. If it has no credential yet,
the script prints the scp-able steps to mint one that is scoped to a single port forward.

### Option A: `csd-agent` (Claude Code, DeepSeek Harness and Pi, and the supported path)

[sdk/csd-agent](sdk/csd-agent/) owns the whole agent-host side — the tunnel, the credentials,
the liveness probe, the Claude Code hooks, the DeepSeek Harness and Pi reporters, and the
task-verdict launcher/skill. One command on the agent host, no sudo, no pip:

```bash
scp -r sdk/csd-agent sdk/cpctl sdk/dsh-csd-plugin sdk/pi-csd-extension you@agent-host:~/csd-src/
PYTHONPATH=~/csd-src/csd-agent python3 -m csd_agent install \
    --cloud <dashboard-host> --source ~/csd-src/csd-agent
```

It writes the systemd user units (with the `ServerAliveInterval`/`Restart=always` pair that
makes the tunnel self-healing rather than silently half-open), merges four hook entries into
`settings.json` without touching yours, enables a five-minute liveness probe, and verifies the
result instead of assuming it. `csd-agent status` reports what it believes; `csd-agent
uninstall` reverses it.

**If DeepSeek Harness is installed on the same machine, the same command wires it too** — one
dependency and one bundle entry per existing DSH profile, installed from a tarball under
`~/.csd` rather than linked to this checkout. `--no-dsh` opts out; `--dsh-profile web` restricts
it. A host that runs DSH but not Claude Code can use `csd-agent dsh install|status|uninstall`
directly. See [its README](sdk/dsh-csd-plugin/README.md) for what it reports.

**Standalone `pi` is wired by the same command, as one copied file.** Pi has no plugin manager:
it loads every `*.js` in `~/.pi/agent/extensions/` at startup, so `csd-agent install` copies
[csd-reporter.js](sdk/pi-csd-extension/) there and `uninstall` deletes only a file carrying its
marker. A **run** — one prompt and everything Pi does for it — is one session, and its verdict
is Pi's own `stopReason`. `--no-pi` opts out, `--pi-home` points at another agent directory, and
`csd-agent pi install|status|uninstall` is the Pi-only path. The Agent tab's `native` personal
agent is the same binary and stays silent: its children carry `CSD_AGENT=1`, which the extension
honours by registering nothing. See [its README](sdk/pi-csd-extension/README.md).

**If the coding agent runs on the dashboard's own machine**, install it with `--local` instead
of `--cloud`: there is nothing to tunnel to, so no `csd-tunnel.service` is written and the
connector talks straight to loopback. Same hooks, same probe, same registration.

```bash
PYTHONPATH=~/csd-src/csd-agent python3 -m csd_agent install \
    --local --source ~/csd-src/csd-agent
```

**Either way the host shows up in the rail as it installs** — a machine that has connected is
drawn even with nothing running on it, which is the whole point of that level. It is reported
on every heartbeat, so it also keeps saying so; a box that is switched off stops being heard
from and reads as "maybe offline" rather than as a status nobody sent.

See [its README](sdk/csd-agent/README.md) for what the probe may and may not say, and why
Claude Code's `Stop` hook reports a turn boundary instead of ending the session.

### Option B: wrap it (no changes to the agent)

[run-agent.sh](run-agent.sh) — copy it, point it at any agent command:

```bash
export CSD_BASE_URL=http://127.0.0.1:8780
export CSD_INGEST_TOKEN=<token>
./run-agent.sh --title "Fix the flaky test" -- codex "fix tests/session/test_compaction.py"
```

It opens a session, traps INT/TERM to mark the session interrupted, runs the agent untouched
(no output captured or parsed), and closes with the agent's real exit code. It exits with the
agent's status, never the reporter's.

**An agent that exits 0 can still have failed the task**, and nothing in its exit status says
which. Give the wrapper a verifier and its exit code becomes a reported verdict:

```bash
./run-agent.sh --verdict "pytest -q" -- claude -p "fix the flaky test"
```

The verifier runs after a clean agent exit; a non-zero result reports a `task_failed` event and
closes the session as failed, and a passing one reports `task_completed`. It does not change
the wrapper's exit status — that remains the agent's. See [docs/design.md](docs/design.md) §40.

DSH's one-shot `headless` profile fits the same shape:
`CSD_AGENT_TYPE=dsh ./run-agent.sh -- dsh --profile headless "run the tests"`. Its interactive
web profile is what the `csd-agent` reporter above is for.

**nanobot is not a coding-session harness.** It is a *personal agent*, the thing the Agent tab
talks to, and it has no session reporter on purpose. A nanobot hook that mirrored its runs into
Sessions made the channel look like work being observed; the two roles are separate, and only
the Agent tab connects to it.

---

## agenest — a channel onto your own agent

Everything above *reports on* agents running elsewhere. agenest is the other direction: it
**talks to** one. The dashboard becomes one page of two, and the bottom tab bar switches
between them.

It is not an agent. It is a client of one, and the second tab is where you talk to it. In
nanobot's own vocabulary this dashboard is a **channel**, the same kind of thing as its
Telegram adapter, with a richer screen.

**It works out of the box.** The default backend is a local agent this service runs itself, so
all it needs is a provider key:

```bash
cp .env.example .env     # put DEEPSEEK_API_KEY (or another provider's) in it
./dev.sh start           # then open :5173 and tap Agent
```

To talk to an agent you already run instead — **nanobot**, say — ask it to connect itself
(§25). Its gateway already serves the door; nothing has to be configured or restarted:

```bash
# on the agent's machine, as the agent (what /api/agent/onboard tells it to do)
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/webui/bootstrap
curl -sS -X POST http://127.0.0.1:8780/api/agent/backends \
  -H 'Content-Type: application/json' \
  -d '{"kind":"nanobot","url":"http://127.0.0.1:8765"}'
```

The websocket channel is enabled by default; `/webui/bootstrap` mints this page's one-time
socket token, so there is no static token to place in nanobot's config and no gateway restart.
If your config sets `channels.websocket.tokenIssueSecret`, send that value as `token` in the
registration. Then switch to **nanobot** in the settings panel (the gear, top right of the
Agent tab). The door carries everything: checkpoints, tool rows, reasoning, and a real stop.

Either way the choice sticks across restarts. Each agent keeps its own conversation, so
switching swaps the transcript for that agent's — a thread talks to the agent it was started
on, and continuing it elsewhere would drop the context the new agent never had.

| | |
|---|---|
| Default backend | `native` — a Pi process this service runs (`npm i -g @earendil-works/pi-coding-agent`) |
| nanobot | `nanobot` — its gateway's websocket channel, registered by the agent itself (loopback) |
| OpenClaw | `openclaw` — its Gateway WebSocket |
| Hermes | `hermes` — its API server (HTTP + SSE), not the Desktop app's private socket |
| Which one | Settings panel, or `CSD_AGENT_BACKEND` |
| This client's id | `CSD_NANOBOT_CLIENT_ID`, for nanobot's `allowFrom` allow-list |
| Page access token | `./dev.sh access-token` — gates reads *and* everything that acts; distinct from `CSD_NANOBOT_TOKEN` |

### Configuring the local agent from the page

**Settings → Agent → the gear on the agenest row** edits the model, the credential and what the
agent may touch — provider, model, API key, a custom base URL and context window, the tool
allowlist, and whether the workspace's own `.pi` resources are trusted. Only `native` has a gear:
the other three are agents you already run, and their models and keys are their own.

Two rules worth knowing before you use it:

- **The page wins; the environment is the fallback.** Clearing a field goes back to
  `CSD_PI_PROVIDER` / `CSD_PI_MODEL` / `CSD_PI_TOOLS`, which is what "reset to environment" does
  in one tap.
- **The key is stored on the server and never shown again.** It reaches Pi through the child's
  environment rather than its command line, so it does not appear in `ps`.

A change reaches the agent on its **next message** — that is when its process is (re)started —
and never interrupts a reply already being written. The working directory stays an environment
setting: it decides both what the agent may write and where its memory lives.

### Four backends, one seam

`backend/app/agent/backends/` defines one interface; a backend turns its agent's events into
five normalized shapes (delta, message, tool call, status, notice) and everything above it —
the store, the stream, the page — never learns which agent it is talking to.

- **native** (default) — a Pi process this service runs itself. `CSD_PI_WORKSPACE` and friends
  configure it; see [.env.example](.env.example).
- **nanobot** — the gateway's **websocket channel** on loopback: the agent, its memory, its
  models, its tools and its workspace all stay over there, and this service is a client of the
  channel nanobot's own WebUI uses. It carries checkpoints, tool rows, reasoning and the stop
  command. Connecting mints a one-time token from the gateway's `/webui/bootstrap` route, so
  the agent can register the door itself with no config edit and no restart (§25) — there is
  deliberately no second, text-only nanobot backend.
- **openclaw** — OpenClaw's Gateway WebSocket, with its `tool-events` capability enabled.
- **hermes** — Hermes' API server (HTTP + SSE), the surface its docs describe for other
  frontends; the Desktop app's own socket is internal and deliberately not used. Turns are
  created through the **Runs API** (`POST /v1/runs` + `GET /v1/runs/{id}/events`) rather than
  Chat Completions, because a Chat Completions turn is not a run and cannot be stopped — the
  stop button now works (`run.cancelled` on the stream). That surface also carries tool
  completion errors and reasoning deltas. `/health` is unauthenticated, so it proves liveness
  rather than a valid key.

What the nanobot door costs is stated rather than hidden: the channel **cannot replay**, so
this page is a mirror of what it has seen rather than nanobot's whole memory — a conversation
continued from Telegram appears from that point on, and switching to this agent starts a new
transcript here (the agent's own memory is unaffected). [§17](docs/design.md) has the
reasoning; §25 has why the text-only Chat Completions door was removed.

Adding another backend means implementing one interface. It does not mean touching the page,
the database, or the stream.

### The agent page

Distilled from Telegram iOS, because that is the shape people already know for talking to
something from a phone. Messages group by sender, timestamps sit on the last message of a
group, and agent replies are rendered as **Markdown** (headings, lists, bold, code blocks).

**Reasoning and tool calls are hidden by default.** Turn them on in Settings → Conversation and
the agent's thinking and its tool calls appear inline as they happen. Off is the default
because the answer is what you came for; the machinery is available when you want it.

Two things it deliberately does not do:

**The conversation lives with the agent.** Pi is this service's own agent, so the page reads Pi's
session file — the same file Pi resumes from. Refresh after a restart and the whole conversation
is there, including the command lines of the tools it ran, and **none of it needs Pi to be
running**: a file on disk is a file on disk, and a viewer should not need a live agent to render.

That is not true of every backend, and the difference is the seam's whole point. An agent that can
hand back its transcript — OpenClaw's `chat.history`, Hermes' `/api/sessions/{id}/messages` —
shows the conversation as the agent has it. **nanobot cannot**: its `attach` subscribes a
connection without replaying history and its channel carries no history frame, so that adapter
keeps its own record and the page shows what *this channel* has seen. The same conversation
continued from Telegram appears from that point on and is not retroactively filled in.

**A tool call that is running is shown as running**, and nothing appears until it finishes. A tool
that started and never finished has no outcome, and the page will not invent one — the same rule
[§2](docs/design.md) applies to session status, at a much shorter timescale.

### Files and images

The composer has a `+` to the left of the input: **Photo** opens the picture picker, **File**
opens the file browser, and what you choose appears as a chip above the input until you send it.
A message may be a file with no caption. Images render inline in the transcript; everything else
renders as a card with its name and size. Tapping a card hands the file to the phone's share
sheet (or saves it, where a browser has no share sheet); the arrow beside it downloads it.

Both directions use the same idea: **the agent and the page are on the same machine, so a file
crosses the channel as a path, not as bytes in a protocol that has no media frame.**

- **You → agent.** The upload lands in `attachments/` beside the database, and the message the
  agent receives ends with a line naming the absolute path. It opens the file with the tools it
  already has. The transcript keeps a card on that message, not the path.
- **Agent → you.** The agent is told — in its per-turn appendix, or in a one-line note at the end
  of the prompt for a gateway agent with no appendix — to write a file into its **outbox**
  directory. When the turn ends the service moves whatever is there into the conversation as an
  attachment, and the outbox is emptied so nothing is delivered twice. Files the agent merely
  writes in its workspace are *not* sent: "the agent wrote this" is not "the agent meant to hand
  it to you" (§29).

Attachments are served only behind the access token, and the page fetches them with `fetch()`
and shows a blob URL rather than putting the token in an `<img src>` — the same rule the agent
stream follows. Uploads are capped at 25 MB (`CSD_ATTACHMENT_MAX_MB`) and written straight to
disk as they stream.

### Memory

Pi is this service's agent — we run the process, choose its arguments, and speak its protocol —
so **the conversation lives in Pi**, in its own session file. Nothing is copied here. That is what
makes a memory layer possible at all: the transcript is complete, including the command lines of
the tools it ran, which a copy kept for the page would have had no reason to hold.

Two layers, on two clocks:

- **The journal** (`memory/history.jsonl`) is written as runs finish: one entry per settled run,
  derived from Pi's record through `AgentBackend.history()`. It is derived, never appended — the
  cursor points into the conversation Pi reports — so a crash between the write and the cursor
  update converges instead of duplicating an entry, and the file can always be rebuilt.
- **Dream** is a second Pi process that reads the unconsumed entries and edits four files —
  `MEMORY.md`, `USER.md`, `SOUL.md`, `skills/<name>/SKILL.md` — with `read`, `edit` and `write`
  only. No `bash`: a curation process that can run shell commands is a second agent with your
  full privileges, and the four-file restriction would be decorative.
- **Recall re-reads on every prompt.** A small Pi extension (`backend/app/agent/pi/appendix.ts`,
  loaded with `-e`) hooks `before_agent_start` and appends the memory to the system prompt for
  each message — the same place nanobot assembles its own prompt. So a Dream reaches the agent on
  its **next message**, with no restart and no waiting for an idle moment; the file it reads is
  regenerated when a Dream commits, when Pi starts, and when a run settles.
- **Skills are passed by path.** Dream's fourth writable path, `memory/skills/`, is outside
  every location Pi scans, so the native backend passes it with `--skill`. Only each skill's
  name and description stay in the prompt; the body is read on demand.

**Dream's commit message comes from the file diff, not from the model.** It is the same model
that just edited the files, and a self-report is an audit performed by the auditee: it will say
"consolidated memory" whether it consolidated memory or truncated it. The subject names what
changed on disk; the model's own account goes in the body. Every run is a commit, so the memory
has a history you can read with `git log`.

**On by default, every 2 hours** — the same interval nanobot ships. `CSD_MEMORY_INTERVAL_H`
changes it, and `0` turns the schedule off and leaves `POST /api/agent/memory/dream` as the only
way to run one. It is on because a memory layer nobody switches on is a layer that does not
exist; the interval is the knob for what it costs.

Read the files with `ls`. That is the whole reason they are files and not rows.

**The context is compacted while idle.** There is one conversation and it never ends, so its
context only grows. Pi compacts on its own when the window is nearly full; the service also asks
it to compact after 15 idle minutes (`CSD_AGENT_IDLE_COMPACT_MINUTES`; `0` disables). Only a
process that is already running is asked, never mid-turn, and only once per new message. The page
still shows the whole conversation — compaction changes what the model sees, not what the record
says.

[§18](docs/design.md) has the rest, including why the journal could not be written by the agent
the way nanobot's is.

### Host access — letting the agent reach a registered machine

A coding-agent host dials *in*, so connecting proves it can reach you and says nothing about the
other direction. To let the personal agent run commands there, one decision and one automatic step:

1. **The host's connector declares it can be driven** (`csd-agent install` does this by default;
   `--no-drive` opts out) and says which address it believes reaches it. That declaration rides in
   on the shared ingest token, so it grants nothing — it only puts a decision in front of you,
   shown as `ssh?` on the machine in the rail and as *not authorized* in Settings → Host access.
2. **You approve it once.** The service runs `ssh-keygen`, keeps the private half at
   `$HOME/.ssh` (0600), and stores the public half.
3. **The host installs the public half itself**, on its next heartbeat, as the user its connector
   runs as. No password is ever stored or sent; that account is confirmed by you, not guessed.

Only after step 3 does the state read `ready` and the agent get the `ssh` line.

What the agent sees:

- `MACHINES.registered.md` in its workspace — generated, rewritten whenever a host registers or an
  approval changes. Lists each machine, its access state, and the exact `ssh -i … user@host` line
  when it is `ready`. Your own `MACHINES.md` (paths, which agent is installed where) sits beside it
  and is never touched.
- The **`csd_registry` tool**, for what a file cannot answer: hosts, access states, and what is
  running *now*. It reads the dashboard's database read-only from the agent's own machine — no
  token to hand out. Try “what hosts can you reach?” or “what's running on lab-box?” on the agent
  page.

Two things to know before relying on it:

- **The address must be reachable from the dashboard.** A machine behind NAT can dial out and still
  be undialable; the connector reports a Tailscale IP when it has one, and you can correct the
  address while approving. A reverse tunnel is not built — see the note in [CLAUDE.md](CLAUDE.md).
- **Revoking** deletes the key here immediately; the host removes its `authorized_keys` line on its
  next heartbeat. If you uninstall a connector before that, delete the `csd-agent:<label>` line by
  hand.

[§24](docs/design.md) has the reasoning, including why declaring and authorizing are two tables and
why the key is generated rather than a password collected.

### Your agent can connect itself

You do not have to configure the agent page from the server side. Tell your agent to connect, and
it can do the whole thing from its own shell — no token to hand it: **[§25](docs/design.md)**.

```
user:   connect yourself to agenest — http://127.0.0.1:8780
agent:  reads  GET /api/agent/onboard            (written for an agent; exact commands)
        checks the door it already serves        (for nanobot: 127.0.0.1:8765, on by default)
        POST /api/agent/backends                 (says where it is; gets probed immediately)
user:   Settings → Agent → switch to it          (the one call that needs your token)
```

Three things make it safe to hand a model:

- **Only loopback addresses are accepted.** A personal agent is a local component, so a remote
  address is refused outright — which removes "point the page at somebody else's server" by
  construction instead of by another credential.
- **The agent never gets your token.** Registration only *declares* a door, so it is open; the
  call that decides which agent receives what you type is still behind the page credential, and
  stays your click (§22, §25).
- **Opening the door changes nothing on the agent's side.** For nanobot the door is the
  gateway's own websocket channel, enabled by default; this service mints a one-time connection
  token from `/webui/bootstrap`. No config edit, no gateway restart, nothing an update can
  overwrite — and no text-only fallback to give up checkpoints for.

There is nothing to install on the agent's side: the door this page uses is the one nanobot's
gateway already serves.

Registration shows up in Settings → Agent, and removal is `DELETE /api/agent/backends/<kind>`
(refused while it is the active one — switch away first).

### Security

Every `/api/agent/*` route that drives the agent needs the same access token as the rest of the
page, and it must **exist**: with no token configured those routes answer 403, disabled, while
reads stay open. The three self-onboarding routes (`GET /api/agent/onboard(.json)`, `POST
/api/agent/backends`) are open on purpose — they only declare, and an agent cannot be asked for
a credential before it can read how to introduce itself (§25). Inside that, the promise is
narrower than it looks — the token lets you *talk to* your agent, and what that agent may do is
decided by the agent's own configuration (its workspace, its `MACHINES.md`) plus the ssh access
you approved per host (§24). This service still runs nothing and dials nothing: it mints a key
for an approved host and tells the agent where it is; the `ssh` is the agent's own.

- **Never expose `/api/agent/*` through Tailscale Funnel.** The read token can be public
  because reading is all it does. This one can drive an agent with file and shell access.
- **`CSD_NANOBOT_TOKEN` is nanobot's credential**, not ours. Leaking it grants nothing on
  this host; it authenticates this client to the gateway.
- The agent stream is consumed with `fetch()` rather than `EventSource`, so this token
  travels in a header instead of a URL.
- **Attachments are behind the same token**, and the page fetches their bytes the same way —
  with a header, then a blob URL. An `<img src="…?token=…">` would put the credential that can
  drive the agent into browser history and every proxy log on the path. The download route
  also sends `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`: an
  uploaded HTML file must not render in this origin.

---

## Configuration

All via environment variables; nothing is hardcoded. See [.env.example](.env.example) for
the annotated list.

| Variable | Default | Purpose |
|---|---|---|
| `CSD_INGEST_TOKEN` | *(required)* | Bearer token for writes. Server refuses to start without it. |
| `CSD_HOST` / `CSD_PORT` | `127.0.0.1` / `8780` | Bind address. Loopback until you deliberately widen it. |
| `CSD_DB_PATH` | `backend/data/csd.sqlite3` | SQLite file. The only durable state. |
| `CSD_STATIC_DIR` | *(unset)* | Path to `frontend/dist`. Unset serves the API only. |
| `CSD_DASHBOARD_TOKEN` | *(unset)* | The one token for the page: reads, `close`, and everything under `/api/agent/*` that drives the agent (the three self-onboarding routes are open — §25). Unset leaves reads open and everything that *acts* disabled (403). |
| `CSD_STALE_AFTER_SECONDS` | `900` | Quiet period before the UI says "maybe offline". |
| `CSD_AGENT_PROGRESS` | `1` | Ask the agent for one progress line per stage. `0` turns the narration off. |
| `CSD_AGENT_MACHINES_FILE` | `<workspace>/MACHINES.md` | Your own notes about the machines the agent may `ssh` to (`cd` targets, which agent is installed); appended to its system prompt every message. Write it by hand. |
| `CSD_AGENT_KEY_DIR` | `$HOME/.ssh` | Where keys minted for approved hosts are kept (0600). `$HOME` is the agent's own home under systemd, so the default is where `ssh` already looks. |
| `CSD_ATTACHMENTS_DIR` | `<db dir>/attachments` | Where files carried by the conversation live: uploads, the agent's outbox, and delivered files. |
| `CSD_ATTACHMENT_MAX_MB` | `25` | Largest single file in either direction, enforced while the upload streams. |
| `CSD_AGENT_BACKEND` | `native` | Which agent to talk to: `native` (local Pi) or `nanobot` (a gateway). Switchable at runtime. |
| `CSD_PI_ENV_KEYS` | see `.env.example` | Provider keys forwarded to the local agent. |
| `CSD_NANOBOT_WS_URL` | `ws://127.0.0.1:8765/` | The nanobot gateway's websocket channel (only used with `CSD_AGENT_BACKEND=nanobot`). |
| `CSD_NANOBOT_CLIENT_ID` | `csd-dashboard` | This client's id, for nanobot's `allowFrom`. |
| `CSD_NANOBOT_TOKEN` | *(unset)* | nanobot's own channel token, when it requires one. |
| `CSD_PI_BIN` | `pi` | *native only.* The Pi command, as argv (shlex-parsed). |
| `CSD_PI_ARGS` | *(unset)* | *native.* Extra argv after `--mode rpc`. |
| `CSD_PI_WORKSPACE` | *(unset)* | *native.* The directory the agent works in. Set it deliberately. |
| `CSD_PI_SESSION_DIR` | `<db dir>/pi-sessions` | *native.* Where Pi keeps its JSONL sessions. |
| `CSD_PI_PROVIDER` / `CSD_PI_MODEL` | *(unset)* | *native.* Pi defaults to `google`, so set at least one. |
| `CSD_PI_TOOLS` | *(unset = all)* | *native.* Pi `--tools` allowlist; `read,grep,find,ls` for read-only. |
| `CSD_PI_COMMAND_TIMEOUT` | `30` | *native.* Seconds to wait for Pi to acknowledge a command. |
| `CSD_AGENT_IDLE_COMPACT_MINUTES` | `15` | *native.* Idle minutes before Pi is asked to compact its context. `0` disables it. |
| `CSD_AGENT_IDLE_COMPACT_CHECK_SECONDS` | `60` | *native.* How often that idle scan runs. |
| `CSD_MEMORY_DIR` | `<CSD_PI_WORKSPACE>/memory` | Where the agent's memory files live. |
| `CSD_MEMORY_INTERVAL_H` | `2` | Hours between automatic Dream runs. `0` means it never runs on its own. |
| `CSD_MEMORY_DREAM_BATCH` | `20` | Journal entries one Dream run reads. |
| `CSD_MEMORY_JOURNAL_MIN_CHARS` | `400` | Runs shorter than this are not journalled. |
| `CSD_MEMORY_MODEL` | *(unset = the agent's)* | Model Dream runs on. |

---

## API

All under `/api`. Interactive docs at `/api/docs`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/api/sessions` | ingest | Create a session |
| `GET` | `/api/sessions` | read | List, running first; `?status=&agent=&limit=&offset=` |
| `GET` | `/api/sessions/{id}` | read | Detail with recent events |
| `PATCH` | `/api/sessions/{id}` | ingest | Update status / metadata / summary |
| `POST` | `/api/sessions/{id}/close` | access | Mark a session interrupted by hand |
| `POST` | `/api/sessions/{id}/dismiss` | access | Mark it read — it leaves needs-attention |
| `POST` | `/api/sessions/{id}/events` | ingest | Report one event |
| `GET` | `/api/sessions/{id}/events` | read | Event history, newest first |
| `POST` | `/api/hosts` | ingest | Report a connected agent host, its disconnect, or that it may be driven |
| `GET` | `/api/hosts` | read | Connected hosts, most recently seen first, with their access state |
| `POST` | `/api/hosts/{id}/access` | access | Approve the agent's `ssh` access to one host (mints a key) |
| `DELETE` | `/api/hosts/{id}/access` | access | Revoke it, deleting the key |
| `GET` | `/api/hosts/access` | ingest | *connector:* is there a key waiting for me? |
| `POST` | `/api/hosts/access/installed` | ingest | *connector:* I installed it (or here is why I could not) |
| `GET` | `/api/events` | read | SSE stream; `?cursor=<event id>&session_id=<id>` |

### agenest

All of these need the access token (`CSD_DASHBOARD_TOKEN`), and every one of them answers 403 when
it is not configured at all.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/agent/onboard` | How an agent connects itself — Markdown, written for a model. **No token** |
| `GET` | `/api/agent/onboard.json` | The same document as data. **No token** |
| `GET` | `/api/agent/backends` | Endpoints an agent registered (never returns tokens) |
| `POST` | `/api/agent/backends` | An agent says where it can be reached; loopback only, probed on the spot. **No token**; a *different* address for the active kind is 409 |
| `DELETE` | `/api/agent/backends/{kind}` | Forget one; refused while it is the active backend |
| `GET` | `/api/agent/status` | Which backend, whether it is reachable, and what it reports |
| `POST` | `/api/agent/switch` | Switch and return the conversation to show, in **one** request — the page uses this. `?process=false` omits tool rows |
| `POST` | `/api/agent/session` | **The** conversation, created on first call. Idempotent — see below |
| `GET` | `/api/agent/threads/{id}` | One conversation with its messages. `?process=false` omits tool rows |
| `POST` | `/api/agent/threads/{id}/messages` | Send a prompt. **202** — accepted, not answered. Body may carry `attachment_ids` |
| `POST` | `/api/agent/threads/{id}/attachments` | Upload one file (raw body, `X-CSD-Filename`). **201** with the id to send |
| `GET` | `/api/agent/attachments/{id}` | The file's bytes, `Content-Disposition: attachment`, `nosniff` |
| `DELETE` | `/api/agent/attachments/{id}` | Forget an upload that never reached a message |
| `POST` | `/api/agent/threads/{id}/abort` | Stop the current run |
| `GET` | `/api/agent/stream` | SSE; `?thread_id=<id>` to filter. **No cursor** — see below |
| `GET` | `/api/agent/memory` | The memory files, the journal's backlog, and recent commits |
| `POST` | `/api/agent/memory/dream` | Consolidate the journal now |

`/memory` reports file **names and sizes, never contents** — these files are the agent's working
memory of you, and a status route is not where that gets shipped to a phone. It answers `404`
when the deployment has no memory directory, rather than inventing one.

`/memory/dream` answers `200` even when the run fails, and says so in the body. `advanced:
false` distinguishes *"the model call failed"* from *"memory is already current"* — two states a
status code would collapse into one, and the second is the normal, healthy outcome of running
Dream twice.

There is one conversation per agent and no endpoint that makes a second. `POST /session` asks
for it rather than creating one blindly, so the page can call it on every boot; the route is
scoped to the active backend, because a thread talks to the agent it was started on. See
[§19](docs/design.md).

Model switching is not an endpoint: nanobot's `/model` chat command is how an agent's model
is changed, and it belongs in the conversation rather than in a settings panel here.

The agent stream is the one place the API deliberately does *not* follow the dashboard's SSE
contract. There is no `?cursor=` and the frames carry no `id:`, because most of them are
incremental text that is never written down and so cannot be resumed from. Re-fetch the
thread instead; the page does this on every reconnect. See [§17](docs/design.md).

Three behaviours worth knowing, because they are easy to break by accident:

**A finished session stays finished.** A late event or a late `PATCH` cannot move a session
out of completed/failed/interrupted. Without this, the final status depends on network
arrival order — a `session_failed` racing a retried `session_completed` would flip the
verdict. Of two conflicting terminal reports, the first wins.

**Blocked-on-you is an event, not a status.** A session waiting for an answer is reported as
an `awaiting_user` event and stays `running`; `POST /api/sessions` and `PATCH` have no way to
express it. That is what lets it clear itself — the dashboard reads the newest display event,
so the next thing the session reports replaces it. Promoting it to a status would make it
sticky, and a session that was only waiting could never be reported as running again.

**Idle is the same idea, for a finished reply.** An interactive editor keeps one Claude Code
process alive for days, so `SessionEnd` cannot tell you the agent stopped working. The
connector reports `turn_completed` when a reply ends; the page shows **idle** while the status
stays `running`, and the next prompt (`progress`) clears it. A session that is open but not
working is neither "running now" nor finished, so it gets its own count in the overview and
its own filter chip in the rail.

**A checkpoint is the agent's own sentence.** Between the prompt and the reply boundary there
used to be nothing; `checkpoint` events fill that gap with text the agent wrote ("Now I'll add
the cursor test") — one per stage, not one per tool call. `csd-agent` accumulates
`MessageDisplay` and reports only when a later `PostToolBatch` proves a tool followed the
message, so an answer is never mistaken for progress. Pi does the same with
`turn_end.toolResults`; DSH uses `assistant/message` and turns its `todo/write` whole-list
snapshot into a `source: "plan"` checkpoint. Codex's hooks expose no intermediate prose, so
that reporter stays turn-level and sends none. A reporter with no prose surface sends none;
the absence is honest. See design.md §37 and §38.

**A task verdict is its own report.** `failed` used to mean two different things: the agent's
run failed (an API error, a crash) and the work failed (tests red, build broken). An agent can
exit 0 and still have failed the task — for `claude -p` that is the ordinary case. The verdict
is a `task_failed` or `task_completed` event, reported by the agent itself (`cpctl task
done|failed`, installed as a skill by `csd-agent skill install`), a verifier
(`run-agent.sh --verdict "pytest -q"`), or the orchestrator — never inferred from an exit code
or the final prose, and never expected from the agent runtime, whose vocabulary is
`error`/`stop`. It moves no status, so it can land after the session has closed. It is
projected onto the session as `last_verdict`, separately from `last_event`, because the
`turn_completed` that follows an agent's own report would otherwise hide it. A completed run
with a `task_failed` verdict sits under needs-attention, not finished; `task_completed` is the
positive half and stays where it was. See design.md §40.

**Needs-attention has an exit.** `failed` and `interrupted` rows used to offer nothing to click,
and the ordinary source of `interrupted` is the connector closing a process that vanished — so
the row asked for attention forever. `POST /api/sessions/{id}/dismiss` records that a person has
read it. The row stores the newest event id at dismissal, so a report newer than the
acknowledgement — typically a `task_failed` verdict that arrived after the run closed — asks
again; a heartbeat does not, and there is no un-dismiss route because that is the way back. It
moves no status, writes no event, and leaves `updated_at` alone; the session lands in *Finished*
(or *Running*, if it is still open) rather than disappearing. See design.md §42.

**SSE is a notification, not a record.** The stream carries `id:` (a database event id) so a
reconnecting client resumes with `?cursor=<id>`. A client that was away longer than the
replay buffer (2048 frames) re-fetches `/api/sessions/{id}/events`; the stream alone is never
assumed complete. Frames for *other* sessions reach a filtered stream as `: skip <id>`
comments, so the client's cursor keeps advancing even while it filters.

---

## Testing

```bash
cd backend
.venv/Scripts/python -m pytest        # Windows; use .venv/bin/python on Linux/macOS
```
The suite is 308 tests and needs no network, API key or model.

The suite is organised around the rules that must not regress, not around the code layout:
`tests/test_contract.py` covers auth, validation, terminal-state stickiness, idempotency,
staleness, and restart durability; `tests/test_sse.py` covers cursor resume and filtered
streams; `tests/test_agent_contract.py` covers the agent's policy, what gets stored versus
streamed, and the JSONL framing; `tests/test_nanobot_adapter.py` covers the channel protocol.
The SSE tests run against a **real uvicorn socket**, because `TestClient` runs the ASGI app to
completion and an SSE generator never completes — it would hang rather than test anything.

Neither agent suite needs a network, an API key, or a model:
`test_agent_contract.py` points `CSD_PI_BIN` at a **stub Pi written by the tests**, and
`test_nanobot_adapter.py` runs a **stub nanobot gateway** (`websockets.serve`) in-process. What
is being pinned is our side of each protocol and our policy, not either project's ability to
talk to an LLM. Two cases in there are load-bearing:

- A `\r` and a `U+2028` inside tool output must survive the transport — both are legal inside a
  JSON string and both break a reader that splits on anything but `\n`.
- nanobot sends `delta` … `stream_end` and **no** `message` frame when streaming, so the reply
  has to be assembled from the deltas. A client that waited for `message` would record every
  reply as empty while appearing to work.

Snapshot discipline: the baselines here are assertions about the contract, so they are
regenerated by changing a rule on purpose, never by re-recording current behaviour.

---

## Deployment

The supported shape is one long-lived process on a small Linux host, bound to
`127.0.0.1:8780`, serving the built frontend from the same process. `deploy/deploy.sh` performs
the whole install — venv, service user, code, static files, `/etc/csd/csd.env`, systemd unit,
restart, verification — and is re-runnable, so the same command is the upgrade path.

Day to day, **production runs a release, not your working tree**:

```bash
export CSD_DEPLOY_HOST=root@<dashboard-host>
./release.sh cut v0.2.0     # clean tree + tests + build, then tag and write releases/v0.2.0.tgz
./release.sh deploy v0.2.0  # ship that artifact; the server records it in /srv/csd/VERSION
./release.sh rollback       # re-deploy the release before the current one
```

`cut` refuses a dirty tree, so a half-finished feature cannot reach production by accident.
The manual tarball flow below remains the fallback; it produces an unversioned build, which
the installer says out loud.

```bash
# 0. build the frontend wherever npm exists (the server itself needs no node)
cd frontend && npm ci && npm run build && cd ..

# 1. ship the repo, but not local state or secrets. Keep frontend/dist: step 0 built it,
#    and the installer copies it to /srv/csd/static — it is the only thing the server cannot build.
tar --exclude='.git' --exclude='.venv' --exclude='node_modules' \
    --exclude='__pycache__' \
    --exclude='.env' --exclude='data' --exclude='backend/data' \
    -czf /tmp/agenest.tgz -C .. agenest

# 2. install on the server
scp /tmp/agenest.tgz <user>@<dashboard-host>:/tmp/
ssh <user>@<dashboard-host> \
  'rm -rf /opt/csd && mkdir -p /opt/csd && \
   tar -xzf /tmp/agenest.tgz -C /opt/csd --strip-components=1 && \
   cd /opt/csd && sudo bash deploy/deploy.sh install'
```

The `--exclude` list is load-bearing. A tarball made from a working tree otherwise carries
`.env` (your provider key) and the SQLite database and Pi transcripts (your work history).
The `rm -rf` on the staging directory matters too: the installer rsyncs from that directory,
so files a new version no longer has would keep deploying themselves.

The layout the installer produces:

```
service : csd.service, enabled at boot, 127.0.0.1:8780
data    : /srv/csd/data/csd.sqlite3
env     : /etc/csd/csd.env, mode 640 root:csd
code    : /srv/csd/backend (rsynced, tests and data excluded)
static  : /srv/csd/static  (frontend/dist)
```

It never overwrites `/etc/csd/csd.env`, so the ingest token survives an upgrade, and it never
touches the SQLite file. `deploy/README.md` has the step-by-step and the host assumptions each
step encodes.

Because the service binds loopback, reach it over a tunnel:

```bash
ssh -L 8780:127.0.0.1:8780 <user>@<dashboard-host>
# open http://127.0.0.1:8780
```

### Reaching it from a phone

A phone browser cannot hold an SSH tunnel comfortably. The usual answer is Tailscale, in two
deliberately different modes:

| Path | Reaches you at | Needs the VPN app? | Who can read |
|---|---|---|---|
| SSH tunnel | `http://127.0.0.1:8780` | no | anyone with the SSH key |
| Tailscale (tailnet) | `https://<your-tailnet>.ts.net/` | yes | devices on the tailnet |
| Tailscale **Funnel** | the same URL, from anywhere | no | anyone with the URL **and the read token** |

The Funnel path is why `CSD_DASHBOARD_TOKEN` exists and must be set. Without it, a public URL
is a public list of what you are working on. With it, `/` still serves the app shell — that is
a static bundle and always will be public — but every `/api` read is a 401 until the token is
entered, and the app prompts for it. The token is kept in `localStorage`, never compiled into
the bundle, because on a Funnel URL the bundle is public.

To avoid typing it on a phone, put it in the URL fragment:

```
https://<your-tailnet>.ts.net/#token=<token>
```

A fragment rather than a query parameter on purpose: the browser never sends it to the server,
so it stays out of access logs, proxy logs and `Referer` headers. The app adopts it on load and
rewrites the address bar with `replaceState`, so it does not survive in the history entry that
consumed it.

`csd.service` still binds `127.0.0.1`. Both paths reach it as a local port: the SSH tunnel
forwards to loopback, and `tailscale serve` proxies to `127.0.0.1:8780`.

**One cloud-specific warning.** On some hosts — Aliyun ECS is the measured case —
Tailscale's `ts-input` chain drops `100.64.0.0/10` unless it arrives via `tailscale0`, and the
provider's metadata, MQTT, OSS and DHCP-supplied DNS resolvers all live in that range. The
symptom is not "Tailscale is down" but DNS and metadata failing. Run
[`deploy/scripts/tailscale-prefly.sh`](deploy/scripts/tailscale-prefly.sh) before and after any
change there; it treats *reachability* as the verdict, because rule inspection once reported
`[ok]` through an outage. [docs/design.md](docs/design.md) §11 has the full analysis.

### Installing it as an app

`npm run build` emits a PWA: `manifest.webmanifest`, icons under `icons/`, and `sw.js`. The
service worker caches **only the app shell** — `/api` is excluded, so the data is always live or
absent — and it registers only in a production build, never under the Vite dev server. The shell
and the worker are served with `Cache-Control: no-cache` while `/assets/*` is immutable, so a
deploy cannot sit in a phone's HTTP cache; an installed app reloads once when a new worker takes
control. An app installed before that policy existed may need to be closed fully once.

- iOS: Safari → Share → **Add to Home Screen**
- Android/Chrome: the install prompt, or ⋮ → **Install app**

**The install target must be HTTPS.** Service workers (and Android's install prompt) only run in
a secure context, so use the Tailscale name, not `http://<LAN-IP>:8780`. On the machine that
runs `csd.service`:

```bash
tailscale serve --bg 8780          # https://<host>.<tailnet>.ts.net → 127.0.0.1:8780
tailscale serve status
```

That is tailnet-only. For public access, `tailscale funnel --bg 8780` — and set
`CSD_DASHBOARD_TOKEN` first (see above). MagicDNS and HTTPS certificates must be enabled in the
tailnet's DNS settings; `tailscale serve` prints the exact URL and will tell you if a
certificate cannot be issued. On older Tailscale builds, spell the target out:
`tailscale serve --bg http://127.0.0.1:8780`.

The installed app has **its own storage container**: a token entered in a Safari/Chrome tab does
not necessarily carry over. Enter it once inside the installed app. The origin is the identity —
installing from `https://<host>.<tailnet>.ts.net` and later visiting a different hostname means
a second installation with a second token.

It opens standalone, with no browser chrome and the same hash routes. If a deploy ever leaves a
phone showing stale HTML, close and reopen the app: navigations are network-first, so one online
launch fetches the new shell.

### Turning on the agent page

The agent page is **off on a deployment** until an access token exists: with none configured,
every route that drives the agent answers 403 while reads stay open (the onboarding document and
registration stay open — they only declare, §25). Enabling it installs a Node runtime, pins Pi,
gives the agent a workspace it can write and a `HOME` for its ssh keys, and turns on progress
notes:

```bash
scp deploy/scripts/enable-agent.sh <user>@<dashboard-host>:/tmp/
ssh <user>@<dashboard-host> 'DEEPSEEK_API_KEY=... bash /tmp/enable-agent.sh'
```

That script is idempotent — re-running it is the repair path — and it ends by checking both
directions of the credential: unauthenticated `/api/agent/status` must be refused, and the same
call with the token must report `native` active. Why each piece is what it is (the pinned Pi
version, the systemd hardening that had to yield to V8's JIT, the heap cap and the swapfile on a
box with none) is recorded in [docs/design.md](docs/design.md) §21.

---

## Layout

```
backend/
  app/
    api/          HTTP layer: sessions, events, the SSE stream, the agent routes
    services/     business rules (terminal-state handling, event ingestion)
    agent/        agenest: the agent clients — a backend seam, nanobot, and a local Pi
                  rpc.py is the Pi JSONL transport the `native` backend drives
                  memory.py is the `native` agent's long-term memory (journal + Dream)
                  attachments.py is files both ways: uploads, the outbox, the path marker
    schemas.py    the contract: statuses, event types, length caps, timestamp form
    stream.py     the in-process SSE broker + cursor, and the agent's cursorless one
    auth.py       the ingest token and the page's access token
    config.py     environment → Settings (fails fast, no insecure defaults)
  tests/          organised by contract not by module
sdk/
  cpctl/          the reporting CLI
  csd-agent/      the agent-host side: tunnel, liveness probe, Claude Code hooks
  agent-host-preflight.sh  proves the agent-host → cloud path, one leg at a time
frontend/         React + Vite, phone-first, hash routing, two tabs
  src/pages/AgentPage.tsx   the agenest conversation
  src/api/agent.ts          its client — fetch-streamed SSE, header credential
  public/                   manifest, service worker, icons (copied verbatim by Vite)
images/           source art for the icons and the social preview
deploy/
  deploy.sh       installer: venv, user, code, static, env, systemd, verify
  systemd/        the service unit
  scripts/        tailscale-prefly.sh, enable-agent.sh
docs/
  design.md       scope decisions, and what was deliberately left out (§17 = agenest)
  design-ui.md    the two-theme token system
dev.sh            local development: start/stop/restart/logs/reset
run-agent.sh      example wrapper: a real agent's whole lifecycle
```

---

## License

MIT — see [LICENSE](LICENSE). Third-party dependencies are MIT/BSD/Apache-2.0; there is no
vendored code in this repository.
