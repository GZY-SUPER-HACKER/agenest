/**
 * Where the reporter finds the dashboard, and who it says it is.
 *
 * Precedence: the plugin's config row > the process environment > `~/.csd/env` > the
 * built-in default. `~/.csd/env` is read because a machine that already runs `csd-agent` for
 * Claude Code keeps `CSD_BASE_URL` and `CSD_INGEST_TOKEN` there in a 0600 file — asking the
 * operator to copy the secret somewhere else is how two copies of it start to drift. A machine
 * with no `csd-agent` sets two environment variables instead; nothing in this package needs to
 * be given the token at install time.
 *
 * `envFile: false` disables that fallback, which is what the tests and a deliberately
 * environment-only deployment use.
 */

import { readFileSync } from 'node:fs'
import { homedir, hostname } from 'node:os'
import { join } from 'node:path'

/** The dashboard's own default; `csd-agent`'s `--local` install talks to the same port. */
export const DEFAULT_BASE_URL = 'http://127.0.0.1:8780'

/**
 * What the rail shows as the harness. Deliberately stable and short: it is half of a host's
 * identity, `(label, agent_type)`, so renaming it would look like a second machine.
 */
export const DEFAULT_AGENT_TYPE = 'dsh'

/**
 * Parse one `KEY=value` per line, ignoring comments and blank lines.
 *
 * Not `source`d: this file is read by a long-lived Node process, and shell quoting rules are
 * not a thing Node can reproduce without inventing a second dialect for the same file.
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
 * Resolve the effective configuration. Never throws: a reporter that cannot read its own
 * config must not stop DSH from booting.
 *
 * @param {object} raw the Loader row's `config` object, possibly empty.
 * @param {{env?: object, readEnv?: (path: string) => object, home?: string}} [deps]
 */
export function resolveConfig(raw = {}, deps = {}) {
  const config = raw && typeof raw === 'object' ? raw : {}
  const env = deps.env ?? process.env
  const readEnv = deps.readEnv ?? readEnvFile
  const home = deps.home ?? homedir()

  const envFile =
    config.envFile === false || config.envFile === ''
      ? ''
      : pick(config.envFile, env.CSD_ENV_FILE) || join(home, '.csd', 'env')
  const fromFile = envFile ? readEnv(envFile) : {}

  // `host: false` (or an empty string) is the supported way to say "do not report a machine":
  // a session with no host is grouped under "no host recorded", which is better than a
  // manufactured one. `pick` skips empty strings, so it is handled before the defaults.
  const host =
    config.host === false || config.host === ''
      ? ''
      : pick(config.host, env.CSD_HOST_LABEL) || hostname()

  return {
    baseUrl: (pick(config.baseUrl, env.CSD_BASE_URL, fromFile.CSD_BASE_URL) || DEFAULT_BASE_URL).replace(/\/+$/, ''),
    token: pick(config.token, env.CSD_INGEST_TOKEN, fromFile.CSD_INGEST_TOKEN),
    envFile,
    // The API caps both at 64 (MAX_AGENT_TYPE / MAX_HOST); a longer hostname would otherwise
    // turn every report into a 422.
    agentType: (pick(config.agentType, env.CSD_AGENT_TYPE) || DEFAULT_AGENT_TYPE).slice(0, 64),
    host: host.slice(0, 64),
    reportHost: config.reportHost !== false,
    // Both are in minutes, and 0 disables the timer. The turn heartbeat exists only to keep a
    // long model step from being rendered as "maybe offline" (design.md §8); it renews
    // liveness, never activity.
    turnHeartbeatMinutes: positiveNumber(config.turnHeartbeatMinutes, 5),
    hostHeartbeatMinutes: positiveNumber(config.hostHeartbeatMinutes, 5),
    // Seconds, the same unit `cpctl` uses (`CSD_TIMEOUT`), converted once at the seam.
    timeoutSeconds: Math.max(0.1, positiveNumber(config.timeoutSeconds, 3)),
    retries: Math.floor(positiveNumber(config.retries, 3)),
  }
}
