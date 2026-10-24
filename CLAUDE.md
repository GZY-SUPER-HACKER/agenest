# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**One page that watches coding agents, and one page that talks to yours:**

```
agent runs → cpctl reports → cloud persists → SSE pushes → phone shows
you type  → agenest → your personal agent → it writes code, and may ssh out to drive others
```

The first answers exactly one question — *what are my coding agents doing right now, without
SSH-ing into the box* — and deliberately answers nothing else: no orchestration, no remote
commands, no inferred agent state. Those three still hold for everything under `/api/sessions`.

The second is not observability and should not be described as if it were. It is a channel onto a
personal agent (below), and that agent **works**: it writes files, runs commands, and reaches other
machines by running `ssh` itself. What has not changed is who dials: **this service opens no
outbound connection of its own** (§1, §13). The agent it spawns does, on the operator's behalf, with
the operator's keys — docs/design.md §23.

It now reaches *registered* machines without a hand-placed key: a connector declares that its host
may be driven, the user approves it once, and this service generates the key the agent ssh's with
(§24). The SSH is still the agent's own.

### Two kinds of agent, and the direction each one connects

Every agent in this codebase is one of two kinds. They connect in **opposite directions**, and
conflating them is how a design discussion goes wrong:

| | **Personal agent** | **Coding agent** |
|---|---|---|
| What it is | the agent you talk to: `nanobot`, `native` (Pi), OpenClaw, Hermes | the agent that writes code: Claude Code, Codex, … |
| Role here | the operator's agent — it is what you talk to, and it drives the coding agents through its own tools | the observed, driven side |
| Direction | this service is the **client** — it dials the agent's gateway, or spawns it (`native`) | the agent host is the **client** — it reports to `/api/*` and originates the connection |
| Where it runs | **on this machine, together with this service — always** | anywhere: its own host, or this one |
| Surface | the Agent tab (agenest) | the Sessions tab |

A personal agent is a **local component of the control plane**, not a remote service: the
agenest backends exist so the page has an agent to talk to *here*, and pointing one at another
machine is not a supported deployment shape. A coding agent has the opposite freedom — it may
run anywhere, and its connection is always initiated **from its own host** (`csd-agent`,
`cpctl`, `run-agent.sh`), because the dashboard never dials out (§1, §13). "No remote commands"
above describes that second direction; it does not mean this service never opens a client
connection of its own — agenest does, to the personal agent beside it.

**[docs/design.md](docs/design.md) is the authority for every architectural rule.** It opens
with the governing convention: *if a decision is not written down, it should be treated as
not yet made.* Read it before changing behaviour; add an entry when you make a decision that
rejects the obvious option. [docs/design-ui.md](docs/design-ui.md) is the same for the UI.
The README is the operator-facing surface — keep it in sync when behaviour changes.

## Commands

Local development is one script; it never touches the cloud deployment (separate database,
token, and host).

```bash
./dev.sh start      # backend :8780, frontend :5173 (hot reload). First run creates the venv,
                    # installs node deps, and generates backend/data/dev-token
./dev.sh stop | restart | logs | status | token | reset
```

Open **:5173**, not :8780 — Vite proxies `/api` to the backend, so there is no CORS and no
hardcoded API address in the frontend.

Production does not run the working tree. A release is a tag plus an artifact, and `deploy`
ships an artifact that already exists on disk (design.md §30):

```bash
./release.sh cut v0.2.0     # refuses a dirty tree; tests; builds dist; tags; releases/v0.2.0.tgz
./release.sh deploy v0.2.0  # needs CSD_DEPLOY_HOST=user@host; verifies /srv/csd/VERSION
./release.sh status | list | rollback
```

```bash
# Tests — run from backend/. The venv is at backend/.venv (Scripts/ on Windows, bin/ on Linux)
cd backend && .venv/Scripts/python -m pytest
.venv/Scripts/python -m pytest tests/test_sse.py::test_cursor_resumes_after_last_seen_id  # single test

# Frontend (from frontend/)
npm run build       # tsc -b && vite build → dist/, which the backend serves via CSD_STATIC_DIR
npm run check:rail  # the rail's host→harness→project→session grouping; esbuild + node, no framework
npm run dev

# Seed realistic multi-harness data (needs ./dev.sh start first; reads backend/data/dev-token)
python seed_dev_data.py

# Run the backend directly. WorkingDirectory MUST be backend/ — `python -m app.main`
# resolves the `app` package from cwd. The systemd unit carries the same constraint.
cd backend && CSD_INGEST_TOKEN=... .venv/Scripts/python -m app.main
```

`cpctl` is the reporting CLI (`sdk/cpctl`); it reports, it never runs the agent.

```bash
export CSD_BASE_URL=http://127.0.0.1:8780
export CSD_INGEST_TOKEN=$(cat backend/data/dev-token)
cd sdk/cpctl && PYTHONPATH=. ../backend/.venv/Scripts/python -m cpctl status
# subcommands: session start|finish|update, event, status, list, show
```

Backend deps are installed by `dev.sh` (fastapi, uvicorn[standard], pytest, httpx, psutil);
`backend/pyproject.toml` lists them for packaging. Test config lives there too
(`testpaths=["tests"]`, `pythonpath=[".","tests"]`, `filterwarnings = error::DeprecationWarning`).

```bash
# DSH reporter (from sdk/dsh-csd-plugin/) — Node's built-in runner, no dependencies
npm test
# Pi reporter (from sdk/pi-csd-extension/) — same runner, no dependencies
npm test
```

### agenest (the second tab)

**`native` is the default** — a Pi process this service runs itself, so the page works with
nothing but a provider key. Needs `npm i -g @earendil-works/pi-coding-agent` and a key in
`.env` (`DEEPSEEK_API_KEY=…`, `CSD_PI_PROVIDER=deepseek`, `CSD_PI_MODEL=deepseek/deepseek-chat`).
`dev.sh` sources `.env`, forwards the well-known provider keys, and warns when none is present.

```bash
cp .env.example .env      # then put a provider key in it
./dev.sh start            # the Agent tab works
./dev.sh access-token     # the one token that gates the whole page
```

To talk to an agent you already run instead, switch in the settings panel (gear icon, top
right of the agent page). **nanobot has exactly one door**: its gateway's own websocket
channel on `127.0.0.1:8765`, enabled by default. The agent points us at it, and we mint a
one-time socket token from the gateway's `/webui/bootstrap` route — so connecting needs no
config edit, no static token and no gateway restart, which is what makes it registerable by
the agent itself (§25). `CSD_AGENT_BACKEND=nanobot` selects it at startup; the fallback
address/token (`CSD_NANOBOT_WS_URL`, `CSD_NANOBOT_TOKEN`) exist for an operator who wants to
skip registration. The choice persists in `meta`, so a restart does not revert it — unless the
stored choice names a door this build no longer has, in which case startup falls back and says
so. There is no second nanobot backend: the text-only Chat Completions door was removed
(§25), because it cannot carry a checkpoint and two rows called "nanobot" read as two agents.

`dev.sh` points `native` at `backend/data/agent-workspace` — **not** the repo root, because a
bash-capable agent that silently starts with write access to this project's own source is not a
default anyone should inherit. Point it at real code with
`CSD_AGENT_WORKSPACE=/your/code ./dev.sh restart`. Read-only tools: `CSD_PI_TOOLS=read,grep,find,ls`.

## Architecture

```
backend/app/
  main.py        app factory: settings → database → broker, then routers, then static mount
  config.py      env → Settings; fails fast (no CSD_INGEST_TOKEN = refuses to boot)
  auth.py        three credentials, three different failure modes (see below)
  schemas.py     THE contract: statuses, event types, length caps, timestamp form
  database.py    raw sqlite3 + WAL, no ORM; three tables + the additive migration
  stream.py      in-process SSE broker, replay buffer, db-id cursor
  api/           HTTP layer (sessions, events, hosts, SSE stream, agent)
  services/      the business rules — terminal-state handling, event ingestion
  agent/         agenest: the Pi subprocess, its supervisor, its store (see below)
backend/tests/   organised by contract, not by module: test_contract.py, test_sse.py,
                 test_agent_contract.py
frontend/        React + Vite, phone-first, hash routing, hand-written CSS
sdk/cpctl/       the reporting CLI (Python ≥3.10, zero runtime deps)
sdk/csd-agent/   the coding-agent host side: ssh tunnel, liveness probe, Claude Code hooks,
                 and the DSH reporter's installer (vendor → tarball → profile)
sdk/dsh-csd-plugin/  the DeepSeek Harness side: a native Cordis plugin that reports turns (§28)
sdk/pi-csd-extension/  the standalone `pi` CLI side: one extension file that reports runs (§31)
sdk/csd_nanobot.py, sdk/agent-host-preflight.sh
deploy/          installer, systemd unit, tailscale-prefly.sh
```

### Rules that are load-bearing and easy to break by accident

These span multiple files and are pinned by tests. Break one and the test suite is the least
of your problems.

- **Terminal states are sticky, and first-write-wins.** Once completed/failed/interrupted, no
  late event or `PATCH` moves a session. Without this the verdict depends on network arrival
  order — a retried `session_completed` racing a `session_failed` would flip the outcome.
  `unknown` is a *wall* for automatic transitions but an explicit `PATCH` may still change it.
  That rule is only worth anything if it survives **concurrent** writers, which is why the
  storage layer is part of it: one SQLite connection per thread, `write()` takes the write lock
  with `BEGIN IMMEDIATE` before it reads, and an event never writes a status it did not change
  (§35).
- **Status is reported, never inferred.** No completion from event count, elapsed time, or
  output parsing. The one inference is `possibly_stale` — a display hint computed per
  response, rendered as "maybe offline" in the quietest style so it cannot read as a status.
- **`awaiting_user` is an event, not a status.** A blocked session stays `running`; the UI
  reads the newest *display* event. That is what lets it clear itself — a status would be
  sticky and a session that was merely waiting could never be reported as working again.
- **`turn_completed` (idle) is the same shape, for a different fact.** An editor keeps one
  Claude Code process alive for days, so `SessionEnd` cannot say the agent stopped working;
  `Stop` reports the reply boundary as an event, the UI shows `idle` while the status stays
  `running`, and the next `progress` event clears it. Never a sixth status, never a
  `session finish` (§36).
- **Heartbeats renew liveness, not activity.** A heartbeat advances `last_activity_at` but
  **not** `updated_at`, and is excluded from timelines and from `last_event`. Otherwise 40
  pings during a test run bury the last thing the user actually did.
- **Idempotency is `event_key`, not timestamps.** A unique partial index plus a CLI-derived
  deterministic key from (session, type, message). Two reporters *can* legitimately report
  the same message in the same millisecond; a timestamp key would silently drop one.
- **SSE is a notification; SQLite is the record.** The `id:` is a **database event id**, so a
  cursor survives a service restart. The 2048-frame replay buffer is not history — a client
  away longer must re-fetch `/api/sessions/{id}/events`. Filtered streams still receive other
  sessions' frames as `: skip <id>` comments so their cursor never lags.
- **Subscribe before replay.** `Broker.replay_and_subscribe()` snapshots the replay *after*
  registering the subscriber. A duplicate frame is harmless (the cursor skips it); a frame
  delivered zero times is not, because the client cannot know to ask for it.
- **Reporting must never fail the work.** `cpctl` has a 3 s timeout, bounded backoff with
  jitter, exits 3 on failure. `run-agent.sh` uses `set -uo pipefail` and deliberately *not*
  `-e`, and exits with the agent's status, never the reporter's.
- **`session_frame()` must carry every field the client's `Session` type declares.** The list
  hook replaces its copy wholesale, so a field omitted from the frame is a field erased from
  the screen. `test_session_frame_carries_every_field_the_client_type_declares` pins this.
- **A host is not a session, and never becomes one.** It has its own table (`agent_hosts`),
  its own endpoints, and its own frame kind. A session's status says how work *ended*; a host
  is a machine being reachable, which has no outcome — so `completed`/`failed` would be
  meaningless on it, and every later reader of that column would have to know which rows were
  the exceptions (§20).
- **The registry is keyed on `(label, agent_type)`; the rail draws a node per *machine*.**
  One connector speaks for one coding agent, so a box running two agents has two rows — but a
  node per row would list that box's sessions once under each connector. Session
  `test_one_box_with_two_coding_agents_is_two_hosts` and `npm run check:rail` pin the two
  halves respectively.
- **A host that goes quiet is never marked disconnected.** Silence means `possibly_stale`, the
  same hint and the same hollow rendering a session gets; `disconnected_at` is written only
  when a connector says goodbye. `csd-agent uninstall` says it *before* stopping the units,
  because on a remote host the tunnel is what carries the message.
- **Host frames borrow an event id rather than minting a cursor.** `publish_host(..., after=)`
  anchors to the newest `session_events.id` so §4's promise — the `id:` you resume from is a
  database event id — stays single-valued. A second numbering space on one stream would make a
  cursor mean two things.
- **No session is ever attributed to a host that did not report one.** A session may name a
  machine (`cpctl session start --host`), and that groups it correctly, but it draws no
  connection marker. Deriving "this machine is fine" from the presence of work is the
  inference §2 exists to forbid.
- **Declaring a host is not authorizing it.** A connector's "I can be driven" rides in on the
  shared ingest token and only produces a prompt for the user; a shell is handed out by a
  `host_access` row, which requires the credential that acts (§24). The two live in different
  tables for exactly this reason.
- **The private key never leaves this side.** It is generated under the agent's `$HOME/.ssh`,
  and the database, the API responses, the generated machines file and the prompt all carry a
  *path* at most. The connector only ever receives a public key, and only ever edits its own
  line in `authorized_keys`.
- **An approval is for one address.** What the host declared at approval time is stored; if it
  starts declaring something else the state becomes `address_changed` and the old command is
  void. Comparing against the approved address instead would call the user's own override a
  host move.
- **A DSH turn is a session, and `turn/end.reason` is its verdict.** A DSH conversation is
  resumable and has no end, so a row per conversation could only ever say `running`; a turn has
  a reported outcome. Subagent child sessions belong to the parent's turn and are not reported.
  An unrecognized end reason closes as `unknown`, never `completed` (§28). This is the same
  "reported, never inferred" rule as §2, applied to a second agent's log vocabulary.
- **The DSH reporter is installed by `csd-agent install`, from a vendored tarball.** One command
  wires both agents; `dsh plugin add <checkout>` records a `link:` to the checkout and is not a
  deployment. `csd-agent dsh install|status|uninstall` is the same module without the Claude
  Code half, for a DSH-only host (§28).
- **A Pi run is a session, and the native personal agent is the same binary — so it must stay
  inert.** `sdk/pi-csd-extension/csd-reporter.js` opens on `before_agent_start` and closes on
  `agent_settled` with `AssistantMessage.stopReason` (`turn_end` is only a progress line, never a
  boundary). `native.py` sets `CSD_AGENT=1` on every Pi child it spawns, and the extension
  registers nothing when it sees it; reporting those runs would show the Agent tab talking to
  itself. The install is **one copied file** under `~/.pi/agent/extensions/` (global only — the
  project-local directory is trust-gated), and uninstall deletes only a file carrying the
  reporter's marker. Diagnostics go to stderr because in `pi --print` stdout is the answer (§31).
- **`csd-agent install` writes the long-lived pieces *the platform has*, not systemd's.** Linux:
  the two systemd user units. Windows: a Startup-folder keeper that supervises `ssh` (a
  scheduled task is killed after 72 h, which is how a tunnel silently stops on day four) and a
  Task Scheduler heartbeat every five minutes (§32). Two Windows hazards are guarded in
  `tracked.py`: `os.kill(pid, 0)` *terminates* the process on Windows, so liveness uses
  `OpenProcess`, and pid discovery falls back to the only `claude.exe` — returning "unknown"
  when there is more than one rather than guessing. `--url` installs skip the tunnel for a
  dashboard that is already published; it is not the default, because a public TLS handshake can
  exceed the hook wrapper's 2 s budget.

### The credentials, and why their defaults differ

All read from `request.app.state` (a dependency taking a dataclass as a parameter is
indistinguishable to FastAPI from a request body, which is how a security check silently
becomes optional). Sent via `X-CPD-Token` or `Authorization: Bearer`.

| Credential | Powers | Unset means |
|---|---|---|
| `CSD_INGEST_TOKEN` | `POST /sessions`, `PATCH`, events — what a *host* reports with; also the connector's two host-access routes | **server refuses to start** |
| `CSD_DASHBOARD_TOKEN` | the whole page: reads, `POST /{id}/close`, `POST|DELETE /api/hosts/{id}/access`, `/api/agent/*` **except** the three self-onboarding routes (§25) | reads **open**; anything that *acts* **403 disabled** |

**One token, two rules** — and the second row is the one to keep straight. Until 2026-10-19 there
were three page credentials (read / control / agent); they were collapsed into the access token
above, because three secrets guarding one page is a login ritual rather than a boundary. What must
not collapse is the *requirement*: `auth.dashboard_header` returns when the token is unset (reads
open, which costs nothing privately) while `auth.access_header` refuses with 403 (acting off,
because a missing token there would hand out a shell). Separate because it is not a page credential
at all: the ingest token has to be issuable to machines that must not be able to drive an agent.
See docs/design.md §22.

The read path accepts the token as `?token=` — **only** because
`EventSource` cannot set headers. No other token is, ever.

`CSD_NANOBOT_TOKEN` is a fifth value and is **not** one of ours: it is nanobot's own
credential — the `tokenIssueSecret` a registered door carries, or a static channel token on an
older gateway — it authenticates this client to nanobot, and leaking it grants nothing on this
host.

## agenest — a channel onto an existing agent

This service is a **client of an agent**, not an agent. It connects to one you already run —
`native` (a Pi process it starts itself) by default, or a nanobot/OpenClaw/Hermes instance you
point it at — and the second tab is where you talk to it. The agent on the far side is
a **personal agent**, and by the rule at the top of this file it runs on this same machine.
In nanobot's vocabulary this
dashboard is a *channel*, the same kind of thing as its Telegram adapter with a richer screen.
Read [docs/design.md](docs/design.md) §17 first: it records why the first attempt at this
(our own Pi-based kernel) was the wrong product, and what that changed.

```
backend/app/agent/
  backends/base.py     the seam: one interface + five normalized event shapes
  backends/nanobot.py  websocket client for nanobot's channel protocol  <- the door it opens itself
  backends/native.py   a Pi process this service runs itself            <- the fallback
  attachments.py       files both directions: inbound bytes, the outbox, and the path marker (§29)
  endpoints.py         where an agent said it can be reached, loopback only (§25)
  onboard.py           the document an agent reads to connect itself (§25)
  rpc.py               Pi's JSONL transport, driven by the native backend
  memory.py            journal + Dream + git audit + the system-prompt recall text
  pi/appendix.ts       the Pi extension that re-reads the appendix on every prompt
  pi/registry.ts       the Pi extension that registers the `csd_registry` tool (§24)
  pi/provider.ts       the Pi extension that registers a custom provider/endpoint (§26)
  native_config.py     the local agent's runtime config: stored in `meta`, env is the fallback
  hosts_view.py        the generated machines file + that tool's own output
  legacy.py            one-shot upgrade from the old shared transcript table
  supervisor.py        threads + storage + SSE frames; backend-agnostic
  store.py             agent_threads
backend/app/api/agent.py   REST + the cursorless SSE stream
frontend/src/pages/AgentPage.tsx, api/agent.ts, components/TabBar.tsx, styles-agent.css
```

`build_backend` is the only code that knows both backends exist. Everything above the seam —
store, stream, page — addresses `AgentBackend` and never learns which agent it is talking to.
`Supervisor` holds *all* of them with one active; each thread records its own backend, so a
conversation keeps talking to the agent it was started on. There is **one conversation per
agent** — no UI makes a second (§19) — and `POST /api/agent/session` is scoped to the active
backend so switching swaps the transcript rather than re-pointing a thread.

Rules that are easy to break:

- **`app/agent/backends/` is the seam; keep it that way.** A backend translates its agent's
  protocol into `Delta` / `Message` / `ToolCall` / `Status` / `Notice` and nothing else. If a
  backend needs a new frame shape, that is a signal the abstraction is wrong, not a reason to
  teach the UI about it.
- **A backend retires itself, and the core keeps no list of retired names.** `BACKEND_OFFERED =
  False` keeps a module registered — an explicit `CSD_AGENT_BACKEND` still builds it, and its
  protocol code and tests stay — while removing it from `available_backends()` and refusing it
  in `POST /api/agent/backend`. A stored `meta.agent_backend` naming a retired door is **not**
  honoured at startup: hiding the backend the page is actually using is worse than falling
  back. No door is retired today. Deleting a module (as `nanobot_api` was) is the stronger
  move, and startup falls back the same way because the name is no longer registered at all.
- **A switch is recorded before the old backend stops, and the stop is a task.** Awaiting it
  made the settings tap feel like slow message syncing: Pi's `stop()` can wait out its 5 s grace
  period, and the caller is waiting on the response so it can load the new transcript. A stop
  still draining is awaited before the next `start()`, so it cannot clear state underneath a
  fresh process; its failure is logged, not returned as a failed switch. Pinned by
  `test_switching_does_not_wait_for_the_previous_backends_teardown`.
- **The page's switch is one round trip, and the transcript is sent lean.** `POST
  /api/agent/switch` composes switch + session + messages, because the three primitives are
  sequential and over a phone link each costs about a second. `?process=false` drops tool rows —
  measured: 215 KB and ~9 s became 5.6 KB and ~1 s, and tool rows were 191 KB of it. The page
  passes its process-view setting and refetches when that view is turned on. Responses are
  gzipped (Starlette's middleware excludes `text/event-stream`, which is what makes it safe
  next to our SSE).
- **Loopback HTTP clients set `trust_env=False`.** `nanobot`'s token bootstrap and `hermes`
  talk to 127.0.0.1; without it httpx picks up the machine's system proxy (on Windows, from the
  registry) and sends loopback through it, and the proxy's own answer looks like "the agent's
  door is down". `tests/conftest.py` sets `NO_PROXY` for the same reason.
- **nanobot's door is its WebUI socket, and the token is minted per connection.** Connecting is
  `GET http://127.0.0.1:8765/webui/bootstrap` (Bearer = the gateway's
  `channels.websocket.tokenIssueSecret`, which a registration carries as `token`) → `{token,
  ws_url}` → `ws://…?client_id=…&token=…`. The response also carries an `api_token` for the
  gateway's whole WebUI HTTP API; **it is ignored on purpose** — one socket is not a reason to
  hold a control-surface credential. A failed bootstrap is reported as a door that did not
  open (401 names the secret), which is what the registration probe returns. The old
  Chat-Completions door is gone: it could not carry a checkpoint, and two doors for one agent
  read as two agents (§25).
- **`stream_end` ends a segment; `resuming` says whether the turn continues.** A checkpoint
  arrives as `delta … stream_end{resuming:true}` followed by a textless
  `message{kind:"progress"}` marker, and only the final segment (no `resuming`) is the answer.
  `turn_end` is what finishes the turn. Reading every `stream_end` as the answer files each
  checkpoint as a reply and tells the supervisor the run is over once per stage. Pinned by
  tests in `test_nanobot_adapter.py`.
- **`attach` is connection-scoped and does not replay history.** Every conversation must be
  re-attached after a reconnect or replies silently stop. And this page is a *mirror*, not the
  agent's memory — nanobot has no history endpoint to ask.
- **A user message is stored once, by us.** Every backend echoes user input in some form
  (`user_message` over nanobot's socket, a message echo from Pi). Each adapter suppresses its
  own echo; suppressing cross-channel input too would destroy the point of a shared session.
- **Framing is LF-only** in `rpc.py`. Pi's protocol requires splitting on `
` alone and warns
  that generic line readers break on `U+2028`/`U+2029`; Python's text mode has the mirror
  problem, splitting on `
`. Hence bytes + `b"
"`, pinned by a test using both.
- **Deltas are never stored.** Streaming text goes to the client and nowhere else; that is why
  `/api/agent/stream` has no cursor and emits no `id:`. Storing per-token rows to buy
  resumability would be unbounded and worthless.
- **The agent stream uses `fetch()`, not `EventSource`**, so its token travels in a header
  rather than a URL. Do not "simplify" this to `EventSource` + `?token=`.
- **Memory belongs to `native`, and it has two halves.** The journal is derived from Pi's
  transcript; Dream edits `MEMORY.md` / `USER.md` / `SOUL.md` under the memory directory, on a
  **2-hour schedule by default** (`CSD_MEMORY_INTERVAL_H=0` disables it). The **recall** half is
  a Pi extension (`agent/pi/appendix.ts`) that re-reads the **appendix** on every prompt through
  `before_agent_start`, so a Dream reaches the agent on its next message with no restart. The
  appendix is an ordered list the backend decides (`agent/appendix.py`): the memory
  (`memory/.recall.md`), the machines the agent may ssh to (`MACHINES.md`), and the progress-note
  instruction (§23) — the last two travel as a path and a string rather than as built text. Only on the fallback path — the extension file missing — does the
  backend pass `--append-system-prompt` and `refresh_context()` restart an **idle** Pi process.
  Dream's fourth writable path, `memory/skills/`, is outside Pi's scan locations; the native
  backend passes it with `--skill`, so names and descriptions are resident and bodies are read
  on demand.
- **The registry tool is a second extension, loaded independently, and it reads the database
  read-only.** `pi/registry.ts` registers `csd_registry`; `pi/appendix.ts` carries the prompt text.
  Losing one must not lose the other, which is why `build_argv` loads them separately and the
  appendix index drops the sentence naming the tool when the tool's file is missing. It answers by
  running `app/agent/hosts_view.py` under `sys.executable` with `Database.open_read_only` — **not**
  by calling the API, because a route would need a credential in the agent's environment and the
  only one this service has drives the whole page.
- **A settings save never interrupts a turn, and the key never leaves.** The agenest page edits
  the local agent's model/credential/permissions (§26). `meta['native_config']` wins over the
  environment, which stays the fallback; the API key is reported only as `api_key_set` /
  `api_key_env`, never returned, and reaches Pi in the child's environment rather than argv.
  `apply_config` records and marks conversations `restart_pending`; `_ensure` replaces the
  process on the next prompt. A custom endpoint is our own Pi extension (`pi/provider.ts`),
  loaded only when needed — never an edit to Pi's `models.json`.
- **Legacy transcripts are mapped, not merged.** `agent/legacy.py` runs once at startup: it
  records pre-`--session-id` Pi session files in `pi-sessions/.legacy/<conversation id>.json`,
  copies the old nanobot rows into that adapter's own table, then drops `agent_messages`. The
  mapping recovers the page and the journal, **not Pi's context** — the agent still starts from
  the new session, because merging files would break the parent chains Pi resumes from.
- **An agent that connects itself still only tells us where to knock.** This service is the
  client, always, so `/api/agent/onboard` documents the door *the agent* opens on its own machine
  and `/api/agent/backends` records the address it reports. Registration is not the switch — that
  is a separate call — and an endpoint can only be **loopback**: a personal agent is a local
  component, and refusing everything else is what removes "point the page at somebody else's
  server" by construction rather than with a second credential (§25).
- **The introduction is open; the switch is not.** `GET /api/agent/onboard(.json)` and `POST
  /api/agent/backends` carry no `access_header`, because an agent that has not been handed the
  user's token still has to read how to connect and say where its door is. They only declare.
  `POST /api/agent/backend` — the call that decides who receives everything the user types —
  stays behind the credential and is the user's click. Do not "simplify" the split by gating the
  document again (self-onboarding dies at its first step) or by opening the switch (a funneled
  deployment becomes repointable by a stranger). Because that route is open, a declaration may
  not *move* a door the page is standing on — a different address for the active kind is 409,
  or an unprivileged local process could silently receive the user's next message. §25.
- **The onboarding document is generated from the backends** (`BACKEND_ONBOARD`, collected by
  `registry.onboard_kinds()`), the same rule `BACKEND_BLURB` follows. A hand-written copy is a
  second answer to "how does this agent expose itself" and is wrong the first time either side
  changes. It lists only doors an agent can open *now* — the richer websocket door stays out of it
  because it needs a config edit and a gateway restart, which is not a step to hand a model
  mid-conversation.
- **A registered door that does not answer is a row, not a rejection.** `last_error` on a `201`:
  the registration succeeded, the other machine did not answer, and both facts have to survive
  into the settings panel. The same rule host access follows — reported, never inferred (§2).
- **Idle compaction is a trigger, not a policy.** `Supervisor` owns the clock
  (`CSD_AGENT_IDLE_COMPACT_MINUTES`, default 15); `NativeBackend.compact_when_idle` owns the
  rules — only a live process (never spawn one to compact), never mid-turn (Pi's manual
  `compact` aborts the current operation first), one attempt per new message (Pi refuses a small
  or already-compacted session). Pi also compacts on its own at the context threshold; this is
  the proactive half. The page is unaffected: Pi keeps the entries and `history()` still
  returns them.
- **`CSD_DASHBOARD_TOKEN` and `CSD_NANOBOT_TOKEN` are different credentials with different scope.**
  The first gates our page (§22). The second is nanobot's, and leaking it grants nothing on this host.
- **A conversation can live in more than one file.** Pi starts a *new* session file whenever it
  continues a session it does not currently have open — every service restart. Read every file whose
  header carries the conversation's id (`_session_files_of`), ordered by filename: reading one match
  is how a live conversation went invisible while its prompt and answer sat on disk (§23).
- **A file crosses the channel as a path, in the message text** (§29). No backend protocol has a
  media frame and every agent has file tools, so the user's upload is stored under
  `attachments/in/` and the agent receives one `[attachment <id>] <name>: <abs path>` line per
  file. The id on that line is also the transcript's identity: `_to_out` resolves it against
  `agent_attachments` and strips the line, so the page draws a card and never parses text.
  **Only ids that resolve to this thread are stripped** — anything else is words.
- **The agent's outbox delivers by moving, and the workspace is not a delivery.** The appendix
  (or, for a backend without one, a one-line `[channel]` note appended to the prompt) names
  `attachments/outbox/<conversation>/`; a settle moves whatever is there to `delivered/` and
  records one row per file. A drain happens on `Status(streaming=False)` **and** on `history()`,
  which repairs a restart between the turn ending and the event. Rendering files merely found in
  the workspace would be §2's inference applied to attachments.
- **An outgoing attachment row is the message.** The agent's transcript has no entry for a
  filesystem handoff, so `history()` merges those rows into the conversation by `created_at`
  (id `outbox:<attachment id>`) instead of decorating a neighbouring answer.
- **Attachment bytes are fetched with a header, then shown from a blob URL.** `GET
  /api/agent/attachments/{id}` is behind `access_header`; the page never uses
  `<img src="…?token=…">`, for the same reason the agent stream is not `EventSource`. The route
  sends `Content-Disposition: attachment` + `nosniff`, and uploads are a **raw body** with the
  filename percent-encoded in `X-CSD-Filename`, enforced by streaming rather than by a declared
  `Content-Length`.

### The agent page's UI

Distilled from Telegram iOS, and the interaction rules are load-bearing rather than decorative:

- **There is exactly one conversation per agent, and no UI makes a second** (§19). The `+`, the
  conversation drawer and the settings row that switched between them were all removed. The page
  calls `POST /api/agent/session` on boot — idempotent, backend-scoped — rather than tracking an
  id of its own.
- **The tab's dot means unread, not busy.** It is set by an assistant message arriving while the
  dashboard is on screen, and cleared by looking at the agent page. A dot for "a run is in
  flight" is on for the whole run and says nothing the page itself does not (§33).
- **Send never becomes Stop.** While a turn is running the send button queues the message and a
  second button stops the turn; the queue is per conversation, persisted, released one message
  per settled turn — so Stop releases it rather than discarding it (§33).
- **No clock on a bubble; time lives in a separator.** A centred line above a message says when
  the conversation resumed: the date (plus time) at the start, the date across days, the time
  after more than five minutes of silence (a real elapsed-time comparison), nothing otherwise.
  The old group-tail timestamp was still a log; hover-only was tried and reverted because touch
  devices — the primary case — had no way to see a time at all. A separator breaks the grouping
  run: a pause means the next message is a new utterance even from the same side
  (docs/design-ui.md).
- **Reasoning and tool rows are opt-in** (settings → Conversation, default off). When off,
  thinking frames are dropped rather than accumulated-and-hidden, and the header must not say
  "thinking…" on the strength of them — inferring a state from data the user chose not to see is
  §2's rule at its smallest scale. `showProcess` is read through a ref inside the stream handler,
  so toggling it does not tear down and rebuild the SSE connection. **A checkpoint is not behind
  that toggle** (`message.progress`): it is prose addressed to the reader, not the machinery
  behind an answer, and hiding it would recreate the blank screen it exists to fill (§23). It is
  **drawn as an ordinary incoming bubble**, not a stage line: `native` and nanobot's websocket
  door produce checkpoints, an agent that streams one undivided blob cannot fill a display of
  its own, and one shape for all agent prose is the trade (§23).
- **Settings has no memory section and no "this device" row.** The memory group was the only UI for
  the memory feature and the row was the only way to forget a token — with one credential and no
  second secret, both went (§22, §23). The backend memory is untouched: Dream still runs on its
  clock and the agent still re-reads it every prompt; it just has no settings entry.
- **A sent message is drawn before anything answers, and a switch draws a spinner.** The POST is
  one round trip and each file another, so waiting for it looked like a button that did nothing;
  the bubble appears with the press, is drawn quieter until acknowledged, and is replaced by the
  agent's own row from *either* the POST response or the stream echo, whichever wins. It is a
  separate list from `messages` — that one is the agent's record, deduped on its id, and a
  placeholder there could never match its own echo. A failed send stays where it was typed and
  says "Not sent"; a **queued** message is still not a bubble (§33). Switching clears the
  transcript at once and shows a spinner, never the empty state — "this agent has never said
  anything" is a claim, and the second after the user asked to see it is the one moment it is
  certainly wrong. `threadId` still names the old agent mid-switch, so the composer refuses to
  send then (§34).
- **The typing dots step aside only for a tool row that is being shown.** Suppressing them on the
  strength of a hidden one left the transcript with no sign of life for the length of every
  command. The header follows the same rule: with the process view off it says "typing…", not the
  name of a command the reader chose not to see.
- **Only agent prose is Markdown.** A user's message is rendered as typed; reinterpreting their
  own words back at them is presumptuous and wrong if they pasted markup.
- **Attachments are cards, and the `+` is outside the pill.** The attach menu offers Photo
  (image picker) and File; chosen files are chips above the input until sent, because removing
  one must be a different tap from editing the text. A card answers name, size and what a tap
  does; tapping it goes through the share sheet (falling back to download), and the arrow
  downloads. Colours inherit from the bubble — a card with its own token is unreadable on one
  side or the other (§29, design-ui.md).
- **`src/lib/markdown.tsx` is hand-written on purpose** — no `dangerouslySetInnerHTML`, no
  dependency. It builds React elements from escaped text, so there is no path from message text
  to markup and injection is structurally impossible rather than filtered. This project has no
  runtime dependencies but React; keep it that way.
- **Tool rows say `✓` or `failed` in words**, not only by border colour — the same rule the
  dashboard applies to session status.
- **The agent pane is a flex child of `.pane--agent`; `min-height: 0` is load-bearing.** Without
  it the transcript refuses to shrink, grows past the viewport, and pushes the composer off
  screen — it did exactly that (1296px of chat inside a 726px pane).

### Frontend

Two pages behind a bottom tab bar (phone-first; the rail is a dashboard-only affordance and
the agent page takes the full width). `App.tsx`'s hash parser handles all three routes —
`#/`, `#/session/<id>`, `#/agent` — because two route tables in one app is how they drift
apart. Hash routing because the app is served at an unknown path behind a private address, and
`#/session/<id>` survives a mobile refresh with no server rewrite. No router library, no state
library, no CSS framework. The read token is entered in the UI and kept in `localStorage` —
**never** compiled into the bundle, because on a public URL the bundle is public. It can be
seeded via `#token=<value>`; a fragment, not a query parameter, so it never reaches a server
log, and `replaceState` strips it immediately.

**There is one token for the whole page**, stored under one key (`csd.token`) and entered on either
page — the agent page's gate writes the same value, so unlocking one unlocks both (design.md §22).
It still never travels in a URL on the agent side: the agent stream uses `fetch()` and a header
rather than `EventSource` and `?token=`, because the routes behind it can execute.

- **The shell is cached; `/api` never is.** `frontend/public/sw.js` registers only in a
  production build, answers navigations network-first and hashed assets cache-first. Caching a
  session list or a stream would show a state that is not the reported one (§2); offline means
  the shell paints and the data is absent. The manifest, icons and worker are copied verbatim
  from `frontend/public/`; `_mount_static` serves the whole `dist/` (API routes are registered
  first), and `.webmanifest` gets `application/manifest+json` explicitly because Python's MIME
  table does not know it on every platform. Source art lives in `images/`.

`AgentPage` stays mounted while the dashboard is showing (hidden, not unmounted) so a run keeps
streaming while you look at the sessions it is changing, and the tab bar can show a live dot.

`possibly_stale` is rendered with a hollow dot and dashed capsule, reported statuses with a
solid dot — the design rule is **an inferred state must never look like a reported one**.
One `light-dark()` token set in `styles.css` drives both themes; switching is a `data-theme`
attribute, so there is no second palette to keep in sync. `styles-agent.css` introduces no new
tokens, so the two pages cannot drift into two different-looking products.

## Deployment

The supported deployment is a Linux host running `csd.service`, bound to `127.0.0.1:8780`
only. Reaching it means a tunnel or Tailscale; `tailscale funnel` publishes it publicly and is
the one place the zero-exposure premise is deliberately broken, which is why
`CSD_DASHBOARD_TOKEN` stops being optional there. `deploy/README.md` has the step-by-step;
re-running the installer is the upgrade path and never overwrites `/etc/csd/csd.env` or the
SQLite file.

**A cloud-provider caveat, measured on Aliyun ECS:** Tailscale's `ts-input` chain drops
`100.64.0.0/10` unless it arrives via `tailscale0`, and the provider's metadata, MQTT, OSS
**and DHCP-supplied DNS resolvers** all live in that range. The fix is
`tailscale up --hostname=<name> --netfilter-mode=off`, defensible only on a host with no host
firewall of its own. Run `deploy/scripts/tailscale-prefly.sh` before and after any change there
— it treats *reachability* as the verdict, because an earlier rule-inspection version reported
`[ok]` through an outage. Full analysis in [docs/design.md](docs/design.md) §11.

## Conventions

- **Written decisions over implicit ones.** A rejected obvious option gets an entry in
  `docs/design.md` with its reason. Comments in this codebase explain *why*, usually by
  naming the failure being prevented — match that, don't add what-comments.
- **Tests assert the contract, not the code layout.** Snapshot baselines are regenerated by
  deliberately changing a rule, never by re-recording current behaviour.
- `test_sse.py` runs against a **real uvicorn socket**, because `TestClient` runs the ASGI app
  to completion and an SSE generator never completes — it would hang rather than test.
- Comments and commit messages in this repo are English; `dev.sh` and `docs/design-ui.md`
  carry Chinese commentary, and seeded/report content follows the topic's language.
- **`README.md` is the operator-facing English doc; `README.zh-CN.md` carries the author's
  "why" in Chinese plus the quick start.** Keep the two consistent when commands or supported
  agents change — the Chinese file is a guide, not a translation of every section.
- Do not wire Claude Code's `Stop` hook to session end: it fires per *reply*, and with sticky
  terminal states every session would go permanently "finished" seconds after opening. It
  reports `turn_completed` instead (§36).

## Where this is going

The shape is settled: the dashboard is one page of two, and the second page is a **channel onto a
personal agent** (see the agenest section above) — one that works, rather than one that only chats.
No IM channels are in scope; this *is* the channel. As of 2026-10-19 it writes code in its workspace,
reports progress in structural checkpoints, and can drive a coding agent on another machine by
running `ssh` itself (§22, §23).

Reference material, neither vendored: the nanobot checkout (the authority on its channel
protocol: `docs/websocket.md`) and, for the `native` backend, the `pi-mono` checkout.

Deliberately **not** built yet:

- **A managed way to drive remote agents — built on 2026-10-19 (§24).** A connector declares its
  host driveable, the user approves it once in Settings → Host access, and the service generates
  the key the agent ssh's with. What is still *not* built: **NAT traversal.** The approved address
  must be reachable from this box (public, LAN, or a Tailscale IP the connector reports). A host
  that can only dial out cannot be dialled, and the reverse tunnel (`ssh -R`, either to the host's
  sshd or to a small host-side executor) is the next decision — deliberately not taken here.
- **An agent connecting itself — built on 2026-10-19 (§25).** `/api/agent/onboard` is the document
  an agent reads (generated from the backends), `/api/agent/backends` records the loopback door it
  opened, and the switch is the call it makes afterwards. Two things are deliberately **not** in
  it: nanobot's websocket channel, whose door needs a config edit plus a gateway restart (it stays
  the operator's upgrade path, and switching to the `nanobot` backend is how you take it), and any
  agent kind whose door nobody has written down — a new one is a `BACKEND_ONBOARD` dict in its
  module and nothing else.
- **nanobot's richer surface** — Apps, Skills, Automations, attachments and images, `/model`
  presets beyond the chat command.
- **Pulling nanobot's full history.** `attach` does not replay, so this page mirrors what the
  channel has seen rather than the agent's whole memory.
- **Thread management UI** (rename, delete, branch) — note that the rest of the thread UI is gone
  by design (§19), so this is now "any thread UI at all" rather than "more of it". Also idle
  process reaping for `native`, and reporting agenest's own runs into the dashboard as sessions —
  the last of which is a natural next step, since it would reuse `cpctl`'s reporting path.
