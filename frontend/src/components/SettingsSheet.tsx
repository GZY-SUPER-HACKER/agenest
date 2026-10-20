/**
 * Settings — one sheet, one set of sections, opened by the gear on either page.
 *
 * The gear is in the same corner of both pages and opens this same sheet, with the same
 * contents. An app with two settings screens has two places for one preference to drift, and
 * "which agent" is a property of the app, not of the tab you happen to be looking at: the tab
 * bar already shows a live dot for a run started on the other page, so the two tabs are one
 * session with two views rather than two applications.
 *
 * **The sections are always all here.** The sheet is rendered from the agent page, which is the
 * component that owns the agent's state, and mounted through a portal so it survives being
 * opened while the dashboard is showing — the agent pane is `hidden` in that case, and
 * `display: none` on an ancestor would take the sheet down with it.
 *
 * The sheet keeps its Telegram "Chat Info" shape — grouped rows on cards, a close affordance at
 * the top — because that is the form people already read as "settings for the thing I am in".
 */

import { useEffect, useState } from "react";

import { AppearanceGroup } from "./AppearanceGroup";
import { HostAccessGroup } from "./HostAccessGroup";
import { NativeConfigPanel } from "./NativeConfigPanel";
import { IconArrowLeft, IconCheck, IconSettings, IconTerminal } from "./icons";
import { backendLabel } from "../pages/AgentPage";
import type { AgentBackend, AgentStatus } from "../types/api";
import type { Theme } from "../lib/theme";

/**
 * The local agent, for the moment before (or without) a status response.
 *
 * It is a fact of this build rather than a discovery: `native` is compiled in, always
 * constructible, and its settings page is the only way to repair a wrong model or a missing
 * key. Deriving the row purely from the last successful `/api/agent/status` made the screen
 * that fixes a broken agent vanish exactly when the agent — or the token — was what broke:
 * the sheet said "No agents available", and with no row there was no gear either.
 *
 * The lamp is amber because nothing here claims it is ready. That is the honest rendering of
 * "this exists, its state is unknown".
 */
const LOCAL_AGENT: AgentBackend = {
  name: "native",
  active: false,
  ready: false,
  connected: false,
  detail: null,
  registered: false,
  configurable: true,
};

interface Props {
  onClose: () => void;
  theme: Theme;
  onTheme: (theme: Theme) => void;
  status: AgentStatus | null;
  /**
   * The server has no access token set, so there is no agent to describe or switch.
   *
   * Passed rather than inferred from `status === null`, because null is also the state while
   * the first status fetch is in flight on a perfectly healthy deployment — and a section that
   * announced "switched off" during every load would be reporting a state it had not checked.
   */
  agentOff: boolean;
  onSwitchBackend: (name: string) => void;
  /** The backend a switch is in flight for, if any. Feedback matters here: the request takes
   *  a round trip, and a tapped row that looks inert reads as a broken tap. */
  switchingTo: string | null;
  showProcess: boolean;
  onShowProcess: (on: boolean) => void;
}

export function SettingsSheet({
  onClose,
  theme,
  onTheme,
  status,
  agentOff,
  onSwitchBackend,
  switchingTo,
  showProcess,
  onShowProcess,
}: Props) {
  // Escape closes, like every other sheet anyone has ever used.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  /**
   * Which backend's settings sub-page is open, if any.
   *
   * A page inside the sheet rather than a second sheet: the panel is "settings for the thing I
   * am in", which is the same Telegram shape the sheet already has (Chat Info → a group →
   * back). Two stacked modals would also need their own focus and Escape handling.
   */
  const [editing, setEditing] = useState<string | null>(null);

  /** Closing forgets the sub-page, so reopening starts where people expect to land. */
  const close = () => {
    setEditing(null);
    onClose();
  };

  const backends = status?.backends ?? [];
  /** No status yet: the list has not arrived, so only what is compiled in can be drawn. */
  const loading = status === null;
  const rows = backends.length > 0 ? backends : [LOCAL_AGENT];

  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-label="Settings">
      <button className="sheet__scrim" onClick={close} aria-label="Close settings" />
      <div className="sheet__panel">
        <header className="sheet__head">
          {editing !== null && (
            <button
              className="sheet__back"
              onClick={() => setEditing(null)}
              aria-label="Back to settings"
            >
              <IconArrowLeft size={18} />
            </button>
          )}
          <h2 className="sheet__title">
            {editing !== null ? `${backendLabel(editing)} settings` : "Settings"}
          </h2>
          <button className="sheet__close" onClick={close} aria-label="Close">
            ×
          </button>
        </header>

        <div className="sheet__body">
          {editing !== null ? <NativeConfigPanel /> : (<>
          {/* --- which agent ------------------------------------------ */}
          <section className="sheet__group">
            <h3 className="sheet__label">Agent</h3>
            <div className="sheet__card">
              {agentOff ? (
                /* The agent routes all answer 403 here, so the server reports no agents at all.
                   Saying "switched off" is the same fact the agent page's gate states, in this
                   sheet's quieter voice — a section that needs agent data must name the reason
                   it has none rather than show an empty list and let the reader guess. */
                <div className="srow srow--static">
                  <span className="srow__icon">
                    <IconTerminal size={17} />
                  </span>
                  <span className="srow__main">
                    <span className="srow__title">Switched off</span>
                    <span className="srow__sub">
                      This server has no access token set, so no agent is reachable
                    </span>
                  </span>
                </div>
              ) : (
                <>
                  {rows.map((b) => {
                    const usable = b.ready;
                    const switching = switchingTo === b.name;
                    // Reported, never inferred: `ready` and `connected` both come from the
                    // backend. Amber is "not usable right now" — which is the honest lamp for
                    // an agent with no key, no binary, or a gateway that is down — and green
                    // only when it is actually reachable.
                    const lamp = usable && b.connected ? "ok" : "down";
                    return (
                      /* A row with two actions, so it is a div holding two buttons rather than
                         one button: a gear nested inside the row's own button is invalid markup
                         and, worse, a tap that means two things depending on where it lands.
                         `configurable` comes from the backend (§26) — only the agent this
                         service runs itself has a model we may edit. */
                      <div key={b.name} className="srow srow--split">
                        <button
                          className="srow__tap"
                          data-active={b.active}
                          disabled={loading || !usable || switchingTo !== null}
                          onClick={() => onSwitchBackend(b.name)}
                        >
                          <span className="srow__icon">
                            <IconTerminal size={17} />
                          </span>
                          <span className="srow__main">
                            <span className="srow__title">{backendLabel(b.name)}</span>
                            <span className="srow__sub">
                              {/* Feedback first: the switch is a round trip, and a tapped row
                                  that looks inert reads as a broken tap.

                                  `!usable` covers two different situations, and the backend's
                                  own `detail` tells them apart: "no API key" is ours to fix
                                  here, "no agent has registered an endpoint yet" is the agent's
                                  move (§25). The old flat "not installed on this server" was
                                  true of neither and sent readers to install something they
                                  already had. */}
                              {loading
                                ? "reading the agent list…"
                                : switching ? "switching…" : !usable
                                  ? (b.detail ?? "not installed on this server")
                                  : b.connected
                                    ? (b.detail ?? "connected")
                                    : b.registered
                                      ? `registered by the agent${b.detail ? ` — ${b.detail}` : ""}`
                                      : b.name === "nanobot"
                                        ? `not reachable${b.detail ? ` at ${b.detail}` : ""}`
                                        : "ready"}
                            </span>
                          </span>
                          <span className="srow__mark">
                            {/* Two distinct signals, not one: the dot says whether it is up,
                                the check says whether it is the one being talked to. A backend
                                can be active and down, which is exactly when you need both. */}
                            <span className="srow__dot" data-state={lamp} aria-hidden="true" />
                            {b.active && <IconCheck size={16} />}
                          </span>
                        </button>
                        {b.configurable && (
                          <button
                            className="srow__gear"
                            aria-label={`Configure ${backendLabel(b.name)}`}
                            onClick={() => setEditing(b.name)}
                          >
                            <IconSettings size={17} />
                          </button>
                        )}
                      </div>
                    );
                  })}
                </>
              )}
            </div>
            {!agentOff && (
              <p className="sheet__hint">
                Each agent keeps its own conversation, so switching swaps the transcript for that
                agent's. A thread talks to the agent it was started on — continuing it elsewhere
                would drop the context the new agent never had.
              </p>
            )}
          </section>

          {/* --- conversation ----------------------------------------- */}
          <section className="sheet__group">
            <h3 className="sheet__label">Conversation</h3>
            <div className="sheet__card">
              <div className="srow srow--static">
                <span className="srow__main">
                  <span className="srow__title">Show agent process</span>
                  <span className="srow__sub">Reasoning and tool calls, as they happen</span>
                </span>
                <Switch on={showProcess} onChange={onShowProcess} label="Show agent process" />
              </div>
            </div>
            <p className="sheet__hint">
              Off by default. The answer is always shown; this is only about how much of the
              machinery behind it you want to watch.
            </p>
          </section>

          {/* --- what the agent may reach ------------------------------ */}
          <HostAccessGroup />

          <AppearanceGroup theme={theme} onTheme={onTheme} />
          </>)}
        </div>
      </div>
    </div>
  );
}

/**
 * A switch.
 *
 * A real `role="switch"` button rather than a styled checkbox: the row it sits in is already
 * a flex container with its own layout, and a hidden input plus a label would fight it. The
 * accessible name comes from `aria-label`, and `aria-checked` is what actually conveys the
 * state — the fill is a second signal, never the only one.
 */
function Switch({
  on,
  onChange,
  label,
}: {
  on: boolean;
  onChange: (on: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className="switch"
      data-on={on}
      onClick={() => onChange(!on)}
    >
      <span className="switch__knob" />
    </button>
  );
}
