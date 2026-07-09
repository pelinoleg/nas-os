/* Минимальный service worker для NAS-OS.
   Регистрируется только в secure context (HTTPS/localhost) — на LAN по http не активен.
   Сеть-первично; при офлайне отдаём кэш оболочки. API и не-GET не кэшируем. */
const CACHE = "nasos-shell-v24";
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
  // HTML-навигация (оболочка) — всегда из сети, кэш только как офлайн-фолбэк.
  // Иначе SW отдаёт старую страницу после правок.
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
