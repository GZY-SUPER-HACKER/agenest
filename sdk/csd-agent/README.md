# csd-agent — the agent-host side

Puts the machine a coding agent runs on onto the dashboard. One command, no sudo, no pip.

```bash
# on the agent host
scp -r sdk/csd-agent sdk/cpctl you@agent-host:~/csd-src/
ssh you@agent-host
PYTHONPATH=~/csd-src/csd-agent python3 -m csd_agent install \
    --cloud <dashboard-host> --source ~/csd-src/csd-agent
```

That is the whole setup. `csd-agent status` tells you whether it worked.

### If the agent runs on the dashboard's own machine

Use `--local` instead of `--cloud`. There is nothing to tunnel to — the API is already on
loopback — so no `csd-tunnel.service` is written and the connector talks straight to the
dashboard's port. Everything else is identical: the same hooks, the same five-minute probe,
the same host registration.

```bash
PYTHONPATH=~/csd-src/csd-agent python3 -m csd_agent install \
    --local --source ~/csd-src/csd-agent
```

The token comes from that machine's `/etc/csd/csd.env` (0640 root:csd, so run it as root or
pass `--token`). Switching an existing remote install to `--local` removes the leftover
tunnel unit rather than leaving two answers to "how does this host reach the dashboard" on
disk at once.

### On Windows

There is no systemd, so the installer writes the two things Windows actually has: a **Startup
entry** for the tunnel keeper and a **Task Scheduler** task for the heartbeat. The hooks are
the same file merge as everywhere else.

```powershell
# from the repo, with a normal Python (not a project venv — the hook bakes this path)
python -m csd_agent install --cloud <dashboard-host> --cloud-user root `
    --python C:/path/to/Python312/python.exe --no-drive
```

Or, when the dashboard is already published at an address this machine can reach (Tailscale
Funnel, a reverse proxy):

```powershell
python -m csd_agent install --url https://<dashboard> --no-drive
```

`--url` removes the tunnel entirely. It is the right shape when the published endpoint
answers quickly; prefer the tunnel when the endpoint is slow, because the hook wrapper gives a
report **2 seconds** before it gives up. A TLS handshake to a distant Funnel can take longer
than that, and a hook that blocks on every prompt is worse than one that reports nothing.

**What survives a reboot on Windows:**

| | Where it lives | When it comes back |
|---|---|---|
| hooks | `~/.claude/settings.json` | always — it is just a config file |
| tunnel | `%APPDATA%\…\Startup\csd-agent-tunnel.vbs` | at logon; the keeper restarts `ssh` in a loop |
| heartbeat | Task Scheduler, `CSD-Agent Heartbeat` | every 5 minutes after logon |

Nothing runs before a user logs in, and there is deliberately no service: installing one
needs admin rights and is a bigger commitment than this connector should make on your
machine. While the machine is off or logged out, the dashboard shows the host as *maybe
offline* — which is what is actually true. The tunnel starts with no terminal, so the SSH key
must work without a passphrase prompt (an unencrypted key, or one loaded into the Windows
OpenSSH agent); `BatchMode=yes` turns a protected key into a missing tunnel rather than a
prompt nobody can answer.

`csd-agent status` prints the tunnel port and the scheduled task next to the Linux unit
checks, so "did it reconnect after the reboot?" has an answer without opening Task Scheduler.


---

## Why this exists

The dashboard never dials out ([design.md](../../docs/design.md) §1) and binds loopback, so the
**agent host has to originate the connection**. Doing that by hand is nine steps, four of
which fail silently:

| Step | How it fails when you get it wrong |
|---|---|
| `nohup ssh … -L … &` | dies with the SSH session that started it; the tunnel is gone and nothing says so |
| no `ExitOnForwardFailure` | ssh logs a warning, stays "healthy", forwards nothing |
| no `ServerAliveInterval` | the socket goes half-open on a NAT timeout; ssh never exits, so `Restart=always` never fires |
| hand-written hooks in `settings.json` | on a machine whose settings are rewritten by a config manager, they vanish and reporting stops |

`csd-agent install` is those steps with the failure modes handled, and it verifies the
result rather than assuming it.

## What it does to your machine

Exhaustively — because on a shared machine "what did that script touch" is the first
question, and "nothing outside your home directory" is a better answer than a list.

```
~/.csd/                              agent.json, env (0600), cpctl/, csd-agent/, bin/, state/
~/.csd/dsh-csd-plugin/               the DSH reporter, when DSH is installed here
~/.csd/dsh/*.tgz                     the copy DSH's plugin manager installs from
~/.csd/pi-csd-extension/             the Pi reporter, when Pi is installed here
~/.ssh/authorized_keys               one line, only if you approve this host in the dashboard
~/.config/systemd/user/              csd-tunnel.service, csd-heartbeat.{service,timer}
~/.claude/settings.json              four hook entries merged in; a .bak-csd-agent is taken first
~/.claude/skills/csd-task-verdict/   the skill that tells the agent when to report done/failed
~/.csd/bin/csd-cpctl                 the launcher that skill calls (under ~/.csd/ above)
~/.dsh/profiles/*/package.json       one dependency + bundle entry, one per DSH profile
~/.pi/agent/extensions/csd-reporter.js  one copied file, the whole Pi install
```

On Windows the systemd directory is replaced by
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\csd-agent-tunnel.vbs`, and the timer
by the Task Scheduler task `CSD-Agent Heartbeat`. `bin/` also holds the two `pythonw`
launchers those entries run.

No sudo, no pip, no conda environment, nothing outside `$HOME`. `csd-agent uninstall` reverses
all of it except the `~/.csd` directory itself, which it tells you to remove by hand once you are
sure.

## The verbs

| Command | What it is |
|---|---|
| `install` | one-time setup; vendors the code, writes the env file and the service entries (systemd units, or the Windows Startup + Task Scheduler pair), merges the Claude Code and Codex hooks, wires every DSH profile and the Pi extension, writes the task-verdict launcher/skill, verifies |
| `status` | what this host believes, which units are up, whether Codex's hooks are in place, which DSH profiles carry the reporter, whether the Pi extension is in place, and whether the API actually answers |
| `heartbeat` | the probe. Run by a timer every 5 minutes; also fine to run by hand |
| `hook` | what Claude Code calls. Reads the payload on stdin; **always exits 0** |
| `hook-codex` | what Codex calls. Same contract, different event vocabulary |
| `skill install\|status\|uninstall` | the agent-side task verdict: `~/.csd/bin/csd-cpctl` plus the Claude Code skill that tells the model when to call it |

`csd-agent dsh install\|status\|uninstall` is the same DSH wiring without the Claude Code half,
for a host that runs DeepSeek Harness but not Claude Code. `csd-agent pi ...` and
`csd-agent codex ...` are the same idea for those two surfaces.

## The task verdict

Lifecycle reporting says how the *run* ended. It cannot say whether the *work* passed — an
agent can exit 0 with the tests still red. `csd-agent skill install` writes two small pieces
that let the agent itself say so:

- `~/.csd/bin/csd-cpctl` — the reporting CLI with this install's `PYTHONPATH` and `~/.csd/env`
  already applied, because the agent's shell has neither;
- `~/.claude/skills/csd-task-verdict/SKILL.md` — when to call `task done` / `task failed`, and
  when not to (questions, partial steps, every turn).

```bash
bash "$HOME/.csd/bin/csd-cpctl" task done -m "12 tests pass"
bash "$HOME/.csd/bin/csd-cpctl" task failed -m "pytest: 3 failed"
```

`bash` is explicit because the launcher is a bash script: on Windows the agent's shell may be
PowerShell, where a bare path to a `.sh`-style file produces no output and no exit code. In a
POSIX shell `bash <path>` works just as well.

The command resolves the session the connector opened in this directory, so the agent never
has to know an id. It is optional: a wrapper can report a verifier's verdict instead
(`run-agent.sh --verdict`), and a session with no verdict simply claims nothing. See
[design.md](../../docs/design.md) §40.

## DeepSeek Harness

DSH has no hook configuration file to merge; its extension surface is a Cordis plugin mounted
from the profile's bundle list. `install` handles that without asking the operator to learn a
second installation command:

1. copy the reporter from `sdk/dsh-csd-plugin` into `~/.csd/dsh-csd-plugin`;
2. build an npm-style tarball at `~/.csd/dsh/`;
3. for every existing profile (`web`, `headless`, `sdk`, …), run
   `dsh plugin --profile <name> add <tarball>`.

The tarball matters. `dsh plugin add <directory>` records a `link:` to that directory, and a
reporter linked to a development checkout stops working the day the checkout moves. A tarball
installs as a real package; DSH's own plugin manager keeps the dependency and bundle list
consistent, so a later `dsh plugin update` does not drop the reporter.

`--no-dsh` skips it. `--dsh-profile web` limits it to one profile. A host without `~/.dsh` is
not an error: the Claude Code install is unaffected, and re-running the installer after DSH
appears wires it then. The reporter itself is documented in
[dsh-csd-plugin](../dsh-csd-plugin/README.md); its host row is `(label, dsh)`, separate from
this connector's `(label, claude_code)`, because one connector speaks for one coding agent.

## Codex

Codex loads hooks from `$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`), and its
payloads are Claude Code-compatible in shape: JSON on stdin, `hook_event_name` in the body, a
short-lived command. `install` merges our matcher groups into that file; a host without Codex
is skipped. `csd-agent codex install|status|uninstall` is the same module on its own.

The file belongs to you. Install replaces only groups whose command carries our marker, and
uninstall removes only those; anything else in the file is preserved verbatim. A malformed file
is reported and left untouched.

**The envelope is the same; the events are not.** Codex has no `MessageDisplay` and no
`PostToolBatch`, and `postModelRequest` carries status and token usage but no text. So this
reporter is **turn-level**: session start/end, the prompt, the reply boundary (`Stop`), user
interrupts, and `awaiting_user` when Codex asks for approval. It emits **no checkpoints** —
the only prose Codex exposes is `stop.last_assistant_message`, which is the answer, not
progress (docs/design.md §38).

`PermissionRequest` is observed, never decided. Codex broadcasts the request to every connected
client and the first answer wins; a deny from this hook could beat the human in the TUI.
Returning no decision is the safe behavior, and it is what the handler does.

Codex marks new hooks untrusted until you approve them. An interactive session asks; a headless
run can pass `--dangerously-bypass-hook-trust`. `csd-agent codex status` reports the file, not
a trust decision this package cannot make for Codex.

```bash
python3 -m csd_agent codex install
python3 -m csd_agent codex status
python3 -m csd_agent codex uninstall
```

## Pi

Pi has neither a hooks file nor a plugin manager. It discovers every `*.js` in
`$PI_CODING_AGENT_DIR` (default `~/.pi/agent`)`/extensions/` at startup, so the install is a copy
and nothing else:

1. copy `sdk/pi-csd-extension` into `~/.csd/pi-csd-extension` (the deployed artifact);
2. copy `csd-reporter.js` to `~/.pi/agent/extensions/`.

The **global** directory is the only target. `.pi/extensions/` is project-local and loads only
after Pi trusts the project, so a reporter there would go silent the first time it runs somewhere
new. `--no-pi` skips the step; `--pi-home` points at another agent directory. A host without
`~/.pi/agent` is not an error — the Claude Code and DSH installs are unaffected, and re-running
after Pi appears wires it then.

Uninstall deletes **only a file carrying the reporter's marker**. The extensions directory is
the user's and may hold their own work; a file that does not carry the marker is reported and
left alone. The reporter is documented in
[pi-csd-extension](../pi-csd-extension/README.md); its host row is `(label, pi)`.

The dashboard's own personal agent (`native`) is the same binary and is *not* wired twice: the
backend marks every child it spawns with `CSD_AGENT=1`, and the extension registers nothing when
it sees that. Otherwise the agent you talk to would report its conversations as coding sessions.

A host that runs Pi but not Claude Code:

```bash
export PYTHONPATH=~/csd-src/csd-agent
python3 -m csd_agent pi install --source ~/csd-src
python3 -m csd_agent pi status
python3 -m csd_agent pi uninstall
```

## Being driven: the one line it may write outside `~/.csd`

By default the installer **declares this host driveable** (`--no-drive` opts out): the dashboard
offers it to the user as something the personal agent could `ssh` into, and nothing more happens
until that user approves it there. Declaring grants nothing — it is a request for a decision.

On approval, the dashboard mints a key and the next `heartbeat` installs its *public* half into
`~/.ssh/authorized_keys` as:

```
ssh-ed25519 AAAA… csd-agent:<label>
```

That comment is the whole contract. The connector appends that line, replaces that line when a new
key is issued, and deletes that line when access is revoked — and **never touches any other line**.
Your own keys are in that file; a package that rewrote it could lock you out, so it does not touch
what it did not write. The private half never leaves the dashboard.

Revoking in the dashboard deletes the key there immediately and the line here on the next
heartbeat. If you `uninstall` before that beat, remove the `csd-agent:<label>` line by hand — the
connector is what carries the removal, and it is gone.

## The probe, and what it is allowed to say

The first thing the probe does is **report the host itself**, before it looks for anything
else. That is not a detail: it used to return early when `tracked.json` was empty, so a
connector that had just been installed reported nothing at all — no heartbeat, no hello —
until the user happened to start a Claude Code session on that machine. "Did my host connect?"
had no answer that did not involve SSH. Now the machine appears on the dashboard the moment
`install` finishes, and stays there whether or not anything is running on it.

It reports **only** `connected`. There is no honest way for this to report a disconnection:
the cases that matter — a power cut, a dead network, a laptop lid — are precisely the ones
where it never runs. Those hosts simply stop being heard from, and the dashboard renders that
as "maybe offline", which is the honest reading of a question nobody can ask any more. The one
place a disconnect *is* reported is `uninstall`, which says goodbye before stopping the units
(for a remote host the tunnel is what carries the message).

Beyond that, `heartbeat` asks one question per tracked session: *is the Claude Code process I
recorded still there?* That answer is a fact, not an inference, which is what keeps it on the
right side of design.md §2.

It is deliberately narrow. It reports a **heartbeat**, which by design (§8) renews
*liveness* and not *activity*: it advances `last_activity_at` so the UI stops calling the
session stale, and does not touch `updated_at`, so a session that is merely alive never
outranks one that reported real work. **It cannot say "in progress."** Nothing outside
Claude Code can know that, and a dashboard that guessed would be worse than one that admits
it does not know.

The distinction that matters in the code: **an unknown pid is not a dead process.** If
`find_cc_pid()` cannot identify the Claude Code ancestor, the probe skips the entry rather
than judging it — a probe that announced the death of a live session within five minutes of
it starting would be the worst possible bug in this package.

The second job: a Claude Code killed with `SIGKILL`, or taken out by a dropped SSH session,
never fires `SessionEnd`. Its session would sit at `running` forever. The process being gone
*is* the report, so the heartbeat closes it as `interrupted` — and if that report cannot be
delivered, the entry is kept and retried rather than forgotten.

## Which hooks are wired

| Event | Action |
|---|---|
| `SessionStart` | create the session, or reuse it on `--resume`; close any dead session in the same directory |
| `UserPromptSubmit` | replace the placeholder title, and report `Turn N: <prompt>` as a `progress` event; a harness-injected message (e.g. `<task-notification>`) does neither (§39) |
| `MessageDisplay` | accumulate the assistant text in `tracked.json`; report nothing yet |
| `PostToolBatch` | report the accumulated sentence as a `checkpoint` |
| `Notification` | report an `awaiting_user` event — Claude Code saying it wants a human |
| `Stop` | report a `turn_completed` event; **never a status** |
| `SessionEnd` | `finish --status completed` |

### The heartbeat puts the entries back

`settings.json` has two writers with different semantics: this installer merges by marker, and
a config manager that stores a whole settings template (cc-switch does) writes it over the top
on every provider switch. The second deletes every hook its template does not carry — on this
project's own dev machine the notification-sound hook survived because it lives in that
template, while all seven of ours vanished.

The five-minute heartbeat checks for our marker and re-merges when any event is missing. It is
deliberately conservative: a missing file or one that does not parse is left alone, because
silently replacing a document we cannot read is worse than an unreported session. `csd-agent
uninstall` deletes the timer before removing the entries, so the repair cannot fight a
deliberate removal. Full argument in `docs/design.md` §41.

### `Notification` is an event, not a status

The session is blocked on you, and the dashboard shows it under *needs attention*. It stays
`running` while that is true, and goes back to being an ordinary running session the moment
anything else is reported — the type is an event precisely so it can clear itself. A status
would be sticky, and a session that was merely waiting for an answer could never honestly be
reported as working again. Full argument in `docs/design.md` §16b.

Claude Code re-fires this while it stays blocked. That is harmless: `cpctl` derives
`event_key` from the session, type and message, so a repeated notification is ignored rather
than stacking up rows.

### `Stop` is a turn boundary, not a session end

`Stop` fires every time Claude Code finishes *a reply* — the first one seconds after the
session opens. Wiring it to `session finish` would make every session terminal immediately,
and it would be permanent: terminal states are sticky and first-write-wins (§3), so every
later event would be silently discarded and the dashboard would show a wall of "finished"
sessions that are all still running.

Doing nothing is wrong in the other direction, and an editor makes it obvious: a VS Code
window keeps one Claude Code process alive for days, so `SessionEnd` does not fire between
prompts and the page showed `running` for hours after the work had stopped. `Stop` therefore
reports a **`turn_completed` event**, which moves no status. The dashboard renders it as
`idle` — a display state read off the newest event, cleared by the next `progress` — while the
session stays `running`, because the process really is still open (design.md §36).

The other events (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStop`,
`PreCompact`) are not wired, and that one is a choice rather than a rule: they fire
per tool call, and `PostToolUse` carries `tool_input` — for `Bash` that is your command line,
for `Edit` it is your code. Sending that to a cloud database is a privacy decision, not a
configuration detail. Turn them on deliberately if you want per-tool granularity; the
mapping is one line each in `csd_agent/hooks.py`.

`PostToolBatch` is the exception: it fires **once per batch** of parallel tool calls, not
once per tool, and the connector uses it only as a boundary — it never reads the tool calls.
That is what makes a checkpoint possible without uploading the user's code.

### A checkpoint is the agent's own sentence

`MessageDisplay` carries the assistant text as it is displayed. The connector appends each
batch to `tracked.json` and reports nothing at that point, because the message might still be
the answer. `PostToolBatch` is the proof that it was commentary: a tool followed it. Only
then is the first line sent as a `checkpoint` event, with `source: "prose"` and the agent's
`message_id` as its idempotency key.

A message with no tool batch after it is the answer, and `Stop` discards it. Without that
half, every reply would appear twice — once as a checkpoint, once as the turn boundary.

Both hooks run synchronously, and that is about ordering: `MessageDisplay` has to finish
writing the pending text before the model can ask for a tool, and `PostToolBatch` has to read
and clear it before the next message can replace it. An async flush could attribute the next
message to the previous batch. The cost is one `cpctl` call between a batch and the next model
call; the full argument is `docs/design.md` §37.

### A prompt is progress

`UserPromptSubmit` reports `Turn N: <first line of the prompt>` as a `progress` event, beside
updating the title. With the per-tool hooks off, that is the only "working on X" signal the
connector has between `SessionStart` and `Stop`, and without it an interactive session's
timeline held exactly one line. The turn number comes from `tracked.json` and is part of the
message because `cpctl` derives its idempotency key from the message — two turns whose prompts
read the same ("continue") must still be two rows.

A message the harness injects is **not** a prompt. A finished background task arrives on
`UserPromptSubmit` as a user-role message wrapped in `<task-notification>`, and a context
reminder as `<system-reminder>`; naming the session after either is how rows called
`<task-notification>` reached the deployed dashboard. Neither the title nor the `Turn N:` line
is written for them. The turn counter still advances, because `Stop` keys its boundary on the
number and reusing the previous one would deduplicate that boundary away. Full argument in
`docs/design.md` §39.

## Requirements

- Linux with systemd user units, and **lingering enabled** (`loginctl enable-linger $USER`)
  or the units die when you log out. The installer checks this and tells you — it does not
  need sudo on the machines this was built for.
- **Windows 10/11** with Git Bash (`bash` on PATH — Claude Code hooks call the wrapper
  through it), Python ≥3.10 including `pythonw.exe`, the OpenSSH client, and `curl`. The
  installer writes a Startup entry and a Task Scheduler task instead of systemd units; see
  [On Windows](#on-windows). No admin rights are needed.
- Python ≥ 3.10, which is what both SDK packages declare. Nothing here needs 3.11, and 3.10 is
  still common on agent hosts; the floor matches reality rather than making every deployment
  work around it.
- `ssh` and, for the self-check, `curl`.
- A Claude Code build that emits `MessageDisplay` and `PostToolBatch` if you want checkpoints.
  On a build that does not, those two hooks never fire and the rest of the connector is
  unaffected; the timeline then has turn-level progress only, which is the honest result
  (docs/design.md §37).

## Files

```
csd_agent/
  cli.py         the four verbs
  config.py      ~/.csd layout and agent.json
  runtime.py     running cpctl; a failed report is never an exception
  hooks.py       the Claude Code dispatcher — the only piece in someone else's critical path
  heartbeat.py   the liveness probe
  host.py        reporting this machine (and whether it may be driven), so a host is visible
                 before its first session
  access.py      installing the dashboard's public key, and removing it again
  tracked.py     which sessions are live, and how a pid is judged alive
  install.py     the installer, the units, and the settings.json merge
```

Everything the server is told goes through `cpctl` — this package speaks no HTTP of its own —
so `cpctl host report`, `cpctl host access` and `cpctl host access-report` are part of that
surface. `cpctl host list` is what lets a human ask the dashboard what it thinks is connected,
without SSH.
