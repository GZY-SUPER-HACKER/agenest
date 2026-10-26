/**
 * dsh-csd-reporter — put DeepSeek Harness turns on the dashboard.
 *
 * DSH is a **coding agent**, and this plugin is its reporter. The dashboard never calls into
 * it; the machine running the agent originates every connection, the same direction
 * `csd-agent` follows for Claude Code (design.md §1). What the plugin does is thin on purpose:
 * it translates durable facts DSH already recorded (`session/event`, `session/created`,
 * `session/disposed`) into `cpctl`-shaped HTTP calls. It never inspects tool output, counts
 * events to decide completion, or parses a stream (design.md §2).
 *
 * ## Why a native plugin and not DSH's Claude Code hook bridge
 *
 * DSH ships `dsh-hooks-claude-code`, which runs a Claude Code `settings.json` hooks block, and
 * pointing `csd-agent` at it is the obvious one-line answer. It cannot carry this contract: the
 * bridge supports `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, and
 * the two subagent events, and deliberately **not** `SessionEnd` or `Notification`. A session
 * would be created and never closed, and "waiting for a human" could not be shown at all — the
 * dashboard would have to guess from the process, which is the one thing it refuses to do.
 * The durable log has all of it, so the plugin reads the durable log.
 *
 * ## Why a turn is the session
 *
 * A DSH session is a resumable conversation with no end: it stays in the store for the life of
 * the process and can be resumed after a restart. A dashboard row built on it could only ever
 * say `running`, which would be a state nobody reported. A **turn** opens when the loop claims
 * input and closes with a reason DSH records itself (`turn/end.reason`), which maps onto
 * `completed` / `failed` / `interrupted` without a guess (design.md §28).
 *
 * ## What is ignored, and why
 *
 * Subagent child sessions (`header.origin === 'subagent'`, or a non-zero delegation depth) are
 * part of the parent's turn. Reporting them would multiply a single task into one row per
 * child and make the rail read as more work than happened.
 */

import { mkdirSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

import { resolveConfig } from './config.js'
import { autoKey, firstLine, ReportClient, titleFromContent, turnOutcome } from './reporter.js'

export const name = 'dsh-csd-reporter'

// The store is read once at load to recover a turn that was already running when this plugin
// (re)mounted — a live patch reload otherwise loses that turn's outcome. Nothing else here
// needs the service, but injecting it is what guarantees it exists before `apply` runs.
export const inject = ['sessions']

const TURN_START = 'turn/start'
const TURN_END = 'turn/end'
const STEP_END = 'step/end'
const ASSISTANT_MESSAGE = 'assistant/message'
const TODO_WRITE = 'todo/write'
const USER_MESSAGE = 'user/message'
const SESSION_TITLE = 'session/title'
const APPROVAL_ASKED = 'approval/asked'
const APPROVAL_DECIDED = 'approval/decided'

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

/**
 * A readable idempotency key, hashed only when caller-supplied ids (an approval id, a
 * question id) would push it past the server's cap. Hashing there keeps retries idempotent
 * without ever sending a key the API rejects.
 */
function keyOf(...parts) {
  const key = parts.join(':')
  return key.length <= MAX_EVENT_KEY ? key : autoKey(...parts)
}

/**
 * The assistant's own sentence from a DSH `AssistantMessage`.
 *
 * `content` interleaves text, reasoning and tool calls (dsh-llm `ContentBlock`). Only `text`
 * blocks are prose addressed to the reader. A reasoning block is thinking, not a checkpoint.
 */
export function assistantText(message) {
  if (!message || message.role !== 'assistant' || !Array.isArray(message.content)) return ''
  return message.content
    .filter((block) => block?.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
    .trim()
}

/**
 * Whether the same assistant message requested a tool.
 *
 * This is what makes the checkpoint rule need no pending state: DSH puts the prose and the
 * tool-call blocks in one event. A message with tools was commentary; one without is the
 * answer, and the turn boundary closes it (design.md §37).
 */
export function hasToolCall(message) {
  if (!message || !Array.isArray(message.content)) return false
  return message.content.some((block) => block?.type === 'tool-call')
}

/** One line for a whole-list todo snapshot: progress plus the item being worked now. */
function planLine(todos) {
  const total = todos.length
  const done = todos.filter((todo) => todo.status === 'completed').length
  const active = todos.find((todo) => todo.status === 'in_progress')
  return active
    ? `Plan ${done}/${total} · ${firstLine(active.title, 200)}`
    : `Plan ${done}/${total} done`
}

/** Last path segment, for both separators: a DSH session may be described with either. */
function projectName(cwd) {  const parts = String(cwd || '').split(/[\\/]+/).filter(Boolean)
  return cap(parts[parts.length - 1] || 'dsh', MAX_PROJECT)
}

function isoTime(event, fallback) {
  const millis = event && Number.isFinite(event.time) ? event.time : null
  return new Date(millis ?? fallback()).toISOString()
}

/**
 * Wait for the reports, but never longer than a shutdown should take.
 *
 * A dispose is DSH exiting: the retry budget that is right for a live turn (3 attempts with
 * backoff) is wrong here, because it would turn a dashboard outage into a SIGTERM that hangs
 * for fifteen seconds. A close that misses the deadline is dropped; the session then goes
 * `possibly_stale` instead of `interrupted`, which is the honest rendering of a report that
 * did not arrive.
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

/**
 * The turn a durable log leaves open, if any.
 *
 * Only used when the plugin mounts into a process that is already running a turn — a live
 * patch reload, or an install during a session. Without it the eventual `turn/end` would
 * arrive for a turn this instance never opened, and the dashboard row would sit `running`
 * until someone closed it by hand. The log is the same evidence DSH uses after a restart.
 */
export function openTurnOf(events) {
  let open = null
  for (const event of Array.isArray(events) ? events : []) {
    switch (event?.type) {
      case TURN_START:
        open = { number: Number(event.data?.turn), firstPromptSeq: null, title: '' }
        break
      case TURN_END:
        open = null
        break
      case USER_MESSAGE:
        if (!open || open.firstPromptSeq !== null || event.data?.source?.kind !== 'user') break
        open.firstPromptSeq = event.seq
        open.title = titleFromContent(event.data?.content)
        break
      case SESSION_TITLE: {
        if (!open || open.firstPromptSeq === null || !event.data?.title) break
        const seqs = Array.isArray(event.data.messageSeqs) ? event.data.messageSeqs : []
        if (seqs.includes(open.firstPromptSeq)) open.title = event.data.title
        break
      }
      default:
        break
    }
  }
  return open
}

/**
 * Install the reporter on a Cordis context.
 *
 * Exported separately from `apply` so tests can drive the state machine with a fake context
 * and a fake client, without a DSH process or a network.
 *
 * @returns {{flush: () => Promise<void>, dispose: () => Promise<void>}}
 */
export function install(ctx, rawConfig = {}, deps = {}) {
  const config = resolveConfig(rawConfig, deps)
  const logger = deps.logger ?? ctx?.logger ?? console
  const now = deps.now ?? (() => Date.now())
  const setIntervalImpl = deps.setInterval ?? setInterval
  const clearIntervalImpl = deps.clearInterval ?? clearInterval

  // Cordis's logger and `console` expose different subsets; pick one method rather than
  // calling both, or every line lands twice wherever both exist.
  const warn = (message) => {
    if (typeof logger.warn === 'function') logger.warn(message)
    else if (typeof logger.log === 'function') logger.log(message)
  }
  const info = (message) => {
    if (typeof logger.info === 'function') logger.info(message)
    else if (typeof logger.log === 'function') logger.log(message)
  }

  const handle = {
    flush: async () => {},
    dispose: async () => {},
  }

  if (!config.token) {
    // Inert, not broken. A DSH profile can be installed before anyone has put a token on the
    // machine, and a plugin that throws at boot would take the whole profile down with it.
    warn(
      'dsh-csd-reporter: no ingest token (CSD_INGEST_TOKEN, ~/.csd/env, or the plugin config); ' +
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

  /** @type {Map<string, object>} one conversation state per live DSH session */
  const states = new Map()
  const timers = new Set()
  let disposed = false

  function timer(fn, minutes) {
    if (!(minutes > 0)) return null
    const value = setIntervalImpl(fn, minutes * 60_000)
    // A reporter must never be the reason a process refuses to exit. DSH owns its own
    // lifetime; an unref'd timer still fires while the server is up.
    value?.unref?.()
    timers.add(value)
    return value
  }

  function stopTimer(value) {
    if (!value) return
    clearIntervalImpl(value)
    // A long-lived server runs thousands of turns; leaving cleared handles in the set would
    // grow it for no reason.
    timers.delete(value)
  }

  function stateFor(session) {
    const id = session?.id
    if (!id) return null
    const header = session.header ?? {}
    // A subagent's work belongs to the turn that spawned it, not to a row of its own.
    if (header.origin === 'subagent' || Number(header.delegationDepth ?? 0) > 0) return null

    let state = states.get(id)
    if (state) return state

    const cwd = typeof header.cwd === 'string' && header.cwd ? header.cwd : process.cwd()
    state = {
      id,
      cwd,
      project: projectName(cwd),
      // The open turn is one immutable record per `turn/start`. A shared mutable `csdId`
      // looked simpler and was wrong: the next turn's start arrived while the previous turn's
      // completion was still queued, and the queued task read the overwritten id.
      current: null,
      chain: Promise.resolve(),
    }
    states.set(id, state)
    return state
  }

  /**
   * Publish where each live turn's session lives, for `cpctl task done|failed` to find.
   *
   * The agent's shell cannot see this plugin's process or environment. One file per DSH
   * process under `~/.csd/state/sessions.d/` is the handoff; `cpctl` reads the newest entry
   * matching the working directory. Removed when the last turn closes.
   */
  function sessionMapFile() {
    const home = process.env.CSD_AGENT_HOME || join(homedir(), '.csd')
    return join(home, 'state', 'sessions.d', `dsh-${process.pid}.json`)
  }

  function syncSessionMap() {
    const byCwd = {}
    for (const state of states.values()) {
      const turn = state.current
      if (!turn || turn.closed || !turn.csdId || !state.cwd) continue
      if (!byCwd[state.cwd]) byCwd[state.cwd] = []
      byCwd[state.cwd].push({
        session_id: turn.csdId,
        harness_id: state.id,
        pid: process.pid,
        started_at: turn.startedAt,
      })
    }
    for (const entries of Object.values(byCwd)) {
      entries.sort((a, b) => b.started_at - a.started_at)
    }
    try {
      const file = sessionMapFile()
      if (Object.keys(byCwd).length === 0) {
        rmSync(file, { force: true })
        return
      }
      mkdirSync(dirname(file), { recursive: true })
      const tmp = `${file}.tmp`
      writeFileSync(tmp, JSON.stringify({ writer: 'dsh', by_cwd: byCwd }, null, 2) + '\n', 'utf8')
      renameSync(tmp, file)
    } catch {
      // Convenience for the agent's CLI, never a reason to disturb a turn.
    }
  }

  function newTurn(number) {
    return {
      number,
      csdId: null,
      // Seconds, not milliseconds: the map is shared with the Python connectors, which use
      // epoch seconds, and `cpctl` compares the newest entry across all of them.
      startedAt: now() / 1000,
      title: '',
      appliedTitle: '',
      // Seq of this turn's first human message. `session/title` is derived from the whole
      // conversation, so a later turn would otherwise inherit the first turn's title.
      firstPromptSeq: null,
      heartbeat: null,
      closed: false,
      // Steps whose assistant message was already reported as a checkpoint. `step/end` uses
      // this to stay silent: the prose is the checkpoint, and a "step N complete" line right
      // after it is the same fact twice (design.md §37).
      checkpointedSteps: new Set(),
      // A `todo/write` in this turn makes the plan the progress track; `step/end` falls back to
      // silence rather than narrating the same work in the machine's voice.
      hasPlan: false,
    }
  }

  /**
   * Serialize one conversation's operations.
   *
   * DSH emits `turn/start`, `user/message`, and `turn/end` in log order, but each report is an
   * await: a fast turn could otherwise finish before its create resolved, and the `PATCH`
   * would name a session id that did not exist yet. One promise chain per DSH session keeps
   * the order the log already has, and a rejection is logged rather than left unhandled.
   */
  function enqueue(state, task) {
    state.chain = state.chain
      .then(task)
      .catch((error) => warn(`dsh-csd-reporter: ${error?.message ?? error}`))
    return state.chain
  }

  async function ensureSession(state, turn) {
    if (turn.csdId) return true
    if (turn.closed || disposed) return false

    const body = {
      agent_type: config.agentType,
      title: cap(turn.title || `DSH · ${state.project}`, MAX_TITLE),
      project_name: state.project,
      workspace: cap(state.cwd, MAX_WORKSPACE),
      status: 'running',
      // No `started_at`: the dashboard labels any explicit value as "(reported late)", and
      // this report is live. The server's receive time is the honest start.
    }
    if (config.host) body.host = config.host

    try {
      const session = await client.createSession(body)
      turn.csdId = session?.id ?? null
      if (!turn.csdId) throw new Error('the dashboard returned no session id')
      if (turn.title) turn.appliedTitle = turn.title
      info(`dsh-csd-reporter: turn ${turn.number} of ${state.id} -> ${turn.csdId}`)
      startHeartbeat(state, turn)
      syncSessionMap()
      return true
    } catch (error) {
      // Leave csdId null: the next event on this turn tries again. A dashboard that was down
      // for the first second of a turn should not cost the whole turn.
      warn(`dsh-csd-reporter: could not open a session for ${state.id}: ${error?.message ?? error}`)
      return false
    }
  }

  function startHeartbeat(state, turn) {
    stopTimer(turn.heartbeat)
    turn.heartbeat = timer(() => {
      if (turn.closed || !turn.csdId) return
      // The bucket is what makes retries idempotent without making the beat a duplicate: the
      // dashboard applies no side effects to a repeated `event_key` (design.md §7), so a
      // constant message would renew liveness exactly once.
      const bucket = Math.floor(now() / (config.turnHeartbeatMinutes * 60_000))
      enqueue(state, async () => {
        if (turn.closed || !turn.csdId) return
        await client.reportEvent(turn.csdId, {
          event_type: 'heartbeat',
          message: cap(`DSH turn ${turn.number} still running`, MAX_MESSAGE),
          event_key: keyOf('dsh', state.id, turn.number, 'hb', bucket),
          occurred_at: new Date(now()).toISOString(),
          payload: { turn: turn.number },
        })
      })
    }, config.turnHeartbeatMinutes)
  }

  function closeTurn(state, turn) {
    stopTimer(turn.heartbeat)
    turn.heartbeat = null
    turn.closed = true
    if (state.current === turn) state.current = null
    syncSessionMap()
  }

  async function startTurn(state, turn) {
    await ensureSession(state, turn)
  }

  async function applyTitle(state, turn, title) {
    const value = firstLine(title, MAX_TITLE)
    if (!value || turn.closed || value === turn.appliedTitle) return
    turn.title = value
    if (!(await ensureSession(state, turn))) return
    try {
      await client.updateSession(turn.csdId, { title: value })
      turn.appliedTitle = value
    } catch (error) {
      warn(`dsh-csd-reporter: could not set the title for ${turn.csdId}: ${error?.message ?? error}`)
    }
  }

  async function reportStep(state, turn, data, event) {
    if (turn.closed) return
    // A checkpoint anywhere in this turn — prose or a plan snapshot — is the progress track.
    // `step/end` is the fallback for a turn that has neither, not a parallel commentary track
    // in the machine's voice (design.md §37).
    if (turn.hasPlan || turn.checkpointedSteps.size > 0) return
    const step = Number(data?.step)
    if (!(await ensureSession(state, turn))) return
    const message = Number.isFinite(step) ? `step ${step} complete` : 'step complete'
    await client.reportEvent(turn.csdId, {
      event_type: 'progress',
      message: cap(message, MAX_MESSAGE),
      event_key: keyOf('dsh', state.id, turn.number, 'step', Number.isFinite(step) ? step : event.seq),
      occurred_at: isoTime(event, now),
      payload: { turn: turn.number, ...(Number.isFinite(step) ? { step } : {}) },
    })
  }

  /**
   * Report an assistant message as a checkpoint.
   *
   * Called only when the same message also requested a tool (commentary), or when DSH marked
   * it `interrupted` (a delivered prefix that never became an answer). The agent's own
   * `message.id` is the idempotency key; the text is never the key.
   */
  async function reportCheckpoint(state, turn, data, event) {
    if (turn.closed) return
    const message = data?.message
    const text = assistantText(message)
    if (!text) return
    if (!hasToolCall(message) && data?.interrupted !== true) return
    if (!(await ensureSession(state, turn))) return
    const step = Number(data?.step)
    await client.reportEvent(turn.csdId, {
      event_type: 'checkpoint',
      message: cap(firstLine(text, MAX_MESSAGE), MAX_MESSAGE),
      event_key: keyOf('dsh', state.id, turn.number, 'message', message?.id ?? event.seq),
      occurred_at: isoTime(event, now),
      payload: {
        source: 'prose',
        turn: turn.number,
        ...(message?.id ? { message_id: message.id } : {}),
        ...(data?.interrupted === true ? { interrupted: true } : {}),
      },
    })
    if (Number.isFinite(step)) turn.checkpointedSteps.add(step)
  }

  /**
   * Report a `todo/write` whole-list snapshot as a plan checkpoint.
   *
   * DSH's todo list is already the agent's own plan; the event is a full replacement, so the
   * payload is too. Latest write wins, which is why the idempotency key is the event's `seq`
   * rather than anything derived from the contents.
   */
  async function reportPlan(state, turn, data, event) {
    if (turn.closed) return
    const todos = Array.isArray(data?.todos) ? data.todos : []
    const plan = todos
      .slice(0, 50)
      .map((todo) => ({
        title: cap(typeof todo?.content === 'string' ? todo.content : '', 200),
        status:
          todo?.status === 'completed' || todo?.status === 'in_progress'
            ? todo.status
            : 'pending',
      }))
      .filter((item) => item.title)
    if (!plan.length) return
    if (!(await ensureSession(state, turn))) return
    await client.reportEvent(turn.csdId, {
      event_type: 'checkpoint',
      message: cap(planLine(plan), MAX_MESSAGE),
      event_key: keyOf('dsh', state.id, turn.number, 'plan', event.seq),
      occurred_at: isoTime(event, now),
      payload: { source: 'plan', turn: turn.number, plan },
    })
    turn.hasPlan = true
  }

  async function reportApproval(state, turn, data, event) {
    if (turn.closed) return
    if (!(await ensureSession(state, turn))) return
    const tool = typeof data?.toolName === 'string' && data.toolName ? data.toolName : 'a tool'
    const reason = firstLine(data?.reason, 160)
    await client.reportEvent(turn.csdId, {
      event_type: 'awaiting_user',
      message: cap(`approval needed: ${tool}${reason ? ` — ${reason}` : ''}`, MAX_MESSAGE),
      event_key: keyOf('dsh', state.id, turn.number, 'approval', data?.id ?? event.seq),
      occurred_at: isoTime(event, now),
      payload: { turn: turn.number, tool },
    })
  }

  async function reportApprovalDecision(state, turn, data, event) {
    if (turn.closed || !turn.csdId) return
    const outcome = typeof data?.outcome === 'string' ? data.outcome : 'decided'
    // A decision is what makes `awaiting_user` stop being the newest event. Reporting it as
    // `progress` clears "needs attention" without inventing a clear protocol.
    await client.reportEvent(turn.csdId, {
      event_type: 'progress',
      message: cap(`approval ${outcome}`, MAX_MESSAGE),
      event_key: keyOf('dsh', state.id, turn.number, 'approval', data?.id ?? event.seq, 'decided'),
      occurred_at: isoTime(event, now),
      payload: { turn: turn.number, outcome },
    })
  }

  async function endTurn(state, turn, data) {
    const outcome = turnOutcome(data?.reason)
    const csdId = turn.csdId
    if (!csdId) {
      // A turn that never got a session is dropped rather than retried here: the next turn
      // will create a fresh one, and a late `PATCH` would be more confusing than the gap.
      closeTurn(state, turn)
      return
    }
    try {
      await client.updateSession(csdId, {
        status: outcome.status,
        summary: cap(outcome.summary, MAX_SUMMARY),
      })
      info(`dsh-csd-reporter: ${csdId} -> ${outcome.status}`)
    } finally {
      // Clear even when the patch failed: the client has already retried, and keeping the row
      // open would only make the next turn's events report against the wrong session.
      closeTurn(state, turn)
    }
  }

  async function closeOpenTurn(state, turn, summary) {
    if (!turn) return
    if (turn.closed || !turn.csdId) {
      closeTurn(state, turn)
      return
    }
    try {
      await client.updateSession(turn.csdId, {
        status: 'interrupted',
        summary: cap(summary, MAX_SUMMARY),
      })
    } finally {
      closeTurn(state, turn)
    }
  }

  async function reportHost() {
    if (!config.reportHost || !config.host) return
    try {
      await client.reportHost({
        label: config.host,
        agent_type: config.agentType,
        connected: true,
        // Declaring a machine driveable is `csd-agent`'s job; this plugin only says "the
        // harness on this machine is here". Declaring on its behalf would put an ssh prompt in
        // front of the user for a connector that cannot install a key (design.md §24).
        wants_access: false,
      })
    } catch (error) {
      warn(`dsh-csd-reporter: could not report host ${config.host}: ${error?.message ?? error}`)
    }
  }

  function on(event, handler) {
    if (typeof ctx?.on === 'function') ctx.on(event, handler)
  }

  on('session/created', (session) => {
    stateFor(session)
  })

  on('session/disposed', (session) => {
    const state = stateFor(session)
    if (!state) return
    const turn = state.current
    enqueue(state, () =>
      closeOpenTurn(state, turn, 'DSH disposed the session before the turn reported an end'),
    ).finally(() => {
      states.delete(state.id)
    })
  })

  on('session/event', (session, event) => {
    const state = stateFor(session)
    if (!state || !event) return

    switch (event.type) {
      case TURN_START: {
        // `turn/start` is the open boundary, and the log gives it a number even when the turn
        // closes without a step. Creating here (rather than on the first prompt) is what keeps
        // a blocked or empty turn visible as the reported outcome it is.
        const previous = state.current
        const turn = newTurn(Number(event.data?.turn))
        state.current = turn
        if (previous && !previous.closed) {
          // The log should always pair these. If a build ever does not, closing the old row
          // is better than leaving it `running` for the life of the server.
          enqueue(state, () =>
            closeOpenTurn(state, previous, 'DSH opened a new turn before the previous one ended'),
          )
        }
        enqueue(state, () => startTurn(state, turn))
        break
      }
      case USER_MESSAGE: {
        // Only a human prompt renames the turn. Synthetic injections share this event type,
        // and a title is the user's words, not the machinery's.
        if (event.data?.source?.kind !== 'user') break
        const turn = state.current
        if (!turn) break
        if (turn.firstPromptSeq === null) turn.firstPromptSeq = event.seq
        const title = titleFromContent(event.data?.content)
        if (title) enqueue(state, () => applyTitle(state, turn, title))
        break
      }
      case SESSION_TITLE: {
        const turn = state.current
        if (!turn || !event.data?.title) break
        // A title derived from an earlier turn's prompt must not rename this one.
        const seqs = Array.isArray(event.data.messageSeqs) ? event.data.messageSeqs : []
        if (turn.firstPromptSeq === null || !seqs.includes(turn.firstPromptSeq)) break
        enqueue(state, () => applyTitle(state, turn, event.data.title))
        break
      }
      case STEP_END: {
        const turn = state.current
        if (turn) enqueue(state, () => reportStep(state, turn, event.data, event))
        break
      }
      case ASSISTANT_MESSAGE: {
        const turn = state.current
        if (turn) enqueue(state, () => reportCheckpoint(state, turn, event.data, event))
        break
      }
      case TODO_WRITE: {
        const turn = state.current
        if (turn) enqueue(state, () => reportPlan(state, turn, event.data, event))
        break
      }
      case APPROVAL_ASKED: {
        const turn = state.current
        if (turn) enqueue(state, () => reportApproval(state, turn, event.data, event))
        break
      }
      case APPROVAL_DECIDED: {
        const turn = state.current
        if (turn) enqueue(state, () => reportApprovalDecision(state, turn, event.data, event))
        break
      }
      case TURN_END: {
        const turn = state.current
        if (turn) enqueue(state, () => endTurn(state, turn, event.data))
        break
      }
      default:
        break
    }
  })

  // `user-questions` is a waterfall, not a log event, so it is observed by delegating. The
  // plugin reports and immediately calls `next()`: it has no opinion about the answer, and a
  // listener that swallowed the request would break DSH.
  on('user-questions/request', (request, next) => {
    try {
      const state = stateFor(request?.agent?.session)
      const turn = state?.current
      if (state && turn && !turn.closed) {
        const first = Array.isArray(request?.questions) ? request.questions[0] : null
        const prompt = firstLine(first?.question, 200) || 'DSH is asking a question'
        enqueue(state, async () => {
          if (!(await ensureSession(state, turn))) return
          await client.reportEvent(turn.csdId, {
            event_type: 'awaiting_user',
            message: cap(`question: ${prompt}`, MAX_MESSAGE),
            event_key: keyOf('dsh', state.id, turn.number, 'question', first?.id ?? 'first'),
            occurred_at: new Date(now()).toISOString(),
            payload: { turn: turn.number },
          })
        })
      }
    } catch (error) {
      warn(`dsh-csd-reporter: ${error?.message ?? error}`)
    }
    return typeof next === 'function' ? next() : undefined
  })

  function recoverOpenTurns() {
    let sessions = []
    try {
      sessions = ctx?.sessions?.list?.() ?? []
    } catch (error) {
      warn(`dsh-csd-reporter: could not read live sessions on load: ${error?.message ?? error}`)
      return
    }
    for (const session of sessions) {
      const state = stateFor(session)
      if (!state || state.current) continue
      let open = null
      try {
        open = openTurnOf(session.events)
      } catch {
        continue
      }
      if (!open || !Number.isFinite(open.number)) continue
      const turn = newTurn(open.number)
      turn.firstPromptSeq = open.firstPromptSeq
      turn.title = open.title
      state.current = turn
      enqueue(state, () => startTurn(state, turn))
    }
  }

  // The machine appears before its first turn, the same reason `csd-agent` reports the host
  // before it looks for sessions: "did my host connect?" needs an answer that does not involve
  // SSH. No goodbye is sent on dispose — a plugin reload or a DSH restart is not an uninstall,
  // and silence is honestly rendered as "maybe offline" (design.md §14, §20).
  recoverOpenTurns()
  void reportHost()
  timer(() => void reportHost(), config.hostHeartbeatMinutes)

  handle.flush = async () => {
    await Promise.allSettled([...states.values()].map((state) => state.chain))
  }

  handle.dispose = async () => {
    if (disposed) return
    disposed = true
    for (const value of [...timers]) stopTimer(value)
    timers.clear()
    await settleBriefly(
      [...states.values()].map((state) =>
        enqueue(state, () =>
          closeOpenTurn(state, state.current, 'DSH shut down before the turn reported an end'),
        ),
      ),
    )
    states.clear()
    syncSessionMap()
  }

  if (typeof ctx?.effect === 'function') {
    // Cordis disposes the fiber on unload and on SIGINT/SIGTERM, so this is the one place a
    // mid-turn shutdown closes its rows instead of leaving them at `running`.
    ctx.effect(() => () => handle.dispose())
  }

  return handle
}

/** Cordis entry point: the Loader row's `config` arrives as the second argument. */
export function apply(ctx, config) {
  install(ctx, config)
}

export default { name, apply, install }
