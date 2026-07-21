/* Minimal service worker for NAS-OS.
   Registers only in a secure context (HTTPS/localhost) — inactive on plain-http LAN.
   Network-first; when offline, serve the shell cache. API and non-GET are never cached. */
const CACHE = "nasos-shell-v79";
const SHELL = ["/", "/desktop.html", "/setup.html", "/icon.svg", "/icon-192.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {})));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.pathname.startsWith("/api/") || u.pathname.startsWith("/ws/")) return;
  // HTML navigation (shell) — always from the network, cache only as an offline fallback.
  // Otherwise the SW would serve a stale page after edits.
  if (e.request.mode === "navigate" || u.pathname.endsWith(".html")) {
    e.respondWith(fetch(e.request).catch(() => caches.match("/desktop.html")));
    return;
  }
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        if (r.ok && u.origin === location.origin) {
          const cp = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, cp)).catch(() => {});
        }
        return r;
      })
      .catch(() => caches.match(e.request).then((m) => m || caches.match("/desktop.html")))
  );
});
