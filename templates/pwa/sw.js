/* BlockShare service worker (rendered by web/pwa.py).
 *
 * - Static assets (/static/…) and CDN libraries: stale-while-revalidate.
 * - Page navigations: always from the network; if offline, the cached offline page.
 *   HTML is never cached — pages contain private data and CSRF tokens.
 * - Everything else (POST, uploads, downloads, API, admin): untouched.
 */
const VERSION = "{{ version }}";
const STATIC_CACHE = "bs-static-" + VERSION;
const RUNTIME_CACHE = "bs-runtime-" + VERSION;
const OFFLINE_URL = "{{ offline_url }}";
const STATIC_PREFIX = "{{ static_prefix }}";
const PRECACHE = {{ precache|safe }};
const CDN_HOSTS = ["cdnjs.cloudflare.com", "cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(PRECACHE.map((u) => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keep = [STATIC_CACHE, RUNTIME_CACHE];
    for (const key of await caches.keys()) {
      if (key.startsWith("bs-") && !keep.includes(key)) await caches.delete(key);
    }
    if (self.registration.navigationPreload) await self.registration.navigationPreload.enable();
    await self.clients.claim();
  })());
});

async function staleWhileRevalidate(event, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(event.request);
  const network = fetch(event.request)
    .then((res) => {
      if (res && (res.ok || res.type === "opaque")) cache.put(event.request, res.clone());
      return res;
    })
    .catch(() => cached);
  if (cached) {
    event.waitUntil(network);
    return cached;
  }
  return network;
}

async function networkFirstPage(event) {
  try {
    const preloaded = await event.preloadResponse;
    if (preloaded) return preloaded;
    return await fetch(event.request);
  } catch (err) {
    const offline = await caches.match(OFFLINE_URL);
    return offline || Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    if (req.mode === "navigate") {
      event.respondWith(networkFirstPage(event));
    } else if (url.pathname.startsWith(STATIC_PREFIX)) {
      event.respondWith(staleWhileRevalidate(event, STATIC_CACHE));
    }
    return;
  }
  if (CDN_HOSTS.includes(url.hostname)) {
    event.respondWith(staleWhileRevalidate(event, RUNTIME_CACHE));
  }
});
