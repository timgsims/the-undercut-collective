// Minimal service worker — exists mainly so Chrome/Android treats this site
// as a genuinely installable PWA (a registered service worker with a fetch
// handler is one of Chrome's install criteria; without one, "Add to Home
// Screen" just creates a bookmark shortcut that opens in a normal browser
// tab with the nav bar, rather than a standalone app window).
//
// Network-first: always tries the network before falling back to cache, so
// this never causes the app to show stale data — it only serves cached
// content when there's genuinely no connection.
const CACHE_NAME = 'undercut-v1';
const PRECACHE_URLS = ['/', '/manifest.json'];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
