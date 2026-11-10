// Minimaler Service Worker (PWA): cached nur App-Shell/Static, NIE /api.
// Netzwerk zuerst (auch fuer index.html), der Cache dient nur als Rueckfall ohne Verbindung.
const CACHE = 'h2m-shell-v1';
const SHELL = ['/', '/manifest.webmanifest', '/static/app.css', '/static/app.js', '/static/common.js', '/static/i18n.js',
  '/static/history.js', '/static/yields.js', '/static/awards.js', '/static/favicon.svg', '/static/icons/icon-192.png'];
const cacheable = (p) => p === '/' || p === '/manifest.webmanifest' || p.startsWith('/static/') || p.startsWith('/i18n/');

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => Promise.allSettled(SHELL.map((u) => c.add(u)))).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const req = e.request; const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api') || !cacheable(url.pathname)) return;
  e.respondWith(fetch(req).then((res) => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
    return res;
  }).catch(() => caches.match(req).then((r) => r || (req.mode === 'navigate' ? caches.match('/') : Response.error()))));
});
