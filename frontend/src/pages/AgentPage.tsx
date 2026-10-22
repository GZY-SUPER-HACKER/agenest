/**
 * agenest: the conversation with whichever agent this deployment is pointed at.
 *
 * The interaction model is distilled from Telegram iOS, because that is the shape people
 * already know for "talk to something from a phone", and every deviation from it costs the
 * user a moment of figuring out. What that means concretely:
 *
 * - Messages are bubbles, each standing on its own, with no clock on the bubble itself. Time
 *   appears only where it carries information: a centred separator when the conversation
 *   resumes after a pause, or when it moves to another day.
 * - Consecutive messages from the same side get tighter spacing and only the outer corners
 *   of the group are rounded, so a burst reads as one utterance.
 * - The composer is a single pill that grows with the text. Send never disappears: while the
 *   agent is working it queues the message instead, and a second button stops the current turn
 *   (design.md §33). Replacing send with stop — which is what this used to do — meant a turn
 *   could not be added to without cancelling it first.
 * - Date separators, because a conversation that spans days is unreadable without them.
 *
 * What is *not* copied: Telegram's delivery ticks, read receipts and avatars. Those all mean
 * something specific about a human on the other end, and inventing them for an agent would be
 * the same category of lie as inferring a session status. What replaces them is the one
 * status that is real here — whether the agent is working.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import {
  abortAgentRun,
  agentErrorMessage,
  deleteAgentAttachment,
  fetchAgentAttachment,
  fetchAgentStatus,
  fetchAgentThread,
  isAgentDisabled,
  openAgentSession,
  openAgentStream,
  sendAgentMessage,
  switchAgentBackend,
  uploadAgentAttachment,
} from "../api/agent";
import { UNAUTHORIZED, readToken, storeToken } from "../api/client";
import {
  IconDownload,
  IconFile,
  IconImage,
  IconPlus,
  IconSend,
  IconSettings,
  IconStop,
  IconTerminal,
} from "../components/icons";
import { SettingsSheet } from "../components/SettingsSheet";
import { Markdown } from "../lib/markdown";
import { type Theme } from "../lib/theme";
import type {
  AgentAttachment,
  AgentMessage,
  AgentStatus,
  AgentStreamFrame,
} from "../types/api";

const THREAD_KEY = "csd.agent-thread";
/** Whether to show the agent's reasoning and tool calls. Off by default (spec §5). */
const SHOW_PROCESS_KEY = "csd.agent-show-process";

function readStoredThread(): string | null {
  try {
    return window.localStorage.getItem(THREAD_KEY);
  } catch {
    return null;
  }
}

function storeThread(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(THREAD_KEY, id);
    else window.localStorage.removeItem(THREAD_KEY);
  } catch {
    /* not persisting is fine */
  }
}

function readShowProcess(): boolean {
  try {
    return window.localStorage.getItem(SHOW_PROCESS_KEY) === "1";
  } catch {
    return false;
  }
}

function storeShowProcess(on: boolean): void {
  try {
    window.localStorage.setItem(SHOW_PROCESS_KEY, on ? "1" : "0");
  } catch {
    /* not persisting is fine */
  }
}

/** Above this much quiet, the transcript says when the conversation resumed. */
const SEPARATOR_GAP_MS = 5 * 60 * 1000;

/** One file chosen in the composer, waiting for the message that will carry it. */
interface PendingFile {
  /** Not the filename: two photos can share a name, and the list needs a stable key. */
  key: string;
  file: File;
}

/** The server's per-message cap (`AgentMessageCreate.attachment_ids`). */
const MAX_ATTACHMENTS = 10;

/**
 * A message typed while the agent was working, waiting for the turn to end (design.md §33).
 *
 * It is the page's own state, not the agent's: nothing has been sent, so the agent has no
 * record of it and no id to give it. `key` is local and only has to be stable within this
 * list. Attachments are uploaded the moment the message is queued — a send the user has
 * committed to cannot be left un-uploaded, or closing the page would silently drop the file.
 */
interface QueuedMessage {
  key: string;
  content: string;
  attachmentIds: string[];
  /** File names, so a reloaded page can still say what a queued message will carry. */
  names: string[];
}

/** Queue per conversation: the queue belongs to the agent the words were addressed to. */
const QUEUE_KEY = "csd.agent-queue";

type StoredQueues = Record<string, QueuedMessage[]>;

function readQueues(): StoredQueues {
  try {
    const raw = window.localStorage.getItem(QUEUE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return parsed as StoredQueues;
  } catch {
    // Unparseable is the same as empty: a queue is a convenience, never the record.
    return {};
  }
}

function readQueue(threadId: string | null): QueuedMessage[] {
  if (!threadId) return [];
  const raw: unknown = readQueues()[threadId];
  const stored: unknown[] = Array.isArray(raw) ? raw : [];
  // Rebuilt field by field rather than trusted wholesale: this is storage an older build may
  // have written, and one malformed entry must not take the composer down with it.
  return stored.flatMap((entry) => {
    if (
      entry === null ||
      typeof entry !== "object" ||
      typeof (entry as QueuedMessage).key !== "string" ||
      typeof (entry as QueuedMessage).content !== "string" ||
      !Array.isArray((entry as QueuedMessage).attachmentIds) ||
      !Array.isArray((entry as QueuedMessage).names)
    ) {
      return [];
    }
    const { key, content, attachmentIds, names } = entry as QueuedMessage;
    return [
      {
        key,
        content,
        attachmentIds: attachmentIds.filter((id): id is string => typeof id === "string"),
        names: names.filter((name): name is string => typeof name === "string"),
      },
    ];
  });
}

function writeQueue(threadId: string, items: QueuedMessage[]): void {
  try {
    const all = readQueues();
    if (items.length > 0) all[threadId] = items;
    else delete all[threadId];
    window.localStorage.setItem(QUEUE_KEY, JSON.stringify(all));
  } catch {
    /* not persisting is fine; the queue still works for this session */
  }
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function sameDay(a: Date, b: Date): boolean {
  return a.toDateString() === b.toDateString();
}

/** Telegram groups by day; so does this, for the same reason. */
function dayLabel(iso: string): string {
  const date = new Date(iso);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (sameDay(date, today)) return "Today";
  if (sameDay(date, yesterday)) return "Yesterday";
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    // Without the year, a conversation old enough to cross one reads as if it happened this
    // year — and "Oct 20" is exactly the label most likely to be mistaken that way.
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
  });
}

function timeLabel(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/**
 * The separator to draw above a message, or `null` when the one before it needs no marking.
 *
 * The rule is the chat-UI spec's: within a day, time only after more than five minutes of
 * silence; across days, the date instead of the time (a date and a time stacked over one
 * message says the same thing twice); and nothing at all otherwise, so a burst of messages
 * reads as one moment rather than a log with a clock on every line. The first message is the
 * exception — it always carries its date *and* time, because the start of a transcript is
 * where the reader is trying to work out when any of this happened.
 *
 * The gap is a real elapsed-time comparison, not a difference of clock faces: 23:59 → 00:01
 * is two minutes even though the numbers say otherwise.
 */
function separatorLabel(previous: string | null, iso: string): string | null {
  const date = new Date(iso);
  if (previous === null) return `${dayLabel(iso)} · ${timeLabel(iso)}`;
  const before = new Date(previous);
  if (!sameDay(before, date)) return dayLabel(iso);
  if (date.getTime() - before.getTime() > SEPARATOR_GAP_MS) return timeLabel(iso);
  return null;
}

/** The display name of a backend, in the UI's voice rather than the config's. */
export function backendLabel(name: string): string {
  if (name === "native") return "agenest";
  if (name === "nanobot") return "nanobot";
  return name;
}

export function AgentPage({
  visible,
  onUnreadChange,
  theme,
  onTheme,
  settingsOpen,
  onOpenSettings,
  onCloseSettings,
}: {
  /** Whether this page is the one on screen. Owned by App, which owns the tab bar. */
  visible: boolean;
  /** A reply the user has not seen. Drives the tab's dot; never "the agent is working". */
  onUnreadChange?: (unread: boolean) => void;
  /** Owned by App: the theme governs both pages, so it is not this page's to hold. */
  theme: Theme;
  onTheme: (theme: Theme) => void;
  /** Owned by App, because the dashboard's gear opens this sheet too. */
  settingsOpen: boolean;
  onOpenSettings: () => void;
  onCloseSettings: () => void;
}) {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [threadId, setThreadId] = useState<string | null>(readStoredThread);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  //: The backend a switch is currently in flight for. The transcript is cleared the moment a
  //: switch starts, so without this the pane is blank for the round trip and reads as "my
  //: messages are not syncing" rather than "this is loading".
  const [switchingTo, setSwitchingTo] = useState<string | null>(null);
  const [liveText, setLiveText] = useState("");
  const [liveThink, setLiveThink] = useState("");
  const [liveTool, setLiveTool] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [needsToken, setNeedsToken] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const [draft, setDraft] = useState("");
  // Files chosen but not sent. Uploading starts with the send, not with the picker: a file the
  // user removes again never reaches the server, and the composer has no orphan to clean up.
  const [pending, setPending] = useState<PendingFile[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  // A second Enter can arrive before React has re-rendered with the composer cleared, and two
  // sends would upload the same files twice. So the composer's contents are claimed through
  // refs, synchronously, rather than by refusing the second send: refusing was how a message
  // typed in the second or two the first POST takes was silently dropped. The counter is what
  // keeps the queue's own delivery from overlapping another send.
  const sending = useRef(0);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const imageInput = useRef<HTMLInputElement | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  // Off by default, per spec §5. A ref as well as state because the SSE frame handler is
  // created once and must not close over a stale value.
  const [showProcess, setShowProcess] = useState(readShowProcess);
  const showProcessRef = useRef(showProcess);
  showProcessRef.current = showProcess;

  // What the tab's dot means: a reply arrived that the user has not looked at yet. Not "a run
  // is in flight" — a dot for that could not tell you whether there is anything to read, and
  // it was on for the whole run, which made it permanent background decoration (design.md §33).
  const [unread, setUnread] = useState(false);
  // Read inside the stream handler, which is created once and must not go stale when the page
  // is hidden or shown.
  const visibleRef = useRef(visible);
  visibleRef.current = visible;

  // Messages typed while the agent works, delivered when the turn settles. Paired with the
  // conversation so switching agents shows that agent's queue rather than a stranger's.
  const [queue, setQueue] = useState<{ thread: string | null; items: QueuedMessage[] }>(() => {
    const id = readStoredThread();
    return { thread: id, items: readQueue(id) };
  });
  // Whether the conversation's state is known yet. A reload with a queued message must not
  // release it before `streaming` has been read from the server: the agent may be mid-run, and
  // sending then would be exactly the "talk over it" behaviour the queue exists to avoid.
  const [ready, setReady] = useState(false);

  // Message ids already rendered. The stream and a REST re-fetch overlap by design — the
  // client opens the stream first and then fetches — so the same message legitimately arrives
  // twice. Ids make that free to ignore.
  //
  // **Only ids the agent supplied go in here.** `null` means the backend could not name the
  // message, and adding `null` to the set would make the *first* unnamed message the only one
  // ever rendered — every later one would look like a duplicate of it. An unnamed message is
  // therefore always shown, which can repeat one on a reconnect and can never hide one.
  const seen = useRef<Set<string>>(new Set());
  const scroller = useRef<HTMLDivElement | null>(null);
  const composer = useRef<HTMLTextAreaElement | null>(null);

  const upsert = useCallback((message: AgentMessage) => {
    const id = message.id;
    if (id !== null) {
      if (seen.current.has(id)) return;
      seen.current.add(id);
    }
    setMessages((current) => [...current, message]);
  }, []);

  const loadThread = useCallback(async (id: string) => {
    // Only ask for what the page will draw: tool rows are most of a long transcript's bytes,
    // and the process view is off by default. Turning it on refetches with them.
    const detail = await fetchAgentThread(id, showProcessRef.current);
    seen.current = new Set(
      detail.messages.map((m) => m.id).filter((mid): mid is string => mid !== null),
    );
    setMessages(detail.messages);
    setStreaming(detail.streaming);
    // The live bubble survives a re-fetch **while the turn is still running**. This path is
    // reached on every SSE (re)connect, and a reply that is still streaming is not in the
    // record yet — a message is written when its segment ends, not while it grows.
    // Clearing it therefore made a bubble the user was reading vanish into the typing
    // indicator, and the text then restarted from the *next* delta, losing what had already
    // arrived. A finished turn is the opposite case: its message is in the re-fetched history,
    // so the live copy is cleared there and the recorded one stays.
    if (!detail.streaming) {
      setLiveText("");
    }
    setLiveTool(null);
    setReady(true);
  }, []);

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await fetchAgentStatus());
      setNeedsToken(false);
      setDisabled(false);
      return true;
    } catch (err) {
      if (isAgentDisabled(err)) setDisabled(true);
      else if (err instanceof Error && err.message === UNAUTHORIZED) setNeedsToken(true);
      else setError(agentErrorMessage(err));
      return false;
    }
  }, []);

  // --- the stream ---------------------------------------------------------
  useEffect(() => {
    const handle = openAgentStream({
      onOpen: () => {
        setNeedsToken(false);
        setDisabled(false);
        // Re-fetch on every (re)connect: the stream has no cursor, so it cannot replay what
        // was missed while the connection was down.
        const id = readStoredThread();
        if (id) void loadThread(id).catch(() => undefined);
      },
      onFrame: (frame: AgentStreamFrame) => {
        if (frame.thread_id !== readStoredThread()) return;
        switch (frame.kind) {
          case "agent.delta":
            // `replace` means "this is the whole text", not "add this". A backend that
            // restarts an item (OpenClaw, on item boundaries) sets it; appending would show
            // the answer twice.
            if (frame.channel === "text") {
              setLiveText((t) => (frame.replace ? frame.delta : t + frame.delta));
            } else if (showProcessRef.current) {
              // Reasoning is only accumulated when the user asked to see it. When the setting
              // is off the frames are dropped entirely rather than accumulated and hidden —
              // and the header must not say "thinking…" on the strength of them either, since
              // that would be inferring a state from data we are not showing (design.md §2).
              setLiveThink((t) => (frame.replace ? frame.delta : t + frame.delta));
            }
            break;
          case "agent.message":
            upsert(frame.message);
            if (frame.message.role === "assistant") {
              setLiveText("");
              setLiveThink("");
              // A checkpoint counts, and a tool row does not: the first is the agent talking to
              // the reader, the second is machinery. Marked only when the page is not the one on
              // screen — a message you watched arrive is not unread.
              if (!visibleRef.current) setUnread(true);
            }
            if (frame.message.role === "tool") setLiveTool(null);
            break;
          case "agent.tool":
            if (frame.phase === "start") setLiveTool(frame.tool_name ?? "tool");
            else setLiveTool(null);
            break;
          case "agent.status":
            setStreaming(frame.streaming);
            break;
          case "agent.notice":
            setNotice(frame.detail ? `${frame.notice}: ${frame.detail}` : frame.notice);
            break;
        }
      },
      onError: (err) => {
        if (isAgentDisabled(err)) setDisabled(true);
        else if (err instanceof Error && err.message === UNAUTHORIZED) setNeedsToken(true);
      },
    });
    return () => handle.close();
  }, [loadThread, upsert]);

  // --- initial load -------------------------------------------------------
  /**
   * Resolve the one conversation and load it.
   *
   * There is exactly one session per agent and no way to make a second: the agent's memory
   * lives outside the conversation (docs/design.md §18), so a second thread would be a second,
   * emptier room rather than more space.
   *
   * The server owns which conversation that is, so this asks rather than guesses at it. A
   * remembered id would be the wrong answer after switching agents — a thread talks to the
   * agent it was started on, and this page has to show the conversation belonging to the one
   * that is actually active.
   */
  const boot = useCallback(async () => {
    if (!(await refreshStatus())) return;
    try {
      const session = await openAgentSession();
      storeThread(session.id);
      setThreadId(session.id);
      await loadThread(session.id);
    } catch (err) {
      setError(agentErrorMessage(err));
    }
  }, [loadThread, refreshStatus]);

  useEffect(() => {
    void boot();
  }, [boot]);

  // The active agent can change underneath us (another device, or a restart), so the header
  // is refreshed periodically rather than only on load.
  useEffect(() => {
    const timer = window.setInterval(() => void refreshStatus(), 20000);
    return () => window.clearInterval(timer);
  }, [refreshStatus]);

  // Keep the newest line in view, including while text is still arriving.
  useEffect(() => {
    const node = scroller.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, liveText, liveTool]);

  // Looking at the conversation is what marks it read. There is no per-message receipt to
  // track, and the dot only exists while the user is on the other page anyway.
  useEffect(() => {
    if (visible) setUnread(false);
  }, [visible]);

  useEffect(() => {
    onUnreadChange?.(unread);
  }, [unread, onUnreadChange]);

  // The queue follows the conversation. Loading and writing are separate effects, and the load
  // is keyed on the id the items are *stored under* rather than on `threadId` alone — writing
  // the old agent's items under the new id would hand this agent words meant for another.
  useEffect(() => {
    setQueue((current) =>
      current.thread === threadId ? current : { thread: threadId, items: readQueue(threadId) },
    );
  }, [threadId]);

  useEffect(() => {
    if (queue.thread) writeQueue(queue.thread, queue.items);
  }, [queue]);

  // --- theme --------------------------------------------------------------
  // Applied by App, not here: the theme is written to <html> and governs both pages, so it
  // belongs to the app rather than to whichever page happens to own the switch. This page only
  // forwards it to the settings sheet.

  // --- actions ------------------------------------------------------------
  /** Take what the picker returned, up to the server's per-message cap. */
  const addFiles = useCallback(
    (files: FileList | null) => {
      setMenuOpen(false);
      if (!files || files.length === 0) return;
      const chosen = Array.from(files);
      const room = Math.max(0, MAX_ATTACHMENTS - pending.length);
      if (chosen.length > room) {
        setError(`A message can carry at most ${MAX_ATTACHMENTS} files.`);
      }
      const accepted = chosen.slice(0, room).map((file) => ({
        // Not the filename: two photos can share a name, and two picks of the same file must
        // still be two removable chips.
        key: `${file.name}:${file.size}:${file.lastModified}:${Math.random()
          .toString(36)
          .slice(2)}`,
        file,
      }));
      if (accepted.length > 0) setPending((current) => [...current, ...accepted]);
    },
    [pending.length],
  );

  const removePending = useCallback((key: string) => {
    setPending((current) => current.filter((entry) => entry.key !== key));
  }, []);

  // Tapping anywhere else, or Escape, closes the little menu. A menu that stays open over the
  // transcript is a mode, and this is one action.
  useEffect(() => {
    if (!menuOpen) return;
    const dismiss = (event: Event) => {
      if (event instanceof KeyboardEvent) {
        if (event.key === "Escape") setMenuOpen(false);
        return;
      }
      if (event.target instanceof Element && event.target.closest(".tgcompose")) return;
      setMenuOpen(false);
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", dismiss);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", dismiss);
    };
  }, [menuOpen]);

  // --- derived ------------------------------------------------------------
  // `liveTool` only counts as busy when it is being shown. With the process toggle off, a
  // running tool is invisible, and the header saying "bash…" would be reporting something the
  // user cannot see and did not ask for. Declared here rather than below the actions because
  // both the send path and the queue's release depend on it.
  const busy = streaming || (showProcess && liveTool !== null);

  const send = useCallback(async () => {
    const content = draftRef.current.trim();
    const files = pendingRef.current;
    if (!content && files.length === 0) return;
    // Claimed before the first `await`, so a second Enter in the same tick sees nothing to
    // send rather than uploading the same files again.
    draftRef.current = "";
    pendingRef.current = [];
    sending.current += 1;
    setDraft("");
    setPending([]);
    setError(null);

    // Whether *this* call set `streaming`. A failure on the queue path (an upload that did not
    // land) must not clear it: the turn that was already running is still running, and
    // clearing it would both hide the stop button and release the queue into a live turn.
    let started = false;
    // Uploaded but not yet named by a message. If the send then fails, these are the ids whose
    // bytes nobody will ever point at, so they are cleaned up rather than left on disk.
    const uploaded: string[] = [];
    try {
      // A reload can land here before `boot` resolved the session. Rather than making the
      // send button do nothing until then, ask for it now — the server returns the same
      // conversation either way, so this creates nothing extra.
      let id = threadId;
      if (!id) {
        const session = await openAgentSession();
        id = session.id;
        setThreadId(id);
        storeThread(id);
        seen.current = new Set();
        setMessages([]);
      }

      // Upload first, then send one message naming the files. The agent receives a message, not
      // a stream of half-attached files, and a failure part-way leaves nothing to retract.
      if (files.length > 0) setUploading(true);
      for (const entry of files) {
        const attachment = await uploadAgentAttachment(id, entry.file);
        uploaded.push(attachment.id);
      }
      setUploading(false);

      if (busy) {
        // The agent is mid-run, so this waits its turn instead of replacing the send button
        // with a stop button and leaving the user no way to say the next thing (design.md
        // §33). Nothing is posted yet: the message is ours until the turn settles, which is
        // why it can still be removed, and why `setStreaming` is deliberately not called — the
        // stop button belongs to the turn that is actually running.
        const queued: QueuedMessage = {
          key: `${Date.now()}:${Math.random().toString(36).slice(2)}`,
          content,
          attachmentIds: uploaded,
          names: files.map((entry) => entry.file.name),
        };
        setQueue((current) => {
          // The conversation may have just been resolved above, in which case `current` is
          // still keyed on the old one (or none) and appending blindly would either attach
          // this to the wrong agent or be replaced by the load effect.
          const base = current.thread === id ? current.items : readQueue(id);
          return { thread: id, items: [...base, queued] };
        });
        return;
      }

      started = true;
      setStreaming(true);
      // The server stores and publishes the user's message, but waiting for the round trip
      // through the stream would make the composer feel broken on a slow link, so the
      // returned row is rendered directly. `upsert` makes the later stream copy a no-op.
      upsert(await sendAgentMessage(id, content, uploaded));
    } catch (err) {
      setUploading(false);
      if (started) setStreaming(false);
      // The selection is not lost: a failed send puts the files back *in front of* anything
      // chosen while it was in flight. Uploads are removed only when the server answered with
      // an error — that request never reached the agent. A **transport** failure (`TypeError`)
      // may have been accepted before the connection dropped, and deleting then would break a
      // message that landed; those files stay on disk, orphaned, which costs bytes and nothing
      // else.
      if (files.length > 0) setPending((current) => [...files, ...current]);
      if (!(err instanceof TypeError)) {
        for (const attachmentId of uploaded) {
          void deleteAgentAttachment(attachmentId).catch(() => undefined);
        }
      }
      if (isAgentDisabled(err)) setDisabled(true);
      else if (err instanceof Error && err.message === UNAUTHORIZED) setNeedsToken(true);
      else setError(agentErrorMessage(err));
    } finally {
      sending.current -= 1;
    }
  }, [busy, threadId, upsert]);

  /**
   * Deliver the head of the queue once the agent is no longer working.
   *
   * One message per turn, not the whole queue at once: the next one is a *follow-up*, and
   * sending it while the previous answer is still being written would make two replies
   * interleave. `busy` turning false is the only trigger, so pressing Stop releases the queue
   * — which is the interruption behaviour, and the reason Stop does not clear it.
   */
  useEffect(() => {
    if (!ready || busy || sending.current > 0) return;
    if (queue.thread === null || queue.thread !== threadId) return;
    const item = queue.items[0];
    if (!item) return;

    const thread = queue.thread;
    sending.current += 1;
    setError(null);
    void (async () => {
      try {
        setStreaming(true);
        const message = await sendAgentMessage(thread, item.content, item.attachmentIds);
        upsert(message);
        setQueue((current) => ({
          ...current,
          items: current.items.filter((entry) => entry.key !== item.key),
        }));
      } catch (err) {
        // Kept at the head of the queue: a message the user wrote is not discarded because
        // the agent was unreachable. Removing it stays the user's decision.
        setStreaming(false);
        if (isAgentDisabled(err)) setDisabled(true);
        else if (err instanceof Error && err.message === UNAUTHORIZED) setNeedsToken(true);
        else setError(agentErrorMessage(err));
      } finally {
        sending.current -= 1;
      }
    })();
  }, [busy, queue, ready, threadId, upsert]);

  const stop = useCallback(async () => {
    if (!threadId) return;
    try {
      await abortAgentRun(threadId);
    } catch (err) {
      setError(agentErrorMessage(err));
    }
  }, [threadId]);

  const switchBackend = useCallback(
    async (name: string) => {
      setError(null);
      setSwitchingTo(name);
      try {
        // One request: the switch, the conversation to show, and its messages. The three
        // primitives it composes are sequential — session needs the switch, messages need the
        // session — and each round trip is about a second on a phone link.
        const answer = await switchAgentBackend(name, showProcessRef.current);
        setStatus(answer);
        // The conversation changes with the agent: each keeps its own, because a thread talks
        // to the agent it was started on and continuing it elsewhere would drop the context
        // the new agent never had.
        seen.current = new Set(
          answer.thread.messages
            .map((m) => m.id)
            .filter((mid): mid is string => mid !== null),
        );
        setMessages(answer.thread.messages);
        setStreaming(answer.thread.streaming);
        setLiveText("");
        setLiveThink("");
        setLiveTool(null);
        setThreadId(answer.thread.id);
        storeThread(answer.thread.id);
        // A different agent has said nothing to this one's reader yet, and the queue for it is
        // loaded by the thread-id effect above.
        setUnread(false);
        setReady(true);
      } catch (err) {
        setError(agentErrorMessage(err));
      } finally {
        setSwitchingTo(null);
      }
    },
    [],
  );

  // --- derived ------------------------------------------------------------
  const agentName = backendLabel(status?.backend ?? "agenest");

  /**
   * The transcript: messages with their separators and grouping folded in.
   *
   * Separators are derived here rather than stored, because they are a property of the
   * *pair* — the answer depends on the neighbour on both sides. Deriving them in one pass over
   * the list is what makes a history load, a reconnect that re-fetches, and a live append all
   * produce the same separators with no repair pass.
   *
   * The gap is measured between messages people or the agent actually *said*, not between tool
   * rows: process is what happened during a moment, not a new moment. That also makes the
   * markers independent of the "show agent process" toggle — a display switch must not move
   * where time is reported.
   */
  const transcript = useMemo(() => {
    const visible = messages.filter((m) => !(m.role === "tool" && !showProcess));

    const items: Array<
      | { kind: "sep"; label: string; key: string }
      | { kind: "msg"; message: AgentMessage; key: string }
    > = [];

    let previousAt: string | null = null;
    for (let i = 0; i < visible.length; i += 1) {
      const m = visible[i];

      // The index is part of the key because a live row has no id yet: several of them can be
      // in flight before the next thread re-fetch replaces the whole list, and `m-null` for
      // each of them is a duplicate key React resolves by dropping rows.
      const key = m.id ?? `live-${i}`;

      // A tool row is process: it carries no time marker and does not advance the clock.
      if (m.role !== "tool") {
        const label = separatorLabel(previousAt, m.created_at);
        if (label !== null) items.push({ kind: "sep", label, key: `sep-${key}` });
        previousAt = m.created_at;
      }

      items.push({ kind: "msg", message: m, key: `m-${key}` });
    }
    return items;
  }, [messages, showProcess]);

  // --- render -------------------------------------------------------------
  /**
   * The settings sheet, built here — above the early returns — and rendered into every state
   * below, including the two gates.
   *
   * It has to outlive the pane it is declared in (see the portal note further down), and it
   * also has to outlive the *gate*: it is the app's only settings surface, shared with the
   * dashboard's gear, and nothing in it depends on a personal agent being configured. When a
   * gate returned before the portal, the dashboard's gear — and every gate's — became a dead
   * button on exactly the deployments without `CSD_AGENT_TOKEN`, which are the ones where the
   * theme is all there is to set.
   */
  const sheet = settingsOpen
    ? createPortal(
        <SettingsSheet
          onClose={onCloseSettings}
          theme={theme}
          onTheme={onTheme}
          status={status}
          agentOff={disabled}
          onSwitchBackend={(name) => void switchBackend(name)}
          switchingTo={switchingTo}
          showProcess={showProcess}
          onShowProcess={(on) => {
            setShowProcess(on);
            storeShowProcess(on);
            if (!on) setLiveThink("");
            if (on) {
              // The thread was fetched without tool rows while the toggle was off. Set the
              // ref first: it is what `loadThread` reads, and the state write only lands on
              // the next render.
              showProcessRef.current = on;
              if (threadId) void loadThread(threadId);
            }
          }}
        />,
        document.body,
      )
    : null;

  if (disabled) {
    return (
      <>
        <div className="agent">
          <GateHeader onOpenSettings={onOpenSettings} />
          <div className="agent__gate">
            <h2 className="agent__gate-title">The agent is switched off</h2>
            <p className="agent__gate-body">
              This server has no access token set, so the routes that can <em>do</em> something
              answer 403. That is the default: the agent writes files, runs commands and can reach
              other machines, so it stays off until an access token exists — and reads stay open,
              which is the one place a single token does not mean a single rule.
            </p>
          </div>
        </div>
        {sheet}
      </>
    );
  }

  if (needsToken) {
    return (
      <>
        <AgentTokenPrompt
          onOpenSettings={onOpenSettings}
          onSubmit={(t) => {
            // The same token the dashboard uses, stored in the same place: there is one
            // credential for this deployment, so entering it on either page unlocks both.
            storeToken(t);
            void boot();
          }}
        />
        {sheet}
      </>
    );
  }

  return (
    <div className="agent">
      {/* --- header ---------------------------------------------------- */}
      <header className="tghead">
        <div className="tghead__who">
          <span className="tghead__name">{agentName}</span>
          <span className="tghead__sub">
            {switchingTo ? (
              <>
                <span className="tghead__dot" data-state="working" />
                switching…
              </>
            ) : busy ? (
              <>
                <span className="tghead__dot" data-state="working" />
                {liveTool ? `${liveTool}…` : "typing…"}
              </>
            ) : !status?.connected ? (
              <>
                <span className="tghead__dot" data-state="down" />
                offline
              </>
            ) : null}
          </span>
        </div>

        <button
          className="tghead__side"
          onClick={onOpenSettings}
          aria-label="Settings"
          title="Settings"
        >
          <IconSettings size={21} />
        </button>
      </header>

      {/* --- notices --------------------------------------------------- */}
      {error && (
        <div className="notice notice--bad" role="alert">
          <span>{error}</span>
          <button className="notice__btn" onClick={() => setError(null)} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}
      {notice && (
        <div className="notice notice--warn" role="status">
          <span>{notice}</span>
          <button className="notice__btn" onClick={() => setNotice(null)} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}

      {/* --- transcript ------------------------------------------------ */}
      <div className="tgchat" ref={scroller}>
        {messages.length === 0 && !busy ? (
          <div className="tgempty">
            <p className="tgempty__title">{agentName}</p>
            <p className="tgempty__body">
              {switchingTo
                ? "Switching agents — loading its conversation…"
                : status?.backend === "native"
                  ? `A local agent on this machine${
                      status.workdir ? `, working in ${status.workdir}` : ""
                    }. It can read and edit files and run commands.`
                  : "Your own agent gateway, with its memory and tools."}
            </p>
          </div>
        ) : (
          <div className="tgchat__inner">
            {transcript.map((item) =>
              item.kind === "sep" ? (
                <div key={item.key} className="tgsep">
                  <span>{item.label}</span>
                </div>
              ) : (
                <Bubble key={item.key} message={item.message} />
              ),
            )}

            {/* Reasoning, live. Shown only when asked for; see the delta handler. */}
            {showProcess && liveThink && (
              <details className="bubble bubble--think" open>
                <summary className="bubble__think-head">
                  <span className="bubble__think-label">Thinking</span>
                </summary>
                <div className="bubble__think-body">{liveThink}</div>
              </details>
            )}

            {showProcess && liveTool && (
              <div className="bubble bubble--tool" data-live="true">
                <IconTerminal size={14} />
                <span className="bubble__tool-name">{liveTool}</span>
                <span className="bubble__tool-state">running…</span>
              </div>
            )}

            {liveText && (
              <div className="bubble bubble--in">
                <Markdown text={liveText} />
              </div>
            )}

            {busy && !liveText && !liveTool && (
              /*
               * The gap between "accepted" and "first token". Without this the page looks
               * frozen for the second or two the model spends thinking, and the most likely
               * user response to a frozen page is to press send again.
               */
              <div className="bubble bubble--in bubble--typing" role="status">
                <span className="typing">
                  <i />
                  <i />
                  <i />
                </span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* --- composer -------------------------------------------------- */}
      <form
        className="tgcompose"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        {/* Typed while the agent was working. Above the pill, like the pending files, because
            removing one must be its own tap — and because a queued message is not part of the
            transcript until it has been sent. */}
        {queue.items.length > 0 && (
          <div className="tgcompose__queue" role="status" aria-label="Queued messages">
            {queue.items.map((item) => (
              <span className="queued" key={item.key}>
                <span className="queued__tag">Queued</span>
                <span className="queued__text">
                  {item.content || item.names.join(", ") || "Attachment"}
                </span>
                <button
                  type="button"
                  className="queued__x"
                  onClick={() => {
                    // The upload happened when this was queued, and removing the message is
                    // the one case where nobody will ever name those bytes — unlike a
                    // transport failure, which may have landed.
                    for (const attachmentId of item.attachmentIds) {
                      void deleteAgentAttachment(attachmentId).catch(() => undefined);
                    }
                    setQueue((current) => ({
                      ...current,
                      items: current.items.filter((entry) => entry.key !== item.key),
                    }));
                  }}
                  aria-label="Remove queued message"
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}

        {(pending.length > 0 || uploading) && (
          <div className="tgcompose__pending">
            {pending.map((entry) => (
              <span className="filechip" key={entry.key}>
                {entry.file.type.startsWith("image/") ? (
                  <IconImage size={14} />
                ) : (
                  <IconFile size={14} />
                )}
                <span className="filechip__name">{entry.file.name}</span>
                <span className="filechip__size">{formatSize(entry.file.size)}</span>
                <button
                  type="button"
                  className="filechip__x"
                  onClick={() => removePending(entry.key)}
                  aria-label={`Remove ${entry.file.name}`}
                >
                  ×
                </button>
              </span>
            ))}
            {uploading && <span className="tgcompose__status">uploading…</span>}
          </div>
        )}

        <div className="tgcompose__row">
          <div className="tgcompose__attach">
            <button
              type="button"
              className="tgcompose__add"
              onClick={() => setMenuOpen((open) => !open)}
              aria-label="Attach a file"
              aria-expanded={menuOpen}
              aria-haspopup="menu"
              title="Attach"
            >
              <IconPlus size={20} />
            </button>
            {menuOpen && (
              <div className="attachmenu" role="menu">
                <button
                  type="button"
                  role="menuitem"
                  className="attachmenu__item"
                  onClick={() => imageInput.current?.click()}
                >
                  <IconImage size={17} />
                  <span>Photo</span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="attachmenu__item"
                  onClick={() => fileInput.current?.click()}
                >
                  <IconFile size={17} />
                  <span>File</span>
                </button>
              </div>
            )}
          </div>

          <textarea
            ref={composer}
            className="tgcompose__input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends on a desktop; on a phone the software keyboard's return key inserts
              // a newline, which is what a multiline prompt wants.
              if (e.key === "Enter" && !e.shiftKey && !("ontouchstart" in window)) {
                e.preventDefault();
                void send();
              }
            }}
            placeholder="Message"
            rows={1}
            aria-label={`Message ${agentName}`}
          />
          {/* Two buttons while a turn is running, not one that changes meaning: send stays
              send (it queues), and stop is beside it. Swapping them was what made "say one
              more thing" impossible without cancelling the answer being written. */}
          {busy && (
            <button
              type="button"
              className="tgcompose__btn tgcompose__btn--stop"
              onClick={() => void stop()}
              aria-label="Stop"
              title="Stop"
            >
              <IconStop size={18} />
            </button>
          )}
          <button
            type="submit"
            className="tgcompose__btn"
            disabled={uploading || (!draft.trim() && pending.length === 0)}
            aria-label={busy ? "Queue message" : "Send"}
            title={busy ? "Queue — sent when the agent finishes" : "Send"}
          >
            <IconSend size={18} />
          </button>
        </div>

        {/* Two pickers rather than one: `accept` is what makes a phone offer the camera roll
            for a photo and the file browser for everything else. Hidden and clicked
            programmatically by the menu, which is the only affordance the user sees. */}
        <input
          ref={imageInput}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={(e) => {
            addFiles(e.target.files);
            // Reset so choosing the same file twice still fires a change event.
            e.target.value = "";
          }}
        />
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            addFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </form>

      {/* Portalled into <body> rather than rendered here, because this page is `hidden`
          whenever the dashboard is showing and `display: none` on an ancestor takes the sheet
          down with it. Built above the early returns so the gate states can render it too. */}
      {sheet}
    </div>
  );
}

/**
 * The settings gear for the gate panes — the states that replace the whole page.
 *
 * Those states have no `.tghead`, so without this the agent tab on a deployment whose agent is
 * switched off has no gear at all, and the app's only settings surface becomes unreachable from
 * that tab. The bar is deliberately empty apart from the gear: the header's name and liveness
 * dot describe a running agent, and drawing them here would report a state that does not exist.
 */
function GateHeader({ onOpenSettings }: { onOpenSettings: () => void }) {
  return (
    <header className="tghead">
      <span className="tghead__who" />
      <button
        className="tghead__side"
        onClick={onOpenSettings}
        aria-label="Settings"
        title="Settings"
      >
        <IconSettings size={21} />
      </button>
    </header>
  );
}

/**
 * A tool call's arguments as one line, for the collapsed tool row.
 *
 * The command is the fact worth having, and it is the half that used to be missing: an agent's
 * record held only the output, so a re-read conversation showed that something failed without
 * showing what. `bash` (and anything else that names a `command`) gets it bare, because
 * `{'command': '…'}` is the same information wearing a dictionary. Anything else falls back to
 * its arguments, truncated — an unfamiliar tool's shape still beats a blank.
 */
function toolCommand(args: Record<string, unknown> | null, limit = 120): string {
  if (!args) return "";
  const command = typeof args.command === "string" ? args.command : null;
  let rendered: string;
  if (command !== null) {
    rendered = command;
  } else {
    try {
      rendered = JSON.stringify(args);
    } catch {
      return "";
    }
  }
  // One line: a command containing a newline would otherwise stretch the collapsed row and
  // defeat the point of collapsing it.
  const flat = rendered.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

/**
 * One message.
 *
 * Each bubble stands on its own: there is no same-sender run to tighten. That rule came from
 * Telegram, where consecutive messages from one side are one person typing several lines — but
 * the agent speaks in checkpoints and answers, and gluing those together made a stage note and
 * the answer it introduced read as one block. The spacing between bubbles is now the only
 * grouping signal, and it is a gap rather than a shared corner.
 *
 * A checkpoint (`message.progress`) is drawn as an ordinary incoming bubble too. It used to get
 * a quiet stage line of its own, which was a display only the agents whose protocol carries
 * checkpoints can fill — and an agent that streams one undivided blob left the page looking as
 * if it skipped straight from the question to the answer. One shape for all agent prose is the
 * trade: a checkpoint now reads as a message, and the tool rows below it remain the thing that
 * says work is still happening (docs/design.md §23).
 */
function Bubble({ message }: { message: AgentMessage }) {
  if (message.role === "tool") {
    const command = toolCommand(message.args);
    return (
      <details className="bubble bubble--tool" data-error={message.is_error} open={false}>
        <summary className="bubble__tool-head">
          <IconTerminal size={14} />
          <span className="bubble__tool-name">{message.tool_name ?? "tool"}</span>
          {/* The command, in the collapsed row. It is the whole reason the arguments are kept:
              "bash ✓" says a command ran, "bash pytest tests/ ✓" says which one — and only the
              second is a transcript you can re-read instead of re-running to understand. */}
          {command && <span className="bubble__tool-arg mono">{command}</span>}
          {/* Completed and failed are distinguished by a mark *and* a word, never by colour
              alone — the same rule the dashboard applies to session status. */}
          {message.is_error ? (
            <span className="bubble__tool-err">failed</span>
          ) : (
            <span className="bubble__tool-ok" aria-label="completed">
              ✓
            </span>
          )}
          {/* No timestamp here: a tool row is process, and the surrounding messages carry
              the time. A time on every step would be the noise the spec asks to avoid. */}
        </summary>
        <pre className="bubble__tool-out mono">{message.content}</pre>
      </details>
    );
  }

  const out = message.role === "user";
  const hasText = message.content.trim().length > 0;
  return (
    <div
      className={`bubble ${out ? "bubble--out" : "bubble--in"}`}
      data-out={out}
      data-attachment={message.attachments.length > 0 ? "true" : undefined}
    >
      {/* Only agent prose is Markdown. A user's message is shown as typed — rendering it
          would reinterpret their own words back at them, which is presumptuous and, if they
          pasted markup, wrong. */}
      {hasText &&
        (out ? (
          <span className="bubble__text">{message.content}</span>
        ) : (
          <Markdown text={message.content} />
        ))}
      {message.attachments.length > 0 && (
        <div className="attachments">
          {message.attachments.map((attachment) => (
            <AttachmentCard key={attachment.id} attachment={attachment} />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * One file in the transcript (docs/design.md §29).
 *
 * **The bytes are fetched, not linked.** An `<img src="/api/agent/attachments/…?token=…">`
 * would put the access token — the credential that can run commands — into browser history and
 * every proxy log, which is the same reason the agent stream uses `fetch()`. So the card asks
 * for its blob with a header and shows an object URL.
 *
 * Two actions, because "open" and "keep" are different intentions. Tapping the card hands the
 * file to the OS through the Web Share API — on a phone that is the app chooser, which is the
 * "open with another application" the user means. The arrow downloads it to the device. Where
 * the share API is unavailable (most desktops), opening falls back to a download rather than a
 * button that does nothing.
 */
function AttachmentCard({ attachment }: { attachment: AgentAttachment }) {
  const [blob, setBlob] = useState<Blob | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const isImage = attachment.mime.startsWith("image/");

  useEffect(() => {
    let alive = true;
    let created: string | null = null;
    setBlob(null);
    setUrl(null);
    setLoadError(null);
    fetchAgentAttachment(attachment.id)
      .then((data) => {
        if (!alive) return;
        created = URL.createObjectURL(data);
        setBlob(data);
        setUrl(created);
      })
      .catch((err) => {
        if (alive) setLoadError(agentErrorMessage(err));
      });
    return () => {
      alive = false;
      // Revoking here rather than on every render is what keeps a long transcript from
      // leaking one blob per card; the effect re-runs only when the id changes.
      if (created) URL.revokeObjectURL(created);
    };
  }, [attachment.id]);

  const download = useCallback(() => {
    if (!url) return;
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = attachment.name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }, [url, attachment.name]);

  const open = useCallback(async () => {
    if (!blob) return;
    const file = new File([blob], attachment.name, {
      type: blob.type || attachment.mime,
    });
    if (
      typeof navigator.canShare === "function" &&
      navigator.canShare({ files: [file] })
    ) {
      try {
        await navigator.share({ files: [file], title: attachment.name });
        return;
      } catch (err) {
        // Dismissing the share sheet is a decision, not a failure. Anything else falls back to
        // saving the file, which is still the useful half of "open it somewhere".
        if (err instanceof DOMException && err.name === "AbortError") return;
      }
    }
    download();
  }, [blob, attachment.name, attachment.mime, download]);

  const meta = (
    <>
      <span className="attach__name">{attachment.name}</span>
      <span className="attach__size">{formatSize(attachment.size)}</span>
    </>
  );

  if (isImage) {
    return (
      <div className="attach attach--image">
        <button
          type="button"
          className="attach__image-btn"
          onClick={() => void open()}
          disabled={!blob}
          aria-label={`Open ${attachment.name}`}
        >
          {url ? (
            <img src={url} alt={attachment.name} />
          ) : (
            <span className="attach__placeholder" aria-hidden="true">
              {loadError ? "unavailable" : ""}
            </span>
          )}
        </button>
        <div className="attach__row">
          <span className="attach__text">{meta}</span>
          <button
            type="button"
            className="attach__dl"
            onClick={download}
            disabled={!url}
            aria-label={`Download ${attachment.name}`}
            title="Download"
          >
            <IconDownload size={16} />
          </button>
        </div>
        {loadError && <span className="attach__err">{loadError}</span>}
      </div>
    );
  }

  return (
    <div className="attach">
      <button
        type="button"
        className="attach__body"
        onClick={() => void open()}
        disabled={!blob}
        aria-label={`Open ${attachment.name}`}
      >
        <span className="attach__icon" aria-hidden="true">
          <IconFile size={20} />
        </span>
        <span className="attach__text">{meta}</span>
      </button>
      <button
        type="button"
        className="attach__dl"
        onClick={download}
        disabled={!url}
        aria-label={`Download ${attachment.name}`}
        title="Download"
      >
        <IconDownload size={17} />
      </button>
      {loadError && <span className="attach__err">{loadError}</span>}
    </div>
  );
}

/**
 * Asks for the access token when the server rejects an unauthenticated request.
 *
 * The same token the dashboard asks for, and the same storage: one deployment, one credential
 * (docs/design.md §22). It is still a gate of its own, because this page's routes are the ones
 * that can *do* something — an unconfigured server answers 403 and never reaches this form.
 */
function AgentTokenPrompt({
  onSubmit,
  onOpenSettings,
}: {
  onSubmit: (token: string) => void;
  onOpenSettings: () => void;
}) {
  const [value, setValue] = useState(() => readToken());

  return (
    <div className="agent">
      <GateHeader onOpenSettings={onOpenSettings} />
      <div className="agent__gate">
        <h2 className="agent__gate-title">This page needs its token</h2>
        <p className="agent__gate-body">
          The same one the dashboard uses. Behind it, this page can drive an agent that writes
          files, runs commands, and reaches other machines.
        </p>
        <form
          className="tokenform"
          onSubmit={(e) => {
            e.preventDefault();
            const trimmed = value.trim();
            if (trimmed) onSubmit(trimmed);
          }}
        >
          <input
            className="tokenform__input"
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="access token"
            aria-label="Access token"
            autoComplete="current-password"
          />
          <button className="tokenform__btn" type="submit" disabled={!value.trim()}>
            Unlock
          </button>
        </form>
      </div>
    </div>
  );
}
