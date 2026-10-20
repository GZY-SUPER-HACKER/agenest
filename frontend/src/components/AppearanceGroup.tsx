/**
 * The Appearance group, shared by both settings sheets.
 *
 * Extracted rather than duplicated because it is the one section that belongs to the whole
 * app: the theme is applied to `<html>`, so it governs the dashboard and the agent page at
 * once. Two copies of the control would be two places for it to drift, and the same setting
 * offered two ways on two pages reads as two settings.
 *
 * The `sub` says "Applies to the whole app" for the same reason — it is the honest
 * description of a switch that changes the page you are not looking at.
 */

import { IconMoon, IconSun } from "../components/icons";
import type { Theme } from "../lib/theme";

export function AppearanceGroup({
  theme,
  onTheme,
}: {
  theme: Theme;
  onTheme: (theme: Theme) => void;
}) {
  return (
    <section className="sheet__group">
      <h3 className="sheet__label">Appearance</h3>
      <div className="sheet__card">
        <div className="srow srow--static">
          <span className="srow__icon">
            {theme === "dark" ? <IconMoon size={17} /> : <IconSun size={17} />}
          </span>
          <span className="srow__main">
            <span className="srow__title">Theme</span>
            <span className="srow__sub">Applies to the whole app</span>
          </span>
          <span className="segmented" role="group" aria-label="Theme">
            <button
              className="segmented__opt"
              data-active={theme === "light"}
              onClick={() => onTheme("light")}
            >
              Light
            </button>
            <button
              className="segmented__opt"
              data-active={theme === "dark"}
              onClick={() => onTheme("dark")}
            >
              Dark
            </button>
          </span>
        </div>
      </div>
    </section>
  );
}
