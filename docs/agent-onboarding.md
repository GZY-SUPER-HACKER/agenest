# Connecting an agent to this dashboard

**Who this is for.** Someone who wants a conversation with an agent to show up on this
dashboard's Agent tab. That is three different readers, so the page is in three parts:

- [The agent itself](#tell-your-agent) — what it needs to know, and what it cannot do.
- [The person running this service](#the-one-thing-you-configure) — one setting, once.
- [The next person adding an agent](#adding-a-fourth-agent) — where the seam is.

It is deliberately short. Anything longer than a page would be documentation for a protocol
the agent already speaks, and the two values below are the whole of what is being agreed.

---

## The idea, in one paragraph

This dashboard is a **channel onto an agent you already run** (docs/design.md §17). It is not
an agent, and it does not run one: your agent keeps its own memory, its own model
credentials, its own tools and its own workspace, and this service carries messages to it
and renders what comes back. The same relationship your agent already has with Telegram or
the CLI, with a screen that shows more — the reply, the tools, and the thinking if you ask
for it.

## Tell your agent

Send this to your agent, in whatever conversation you already have with it:

```
接入 agenest
```

It needs two values from you, and nothing else:

| Value | Example | What it is |
|---|---|---|
| `ws_url` | `ws://127.0.0.1:8765/` | The dashboard-side address of your agent's gateway |
| `client_id` | `csd-dashboard` | The name this dashboard connects under |

### What your agent cannot do

Worth saying plainly, because "let the agent wire itself up" implies more than it does:

- It **cannot** change this service's configuration, restart it, or read its credentials.
  Those live with the operator, in `.env` or the systemd unit.
- It **cannot** make itself reachable if its own gateway is off. If the page says the agent
  is unreachable, the fix is on the agent's side.

That asymmetry is the point rather than a limitation. An agent with file write access and a
shell being told "connect yourself to this dashboard" must not turn into an agent that edits
this service's environment file, because the promise this dashboard makes — the worst case is
that someone can see your task titles — depends on that never happening.

## The one thing you configure

On this service, once:

| Agent | What to set | Where |
|---|---|---|
| **nanobot** | `CSD_AGENT_BACKEND=nanobot`, `CSD_NANOBOT_WS_URL` | `.env` / systemd unit |
| **OpenClaw** | `CSD_AGENT_BACKEND=openclaw`, `CSD_OPENCLAW_WS_URL`, and the Gateway's shared token if it has one | same |
| **Hermes** | `CSD_AGENT_BACKEND=hermes`, `CSD_HERMES_API_URL`, `CSD_HERMES_API_KEY` | same |
| **native** (default) | `CSD_PI_MODEL`, and a provider key | same |

Afterwards, nothing per agent: **each agent connects by itself** by being asked, every time
it wants to. [.env.example](../.env.example) carries the annotated list, and the Agent tab's
settings panel switches between agents without a restart — a thread records which agent it
was created against and keeps talking to that one.

### Per agent, what "connect" actually involves

- **nanobot** — nothing. Its websocket channel is enabled by default and its `allowFrom`
  defaults to `["*"]`, so there is no credential to copy. Point the URL at it and it works.
- **OpenClaw** — on the same machine with the Gateway's shared token, nothing. From
  elsewhere, or without a token, the Gateway answers `PAIRING_REQUIRED` and the page names
  the command that resolves it: `openclaw devices list`, then
  `openclaw devices approve <requestId>`, on the Gateway host.
  **Verified against a real 2026.10.4 Gateway:** the loopback path needs no pairing at all —
  a `gateway-client` / `backend` connection with the shared token is accepted outright. Set
  the token with `openclaw config set gateway.auth.token <token>`; without one the Gateway
  mints a fresh runtime token on every start, which nothing outside the Gateway can know.
- **Hermes** — one setting there and one here. Hermes' **API server** must be on
  (`API_SERVER_ENABLED = true`) with a key (`API_SERVER_KEY`), and this service needs the
  same key as `CSD_HERMES_API_KEY`. Without the key every request is a 401, so the settings
  row reports the credential as missing rather than suggesting you check whether the server
  is running.
  **Verified against a real v0.18.2 server:** `/health` is *unauthenticated* — it answers 200
  to a wrong key and to no key — so this service checks the key against `/api/sessions`
  instead of trusting a healthy `/health`.
  **And against v0.19.0 and v0.22.2** — a turn, a tool call, a transcript read and a stop all
  behave identically on all three, so the surface this adapter uses is stable across them.
  Two version facts are worth knowing before you install:
  - **PyPI lags.** `pip install hermes-agent` gives you **0.19.0**; the newest release is
    **v0.22.2**, which only Hermes' own installer (a source checkout) reaches.
  - **The API server needs `aiohttp`, which the package does not pull in.** Without it
    Hermes logs `API Server: aiohttp not installed` and the gateway comes up with the API
    surface simply dead. `pip install aiohttp` fixes it.
- **native** — a Pi process this service runs itself, for when there is no gateway to
  install. Needs Pi and a provider key, and it is the one backend that acts on this host
  under this service's user.

### What each backend does not support

Stated here rather than discovered later, because each is a place the page looks broken
while working exactly as designed:

| | Not supported | Why |
|---|---|---|
| **Hermes** | tool output while it is streaming | `tool.started` / `tool.completed` carry the tool's name, duration and error flag but **no result**. A live tool row therefore has no content; the content is in the transcript, which `history()` reads. |
| **Hermes** | correlating a tool's start with its end by id | the runs stream names no tool call, so a start and its end pair by order. The page renders `tool_name` only, and the transcript carries the ids. |
| **OpenClaw** | the user's own message id | `chat.send` answers `{"runId", "status"}` and names no row. The page draws the message from the send and reconciles on the next history read. |
| **OpenClaw** | tool output as it streams | the `update` phase carries a `partialResult`; `stream: "command_output"` carries the raw stdout and is not rendered today. |
| **nanobot** | replaying history | `attach` is connection-scoped and does not replay, so that page is a mirror of what this connection has seen. |

### Stopping a turn

The page's stop button calls the backend's `abort()`, and whether it does anything is a
property of the agent's protocol rather than of this service:

- **Hermes** — works, over `POST /v1/runs/{run_id}/stop`. This is why the adapter creates its
  turns through the **Runs API** rather than Chat Completions: a Chat Completions turn is not
  a run, its stream names a `chatcmpl-…` completion, and stopping that is refused with
  `404 run_not_found` (verified in both directions against a live server).
- **OpenClaw** — works, over `chat.abort`, when a run is in flight.
- **native** — a Pi process this service owns, so it stops with the process.

### Where these facts come from

The two socket-based adapters were originally written from published protocol contracts and
checked only against stubs that encoded the same guesses — which is how a `sessions.create`
call that the real Gateway refuses, and a session key the real Gateway renames, both passed
a green test suite. They were then run against real instances: **OpenClaw 2026.10.4**, and
**Hermes v0.18.2, v0.19.0 and v0.22.2**, on this machine, with a real model behind each.
Everything asserted about those two protocols in this repository now comes from an observed
frame or from the agent's own source, and the stubs in `backend/tests/test_*_adapter.py`
were rewritten to those shapes so the suite fails if the behaviour drifts back.



## What the page will and will not show

- **History, when the agent has it.** A backend whose agent can hand back its transcript —
  OpenClaw's `chat.history`, Hermes' `/api/sessions/{id}/messages`, our own Pi's session —
  shows the conversation as the agent has it, including turns that happened somewhere else.
  nanobot's channel protocol cannot replay, so that page honestly shows *what this
  connection has seen* rather than pretending to be the agent's memory.
- **Reported states, not inferred ones.** The header says busy, offline, or nothing, and
  those come from the agent's own events. An idle look is never turned into "finished".
- **Tool activity and reasoning are opt-in** (settings → Conversation). When they are off
  they are not collected at all, rather than hidden — a state must not be inferred from data
  the page chose not to keep.
- **One conversation per agent.** There is no new-chat button and no conversation list: the
  list of what you have been working on is the dashboard itself, and a second, weaker list
  would be a worse answer to a question the other tab already answers (§19).

## Adding a fourth agent

Adding an agent is additive: write `backend/app/agent/backends/<name>.py`, define
`BACKEND_NAME`, `BACKEND_LABEL`, `BACKEND_BLURB` and a `build(settings, on_event)`, and
restart. The registry scans that directory, so the settings panel grows a row and nothing in
the core learns the new agent's name. Read
[backends/base.py](../backend/app/agent/backends/base.py) first — it states the one rule that
matters:

> A backend's job is translation, not invention. If something cannot be expressed as a
> `Delta`, a `Message`, a `ToolCall`, a `Status` or a `Notice`, that is a signal the
> abstraction is wrong, not a reason to add a frame the UI must learn.

Two questions decide the shape of a new adapter, and both are answered by reading the
agent's own protocol rather than by guessing:

1. **Can it replay history?** If the protocol exposes a transcript, implement
   `history()` and store nothing — the conversation then lives in one place, and it is the
   agent's. If it cannot, record what you need **inside your own adapter**: the core keeps
   no message table, on purpose (`base.history`).
2. **Does its stream carry tool calls, or only text?** If only text, say so in
   `BACKEND_BLURB`. A page that can show an agent talking but not working is a smaller
   product, and the person connecting it deserves to know that before they do.

Two existing adapters are worth reading as the two ends of the range: `openclaw.py`
(WebSocket, request/reply multiplexing, a capability that silently withholds tool events)
and `hermes.py` (HTTP with an SSE reply, a session carried in a header, and keepalive
comments that are not data).
