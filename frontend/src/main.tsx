/**
 * Entry point.
 *
 * `StrictMode` is on: it double-invokes effects in development, which is exactly the stress
 * an SSE hook needs — the subscribe/unsubscribe pair must be idempotent or this surfaces it.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { adoptTokenFromFragment } from "./api/client";

// Before anything renders or fetches: a `#token=` link is how a long credential gets onto a
// phone without being typed. Doing it here rather than in a component means the very first
// request already carries it, and the fragment is out of the address bar before the user
// could copy a URL that contains it.
adoptTokenFromFragment();

const container = document.getElementById("root");
if (!container) throw new Error("#root not found in index.html");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Production only. In development the Vite dev server rewrites modules on every save, and a
// service worker in front of it turns hot reload into stale HTML with no obvious cause.
// The offline shell is an enhancement, so a failed registration is swallowed: a browser that
// refuses service workers (private mode, an insecure origin) must still get the app.
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  // `updateViaCache: "none"` keeps the *worker script* out of the HTTP cache. The server sends
  // `Cache-Control: no-cache` for it too, but this is the client-side half: an installed app
  // that stopped noticing releases is exactly a cached `sw.js`.
  const controlled = Boolean(navigator.serviceWorker.controller);

  // A new worker taking control means the shell it installed belongs to a newer build, but
  // `clients.claim()` does not re-execute this page's JavaScript: an already-open home-screen
  // app would keep the previous bundle until it was force-quit. Reload once, so a deploy
  // reaches it now. Guarded on `controlled` so the very first install does not reload the page
  // that just loaded it.
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (controlled) window.location.reload();
  });

  window.addEventListener("load", () => {
    void navigator.serviceWorker
      .register("./sw.js", { updateViaCache: "none" })
      .catch(() => undefined);
  });
}
