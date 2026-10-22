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

## The four verbs

| Command | What it is |
|---|---|
| `install` | one-time setup; vendors the code, writes the env file and the service entries (systemd units, or the Windows Startup + Task Scheduler pair), merges the Claude Code hooks, wires every DSH profile and the Pi extension, verifies |
| `status` | what this host believes, which units are up, which DSH profiles carry the reporter, whether the Pi extension is in place, and whether the API actually answers |
| `heartbeat` | the probe. Run by a timer every 5 minutes; also fine to run by hand |
| `hook` | what Claude Code calls. Reads the payload on stdin; **always exits 0** |

`csd-agent dsh install\|status\|uninstall` is the same DSH wiring without the Claude Code half,
for a host that runs DeepSeek Harness but not Claude Code.

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
| `UserPromptSubmit` | replace the placeholder title with the user's first prompt |
| `Notification` | report an `awaiting_user` event — Claude Code saying it wants a human |
| `SessionEnd` | `finish --status completed` |
| `Stop` | **nothing** |

### `Notification` is an event, not a status

The session is blocked on you, and the dashboard shows it under *needs attention*. It stays
`running` while that is true, and goes back to being an ordinary running session the moment
anything else is reported — the type is an event precisely so it can clear itself. A status
would be sticky, and a session that was merely waiting for an answer could never honestly be
reported as working again. Full argument in `docs/design.md` §16b.

Claude Code re-fires this while it stays blocked. That is harmless: `cpctl` derives
`event_key` from the session, type and message, so a repeated notification is ignored rather
than stacking up rows.

### `Stop` is not a session end

`Stop` fires every time Claude Code finishes *a reply* — the first one seconds after the
session opens. Wiring it to `session finish` would make every session terminal immediately,
and it would be permanent: terminal states are sticky and first-write-wins (§3), so every
later event would be silently discarded and the dashboard would show a wall of "finished"
sessions that are all still running.

The other events (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStop`,
`PreCompact`) are not wired either, and that one is a choice rather than a rule: they fire
per tool call, and `PostToolUse` carries `tool_input` — for `Bash` that is your command line,
for `Edit` it is your code. Sending that to a cloud database is a privacy decision, not a
configuration detail. Turn them on deliberately if you want per-tool granularity; the
mapping is one line each in `csd_agent/hooks.py`.

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
