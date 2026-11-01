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

// Push notifications (notify.py -> push-service on Sentinel). Every push must
// show a notification, or iOS cancels the subscription.
self.addEventListener('push', (event) => {
  let m = {};
  try { m = event.data ? event.data.json() : {}; } catch (e) { m = { body: event.data ? event.data.text() : '' }; }
  event.waitUntil(self.registration.showNotification(m.title || 'The Undercut Collective', {
    body: m.body || '', tag: m.tag || undefined, icon: 'icons/icon-192.png', data: { url: m.url || '/' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    const w = list.find((c) => 'focus' in c);
    if (w) return (w.navigate ? w.navigate(url).catch(() => w) : Promise.resolve(w)).then((c) => (c || w).focus());
    return self.clients.openWindow(url);
  }));
});
