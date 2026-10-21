/**
 * The HTTP client, and the three translations that decide what the dashboard reads.
 *
 * This is the reporting half of the plugin; `index.js` owns the DSH event wiring. Keeping the
 * translations here is what makes them testable without a running DSH — and they are the part
 * worth testing, because each one is a decision the dashboard renders.
 *
 * The transport mirrors `cpctl` (sdk/cpctl/cpctl/cli.py) rather than inventing a second set of
 * rules: a 3 s timeout, bounded retries with jitter, 4xx is the server's answer and is not
 * retried, and a failed report is logged and dropped. Reporting must never fail the work it
 * reports on (design.md §6).
 */

import { createHash } from 'node:crypto'

/** A report that did not reach the server. Callers log it and carry on. */
export class ReportError extends Error {
  constructor(message) {
    super(message)
    this.name = 'ReportError'
  }
}

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Deterministic idempotency key, the same shape `cpctl` derives.
 *
 * The dashboard dedupes on `event_key` across the whole table, so the parts must identify one
 * logical event: a retry hashes to the same key, and a genuinely new event does not. Callers
 * that have a stable DSH identity (a turn, a step, an approval id) pass it in rather than
 * relying on the message text.
 */
export function autoKey(...parts) {
  const digest = createHash('sha1').update(parts.join('|')).digest('hex').slice(0, 16)
  return `auto-${digest}`
}

/** Collapse text to one presentable line. Titles are read on a phone; newlines are noise. */
export function firstLine(text, limit = 68) {
  if (typeof text !== 'string') return ''
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/\s+/g, ' ').trim()
    if (!line) continue
    // The ellipsis is inside the limit, not added on top of it: this value is sent as a
    // `title`, and one character over the API's cap would make the whole patch a 422.
    return line.length > limit ? `${line.slice(0, Math.max(1, limit - 1)).trimEnd()}…` : line
  }
  return ''
}

/** The prompt text a turn was opened with, from DSH's content blocks. */
export function titleFromContent(content, limit = 68) {
  if (!Array.isArray(content)) return ''
  const text = content
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join(' ')
  return firstLine(text, limit)
}

function cancelCause(cause) {
  if (!cause || typeof cause !== 'object') return 'unknown'
  if (cause.kind === 'hook' && typeof cause.reason === 'string' && cause.reason.trim()) {
    return `hook: ${firstLine(cause.reason, 120)}`
  }
  return typeof cause.kind === 'string' ? cause.kind : 'unknown'
}

/**
 * Map DSH's own turn-end reason onto a dashboard outcome.
 *
 * This is a translation, not an inference (design.md §2): DSH records the reason in its
 * durable log, and every branch below quotes it. The `unknown` fallback is honest — a plugin
 * may add a reason kind this build has never seen, and inventing `completed` for it would turn
 * uncertainty into a verdict.
 */
export function turnOutcome(reason) {
  const kind = reason && typeof reason.kind === 'string' ? reason.kind : 'unknown'
  switch (kind) {
    case 'completed':
      return { status: 'completed', summary: 'DSH turn completed' }
    case 'error': {
      const error = reason.error ?? {}
      const message = typeof error.message === 'string' && error.message ? error.message : 'no reason given'
      const code = typeof error.code === 'string' && error.code ? ` (${error.code})` : ''
      return { status: 'failed', summary: `DSH turn failed${code}: ${message}` }
    }
    case 'aborted':
      return { status: 'interrupted', summary: `DSH turn aborted (${cancelCause(reason.reason)})` }
    case 'blocked':
      // The prompt was refused before the first step; no model work happened.
      return { status: 'interrupted', summary: 'DSH turn was blocked before the first step' }
    case 'max-tokens':
      // The turn did finish; its last step merely hit the provider's output ceiling.
      return { status: 'completed', summary: 'DSH turn completed at the output-token ceiling' }
    case 'interrupted':
      return { status: 'interrupted', summary: 'DSH closed a crash-orphaned turn on reload' }
    default:
      return { status: 'unknown', summary: `DSH turn ended with an unrecognized reason: ${kind}` }
  }
}

/** One client per plugin instance. Every method either resolves or throws `ReportError`. */
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
            'User-Agent': 'dsh-csd-reporter/0.1',
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
