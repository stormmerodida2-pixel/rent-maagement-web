{% load static %}// Service worker: makes the app installable and shows a friendly page when offline.
// Privacy: it only stores the app's design files (CSS, scripts, images) and the offline page.
// Pages and data always come fresh from the server and are never saved on the phone.
const CACHE = 'static-{{ version }}';
const OFFLINE_URL = '{% url "offline" %}';
const PRECACHE = [OFFLINE_URL, {% for url in precache %}'{{ url }}'{% if not forloop.last %}, {% endif %}{% endfor %}];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === 'navigate') {
    // Pages: always the network; the offline page only if there's no connection.
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)));
    return;
  }
  if (url.pathname.startsWith('{% get_static_prefix %}')) {
    const save = (response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
      }
      return response;
    };
    {% if cache_first %}// Static file names carry a content hash, so a cached copy is always correct.
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request).then(save)));
    {% else %}// File names aren't hashed here (development), so always ask the server; the cache is only for offline.
    event.respondWith(fetch(request).then(save).catch(() => caches.match(request)));
    {% endif %}
  }
  // Everything else (live updates, downloads…) goes straight to the network, uncached.
});
