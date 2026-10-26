import assert from 'node:assert/strict'
import { test } from 'node:test'

import { install, openTurnOf } from '../lib/index.js'

const QUIET = { warn() {}, info() {}, log() {} }
const CONFIG = {
  baseUrl: 'http://dashboard',
  token: 'test-token',
  envFile: false,
  host: 'box',
  reportHost: false,
  turnHeartbeatMinutes: 0,
  hostHeartbeatMinutes: 0,
  retries: 0,
}

function fakeContext() {
  const handlers = new Map()
  const effects = []
  return {
    handlers,
    effects,
    on(name, handler) {
      const list = handlers.get(name) ?? []
      list.push(handler)
      handlers.set(name, list)
    },
    emit(name, ...args) {
      for (const handler of handlers.get(name) ?? []) handler(...args)
    },
    effect(fn) {
      effects.push(fn())
    },
  }
}

class FakeClient {
  constructor() {
    this.calls = []
    this.created = 0
  }

  async createSession(body) {
    this.calls.push({ kind: 'create', body })
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

function setup(overrides = {}) {
  const ctx = fakeContext()
  const client = new FakeClient()
  const handle = install(ctx, { ...CONFIG, ...overrides }, { client, env: {}, logger: QUIET })
  return { ctx, client, handle }
}

let seq = 0
function event(type, data, time = Date.now()) {
  seq += 1
  return { type, data, seq, time }
}

const SESSION = { id: 'dsh-1', header: { cwd: '/work/demo' } }

test('openTurnOf finds the turn a log leaves open, with its prompt and generated title', () => {
  assert.equal(openTurnOf([]), null)
  assert.deepEqual(
    openTurnOf([
      { type: 'turn/start', seq: 1, data: { turn: 3 } },
      { type: 'user/message', seq: 2, data: { source: { kind: 'user' }, content: [{ type: 'text', text: 'do it' }] } },
      { type: 'session/title', seq: 3, data: { title: 'A title', messageSeqs: [2] } },
      { type: 'step/end', seq: 4, data: { turn: 3, step: 1 } },
    ]),
    { number: 3, firstPromptSeq: 2, title: 'A title' },
  )
  assert.equal(
    openTurnOf([
      { type: 'turn/start', seq: 1, data: { turn: 3 } },
      { type: 'turn/end', seq: 2, data: { turn: 3, reason: { kind: 'completed' } } },
    ]),
    null,
  )
})

test('mounting during a turn recovers it from the durable log', async () => {
  const ctx = fakeContext()
  const restored = {
    id: 'dsh-restored',
    header: { cwd: '/work/demo' },
    events: [
      { type: 'turn/start', seq: 1, data: { turn: 4 } },
      {
        type: 'user/message',
        seq: 2,
        data: { source: { kind: 'user' }, content: [{ type: 'text', text: 'recovered prompt' }] },
      },
    ],
  }
  ctx.sessions = { list: () => [restored] }
  const client = new FakeClient()
  const handle = install(ctx, { ...CONFIG }, { client, env: {}, logger: QUIET })
  await handle.flush()

  assert.equal(client.of('create')[0].body.title, 'recovered prompt')

  // The turn's eventual end still closes the row this instance did not open.
  ctx.emit('session/event', restored, event('turn/end', { turn: 4, reason: { kind: 'completed' } }))
  await handle.flush()
  assert.equal(client.of('update')[0].body.status, 'completed')
})

test('a turn opens a dashboard session with the harness, project, workspace and host', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  await handle.flush()

  const [create] = client.of('create')
  assert.ok(create)
  assert.equal(create.body.agent_type, 'dsh')
  assert.equal(create.body.title, 'DSH · demo')
  assert.equal(create.body.project_name, 'demo')
  assert.equal(create.body.workspace, '/work/demo')
  assert.equal(create.body.host, 'box')
  assert.equal(create.body.status, 'running')
  assert.equal(create.body.started_at, undefined, 'the server stamps a live report')
})

test('the first human prompt renames the turn; synthetic context does not', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit(
    'session/event',
    SESSION,
    event('user/message', {
      source: { kind: 'plugin', plugin: 'skill' },
      content: [{ type: 'text', text: 'synthetic context' }],
    }),
  )
  ctx.emit(
    'session/event',
    SESSION,
    event('user/message', {
      source: { kind: 'user' },
      content: [{ type: 'text', text: 'Fix the\nflaky test' }],
    }),
  )
  await handle.flush()

  const updates = client.of('update')
  assert.equal(updates.length, 1)
  assert.equal(updates[0].id, 'csd-1')
  assert.deepEqual(updates[0].body, { title: 'Fix the' })
})

test('steps become progress events with stable keys', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit('session/event', SESSION, event('step/end', { turn: 1, step: 2 }))
  await handle.flush()

  const [step] = client.of('event')
  assert.equal(step.id, 'csd-1')
  assert.equal(step.body.event_type, 'progress')
  assert.equal(step.body.message, 'step 2 complete')
  assert.match(step.body.event_key, /:1:step:2$/)
  assert.deepEqual(step.body.payload, { turn: 1, step: 2 })
})

test('an assistant message followed by tools is a checkpoint, and its step is not repeated', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit('session/event', SESSION, event('step/start', { turn: 1, step: 1 }))
  ctx.emit(
    'session/event',
    SESSION,
    event('assistant/message', {
      turn: 1,
      step: 1,
      message: {
        id: 'm-1',
        role: 'assistant',
        content: [
          { type: 'text', text: 'Now I will add the cursor test' },
          { type: 'tool-call', id: 't-1', name: 'edit', arguments: '{}' },
        ],
      },
    }),
  )
  ctx.emit('session/event', SESSION, event('step/end', { turn: 1, step: 1 }))
  ctx.emit('session/event', SESSION, event('step/end', { turn: 1, step: 2 }))
  await handle.flush()

  const events = client.of('event')
  assert.equal(events.length, 1, 'the checkpoint is the progress track for this turn')
  assert.equal(events[0].body.event_type, 'checkpoint')
  assert.equal(events[0].body.message, 'Now I will add the cursor test')
  assert.equal(events[0].body.payload.source, 'prose')
  assert.equal(events[0].body.payload.message_id, 'm-1')
  assert.match(events[0].body.event_key, /:1:message:m-1$/)
})

test('a text-only assistant message is the answer, not a checkpoint', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit(
    'session/event',
    SESSION,
    event('assistant/message', {
      turn: 1,
      step: 1,
      message: {
        id: 'm-2',
        role: 'assistant',
        content: [{ type: 'text', text: 'All tests pass' }],
      },
    }),
  )
  await handle.flush()

  assert.equal(client.of('event').length, 0)
})

test('an interrupted assistant prefix is reported as a checkpoint', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit(
    'session/event',
    SESSION,
    event('assistant/message', {
      turn: 1,
      step: 1,
      interrupted: true,
      message: {
        id: 'm-3',
        role: 'assistant',
        content: [{ type: 'text', text: 'I was in the middle of' }],
      },
    }),
  )
  await handle.flush()

  const [checkpoint] = client.of('event')
  assert.equal(checkpoint.body.event_type, 'checkpoint')
  assert.equal(checkpoint.body.payload.interrupted, true)
})

test('todo/write becomes a plan checkpoint with the whole list', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit(
    'session/event',
    SESSION,
    event('todo/write', {
      todos: [
        { content: 'Read the broker', status: 'completed' },
        { content: 'Write the test', status: 'in_progress' },
        { content: 'Run the suite', status: 'pending' },
      ],
    }),
  )
  await handle.flush()

  const [plan] = client.of('event')
  assert.equal(plan.body.event_type, 'checkpoint')
  assert.equal(plan.body.message, 'Plan 1/3 · Write the test')
  assert.deepEqual(plan.body.payload, {
    source: 'plan',
    turn: 1,
    plan: [
      { title: 'Read the broker', status: 'completed' },
      { title: 'Write the test', status: 'in_progress' },
      { title: 'Run the suite', status: 'pending' },
    ],
  })
})

test('after a plan snapshot, step/end stays silent', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit(
    'session/event',
    SESSION,
    event('todo/write', {
      todos: [
        { content: 'Read the broker', status: 'completed' },
        { content: 'Write the test', status: 'in_progress' },
      ],
    }),
  )
  ctx.emit('session/event', SESSION, event('step/end', { turn: 1, step: 1 }))
  await handle.flush()

  const events = client.of('event')
  assert.equal(events.length, 1)
  assert.equal(events[0].body.payload.source, 'plan')
})

test('an approval is awaiting_user, and its decision clears it with progress', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit(
    'session/event',
    SESSION,
    event('approval/asked', { id: 'ap-1', toolName: 'bash', reason: 'writes outside the tree' }),
  )
  ctx.emit('session/event', SESSION, event('approval/decided', { id: 'ap-1', outcome: 'allowed-once' }))
  await handle.flush()

  const events = client.of('event')
  assert.equal(events.length, 2)
  assert.equal(events[0].body.event_type, 'awaiting_user')
  assert.match(events[0].body.message, /bash/)
  assert.match(events[0].body.message, /writes outside the tree/)
  assert.equal(events[1].body.event_type, 'progress')
  assert.equal(events[1].body.message, 'approval allowed-once')
})

test('a user question is observed by delegating to the next listener', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  let delegated = false
  ctx.emit(
    'user-questions/request',
    {
      agent: { session: SESSION },
      questions: [{ id: 'q-1', question: 'Which branch?' }],
    },
    () => {
      delegated = true
      return Promise.resolve({ answers: [] })
    },
  )
  await handle.flush()

  assert.equal(delegated, true)
  const [question] = client.of('event')
  assert.equal(question.body.event_type, 'awaiting_user')
  assert.match(question.body.message, /Which branch\?/)
})

test('turn/end carries DSH’s recorded outcome onto the session', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit('session/event', SESSION, event('turn/end', { turn: 1, reason: { kind: 'completed' } }))
  await handle.flush()

  assert.deepEqual(client.of('update')[0].body, {
    status: 'completed',
    summary: 'DSH turn completed',
  })
})

test('an error turn finishes failed and quotes the provider code', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit(
    'session/event',
    SESSION,
    event('turn/end', {
      turn: 1,
      reason: { kind: 'error', error: { code: 'RATE_LIMIT', message: 'slow down' } },
    }),
  )
  await handle.flush()

  const update = client.of('update')[0]
  assert.equal(update.body.status, 'failed')
  assert.match(update.body.summary, /RATE_LIMIT/)
})

test('an unpaired new turn closes the previous row instead of leaving it running', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  await handle.flush()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 2 }))
  await handle.flush()

  assert.deepEqual(client.of('update')[0].body, {
    status: 'interrupted',
    summary: 'DSH opened a new turn before the previous one ended',
  })
  assert.equal(client.of('create').length, 2)
})

test('a turn that starts before the previous completion drained still closes the right session', async () => {  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit('session/event', SESSION, event('turn/end', { turn: 1, reason: { kind: 'completed' } }))
  // Emitted in the same tick, before the queued completion could run: the shared-mutable
  // version of this state machine lost the first turn here.
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 2 }))
  await handle.flush()

  const creates = client.of('create')
  assert.equal(creates.length, 2)
  assert.deepEqual(client.of('update')[0], {
    kind: 'update',
    id: 'csd-1',
    body: { status: 'completed', summary: 'DSH turn completed' },
  })

  ctx.emit(
    'session/event',
    SESSION,
    event('user/message', { source: { kind: 'user' }, content: [{ type: 'text', text: 'second' }] }),
  )
  await handle.flush()
  assert.deepEqual(client.of('update')[1].id, 'csd-2')
})

test('a title derived from an earlier turn does not rename the current one', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit(
    'session/event',
    SESSION,
    event('user/message', { source: { kind: 'user' }, content: [{ type: 'text', text: 'first' }] }, 1),
  )
  await handle.flush()
  assert.equal(client.of('update')[0].body.title, 'first')

  // End turn 1 cleanly, then open turn 2 and let turn 1's title arrive late.
  ctx.emit('session/event', SESSION, event('turn/end', { turn: 1, reason: { kind: 'completed' } }))
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 2 }))
  await handle.flush()
  const before = client.of('update').length
  ctx.emit(
    'session/event',
    SESSION,
    event('session/title', { title: 'Generated from turn one', messageSeqs: [2] }),
  )
  await handle.flush()

  assert.equal(client.of('update').length, before, 'no rename was applied to turn 2')
})

test('a title event for this turn’s own prompt is applied', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  const prompt = event('user/message', {
    source: { kind: 'user' },
    content: [{ type: 'text', text: 'first' }],
  })
  ctx.emit('session/event', SESSION, prompt)
  ctx.emit(
    'session/event',
    SESSION,
    event('session/title', { title: 'A generated title', messageSeqs: [prompt.seq] }),
  )
  await handle.flush()

  const titles = client.of('update').map((call) => call.body.title)
  assert.ok(titles.includes('A generated title'))
})

test('a disposed DSH session closes its open turn as interrupted', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  await handle.flush()
  ctx.emit('session/disposed', SESSION)
  await handle.flush()

  assert.deepEqual(client.of('update')[0].body, {
    status: 'interrupted',
    summary: 'DSH disposed the session before the turn reported an end',
  })
})

test('subagent child sessions are part of the parent turn and are not reported', async () => {
  const { ctx, client, handle } = setup()
  const child = { id: 'dsh-child', header: { cwd: '/work/demo', origin: 'subagent' } }
  ctx.emit('session/event', child, event('turn/start', { turn: 1 }))
  ctx.emit('session/event', child, event('turn/end', { turn: 1, reason: { kind: 'completed' } }))
  await handle.flush()
  assert.equal(client.calls.length, 0)
})

test('a turn with an unrecognized end reason is reported as unknown', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  ctx.emit(
    'session/event',
    SESSION,
    event('turn/end', { turn: 1, reason: { kind: 'added-by-a-plugin' } }),
  )
  await handle.flush()

  assert.equal(client.of('update')[0].body.status, 'unknown')
})

test('with no token the plugin is inert: no listeners, no calls, no boot failure', async () => {
  const ctx = fakeContext()
  const client = new FakeClient()
  const warnings = []
  const handle = install(
    ctx,
    { envFile: false, reportHost: false, turnHeartbeatMinutes: 0, hostHeartbeatMinutes: 0 },
    { client, env: {}, logger: { warn: (m) => warnings.push(m), info() {}, log() {} } },
  )
  await handle.flush()
  assert.equal(ctx.handlers.size, 0)
  assert.equal(client.calls.length, 0)
  assert.match(warnings[0], /no ingest token/)
})

test('the host is reported on install but never declared driveable', async () => {
  const ctx = fakeContext()
  const client = new FakeClient()
  install(
    ctx,
    { ...CONFIG, reportHost: true, host: 'lab-box', hostHeartbeatMinutes: 0 },
    { client, env: {}, logger: QUIET },
  )
  await new Promise((resolve) => setImmediate(resolve))
  const [host] = client.of('host')
  assert.ok(host)
  assert.equal(host.body.label, 'lab-box')
  assert.equal(host.body.agent_type, 'dsh')
  assert.equal(host.body.connected, true)
  assert.equal(host.body.wants_access, false)
})

test('dispose closes an open turn and stops timers', async () => {
  const { ctx, client, handle } = setup()
  ctx.emit('session/event', SESSION, event('turn/start', { turn: 1 }))
  await handle.flush()
  await ctx.effects[0]()
  await handle.flush()

  assert.equal(client.of('update')[0].body.status, 'interrupted')
})
