import assert from 'node:assert/strict'
import { test } from 'node:test'

import { autoKey, firstLine, titleFromContent, turnOutcome } from '../lib/reporter.js'

test('autoKey is deterministic and distinguishes its parts', () => {
  assert.equal(autoKey('a', 'b'), autoKey('a', 'b'))
  assert.notEqual(autoKey('a', 'b'), autoKey('a', 'c'))
  assert.match(autoKey('a', 'b'), /^auto-[0-9a-f]{16}$/)
})

test('titleFromContent uses the first non-empty line and collapses spacing', () => {
  assert.equal(
    titleFromContent([
      { type: 'text', text: '\n\n   Fix   the   flaky test  \nand then the rest' },
      { type: 'text', text: 'ignored' },
    ]),
    'Fix the flaky test',
  )
  assert.equal(titleFromContent([{ type: 'image' }]), '')
  assert.equal(titleFromContent(undefined), '')
})

test('titleFromContent caps at a phone-readable length', () => {
  const title = titleFromContent([{ type: 'text', text: 'x'.repeat(200) }], 20)
  assert.equal(title.length, 20)
  assert.ok(title.endsWith('…'))
})

test('firstLine returns empty for blank text', () => {
  assert.equal(firstLine('   \n\t\n'), '')
  assert.equal(firstLine(undefined), '')
})

test('a completed turn maps to completed', () => {
  assert.deepEqual(turnOutcome({ kind: 'completed' }), {
    status: 'completed',
    summary: 'DSH turn completed',
  })
})

test('an error turn keeps DSH’s message and code', () => {
  const outcome = turnOutcome({
    kind: 'error',
    error: { code: 'PROVIDER_UNAVAILABLE', message: 'upstream said no' },
  })
  assert.equal(outcome.status, 'failed')
  assert.match(outcome.summary, /PROVIDER_UNAVAILABLE/)
  assert.match(outcome.summary, /upstream said no/)
})

test('an aborted turn is interrupted, and a hook cause is named', () => {
  const outcome = turnOutcome({ kind: 'aborted', reason: { kind: 'hook', reason: 'policy' } })
  assert.equal(outcome.status, 'interrupted')
  assert.match(outcome.summary, /hook: policy/)
})

test('blocked and crash-orphaned turns are interrupted, not failed', () => {
  assert.equal(turnOutcome({ kind: 'blocked' }).status, 'interrupted')
  assert.equal(turnOutcome({ kind: 'interrupted' }).status, 'interrupted')
})

test('max-tokens still completed: the turn finished, the last step was capped', () => {
  assert.equal(turnOutcome({ kind: 'max-tokens' }).status, 'completed')
})

test('an unrecognized reason is reported as unknown, never guessed', () => {
  const outcome = turnOutcome({ kind: 'killed-by-a-newer-plugin' })
  assert.equal(outcome.status, 'unknown')
  assert.match(outcome.summary, /killed-by-a-newer-plugin/)
  assert.equal(turnOutcome(undefined).status, 'unknown')
})
