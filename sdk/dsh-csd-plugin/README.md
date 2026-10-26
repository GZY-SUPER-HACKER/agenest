# dsh-csd-plugin — DeepSeek Harness on the dashboard

Reports DeepSeek Harness (`dsh`) work to a coding-session-dashboard, so the Sessions tab shows
it beside Claude Code and any other `cpctl` reporter. It is a **reporter**, not a control
surface: the dashboard never calls into DSH, and this plugin only says what DSH itself recorded.

A DSH conversation is not a session on the dashboard. A **turn** is: it opens when the loop
claims input and closes with a reason DSH writes to its durable log (`turn/end.reason`). That
gives every row a real verdict — `completed`, `failed`, or `interrupted` — instead of a
conversation that would look `running` until the process died.

## Install

Use the same command that connects Claude Code, on the agent host:

```bash
scp -r sdk/csd-agent sdk/cpctl sdk/dsh-csd-plugin you@agent-host:~/csd-src/
ssh you@agent-host
PYTHONPATH=~/csd-src/csd-agent python3 -m csd_agent install \
    --cloud <dashboard-host> --source ~/csd-src/csd-agent
```

`csd-agent install` copies this package into `~/.csd/dsh-csd-plugin`, builds the tarball it
installs from, and wires **every DSH profile that exists** (`web`, `headless`, `sdk`, …) through
DSH's own plugin manager. `--no-dsh` opts out; `--dsh-profile web` limits it to one profile.
`csd-agent status` reports which profiles carry the reporter, and `csd-agent uninstall` removes
exactly the dependency and bundle entry it added.

The copy is deliberate. `dsh plugin add <directory>` records a `link:` to that directory, so a
reporter installed from a checkout breaks the day the checkout moves. The tarball installs as a
real package, and the vendored copy under `~/.csd` is the artifact that survives.

On a host that runs DSH but not Claude Code:

```bash
PYTHONPATH=~/csd-src/csd-agent python3 -m csd_agent dsh install --source ~/csd-src
```

For local development, the standalone path is still DSH's own command:

```bash
dsh plugin --profile web add /path/to/coding-session-dashboard/sdk/dsh-csd-plugin
```

That is the one form that creates a `link:`; use it for editing the reporter, not for deploying
it.

The dashboard's address and token are not part of the install. The plugin resolves them in this
order:

| Source | How |
|---|---|
| The plugin's Loader `config` | target the row from the profile's own patch layer (see below) |
| The process environment | `CSD_BASE_URL`, `CSD_INGEST_TOKEN`, `CSD_HOST_LABEL`, `CSD_AGENT_TYPE` |
| `~/.csd/env` | written by `csd-agent install` for Claude Code, mode 0600 |
| Built-in default | `http://127.0.0.1:8780` |

The config row is a *targeting* entry in `$DSH_HOME/profiles/<profile>/cordis.patch.yml`, not an
edit to the installed package:

```yaml
# later layers win per row; this replaces the bundle row's config wholesale
- id: csd-reporter
  config:
    baseUrl: http://127.0.0.1:18781
```

The environment is usually the better place for the token: a config file is a second copy of a
secret.

A machine that already runs `csd-agent` already has the token in `~/.csd/env`; nothing else is
needed. A machine without it can export the two variables before launching DSH:

```bash
export CSD_BASE_URL=http://127.0.0.1:8780
export CSD_INGEST_TOKEN=$(cat backend/data/dev-token)
dsh web
```

If the dashboard is on another machine, the agent host has to originate the connection
(design.md §1). Point `CSD_BASE_URL` at the near end of a tunnel — `csd-agent install` writes one
for Claude Code at `http://127.0.0.1:18781`, or use any tunnel of your own.

Optional config keys (all optional):

| Key | Default | Meaning |
|---|---|---|
| `baseUrl` | `http://127.0.0.1:8780` | Dashboard origin |
| `token` | — | Ingest token; prefer the environment or `~/.csd/env` |
| `envFile` | `~/.csd/env` | Token file to read instead; `false` disables it |
| `agentType` | `dsh` | The harness name the rail groups by |
| `host` | the machine's hostname | Machine label; `false` reports no machine |
| `reportHost` | `true` | Report the host row at all |
| `turnHeartbeatMinutes` | `5` | Liveness beat while a turn is open; `0` disables |
| `hostHeartbeatMinutes` | `5` | Host row refresh; `0` disables |
| `timeoutSeconds` | `3` | Per-request timeout |
| `retries` | `3` | Retries after a transient failure |

## What it reports

| DSH fact (durable log) | Dashboard row |
|---|---|
| `turn/start` | creates a session: `agent_type: dsh`, project = cwd's last segment, workspace = cwd, host = this machine |
| first human `user/message` in the turn | replaces the placeholder title with the prompt's first line |
| `session/title` derived from this turn's prompt | replaces the title with DSH's generated one |
| `step/end` | a `progress` event (`step N complete`), unless that step's assistant message was already a checkpoint |
| `assistant/message` | a `checkpoint` when the same message also requested a tool (or DSH marked it `interrupted`); a text-only message is the answer and is left to `turn/end` |
| `todo/write` | a `checkpoint` with `source: "plan"` carrying the whole todo list |
| `approval/asked` | an `awaiting_user` event, so the session appears under *needs attention* |
| `approval/decided` | a `progress` event that clears the attention state |
| `user-questions/request` | an `awaiting_user` event; the request is delegated to DSH unchanged |
| `turn/end` `completed` | closes `completed` |
| `turn/end` `error` | closes `failed`, quoting DSH's error code and message |
| `turn/end` `aborted` / `blocked` / `interrupted` | closes `interrupted` |
| `turn/end` `max-tokens` | closes `completed`, noting the output ceiling |
| `session/disposed` mid-turn | closes `interrupted` — the disposal is the report |
| host at boot and every 5 minutes | a connected host row, `wants_access: false` |

An end reason this build does not recognize closes the session as **`unknown`**, not as
`completed`: a newer DSH plugin may add one, and guessing would turn uncertainty into a verdict.

A plugin (re)load during a turn reads the live session log once and resumes the turn that is
open, so a live patch reload does not lose the outcome. The recovery uses the same events the
normal path does; it does not invent a status.

## What it deliberately does not do

- **Subagent child sessions.** They are part of the parent's turn; a row per child would make
  one task look like several. `header.origin: 'subagent'` and non-zero delegation depth are
  ignored.
- **Tool output, token counts, or "still working".** The plugin reports facts DSH wrote down.
  It never parses a stream or decides completion from elapsed time or event count (design.md §2).
- **A goodbye on shutdown.** A DSH stop or a plugin reload is not an uninstall, so the machine
  simply stops talking and the dashboard renders `possibly_stale`. Only `csd-agent uninstall`
  says `disconnected`.
- **A driveable-host declaration.** `wants_access` is always false: this connector cannot install
  an ssh key, so asking the user to approve one would promise something it cannot deliver.
  Install `csd-agent` on the same machine if you want the personal agent to drive it.

## Why not the Claude Code hook bridge

DSH ships `dsh-hooks-claude-code`, which runs a Claude Code `settings.json` hooks block. Pointing
`csd-agent` at it is the obvious one-line answer and it does not work here: the bridge supports
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, and the subagent events,
and deliberately not `SessionEnd` or `Notification`. A session would open and never close, and a
blocked turn could not be shown. The durable session log has both, so this plugin reads it
instead. The full reasoning is in `docs/design.md` §28.

## Requirements and tests

Node.js 20+ (DSH's own floor is higher) and any DSH 0.1.x build that exposes the documented
session events. Python is not required — the reporter speaks HTTP directly.

```bash
cd sdk/dsh-csd-plugin
npm test
```
