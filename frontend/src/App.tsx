/**
 * App shell.
 *
 * Hash routing on purpose: the dashboard is served at an unknown path behind a private
 * address, and `#/session/<id>` survives a mobile browser refresh and works as a shareable
 * link with no server rewrite rule.
 *
 * The rail is a *docked* column on desktop and an *overlay drawer* on a phone, and the
 * difference is handled in CSS rather than in JS: one `data-rail` attribute on the root,
 * and the media query decides what that means. Doing it in JS would mean measuring the
 * viewport on every resize and re-rendering the whole tree.
 *
 * Rail state and theme both persist, because a sidebar that reopens every reload is a
 * sidebar you learn to ignore.
 */

import { useCallback, useEffect, useState } from "react";

import { isUnauthorized, storeToken, useSessions } from "./api/client";
import { Rail } from "./components/Rail";
import {
  IconMenu,
  IconPanel,
  IconRefresh,
  IconSettings,
} from "./components/icons";
import { TabBar, type AppTab } from "./components/TabBar";
import { applyTheme, readStoredTheme, storeTheme, systemTheme, type Theme } from "./lib/theme";
import { AgentPage } from "./pages/AgentPage";
import { DetailPage } from "./pages/DetailPage";
import { OverviewPage } from "./pages/OverviewPage";

import "./styles.css";
import "./styles-rail.css";
import "./styles-overview.css";
import "./styles-main.css";
import "./styles-agent.css";

/**
 * `agent` is a third route but not a third level of the dashboard: it is a sibling page.
 * Kept in the same tiny hash parser rather than split into its own router, because two route
 * tables in one app is how they drift apart.
 */
type Route = { name: "overview" } | { name: "session"; id: string } | { name: "agent" };

const RAIL_KEY = "csd.rail-collapsed";

function parseHash(): Route {
  const hash = window.location.hash.replace(/^#\/?/, "");
  if (hash === "agent") return { name: "agent" };
  const match = /^session\/(.+)$/.exec(hash);
  return match
    ? { name: "session", id: decodeURIComponent(match[1]) }
    : { name: "overview" };
}

function initialRail(): "expanded" | "collapsed" {
  try {
    const stored = window.localStorage.getItem(RAIL_KEY);
    if (stored === "collapsed") return "collapsed";
    if (stored === "expanded") return "expanded";
  } catch {
    /* fall through to the default below */
  }
  // No stored preference. Start collapsed on a phone: the rail is an overlay there, so
  // opening it by default would cover the dashboard you were trying to look at. On a
  // desktop it is a docked column and the tree is the point of the app, so it starts open.
  return window.matchMedia("(max-width: 760px)").matches ? "collapsed" : "expanded";
}

export default function App() {
  const [route, setRoute] = useState<Route>(parseHash);
  const [rail, setRail] = useState<"expanded" | "collapsed">(initialRail);
  // `null` means "no explicit choice" — the CSS then follows the OS preference.
  const [theme, setTheme] = useState<Theme | null>(readStoredTheme);
  // The OS preference is tracked in state, not read at render time, for one functional
  // reason: the toggle's label and icon must describe what is on screen *now*. Reading
  // `matchMedia` during render would give a value that never updates when the OS flips.
  const [system, setSystem] = useState<Theme>(systemTheme);
  // Reported up by the agent page. It keeps its stream open while you are looking at the
  // dashboard, so a run started on that tab keeps arriving — and this is what lets the tab
  // say so without you having to switch to it and find out.
  const [agentBusy, setAgentBusy] = useState(false);
  const { sessions, hosts, loading, error, reload, connection } = useSessions({ limit: 300 });

  const view: AppTab = route.name === "agent" ? "agent" : "dashboard";

  // What the page is actually showing. Every use of the theme for *display* must go through
  // this, never `theme` directly — see toggleTheme.
  const showing: Theme = theme ?? system;

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    applyTheme(theme);
    if (theme) return;

    // Following the OS means the chrome colour has to be repainted when the OS changes it.
    // The page itself needs no help: `color-scheme: light dark` handles that in CSS.
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onSystemChange = () => {
      setSystem(systemTheme());
      applyTheme(null);
    };
    query.addEventListener("change", onSystemChange);
    return () => query.removeEventListener("change", onSystemChange);
  }, [theme]);

  /**
   * Set the theme from the settings sheet's segmented control.
   *
   * This replaced a one-tap toggle, and the comment above it is why the distinction mattered:
   * a toggle has to compute the *opposite* of what is displayed, so `theme === null` — "no
   * explicit choice, follow the OS" — had to be resolved before flipping or the button went
   * dead for anyone on a dark-mode device who had never chosen. A control that names both
   * options has no opposite to compute: the choice is the value.
   */
  const chooseTheme = useCallback((next: Theme) => {
    storeTheme(next);
    setTheme(next);
  }, []);

  const [settingsOpen, setSettingsOpen] = useState(false);

  const setRailState = useCallback((next: "expanded" | "collapsed") => {
    setRail(next);
    try {
      window.localStorage.setItem(RAIL_KEY, next);
    } catch {
      /* not persisting is fine; the rail still works this session */
    }
  }, []);

  const openSession = useCallback((id: string) => {
    window.location.hash = `#/session/${encodeURIComponent(id)}`;
    // On a phone the rail covers the content, so selecting a session has to dismiss it.
    if (window.matchMedia("(max-width: 760px)").matches) setRailState("collapsed");
  }, [setRailState]);

  const goOverview = useCallback(() => {
    window.location.hash = "#/";
    if (window.matchMedia("(max-width: 760px)").matches) setRailState("expanded");
  }, [setRailState]);

  const selectTab = useCallback(
    (tab: AppTab) => {
      window.location.hash = tab === "agent" ? "#/agent" : "#/";
      // The rail is a dashboard-only affordance; leaving it open behind the agent page would
      // put a session list on top of a conversation the next time you switch back.
      if (window.matchMedia("(max-width: 760px)").matches) setRailState("collapsed");
    },
    [setRailState],
  );

  const activeId = route.name === "session" ? route.id : null;
  const active = activeId ? sessions.find((s) => s.id === activeId) : undefined;

  return (
    <div className="app" data-rail={rail} data-view={view}>
      {/* The rail belongs to the dashboard. The agent page is a conversation, not a list of
          sessions, so it gets the full width instead of a second sidebar nobody asked for. */}
      {view === "dashboard" && (
        <Rail
          sessions={sessions}
          hosts={hosts}
          activeId={activeId}
          connection={connection}
          onSelect={openSession}
          onCollapse={() => setRailState("collapsed")}
        />
      )}

      <div className="main">
        {/*
          Dashboard-only. The agent page has its own header — a chat needs a different one —
          and rendering both stacks two bars, the upper one crumbed "Overview" above somebody's
          conversation. The tab bar is still there, so navigation is not lost.
        */}
        {view === "dashboard" && (
        <header className="topbar">
          {/*
            The way back in, and it sits where the rail was: a collapsed sidebar whose only
            affordance is on the far right reads as "gone", not "hidden".
            Desktop only — on a phone the drawer's own button below does this job.
          */}
          {rail === "collapsed" && (
            <button
              className="iconbtn topbar__expand"
              onClick={() => setRailState("expanded")}
              aria-label="Show session list"
              title="Show session list"
            >
              <IconPanel size={19} />
            </button>
          )}

          <button
            className="iconbtn topbar__menu"
            onClick={() => setRailState("expanded")}
            aria-label="Open session list"
          >
            <IconMenu size={20} />
          </button>

          <div className="topbar__where">
            <span className={`crumb${active ? " crumb--now" : ""}`}>
              {active ? active.agent_type : "Overview"}
            </span>
            {active && (
              <span className="topbar__title" title={active.title}>
                {active.title}
              </span>
            )}
          </div>

          {/* The same gear, in the same corner, as the agent page's. One control in one place
              on both pages — it used to be a moon here and a gear there, which read as two
              settings for one app. */}
          <button
            className="iconbtn"
            onClick={() => setSettingsOpen(true)}
            aria-label="Settings"
            title="Settings"
          >
            <IconSettings size={19} />
          </button>
        </header>
        )}

        {/*
          Dashboard-only notices. The sessions stream runs whoever you are looking at, so
          "connection dropped" on the agent page would be reporting a channel that page does
          not use — a warning about the wrong thing is worse than no warning.
        */}
        {view === "dashboard" &&
          (isUnauthorized(error) ? (
            <TokenPrompt
              onSubmit={(value) => {
                storeToken(value);
                void reload();
              }}
            />
          ) : error ? (
            <div className="notice notice--bad" role="alert">
              <span>Could not reach the server: {error}</span>
              <button
                className="notice__btn"
                onClick={() => void reload()}
                aria-label="Retry"
              >
                <IconRefresh size={15} />
              </button>
            </div>
          ) : null)}

        {view === "dashboard" && connection === "reconnecting" && !error && (
          <div className="notice notice--warn" role="status">
            <span>
              Connection dropped. Showing the last known state while it reconnects.
            </span>
          </div>
        )}

        <div className="pane" hidden={view !== "dashboard"}>
          {route.name === "session" ? (
            <DetailPage sessionId={route.id} onBack={goOverview} />
          ) : loading && sessions.length === 0 ? (
            <p className="empty__b">loading…</p>
          ) : (
            <OverviewPage sessions={sessions} onOpen={openSession} />
          )}
        </div>

        {/*
          Mounted even while the dashboard is showing, and hidden rather than unmounted.
          Two reasons: a run started here keeps streaming while you go look at the sessions
          it is changing, and coming back to the tab is instant instead of a re-fetch.
        */}
        <div className="pane pane--agent" hidden={view !== "agent"}>
          <AgentPage
            onBusyChange={setAgentBusy}
            theme={showing}
            onTheme={chooseTheme}
            settingsOpen={settingsOpen}
            onOpenSettings={() => setSettingsOpen(true)}
            onCloseSettings={() => setSettingsOpen(false)}
          />
        </div>

        <TabBar active={view} onSelect={selectTab} agentBusy={agentBusy} />
      </div>
    </div>
  );
}

/**
 * Asks for the read token when the server rejects an unauthenticated request.
 *
 * This exists for exactly one deployment shape: the dashboard published on a public URL,
 * where `CSD_DASHBOARD_TOKEN` is the only thing between the internet and a list of what you
 * are working on. Because the bundle itself is public in that shape, the token cannot be a
 * build-time constant — so it is entered here and kept in `localStorage`.
 *
 * A `type="password"` field rather than a styled text input: it is a credential, and it is
 * usually typed with someone standing behind you.
 */
function TokenPrompt({ onSubmit }: { onSubmit: (token: string) => void }) {
  const [value, setValue] = useState("");

  return (
    <div className="notice notice--warn" role="status">
      <span>This dashboard needs its read token.</span>
      <form
        className="tokenform"
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = value.trim();
          if (trimmed) onSubmit(trimmed);
        }}
      >
        <input
          className="tokenform__input"
          type="password"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="read token"
          aria-label="Dashboard read token"
          autoComplete="current-password"
          autoFocus
        />
        <button className="tokenform__btn" type="submit" disabled={!value.trim()}>
          Unlock
        </button>
      </form>
    </div>
  );
}
