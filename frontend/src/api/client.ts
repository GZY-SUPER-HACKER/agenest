/**
 * API client and SSE hook.
 *
 * Two things drive the design:
 *
 * 1. **The cursor is persisted, not guessed.** The last event id this tab rendered is stored
 *    in `localStorage`, so a reconnect passes `?cursor=<id>` and the server resumes from
 *    genuinely after it. The id is a *database* row id, so it survives a service restart —
 *    which a per-connection counter would not.
 * 2. **Reconnect refreshes, it does not assume.** The spec is explicit that a reconnect may
 *    have missed messages, so on every reconnect the list is re-fetched. SSE is treated as a
 *    hint ("something changed"), never as the source of truth.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type {
  ConnectionState,
  Host,
  HostAccessResponse,
  HostListResponse,
  Session,
  SessionDetail,
  SessionEvent,
  SessionListResponse,
  StreamFrame,
} from "../types/api";

const CURSOR_KEY = "csd.last-event-id";
const TOKEN_KEY = "csd.token";
const API_BASE = import.meta.env.VITE_API_BASE ?? "";

/**
 * The dashboard read token, if the server asks for one.
 *
 * Read from `localStorage` at request time, never baked in at build time. The build output is
 * served to whoever asks for it — over a public Funnel URL, that is everyone — so a token
 * compiled into the bundle would be a token published to the world. It has to be something
 * the person in front of the screen supplies, which is why the UI prompts on a 401.
 */
export function readToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    // Private mode. The token still works for this session if the caller holds it.
    return "";
  }
}

export function storeToken(token: string): void {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* not persisting is fine */
  }
}

/**
 * Adopt a read token supplied in the URL fragment, then strip it from the address bar.
 *
 * This exists so a 43-character credential can get onto a phone without being typed. Paste
 *
 *     https://<host>/#token=<value>
 *
 * into any chat app, tap the link, and the dashboard is unlocked.
 *
 * A **fragment** rather than a query parameter, deliberately: a fragment is never sent to the
 * server, so the credential cannot appear in an access log, a proxy log, or a `Referer`
 * header. The one place it does land — the address bar and the history entry — is rewritten
 * immediately by `replaceState`, so it does not survive the load that consumed it.
 *
 * Only a fragment that is *exactly* a parameter list is considered. `#/session/<id>` is the
 * app's own routing and must never be parsed for credentials.
 */
export function adoptTokenFromFragment(): boolean {
  const raw = window.location.hash.replace(/^#/, "");
  // A leading slash means this is a route, not a credential.
  if (!raw || raw.startsWith("/")) return false;

  const token = new URLSearchParams(raw).get("token");
  if (!token) return false;

  storeToken(token);
  history.replaceState(null, "", window.location.pathname + window.location.search);
  return true;
}

/** Sentinel error text for "the server wants a read token and does not like ours".

 * A sentinel rather than an error subclass because it has to survive the hooks' `error: string | null`
 * state, and a string that a caller compares against is harder to accidentally swallow than a
 * class that ends up stringified into a banner.
 */
export const UNAUTHORIZED = "unauthorized";

/**
 * Accepts either a plain string (the hooks keep `error` as text) or a thrown `Error` (the
 * controls call the API directly), because the same sentinel has to be recognisable from
 * both shapes and a wrong-signature call site fails at compile time rather than at 3 a.m.
 */
export function isUnauthorized(error: unknown): boolean {
  return (
    error === UNAUTHORIZED ||
    (error instanceof Error && error.message === UNAUTHORIZED)
  );
}

// --------------------------------------------------------------------------- //
// REST
// --------------------------------------------------------------------------- //
async function apiGet<T>(path: string): Promise<T> {
  const token = readToken();
  const response = await fetch(API_BASE + path, {
    headers: {
      Accept: "application/json",
      ...(token ? { "X-CPD-Token": token } : {}),
    },
  });
  if (response.status === 401) throw new Error(UNAUTHORIZED);
  if (!response.ok) {
    throw new Error(`${path} -> ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export interface ListParams {
  status?: string;
  agent?: string;
  limit?: number;
}

export function fetchSessions(params: ListParams = {}): Promise<SessionListResponse> {
  const query = new URLSearchParams();
  if (params.status) query.set("status", params.status);
  if (params.agent) query.set("agent", params.agent);
  if (params.limit) query.set("limit", String(params.limit));
  const suffix = query.toString();
  return apiGet<SessionListResponse>(`/api/sessions${suffix ? `?${suffix}` : ""}`);
}

export function fetchSession(id: string, eventLimit = 200): Promise<SessionDetail> {
  return apiGet<SessionDetail>(
    `/api/sessions/${id}?event_limit=${eventLimit}`,
  );
}

/**
 * Every connected agent host.
 *
 * Not filtered, and not paginated: there is one row per machine-and-coding-agent, so this is
 * a list of the boxes you own rather than a feed. It is fetched alongside the session list
 * and kept live by `host.updated` frames, so a host that connects while the page is open
 * appears without a refresh — which is the whole point of showing it.
 */
export function fetchHosts(): Promise<HostListResponse> {
  return apiGet<HostListResponse>("/api/hosts");
}

export function fetchEvents(id: string): Promise<SessionEvent[]> {
  return apiGet<{ events: SessionEvent[] }>(`/api/sessions/${id}/events`).then(
    (r) => r.events,
  );
}

/**
 * An action: the routes that change something, all of which answer with the server's own
 * sentence when they refuse.
 *
 * One token, same as every read (docs/design.md §22). What is not the same is the
 * *requirement*: these routes refuse with 403 while `CSD_DASHBOARD_TOKEN` is unset, and that
 * detail is surfaced as it comes rather than translated into a local sentinel, because the
 * sentence the operator needs ("set CSD_DASHBOARD_TOKEN") is the one the server wrote.
 */
async function apiAction<T>(
  path: string,
  method: "POST" | "DELETE",
  body?: unknown,
): Promise<T> {
  const token = readToken();
  const response = await fetch(API_BASE + path, {
    method,
    headers: {
      Accept: "application/json",
      ...(token ? { "X-CPD-Token": token } : {}),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    let detail = `${method} ${path} -> ${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      /* not JSON; keep the status line */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

/**
 * Close a session nothing is going to report on.
 *
 * The one action in this client that changes the *session* record, and it uses the same token
 * as every read — one page, one credential (docs/design.md §22).
 */
export async function closeSession(id: string): Promise<Session> {
  return apiAction<Session>(`/api/sessions/${id}/close`, "POST");
}

/**
 * Authorize the personal agent to ssh into a registered host (docs/design.md §24).
 *
 * The server mints a key and answers with the *public* half; the host's own connector installs
 * it on its next heartbeat, which is why the response comes back `pending` rather than `ready`.
 * The private key never leaves the server.
 */
export async function authorizeHostAccess(
  hostId: string,
  overrides: { ssh_host?: string; ssh_user?: string; ssh_port?: number } = {},
): Promise<HostAccessResponse> {
  return apiAction<HostAccessResponse>(`/api/hosts/${hostId}/access`, "POST", overrides);
}

/** Withdraw access and delete the key. The host removes its `authorized_keys` line next beat. */
export async function revokeHostAccess(hostId: string): Promise<Host> {
  return apiAction<Host>(`/api/hosts/${hostId}/access`, "DELETE");
}

// --------------------------------------------------------------------------- //
// cursor
// --------------------------------------------------------------------------- //
function readCursor(): number {
  const raw = window.localStorage.getItem(CURSOR_KEY);
  const parsed = raw ? Number.parseInt(raw, 10) : Number.NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function writeCursor(id: number): void {
  if (id > 0) window.localStorage.setItem(CURSOR_KEY, String(id));
}

// --------------------------------------------------------------------------- //
// SSE
// --------------------------------------------------------------------------- //
export interface StreamOptions {
  /** Only receive frames for this session. The cursor still advances for all frames. */
  sessionId?: string;
  onFrame: (frame: StreamFrame) => void;
}

export type ConnectionListener = (state: ConnectionState) => void;

export interface StreamHandle {
  /** The current connection state (a live value, read at access time). */
  readonly state: ConnectionState;
  /** Subscribe to state changes. Returns an unsubscribe function. */
  subscribe: (listener: ConnectionListener) => () => void;
  close: () => void;
}

/**
 * Open the SSE stream and keep it open.
 *
 * `EventSource` cannot send custom headers. That is the entire reason the backend also
 * accepts the read token as a `?token=` query parameter — see backend/app/auth.py, which
 * documents the cost of that concession. The default deployment (no dashboard token, reached
 * over a tailnet) needs no credential at all.
 *
 * `onFrame` is held in a ref, so a re-render never tears down a live connection; changing
 * `sessionId` after connecting is not supported (open a new handle instead).
 */
export function openStream(
  options: StreamOptions & { token?: string } = { onFrame: () => {} },
): StreamHandle {
  const onFrameRef = { current: options.onFrame };
  const listeners = new Set<ConnectionListener>();

  let state: ConnectionState = "connecting";
  let source: EventSource | null = null;
  let closed = false;
  let retryDelay = 1000;
  let reconnectTimer: number | undefined;

  const emit = () => {
    for (const listener of [...listeners]) listener(state);
  };

  const setState = (next: ConnectionState) => {
    if (state === next) return;
    state = next;
    emit();
  };

  const connect = () => {
    if (closed) return;

    const cursor = readCursor();
    // Read at connect time rather than at construction, so that entering a token and
    // reconnecting picks it up without tearing down and rebuilding the handle.
    const token = options.token ?? readToken();
    const params = new URLSearchParams();
    if (cursor > 0) params.set("cursor", String(cursor));
    if (options.sessionId) params.set("session_id", options.sessionId);
    if (token) params.set("token", token);

    source = new EventSource(`${API_BASE}/api/events?${params.toString()}`);
    setState(cursor > 0 ? "reconnecting" : "connecting");

    for (const name of ["session.updated", "session.event", "host.updated"]) {
      source.addEventListener(name, (raw) => {
        const event = raw as MessageEvent<string>;
        let frame: StreamFrame;
        try {
          frame = JSON.parse(event.data) as StreamFrame;
        } catch {
          return; // a malformed frame is dropped, never fatal
        }

        // `id:` and `data:` may arrive in either order within a frame; `lastEventId` is the
        // authoritative cursor for the frame that just completed.
        const id = Number.parseInt(event.lastEventId, 10);
        if (Number.isFinite(id) && id > 0) {
          writeCursor(id);
        }
        onFrameRef.current(frame);
      });
    }

    source.onopen = () => {
      retryDelay = 1000;
      setState("live");
    };

    // `EventSource` retries on its own, but not through a server restart that leaves the
    // socket half-open. This is the belt to its braces.
    source.onerror = () => {
      if (closed) return;
      setState("reconnecting");
      source?.close();
      window.clearTimeout(reconnectTimer);
      reconnectTimer = window.setTimeout(() => {
        retryDelay = Math.min(retryDelay * 2, 15000);
        connect();
      }, retryDelay);
    };
  };

  connect();

  return {
    get state(): ConnectionState {
      return state;
    },
    subscribe: (listener: ConnectionListener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    close: () => {
      closed = true;
      window.clearTimeout(reconnectTimer);
      source?.close();
      listeners.clear();
    },
  };
}

// --------------------------------------------------------------------------- //
// hooks
// --------------------------------------------------------------------------- //
/**
 * The live dashboard feed: sessions and the hosts running them.
 *
 * Both, from one hook and one stream, because the rail renders them as a single tree — a host
 * with its sessions under it. Two hooks would open two `EventSource`s to keep one screen
 * consistent, and could disagree with each other while reconnecting.
 */
export function useSessions(params: ListParams = {}): {
  sessions: Session[];
  hosts: Host[];
  loading: boolean;
  error: string | null;
  reload: () => void;
  connection: ConnectionState;
} {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [hosts, setHosts] = useState<Host[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");

  // Params can change as the user taps a filter; keep them in a ref so the SSE connection
  // is not torn down and rebuilt on every keystroke.
  const paramsRef = useRef(params);
  paramsRef.current = params;

  const reload = useCallback(async () => {
    try {
      const response = await fetchSessions(paramsRef.current);
      setSessions(response.sessions);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }

    // Hosts are fetched separately, and a failure here is swallowed, so that it cannot blank
    // the session list. On screen the two are one tree, but the session list *is* the page
    // and the hosts are an annotation on it: a backend that predates `/api/hosts` should cost
    // you the annotation, not the dashboard.
    try {
      const hostResponse = await fetchHosts();
      setHosts(hostResponse.hosts);
    } catch {
      // Keep whatever we already had. Silently, because the sessions list above has already
      // reported anything genuinely wrong with the connection.
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload, params.status, params.agent, params.limit]);

  // Re-fetch on a reconnect, which the module docstring has always claimed and which was not
  // actually done. The mount fetch covers the first connection; every transition back into
  // `live` after that means frames were missed, and missed frames are not recoverable from the
  // cursor alone — a host frame is *anchored* to the newest event id precisely so it does not
  // invent a second numbering space, and the cost of that choice is that a client already past
  // that id is never sent it. Without this, a host that connected while the phone was in a
  // pocket would stay invisible until something forced a manual refresh.
  const wasLive = useRef(false);
  useEffect(() => {
    if (connection !== "live") return;
    if (!wasLive.current) {
      wasLive.current = true;
      return;
    }
    void reload();
  }, [connection, reload]);

  useEffect(() => {
    const handle = openStream({
      onFrame: (frame) => {
        if (frame.kind === "host.updated") {
          // Upsert rather than replace-in-place. A host that connects while this page is
          // open is not in the list yet — the frame is often the first we hear of it, and
          // that arrival *is* the feature. A replayed frame after a reconnect lands here
          // too, and upserting the same object is idempotent.
          setHosts((current) => {
            const index = current.findIndex((h) => h.id === frame.host.id);
            if (index === -1) return [frame.host, ...current];
            const next = [...current];
            next[index] = frame.host;
            return next;
          });
          return;
        }
        if (frame.kind === "session.updated") {
          // A session may have been filtered out (e.g. the user is viewing "running" and
          // this one just finished), so apply the update in place or drop it.
          setSessions((current) => {
            const index = current.findIndex((s) => s.id === frame.session.id);
            if (index === -1) return current;
            const next = [...current];
            next[index] = frame.session;
            return next;
          });
          return;
        }
        // An event for a session not yet in this list is a *new* session (or one the filter
        // hides). Re-fetching is cheap and avoids a half-updated list.
        void reload();
      },
    });

    const unsubscribe = handle.subscribe(setConnection);

    return () => {
      unsubscribe();
      handle.close();
    };
  }, [reload]);

  return { sessions, hosts, loading, error, reload, connection };
}

/** Live-updating single session. Falls back to a full re-fetch whenever the stream says
 *  something changed, because a reconnecting tab cannot assume it saw everything. */
export function useSessionDetail(id: string | null): {
  detail: SessionDetail | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
  connection: ConnectionState;
} {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");

  const reload = useCallback(async () => {
    if (!id) {
      setDetail(null);
      return;
    }
    setLoading(true);
    try {
      setDetail(await fetchSession(id));
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    if (!id) return;
    const handle = openStream({
      sessionId: id,
      onFrame: (frame) => {
        if (frame.kind === "session.updated") {
          setDetail((current) =>
            current ? { ...current, ...frame.session, events: current.events } : current,
          );
          return;
        }
        // Hosts are a dashboard-level concern; a detail page has nothing to do with them. This
        // stream is filtered to one session, so the server sends those frames as `: skip`
        // comments anyway — but the type union still arrives here and has to be narrowed.
        if (frame.kind !== "session.event") return;
        if (frame.session_id !== id) return;
        setDetail((current) =>
          current ? { ...current, events: [frame.event, ...current.events] } : current,
        );
      },
    });

    const unsubscribe = handle.subscribe(setConnection);

    return () => {
      unsubscribe();
      handle.close();
    };
  }, [id]);

  return { detail, loading, error, reload, connection };
}
