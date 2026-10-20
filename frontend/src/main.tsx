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
  window.addEventListener("load", () => {
    void navigator.serviceWorker.register("./sw.js").catch(() => undefined);
  });
}
