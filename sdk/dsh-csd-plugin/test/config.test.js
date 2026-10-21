import assert from 'node:assert/strict'
import { test } from 'node:test'

import { resolveConfig, DEFAULT_BASE_URL, DEFAULT_AGENT_TYPE } from '../lib/config.js'

test('explicit config wins over the environment and the env file', () => {
  const config = resolveConfig(
    { baseUrl: 'http://dash:9000', token: 'from-config', agentType: 'dsh-dev', host: 'box' },
    { env: { CSD_BASE_URL: 'http://env', CSD_INGEST_TOKEN: 'from-env' }, envFile: false },
  )
  assert.equal(config.baseUrl, 'http://dash:9000')
  assert.equal(config.token, 'from-config')
  assert.equal(config.agentType, 'dsh-dev')
  assert.equal(config.host, 'box')
})

test('the environment wins over ~/.csd/env, which fills only gaps', () => {
  const config = resolveConfig(
    {},
    {
      env: { CSD_BASE_URL: 'http://env:8780', CSD_INGEST_TOKEN: 'from-env' },
      readEnv: () => ({ CSD_BASE_URL: 'http://file:1', CSD_INGEST_TOKEN: 'from-file' }),
    },
  )
  assert.equal(config.baseUrl, 'http://env:8780')
  assert.equal(config.token, 'from-env')
})

test('~/.csd/env is the fallback a csd-agent install already provides', () => {
  const config = resolveConfig(
    {},
    {
      env: {},
      readEnv: (path) => {
        assert.match(path, /\.csd[\\/]env$/)
        return { CSD_BASE_URL: 'http://file:18781', CSD_INGEST_TOKEN: 'from-file' }
      },
    },
  )
  assert.equal(config.baseUrl, 'http://file:18781')
  assert.equal(config.token, 'from-file')
})

test('defaults resolve when nothing is configured; the token stays empty', () => {
  // `envFile` is plugin config, not a test seam: `resolveConfig` would otherwise read the
  // developer's own ~/.csd/env and fail on a machine that has one.
  const config = resolveConfig({ envFile: false }, { env: {} })
  assert.equal(config.baseUrl, DEFAULT_BASE_URL)
  assert.equal(config.token, '')
  assert.equal(config.agentType, DEFAULT_AGENT_TYPE)
  assert.ok(config.host.length > 0)
})

test('envFile: false disables the file fallback', () => {
  let read = false
  const config = resolveConfig(
    { envFile: false },
    {
      env: {},
      readEnv: () => {
        read = true
        return { CSD_INGEST_TOKEN: 'from-file' }
      },
    },
  )
  assert.equal(read, false)
  assert.equal(config.token, '')
})

test('host: false means "do not report a machine"', () => {
  const config = resolveConfig({ host: false, envFile: false }, { env: {} })
  assert.equal(config.host, '')
})

test('readEnvFile ignores comments and malformed lines', async () => {
  const { readEnvFile } = await import('../lib/config.js')
  const { mkdtempSync, writeFileSync } = await import('node:fs')
  const { tmpdir } = await import('node:os')
  const { join } = await import('node:path')
  const dir = mkdtempSync(join(tmpdir(), 'dsh-csd-'))
  const path = join(dir, 'env')
  writeFileSync(path, '# comment\n\nCSD_INGEST_TOKEN=abc\nnonsense\nCSD_BASE_URL=http://x\n')
  assert.deepEqual(readEnvFile(path), { CSD_INGEST_TOKEN: 'abc', CSD_BASE_URL: 'http://x' })
})
