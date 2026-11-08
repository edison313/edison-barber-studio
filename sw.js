const CACHE = "ebs-v1";
self.addEventListener("install", (e) => { self.skipWaiting(); });
self.addEventListener("activate", (e) => { e.waitUntil(clients.claim()); });
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // bookings/schedule always live
  e.respondWith(
    fetch(e.request).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => { try { c.put(e.request, copy); } catch (err) {} });
      return resp;
    }).catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
