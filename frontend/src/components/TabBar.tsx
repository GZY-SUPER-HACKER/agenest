/**
 * The app-level tab bar.
 *
 * It sits at the bottom rather than the top because this is a phone-first product: the top of
 * a phone screen is the hardest place to reach one-handed, and the browser chrome already
 * occupies it. On a desktop it stays at the bottom too — a bar that moves depending on the
 * viewport is two navigation models to learn.
 *
 * It carries the dashboard *and* the agent, which is the structural half of this change: the
 * dashboard stops being the whole app and becomes one of two pages.
 */

import { IconBoard, IconChat } from "./icons";

export type AppTab = "dashboard" | "agent";

interface Props {
  active: AppTab;
  onSelect: (tab: AppTab) => void;
  /** A dot on the agent tab when a run is in flight and you are looking at the dashboard. */
  agentBusy?: boolean;
}

const TABS: { id: AppTab; label: string; Icon: typeof IconBoard }[] = [
  { id: "dashboard", label: "Dashboard", Icon: IconBoard },
  { id: "agent", label: "Agent", Icon: IconChat },
];

export function TabBar({ active, onSelect, agentBusy = false }: Props) {
  return (
    <nav className="tabbar" role="tablist" aria-label="Main">
      {TABS.map(({ id, label, Icon }) => (
        <button
          key={id}
          role="tab"
          aria-selected={active === id}
          className="tabbar__tab"
          data-active={active === id}
          onClick={() => onSelect(id)}
        >
          <span className="tabbar__icon">
            <Icon size={20} />
            {id === "agent" && agentBusy && active !== "agent" && (
              <span className="tabbar__dot" aria-hidden="true" />
            )}
          </span>
          <span className="tabbar__label">{label}</span>
        </button>
      ))}
    </nav>
  );
}
