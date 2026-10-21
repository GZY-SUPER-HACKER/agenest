/**
 * csd-pi-reporter — put standalone Pi runs on the dashboard.
 *
 * Pi is a **coding agent**, so this follows the direction §13 describes rather than §17's: the
 * machine running the agent originates every connection, and the dashboard only reads what it
 * reports. The extension is thin on purpose — it translates facts Pi already emits into the
 * `cpctl`-shaped HTTP calls, and never inspects tool output, counts events to decide completion,
 * or parses a stream (docs/design.md §2).
 *
 * ## Why one run is one session
 *
 * Pi's internal `turn` is one LLM response plus its tool calls, and a prompt that retries or
 * compacts produces several of them. A row per turn would turn one task into a row per model
 * call. `before_agent_start` fires once per user prompt and `agent_settled` fires when Pi has
 * nothing left to do automatically (no retry, no compaction retry, no queued follow-up) — that
 * pair is the unit the user actually asked for, so a run opens there and closes there.
 * `agent_start` is deliberately not the opener: it fires again for retries and compaction inside
 * one prompt.
 *
 * ## Why the personal agent stays silent
 *
 * The same `pi` binary is also what the dashboard's Agent tab spawns (`native`). Those runs are
 * one conversation with the operator, not work on the dashboard's Sessions tab, and reporting
 * them would show the channel talking to itself. The backend marks every child it owns with
 * `CSD_AGENT=1`, and this extension returns without registering anything when it sees it.
 *
 * ## What is not reported
 *
 * There is no `awaiting_user`: Pi has no standard approval or permission event, and inventing
 * one from a tool name would be an inference. The host row is posted at session start and every
 * five minutes, and no goodbye is sent on shutdown — silence is honestly rendered as "maybe
 * offline" (docs/design.md §14, §20).
 */

import { createHash, randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { homedir, hostname as osHostname } from 'node:os'
import { join } from 'node:path'

/** How `csd-agent` recognises its own file. Never delete a file that does not carry it. */
export const MARKER = 'csd-pi-reporter'

/** The dashboard's own default; `csd-agent --local` installs talk to the same port. */
export const DEFAULT_BASE_URL = 'http://127.0.0.1:8780'

/**
 * What the rail shows as the harness. Half of a host's identity, `(label, agent_type)`, so it is
 * stable and short — renaming it would look like a second machine.
 */
export const DEFAULT_AGENT_TYPE = 'pi'

// The dashboard's caps (backend/app/schemas.py). Sending more is a 422, not a truncation.
const MAX_TITLE = 200
const MAX_PROJECT = 120
const MAX_WORKSPACE = 500
const MAX_MESSAGE = 500
const MAX_SUMMARY = 2000
/** `event_key`'s cap in backend/app/schemas.py. */
const MAX_EVENT_KEY = 128

function cap(text, limit) {
  const value = typeof text === 'string' ? text : ''
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value
}

function pick(...values) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function positiveNumber(value, fallback) {
  const number = Number(value)
  return Number.isFinite(number) && number >= 0 ? number : fallback
}

/**
 * Parse one `KEY=value` per line, ignoring comments and blank lines.
 *
 * Not `source`d: this file is read by a long-lived Node process, and shell quoting rules are not
 * something Node can reproduce without inventing a second dialect for the same file.
 */
export function readEnvFile(path) {
  let text
  try {
    text = readFileSync(path, 'utf8')
  } catch {
    return {}
  }
  const values = {}
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const index = line.indexOf('=')
    const key = line.slice(0, index).trim()
    const value = line.slice(index + 1).trim()
    if (key) values[key] = value
  }
  return values
}

/** Collapse text to one presentable line. Titles are read on a phone; newlines are noise. */
export function firstLine(text, limit = 68) {
  if (typeof text !== 'string') return ''
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/\s+/g, ' ').trim()
    if (!line) continue
    // The ellipsis is inside the limit, not added on top of it: this value is sent as a `title`,
    // and one character over the API's cap would make the whole create a 422.
    return line.length > limit ? `${line.slice(0, Math.max(1, limit - 1)).trimEnd()}…` : line
  }
  return ''
}

/** Last path segment, for both separators: Pi runs on either kind of host. */
export function projectName(cwd) {
  const parts = String(cwd || '').split(/[\\/]+/).filter(Boolean)
  return cap(parts[parts.length - 1] || 'pi', MAX_PROJECT)
}

/**
 * Deterministic idempotency key, the same shape `cpctl` derives.
 *
 * The dashboard dedupes on `event_key` across the whole table, so a retry hashes to the same key
 * and a genuinely new event does not. The readable form is kept while it fits; callers supply
 * the parts that identify one logical event (a run, a turn, a heartbeat bucket).
 */
export function autoKey(...parts) {
  const digest = createHash('sha1').update(parts.join('|')).digest('hex').slice(0, 16)
  return `auto-${digest}`
}

function keyOf(...parts) {
  const key = parts.join(':')
  return key.length <= MAX_EVENT_KEY ? key : autoKey(...parts)
}

/**
 * Map the last assistant message's `stopReason` onto a dashboard outcome.
 *
 * This is a translation, not an inference: Pi records the reason on the message, and every
 * branch quotes it. `pending`, a missing value, or a value this build has never seen closes as
 * `unknown` — inventing `completed` there would turn uncertainty into a verdict (§2).
 */
export function outcomeFor(stopReason, errorMessage) {
  switch (stopReason) {
    case 'stop':
    case 'toolUse':
    case 'deferred':
      return { status: 'completed', summary: 'Pi run completed' }
    case 'length':
      // The run did finish; its last answer merely hit the provider's output ceiling.
      return { status: 'completed', summary: 'Pi run completed at the output-token ceiling' }
    case 'error': {
      const message =
        typeof errorMessage === 'string' && errorMessage.trim()
          ? firstLine(errorMessage, 300)
          : 'no reason given'
      return { status: 'failed', summary: `Pi run failed: ${message}` }
    }
    case 'aborted':
      return { status: 'interrupted', summary: 'Pi run was aborted' }
    default:
      return {
        status: 'unknown',
        summary: stopReason
          ? `Pi run ended with an unrecognized stop reason: ${stopReason}`
          : 'Pi run ended without a reported stop reason',
      }
  }
}

/** Expand a leading `~` without inventing a second path dialect. */
function expandHome(path, home) {
  const value = String(path || '')
  if (value === '~') return home
  if (value.startsWith('~/') || value.startsWith('~\\')) return join(home, value.slice(2))
  return value
}

/**
 * Where the reporter finds the dashboard, and who it says it is.
 *
 * Precedence: the injected `config` (tests, and a deliberate override) > the process environment
 * > `~/.csd/env` > the built-in default. `~/.csd/env` is read because a machine that already
 * runs `csd-agent` for Claude Code keeps `CSD_BASE_URL` and `CSD_INGEST_TOKEN` there in a 0600
 * file; asking the operator to copy the secret somewhere else is how two copies start to drift.
 *
 * Never throws: a reporter that cannot read its own config must not stop Pi from booting.
 */
export function resolveConfig(deps = {}) {
  const config = deps.config && typeof deps.config === 'object' ? deps.config : {}
  const env = deps.env ?? process.env
  const readEnv = deps.readEnv ?? readEnvFile
  const home = deps.home ?? homedir()

  const envFile =
    config.envFile === false || config.envFile === ''
      ? ''
      : pick(config.envFile, env.CSD_PI_ENV_FILE) || join(home, '.csd', 'env')
  const fromFile = envFile ? readEnv(expandHome(envFile, home)) : {}

  const host =
    config.host === false || config.host === ''
      ? ''
      : pick(config.host, env.CSD_HOST_LABEL) || (deps.hostname ?? osHostname)()

  return {
    baseUrl: (
      pick(config.baseUrl, env.CSD_BASE_URL, fromFile.CSD_BASE_URL) || DEFAULT_BASE_URL
    ).replace(/\/+$/, ''),
    token: pick(config.token, env.CSD_INGEST_TOKEN, fromFile.CSD_INGEST_TOKEN),
    envFile,
    // The API caps both at 64; a longer hostname would otherwise turn every report into a 422.
    agentType: (pick(config.agentType, env.CSD_AGENT_TYPE) || DEFAULT_AGENT_TYPE).slice(0, 64),
    host: host.slice(0, 64),
    reportHost: config.reportHost !== false,
    // Two switches say "do not report": one marks a Pi child the dashboard owns, the other is
    // the operator's own opt-out. Both are silent — a personal agent must not warn on every
    // launch about a feature it was never meant to have.
    disabled: env.CSD_AGENT === '1' || env.CSD_PI_REPORTER === '0',
    // Minutes, and 0 disables the timer. The run heartbeat exists only to keep a long model
    // step from being rendered as "maybe offline" (docs/design.md §8); it renews liveness,
    // never activity.
    runHeartbeatMinutes: positiveNumber(config.runHeartbeatMinutes, 5),
    hostHeartbeatMinutes: positiveNumber(config.hostHeartbeatMinutes, 5),
    // Seconds, the same unit `cpctl` uses (`CSD_TIMEOUT`), converted once at the seam.
    timeoutSeconds: Math.max(0.1, positiveNumber(config.timeoutSeconds, 3)),
    retries: Math.floor(positiveNumber(config.retries, 3)),
  }
}

/**
 * Wait for the reports, but never longer than a shutdown should take.
 *
 * A settle or a shutdown is Pi finishing: the retry budget that is right for a live run (3
 * attempts with backoff) is wrong here, because it would turn a dashboard outage into a Pi that
 * hangs before exit. A close that misses the deadline is dropped; the row then goes
 * `possibly_stale` instead of `interrupted`, which is the honest rendering of a report that did
 * not arrive.
 */
async function settleBriefly(promises, milliseconds = 2000) {
  let timer
  const deadline = new Promise((resolve) => {
    timer = setTimeout(resolve, milliseconds)
    timer?.unref?.()
  })
  await Promise.race([Promise.allSettled(promises), deadline])
  clearTimeout(timer)
}

/** A report that did not reach the server. Callers log it and carry on. */
export class ReportError extends Error {
  constructor(message) {
    super(message)
    this.name = 'ReportError'
  }
}

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * The HTTP client. Mirrors `cpctl` (sdk/cpctl/cpctl/cli.py) and the DSH reporter rather than
 * inventing a second set of rules: 3 s timeout, bounded retries with jitter, 4xx is the server's
 * answer and is not retried, and a failed report is logged and dropped. Reporting must never
 * fail the work it reports on (docs/design.md §6).
 */
export class ReportClient {
  constructor({
    baseUrl,
    token,
    timeoutMs = 3000,
    retries = 3,
    fetchImpl = globalThis.fetch,
    logger = console,
    sleepImpl = defaultSleep,
    random = Math.random,
  } = {}) {
    this.baseUrl = String(baseUrl || '').replace(/\/+$/, '')
    this.token = token || ''
    this.timeoutMs = timeoutMs
    this.retries = retries
    this.fetchImpl = fetchImpl
    this.logger = logger
    this.sleepImpl = sleepImpl
    this.random = random
  }

  async request(method, path, body) {
    if (!this.token) throw new ReportError('no ingest token; nothing can be reported')
    let lastError = null

    for (let attempt = 0; attempt <= this.retries; attempt += 1) {
      let response = null
      try {
        response = await this.fetchImpl(`${this.baseUrl}${path}`, {
          method,
          headers: {
            'Content-Type': 'application/json',
            'X-CPD-Token': this.token,
            'User-Agent': 'pi-csd-reporter/0.1',
          },
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: AbortSignal.timeout(this.timeoutMs),
        })
      } catch (error) {
        // Network failure, DNS, or the timeout above: transient by assumption, retried below.
        lastError = error
      }

      if (response) {
        if (response.ok) {
          const text = await response.text()
          return text ? JSON.parse(text) : {}
        }
        const detail = (await response.text().catch(() => '')).slice(0, 300)
        const failure = new ReportError(`${method} ${path} -> ${response.status}: ${detail}`)
        // A 4xx is the server answering, not a lost packet. Retrying a malformed request only
        // burns the agent's time; 408 is the one status that explicitly asks for a retry.
        if (response.status >= 400 && response.status < 500 && response.status !== 408) {
          throw failure
        }
        lastError = failure
      }

      if (attempt < this.retries) {
        const backoff = Math.min(0.5 * 2 ** attempt + this.random() * 0.25, 8) * 1000
        await this.sleepImpl(backoff)
      }
    }

    throw new ReportError(
      `${method} ${path} failed after ${this.retries + 1} attempts: ${lastError?.message ?? lastError}`,
    )
  }

  createSession(body) {
    return this.request('POST', '/api/sessions', body)
  }

  updateSession(sessionId, body) {
    return this.request('PATCH', `/api/sessions/${encodeURIComponent(sessionId)}`, body)
  }

  reportEvent(sessionId, body) {
    return this.request('POST', `/api/sessions/${encodeURIComponent(sessionId)}/events`, body)
  }

  reportHost(body) {
    return this.request('POST', '/api/hosts', body)
  }
}

/**
 * Install the reporter on a Pi `ExtensionAPI`.
 *
 * Exported separately from the default factory so tests can drive the run lifecycle with a fake
 * `pi` and a fake client, without a Pi process or a network.
 *
 * @returns {{flush: () => Promise<void>, dispose: () => Promise<void>}}
 */
export function install(pi, deps = {}) {
  const config = resolveConfig(deps)
  // **stderr, never stdout.** In `pi --print` the answer is the stdout stream, and a reporter
  // that logs there injects its own lines into whatever is consuming the agent's output — a
  // wrapper piping `pi -p` into a file or another tool would see them as part of the answer.
  const logger =
    deps.logger ?? { warn: console.error, info: console.error, log: console.error }
  const now = deps.now ?? (() => Date.now())
  const newId = deps.randomUUID ?? randomUUID
  const setIntervalImpl = deps.setInterval ?? setInterval
  const clearIntervalImpl = deps.clearInterval ?? clearInterval

  // Pick one method rather than calling both: a console and a structured logger expose
  // different subsets, and calling both lands every line twice where they overlap.
  const warn = (message) => {
    if (typeof logger.warn === 'function') logger.warn(message)
    else if (typeof logger.log === 'function') logger.log(message)
  }
  const info = (message) => {
    if (typeof logger.info === 'function') logger.info(message)
    else if (typeof logger.log === 'function') logger.log(message)
  }

  const handle = { flush: async () => {}, dispose: async () => {} }

  // Inert, not broken. A Pi child the dashboard spawned for its Agent tab is not a coding
  // session, and an operator who set CSD_PI_REPORTER=0 has said no. Registering nothing is
  // what keeps both promises cheap.
  if (config.disabled) return handle

  if (!config.token) {
    // A Pi host can have the extension installed before anyone has put a token on the machine.
    // An extension that throws at boot would take the whole Pi startup down with it.
    warn(
      'pi-csd-reporter: no ingest token (CSD_INGEST_TOKEN or ~/.csd/env); ' +
        'reporting is disabled until one is present',
    )
    return handle
  }

  const client =
    deps.client ??
    new ReportClient({
      baseUrl: config.baseUrl,
      token: config.token,
      timeoutMs: config.timeoutSeconds * 1000,
      retries: config.retries,
      logger,
    })

  /** The run a prompt opened, or null. Pi processes one conversation at a time. */
  let current = null
  /** Every serialized chain still in flight, so `flush()` can await the state machine. */
  const inFlight = new Set()
  const timers = new Set()
  let hostHeartbeat = null
  let lastCwd = ''
  let disposed = false

  function timer(fn, minutes) {
    if (!(minutes > 0)) return null
    const value = setIntervalImpl(fn, minutes * 60_000)
    // A reporter must never be the reason a process refuses to exit. Pi owns its own lifetime;
    // an unref'd timer still fires while it is up.
    value?.unref?.()
    timers.add(value)
    return value
  }

  function stopTimer(value) {
    if (!value) return
    clearIntervalImpl(value)
    timers.delete(value)
  }

  /**
   * Serialize one run's reports.
   *
   * Each report is an await, so a fast run could otherwise finish before its create resolved and
   * the `PATCH` would name a session id that did not exist yet. One chain per run keeps the order
   * Pi emitted, and a rejection is logged rather than left unhandled.
   */
  function enqueue(run, task) {
    const next = run.chain
      .then(task)
      .catch((error) => warn(`pi-csd-reporter: ${error?.message ?? error}`))
    run.chain = next
    inFlight.add(next)
    next.finally(() => inFlight.delete(next))
    return next
  }

  function openRun(event, ctx) {
    const cwd = typeof ctx?.cwd === 'string' && ctx.cwd ? ctx.cwd : lastCwd
    const run = {
      id: newId(),
      cwd,
      project: projectName(cwd),
      title: cap(firstLine(event?.prompt, 68) || `Pi · ${projectName(cwd)}`, MAX_TITLE),
      csdId: null,
      stopReason: null,
      errorMessage: null,
      heartbeat: null,
      closed: false,
      chain: Promise.resolve(),
    }
    return run
  }

  async function ensureSession(run) {
    if (run.csdId) return true
    if (run.closed || disposed) return false

    const body = {
      agent_type: config.agentType,
      title: run.title,
      project_name: run.project,
      workspace: cap(run.cwd, MAX_WORKSPACE),
      status: 'running',
      // No `started_at`: the dashboard labels any explicit value as "(reported late)", and this
      // report is live. The server's receive time is the honest start.
    }
    if (config.host) body.host = config.host

    try {
      const session = await client.createSession(body)
      run.csdId = session?.id ?? null
      if (!run.csdId) throw new Error('the dashboard returned no session id')
      info(`pi-csd-reporter: run ${run.id} -> ${run.csdId}`)
      startHeartbeat(run)
      return true
    } catch (error) {
      // Leave csdId null: the next event on this run tries again. A dashboard that was down for
      // the first second of a run should not cost the whole run.
      warn(`pi-csd-reporter: could not open a session for ${run.id}: ${error?.message ?? error}`)
      return false
    }
  }

  function startHeartbeat(run) {
    stopTimer(run.heartbeat)
    run.heartbeat = timer(() => {
      if (run.closed || !run.csdId) return
      // The bucket is what makes retries idempotent without making the beat a duplicate: the
      // dashboard applies no side effects to a repeated `event_key` (§7), so a constant message
      // would renew liveness exactly once.
      const bucket = Math.floor(now() / (config.runHeartbeatMinutes * 60_000))
      enqueue(run, async () => {
        if (run.closed || !run.csdId) return
        await client.reportEvent(run.csdId, {
          event_type: 'heartbeat',
          message: cap('Pi run still running', MAX_MESSAGE),
          event_key: keyOf('pi', run.id, 'hb', bucket),
          occurred_at: new Date(now()).toISOString(),
          payload: { run: run.id },
        })
      })
    }, config.runHeartbeatMinutes)
  }

  async function reportTurn(run, turnIndex) {
    if (run.closed) return
    if (!(await ensureSession(run))) return
    const number = Number.isFinite(Number(turnIndex)) ? Number(turnIndex) + 1 : null
    await client.reportEvent(run.csdId, {
      // `turn_end` is progress, not a boundary: a prompt that calls tools ends several turns,
      // and the run is only over when Pi settles.
      event_type: 'progress',
      message: cap(number ? `turn ${number} complete` : 'turn complete', MAX_MESSAGE),
      event_key: keyOf('pi', run.id, 'turn', number ?? 'last'),
      occurred_at: new Date(now()).toISOString(),
      payload: number ? { run: run.id, turn: number } : { run: run.id },
    })
  }

  function recordStop(run, message) {
    if (!message || message.role !== 'assistant') return
    // Both `turn_end.message` and `message_end` carry the same message. Recording from both is
    // deliberate: whichever arrives last wins, and on a retried run that is the final answer.
    if (typeof message.stopReason === 'string') run.stopReason = message.stopReason
    if (typeof message.errorMessage === 'string' && message.errorMessage) {
      run.errorMessage = message.errorMessage
    }
  }

  async function closeRun(run, status, summary) {
    stopTimer(run.heartbeat)
    run.heartbeat = null
    if (run.closed) return
    run.closed = true
    if (current === run) current = null
    if (!run.csdId) {
      // A run that never got a session is dropped rather than retried here: the next prompt will
      // create a fresh one, and a late `PATCH` would be more confusing than the gap.
      return
    }
    try {
      await client.updateSession(run.csdId, {
        status,
        summary: cap(summary, MAX_SUMMARY),
      })
      info(`pi-csd-reporter: ${run.csdId} -> ${status}`)
    } catch (error) {
      warn(`pi-csd-reporter: could not close ${run.csdId}: ${error?.message ?? error}`)
    }
  }

  async function reportHost() {
    if (!config.reportHost || !config.host) return
    try {
      await client.reportHost({
        label: config.host,
        agent_type: config.agentType,
        connected: true,
        // Declaring a machine driveable is `csd-agent`'s job; this extension only says "the
        // harness on this machine is here". Declaring on its behalf would put an ssh prompt in
        // front of the user for a connector that cannot install a key (§24).
        wants_access: false,
      })
    } catch (error) {
      warn(`pi-csd-reporter: could not report host ${config.host}: ${error?.message ?? error}`)
    }
  }

  const on = (event, handler) => {
    if (typeof pi?.on === 'function') pi.on(event, handler)
  }

  // A session starts once per Pi process, and again on /new, /resume and /fork. The previous
  // run was closed by `session_shutdown` before this fires; closing a leftover here is the
  // belt-and-braces version, because a row left `running` is worse than an honest interrupt.
  on('session_start', (_event, ctx) => {
    if (typeof ctx?.cwd === 'string' && ctx.cwd) lastCwd = ctx.cwd
    const leftover = current
    if (leftover && !leftover.closed) {
      enqueue(leftover, () =>
        closeRun(leftover, 'interrupted', 'Pi started a new session before the run settled'),
      )
    }
    stopTimer(hostHeartbeat)
    hostHeartbeat = null
    void reportHost()
    hostHeartbeat = timer(() => void reportHost(), config.hostHeartbeatMinutes)
  })

  on('before_agent_start', (event, ctx) => {
    // A queued follow-up prompt (or an injected continuation) belongs to the run that is already
    // open: one settled run is one row, so this is ignored rather than opening a second.
    if (current && !current.closed) return
    const run = openRun(event, ctx)
    current = run
    enqueue(run, () => ensureSession(run))
  })

  on('turn_end', (event, _ctx) => {
    const run = current
    if (!run || run.closed) return
    recordStop(run, event?.message)
    const index = event?.turnIndex
    enqueue(run, () => reportTurn(run, index))
  })

  on('message_end', (event, _ctx) => {
    const run = current
    if (!run || run.closed) return
    recordStop(run, event?.message)
  })

  // Pi awaits this one, so the handler returns a promise — bounded, because a dead dashboard
  // must not hold the agent's exit. `agent_settled` means no retry, compaction retry or queued
  // follow-up is left, which is exactly the boundary a dashboard row should close on.
  on('agent_settled', async (_event, _ctx) => {
    const run = current
    if (!run || run.closed) return
    const outcome = outcomeFor(run.stopReason, run.errorMessage)
    await settleBriefly([enqueue(run, () => closeRun(run, outcome.status, outcome.summary))])
  })

  on('session_shutdown', async (_event, _ctx) => {
    stopTimer(hostHeartbeat)
    hostHeartbeat = null
    const run = current
    if (!run || run.closed) return
    await settleBriefly([
      enqueue(run, () =>
        closeRun(run, 'interrupted', 'Pi switched or exited before the run settled'),
      ),
    ])
    // No goodbye is sent for the host: a Pi restart or a /new is not an uninstall, and silence
    // is honestly rendered as "maybe offline" (§14, §20).
  })

  handle.flush = async () => {
    // A chain can enqueue another while it runs; loop until the set is quiet.
    while (inFlight.size > 0) {
      await Promise.allSettled([...inFlight])
    }
  }

  handle.dispose = async () => {
    if (disposed) return
    disposed = true
    for (const value of [...timers]) stopTimer(value)
    timers.clear()
    const run = current
    if (run && !run.closed) {
      await settleBriefly([enqueue(run, () => closeRun(run, 'interrupted', 'Pi exited before the run settled'))])
    }
    current = null
  }

  return handle
}

/** Pi's extension entry point: the default export is called with the `ExtensionAPI`. */
export default function csdReporter(pi) {
  install(pi)
}


