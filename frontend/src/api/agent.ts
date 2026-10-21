/**
 * The agenest client.
 *
 * Two things here are deliberately unlike `client.ts` next to it, and both are about the
 * credential rather than about style.
 *
 * **The stream uses `fetch`, not `EventSource`.** The dashboard's stream uses `EventSource` and
 * therefore passes its token as a `?token=` query parameter, because `EventSource` cannot set
 * request headers — and the backend documents the cost of that concession. This page sends the
 * *same* credential (`auth.access_header`), but the routes behind it can drive an agent that runs
 * shell commands and reaches other machines, so putting it in a URL would write it into browser
 * history, `Referer` headers and every proxy log on the path. `fetch` + a `ReadableStream` lets it
 * travel as a real `Authorization` header, which is why this file has an SSE parser in it.
 *
 * **The stream has no cursor and the client does not pretend otherwise.** The server sends no
 * `id:` (see `stream.AgentBroker` — most frames are deltas that are never stored). So the
 * contract here is: open the stream *first*, then fetch the thread. Anything that completes
 * in between arrives twice, which is the harmless direction — a duplicate the client can drop
 * beats a message nobody ever sees.
 */

import { UNAUTHORIZED, readToken } from "./client";
import type {
  AgentAttachment,
  AgentMessage,
  AgentStatus,
  AgentStreamFrame,
  AgentSwitch,
  AgentThread,
  AgentThreadDetail,
  NativeConfig,
  NativeConfigCheck,
  NativeConfigPatch,
} from "../types/api";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

/**
 * The server has no access token configured, so the routes that *act* answer 403.
 *
 * This is the one place one token does not mean one rule (design.md §22): unset means reads are
 * open and acting is off, because a default that left an agent with `bash` open would be the
 * worst default in the codebase. Kept distinct from "unauthorized" on purpose — that one is a
 * configuration choice made on the server, this one is a missing credential in this browser, and
 * telling a user to find a token that does not exist sends them to the wrong problem.
 */
export const AGENT_DISABLED = "agent-disabled";

export function isAgentDisabled(error: unknown): boolean {
  return (
    error === AGENT_DISABLED ||
    (error instanceof Error && error.message === AGENT_DISABLED)
  );
}

export function agentErrorMessage(error: unknown): string {
  if (isAgentDisabled(error)) {
    return "This server has no access token set, so the agent is switched off.";
  }
  if (error === UNAUTHORIZED) return "This page needs its token.";
  return error instanceof Error ? error.message : String(error);
}

// --------------------------------------------------------------------------- //
// REST
// --------------------------------------------------------------------------- //
function headers(token: string): Record<string, string> {
  return {
    Accept: "application/json",
    "Content-Type": "application/json",
    ...(token ? { "X-CPD-Token": token } : {}),
  };
}

async function agentFetch<T>(
  path: string,
  init: RequestInit = {},
  token = readToken(),
  notFound = "",
): Promise<T> {
  const response = await fetch(API_BASE + path, {
    ...init,
    headers: { ...headers(token), ...(init.headers ?? {}) },
  });
  if (response.status === 403) throw new Error(AGENT_DISABLED);
  if (response.status === 401) throw new Error(UNAUTHORIZED);
  // A 404 only means something when the caller says which route it was calling and what its
  // absence means. Most 404s here are "unknown thread", which is a bug; the memory routes'
  // 404s are "this deployment has no memory directory", which is a configuration.
  if (response.status === 404 && notFound) throw new Error(notFound);
  if (!response.ok) {
    // The backend puts the useful half of a failure in `detail` — "pi is not installed",
    // "the provider key is missing". Surfacing the bare status would hide exactly the part
    // that says what to do.
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not JSON; keep the status line */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function fetchAgentStatus(): Promise<AgentStatus> {
  return agentFetch<AgentStatus>("/api/agent/status");
}

/**
 * Change the active agent and get the conversation to show, in one request.
 *
 * The three primitives it composes (`/backend`, `/session`, `/threads/{id}`) still exist for
 * API clients; the page uses this because they are sequential and each round trip costs about
 * a second on a phone link.
 */
export function switchAgentBackend(backend: string, process = true): Promise<AgentSwitch> {
  return agentFetch<AgentSwitch>(
    `/api/agent/switch?process=${process ? "true" : "false"}`,
    {
      method: "POST",
      body: JSON.stringify({ backend }),
    },
  );
}

/**
 * One conversation.
 *
 * `process=false` omits tool rows. They are most of a long transcript's bytes, and the page's
 * default view does not draw them — downloading them anyway is what made a switch slow.
 */
export function fetchAgentThread(id: string, process = true): Promise<AgentThreadDetail> {
  return agentFetch(
    `/api/agent/threads/${encodeURIComponent(id)}?process=${process ? "true" : "false"}`,
  );
}

/**
 * The one conversation, created on first call.
 *
 * Idempotent, so the page can call it on every boot without asking whether a session already
 * exists — the second call returns the same conversation and changes nothing. Scoped to the
 * active backend on the server: each agent keeps its own conversation, because a thread talks
 * to the agent it was started on and re-pointing one would silently drop its context.
 */
export function openAgentSession(): Promise<AgentThread> {
  return agentFetch<AgentThread>("/api/agent/session", { method: "POST" });
}

export function sendAgentMessage(
  threadId: string,
  content: string,
  attachmentIds: string[] = [],
): Promise<AgentMessage> {
  return agentFetch<AgentMessage>(
    `/api/agent/threads/${encodeURIComponent(threadId)}/messages`,
    {
      method: "POST",
      body: JSON.stringify({ content, attachment_ids: attachmentIds }),
    },
  );
}

/**
 * Upload one file and get the id to send with the next message.
 *
 * The body is the file itself rather than a multipart envelope: the backend streams it
 * straight to disk, so there is nothing for a parser to unpack. The filename goes in a header,
 * percent-encoded because HTTP header values are not UTF-8 by contract and a Chinese filename
 * is the ordinary case here.
 */
export function uploadAgentAttachment(
  threadId: string,
  file: File,
): Promise<AgentAttachment> {
  return agentFetch<AgentAttachment>(
    `/api/agent/threads/${encodeURIComponent(threadId)}/attachments`,
    {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-CSD-Filename": encodeURIComponent(file.name),
      },
      body: file,
    },
  );
}

/**
 * The bytes behind one attachment, as a blob.
 *
 * Deliberately not an `<img src>`: that would need the access token in the URL, and this
 * credential can drive an agent. The page fetches with a header and hands the browser a blob
 * URL, exactly as the stream does. Kept out of `agentFetch` because the response is not JSON.
 */
export async function fetchAgentAttachment(id: string): Promise<Blob> {
  const token = readToken();
  const response = await fetch(
    `${API_BASE}/api/agent/attachments/${encodeURIComponent(id)}`,
    { headers: { Accept: "*/*", ...(token ? { "X-CPD-Token": token } : {}) } },
  );
  if (response.status === 403) throw new Error(AGENT_DISABLED);
  if (response.status === 401) throw new Error(UNAUTHORIZED);
  if (!response.ok) {
    throw new Error(`attachment -> ${response.status} ${response.statusText}`);
  }
  return response.blob();
}

/** Remove an upload that never reached a message — the page's cleanup on a failed send. */
export function deleteAgentAttachment(id: string): Promise<void> {
  return agentFetch<void>(`/api/agent/attachments/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function abortAgentRun(threadId: string): Promise<{ aborted: boolean }> {
  return agentFetch(`/api/agent/threads/${encodeURIComponent(threadId)}/abort`, {
    method: "POST",
  });
}

// --------------------------------------------------------------------------- //
// the local agent's configuration (design.md §26)
// --------------------------------------------------------------------------- //
/**
 * Only the agent this service runs itself can be configured here; a gateway agent keeps its own
 * model and credentials on its own host, so its row has no gear.
 */
export function fetchNativeConfig(): Promise<NativeConfig> {
  return agentFetch<NativeConfig>("/api/agent/native/config");
}

/**
 * A partial update: omitted fields keep their value, `null` clears one back to the environment.
 *
 * The change reaches the agent on its **next message** — the command line is built when a Pi
 * process starts — so the panel's own wording has to say that rather than "restarted".
 */
export function saveNativeConfig(patch: NativeConfigPatch): Promise<NativeConfig> {
  return agentFetch<NativeConfig>("/api/agent/native/config", {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/** Whether Pi resolves the configured provider and model. Free, and not a credential test. */
export function checkNativeConfig(): Promise<NativeConfigCheck> {
  return agentFetch<NativeConfigCheck>("/api/agent/native/check", { method: "POST" });
}

// --------------------------------------------------------------------------- //
// the stream
// --------------------------------------------------------------------------- //
export interface AgentStreamHandle {
  close: () => void;
}

/**
 * Parse an SSE byte stream into frames.
 *
 * Kept local and small rather than pulled from a package: the format this server emits is
 * three field names (`event`, `data`, and comments, which are ignored), and a general parser
 * would be more code than this.
 *
 * Records are separated by a blank line. A `data:` payload is JSON on one line — the server
 * serialises with `separators=(",", ":")` and no newlines — so no multi-line `data:`
 * accumulation is needed; if that ever changes, this is the function that must change with it.
 */
async function* readFrames(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<AgentStreamFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const record = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");

        let eventName = "";
        let data = "";
        for (const line of record.split("\n")) {
          // `: keep-alive` and `: agent stream open` are comments, not payload.
          if (line.startsWith(":")) continue;
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!eventName || !data) continue;
        try {
          yield JSON.parse(data) as AgentStreamFrame;
        } catch {
          /* a malformed frame is dropped, never fatal */
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * Open the agent stream and keep it open, reconnecting with backoff.
 *
 * `onOpen` fires on every successful connection, including reconnects — that is the hook the
 * page uses to re-fetch the thread, because this stream carries no cursor and cannot say what
 * was missed.
 */
export function openAgentStream(options: {
  onFrame: (frame: AgentStreamFrame) => void;
  onOpen?: () => void;
  onError?: (error: unknown) => void;
  threadId?: string;
}): AgentStreamHandle {
  const controller = new AbortController();
  let closed = false;
  let retryDelay = 1000;
  let timer: number | undefined;

  const connect = async () => {
    if (closed) return;
    const token = readToken();
    if (!token) {
      // Without a token the server answers 403/401, which the page renders as a prompt.
      options.onError?.(new Error(UNAUTHORIZED));
      return;
    }

    const params = new URLSearchParams();
    if (options.threadId) params.set("thread_id", options.threadId);
    const suffix = params.toString();

    try {
      const response = await fetch(
        `${API_BASE}/api/agent/stream${suffix ? `?${suffix}` : ""}`,
        {
          headers: { Accept: "text/event-stream", "X-CPD-Token": token },
          signal: controller.signal,
        },
      );
      if (response.status === 403) throw new Error(AGENT_DISABLED);
      if (response.status === 401) throw new Error(UNAUTHORIZED);
      if (!response.ok || !response.body) {
        throw new Error(`agent stream -> ${response.status} ${response.statusText}`);
      }

      retryDelay = 1000;
      options.onOpen?.();

      for await (const frame of readFrames(response.body)) {
        if (closed) return;
        options.onFrame(frame);
      }
      // A clean end is still a disconnect: the server closes on redeploy.
      throw new Error("stream ended");
    } catch (error) {
      if (closed || controller.signal.aborted) return;
      options.onError?.(error);
      // A disabled agent or a missing token will not fix itself by retrying, so stop and let
      // the page ask for the token instead of hammering a 403 forever.
      if (
        error instanceof Error &&
        (error.message === AGENT_DISABLED || error.message === UNAUTHORIZED)
      ) {
        return;
      }
    }

    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      retryDelay = Math.min(retryDelay * 2, 15000);
      void connect();
    }, retryDelay);
  };

  void connect();

  return {
    close: () => {
      closed = true;
      window.clearTimeout(timer);
      controller.abort();
    },
  };
}
