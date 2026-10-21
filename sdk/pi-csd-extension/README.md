# pi-csd-extension — Pi on the dashboard

Reports standalone **Pi** (`pi`) runs to a coding-session-dashboard, so the Sessions tab shows
them beside Claude Code and DeepSeek Harness. It is a **reporter**, not a control surface: the
dashboard never calls into Pi, and this extension only says what Pi itself emitted.

Pi has no plugin manager. It discovers every `*.js` in `<agent dir>/extensions/` at startup and
loads it, so installing this reporter is one copy — and uninstalling it is deleting one file,
identified by the marker inside it.

## One run is one session

A Pi **run** is one user prompt and everything Pi does automatically for it: retries, compaction
and retries, and any queued follow-up prompts. `turn_end` fires once per LLM response, so a row
per turn would turn one task into a row per model call.

| Event | What it means here |
|---|---|
| `before_agent_start` | Opens the run. Carries the user's prompt, which becomes the title. Ignored if a run is already open. |
| `turn_end` | A `progress` event, `turn N complete`. Not a boundary. |
| `message_end` | Records the assistant message's `stopReason` / `errorMessage`. |
| `agent_settled` | Closes the run: Pi has no retry, compaction retry or follow-up left. |
| `session_shutdown` | Closes an open run as `interrupted` (Pi switched session or exited first). |
| `session_start` | Posts the host row and starts its five-minute timer. |

`agent_start` is deliberately **not** the opener: it fires again for retries and compaction
inside one prompt.

## Outcomes

The verdict is Pi's own `AssistantMessage.stopReason` (Pi 0.84.x), translated branch by branch:

| `stopReason` | Status | Notes |
|---|---|---|
| `stop`, `toolUse`, `deferred` | `completed` | |
| `length` | `completed` | summary notes the output-token ceiling |
| `error` | `failed` | summary quotes `errorMessage` |
| `aborted` | `interrupted` | |
| `pending`, missing, unrecognized | `unknown` | never guessed as `completed` |

There is no `awaiting_user`: Pi has no standard approval or permission event, and inferring one
from a tool name would be a guess, not a report.

## Install

Use the same command that connects Claude Code and DeepSeek Harness, on the agent host:

```bash
scp -r sdk/csd-agent sdk/cpctl sdk/pi-csd-extension you@agent-host:~/csd-src/
ssh you@agent-host
PYTHONPATH=~/csd-src/csd-agent python3 -m csd_agent install \
    --cloud <dashboard-host> --source ~/csd-src/csd-agent
```

The extension is copied to `<agent dir>/extensions/csd-reporter.js`, where `<agent dir>` is
`$PI_CODING_AGENT_DIR` or `~/.pi/agent`. The **global** directory is the only supported target:
project-local `.pi/extensions` loads only after Pi trusts the project, so a reporter there goes
silent the first time it runs somewhere new. The vendored copy lives at
`~/.csd/pi-csd-extension` and is the artifact this package deploys.

`csd-agent install` skips the step when Pi is not installed (`--no-pi` opts out explicitly,
`--pi-home` points at another agent directory). `csd-agent status` reports whether the extension
is in place, and `csd-agent uninstall` removes it — only a file carrying the
`csd-pi-reporter` marker is deleted, so a user's own extension with the same filename is left
alone and reported.

On a host that runs Pi but not Claude Code:

```bash
export PYTHONPATH=~/csd-src/csd-agent
python3 -m csd_agent pi install --source ~/csd-src
python3 -m csd_agent pi status
python3 -m csd_agent pi uninstall
```

For development, point Pi at the checkout directly instead of copying:

```bash
pi -e ./coding-session-dashboard/sdk/pi-csd-extension/csd-reporter.js
```

`-e` is the one form that loads a working copy; use it for editing the reporter, not for
deploying it.

The dashboard's address and token are **not** part of the install. The extension resolves them
in this order:

| Source | Keys |
|---|---|
| The process environment | `CSD_BASE_URL`, `CSD_INGEST_TOKEN`, `CSD_HOST_LABEL`, `CSD_AGENT_TYPE` |
| `~/.csd/env` (mode 0600) | `CSD_BASE_URL`, `CSD_INGEST_TOKEN` — written by `csd-agent install` for Claude Code |
| Built-in default | `http://127.0.0.1:8780` |

`CSD_PI_ENV_FILE` names a different env file. `CSD_AGENT_TYPE` defaults to `pi` and is half of a
host's identity; changing it makes the machine look like a second host.

## What is deliberately not reported

- **The dashboard's own personal agent.** The Agent tab's `native` backend spawns the same `pi`
  binary. It marks every child with `CSD_AGENT=1`, and this extension returns without
  registering anything when it sees that — otherwise the channel would report conversations with
  its own operator as coding sessions.
- **`CSD_PI_REPORTER=0`** is the operator's own opt-out, and is equally silent.
- **History.** Pi has no endpoint to ask for past runs; this reports what happens while the
  extension is loaded.
- **A goodbye for the host.** Pi exiting is not an uninstall, so the host row is left to go
  `possibly_stale` — the same honest rendering a quiet session gets. `csd-agent uninstall` says
  goodbye explicitly.

Diagnostics go to **stderr**, never stdout: in `pi --print` the answer is the stdout stream, and
a reporter that logged there would inject its own lines into whatever consumes Pi's output.

## Tests

```bash
npm test          # node --test, no dependencies
```

The suite drives a fake `ExtensionAPI` through the documented lifecycle and a fake HTTP client,
so it needs neither Pi nor a dashboard.
