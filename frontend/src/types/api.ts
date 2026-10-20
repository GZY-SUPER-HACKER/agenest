/**
 * Wire types, mirroring `backend/app/schemas.py`.
 *
 * The one thing to keep in mind: `possibly_stale` is a **display hint the server computed
 * for this response**, not a stored status. The status field is always the truth; this flag
 * is how the spec's "可能失联" requirement is satisfied without ever lying about state.
 */

export type SessionStatus =
  | "running"
  | "completed"
  | "failed"
  | "interrupted"
  | "unknown";

export type EventType =
  | "session_started"
  | "status_changed"
  | "progress"
  | "note"
  | "awaiting_user"
  | "session_completed"
  | "session_failed"
  | "session_interrupted"
  | "heartbeat";

export interface SessionEvent {
  id: number;
  session_id: string;
  event_type: EventType;
  message: string;
  payload: Record<string, unknown> | null;
  occurred_at: string;
  received_at: string;
}

export interface Session {
  id: string;
  agent_type: string;
  title: string;
  project_name: string | null;
  workspace: string | null;
  /**
   * The machine this ran on, as the reporter named it — `csd-agent`'s label, usually the
   * hostname. `null` means nobody said, which is a real answer: those rows are grouped under
   * a "no host recorded" heading rather than attributed to a machine we guessed at.
   */
  host: string | null;
  status: SessionStatus;
  started_at: string;
  last_activity_at: string;
  finished_at: string | null;
  summary: string | null;
  exit_code: number | null;
  created_at: string;
  updated_at: string;
  possibly_stale: boolean;
  /**
   * The newest non-heartbeat event, so a row can say what a session is *doing* without
   * fetching it. `null` for a session that has only ever reported a heartbeat.
   */
  last_event: SessionEvent | null;
}

export interface SessionDetail extends Session {
  heartbeat_count: number;
  events: SessionEvent[];
}

export interface SessionListResponse {
  sessions: Session[];
  total: number;
  limit: number;
  offset: number;
}

/** SSE frame payloads. Both carry a database event id in `id:` on the wire. */
export interface SessionUpdatedFrame {
  kind: "session.updated";
  at: string;
  session: Session;
}

export interface SessionEventFrame {
  kind: "session.event";
  at: string;
  session_id: string;
  event: SessionEvent;
}

/**
 * One connected agent host — a machine running a coding agent, not a unit of work.
 *
 * Kept apart from `Session` on purpose: a session's status says how work *ended*, while a host
 * is a machine being reachable, which never ends at all. The two would have had to share a
 * `status` field with two unrelated meanings. See `backend/app/schemas.py` and design.md §20.
 *
 * The identity is `(label, agent_type)`: one connector speaks for one coding agent, so a box
 * running both Claude Code and codex connects twice and appears twice.
 */
export interface Host {
  id: string;
  /** The machine's own name for itself. */
  label: string;
  /** The coding agent this connector speaks for. */
  agent_type: string;
  connected_at: string;
  last_seen_at: string;
  /** Set only when a connector *said* it was leaving. `null` means nobody has said. */
  disconnected_at: string | null;
  /** Display hint, same rule as on a session: computed per response, never stored. */
  possibly_stale: boolean;
  /** The connector declared that the personal agent may be allowed to ssh into this machine.
   *  A declaration, not a grant: it is what puts the approval in front of the user. */
  wants_access: boolean;
  /**
   * What the personal agent may do with this host, or `null` when the connector never asked.
   *
   * `requested` is derived (declared, but no approval on file) and `address_changed` is a live
   * comparison between what the host declares now and what it declared when the user approved —
   * neither is stored, which is why this is a string union rather than a table of flags.
   */
  access_state:
    | "requested"
    | "pending"
    | "ready"
    | "address_changed"
    | "disabled"
    | null;
  /** The approved `user@host:port`, once there is one. Never the declared address: the UI must
   *  not offer an address the user has not confirmed. */
  access_target: string | null;
  /** A short fingerprint of the approved key, for a human comparing two of them. */
  access_fingerprint: string | null;
}

export interface HostListResponse {
  hosts: Host[];
}

/**
 * The answer to approving a host. `public_key` is the only key material any response carries —
 * the private half stays on the server, and the connector receives it from the same place.
 */
export interface HostAccessResponse {
  host: Host;
  public_key: string | null;
  fingerprint: string | null;
  /** What happens next, in the server's words ("waiting for the host to install the key…"). */
  note: string | null;
}

export interface HostUpdatedFrame {
  kind: "host.updated";
  at: string;
  host: Host;
}

export type StreamFrame =
  | SessionUpdatedFrame
  | SessionEventFrame
  | HostUpdatedFrame;

/** Which view the SSE connection is in, so the header can be honest about it. */
export type ConnectionState = "connecting" | "live" | "reconnecting" | "offline";

/* -------------------------------------------------------------------------- */
/* agenest — mirrors the agent section of `backend/app/schemas.py`             */
/* -------------------------------------------------------------------------- */

/**
 * `tool` is a tool *result*, not a tool call. The call itself only ever appears as a live
 * `agent.tool` phase="start" frame, because a tool that started and never finished has no
 * outcome to record — and inventing one would be exactly the kind of inference the dashboard
 * spent its whole design avoiding.
 */
export type AgentRole = "user" | "assistant" | "tool";

export interface AgentMessage {
  /**
   * **The agent's own id for this message**, and what the page dedups on.
   *
   * A string, not a row number: the identity belongs to the agent, and it has to be the same
   * value across a refresh, a reconnect and a restart — the page draws a message early from the
   * POST response and meets it again in a fetched history, and the two must be recognised as
   * one. `null` only when a backend cannot name its own messages, which degrades dedup rather
   * than breaking it.
   */
  id: string | null;
  thread_id: string;
  role: AgentRole;
  content: string;
  tool_name: string | null;
  tool_call_id: string | null;
  /** A tool call's arguments — the command line, for a shell tool. */
  args: Record<string, unknown> | null;
  is_error: boolean;
  created_at: string;
  /**
   * A **checkpoint**: text the agent wrote in the same entry as its tool calls, rather than the
   * answer. Drawn as an ordinary agent bubble — only native and nanobot's websocket door send
   * these, so a display of its own would have been a display three agents could never fill
   * (docs/design.md §23).
   */
  progress: boolean;
}

export interface AgentThread {
  id: string;
  title: string;
  /** Which agent this conversation is with: `nanobot` or `native`. */
  backend: string;
  /** The agent's own id for this conversation (nanobot's `chat_id`); null until first used. */
  conversation_id: string | null;
  provider: string | null;
  model: string | null;
  workdir: string | null;
  created_at: string;
  updated_at: string;
  running: boolean;
  streaming: boolean;
}

export interface AgentThreadDetail extends AgentThread {
  messages: AgentMessage[];
}

/**
 * One agent this deployment can talk to.
 *
 * `ready` and `connected` are separate on purpose: a gateway that is merely down is *ready*
 * (start it and it works), while a local agent whose binary was never installed is not, and
 * the fix is to install it. Collapsing them would send someone to "reconnect" when they need
 * to install something.
 */
export interface AgentBackend {
  name: string;
  active: boolean;
  ready: boolean;
  connected: boolean;
  detail: string | null;
  /**
   * Whether the *agent itself* registered where to knock (design.md §25), as opposed to
   * being configured on this side by env. It is what lets the panel distinguish "nothing has
   * told us where your agent is" from "this backend is not installed here" — two sentences
   * that send the reader to completely different places.
   */
  registered: boolean;
  /**
   * Whether this row gets a gear (design.md §26). True only for the agent this service runs
   * itself: a gateway agent's model and credentials live on its own host, so a form here would
   * change nothing. Read from the payload rather than compared against a name in the page — the
   * backend is the one that knows what it can configure.
   */
  configurable: boolean;
}

export interface AgentStatus {
  backend: string;
  connected: boolean;
  detail: string | null;
  provider: string | null;
  model: string | null;
  workdir: string | null;
  tools: string[];
  threads_running: number;
  backends: AgentBackend[];
}


/**
 * The switch's answer: the new status **and** the conversation to show.
 *
 * Switching used to be three dependent requests (switch → session → thread), which on a phone
 * link reads as "the transcript is slow to sync". The page now makes one.
 */
export interface AgentSwitch extends AgentStatus {
  thread: AgentThreadDetail;
}


/**
 * The local agent's configuration (design.md §26).
 *
 * **There is no key field, and that is the contract.** `api_key_set` says one is stored;
 * `api_key_env` names — never shows — the provider variables already present in the server's
 * environment. The same rule `Host` follows for an address, and the reason a screenshot of this
 * sheet cannot leak a credential.
 *
 * Every field here is what the *user* said. `null` means "not set", and `env_defaults` is what
 * the server would use in that case — which is what the form's placeholders show.
 */
export interface NativeConfig {
  provider: string | null;
  model: string | null;
  base_url: string | null;
  api_kind: string | null;
  context_window: number | null;
  max_tokens: number | null;
  /** `null` means Pi's default: every built-in tool, `bash` included. */
  tools: string[] | null;
  trust_project_files: boolean;
  api_key_set: boolean;
  api_key_env: string[];
  /** Read-only here: it decides where the agent writes *and* where its memory lives. */
  workspace: string | null;
  env_defaults: {
    provider: string | null;
    model: string | null;
    tools: string[] | null;
  };
  /** What the next process will actually run on, after the environment fills the gaps. */
  effective: {
    provider: string | null;
    model: string | null;
    tools: string[];
  };
}

/**
 * A partial update. **Omitted means unchanged; `null` means clear.**
 *
 * The distinction is load-bearing: clearing a field is how you go back to the environment, and a
 * client that had to send every field to change one would erase the rest by omission.
 */
export interface NativeConfigPatch {
  provider?: string | null;
  model?: string | null;
  base_url?: string | null;
  /** Write-only. It is never returned by any route. */
  api_key?: string | null;
  api_kind?: string | null;
  context_window?: number | null;
  max_tokens?: number | null;
  tools?: string[] | null;
  trust_project_files?: boolean;
}

export interface NativeConfigCheck {
  ok: boolean;
  detail: string;
  provider: string | null;
  model: string | null;
}


/**
 * Agent stream frames. Unlike `StreamFrame` above there is no `id:` on the wire — the server
 * sends none, because these frames are mostly deltas that are never stored and so cannot be
 * resumed from. `agent.delta` is transient by definition: it is text that has not finished
 * being a message yet.
 */
export interface AgentDeltaFrame {
  kind: "agent.delta";
  at: string;
  thread_id: string;
  channel: "text" | "thinking";
  delta: string;
  /**
   * The delta is the whole text so far, not an addition to it.
   *
   * Always present (nanobot and native send `false`); a backend that restarts an item sets
   * it. Appending a replacement would render the answer twice, so this cannot be ignored.
   */
  replace: boolean;
}

export interface AgentMessageFrame {
  kind: "agent.message";
  at: string;
  thread_id: string;
  message: AgentMessage;
}

export interface AgentToolFrame {
  kind: "agent.tool";
  at: string;
  thread_id: string;
  phase: "start" | "end";
  tool_name: string | null;
  tool_call_id: string | null;
  /** Present on `phase: "start"` — the arguments the tool was called with. */
  args?: Record<string, unknown> | null;
  is_error?: boolean;
}

export interface AgentStatusFrame {
  kind: "agent.status";
  at: string;
  thread_id: string;
  running: boolean;
  streaming: boolean;
  provider: string | null;
  model: string | null;
}

export interface AgentNoticeFrame {
  kind: "agent.notice";
  at: string;
  thread_id: string;
  notice: string;
  level: "info" | "error";
  detail: string | null;
}

export type AgentStreamFrame =
  | AgentDeltaFrame
  | AgentMessageFrame
  | AgentToolFrame
  | AgentStatusFrame
  | AgentNoticeFrame;
