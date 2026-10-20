/**
 * Theme selection.
 *
 * Three states, two of them visible: no stored choice (follow the OS) and an explicit
 * `light` or `dark`. The CSS does the work — `color-scheme` on <html> picks the side of
 * every `light-dark()` token — so this module only writes the attribute, remembers the
 * choice, and repaints the browser chrome colour that CSS cannot reach.
 *
 * The same key is read by the inline script in index.html before first paint, which is what
 * stops a dark-theme user from getting a white flash on every load.
 */

export type Theme = "light" | "dark";

export const THEME_KEY = "csd.theme";

const CHROME: Record<Theme, string> = { light: "#f5f5f7", dark: "#000000" };

export function readStoredTheme(): Theme | null {
  try {
    const raw = window.localStorage.getItem(THEME_KEY);
    return raw === "light" || raw === "dark" ? raw : null;
  } catch {
    // Private mode: the choice just will not survive a reload.
    return null;
  }
}

export function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** What the page is actually showing right now, explicit choice or not. */
export function resolveTheme(choice: Theme | null): Theme {
  return choice ?? systemTheme();
}

export function applyTheme(choice: Theme | null): void {
  const root = document.documentElement;
  suppressColourTransitions(root);
  if (choice) root.dataset.theme = choice;
  else delete root.dataset.theme;
  paintChrome(resolveTheme(choice));
}

/** Set on <html> for the frame in which the palette changes. See the rule in styles.css. */
const THEMING_CLASS = "theming";

/**
 * A ticket per call, so that a fast double-tap cannot have the first call's cleanup switch
 * transitions back on while the second call's palette is still being painted.
 */
let themingTicket = 0;

/**
 * Switch colour transitions off for exactly the frame that changes the palette.
 *
 * Without this, flipping the theme animates the two elements that define a transition —
 * `.card` and `.chip` — over ~120ms while everything else snaps, which reads as the page
 * tearing rather than as a fade.
 *
 * Two `requestAnimationFrame`s rather than one: the class has to stay applied until the
 * browser has *painted* the new palette, and a single rAF callback can still run before that
 * paint happens.
 */
function suppressColourTransitions(root: HTMLElement): void {
  const ticket = ++themingTicket;
  root.classList.add(THEMING_CLASS);
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (ticket === themingTicket) root.classList.remove(THEMING_CLASS);
    });
  });
}

export function storeTheme(choice: Theme | null): void {
  try {
    if (choice) window.localStorage.setItem(THEME_KEY, choice);
    else window.localStorage.removeItem(THEME_KEY);
  } catch {
    /* not persisting is fine; the theme still applies for this session */
  }
}

function paintChrome(theme: Theme): void {
  for (const meta of document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')) {
    meta.content = CHROME[theme];
  }
}
