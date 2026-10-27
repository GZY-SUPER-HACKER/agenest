/**
 * The app-shell service worker.
 *
 * Its whole job is that opening the dashboard on a phone does not wait for the network to
 * paint: the HTML, the hashed bundle and the icons are cached, and `/api/*` is never touched.
 * The data is live or it is absent — a cached event stream would be a lie about what the
 * agents are doing, which is the one thing this page must not tell.
 *
 * Two strategies, chosen per request:
 *
 * - **Navigations: network-first.** A deploy must reach an online phone immediately; the
 *   cached shell is only the fallback when there is no network at all.
 * - **Assets: cache-first.** Vite gives every build hashed filenames, so a cached asset can
 *   never be stale — a new build is a new URL, and the old one is simply no longer requested.
 *
 * There is no precache manifest to generate and no build plugin: this file is copied verbatim
 * by Vite, and the shell list is the handful of stable URLs that exist in every build.
 */

// Bumped when the shell's own rules change. `activate` deletes every other cache, so the bump
// is how a previous build's HTML and icons stop occupying the app's storage — the hashed
// assets are already separated by name, which is why an ordinary deploy does not need one.
const CACHE = "csd-shell-v2";
const SHELL = ["./", "./manifest.webmanifest", "./icons/icon-192.png", "./icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(precache().then(() => self.skipWaiting()));
});

/**
 * Cache the shell *and the bundle it references*.
 *
 * `install` runs after the first page load, when the worker was not yet controlling the
 * requests — so without reading the HTML and following its `assets/` links, the first offline
 * launch would paint an index that points at files the cache has never seen. The asset names
 * are hashed and therefore unknowable here ahead of time; reading them from the HTML is the
 * whole precache manifest, without a build plugin.
 */
async function precache() {
  const cache = await caches.open(CACHE);
  const shell = await fetch("./", { cache: "reload" });
  if (!shell.ok) throw new Error(`shell fetch failed: ${shell.status}`);

  const html = await shell.clone().text();
  await cache.put("./", shell);

  const assets = [...html.matchAll(/(?:src|href)="([^"]+)"/g)]
    .map((match) => match[1])
    .filter((url) => url.includes("/assets/"));

  await cache.addAll([...SHELL.filter((url) => url !== "./"), ...assets]);
}

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // The record and the streams. Caching either would make the page confidently wrong.
  if (url.pathname === "/api" || url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request));
    return;
  }

  if (["script", "style", "image", "font"].includes(request.destination)) {
    event.respondWith(cacheFirst(request));
  }
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put(request, response.clone());
    return response;
  } catch {
    return (await cache.match(request)) || (await cache.match("./")) || Response.error();
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;

  const response = await fetch(request);
  if (response.ok) await cache.put(request, response.clone());
  return response;
}
