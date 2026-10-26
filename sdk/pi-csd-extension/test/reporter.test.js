/**
 * The Pi reporter, driven without a Pi process or a network.
 *
 * Everything here is either a pure translation or a fake `ExtensionAPI` emitting the events Pi
 * emits in the order its lifecycle diagram documents. That is the whole contract: a run opens on
 * `before_agent_start`, closes on `agent_settled`, and reports only what Pi recorded — no event
 * counting, no inference.
 */

import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import {
  assistantText,
  firstLine,
  install,
  outcomeFor,
  projectName,
  readEnvFile,
  resolveConfig,
} from '../csd-reporter.js'

const QUIET = { warn() {}, info() {}, log() {} }
const CONFIG = {
  baseUrl: 'http://dashboard',
  token: 'test-token',
  envFile: false,
  host: 'box',
  reportHost: false,
  runHeartbeatMinutes: 0,
  hostHeartbeatMinutes: 0,
  retries: 0,
}

function fakePi() {
  const handlers = new Map()
  return {
    handlers,
    on(name, handler) {
      const list = handlers.get(name) ?? []
      list.push(handler)
      handlers.set(name, list)
    },
    async emit(name, ...args) {
      for (const handler of handlers.get(name) ?? []) await handler(...args)
    },
  }
}

class FakeClient {
  constructor() {
    this.calls = []
    this.created = 0
    this.failCreates = 0
  }

  async createSession(body) {
    this.calls.push({ kind: 'create', body })
    if (this.failCreates > 0) {
      this.failCreates -= 1
      throw new Error('dashboard is down')
    }
    this.created += 1
    return { id: `csd-${this.created}` }
  }

  async updateSession(id, body) {
    this.calls.push({ kind: 'update', id, body })
  }

  async reportEvent(id, body) {
    this.calls.push({ kind: 'event', id, body })
  }

  async reportHost(body) {
    this.calls.push({ kind: 'host', body })
  }

  of(kind) {
    return this.calls.filter((call) => call.kind === kind)
  }
}

function setup(overrides = {}, deps = {}) {
  const pi = fakePi()
  const client = new FakeClient()
  const handle = install(pi, {
    client,
    env: {},
    home: '/home/pi',
    logger: QUIET,
    config: { ...CONFIG, ...overrides },
    ...deps,
  })
  return { pi, client, handle }
}

const CTX = { cwd: '/work/demo' }
const PROMPT = { prompt: 'Fix the flaky test', images: [] }

/** An assistant message as Pi records it at `message_end` / `turn_end`. */
function assistant(stopReason, errorMessage) {
  return { role: 'assistant', content: [], stopReason, ...(errorMessage ? { errorMessage } : {}) }
}

// --------------------------------------------------------------------------- #
// pure translations
// --------------------------------------------------------------------------- #

test('firstLine takes the first presentable line and keeps the ellipsis inside the limit', () => {
  assert.equal(firstLine('hello'), 'hello')
  assert.equal(firstLine('\n\n  spaced   out  \nsecond'), 'spaced out')
  assert.equal(firstLine(undefined), '')
  const long = firstLine('x'.repeat(100), 10)
  assert.equal(long.length, 10)
  assert.ok(long.endsWith('…'))
})

test('assistantText keeps prose and drops thinking and tool blocks', () => {
  assert.equal(
    assistantText({
      role: 'assistant',
      content: [
        { type: 'thinking', text: 'hmm' },
        { type: 'text', text: 'Now I will fix it' },
        { type: 'toolCall', name: 'edit' },
      ],
    }),
    'Now I will fix it',
  )
  assert.equal(assistantText({ role: 'assistant', content: [] }), '')
  assert.equal(assistantText({ role: 'user', content: [{ type: 'text', text: 'hi' }] }), '')
})

test('projectName is the last segment, whichever separator the host used', () => {
  assert.equal(projectName('/work/demo'), 'demo')
  assert.equal(projectName('C:\\\\Users\\\\pi\\\\demo'), 'demo')
  assert.equal(projectName('/work/demo/'), 'demo')
  assert.equal(projectName(''), 'pi')
})

test('outcomeFor quotes Pi’s own stop reason and never guesses completed', () => {
  assert.deepEqual(outcomeFor('stop'), { status: 'completed', summary: 'Pi run completed' })
  assert.equal(outcomeFor('toolUse').status, 'completed')
  assert.equal(outcomeFor('deferred').status, 'completed')
  assert.deepEqual(outcomeFor('length'), {
    status: 'completed',
    summary: 'Pi run completed at the output-token ceiling',
  })
  assert.equal(outcomeFor('aborted').status, 'interrupted')

  const failed = outcomeFor('error', 'provider said no')
  assert.equal(failed.status, 'failed')
  assert.match(failed.summary, /provider said no/)

  assert.equal(outcomeFor('pending').status, 'unknown')
  assert.equal(outcomeFor(undefined).status, 'unknown')
  assert.equal(outcomeFor('something-new').status, 'unknown')
  assert.match(outcomeFor('something-new').summary, /something-new/)
})

test('readEnvFile parses KEY=value and ignores comments and blanks', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pi-csd-env-'))
  const path = join(dir, '.csd-env')
  writeFileSync(
    path,
    '# written by csd-agent\n\nCSD_BASE_URL=http://127.0.0.1:8780\nCSD_INGEST_TOKEN = tok \nnot a pair\n',
    'utf8',
  )

  assert.deepEqual(readEnvFile(path), {
    CSD_BASE_URL: 'http://127.0.0.1:8780',
    CSD_INGEST_TOKEN: 'tok',
  })
  assert.deepEqual(readEnvFile('/definitely/not/here'), {})
})

// --------------------------------------------------------------------------- #
// configuration
// --------------------------------------------------------------------------- #

test('config precedence is environment, then ~/.csd/env, then the default', () => {
  const file = { CSD_BASE_URL: 'http://from-file', CSD_INGEST_TOKEN: 'file-token' }

  const fromEnv = resolveConfig({
    config: { envFile: false },
    env: { CSD_BASE_URL: 'http://from-env', CSD_INGEST_TOKEN: 'env-token' },
    home: '/home/pi',
  })
  assert.equal(fromEnv.baseUrl, 'http://from-env')
  assert.equal(fromEnv.token, 'env-token')

  const fromFile = resolveConfig({
    config: { envFile: '/home/pi/.csd/env' },
    env: {},
    readEnv: () => file,
    home: '/home/pi',
  })
  assert.equal(fromFile.baseUrl, 'http://from-file')
  assert.equal(fromFile.token, 'file-token')

  const defaults = resolveConfig({ config: { envFile: false }, env: {}, hostname: () => 'box' })
  assert.equal(defaults.baseUrl, 'http://127.0.0.1:8780')
  assert.equal(defaults.token, '')
  assert.equal(defaults.agentType, 'pi')
  assert.equal(defaults.host, 'box')
  assert.equal(defaults.disabled, false)
})

test('CSD_PI_ENV_FILE names another env file', () => {
  const seen = []
  const config = resolveConfig({
    env: { CSD_PI_ENV_FILE: '/etc/csd/custom.env' },
    readEnv: (path) => {
      seen.push(path)
      return {}
    },
    home: '/home/pi',
  })
  assert.equal(config.envFile, '/etc/csd/custom.env')
  assert.deepEqual(seen, ['/etc/csd/custom.env'])
})

test('two switches make the reporter inert: a dashboard-owned child, and the operator’s no', () => {
  assert.equal(resolveConfig({ config: { envFile: false }, env: { CSD_AGENT: '1' } }).disabled, true)
  assert.equal(
    resolveConfig({
      config: { envFile: false },
      env: { CSD_AGENT: '1', CSD_AGENT_PARENT: String(process.ppid) },
    }).disabled,
    true,
    'the direct child the backend spawned is the Agent tab talking to itself',
  )
  assert.equal(
    resolveConfig({
      config: { envFile: false },
      env: { CSD_AGENT: '1', CSD_AGENT_PARENT: String(process.ppid + 1) },
    }).disabled,
    false,
    'a pi run the personal agent spawns inherits the marker but not the parent — it reports',
  )
  assert.equal(
    resolveConfig({ config: { envFile: false }, env: { CSD_PI_REPORTER: '0' } }).disabled,
    true,
  )
  assert.equal(resolveConfig({ config: { envFile: false }, env: {} }).disabled, false)
})

test('a coding run spawned by the personal agent still reports', () => {
  const pi = fakePi()
  const client = new FakeClient()
  install(pi, {
    config: { ...CONFIG },
    client,
    env: { CSD_AGENT: '1', CSD_AGENT_PARENT: String(process.ppid + 1) },
    home: '/home/pi',
    logger: QUIET,
  })

  assert.ok(pi.handlers.size > 0, 'the inherited marker is not enough to silence a grandchild')
})

test('a dashboard-owned Pi child registers nothing and says nothing', () => {
  const pi = fakePi()
  const client = new FakeClient()
  install(pi, {
    config: { ...CONFIG },
    client,
    env: { CSD_AGENT: '1' },
    home: '/home/pi',
    logger: QUIET,
  })

  assert.equal(pi.handlers.size, 0)
  assert.equal(client.calls.length, 0)
})

test('with no token the reporter is inert, warns once, and does not throw', () => {
  const pi = fakePi()
  const client = new FakeClient()
  const warnings = []
  install(pi, {
    config: { ...CONFIG, token: '' },
    client,
    env: {},
    home: '/home/pi',
    logger: { warn: (m) => warnings.push(m), log() {} },
  })

  assert.equal(pi.handlers.size, 0)
  assert.equal(client.calls.length, 0)
  assert.match(warnings[0], /no ingest token/)
})

// --------------------------------------------------------------------------- #
// one run, one session
// --------------------------------------------------------------------------- #

test('a prompt opens a session with the harness, project, workspace, host and prompt title', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await handle.flush()

  const [create] = client.of('create')
  assert.ok(create)
  assert.equal(create.body.agent_type, 'pi')
  assert.equal(create.body.title, 'Fix the flaky test')
  assert.equal(create.body.project_name, 'demo')
  assert.equal(create.body.workspace, '/work/demo')
  assert.equal(create.body.host, 'box')
  assert.equal(create.body.status, 'running')
  assert.equal(create.body.started_at, undefined, 'the server stamps a live report')
})

test('a blank prompt falls back to the project name', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', { prompt: '  \n ' }, CTX)
  await handle.flush()
  assert.equal(client.of('create')[0].body.title, 'Pi · demo')
})

test('a queued follow-up prompt does not open a second row', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await handle.flush()
  await pi.emit('before_agent_start', { prompt: 'and then summarize' }, CTX)
  await pi.emit('before_agent_start', { prompt: 'and then commit' }, CTX)
  await handle.flush()

  assert.equal(client.of('create').length, 1)
})

test('turns are progress events with stable keys, numbered from one', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit('turn_end', { turnIndex: 0, message: assistant('toolUse'), toolResults: [] }, CTX)
  await pi.emit('turn_end', { turnIndex: 1, message: assistant('stop'), toolResults: [] }, CTX)
  await handle.flush()

  const events = client.of('event')
  assert.equal(events.length, 2)
  assert.equal(events[0].body.event_type, 'progress')
  assert.equal(events[0].body.message, 'turn 1 complete')
  assert.match(events[0].body.event_key, /^pi:.+:turn:1$/)
  assert.deepEqual(events[0].body.payload.turn, 1)
  assert.equal(events[1].body.message, 'turn 2 complete')
  assert.match(events[1].body.event_key, /:turn:2$/)
})

test('a turn followed by tools reports the agent sentence as a checkpoint', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit(
    'turn_end',
    {
      turnIndex: 0,
      message: {
        role: 'assistant',
        content: [{ type: 'text', text: 'Now I will fix the flaky test' }],
        stopReason: 'toolUse',
      },
      toolResults: [{ toolCallId: 't1' }],
    },
    CTX,
  )
  await handle.flush()

  const events = client.of('event')
  assert.equal(events.length, 1)
  assert.equal(events[0].body.event_type, 'checkpoint')
  assert.equal(events[0].body.message, 'Now I will fix the flaky test')
  assert.match(events[0].body.event_key, /:checkpoint$/)
  assert.equal(events[0].body.payload.source, 'prose')
})

test('after a checkpoint, a tool-only turn does not add a step line', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit(
    'turn_end',
    {
      turnIndex: 0,
      message: {
        role: 'assistant',
        content: [{ type: 'text', text: 'Now I will fix the flaky test' }],
        stopReason: 'toolUse',
      },
      toolResults: [{ toolCallId: 't1' }],
    },
    CTX,
  )
  await pi.emit(
    'turn_end',
    {
      turnIndex: 1,
      message: { role: 'assistant', content: [], stopReason: 'toolUse' },
      toolResults: [{ toolCallId: 't2' }],
    },
    CTX,
  )
  await handle.flush()

  const events = client.of('event')
  assert.equal(events.length, 1, 'the checkpoint is the progress track for this run')
  assert.equal(events[0].body.event_type, 'checkpoint')
})

test('agent_settled closes the run with the last assistant stop reason', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit('turn_end', { turnIndex: 0, message: assistant('stop'), toolResults: [] }, CTX)
  await pi.emit('agent_settled', undefined, CTX)
  await handle.flush()

  assert.deepEqual(client.of('update')[0].body, {
    status: 'completed',
    summary: 'Pi run completed',
  })
})

test('message_end is enough to record the outcome when turn_end is not seen', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit('message_end', { message: assistant('aborted') }, CTX)
  await pi.emit('agent_settled', undefined, CTX)
  await handle.flush()

  assert.equal(client.of('update')[0].body.status, 'interrupted')
})

test('an error stop reason finishes failed and quotes the error message', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit('turn_end', { turnIndex: 0, message: assistant('error', 'rate limited'), toolResults: [] }, CTX)
  await pi.emit('agent_settled', undefined, CTX)
  await handle.flush()

  const update = client.of('update')[0]
  assert.equal(update.body.status, 'failed')
  assert.match(update.body.summary, /rate limited/)
})

test('a run with no stop reason is unknown, not completed', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit('agent_settled', undefined, CTX)
  await handle.flush()

  assert.equal(client.of('update')[0].body.status, 'unknown')
})

test('a second prompt after a settle opens a new row', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit('turn_end', { turnIndex: 0, message: assistant('stop'), toolResults: [] }, CTX)
  await pi.emit('agent_settled', undefined, CTX)
  await handle.flush()
  await pi.emit('before_agent_start', { prompt: 'second task' }, CTX)
  await handle.flush()

  const creates = client.of('create')
  assert.equal(creates.length, 2)
  assert.equal(creates[1].body.title, 'second task')
  assert.deepEqual(client.of('update')[0], {
    kind: 'update',
    id: 'csd-1',
    body: { status: 'completed', summary: 'Pi run completed' },
  })
})

test('the session id of the first run is not overwritten by the second', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit('turn_end', { turnIndex: 0, message: assistant('stop'), toolResults: [] }, CTX)
  // Errored and settled in the same tick, before the queued create could resolve.
  await pi.emit('agent_settled', undefined, CTX)
  await pi.emit('before_agent_start', { prompt: 'second' }, CTX)
  await handle.flush()

  const updates = client.of('update')
  assert.equal(updates[0].id, 'csd-1')
  assert.equal(client.of('event').filter((e) => e.body.event_type === 'progress')[0].id, 'csd-1')
})

test('session_shutdown closes an open run as interrupted and stops the host timer', async () => {
  const intervals = []
  const cleared = []
  const { pi, client, handle } = setup(
    { hostHeartbeatMinutes: 5, reportHost: true },
    {
      setInterval: (fn, ms) => {
        const value = { fn, ms, unref() {} }
        intervals.push(value)
        return value
      },
      clearInterval: (value) => cleared.push(value),
      hostname: () => 'box',
    },
  )
  await pi.emit('session_start', { reason: 'startup' }, CTX)
  assert.equal(intervals.length, 1)
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit('session_shutdown', { reason: 'quit' }, CTX)
  await handle.flush()

  assert.deepEqual(client.of('update')[0].body, {
    status: 'interrupted',
    summary: 'Pi switched or exited before the run settled',
  })
  assert.deepEqual(cleared, intervals)
})

test('a new session closes a leftover run instead of leaving it running', async () => {
  const { pi, client, handle } = setup()
  await pi.emit('before_agent_start', PROMPT, CTX)
  await handle.flush()
  // `/new` without a shutdown is not a lifecycle Pi documents, but a row left `running` is
  // worse than an honest interrupt, so the opener cleans up defensively.
  await pi.emit('session_start', { reason: 'new' }, CTX)
  await handle.flush()

  assert.equal(client.of('update')[0].body.status, 'interrupted')
})

test('a failed create is retried by the next event on the run', async () => {
  const { pi, client, handle } = setup()
  client.failCreates = 1
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit('turn_end', { turnIndex: 0, message: assistant('stop'), toolResults: [] }, CTX)
  await handle.flush()

  assert.equal(client.of('create').length, 2)
  const events = client.of('event')
  assert.equal(events.length, 1)
  assert.equal(events[0].id, 'csd-1')
})

test('a create that never succeeds leaves no orphan patch', async () => {
  const { pi, client, handle } = setup()
  client.failCreates = 99
  await pi.emit('before_agent_start', PROMPT, CTX)
  await pi.emit('agent_settled', undefined, CTX)
  await handle.flush()

  assert.equal(client.of('update').length, 0)
})

// --------------------------------------------------------------------------- #
// the host row
// --------------------------------------------------------------------------- #

test('the host is reported at session start and never declared driveable', async () => {
  const { pi, client } = setup({ reportHost: true, host: 'lab-box' })
  await pi.emit('session_start', { reason: 'startup' }, CTX)
  await new Promise((resolve) => setImmediate(resolve))

  const [host] = client.of('host')
  assert.ok(host)
  assert.equal(host.body.label, 'lab-box')
  assert.equal(host.body.agent_type, 'pi')
  assert.equal(host.body.connected, true)
  assert.equal(host.body.wants_access, false)
})

test('shutdown sends no host goodbye: silence is "maybe offline", not a disconnect', async () => {
  const { pi, client } = setup({ reportHost: true })
  await pi.emit('session_start', { reason: 'startup' }, CTX)
  await pi.emit('session_shutdown', { reason: 'quit' }, CTX)
  await new Promise((resolve) => setImmediate(resolve))

  const disconnects = client.of('host').filter((call) => call.body.connected === false)
  assert.equal(disconnects.length, 0)
})

test('the run heartbeat renews liveness with a bucketed key', async () => {
  const ticks = []
  const { pi, client, handle } = setup(
    { runHeartbeatMinutes: 5 },
    {
      now: () => 60_000 * 100,
      setInterval: (fn) => {
        ticks.push(fn)
        return { unref() {} }
      },
      clearInterval: () => {},
    },
  )
  await pi.emit('before_agent_start', PROMPT, CTX)
  await handle.flush()
  ticks[0]()
  await handle.flush()
  ticks[0]()
  await handle.flush()

  const beats = client.of('event').filter((call) => call.body.event_type === 'heartbeat')
  assert.equal(beats.length, 2, 'the second beat is sent, and deduped by its key on the server')
  assert.match(beats[0].body.event_key, /:hb:\d+$/)
  assert.equal(beats[0].body.event_key, beats[1].body.event_key)
})

// --------------------------------------------------------------------------- #
// the factory
// --------------------------------------------------------------------------- #

test('the default export installs from a bare ExtensionAPI', async () => {
  const { default: factory } = await import('../csd-reporter.js')
  const pi = fakePi()
  // The factory reads the process environment, and this machine may really be reporting to a
  // dashboard; the opt-out is the only injection point a default export has.
  const previous = process.env.CSD_PI_REPORTER
  process.env.CSD_PI_REPORTER = '0'
  try {
    assert.doesNotThrow(() => factory(pi))
  } finally {
    if (previous === undefined) delete process.env.CSD_PI_REPORTER
    else process.env.CSD_PI_REPORTER = previous
  }
  // A plain object with `on` is a valid ExtensionAPI, and the opted-out factory registers
  // nothing at all.
  assert.equal(pi.handlers.size, 0)
})

