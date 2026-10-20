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
 * - The composer is a single pill that grows with the text, with a circular send button that
 *   becomes a stop button while the agent is working.
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
  fetchAgentStatus,
  fetchAgentThread,
  isAgentDisabled,
  openAgentSession,
  openAgentStream,
  sendAgentMessage,
  switchAgentBackend,
} from "../api/agent";
import { UNAUTHORIZED, readToken, storeToken } from "../api/client";
import { IconSend, IconSettings, IconStop, IconTerminal } from "../components/icons";
import { SettingsSheet } from "../components/SettingsSheet";
import { Markdown } from "../lib/markdown";
import { type Theme } from "../lib/theme";
import type { AgentMessage, AgentStatus, AgentStreamFrame } from "../types/api";

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
  onBusyChange,
  theme,
  onTheme,
  settingsOpen,
  onOpenSettings,
  onCloseSettings,
}: {
  onBusyChange?: (busy: boolean) => void;
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
  // Off by default, per spec §5. A ref as well as state because the SSE frame handler is
  // created once and must not close over a stale value.
  const [showProcess, setShowProcess] = useState(readShowProcess);
  const showProcessRef = useRef(showProcess);
  showProcessRef.current = showProcess;

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

  useEffect(() => {
    onBusyChange?.(streaming || (showProcess && liveTool !== null));
  }, [streaming, liveTool, showProcess, onBusyChange]);

  // --- theme --------------------------------------------------------------
  // Applied by App, not here: the theme is written to <html> and governs both pages, so it
  // belongs to the app rather than to whichever page happens to own the switch. This page only
  // forwards it to the settings sheet.

  // --- actions ------------------------------------------------------------
  const send = useCallback(async () => {
    const content = draft.trim();
    if (!content) return;
    setDraft("");
    setError(null);
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
      setStreaming(true);
      // The server stores and publishes the user's message, but waiting for the round trip
      // through the stream would make the composer feel broken on a slow link, so the
      // returned row is rendered directly. `upsert` makes the later stream copy a no-op.
      upsert(await sendAgentMessage(id, content));
    } catch (err) {
      setStreaming(false);
      if (isAgentDisabled(err)) setDisabled(true);
      else if (err instanceof Error && err.message === UNAUTHORIZED) setNeedsToken(true);
      else setError(agentErrorMessage(err));
    }
  }, [draft, threadId, upsert]);

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
  // `liveTool` only counts as busy when it is being shown. With the process toggle off, a
  // running tool is invisible, and the header saying "bash…" would be reporting something the
  // user cannot see and did not ask for.
  const busy = streaming || (showProcess && liveTool !== null);

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
        {busy ? (
          <button
            type="button"
            className="tgcompose__btn tgcompose__btn--stop"
            onClick={() => void stop()}
            aria-label="Stop"
            title="Stop"
          >
            <IconStop size={18} />
          </button>
        ) : (
          <button
            type="submit"
            className="tgcompose__btn"
            disabled={!draft.trim()}
            aria-label="Send"
            title="Send"
          >
            <IconSend size={18} />
          </button>
        )}
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
  return (
    <div
      className={`bubble ${out ? "bubble--out" : "bubble--in"}`}
      data-out={out}
    >
      {/* Only agent prose is Markdown. A user's message is shown as typed — rendering it
          would reinterpret their own words back at them, which is presumptuous and, if they
          pasted markup, wrong. */}
      {out ? (
        <span className="bubble__text">{message.content}</span>
      ) : (
        <Markdown text={message.content} />
      )}
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
