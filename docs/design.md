# Design decisions

Every choice here is one where the obvious option was rejected for a stated reason. If a
decision is not written down, it should be treated as not yet made.

> "The spec" in a few entries is the original product brief this project was built against.
> It is not shipped with the repository; the references are kept because they explain what was
> rejected and why.

---

## 1. Observability only. No control surface.

The dashboard reads. It does not start, stop, steer, or approve anything. This is not
timidity — it is what keeps the security story short. A service that can only *display* has no
code path that can execute something on the machine the work runs on, so the worst case for a
stolen read credential is "someone saw your task titles".

The pressure to add control will come fast (a "resume this session" button is 20 lines). The
answer is that it changes the trust model from "renders what it is told" to "acts on the
developer's machine", and those need different auth, different logging, and different
hardening. Do it as a separate service if at all.

## 2. Status is reported, never inferred.

A session is `completed` because a reporter said `session_completed` — never because it went
quiet, never because N events arrived. The one inference on the screen is
`possibly_stale: true`, and it is rendered as "maybe offline", in the quietest style on the
page, precisely so it cannot be mistaken for a status.

This costs something real: a crashed agent with no wrapper shows as "running, maybe offline"
forever instead of "failed". That is the correct trade. A dashboard that guesses wrong about
which task succeeded is worse than one that admits it does not know.

## 3. Terminal states are sticky, and first-write-wins.

Once a session is completed/failed/interrupted, no event or PATCH can move it back. Of two
conflicting terminal reports, the first one wins.

Without this rule, the final status depends on network arrival order. A `session_failed`
followed by a retried duplicate `session_completed` would flip the verdict — and the outcome
of a task is exactly the thing you must be able to trust.

`unknown` is a wall, not a downgrade: an operator who set it said "I do not know", and an
automatic event must not answer that for them. An *explicit* PATCH may still change it, and
`unknown → running` is allowed (resumed work).

## 4. SSE is a notification; SQLite is the record.

`GET /api/events` pushes `id:` frames where the id is a **database event id**, so a
reconnecting client passes `?cursor=<id>` and resumes from genuinely after it. The id
survives a service restart, which a per-connection counter would not.

What this explicitly does not promise: that a client away longer than the 2048-frame replay
buffer can recover from the stream. It cannot, and the client is required to re-fetch
`/api/sessions/{id}/events`. Claiming otherwise would mean the buffer had to be unbounded.

Filtered streams receive other sessions' frames as `: skip <id>` comments. That looks odd
until you realise the alternative: a client filtered to one session whose cursor silently
lags, which then resumes from the wrong place after a reconnect.

## 5. The reporter subscribes before it replays.

`Broker.replay_and_subscribe(cursor)` snapshots the replay *after* registering the
subscriber, so a frame published during reconnect lands in both places rather than in
neither. The duplicate is harmless — the client's cursor skips an id it already rendered. A
frame delivered zero times is not harmless, because the client has no way to know to ask for
it.

## 6. Reporting must never fail the work.

Every `cpctl` command has a 3 s timeout, bounded exponential backoff with jitter, and exits
3 on failure. `run-agent.sh` is written with `set -uo pipefail` and deliberately *not* `-e`,
traps INT/TERM to mark the session interrupted, and exits with the agent's status rather than
the reporter's.

The failure mode this prevents: an agent that aborts a 40-minute refactor at minute 39 because
a dashboard on a phone was unreachable.

## 7. Idempotency via `event_key`, not timestamps.

Retries are unavoidable — the CLI retries on timeout, and a wrapper script retries on exit 3.
Two mechanisms make that safe:

- `event_key` has a unique partial index, so a replayed event returns the original row instead
  of inserting a second one.
- The CLI derives a deterministic key from (session id, event type, message) when the caller
  does not supply one, so `cpctl event -m "Tests green"` run twice is one row.

Timestamps are not used for this: two reporters can legitimately report the same message in
the same millisecond, and a timestamp-based key would drop one of them.

## 8. Heartbeats renew liveness, not activity.

A heartbeat advances `last_activity_at` (which drives the "maybe offline" hint) but not
`updated_at` (which drives list ordering), and it is excluded from the timeline and from the
card's "last event" summary.

Without the split, 40 heartbeats during a long test run would bury the last thing the user
actually did — and worse, a session that is merely alive would outrank one that reported real
work.

## 9. One process, no broker, no container orchestration.

FastAPI serves the API, the SSE stream, and the built static files from one process, with
SQLite on disk. The spec's "no Redis/Postgres/Kafka/WebSocket/K8s in the MVP" is followed
literally, and it holds: the write rate is one agent's events, and the read rate is one phone.

The in-process SSE broker is the one piece that does not survive horizontal scaling. It is
documented as such rather than papered over — and if the day comes, the db-id cursor already
makes a Redis-backed version safe to introduce without changing the client.

## 10. Frontend: hash routing, `EventSource`, no framework additions.

Two pages, React, and nothing else — no router library (a 6-line hash parser), no state
library (two `useState` hooks), no CSS framework (one hand-written stylesheet).

Hash routing because the app is served at an unknown path behind a private address, and
`#/session/<id>` survives a mobile browser refresh without any server rewrite rule.

## 11. Tailscale: installed after all, and a public exception to §1

**Status changed on 2026-10-18.** The original text is kept below, because its reasoning did
not turn out to be wrong — it turned out to be the thing that happened again.

> The spec assumes a tailnet. An earlier Tailscale install on the deployment host removed the
> cloud backup for 13 days, because Tailscale's `ts-input` chain is rule #1 in INPUT and drops
> `100.64.0.0/10 !tailscale0` before conntrack — and the provider's own internal addresses
> (metadata, MQTT, OSS) are inside that range. Nothing in Tailscale's output indicated this.

What changed is that a phone needed to reach the dashboard, and an SSH client on a phone is
worse than a VPN toggle. So Tailscale was installed, and the same rule broke the host again —
this time taking **DNS** with it, because the DHCP-supplied resolvers (`100.100.2.136`,
`100.100.2.138`) are also inside `100.64.0.0/10`. The symptom was not a firewall problem: it
was a TLS error on a phone, because ACME could not resolve, so `tailscale serve` could not
obtain a certificate.

Two things are worth writing down from that.

**The pre-flight script had a hole, and reported health through the whole incident.** It
checked `iptables -L INPUT`; the rule lives in the nftables table `ip filter`, chain
`ts-input`, which `iptables -L` does not print. A check that reports `[ok]` by looking
somewhere the rule cannot be is worse than no check, because it is believed. It now treats
*reachability* as the verdict and rule inspection as the explanation — reachability is what
caught this, so it is what should be authoritative.

**The fix was `--netfilter-mode=off`, and that is defensible only on a host with no firewall of
its own.** It stops Tailscale managing iptables/nftables at all. Where the provider's security
group is the only gate, the rules being removed were protecting nothing the machine relies on
while actively breaking DNS. On a host that depends on its own firewall, the right fix is
explicit `ACCEPT` rules for the provider's addresses instead. The general lesson outlives the
incident: **do not let a VPN daemon own the packet filter on a machine whose data plane lives
inside the VPN's own address range.**

### The exception: Funnel

`tailscale funnel` publishes the dashboard to the public internet, the first thing in this
project to contradict the "zero public exposure" premise the rest of this section rests on.
It is a deliberate exception with a stated boundary:

- §1 still holds, and it is what makes the exception survivable. The service can only
  *display*, so the worst case of a leaked read token is what §1 always said it was: someone
  saw your task titles. That prediction is now load-bearing rather than hypothetical.
- `CSD_DASHBOARD_TOKEN` therefore stops being optional. It is the whole boundary between the
  internet and a list of what you are working on.
- The app shell — HTML, JS, CSS — is public and always will be; it is a static bundle. Only
  the data is gated. Stated so that nobody later mistakes "the page loaded" for "the
  deployment is unprotected".
- The read token may be passed as `?token=` **only because `EventSource` cannot set
  headers**. The ingest token is never accepted from a query string: it can write, and a
  write credential must not end up in a URL, a browser history, or an access log. Both halves
  are pinned by a test.
- The token is entered in the browser and kept in `localStorage`, never compiled into the
  bundle — on a public URL the bundle is public, so a baked-in token would be published.

Turning Funnel off needs no other change: `csd.service` never bound anything but
`127.0.0.1`, and both the SSH tunnel and `tailscale serve` reach it as a local port.

---

## Open questions

- **Should the CLI batch events?** It opens one HTTP connection per event. Fine for
  human-paced agent work; wasteful for a machine-paced loop that reports every step. A
  buffered mode (`--batch-ms 500`) would fix it but adds a flush-on-exit path that is its own
  bug surface. Deferred until something actually reports at high frequency.
- **Retention.** Events accumulate forever. The spec asks for "a simple mechanism" (§8.9);
  none is implemented. A `DELETE /api/sessions/{id}/events?before=` behind the ingest token is
  the smallest thing that satisfies it.
- **Multiple machines.** Session ids are server-generated, so two machines cannot collide.
  Nothing merges a session that moved between machines, and nothing needs to yet.
- **Idle agent processes (§17).** Only the `native` backend spawns anything: one Pi process
  per conversation, started lazily and stopped when the service stops, so a conversation
  opened last week still holds a process today. Fine at personal scale and wrong at any other.
  The nanobot backend has the opposite shape — one socket for everything — and needs nothing
  here. An idle timeout for the native case is easy to add; the question is what "idle" should
  mean for an agent that may be midway through a long tool call.
- **A native conversation does not survive a service restart.** Its Pi session file is on disk
  but the mapping from our thread to it is not persisted, so the next prompt starts a fresh
  session. The transcript is ours and remains; the agent's own context does not. Acceptable
  while `native` is the fallback option, and it is the first thing to fix if that changes.

---

## 12. UI：一份 token，两套主题

浅色为默认（白卡片、细边框、一个蓝色强调色），深色以纯黑为底。两套主题共用同一份 token，
每个颜色只声明一次、写成 `light-dark(浅色, 深色)`，由 `<html>` 上的 `color-scheme` 决定哪一侧
生效——所以手动切换只是一个 `data-theme` 属性，没有第二份调色板要同步。

具体 token 表、切换机制和已知取舍，见 [`design-ui.md`](design-ui.md)。

其中一条是功能性的而非审美的：**推断的状态绝不能长得像报告的状态**。`possibly_stale` 用空心
圆点、虚线胶囊和另一套措辞表达，只有真的报告上来的状态才用实心圆点和实色填充。

状态一律是「小圆点 + 文字」，卡片和行都没有左侧彩色竖条。

侧栏是三层（harness → project → session），因为"哪个 harness 在忙"和"它里面哪个任务在动"
是两个问题；两层时三个 `claude_code` 跨三个项目就是三行毫无关系的条目。

---

## 13. The agent host is one command, not a daemon

Connecting a machine that runs a coding agent is the project's job, not the operator's. That
is a statement about **who writes and maintains the integration layer** — it is not a licence
to reverse the direction of the connection.

Two things get conflated whenever this comes up, and they have different answers:

- **Who originates the connection.** Fixed by §1. If the dashboard could dial into an agent
  host, it would need a credential to that machine, and a stolen dashboard becomes a shell on
  a developer's box. The one-directional rule is what makes "the worst case is that someone
  read your task titles" true.
- **Who does the setup.** Entirely the project's. A host integration that requires the
  operator to mint keys, hand-write hook JSON, and remember that a half-open socket never
  trips `Restart=always` is not a feature, it is a runbook.

So `csd-agent` is the second without touching the first. It is deliberately **not a daemon**:
§9's "one process, no broker" holds. The only long-lived things it creates are systemd units
running `ssh` and a timer — not code of ours. Every command of ours is a short-lived process,
exactly like `cpctl`.

Worth recording because the pressure runs the other way: a scanning dashboard that dials out
to discovered hosts is a coherent product, and it is a *different* product with a different
trust model, different auth, and different hardening — the same conclusion §1 reaches about
control buttons.

## 14. The probe reports liveness, and an unknown pid is not a dead process

A long task produces no hook events. A 40-minute training run is indistinguishable, to the
dashboard, from a process that died an hour ago. So the agent host runs a probe on a timer
and asks the one question only a machine can ask about itself: *is the Claude Code process I
recorded still there?*

That is a fact, not an inference, which is what keeps it on the right side of §2. But it is
narrow, and the narrowness is the design:

- It reports a **heartbeat**, so §8 applies: it renews liveness, not activity. It cannot say
  "in progress". Nothing outside the agent can know that, and a dashboard that guessed would
  be worse than one that admits it does not know.
- **`pid` unknown is a distinct state from `pid` gone.** If the probe cannot identify the
  Claude Code ancestor, it reports nothing. Collapsing the two would mean announcing the
  death of a live session within five minutes of it starting, which is the worst bug
  available in this area — worse than silence, because silence is at least honest.
- The same probe closes sessions that ended without saying so. A `SIGKILL`, or an SSH session
  that drops and takes the process with it, never fires `SessionEnd`. The process being gone
  *is* the report.

The probe runs on the agent host because the answer does not exist anywhere else. Making the
cloud able to answer it would mean giving the cloud a credential to the host, which is §13's
first bullet again.

## 15. `Stop` is not a session end

Claude Code's `Stop` hook fires every time it finishes *a reply* — many times per session, the
first of them seconds after the session opens. Wiring it to `session finish` produces a
dashboard where every session is terminal after its first answer.

That would be merely wrong if it were recoverable. It is not: §3 makes terminal states sticky
and first-write-wins, so every subsequent event would be silently discarded — no error, no
gap in the timeline, just a wall of "finished" sessions that are all still running.
`SessionEnd` is the event that ends a session.

This is written down because it is a harness-specific trap, not a general principle. The
nanobot adapter (`csd_nanobot.py`) has the mirror-image hazard in its turn-level hooks, and
the same rule — a per-turn signal is never a lifecycle signal — is what resolves both.

Reporting the boundary *as an event* is a different question, and §36 answers it.

## 16. The one write, and why it is not a control surface

`POST /api/sessions/{id}/close` is the first thing in this project that a *person* can do from
the dashboard. It is worth being precise about why it does not break §1.

§1 forbids the dashboard from **acting on the machine the work runs on**. Closing a session
changes a row in the dashboard's own database. Nothing is executed, no machine is contacted,
no credential to an agent host exists. The blast radius of the control token is "someone can
mark your sessions interrupted" — which is a nuisance, not an intrusion. That is a different
kind of thing from approving a tool call or sending input to an agent, and §1's "separate
service" answer applies to those, not to this.

**What it is for.** A session created outside the reporting system — a hand-run
`cpctl session start`, a script, a test fixture that outlived its test — has no process behind
it. No heartbeat can probe it (there is no pid) and no `SessionEnd` will fire, so it sits at
`running` forever. §2 correctly forbids the *server* from resolving that out of silence:
silence looks identical whether the work finished, crashed, or was never real. But a person
looking at the row does know.

**Why `interrupted`, never `completed`.** The honest claim is "this stopped and no outcome was
reported", which is exactly what `interrupted` means. Letting the button say "completed" would
be the dashboard guessing an outcome, which is the one thing the entire status model exists to
avoid. It is also idempotent, because §3 makes it so: closing a finished session applies
nothing, which is why the button is not even offered for one.

**Why a third credential rather than reusing the ingest token.** The UI is served to anyone
who loads the URL. On a Funnel deployment that is the entire internet, so a button in it must
not be backed by the token that creates sessions and writes arbitrary events. Letting the
*read* token write would be worse still: §1's promise that a leaked read credential costs you
nothing worse than "someone saw your task titles" would stop being true.

Two asymmetries with `dashboard_header` are deliberate and easy to get wrong later:

- **Unset means disabled (403), not open.** A missing read token leaving reads open costs
  nothing on a private deployment. A missing write token leaving writes open would let anyone
  edit the record. Defaults have to be safe in the direction that matters.
- **Header only, never a query parameter.** The concession on the read path exists solely
  because `EventSource` cannot set headers, and nothing here is called by `EventSource`.


## 16b. Waiting on a human is an event, not a status

The overview groups a session under *needs attention* when it is blocked on an answer. That
state is read off an `awaiting_user` event at the head of the session's timeline. It is
deliberately not a sixth value in `SessionStatus`.

§2 and §3 are what decide it. A status here means how a session *ended*: the values are
terminal, and §3 makes them sticky. "Waiting for you" is the opposite kind of thing — it has
to disappear the instant the user answers. As a status it would be either sticky, so a session
that was merely waiting could never honestly be reported as running again, or it would need a
clear-protocol, and a flag that fails to clear parks a healthy session at the top of the board
forever.

As an event it clears itself: the dashboard shows the newest display event, so the next thing
the session reports supersedes it. No bookkeeping, no expiry to tune, and `status` is left
alone.

The only source is `Notification`, the hook Claude Code fires when it wants a human. The
dashboard never decides a session is blocked because it has been quiet, because its last line
of output looks like a question, or because a tool call has been outstanding too long. Quiet
is `possibly_stale` — an inference, labelled as one (§2). Waiting is a report.

The two do collide, because waiting is mostly *made of* quiet: a session blocked on a prompt
goes 15 minutes without a word, which is the staleness threshold. Where they collide the
report wins, and the card says what the agent is waiting for rather than "no activity
reported" — otherwise the inference would erase the one line that says what to do.

---

## 17. agenest is a channel, not an agent

The product grew a second page: a conversation with an agent, beside the sessions you watch.

The first attempt at this chapter argued for building the agent ourselves — a local kernel,
a process we spawn, tools we enable. **That was the wrong product**, and the reason is worth
recording because it is the kind of mistake that looks like progress:

> A control plane that only works with agents we wrote is not a control plane. It is a second
> agent, competing with the one the user already runs.

The thing being built here is a **control plane**. Its value is that it can sit in front of
*the agent you already have* — nanobot today, something else tomorrow — and be the place you
look at and talk to it from. Writing our own kernel optimised the demo and destroyed the
point.

So: this service is a **client of an agent**. The agent runs somewhere else, with its own
memory, its own model credentials, its own tools, its own workspace. We send it messages and
render what comes back. In nanobot's vocabulary, this dashboard is a **channel** — the same
kind of thing as its Telegram or Discord adapter, just with a richer screen.

### What this does to §1

It makes §1 *easier*, not harder. The original worry was that a dashboard which can act
becomes a dashboard that can execute things on your machine. A channel does neither: it
carries text to an agent that was already able to act, and which you already gave access to
whatever it has access to. The execution surface is nanobot's, on nanobot's host, under
nanobot's own configuration — and it was there before this page existed.

That is not a licence to skip the credential. Every `/api/agent/*` route that drives the agent
still requires the access token to *exist* — **unset = 403, disabled** — a page that can drive an
agent with shell access should not be open by default (§22). The three self-onboarding routes
that only declare are the exception, and §25 says why. But the promise is narrower and more
honest than it was in the previous draft of this chapter: holding this token lets you *talk to*
your agent, and the thing that decides what your agent may do is your agent's configuration.

`CSD_NANOBOT_TOKEN` is a separate credential and a genuinely different one: it is nanobot's,
it is scoped to nanobot, and leaking it grants nothing on this host.

### Why the WebSocket channel and not the OpenAI-compatible API

nanobot exposes two integration surfaces, and the obvious one is wrong for this client. The
HTTP API is OpenAI-compatible — `POST /v1/chat/completions` — which is what most tools would
reach for. Reading the source settles it:

```python
# nanobot/api/server.py
API_SESSION_KEY = "api:default"
session_key = f"api:{session_id}" if session_id else API_SESSION_KEY
```

**The API is single-session by default.** A request without an explicit `session_id` — and
that is a nanobot-specific body field, not something a standard OpenAI client sends — lands in
one shared conversation. Two conversations on this dashboard would be one conversation on the
agent, and the transcript on screen would stop being true.

The second problem is worse than the first: **the API only carries text.** Its SSE stream
emits `{"delta": {"content": ...}}` and nothing else. Tool calls, reasoning and file edits are
not available on that surface at all — they exist only on the websocket channel, where
`file_edit`, `reasoning_delta` and `session_updated` are first-class frames. A chat UI built
on the API could show that the agent is talking and not that it is *working*, which is most of
what this page is for.

So the websocket channel it is. Each `chat_id` is its own conversation, and the event
vocabulary covers everything the page renders.

**This is a nanobot-specific trap, not a general pattern.** A subagent survey of two
comparable projects found both are session-aware on their HTTP surface — OpenClaw via an
`x-openclaw-session-key` header or the standard `user` field, Hermes via `X-Hermes-Session-Id`
— and both also expose a fuller native protocol. nanobot is the outlier in collapsing to one
session by default. Worth knowing before anyone concludes "HTTP APIs are single-session" from
this one datapoint, and worth re-checking per backend rather than assuming either way. The
`AgentBackend` seam is what makes that a per-adapter question instead of an architectural one.

### Four backends, one seam

`app/agent/backends/` defines one interface and normalizes every backend's events into the
same five shapes: a delta, a message, a tool call, a status, a notice. Four implementations
ship:

- **nanobot** — the point of the exercise. A websocket client for its channel protocol.
- **native** — a Pi process this service runs itself, for when there is no gateway to talk to.
- **openclaw** — its Gateway WebSocket, with the `tool-events` capability.
- **hermes** — its API server (HTTP + SSE), the documented integration surface rather than the
  Desktop app's internal socket.

They disagree about almost every mechanism: nanobot multiplexes every conversation over one
socket and mints a `chat_id` per conversation; the native backend spawns a process per
conversation and mints its own id; OpenClaw is request/response over a gateway socket; Hermes
is HTTP plus SSE. They agree completely on what a conversation and an event are. That is the
line the seam is drawn on, and it is why another backend is an implementation of one interface
rather than a fork of the page.

**All four have been exercised against real instances** (OpenClaw Gateway 2026.10.4, Hermes
API server v0.18.2), and `docs/agent-onboarding.md` records the wire-level findings. One
adapter changed its protocol surface because of what the real server exposed: **Hermes creates
turns through the Runs API** (`POST /v1/runs` + `GET /v1/runs/{id}/events`), not Chat
Completions. The reason is stopping — a Chat Completions turn is not a run, so
`/v1/runs/{id}/stop` refuses its `chatcmpl-…` id with `404 run_not_found`; the runs surface is
the one whose id the stop route accepts, and cancellation comes back as `run.cancelled`. That
surface also carries tool completion errors and reasoning deltas (its tool events carry no call
id, which the page never used). Hermes' `/health` is unauthenticated — it proves liveness, not
a valid key — so the adapter validates the key against a real endpoint separately.

**Stopping is a per-protocol property, not a page feature.** Hermes stops over
`/v1/runs/{id}/stop`, OpenClaw over `chat.abort`, nanobot with its `/stop` chat command, and
`native` by stopping the process it owns.

`build_backend` is the only code in the service that knows more than one exists.

### Which one is default, and why it is switchable

**`native` is the default.** A fresh checkout with a provider key works: the Agent tab opens
and answers. Defaulting to `nanobot` would mean a page that is dead on first open — because it
points at a gateway the user has never heard of — and "why is this broken" is a bad first
impression for a feature whose whole claim is convenience.

That inverts the framing slightly, and the framing is what the settings panel exists to fix:
the product's *point* is still to be a channel onto an agent you already run, and nanobot is
one tap away in settings. Making it the default would have optimised the demo at the cost of
the first run.

**Switching is per-thread, not global.** A thread records the backend it was created against
and stays there. Re-pointing existing threads would either fail (nanobot has never heard of a
local Pi session id) or, worse, hand one conversation's history to a different agent while the
transcript on screen kept claiming to be that conversation. So the switch changes where *new*
conversations go, and the settings panel says so in as many words.

**The page's tap is one round trip.** Switching used to be three dependent requests — switch,
then the session, then its messages, each needing the last — and on a phone behind the Funnel
one round trip is about a second, so a tap read as "the transcript is slow to sync".
`POST /api/agent/switch` composes the three into one response and the page uses it; the three
primitives remain the API for everything else. This is a latency decision, not a second way to
switch: the composed route calls the same supervisor method and resolves the same session.

**And the payload, not the query, was the real cost.** A measured native conversation was
215 KB, of which **191 KB was tool rows** — rows the page hides by default. Two changes,
measured against the same conversation through the Funnel: gzip brought the transfer to 65 KB
(9 s → 2.1 s), and `?process=false` — the flag the page sends while its process view is off —
brought it to 5.6 KB (→ about 1 s, which is one round trip). Turning the view on refetches
with the rows. Both are transport decisions: the query itself was 12 ms on the box, and the
same history is always one query parameter away.

The choice persists in `meta`, not just in config, because someone who switched to a local
agent and restarted the service meant it — reverting them on every deploy would read as the
setting not sticking.

**The switch answers before the old backend is gone.** Its `stop()` is the slow half — Pi is
given a five-second grace period before it is killed — and awaiting it made the response arrive
after the call had already done its job, so the page sat blank while its transcript fetch waited
on a teardown nobody was waiting for. The switch is recorded first, then the teardown runs as a
task. Two consequences are handled rather than assumed: a stop still draining is awaited before
the *next* start, because a stop must never clear state underneath a process that just began;
and a stop that fails is logged instead of surfacing as a failed switch, because by then the
switch has happened.

### Seeing the process is opt-in

Reasoning and tool calls are hidden by default and shown from the settings panel. Two
consequences worth stating, because both are the kind of thing that looks like a detail and is
not:

- **When the toggle is off, thinking frames are dropped, not accumulated and hidden.** And the
  header does not say "thinking…" on the strength of them. Reporting a state derived from data
  the user has chosen not to see is §2's rule about inferred status, at the smallest scale it
  has appeared yet.
- **Hiding tool rows is not a lie by omission.** The answer that used them is still there. The
  setting is the user's explicit choice about how much machinery to watch, which is a different
  thing from the app deciding for them.

### The rules that survived the rewrite

- **Deltas are never stored; completed units are.** This is why the agent stream has no cursor
  and carries no `id:` — most of its frames are text that is superseded microseconds later and
  has no identity to resume from. An SSE id that looks resumable and is not would be worse
  than none, so the client's contract is to re-fetch the thread.
- **The agent stream uses `fetch()`, not `EventSource`**, so its token travels in a header
  rather than a URL. This matters less than it did when the token could execute shell
  commands locally, but it costs nothing and the alternative is a credential in browser
  history and every proxy log on the path.
- **A tool call that has not finished is not a result.** It is shown as running and written
  down when it completes. §2's rule about inferred status, at a much shorter timescale.

### What is deliberately not claimed

**This page is a mirror, not the record.** nanobot keeps its own sessions, and its `attach`
subscribes a connection without replaying history — there is no history endpoint to ask
(`api/server.py` is OpenAI-compatible only). So the transcript here is what *this channel has
seen*, which is exactly what an IM client can honestly claim. The same conversation continued
from Telegram will show up going forward, because nanobot emits `user_message` for input from
other channels, but it will not be retroactively filled in.

Stated because the alternative — implying this is the agent's memory — would be the same
category of error as the dashboard inventing a session status. It shows what it was told.



---

## 18. The native agent gets a memory, and it is not nanobot's

§17 ended by claiming the agent "runs somewhere else, with its own memory". That was true of
nanobot and **false of the default backend**. `native` is a Pi process this service spawns, and
Pi has no memory layer: it reads `AGENTS.md` and `CLAUDE.md` from the working directory and
nothing else — no journal, no cursor, no consolidation, no version history. So the default
backend is an agent that forgets everything between conversations, which is a worse first
impression than the dead page §17 was written to avoid.

This chapter adds that layer, distilled from nanobot's. Not copied: four of nanobot's five
mechanisms are worth having, and the fifth is worth having *precisely because* it cannot be
copied.

### What is copied, and what is not

| nanobot | here | why |
|---|---|---|
| Two clocks: fast journal, slow Dream | same | The journal is the hot path's waste product; Dream is the only place consolidation is affordable. |
| Cursor as a **book**, not a state | same | A lost cursor costs a re-read. A lost cursor must never cost history. |
| File routing table (SOUL / USER / MEMORY / skills) | same | One fact, one home. This is the whole defence against a memory file that grows until it is useless. |
| Commit message grounded in the **real diff** | same | The strongest idea in the design. See below. |
| "If it is already current, leave it alone" | same | A Dream that rewrites files to say the same thing is churn with a git history. |
| Four files at the workspace root, shared by every channel | same shape, **one conversation** | This deployment has exactly one conversation (the Agent page has no thread switcher), so there is nothing to scope the files *against*. The routing table does the partitioning instead. |
| Journal written by the agent process | **journal is derived from the agent's own record** | Neither of us writes it: it is generated from the transcript, which belongs to Pi. See below. |
| `history.jsonl` written by the consolidator, consumed by Dream | same shape, **different producer** | Ours is derived from the agent's record rather than from a copy this service kept. |

### The transcript belongs to the agent

Pi is the **kernel** of the agent this service offers, not a foreign process it observes. That
reframing decides where the conversation lives, and the answer is: in Pi. Its session file holds
the transcript verbatim — a message entry is `{type, id, parentId, timestamp, message}`, and
`_persist` writes `JSON.stringify` of the whole entry — its RPC answers `get_entries` with stable
per-entry ids and an incremental `since`, and `get_state` hands back the path. So we read it.

**There is no `agent_messages` table any more, and the path to deleting it is worth recording**,
because the first version of this chapter argued the opposite and the argument was wrong twice.

It first claimed Pi's session file had no message content — that it kept the shape of a
conversation and not its substance. That was simply false. Then it argued that a copy here was
better anyway, on the grounds that Pi's format "belongs to someone else" — which is not true of a
kernel we run ourselves, choose the argv of, and whose entire event vocabulary we already speak.
A false constraint is worse than a bad decision: it stops the decision being questioned.

What replaced it is one method on the seam:

```
async def history(conversation_id: str) -> list[Message]
```

The rule is about **process boundaries**, not about which agent:

| | Shared process, or protocol exposes it | Where the conversation lives |
|---|---|---|
| **Our Pi** | yes — we spawn it, RPC answers `get_entries` | Pi's session file. We store nothing. |
| **OpenClaw** | yes — `chat.history` is its documented client API | Its own. We store nothing. |
| **Hermes** | yes — `GET /api/sessions/{id}/messages` | Its own. We store nothing. |
| **nanobot** | no — `attach` does not replay, the channel carries no history frame | **Its adapter's own table**, because there is nowhere else to keep it. |

That last row is the general mechanism, not a nanobot special case: **a backend whose agent
cannot hand back a transcript keeps its own**, in its own file, with a schema it owns. Nothing in
the core knows about it.

The old shared table was the opposite of that, and its shape showed why. `(role, content,
tool_name, is_error)` is the *lowest common denominator* across agents, which means it is lossy
for every one of them: Pi's tool arguments, its thinking and its per-message model had nowhere to
go, so a re-read conversation could show that a command failed without showing which one — and the
journal inherited that. A shared schema for other people's formats is not a shared model; it is
everyone's leftovers.

Two consequences, both of which landed in code:

- **`Message` grew `id` and `args`.** The id is the agent's own and is what the page dedups on,
  because the page draws a message early from the POST response and meets it again in a
  re-fetched history. `args` carries a tool call's arguments, which existed only on the live
  `ToolCall` frame and were never recorded anywhere.
- **A sent message comes back with the agent's id.** `send()` returns the message as the agent
  recorded it, so the early copy and the history's copy are recognisably one, with no temp id and
  no reconciliation.

### The journal is derived from that record

`history.jsonl` is generated from `AgentBackend.history()`, on a trigger we already have:

- The trigger is `agent_settled`, the frame that means the agent has stopped continuing on its
  own. It is the same event the adapter turns into `Status(streaming=False)`, which is why the
  hook sits above the seam rather than in each adapter.
- The cursor lives in `meta` and holds **an id the agent minted**, so it is opaque: the only thing
  that can be asked of it is whether it appears in the conversation, which is why matching is by
  position rather than by value. A cursor that cannot be found means the transcript was replaced,
  and the safe reading is "start over" — re-reading costs tokens, losing a message costs memory.
- The trigger is **idempotent by construction**: the journal is re-derived from the messages after
  the cursor, so a re-fire, a restart, or a crash between the write and the cursor update all
  converge on the same file. A double append is impossible because there is no append.

nanobot's journal is written by the agent and read by Dream, so a bug in its writer silently
corrupts the memory. Ours is written from the same record the page renders, so **the journal
cannot disagree with the transcript** — it is the transcript.

### Memory is read back, or it is not memory

Everything above writes. Writing is not the effect: nanobot's agent remembers because its
`ContextBuilder` rebuilds the system prompt from `AGENTS.md`, `SOUL.md`, `USER.md` and
`memory/MEMORY.md` every turn. Pi has no such layer built in — but it does have a per-prompt
hook, so the recall half is ours, and it is now assembled the same way nanobot assembles its own.

- **A Pi extension re-reads the memory on every prompt.** `before_agent_start` fires for each
  user message and may replace the system prompt for that run; that is the same place
  `build_system_prompt()` runs in nanobot. The extension (`app/agent/pi/recall.ts`, loaded with
  `-e`) reads one generated file and appends it. It arrives as a *system prompt*, never as a
  message: a message would enter the transcript, be journaled, and be summarised as something
  the user said.
- **The text lives in Python, not in TypeScript.** `memory.recall_prompt()` builds it and
  `write_recall_file()` regenerates it; the extension's whole job is "read that file, append it".
  Formatting the prompt in two languages would be two answers to "what does the agent's memory
  say", and only one of them is covered by the test suite. The file is rewritten when a Dream
  commits, when a Pi process starts, and when a run settles — the last of which is what makes a
  hand-edit of `MEMORY.md` reach a running agent.
- **The fallback is the old start-up append.** If `recall.ts` is missing, the backend passes
  `--append-system-prompt` instead and calls `refresh_context()` after a Dream, which restarts an
  **idle** Pi process — never a streaming one, because stopping a turn the user is waiting on
  would abandon it. Exactly one of the two mechanisms runs, so the memory never appears twice.
  (`--append-system-prompt` is also argv-sized — 16k, against Windows' 32,767-character command
  line — which is a second reason not to make it the primary path.)
- **The journal is named in the appendix**, so the agent can read or grep the detail the summary
  was compressed from — the job nanobot's `memory` skill does.
- **Skills are passed by path** (`--skill <memory>/skills`). `memory/skills/` is not one of the
  locations Pi scans, so Dream's fourth writable path would otherwise be written and never
  discovered. Pi's progressive disclosure keeps only each skill's name and description in the
  prompt until the body is read — cheaper than inlining instruction sets, and it leaves the
  stable prefix alone so the cache survives.

What this costs is now the same as what it costs nanobot: the appendix is part of the prefix, so
a change invalidates the cache from there on, once. Nothing is paid per turn when nothing
changed, and no process is restarted to deliver an update.

### What this costs, stated plainly

**A conversation recorded before this change needs the upgrade's help.** `native` passes
`--session-id`, which Pi uses to find or create a session — but sessions written before that
change have auto-generated names Pi cannot match to our id. `agent/legacy.py` runs once at
startup: it attributes those files to a thread by header `cwd`, by falling inside the thread's
lifetime, and — while the old shared table still exists — by containing a user message within
seconds of one of that thread's rows. The answer is recorded in
`pi-sessions/.legacy/<conversation id>.json`, and `history()` reads the mapped files as a
prefix. It is a mapping rather than a rename because one thread can own several files (every
restart used to start a new one), and merging them would break the `parentId` chains Pi resumes
from. The same one-shot pass copies old nanobot rows into that adapter's own table before
dropping `agent_messages`; a failure leaves the table and retries on the next start.

**The recovered prefix is for the page and the journal, not for Pi's context.** The next prompt
starts a session Pi can match to the thread's id, and the model sees only what is in that file.
Merging old entries into the resume path would mean reconciling separate trees; stating the
limit is better than a transcript that says one thing to the reader and another to the agent.

**Thinking is still not in a re-read conversation.** Pi records it and history drops it, matching
the live behaviour where thinking streams only while the process toggle is on. Including it would
mean a fourth `AgentRole`, and that is a UI change rather than a storage one.

**And one thing that is *not* a cost, because the first version of this section claimed it was.**
That version had `history()` go through the RPC: start a Pi process, then ask it for `get_entries`.
That would have made rendering the page depend on the agent being up, and it would spawn a Node
process — running whatever its extensions and MCP servers do on startup — to read a file sitting
on disk. The defence was that the RPC returns a parsed, version-stable shape while the file is a
versioned format we would have to parse ourselves.

That defence fails on its own terms. Pi is our kernel: the same file is what Pi itself parses on
resume, so depending on it is not more coupling than depending on `get_entries` — and in exchange
we gave up a viewer that renders with the agent down, which is a property worth more than the
parsing we avoided. `history()` reads the file. The RPC path survives only where a process is
necessarily alive and its in-memory view is the consistent one.

### What Dream may write, and why the list is short

Dream is a Pi process of its own, in its own workspace, with a restricted tool set. It may write
exactly four paths and nothing else:

| Path | Content |
|---|---|
| `MEMORY.md` | What remains true about the work: architecture, decisions, dead ends already ruled out. |
| `USER.md` | Who the user is: language, level, how they want answers framed. |
| `SOUL.md` | How the agent should sound. |
| `skills/<name>/SKILL.md` | A workflow that has recurred at least twice. |

All four live directly under `memory/`. The routing table above is what partitions them, and that
is enough: `USER.md` answers *who is asking*, `MEMORY.md` answers *what is true about the work*,
and `SOUL.md` answers *how to sound*. A per-conversation directory was in the first draft of this
chapter and is gone, because the Agent page carries exactly one conversation — there is nothing to
scope the files against, and a `threads/<id>/` level would be a directory that always holds one
entry.

Everything else is refused, and the refusal is not decorative. Dream's prompt asks for surgical
edits to files it can already see; its tool set is `read`, `edit` and `write` restricted to the
four paths above. A model that decides the memory would be better as a Python module has no way
to write one.

### Why the commit message comes from the diff, not the model

Dream's own summary of what it changed is worth recording, and worth **not** trusting as the audit
record. It is the same model that just edited the files, so a self-report is an audit performed by
the auditee: it will say "consolidated memory" whether it consolidated memory or truncated it.

So the commit subject is derived from the filesystem — which of the four files changed, how many
lines in each, and the first line of each hunk — and the model's narrative goes in the body,
clearly labelled as its own account. This is `MemoryStore.build_dream_commit_message` and
`GitStore.summarize_working_tree`, and it is the single idea most worth stealing from nanobot: it
turns memory from a silent mutation into an auditable process, for the price of a `git diff`.

One consequence is worth stating plainly. **The commit is written by this service, with this
service's git identity, into a repository this service initialises.** nanobot can use dulwich; we
shell out to `git`, because a memory repository that needs a Python git library to read its own
history is a worse trade than one `subprocess` call — and `git` is already how the agent's own
workspace is versioned.

### A run ends when Pi settles, not when the process exits

`PiDreamRunner` waits for the `agent_settled` frame. **It does not wait for the process to
exit**, and the difference is a hang rather than a subtlety: Pi in RPC mode is a long-lived
server that goes on reading stdin after a run finishes, so waiting on exit waits forever. Dream
had exactly this bug, and it presented as "dream failed; will retry" on every run — a message
that describes a timeout while looking like it describes a crash.

`agent_settled` is also what the supervisor's journal hook fires on, and for the same reason: it
is the frame that means "nothing more is coming". `agent_end` fires once per low-level run and
would return mid-conversation.

The process is still stopped in a `finally`, because a Dream left running holds the memory files
open and the next run's diff would be against a moving target.

### The failure mode that decides the cursor

Dream runs on its own clock, and the clock is not the point. The point is what happens when it
does not finish.

- **The cursor advances only on completion.** A Dream that crashed, was cancelled, or produced
  nothing leaves `.dream_cursor` where it was, so the same batch is retried. Retrying is safe
  because Dream's edits are idempotent *given the same inputs* — and the inputs are the same,
  because the journal has not moved.
- **A completed Dream that changed nothing still advances the cursor.** Otherwise the journal
  grows forever and every run re-reads it. "No update needed" is a result, not a failure.
- **A Dream that writes nothing at all is a failure and does not advance.** The distinction is
  whether the files moved, not whether the model was pleased with itself. That is the
  diff-grounded rule again, applied to the cursor rather than the commit message.

### What this does to §1

§1 says the dashboard has no control surface. This is not one. Dream writes to a directory the
operator configured, under a service the operator runs, on a schedule the operator set.

The analogue of nanobot's "automatic compaction does not post lifecycle notices to built-in chat
channels by default" is that **a Dream run posts one line to the thread it consolidated and
nothing else** — and, like nanobot's, that line is a notice, not a message. It is not part of the
transcript, so it is never summarised into the next journal entry.

The one new authority is `CSD_MEMORY_INTERVAL_H`, and it is **on by default at nanobot's own
interval** (`DreamConfig.interval_h` = 2 hours). The first version of this chapter defaulted it
to **off**, on the argument that a channel must not spend the operator's model budget on its own
initiative. That decision is reversed here rather than quietly dropped, because the argument was
answered rather than overruled: once the agent re-reads its memory on every prompt, a layer
nobody switches on is a layer that does not exist — every answer is missing the memory and
nothing on screen says so. The knob for the cost is the interval: raise it, or set 0 and run
Dream by hand.

### Memory belongs to the agent, not to the channel

Only the `native` backend gets one, and that is a rule rather than a limitation.

nanobot has its own memory — that is most of what it *is*. Giving it a second one here would mean
two stores drifting against each other while the screen shows one of them, and no way to say
which is true. The same holds for a backend added later that keeps memory of its own. **A
channel mirrors what it has seen; it does not keep a parallel copy of what the agent knows.**

`native` is the exception because it is the one backend with no memory at all: it is a Pi
process, and Pi reads `AGENTS.md` from the working directory and starts every conversation from
nothing. So this is not "memory is supported on `native` for now" — it is "memory is written for
exactly the agents that have none". Adding a third backend does not automatically add a third
memory store; it adds a backend whose memory is its own business.

The mechanical expression is one constant, `supervisor.MEMORY_BACKEND`. A thread whose backend is
not that one is never journalled, so switching agents does not spill one agent's conversations
into the other's memory.

### What is deliberately not built

- **No memory search UI.** The journal is greppable and `MEMORY.md` is readable, which is the whole
  point of the filesystem-and-evidence route. A search box would imply an index we do not have.
- **No decay, no half-life, no `valid_at`.** The strong version of this rule is the one taken.
  Ours is the weak version — a human can read the file, see what is stale, and say so, and the
  Dream prompt carries a routing table that tells it to remove superseded facts.
- **No cross-harness memory.** `cpctl` reports sessions from other agents; this memory belongs to
  the agent on this page. Merging them would mean deciding which agent's recollection is true.
- **`nanobot` is unchanged.** Its memory is its own, it works, and the seam (§17) is what makes
  leaving it alone possible.

## 19. The agent has one conversation, not many

The Agent tab used to behave like a chat app: a `+` for a new conversation, a drawer listing the
old ones, and a settings row to switch between them. All of it is gone. There is one conversation
per agent, and talking in it is the only thing the page does.

The obvious option was to keep them, and it is the one every chat UI takes. It was rejected for a
specific reason rather than for simplicity:

> **A list of conversations is a filing system, and this page was never a filing cabinet.**

The dashboard beside it already answers "what have I been working on" — that is what the rail is,
and it is backed by real sessions that real harnesses reported. A second, weaker list of things
*the agent* remembers is a worse answer to a question that is already answered, on the tab next to
it. Two lists that both claim to be your history, and disagree, is the failure being avoided.

What replaced it is `POST /api/agent/session`: get the conversation, create it on first use.
Idempotent, so the page calls it on every boot without first asking whether one exists. It is
scoped to the **active backend**, and that detail is load-bearing — a thread records which agent
it belongs to (§17) and keeps talking to that one, so returning the globally newest thread after a
switch would show one agent's transcript while routing the next message to another. That is the
silent context loss §17 exists to prevent, reintroduced through the back door.

`POST` rather than `GET` because on a fresh install it really does write a row, and calling that a
read would be a lie about a route a client might cache.

Three things follow from the reduction, and each is a decision rather than a side effect:

- **The header's subtitle no longer shows a title.** It carried the conversation's name; with one
  conversation that name is a fact the user already has, and it was competing for the line with
  the one thing not knowable from the screen — whether the agent is working. The subtitle is now
  busy, offline, or nothing.
- **The avatar-and-name-per-message problem never appears**, because there is no list to
  disambiguate rows of. `styles-agent.css` never had to grow the "who said this" affordances a
  multi-conversation view would need.
- **`store.list_threads` is gone with the route that used it**; `latest_thread(db, backend=…)`
  replaced it, and `create_thread` survives because the session route calls it. Removing the
  *product* feature did not mean removing the storage: a deployment that already has conversations
  keeps the one it was last using, ordered by `updated_at`.

A consequence worth stating plainly, because it is the part that will surprise someone: the
database still holds the old conversations, and they are still reachable by id through
`GET /api/agent/threads/{id}`. What is gone is any *path through the UI* to them. That is
deliberate — the alternative was a migration that deletes a user's history to make a UI simpler,
which is not a trade this project makes.

What this does to §2: nothing. The page still reports and never infers; there is simply less of it
to report. The one new claim it could have made — "you have N conversations" — is one it no longer
makes at all.

### One conversation means one context, and it only grows

The reduction has a cost the multi-conversation design spread around: this conversation never
ends, so its context — and the per-turn price of it — only goes up. Pi compacts on its own when
the window is nearly full (its `threshold` / `overflow` reasons), but that is the *last* moment,
and it happens while the user is waiting.

So the service also compacts **when idle**: after `CSD_AGENT_IDLE_COMPACT_MINUTES` (default 15,
nanobot's own idle-compact threshold) with no activity, a scan asks the native backend to send
Pi's manual `compact` command. The division is the one the memory layer already uses — the
supervisor owns the clock, the backend owns the rules:

- **Only a live process is compacted.** Spawning Node and paying for a summary call on behalf of
  a page nobody has open is the opposite of saving anything.
- **Never mid-turn.** Pi's manual compaction aborts the current operation first, so both the scan
  and the backend check for an in-flight run.
- **One attempt per new message.** Pi refuses a session that is small or already compacted;
  remembering the newest entry id at the attempt stops an idle conversation from asking — and
  logging — every minute.
- **A refusal is a result.** "Nothing to compact" is the normal state of a short conversation,
  not a failure to retry.

The page is unaffected: Pi keeps the pre-compaction entries in its session file and the history
reader still returns them. Compaction changes what the *model* sees, not what the record says —
the same distinction §18 draws for memory, applied to the context window.

## 20. A host is not a session

The Sessions list used to be derivable entirely from sessions, and that turned out to have a
hole in it. Every group in the rail was built by grouping the sessions it had; a machine that
had connected but had nothing running on it produced no sessions, therefore no groups, therefore
nothing on screen at all. Telling "this host is set up and reachable" apart from "this host was
never set up" meant SSH-ing in and running `csd-agent status` — which is the exact errand §13
exists to delete.

It was worse than a missing row. `heartbeat.run()` returned early when `tracked.json` was empty
(`if not entries: return 0`), so a freshly installed connector reported *nothing*: not a
heartbeat, not a hello, nothing until the user happened to start their first Claude Code session
on that box. The feature that was supposed to make a host visible was silent in precisely the
state it was needed for.

### Why the host is its own table

The obvious option was to register the host as a session — `agent_type: "csd-agent"`, created on
install and kept alive by the same heartbeat. It needs no new table, no new endpoint, and no new
UI. It was rejected because it would make `sessions` mean two things:

> **A session's status says how work ended. A host is reachable, which has no outcome.**

`completed`, `failed` and `interrupted` are the vocabulary of a task finishing. A host row has
nothing to put there: it is `running` for as long as the box exists and then it is not, and the
only honest status for "the machine went away" is one nobody will ever report. Filling a column
with a value that can never be meaningful is how a schema starts lying, and every reader of that
column afterwards has to know which rows were the exceptions.

It is also the wrong *shape*. §2 says a status is reported, never inferred — and a host's status
cannot be reported at the one moment it matters. Nobody sends a message when a machine is
switched off. So a host entity needs a different temporal model from the start: `last_seen_at`
and a `possibly_stale` hint, with no verdict at all. §8's rule (a heartbeat renews liveness, not
activity) was already the right shape for it; what was missing was a place to put it.

### The identity is `(label, agent_type)`, not `label`

A connector speaks for **one coding agent**. A box running Claude Code and codex connects twice,
through two packages, and reports twice — and those are two facts, not one fact refreshed. Keying
on the label alone would make the second connection look like an update to the first, and the
audit question you actually have ("which of my agents is reachable on that box?") would be
unanswerable.

That has a consequence one level up, in the UI, which is worth writing down because it is easy to
get wrong in either direction: the registry holds a row per *connector*, but the rail draws a
node per **machine** — otherwise a two-agent box lists its sessions twice, once under each
connector. The node's connection state is then the most live of its connectors (connected → stale
→ disconnected), because a machine cannot be connected and not connected at once, and letting a
codex connector uninstalled last week drag a healthy Claude Code box down to "disconnected" would
be the wrong answer to the question the level is asking. `frontend/scripts/check-rail-tree.mjs`
pins both halves.

### Silence produces no status, and the empty group says so

A host that stops talking is never marked disconnected. It goes `possibly_stale`, on the same
threshold and with the same hollow-dot rendering a quiet session gets — because the two are the
same situation, and §2's smallest rule applies at this scale: *an inferred state must never look
like a reported one*. `disconnected_at` is set only when a connector says goodbye, which
`csd-agent uninstall` does explicitly, before it stops the units (for a remote host the tunnel is
what carries the message, so saying it afterwards would be talking into a closed socket).

The corollary is the part that looks odd until you know why: **a connected host with nothing on
it draws an empty group, in words** — "Connected — no sessions running on it." An empty group on
its own is indistinguishable from a rendering bug, and the whole point of the level is that this
state is real.

A machine name that appears only on sessions — `cpctl session start --host lab-box` from a box
whose connector was never installed — still groups its work correctly and gets **no** connection
marker. There is a row for it in the tree because the sessions are real; there is nothing to say
about the machine because nothing was reported. Inventing a marker from the absence of a row is
the inference this design keeps refusing.

### Host frames borrow a cursor instead of minting one

§4 promises that the `id:` on the wire is a **database event id**, which is what makes a cursor
survive a service restart. A host update has no row in `session_events` — there is no session for
it to belong to — so it cannot mint an id of its own.

The option was a second numbering space for host frames. That would quietly break the promise: two
spaces interleaved on one stream means a cursor is no longer one number, and the client would have
to know which frames carry which kind of id to resume correctly.

Instead a host frame is published **anchored** to the newest event id, which is the move
`PATCH /api/sessions/{id}` already makes for a metadata-only patch. The cost is that such a frame
is not replayed to a client whose cursor is already past that id — and that is acceptable for the
same reason it is acceptable there: a host frame is a whole replacement of the client's copy, so
replaying one is idempotent, and a client that reconnects re-fetches `/api/hosts` alongside the
session list anyway.

### What this does to §1 and §2

Nothing, and that is the point of putting the new table next to the old ones rather than inside
them. Hosts are still reported by the host's own side, in the direction §13 fixes; the dashboard
still never dials out and still cannot start or stop anything. And §2 is intact in both
directions: a host is connected because a connector said so, offline because a connector said so,
and "maybe offline" is the only thing ever derived — rendered so it cannot be mistaken for a
report, exactly like a session's.

## 21. The agent runs on the same box, so the box had to change

§17 settled that agenest is a channel onto a personal agent, and the direction rule at the top of
`CLAUDE.md` settled that a personal agent is a *local* component of the control plane. Deploying the
dashboard to a cloud host therefore did not create an option where the page in the cloud talks to the
agent on the operator's laptop: the service is the client, the agent sits beside it, and the only way
to reach across would be for the service to dial out — which §1 and §13 forbid. So enabling the agent
page on a cloud host meant **installing an agent on that host**.

That is one decision with five consequences. Each was measured rather than assumed, and each is
reproducible from `deploy/scripts/enable-agent.sh`.

### Node arrives on a box that had none

Pi is an npm package and the server had no `node` at all. It cannot reach GitHub, so code arrives by
tarball — the README already leans on that — and npm's own registry answered in 9.8 s against
npmmirror's 0.32 s. Node 22.23.3 came from nodejs.org with its SHA256 verified. The version matters:
Pi's floor is `>=22.19.0`, so Ubuntu 24.04's own `nodejs` package is too old to be an option.

### Pi is pinned, not floated

`@earendil-works/pi-coding-agent@latest` is 1.3.0. The deployment installs **0.84.1**: the version of
the `pi-mono` checkout this repo names as the authority on the native backend, and the version every
protocol test and every verification in `HANDOFF-native.md` was run against. `rpc.py` parses Pi's
JSONL frames — `agent_settled`, `message_end`, the reply to a manual `compact` — so a runtime whose
frame shapes are the other half of that contract is not a dependency to take on trust because it is
newer. The flags (`--mode rpc`, `--session-id`, `--skill`) do still exist in 1.3.0; the frames were
not tested, and "the flag is still there" is not evidence about the protocol. An agent runtime is a
dependency with a contract, and contracts get pinned.

### `MemoryDenyWriteExecute=true` could not stay

This is the interesting one, because the option was correct when it was written and became wrong
without anyone editing it. It forbids the service's cgroup from ever holding memory that is writable
*and* executable, which is free protection for a process that only reads a SQLite file — and fatal
for V8, whose JIT writes machine code into exactly those pages:

```
systemd-run -p MemoryDenyWriteExecute=yes pi --version
  # Fatal error in , line 0
  # Check failed: 12 == (*__errno_location()).      ← EACCES on the code-range mmap
systemd-run -p MemoryDenyWriteExecute=no  pi --version
  # 0.84.1
```

So the unit sets it to `false`, with that measurement quoted in a comment beside it. The rejected
option was keeping the hardening and running Pi `--jitless`: that trades away a real capability — an
agent that can run code — for a flag whose purpose here is served by `ProtectSystem=strict` plus
`ReadWritePaths=/srv/csd/data`, which bounds *where* the agent can write and is what actually keeps
it out of the deployed source. What was given up is precise and should be named plainly: the unit no
longer promises the process cannot create executable memory. Pi is an npm package we install
ourselves, pinned above, not untrusted native code.

### HOME, and the workspace, had to be pointed into the writable tree

The `csd` account's home is `/srv/csd`, and `ProtectSystem=strict` leaves only `/srv/csd/data`
writable. Pi writes `~/.pi/agent/models-store.json` on its first run, so the default would have
produced an EROFS naming a path nobody configured. The unit therefore sets
`Environment=HOME=/srv/csd/data/agent-home`. Confirmable after the fact: that directory now contains
`agent-home/.pi/agent/{auth.json,models-store.json}`.

The same reasoning from the other direction is the workspace. `CSD_PI_WORKSPACE` defaults to Pi's
cwd, which under the unit is `WorkingDirectory=/srv/csd/backend` — the deployed source. `dev.sh`
already refuses to hand an agent the repo root for exactly this reason; the deployment now names the
workspace (`/srv/csd/data/agent-workspace`) instead of inheriting one by accident.

### The memory argument, with numbers

The question that decided this was whether a second Node process is affordable on a 1.7 GB host.
Measured on the running deployment: Pi 126 MB resident, the whole `csd` cgroup 125 MB *including* it,
against nanobot-gateway's 222 MB and roughly 1 GB available. Two things were changed anyway, because
the *shape* of the failure matters more than the average case: `--max-old-space-size=384`, so a
runaway agent dies as its own process rather than taking the box with it, and a 1 GB swapfile, because
the host had **no swap** — and without swap the failure mode is not slowness, it is the OOM killer
choosing the largest process on the box, which there is nanobot. Dream is what makes that concrete:
it constructs its own `PiProcess` (`agent/memory.py`), so a consolidation is a second ~130 MB
resident for as long as it runs, on a 2-hour clock by default.

## 22. One token, and one rule that is deliberately not the same everywhere

Until 2026-10-19 this page had three credentials: read, control, agent. They were collapsed, on the
operator's call, into **one access token** (`CSD_DASHBOARD_TOKEN`). The argument for the split was
reach — the read token could only read, a separate one could close a session, a third could drive an
agent with `bash` on the host — and that argument is still true. What it was not was *useful* here:
three secrets guarding one page on a single-operator box is a login ritual, and the cost was the
thing this page exists for — you paste a link, it opens, and you are looking at your agent. The
rejected option is the one this file would previously have defended: keep the three.

**One token is not one rule.** The collapse is about the *credential*, not about what happens when
there is none, and the asymmetry is the part that matters:

| Route | Token unset | Token set, caller presents nothing or the wrong thing |
|---|---|---|
| reads (`GET /api/sessions`, the SSE stream) | **open** | 401 |
| anything that acts (`/api/agent/*` except the three self-onboarding routes — §25, `POST /{id}/close`) | **403, disabled** | 401 |

A missing read token leaving reads open costs nothing on a private deployment. A missing token
leaving *acting* open would hand out a shell and a session-closer to whoever loads the URL, which is
the one default in this codebase worst to get wrong — so `auth.access_header` refuses to run at all
while the token is absent, and `auth.dashboard_header` keeps returning. Two dependencies, one
credential, and the difference is a property of the route rather than of the secret.

**The ingest token stays separate**, and it is not a page credential at all: it is what a *host*
presents when it reports work (`POST /api/sessions`, events, `PATCH`). It has to be issuable to
machines that should not be able to drive an agent, so collapsing it would be the collapse that
actually costs something.

**What was given up, precisely.** The credential that drives an agent is now also the credential the
read path accepts as `?token=` and that we hand out as `#token=<value>` links — so it lives in a
browser's history and in whatever chat app delivered the link. That is a deliberate trade, not an
oversight, and it is why `auth.access_header` still refuses `?token=`: even the one credential must
travel in a header when it is about to execute something. A deployment that wants the old separation
back puts a different value in a separate variable; there is no longer a second variable to put it
in, and that is the point.




## 23. The agent works, and it says what it is doing

Two things changed together on 2026-10-19, and they only make sense together: the agent page stopped
being a window onto a chat and became a way to **get work done**, and the first thing that needed was
for a person watching a phone to be able to tell that work was happening.

### Progress: a checkpoint, not a commentary track

A turn that calls tools five times used to produce five silent seconds and then an answer, or — worse
— the model's half-sentences interleaved with the tool rows, indistinguishable from the answer that
followed them. The fix is nanobot's, and it is the operator's own contribution upstream
(`templates/agent/progress_notes.md`, refs #6000): the model is asked to write **one short line per
stage**, in the same message that carries that stage's tool calls.

- **Why that position and not another.** nanobot's switch defaulted to *true* for a long time while
  delivering nothing, and the cause was a model told to be terse in one line of its contract and to
  plan out loud in another. A progress note is only delivered if it rides in the message that has the
  tool calls; text written after them, or in a separate turn, reaches nobody. So the instruction says
  exactly that, twice, in the two ways it can go wrong.
- **One shape, not a new frame.** `Message.progress` marks it, rather than a `Progress` event on the
  backend seam. That was the first design, and implementing it produced the counter-example: nanobot's
  transcript is the *adapter's own table*, so a live-only frame would lose every checkpoint on the
  next refresh — for that backend, the whole feature. A checkpoint is text the agent recorded; the
  only shape that survives a re-read is the one everything recorded uses. It also means one rendering
  path on the page, and rows that dedup like any other message instead of needing a second identity
  scheme.
- **It is not machinery.** Reasoning and tool rows sit behind "show agent process" (default off)
  because they are the machinery behind an answer. A checkpoint is prose addressed to the reader, so
  it is shown regardless — hiding it would recreate exactly the blank screen it exists to fill.
- **One shape for the display, not only the record (2026-10-20).** The checkpoint used to be drawn
  as a quiet stage line of its own, precisely so it could not be mistaken for the answer. It is now
  an ordinary incoming bubble. The special display was a promise only some backends can keep — an
  agent whose protocol streams one undivided blob has no checkpoints to draw — and a page that
  shows stage lines for one agent and then goes silent from prompt to answer for the next is worse
  than a page whose shape does not change. The cost is stated, not hidden: a checkpoint now looks
  like a reply, and the tool rows are what still distinguish "working" from "answering" — for an
  agent with no process rows they were already the only such signal. (The text-only nanobot door
  this once singled out is gone; §25.)
- **On by default** (`CSD_AGENT_PROGRESS`, `0` to disable), which reverses nanobot's default on
  purpose: there the switch promised output a default install could not produce; here the instruction
  that produces it ships with the service.

The instruction reaches Pi through the same per-turn appendix as the memory (`pi/appendix.ts`), which
is where the agent's other standing instruction now lives too.

### The agent reaches other machines, and the service still does not

`CLAUDE.md` opens by describing this project as read-only observability, and §1 forbids the service
from acting on the machine the work runs on. Neither is repealed: the dashboard page is still a
read-only view of sessions, and **this service still opens no outbound connection of its own**. What
changed is what the *agent* is allowed to do, and the distinction is not a technicality — it is who
holds the credentials:

- The agent runs `ssh box 'cd repo && claude -p "…"'` from its own shell, exactly as its owner would.
  The service is not involved, does not know the host names, and cannot start or stop anything.
- What it needs is therefore not a tool but **the same knowledge a person has**, which is why there is
  a `MACHINES.md` in its workspace: prose about somebody's infrastructure, appended to the system
  prompt on every message (`agent/appendix.py`). A capability wired as code would have to guess at
  host names; a paragraph the operator writes does not.
- Its keys live in `$HOME/.ssh` inside the service's writable tree — `HOME=/srv/csd/data/agent-home`
  (§21) — so nothing about the unit's sandbox had to be widened for `ssh` to work.
- The workspace is `/srv/code`, added to the unit's `ReadWritePaths`. That is the line: an agent that
  cannot write code is not the agent this page is for, and the boundary is a path someone chose
  rather than wherever the sandbox happened to allow.

### Found while verifying it: Pi writes more than one session file per conversation

The end-to-end check on the deployed box showed the agent answering and the page not showing it. Two
files in `pi-sessions/` carried the same session id: Pi starts a **new file** whenever it continues a
session it does not currently have open — every service restart, every process it loses. The reader
returned the first glob match, so after any restart the page showed the file from *before* it and
never the one being written now; a prompt and its answer were on disk and invisible.

The fix is `NativeBackend._session_files_of`: every file whose header carries the conversation's id,
ordered by filename, which Pi begins with the ISO timestamp it created it at. It is pinned by
`test_a_conversation_spread_over_two_files_reads_as_one`, and it is the kind of bug this file keeps
recording in the same shape — an assumption that a file, an id and a process are one-to-one, when only
one of the three is ours.


## 24. A registered host is a machine the agent may be allowed to drive

Until now, "the personal agent can drive a coding agent elsewhere" meant an operator hand-placing a
private key into `$HOME/.ssh` and writing the host's address into `MACHINES.md` (§23). That works,
and it is invisible: the dashboard already draws the machine, already shows what is running on it,
and still cannot say whether anything about it is set up. The registry knew a box existed; nobody
knew whether you could reach it.

So the registry gained an **access** dimension, and this section records the three decisions that
shape it.

### Declaring is not authorizing, and the two are separate tables

A connector now declares, on every heartbeat, that this machine may be driven and where it believes
it can be reached (`wants_access`, `ssh_host`, `ssh_user`, `ssh_port`). That declaration rides in on
the **shared ingest token** — the credential every host holds — so on its own it must never be
enough to hand out a shell. It is not: it produces a row the user sees, and nothing else.

The approval is a row in `host_access`, which only a request carrying the **acting** credential can
create (`access_header`, §22). Collapsing the two would mean any host that can report work could
also authorize a shell on any other host, which is precisely the privilege escalation the ingest
token exists to be unable to perform.

Three consequences fall out of the split, and each is pinned by a test:

- **`requested` is a state.** "Declared and not authorized" is a different fact from "never asked",
  and only one of them is a request to the user.
- **An approval is for one address.** `seen_*` records what the host declared *at approval time*;
  if the host later declares something else, the access state becomes `address_changed` and the old
  command is void until a person looks again. The comparison is against the declaration, not against
  the approved address, so a user who deliberately overrode the address when approving does not get
  flagged as a host that moved.
- **A declaration is unverified.** Nothing here can tell whether a declared address is reachable
  from the dashboard — a machine behind NAT can dial out and still be undialable. The file and the
  tool both say the address is the one the user approved and that reachability was never checked,
  which is the honest version of a claim this service cannot make.

### The key is generated here, and only the host installs it

**Why not a password.** The obvious alternative — the user types the machine's ssh account and
password — puts a reusable secret in a request body and then in a command line. `sshpass -p …`
is visible in the process list, and a helper that feeds a password to `ssh` has to hold it to do
so. Meanwhile the connector on that machine already runs as the very user the key must belong to,
so it can do the one thing ssh requires: append a line to `~/.ssh/authorized_keys`. That is the
whole mechanism. The user still decides *which account and which address*; they just never hand
over a password.

The lifecycle, therefore:

1. The user approves a host in Settings → Host access. The service runs `ssh-keygen`, writes the
   private half under the agent's `$HOME/.ssh` at 0600, and records the public half and the path.
2. The connector asks the dashboard what it should do (on the same heartbeat as reporting itself),
   appends the public key with the comment `csd-agent:<label>`, and reports that it did.
3. Only then is the state `ready` and only then does the agent get the `ssh` line.

**What is deliberately not shared.** The private key never appears in the database, in a log, in
a response, or in the generated machines file — the file names its *path*. The connector's two
routes cannot even ask for it: `HostAccessFetchOut` has no such field, so a field that is never
transmitted cannot be leaked by a logging change later. And the connector only ever touches a line
carrying its own comment: the operator's own keys are in that file too, and a connector that
rewrote it would be one that could lock its user out.

Revocation deletes the private key immediately and marks the row `disabled`; the host removes the
`authorized_keys` line on its next heartbeat. Until it does, the key is dead here — which is the
right order, because the half we control is the half that must stop working first.

### The agent sees a file it can read, and a tool it can call

Two surfaces, answering different questions:

- **`MACHINES.registered.md`** in the agent's workspace: written by the service whenever the
  registry or an approval changes, listing each host, its access state, and — when it is `ready` —
  the exact `ssh` line. The per-turn appendix carries **one paragraph indexing it**, not its
  contents: the appendix is paid for on every message and the registry grows with the fleet, so
  the list lives one `read` away. That paragraph also says what to do with an unauthorized host:
  ask the user. It is not a puzzle for the agent to solve by guessing a credential.
- **The `csd_registry` tool**, registered by a second Pi extension (`pi/registry.ts`): hosts,
  access states, and *what is running right now*. It answers by running `app/agent/hosts_view.py`
  under this service's own interpreter with the database opened read-only — deliberately not an
  HTTP call, because a route would need a credential in the agent's environment and the only
  credential this service has is the one that drives the whole page. A read-only file handle needs
  no token, cannot write the record, and cannot reach past this machine.

The index is in the appendix rather than written into the workspace's `AGENTS.md` because that file
belongs to the user and the project. The appendix is the same channel every other standing
instruction already uses (§18, §23), and it is re-read per prompt, so a newly authorized host lands
on the next message with no restart.

### What this does not change

**The service still executes nothing and still opens no outbound connection.** It generates a key,
stores it, and tells the agent where it is; the `ssh` is run by the agent, from its own shell, the
way its owner would. The rule in §1 and §13 is intact — what changed is that the service now holds
a credential on the *user's* behalf instead of requiring one to be placed by hand, and that a
connector can ask for a decision instead of that decision being invisible.

**Two limits worth stating rather than discovering:**

- The ingest token is still shared, so a host can claim another `(label, agent_type)` and read that
  host's *public* key and access state. It cannot obtain a private key — none is ever sent — and it
  cannot approve anything. Should this ever leave single-operator use, per-host tokens are the next
  hardening step, and this paragraph is the reminder that they are not here yet.
- `ssh_user` is the account the connector runs as, because that is the account it can install a key
  into. Approving a *different* account is possible in the API and is a manual setup on the host;
  the UI says so rather than pretending the connector did it.


## 25. The agent connects itself

The agent page has always been a channel *onto* an agent: this service is the client, it dials
the agent's gateway, and §17 settled what that means for the conversation. What it left open is
how the two find each other — until now, by an operator setting `CSD_AGENT_*` environment
variables and restarting the service. That is fine for the person who deployed it and useless
for the person who just wants their agent on this screen.

So the direction is now stated once and built on: **agenest is always the side that dials, so an
agent "connecting itself" means the agent opened a door on its own machine and then told us where
it is.** Both halves belong to the agent; what belongs to us is a document that says how, a route
that records the answer, and a switch that puts the page on it. `/api/agent/onboard` is the
document, `/api/agent/backends` is the recording, `/api/agent/backend` — older than all of this —
is the switch.

Four decisions, and the first one is the load-bearing one.

### Loopback only, and that is the whole credential story

A registration that names any host other than this one is refused. Not "warned about" — refused,
by the same route that stores it (`agent/endpoints.py`).

The reason is not caution, it is the shape of the product. A personal agent is a **local
component of this control plane** — not a remote service, which is the opening rule in CLAUDE.md
— so a remote address is either a mistake or an attempt to point the agent page at somebody
else's server. That second case is worth taking seriously: the page is where the user types what
they want done, and a page pointed elsewhere hands all of it to whoever owns that address, plus
their replies wearing the agent's name. Refusing every non-loopback URL costs a legitimate
deployment nothing, and it removes that entire class of problem *by construction* rather than by
authentication. Which matters, because the alternative was to mint a second credential whose
only job would be to authorize an address that should never have been accepted.

What it does **not** protect against, said plainly: any process on this machine can register a
door and switch the page to it. That is the same trust boundary as everything else here — the
database, the access token, the keys host access writes into `$HOME/.ssh` (§24) — and inventing a
boundary inside one machine, for one user, would be ceremony.

### The document is generated from the backends

`/api/agent/onboard` returns Markdown; `/api/agent/onboard.json` returns the same content as
data. Both are assembled from declarations the backend modules make (`BACKEND_ONBOARD`), which is
the same rule `BACKEND_BLURB` already follows: whoever wrote the protocol translation is who
knows how that protocol gets switched on. A hand-written document would be a second answer to
"how does nanobot expose itself", and it would be wrong the first time either side changed.

**It is written for a model, not a person.** One paragraph of orientation, then the literal
commands with placeholders it can resolve itself (`$CONFIG` and `$NANOBOT_PYTHON` are its own
service unit's `ExecStart` arguments — not this service's interpreter, which has never heard of
`nanobot`), then the calls, then a table of every failure it can hit and what each one means. The
test for a document like this is whether an agent can act on it without asking a follow-up
question.

### The door it recommends for nanobot is the one that changes nothing

> **Superseded on 2026-10-20.** The premise below — that nanobot's websocket door needs a config
> edit and a gateway restart — is false for nanobot 0.3.5: the gateway already mints socket
> tokens through `/webui/bootstrap` on the channel's own port. The websocket door is the offered
> one again and the text-only API door below is gone; the end of §25 records the discovery, the
> decision and what it costs.

nanobot's websocket channel is the richer surface — tool rows, reasoning, a stop command — and it
is **closed** on a stock install: `websocket_requires_token` defaults to true, the default token
is empty, and no issue path is configured, so the handshake is answered `401`
(`nanobot/webui/gateway_endpoint.py`). Opening it means editing nanobot's config and restarting
its gateway, which interrupts every channel the user's agent serves — Telegram, QQ, WhatsApp,
whatever they actually talk to it through.

`nanobot serve` is the other door: a command nanobot already ships, on loopback, needing no
configuration change, starting none of the chat channels, and so safe for an agent to open by
itself *while it is talking to its user*. That is why it is the recommended one, and the price is
stated in the document rather than discovered later: **this surface is text only.** No tool
calls, no reasoning, no file-edit rows — OpenAI-compatible Chat Completions and nothing more. A
backend that cannot show those rows and says so is worth more than one that quietly renders a
plain conversation while the user assumes something broke (§2's rule, at the scale of a header).

The richer door is a later, deliberate choice: register the channel with a token in nanobot's
config and switch to the `nanobot` backend instead. It is not offered by the onboarding document,
because that document lists what an agent can do *now*, and starting a door that needs a config
edit and a gateway restart is not a step to hand a model mid-conversation.

### A shut door is recorded, not rejected

Registration stores the row first and probes second, and the probe's verdict comes back in
`last_error` on a `201`. Two deliberate consequences:

- **The failure survives.** "Your agent said it lives at X and did not answer" is the sentence
  that explains the situation, and it is a row the settings panel can render, a stored error that
  outlives a service restart, and — importantly — *visible as a choice*. The backend is `ready`
  (something registered it) and not `connected` (nothing answers), which are the two facts the
  panel already knew how to show.
- **The registration did succeed.** What failed was the other machine, and answering `4xx` would
  conflate the two: the agent would conclude its own request was malformed and retry it forever.

Switching is a separate call, made by the user after the probe came back clean — one route, one
job. And removing a registration is refused while that backend is active, so the page can never
be left pointed at an address that just stopped existing.

### Introducing yourself needs no credential; switching does

The first version of this put the whole surface behind `access_header` and had the onboarding
document ask the agent for the user's access token. That made self-onboarding a conversation
whose middle step was *a model asking a human to paste a secret* — the exact habit the document
should not teach, and a step that made the feature more expensive than configuring it by hand.

Three routes are now open: `GET /api/agent/onboard`, `/onboard.json`, and `POST
/api/agent/backends`. They are the two halves of an introduction, and neither can change
anything the page does — reading how to connect, and declaring where a door is. The address must
still be loopback (the section above), so this does not open "point the page at somebody else's
server"; it accepts "any process on this machine may say where its door is", which was already
true of any local process holding the token.

What stays gated is the call that actually decides: `POST /api/agent/backend` puts the page on
one backend, and every user message after that goes there. The agent registers and reports the
probe's verdict; the user clicks Settings → Agent. The page credential now guards the
transitions rather than the introductions, and a funneled deployment still cannot be repointed
by a stranger.

**What the open route costs, and the rule that closes it.** The active backend reads its address
per request, so an open registration would otherwise let any local process re-declare the active
kind's URL as its own listener and silently receive the user's next message — no credential read,
nothing changing on screen. So a declaration may not *move* a door the page is standing on: naming
a different address for the active kind is `409`, the same shape as deleting it, and for the same
reason. Re-declaring the same address stays allowed, because that is an agent restarting on its
port rather than a move. The residue that remains is small and worth stating: anyone who can reach
this port may add a door to the list, and may probe a loopback address with a `GET /health`. They
cannot point the page anywhere (loopback only) and cannot put it anywhere (the switch is gated).
**On the reference deployment the service is published through Tailscale Funnel, so "anyone who
can reach this port" includes the public.** That was not true of any `/api/agent/*` route before
this change, and it is the price of the agent walking in without a credential. The exposure is a
write to `agent_endpoints` and a local `GET /health` whose verdict is visible in `last_error` —
row spam and a shallow port scan, not a way to reach the user's words. If that trade stops being
acceptable, the fix is not to gate the document again but to refuse an open registration on a
*proxied* request (`X-Forwarded-For` is how the Funnel announces itself), which costs the agent
nothing because it runs on this machine.

### What this changes for the backends

> **Removed on 2026-10-20** — the kind described here no longer exists; see the end of §25.

One new kind, `nanobot_api`, next to the websocket one rather than replacing it: the same agent
over a different wire, and the module says so in its own docstring. It keeps its **own
transcript** (`NanobotTranscript`, in its own file), because Chat Completions cannot replay and a
page refresh would otherwise show an empty conversation — the identical problem the websocket
adapter documents, in a different protocol. Two doors, two conversations with one agent; not one
transcript shared by two clients.

`httpx` moved from the dev extras into `dependencies` along the way: `hermes.py` had been
importing it at runtime from an optional dependency, which works on every machine where somebody
happened to install the extras and crashes on the one where nobody did.

### The websocket door leaves the list

> **Reversed on 2026-10-20** — see the end of this section. The retirement reason (a config edit
> and a gateway restart) turned out to describe an older gateway, and the API door that replaced
> it could not carry a checkpoint at all.

`nanobot_api` was added next to the websocket backend rather than replacing it, and for a while
both were offered. That was a mistake of presentation rather than of protocol: the settings panel
showed two rows called "nanobot", and one of them was dead on this install because the websocket
channel requires a token the default config does not have. The honest answer — "one agent over two
wires" — is a sentence a UI should not have to be explained for.

So the websocket backend declares `BACKEND_OFFERED = False`. It stays **registered**: the module
still translates the richer protocol, its transcript class is what `nanobot_api` reuses, and an
explicit `CSD_AGENT_BACKEND=nanobot` remains the operator's way in — the same escape hatch that
exists for every backend. What changes is that the page no longer offers it and `POST
/api/agent/backend` refuses it. A retired door that could still be selected would be *hidden*, not
removed, and the user would have no way to see what they had chosen.

One consequence needed handling rather than hoping: a stored `meta.agent_backend` naming a retired
door. Startup honours a persisted choice only while that choice is offered, because the one state
worse than falling back is a page silently using a backend the settings panel no longer lists. An
explicit `CSD_AGENT_BACKEND` is still honoured, since that is the operator saying so at deploy
time rather than a stale click.

### The websocket door comes back, and the API door goes (2026-10-20)

The websocket door was retired because opening it was believed to require a config edit and a
gateway restart. That premise was wrong, and the check that disproves it is one HTTP call:
nanobot's gateway already serves a route on the channel's own port, and it is the route its own
WebUI logs in through.

- `GET http://127.0.0.1:8765/webui/bootstrap` answers `{token, ws_url, expires_in}`
  (`nanobot/webui/ws_http.py`). `token` is a one-time websocket credential, and when
  `websocket_requires_token` is true it is the only thing the handshake accepts.
- With `channels.websocket.tokenIssueSecret` set, the caller presents it as a Bearer token. With
  it empty, a loopback peer presenting a loopback `Host` is served — `is_local_browser_request`
  is wider than its name.
- So any process on this machine can open this door, at any time, with no edit and no restart.
  What used to look like operator work is a `GET`.

Measured on the deployed gateway, a turn over that socket is what §23 asked for and the API door
could not deliver: `delta … stream_end{resuming:true}` then a `message{kind:"progress"}` marker
per stage note, `reasoning_delta` for thinking, `file_edit` for work, `turn_end` for the end. The
Chat Completions surface has nowhere to put any of it — its server forwards `on_stream` tokens and
throws away the segment boundaries its own loop already computes (`nanobot/api/server.py`,
`_on_stream_end`), which is why every checkpoint arrived concatenated into one message.

Two consequences, both deliberate:

- **`nanobot_api` is removed, not retired.** One agent, one row. Its module, tests, systemd unit
  and deploy script are gone; an `agent_endpoints` row naming it routes nothing, and a stored
  `meta.agent_backend` of that name falls back at startup like any unregistered name. Keeping it
  as a hidden fallback would have preserved exactly the second answer to "how does nanobot expose
  itself" that this section spent its length removing.
- **`nanobot` is offered again**, with a `BACKEND_ONBOARD` declaration so the onboarding document
  can point an agent at a door it already has open. A registration carries the address
  (`http://127.0.0.1:8765`) and, when the gateway has one, its `tokenIssueSecret`; this service
  mints a fresh socket token per connection and **ignores the `api_token` in the bootstrap
  response** — it needs one socket, not the gateway's control surface.

What it costs is stated rather than discovered: switching to this agent starts a fresh transcript
on this page (the channel cannot replay, and the adapter's own table is empty for that
conversation), and the API door's conversation and this one are different sessions on the agent's
side. The agent's memory is unaffected. The address is read on every connect, so a registration
that arrives after the service started is used without a restart — the same rule onboarding needs.

Two adapter mistakes are pinned by tests because they are what "it worked once" would hide: a
`stream_end` with `resuming: true` is a checkpoint and *not* the end of the turn, and the progress
`message` frame behind a streamed segment is a textless marker, not a second copy of the text.

## 26. The local agent is configurable from the page

`native`'s provider, model, key and tool allowlist were environment variables read once at
startup. Changing a model therefore meant editing `csd.env` on the box and restarting the service
— the one trip this product exists to remove. The gear on the agenest row opens a page that owns
those values.

**Where the values live.** One JSON row in `meta` (`native_config`), beside `agent_backend`,
which is the field this table has held since switching was runtime. The stored value wins; the
environment is the fallback, and clearing a field restores it — so an operator who configured the
service in a unit file is not silently overruled by an empty form, and "reset to environment" is
a real button rather than a blank screen.

Two rejected options, both plausible enough to be worth naming:

- **Writing back to `.env` / `/etc/csd/csd.env`.** The installer treats that file as the
  operator's (§21) and never overwrites it; a second writer would make `.env.example` a lie the
  first time the two disagreed, and every deploy step would have to reason about which one won.
- **A new settings table.** The configuration is read and written whole, and a partial write
  that landed between `provider` and `model` would leave the agent with half a setup. One row,
  one transaction.

**The key.** Stored in that row. Never returned: the routes report `api_key_set` and the *names*
of provider variables already present in the environment (`api_key_env`), the same shape
`agent_endpoints` uses as `has_token` (§25). It reaches Pi in the child's environment, not on its
command line — argv is readable by every user on the box, a process's environment is not. This
does not widen the trust boundary: whoever can read the SQLite file can already read `csd.env`.

**A custom endpoint, without editing another product's config.** A base URL and a context window
have no command-line spelling in Pi, and its only environment override is Azure's. Pi's own
`~/.pi/agent/models.json` would work and was rejected: that is the user's configuration for their
own `pi`, and a service that edits it changes a program it is merely a client of. Instead
`agent/pi/provider.ts` is a third extension, beside the appendix and the registry tool, loaded
with `-e` **only when the stored configuration needs it**, and driven by a generated JSON file
that names the key's *variable* rather than its value.

Verified against `pi 0.84.1` before writing any of it: an extension-declared provider resolves
for `--provider`/`--model` at startup (the registration is applied while services are built,
before the model scope is resolved); a provider given only `baseUrl` + `apiKey` keeps its
built-in catalogue; and a model given `contextWindow`/`maxTokens` appears in `--list-models` with
that window. The context window is also the switch that says "define this model here" — empty
means Pi's own catalogue, which knows far more about a built-in model than a form can restate.

**When a change takes effect: the next message.** The command line is built when a Pi process
starts, so `apply_config` records the values, rewrites the provider file and marks live
conversations `restart_pending`; `_ensure` replaces the process on the next prompt, under the
per-conversation lock. It deliberately stops nothing. Pi's `stop()` can wait out a 5 s grace
period, and a save is a request the page is waiting on — but the deciding reason is that
interrupting a turn to change a model would abandon the answer being written. This is §22's call
for switching backends applied one level down: the teardown is the agent's business, not the
request's.

**Dream follows the same configuration.** It is a Pi process too, and after a provider change a
Dream still using the old one fails auth in a process nobody is watching. `PiDreamRunner` reads
the same `NativeConfig`; it spawns per run, so no restart is involved.

**What stayed out, and why.**

- **The working directory**, although it is the largest permission there is: it decides what the
  agent may write *and* where `memory/` lives, so moving it is an environment decision. The page
  shows it read-only rather than pretending it is a toggle.
- **Memory settings** — §22/§23 removed that section on purpose; this page does not bring it
  back under another name.
- **`--thinking` and the progress-note toggle.** Both are one-line pass-throughs, and the only
  cost is another row on a phone. They are the obvious next two when someone asks.
- **Pi's own `settings.json`** (compaction thresholds, themes) for the same reason `models.json`
  is out: it is another program's configuration.

**One status changed meaning.** `GET /api/agent/status` now reports the *configured* provider and
model, carried on `BackendStatus` rather than read from `Settings`, because after this section
those are different values for as long as a process outlives a save. `AgentBackendOut.configurable`
is what draws the gear — the page asks the payload instead of naming a backend, and a backend
that cannot be configured refuses the routes rather than opening an empty form.

**A check is worth exactly what it proves.** `POST /api/agent/native/check` runs
`pi --offline --list-models <pattern>`: it catches a typo, an unknown provider, a model the
catalogue does not have, and it costs nothing. It says nothing about the credential — only a
request does — so the page states that in the same breath, rather than letting a green tick imply
a key works.

## 27. The page is installable, and the service worker caches the shell only

The build ships a manifest, icons and a service worker, so a phone can install the dashboard and
open it without an address bar. The worker has one rule worth stating: **`/api` is never
cached.** A cached session list would show work that has already ended or hide work that started
while the phone was in a pocket — exactly the confidently-wrong screen §2 exists to prevent.
Offline means the shell paints and the data is absent, not that the data is old.

Two strategies follow from what the assets are. **Navigations are network-first**, because a
deploy must reach an online phone immediately and the cached shell is only the no-network
fallback. **Hashed assets are cache-first**, because Vite changes the filename on every build, so
a cached asset cannot be stale. There is no precache manifest and no build plugin: the shell list
is the handful of URLs that exist in every build.

The static mount widened from `/assets` to the whole `dist/`, because the manifest, the worker
and the icons sit beside the bundle rather than inside it. API routes are registered first, so the
catch-all cannot shadow them; `StaticFiles` resolves every path inside its directory, so the
traversal surface is unchanged. `.webmanifest` is registered as `application/manifest+json`
explicitly — a browser refuses a manifest served as `application/octet-stream`.

Rejected: a WebView shell. It buys background push and store distribution, neither of which is in
scope, at the cost of a second build, signing, review, and a push path that would contradict §1
more directly than the agent's own `ssh` already does. The worker is registered only in a
production build: under the Vite dev server it would turn hot reload into stale HTML.

## 28. DeepSeek Harness reports turns, through its own log

`dsh` is a coding agent, so it follows §13's shape rather than §17's: the machine running it
originates the connection, the dashboard only reads what it says, and nothing here makes it a
personal agent. The reporter is a native Cordis plugin at `sdk/dsh-csd-plugin`, mounted by the
profile's bundle patch.

**Why not the shipped Claude Code hook bridge.** DSH ships `dsh-hooks-claude-code`, which runs a
Claude Code `settings.json` hooks block, and pointing `csd-agent` at it looks like the whole
adaptation: one config path, no new code. It supports seven events and deliberately omits
`SessionEnd` and `Notification`. A session could be opened and never closed, and a blocked turn
could not be shown at all. Closing it from process liveness would be §14's pid probe applied to a
long-lived server whose process outlives every conversation — that is not a fact about the
session. The durable log carries both facts, so the plugin reads the log rather than a lossy
bridge.

**Why a turn, and not a DSH session.** A DSH session is resumable: it stays in the store for the
life of the process and can be reopened after a restart. There is no "ended" event to report, so a
conversation-level row could only be `running` for as long as the server is up, or end when the
process did. That is §2's failure in its purest form: a status nobody reported. A **turn** opens
at `turn/start`, closes at `turn/end` with a reason DSH itself wrote (`completed`, `error`,
`aborted`, `blocked`, `max-tokens`, `interrupted`), and maps onto the dashboard's statuses branch
by branch. A reason this build has never seen closes as `unknown` — the vocabulary is
merge-extensible, and treating a new value as success would turn uncertainty into a verdict.

**The consequences of reading the log.** `session/disposed` mid-turn is reported as `interrupted`:
the session leaving the store is the fact, the same shape as §14's gone process. Subagent child
sessions are ignored because their work is the parent turn's work; a row each would make one task
look like several. `approval/asked` and `user-questions/request` become `awaiting_user` events,
not a status, for §16b's reason: the state must clear itself when the user answers. The turn
heartbeat renews liveness while a turn runs, and its key includes a time bucket, because §7's
dedupe means a constant message would apply its side effects exactly once. A plugin that mounts
mid-turn reads the live session log once and resumes the open turn, so a live patch reload does
not lose that turn's outcome; it uses the same events, not a second source of truth.

**The host is reported; the machine is not declared driveable.** The plugin posts a host row,
`(label, dsh)`, at boot and every five minutes, so a machine with no turns still answers "did it
connect?". It sends `wants_access: false`: only `csd-agent` can install an approved key, and a
declaration this connector cannot honour would be a prompt promising a shell it cannot deliver.
It sends no goodbye on shutdown — a plugin reload or a DSH stop is not an uninstall, so the honest
rendering is §14's silence.

**Installation is the same command as Claude Code's.** A DSH-specific `dsh plugin add <checkout>`
would make the second agent the one that needs a different manual, and pnpm records such an add
as a `link:` to wherever the checkout happened to be — a deployment that breaks when the
repository moves. `csd-agent install` therefore vendors the reporter under
`~/.csd/dsh-csd-plugin`, builds an npm-style tarball, and asks DSH's own plugin manager to add
that to every existing profile. The plugin manager is the only writer that keeps the profile's
dependency and `dsh.profile.bundles` list consistent, so a later `dsh plugin update` reconciles
the set instead of silently dropping the row. A host that runs DSH without Claude Code uses
`csd-agent dsh install` (same module, no hooks); a one-shot `headless` run can still be wrapped
by `run-agent.sh`, the shape Codex uses.

Rejected: an external tail of `~/.dsh/sessions/**/session.jsonl.zstd`. It would avoid touching
DSH, at the cost of coupling to an internal storage format and re-deriving a status outside the
process that owns it. The session log is an implementation artifact; the documented event bus is
the supported surface.

Also rejected: reporting DSH as a personal agent. DSH's ACP and SDK surfaces would make a backend
possible, but §17 already settled that the second tab is a channel onto the user's agent. A second
kind of conversation there is a different product, not an adaptation of this one.

## 29. A file crosses the channel as a path

The conversation can carry files in both directions, and the mechanism is one sentence: **the
agent and this service are on the same machine, so a file moves as a path rather than as bytes
inside a protocol.** None of the four backends' protocols has a media frame. All four agents
have file tools. Handing them a path is the interface they already implement, and it is the same
interface for every one of them — which is why this is not a per-backend feature.

**The user's file.** The page uploads the bytes; the service stores them under
`attachments/in/<thread>/<attachment id>-<name>` and records the row in `agent_attachments`.
The message the agent receives ends with one line per file:

```
[attachment a_1f…] screenshot.png: /srv/csd/data/attachments/in/t_…/a_1f…-screenshot.png
```

That line is doing two jobs, and both are deliberate. For the agent it is the instruction ("here
is a file, here is where it is"). For the transcript it is the identity: the marker names our
attachment id, so a later read resolves the id against the table and puts the file back on the
message it came with. Stripping it is a lookup, not an inference about which words were a
filename — and an id that does not resolve is left as text, because deleting words on the
strength of their shape is how a transcript loses a sentence a model quoted back.

**The agent's file.** The agent is told where to leave one — in its per-turn appendix for
`native`, or in a one-line note at the end of the prompt for a backend with no appendix — and
the supervisor drains that directory when the turn settles. Drain means **move**: the file leaves
the outbox for `attachments/delivered/<conversation>/`, so a second drain cannot deliver it
twice. The row written by the drain *is* the message: the agent's transcript has no entry for a
handoff that happened on the filesystem, so history merges these rows into the conversation by
time instead of decorating a neighbour. `history()` drains too, which repairs the one window a
restart can open between "the turn ended" and "the event was handled".

Rejected: **render every file the agent wrote.** The workspace is full of build output, caches
and scratch files. Picking them out by extension or modification time would be §2's rule
("reported, never inferred") applied to attachments, and it would make the transcript unusable
on a real project. Rejected: **scan the reply for Markdown links or absolute paths.** A link is
not a delivery — the agent may link to a URL or to a file it never meant to hand over — and it
would make a prose parser decide what is an attachment. Rejected: **base64 in the prompt.** It
inflates a 5 MB image into 7 MB of context, spends the model's context on bytes it will read
through a tool anyway, and no backend's protocol would carry it either.

**The upload is a raw body, not multipart.** The bytes have exactly one destination, so a parser
that buffers or spools them first is a dependency and a second copy for nothing. The filename
travels percent-encoded in `X-CSD-Filename` (headers are not UTF-8 by contract, and a Chinese
filename is the ordinary case); the type travels in `Content-Type`. The size limit is enforced
**while streaming** — `Content-Length` is a claim, the loop is the fact — and a rejected upload
deletes its partial file rather than leaving one behind.

**Downloads never carry the token in a URL.** `GET /api/agent/attachments/{id}` sits behind
`access_header`, and the page fetches it with `fetch()` and a real header before showing a blob
URL. An `<img src="…?token=…">` would write the credential that can drive an agent into browser
history, `Referer` headers and every proxy log on the path — the same reason the agent stream is
not `EventSource`. The response is `Content-Disposition: attachment` with
`X-Content-Type-Options: nosniff`, because an uploaded HTML file rendered in this origin would be
executing a file the page was merely asked to carry.

**Tapping a card is "open with another app".** The page hands the blob to `navigator.share` when
the browser supports sharing files, which on a phone is the system app chooser. Where it does not,
opening falls back to a download. The explicit download arrow exists because "open" and "keep"
are different intentions, and a single button would have to guess.

**The outbox line is stripped from the transcript.** It is addressed to the agent; the reader
does not need to see instructions about a directory. This is also why the channel note is a
single line rather than a paragraph: it is paid for in the agent's context on every prompt for
those backends, and the long form is the appendix, which only `native` has.

Not built, deliberately: thumbnails (the phone's image decoder is already the right thumbnailer),
old-attachment cleanup (the files are the transcript's record; a retention policy is the
operator's decision, not a default), resumable uploads, and sending a file to a *coding agent* on
another machine. That last one is a different path — it would go over the agent's own `ssh`, not
over this channel — and is not implied by this one.
## 30. What runs in production is a release, not a working directory

There were already two instances, and they were already separate: `dev.sh` writes
`backend/data` and listens on 8780/5173, the cloud unit writes `/srv/csd/data` and listens on
its own loopback. Different databases, different ingest tokens, different hosts. What was not
separated was the **code**: the documented deploy path made a tarball from the working tree, so
whatever happened to be half-finished at that moment became production. A dirty tree is the
normal state of a repository being developed, which made "the version I use every day" depend
on when someone last ran `tar`.

**A release is a tag and an artifact.** `release.sh cut <tag>` refuses anything but a clean
tree, runs the backend suite, builds the frontend from that same commit, writes
`releases/<tag>.tgz` from `git archive`, and only then creates the tag. `deploy` ships an
artifact that already exists; it never re-reads the working tree. `deploy.sh` records the
version at `/srv/csd/VERSION` and the shipper reads it back over ssh, so "it deployed" is a
checked fact rather than an exit code.

**Why a tag and not a branch.** A branch is a moving name; "deploy main" is the working-tree
problem with an extra step. A tag names one commit, and an artifact is what makes rollback a
re-upload rather than a rebuild from a checkout someone has to reconstruct.

**Why `git archive` rather than `tar --exclude`.** The old flow's exclude list was
load-bearing: forgetting one entry shipped `.env` or the SQLite database. `git archive` can
only carry tracked files, so secrets and local state are absent by construction rather than by
a filter someone has to maintain. The build product (`frontend/dist`) is added deliberately,
because the server has no Node.

Rejected: **a staging instance on the same host, as the default second step.** For the
dashboard half it would work; for the agent half it would not be the same agent — a second
`native` process has its own workspace and memory, and a second client on the same nanobot is
a different conversation. A staging box that cannot exercise the thing being changed is a
ritual. Fast rollback is the answer until a change is genuinely deployment-specific (systemd,
Tailscale, Pi on Linux); then a staging unit is worth adding on its own merits.

The **data is never part of a release**. `/srv/csd/data` survives an install, and migrations
are additive with no down-migration, so old code tolerates a newer schema — which is what makes
code rollback safe to offer as one command.

## 31. Pi reports runs, and the personal agent it also powers stays silent

Standalone `pi` is a coding agent and follows §13's shape: the machine running it originates the
connection, the dashboard only reads, and nothing here makes it a personal agent. The reporter is
a single Pi extension at `sdk/pi-csd-extension/csd-reporter.js`, copied into the agent's global
extensions directory by `csd-agent install`.

**Why a run, and not a turn.** Pi's `turn_end` fires once per LLM response plus its tool calls,
so a prompt that calls tools ends several turns and a retry ends one more. A row per turn would
make one task look like several, and a row per Pi *session* could never close at all — the same
problem §28 has with a DSH conversation. A **run** is what the user asked for: it opens on
`before_agent_start` (once per prompt, carrying the prompt text) and closes on `agent_settled`
(Pi has no retry, compaction retry or queued follow-up left). `agent_start` is not the opener: it
fires again for retries and compaction inside one prompt. A `before_agent_start` that arrives
while a run is open — a queued follow-up — belongs to that run, so it is ignored rather than
opening a second row. Turns are `progress` events (`turn N complete`).

**The verdict is `AssistantMessage.stopReason`.** `stop`, `toolUse` and `deferred` are
`completed`; `length` is `completed` with a summary that names the output ceiling; `error` is
`failed` with Pi's own `errorMessage`; `aborted` is `interrupted`. `pending`, a missing reason, or
a value a future Pi adds is `unknown` — §2's rule applied to a second agent's vocabulary, because
treating an unrecognized reason as success turns uncertainty into a verdict. Both `turn_end` and
`message_end` record the reason; whichever arrives last wins, which on a retried run is the final
answer. `session_shutdown` closes an open run as `interrupted` and is bounded to two seconds:
Pi awaits that hook, and a dead dashboard must not hold the agent's exit.

**The dashboard's own personal agent is the same binary, and must not report — but only it.**
The Agent tab's `native` backend spawns `pi` for every conversation, and those runs are the
channel, not coding sessions; reporting them would show the dashboard talking to itself. The
backend marks the child with `CSD_AGENT=1` and its own pid in `CSD_AGENT_PARENT`; the extension
is inert only when the marker is present **and** `process.ppid` matches. The parent half matters:
a `pi` run the personal agent spawns is a coding agent, inherits the environment, and has a
different parent — so it reports. Silencing it would make the channel unable to drive the agent
it was asked to drive. A bare `CSD_AGENT=1` (an older backend with no parent marker) stays inert,
which is the compatibility rule. `CSD_PI_REPORTER=0` is the operator's equivalent opt-out.

**One copied file, in the global directory.** Pi discovers `*.js` under the agent's `extensions/`
at startup and has no plugin manager, so the install is a copy and uninstall is a delete —
allowed only for a file carrying the reporter's marker, because the directory belongs to the
user. The global directory (`~/.pi/agent/extensions`) is the only target: `.pi/extensions` is
project-local and loads only after Pi trusts the project, so a reporter there would go silent the
first time it ran somewhere new. A `link:` to a checkout, which is what a development install
looks like, breaks the day the checkout moves — the same reason §28 insists on a tarball.

**The host row and its silence.** The extension posts `(label, pi)` at session start and every
five minutes, so a machine that has ever run Pi answers "did it connect?" without a turn, with
`wants_access: false` for §24's reason: this connector cannot install an approved key, so
declaring itself driveable would promise a shell it cannot deliver. No goodbye is sent on
shutdown — a Pi exit or a `/new` is not an uninstall, and silence is §14's honest rendering.
`csd-agent uninstall` says goodbye explicitly, before its units stop.

**Diagnostics go to stderr.** In `pi --print` the answer is the stdout stream. A reporter that
logged successes to stdout would inject its own lines into whatever is consuming the agent's
output — a wrapper piping `pi -p` into a file would find them in the answer. This is the one place
the Pi reporter differs from the DSH plugin, which logs to a server console.

Rejected: **wrapping `pi` with `run-agent.sh` as the supported path.** It works, but it captures
nothing about a run the user starts by hand, and Pi has a documented extension surface — the same
argument §28 makes against a hook bridge.

Rejected: **reporting `awaiting_user`.** Pi has no standard approval or permission event. A tool
name is not a request for a human, and inferring one would be the one thing this dashboard
refuses to do.

## 32. The connector meets the platform it is on

`csd-agent` was written for a Linux agent host: hooks in `~/.claude/settings.json`, an
`ssh -L` tunnel under systemd (`Restart=always`), and a five-minute heartbeat under a systemd
timer. A Windows host has none of those, and the first version still wrote the units and
printed `WARNING: no systemctl; units written but not started` — a connector that installed
cleanly and reported nothing.

**A hook needs no daemon.** The integration is not the hook: the hook is a short-lived process
that calls `cpctl`, and it works exactly as well on Windows once the command is runnable. What
must survive a reboot is the transport and the probe, and those are two platform questions:

- **Transport.** The supported Windows shape is a tunnel keeper in the user's Startup folder
  (`pythonw` around a loop of `ssh -N`), because it has no 72-hour execution limit and needs
  no admin. `--url` is the alternative for a dashboard that is already published and removes
  the tunnel entirely. It is not the default because a public HTTPS handshake can exceed the
  hook wrapper's 2-second budget, and a hook that blocks every prompt is worse than one that
  reports nothing.
- **Probe.** Task Scheduler, every five minutes, running a `pythonw` launcher that resolves
  the vendored package from its own path. A task created for the current user needs no admin.

**A windowless parent does not make its children windowless — the flag has to be passed.**
`pythonw` suppresses its own console and nothing else: a console program it starts
(`python.exe -m cpctl`, `tailscale ip`) is given a brand-new console window. So the probe was
correct and invisible in every log while a black box flashed on screen every five minutes for
the length of one report. Every spawn on that path carries `creationflags=CREATE_NO_WINDOW` on
Windows (`runtime.NO_WINDOW`, spread into the call); under a hook the parent already owns a
console, so the flag only ever removes an artefact. A new subprocess added to the heartbeat
needs the same, or the flashing comes back.

**Two Windows hazards are guarded, not documented away.** `os.kill(pid, 0)` is a probe on
POSIX but a `TerminateProcess` call on Windows; a liveness check written with it would kill
the session it asked about, so `tracked.process_alive` uses `OpenProcess` +
`GetExitCodeProcess` there. And a hook's parent chain can break when a launcher exits — Windows
does not reparent — so pid discovery falls back to the only `claude.exe` on the machine and
returns "unknown" when there is more than one. The heartbeat already treats unknown as "cannot
judge", which is the honest answer.

**A credential belongs to one dashboard.** The installer used to prefer whatever token was
already in `~/.csd/env`, so switching from a local install to a cloud one kept the local dev
token and every report answered 401. An explicit `--cloud` now makes that host the authority,
and an existing token is reused only when its base URL names the same dashboard.

Rejected: **a Windows service.** Admin rights, an installer and a service manager for a
process whose whole job is to keep one `ssh` alive — the Startup folder does that with a file
the user can see and delete. Rejected: **requiring WSL.** The coding agent runs on Windows;
making its host connector depend on a second Linux is a worse answer than teaching the
connector two platform primitives.

## 33. A message can be queued, and the dot means unread

Two things were wrong with the agent page once a run took more than a few seconds.

**The tab's dot meant "a run is in flight".** That is on for the entire turn, whether the agent
is thinking, running a tool, or writing an answer, so it tells the reader nothing they cannot
see by looking at the page — and it stays lit after the answer lands, until you go and look.
A dot is a *message* indicator everywhere else the user has ever seen one, so this one now
means exactly that: an assistant message arrived while the dashboard was on screen. Looking at
the agent page clears it. A tool row does not set it (that is machinery, not prose); a
checkpoint does (that is the agent addressing the reader, §23). It is reported up from
`AgentPage` because the page stays mounted while hidden — the stream keeps arriving, which is
the only reason the tab can know at all.

**Send became Stop while the agent worked**, so there was no way to say the next thing without
cancelling the answer being written. The composer now keeps both: send stays send, and a stop
button sits beside it. Typing into a busy agent queues the message.

The queue is **the page's**, not the service's, and that is deliberate:

- It is one conversation per agent and one reader in practice; a server-side queue would need a
  new wire field for "not sent yet", a second identity for a message the agent has no record
  of, and a reconciliation rule for when its real id appears. The page already knows which of
  its own messages have been posted.
- The message is **not posted** until the turn settles. Posting it early looks tempting — the
  native backend already has Pi's `follow_up` — but `follow_up` does not echo until Pi begins
  the next turn, so `send` would either block the request for the length of the current answer
  or return no id, and a no-id message is one the page cannot tell apart from history's copy
  later. Queueing here keeps every sent message carrying the agent's own id, which is the rule
  the whole transcript rests on.
- One message per turn, not the whole queue at once: two replies written concurrently would
  interleave. The release condition is the same status frame that ends the turn, so **Stop
  releases the queue** — that is the interruption behaviour, and the reason Stop does not clear
  it.

The queue is keyed by conversation and persisted to `localStorage` (`csd.agent-queue`), so a
reload does not silently drop what someone typed, and switching agents shows each agent's own
queue. It is released only after the conversation's streaming state has been read from the
server (`ready`), because a reload during a run would otherwise post into a live turn — the
exact behaviour the queue exists to avoid. Attachments are uploaded when the message is
queued, not when it is sent: a send the user has committed to must not lose its files to a page
close, and an attachment id is the only part of a queued message that survives a reload. Removing
a queued message deletes those uploads, since unlike a transport failure nothing will ever name
them.

Rejected: **disabling the composer while busy.** It is the simplest implementation and the one
that reads as a broken app — the user's next sentence is exactly what they are composing while
watching the answer arrive.

## 34. A message appears when Send is pressed, and a switch says it is loading

Three things on the agent page were technically correct and felt broken, because each one left
the screen asserting something that was not true yet.

**A sent message existed only after the round trip.** The POST is one request and every file is
another, so pressing Send cleared the composer and changed nothing else — the exact feedback
shape of a button that did not work, and the most likely answer to it is pressing Send again.
`AgentPage` now draws the message in the transcript immediately (`Outgoing`), removes it when
the agent's own row arrives, and does not care which response got there first: the stream echo
of a message routinely beats the POST that sent it, so both paths settle the same placeholder.

- It is a **separate list, not a fake id in `messages`**. That list is the agent's record and is
  deduped on the agent's id; a placeholder in it could never match its own echo and would
  survive the re-fetch that replaces everything else.
- The bubble is the user's, drawn quieter until acknowledged, because the request is genuinely
  on the wire. This is not §2's inference rule being bent: *the page* knows it sent something,
  which is a fact about the page rather than a claim about the agent.
- A send that fails **stays where it was typed** and says "Not sent", with the reason in the
  notice. Removing it would read as delivered-then-vanished, and re-adding the text to the
  composer silently is the same loss with less visible machinery. The files fall back to their
  names, because the uploads behind them are deleted on a server-side error.
- A **queued** message is still not a bubble (§33). Nothing has been posted, so drawing it would
  put words in the agent's mouth; the queue strip is where it belongs.

Rejected: **a server-side "pending" flag and a real id before the POST.** It needs a second
message identity, a reconciliation rule for when the agent names it, and a wire field that says
"not sent yet" — for a state the page already knows and nobody else ever needs.

**The three dots were suppressed by a row nobody could see.** The typing bubble already existed,
but it stood aside whenever a tool was running — including with the process view off, where the
tool row is not drawn at all. For the length of every command the transcript showed nothing at
all. The rule is now the one the header always claimed: the dots step aside for a tool **only
when that tool is being shown**. The header follows the same line: with the process view off it
says "typing…" rather than naming a command the reader chose not to see.

**A switch drew the empty state for a conversation that was still arriving.** Switching agents
now clears the transcript at once — leaving the old agent's messages under the new agent's name
for a round trip is how "my history did not sync" starts — and draws a **spinner** in its place
(`loadingThread`), not the empty state. "This agent has never said anything" is a claim, and the
one moment it is certain to be wrong is the second after the user asked to see that agent. Boot
uses the same flag, so a reload no longer flashes "agenest has never said anything" before the
first fetch answers. A switch whose answer is lost puts the previous conversation back rather
than leaving a pane that reads as an empty agent, and the composer refuses to send while a
switch is in flight: `threadId` still names the agent being left.

## 35. One connection per thread, and a status is only written when it changes

On 2026-10-21 a session on the school host was reported **completed** and the dashboard showed
it `running` for the next 33 hours — with a `status_changed → completed` event on its own
timeline. Closing it two days later wrote `interrupted`, because the row was still `running`
and §3's guard had nothing to refuse.

The evidence is two lines in the same second. Five `SessionEnd` hooks fire when one VS Code
window closes, so the host sent five PATCHes at once. The service answered one of them `500`
with `sqlite3.InterfaceError: bad parameter or other API misuse`, and answered the lost one
`200` — while its `UPDATE` was rolled back underneath it and its event `INSERT` committed. The
connector then dropped its tracked entry on the strength of that 200, so the five-minute
heartbeat, the only thing left that could close the row, no longer knew it existed.

**The cause is the connection, not the rule.** FastAPI runs sync endpoints in a threadpool, and
`Database` held exactly one `sqlite3.connect(..., check_same_thread=False)` for the whole
process. `write()` committed or rolled back whatever transaction was open on it — which, with
two requests in flight, is the *other* request's. The module docstring had claimed "each
request gets its own connection" for as long as the code had not done that; a claim in a
comment is not isolation.

What changed, and why each part is needed:

- **A connection per thread** (`threading.local`, created on first use). Two threads then
  cannot share a transaction at all.
- **`write()` issues `BEGIN IMMEDIATE` before it yields.** Taking the write lock *before* the
  first read is what makes a read-then-write decision atomic; a deferred transaction that
  reads first and writes second can be beaten to the lock by the writer it was reasoning
  about. `isolation_level=None` moves transaction control out of sqlite3's implicit handling
  and into that one place.
- **The session is re-read inside the transaction** (`patch_session`, `ingest_event`). The
  status a decision depends on is the thing a concurrent request may be changing; deciding it
  is "still running" from a snapshot taken before the lock is the lost update.
- **An event writes `status` only when it moves the session to a terminal state.** Writing the
  value back "unchanged" looks like a no-op and is exactly the line that reopened a completed
  session. Nothing is lost by omitting it: an event that does not end a session has no opinion
  about how it ended.
- **The connector keeps its tracked entry when `cpctl` fails** (`SessionEnd`, and the
  stale-session close on `SessionStart`). The heartbeat already reports "process gone"; the
  entry is what lets it. Dropping it on the failure path leaves a row nobody on the host can
  close.

**The 200 is what made this expensive.** `cpctl` retries a failed report, so the `500` cost
nothing. A success is not retried — and a success that committed half a change is worse than a
failure, because it tells the reporter to forget the session. That is the invariant the tests
in `test_concurrency.py` pin: writers are serialised, and a completion racing an event stream
stays completed.

Rejected: **one global lock around every query.** It also serialises reads, so an SSE client
mid-replay would block ingest, and on this workload ingest is the thing that must never wait.
Rejected: **`check_same_thread=True`.** It would only turn the bug into an exception, and the
threadpool is many threads by design. Rejected: **retrying on the client.** The poisoned answer
was `200`.

The same audit found the connector side of the same shape: `on_session_end` dropped
`tracked.json`'s entry without looking at the result, and `tracked._save` used one fixed
`.tmp` name, so five hooks in one second raced `os.replace` (observed: `FileNotFoundError`,
and on Windows `PermissionError`). Entries stay tracked until a report succeeds, and the
temporary file is unique per process and thread with a short retry on replace.


## 36. A reply boundary is an event, so `idle` is a display state

An interactive editor keeps one Claude Code process alive for days: the VS Code extension
starts it once and reuses it for every prompt, so `SessionEnd` fires when you close the
window, not when the agent finishes your task. The result was a dashboard that said `running`
for hours after the work had stopped — the one question this page exists to answer, answered
wrongly.

The fact was already being reported. Claude Code fires `Stop` when a reply ends. §15 explains
why that must not *close* the session; it does not explain why it should be thrown away. It now
travels as a `turn_completed` **event**, and the page reads it the same way it reads
`awaiting_user` (§16b): the newest display event decides a display state called **idle**.
Self-clearing for the same reason — the next prompt reports a `progress` event that supersedes
it, so there is no flag to reset and no expiry to tune.

- **The status is untouched.** The session really is still open, so `running` remains the
  truthful status. `idle` is what the newest event says about *now*, and it is never written
  into the status column: closing the window still produces `completed` from `SessionEnd`, and
  the heartbeat still closes a killed process as `interrupted`.
- **A prompt is progress.** With `PostToolUse` deliberately unwired (the README's privacy
  argument), a long session's timeline otherwise held a single line. `UserPromptSubmit` now
  reports `Turn N: <first line of the prompt>` — the only honest "working on X" the connector
  has between the two ends of a turn. The turn number is in the message because `cpctl` derives
  its idempotency key from the message: two turns with the same prompt text ("continue") would
  otherwise collapse into one row.
- **It is reported, not inferred.** The page never decides a session is idle because nothing
  has happened recently; quiet is still `possibly_stale`. Idle comes from a `Stop` Claude Code
  fired, which is why it outranks staleness on the card, in the rail, and on the detail page.
- **The word matters.** `idle` is drawn with the neutral palette and its own label, not the
  running green: alive-but-not-working is a different claim from alive-and-working, and the
  rail's `Running` filter, its node markers, the footer and the overview's tiles all count it
  separately.

Rejected: **`Stop` → `session finish`.** §15, and it is unrecoverable — terminal states are
sticky, so the same process's next prompt could never be shown as running again. Rejected:
**inferring idle from silence.** That is a guess with a timer attached, and the guess is wrong
for every long tool call. Rejected: **a sixth `SessionStatus`.** An idle session is still
running; two answers to "is it running" would force every later reader of the status column to
choose between them — §20's argument about hosts, applied to a value.


## 37. A checkpoint is the agent's own sentence, and it is an event

A turn can run for twenty minutes. Between the prompt (`Turn N: …`) and `Reply finished` the
dashboard showed nothing, so the one question it exists to answer — *what is it doing right
now* — was unanswered for the whole time it mattered. The other obvious granularity, one row
per tool call, is worse: it buries the two rows a person reads, and `PostToolUse` carries the
file contents and command lines the README already refuses to upload.

The middle layer is the agent's own prose. Agentic turns are full of sentences like *"Now
I'll add the cursor test"* — the model writes them for itself before it acts. A **checkpoint**
is one of those sentences, reported as an event:

```
event_type: checkpoint
message:    "Now I'll add the cursor test"
payload:    { source: "prose", turn: 3, message_id: "…" }
```

- **The agent is the author; the connector is only the carrier.** A checkpoint is text the
  model wrote, never a summary the dashboard composed. `source` names where it came from
  (`prose` today; `plan` / `step` for reporters that can supply them), so the page can never
  draw a machine fact as an agent sentence (§2 applied to prose).
- **It is an event, not a status.** It moves no status, and it is not a sixth `SessionStatus`
  — the same argument as `awaiting_user` (§16b) and `idle` (§36). It is a display event, so
  the next one supersedes it and there is nothing to clear.
- **`idle` is cleared by it.** A checkpoint is evidence of work: if the newest event is a
  checkpoint, the session is not idle. `isIdle` reads the exact `turn_completed` type, so this
  needed no code — it is the same self-clearing shape §36 relies on.

### How a sentence proves it was not the answer

A message that is followed by tools was commentary. A message with no tool after it is the
answer. That distinction is the whole rule, and no model cooperation is required:

- **Claude Code**: `MessageDisplay` fires while the text is displayed, in batches.
  `csd-agent` accumulates it into `tracked.json` (`pending_text`, `pending_message_id`,
  `pending_final`) and reports **nothing** there. `PostToolBatch` — once per batch of parallel
  tool calls, before the next model call — is what proves the message was commentary, and is
  the only place a checkpoint is sent. `Stop` discards whatever is still pending, because that
  message is the reply.
- **Pi**: `turn_end` carries both `message` and `toolResults`. A non-empty `toolResults` and a
  non-empty first text block is a checkpoint; an empty one is the answer and is left to
  `agent_settled`. No state machine is needed at all.
- **DSH**: `assistant/message` carries the text blocks and the tool-call blocks in one event,
  so the rule needs no pending state either: text plus a tool call is commentary, text alone is
  the answer. `todo/write` adds the plan layer for free — a whole-list snapshot where the latest
  write wins, which is exactly the `source: "plan"` shape (§28).
- **Everything else**: a backend with no prose surface reports no checkpoints. That absence is
  honest and is rendered as such — never filled in from tool names, elapsed time, or an event
  count.

Both hooks are installed **synchronously**, and the ordering is the reason. `MessageDisplay`
must finish writing the pending text before the model can ask for a tool, or the batch that
should flush it finds nothing. `PostToolBatch` must read and clear that text before the next
`MessageDisplay` can replace it; an async flush can wake up after the next message has started
and attribute that message to the previous batch. The cost is one `cpctl` call between a tool
batch and the next model call. A dashboard that is down can therefore delay the agent by the
CLI's 3 s timeout — the same failure mode `Stop` and `UserPromptSubmit` already carry, not a
new one. A per-message file plus an async flusher is the upgrade if that cost ever matters;
it is not worth the extra identity scheme today.

### Identity is the agent's own id, not the text

The checkpoint's `event_key` is the agent's `message_id` (Pi: run + turn number), not the
message text. `cpctl` derives a key from the message when none is supplied, which would
collapse two checkpoints that happen to begin with the same words — and the same sentence can
legitimately be said twice in one session. A retried `PostToolBatch` re-sends the same id and
inserts no second row (§7).

### What is deliberately not stored

`MessageDisplay` is a *display* stream, not a record. Only the completed sentence becomes an
event; the intermediate deltas never reach SQLite. That is §4's split applied to prose, and it
is the same rule OpenHands states for `StreamingDeltaEvent`: deltas are a UX affordance, not
part of the conversation record. The source of truth for what the agent said remains the
agent's own transcript, which this service does not read.

### The capability levels, so a reporter cannot pretend

| Level | What it has | Example |
|---|---|---|
| `stream` | token/chunk stream | Claude `MessageDisplay`, Pi `message_update`, ACP `agent_message_chunk` |
| `complete` | one completed message item | DSH `assistant/message`, Pi `turn_end.message`, Codex `item.completed`, OpenHands `MessageEvent` |
| `final_only` | only the last answer | Codex hooks (`stop.last_assistant_message`), Claude `Stop` |
| `none` | no reliable prose surface | a wrapper that only has a process exit code |

A reporter at `final_only` must not emit checkpoints. The codex app-server path — where
`item/agentMessage/delta` is the stream and `MessagePhase` (`commentary` / `partial_answer` /
`final_answer`) is the agent's own classification — is the next place this contract can be
filled in without changing the event; it is deliberately not wired yet, because it changes who
owns the Codex process and who answers its approval requests.

Rejected: **plan-first.** A plan snapshot (ACP `plan`, Codex `turn/plan/updated`, Claude
`TaskCreated`/`TaskCompleted`) is richer where it exists, but it depends on the model using a
task tool; making that a requirement is exactly what this feature is trying to avoid. Rejected:
**one row per `PostToolUse`.** Volume, and the payload is the user's code. Rejected: **reading
the transcript or rollout file.** It is an internal artifact, and §28/§31 already rejected that
coupling for two other agents. Rejected: **a dashboard-side summarizer.** An LLM reading tool
calls to write progress is a second author whose mistakes cannot be audited, and §2 forbids the
first-person claim that would result.


## 38. Codex reports turns through its hooks, and never answers an approval

Codex is a coding agent and follows §13's shape: the host reports, the dashboard reads. It has
two surfaces, and they are not interchangeable.

**`app-server` is the rich one, and it is a long-lived client.** The connection is the
subscription — a client that disconnects stops receiving notifications, and completed items can
only be recovered from `thread/items/list`. It carries `item/agentMessage` (with a `phase` of
`commentary` / `partial_answer` / `final_answer`), `turn/plan/updated`, and the control calls
(`turn/start`, `turn/steer`, `turn/interrupt`). It also sends approval *requests* down the same
connection. That is why it is a different product shape, not a better reporter: the client is a
participant, and if it is the only client, an unanswered approval hangs the turn.

**Hooks are the short-lived surface this package installs.** Codex loads
`$CODEX_HOME/hooks.json`; the payloads are Claude Code-compatible (JSON on stdin,
`hook_event_name` in PascalCase), and the command is a normal short-lived process. What they
carry:

| Event | What it gives the dashboard |
|---|---|
| `SessionStart` / `SessionEnd` | session lifecycle (open; close as completed) |
| `UserPromptSubmit` | the prompt → `Turn N: <prompt>` |
| `Stop` | `turn_completed`; `last_assistant_message` is the answer |
| `PermissionRequest` | `awaiting_user` — **observed, never decided** |
| `Interrupt` | the same turn boundary as `Stop`, keyed together |
| `PreToolUse` / `PostToolUse` | tool name, `tool_input`, `tool_response` — machinery, not prose |
| `PreModelRequest` / `PostModelRequest` | a model call happened; status and usage, **no text** |
| `Subagent*` / `PreCompact` / `PostCompact` | structure, not progress |

There is no Codex equivalent of `MessageDisplay` or `PostToolBatch`. The only supported
assistant prose is `stop.last_assistant_message`, which is the answer; reporting it as a
checkpoint would show every reply as progress. So this reporter is **turn-level on purpose**
and emits no checkpoints — the same rule §37 applies to a `final_only` capability. A reporter
that invented them from tool names would be §2's inference wearing a progress bar.

**An approval is broadcast; the first answer wins.** `PermissionRequest` may return allow or
deny, and the outcome is a real control surface. This reporter returns **nothing**, so Codex's
own flow (the TUI, or whatever reviewer the turn configured) decides. A deny here would race
the human: the request goes to every connected client and the first response resolves it. The
same rule applies to an app-server observer. Only an **owned/headless** turn may decline
automatically, and the correct form is `approvalPolicy: "never"` at turn creation — which is
exactly what `codex exec` does. It does not deny requests; it never creates them.

Rejected: **wiring `PostToolUse` to synthesize checkpoints.** It fires per call, and
`tool_input` / `tool_response` are the user's code — the same privacy argument §37 makes for
Claude Code. Rejected: **reading `transcript_path`.** It is an internal artifact, and §28/§31
already rejected that coupling for two other agents. Rejected: **auto-denying to "align with
`-p`".** `-p` is headless: it sets `approvalPolicy: "never"` and never receives the request.
Auto-denying in a user-driven thread is not the same policy; it is a reporter overriding the
human.
