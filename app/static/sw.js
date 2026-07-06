const CACHE_SHELL = 'qas-shell-v5';
const CACHE_MEDIA = 'qas-media-v1';
const META_KEY = 'qas-offline-meta';

const SHELL_URLS = [
  '/offline',
  '/static/css/app.css',
  '/static/js/pwa.js',
  '/static/js/player.js',
  '/static/vendor/videojs/video.min.js',
  '/static/vendor/videojs/video-js.min.css',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_SHELL).then((cache) => cache.addAll(SHELL_URLS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_SHELL && k !== CACHE_MEDIA).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function streamCacheKey(url) {
  const parsed = new URL(url, self.location.origin);
  return new Request(`${parsed.origin}${parsed.pathname}`);
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  if (url.pathname.startsWith('/stream/')) {
    event.respondWith(cacheFirstStream(request));
    return;
  }

  if (request.method !== 'GET') return;

  if (url.pathname === '/' || url.pathname.startsWith('/media/')) {
    event.respondWith(networkFirst(request));
    return;
  }

  if (url.pathname.startsWith('/static/') || url.pathname === '/offline') {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((res) => {
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE_SHELL).then((c) => c.put(request, clone));
        }
        return res;
      }))
    );
    return;
  }

  event.respondWith(
    fetch(request).catch(() => caches.match(request).then((r) => r || caches.match('/offline')))
  );
});

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_SHELL);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    return cached || caches.match('/offline');
  }
}

async function serveRangeFromCached(response, rangeHeader) {
  const blob = await response.blob();
  const size = blob.size;
  const match = rangeHeader.match(/bytes=(\d*)-(\d*)/);
  if (!match) return response;

  let start = match[1] ? parseInt(match[1], 10) : 0;
  let end = match[2] ? parseInt(match[2], 10) : size - 1;
  end = Math.min(end, size - 1);
  if (start > end || start >= size) {
    return new Response(null, { status: 416 });
  }

  const slice = blob.slice(start, end + 1);
  return new Response(slice, {
    status: 206,
    statusText: 'Partial Content',
    headers: {
      'Content-Type': response.headers.get('Content-Type') || 'application/octet-stream',
      'Content-Length': String(end - start + 1),
      'Content-Range': `bytes ${start}-${end}/${size}`,
      'Accept-Ranges': 'bytes',
    },
  });
}

async function cacheFirstStream(request) {
  const cache = await caches.open(CACHE_MEDIA);
  const key = streamCacheKey(request.url);
  const cached = await cache.match(key);

  if (cached) {
    const rangeHeader = request.headers.get('Range');
    if (rangeHeader) {
      return serveRangeFromCached(cached, rangeHeader);
    }
    return cached;
  }

  try {
    const response = await fetch(request);
    if (response.ok && response.status === 200 && !request.headers.get('Range')) {
      await cache.put(key, response.clone());
    }
    return response;
  } catch {
    return new Response('Offline', { status: 503 });
  }
}

self.addEventListener('message', (event) => {
  const { type } = event.data || {};
  const port = event.ports[0];

  if (type === 'CACHE_MEDIA') {
    const { url, mediaId, title } = event.data;
    event.waitUntil(
      cacheMedia(url, mediaId, title)
        .then(() => port?.postMessage({ ok: true }))
        .catch((err) => port?.postMessage({ ok: false, error: err.message || 'Cache failed' }))
    );
    return;
  }

  if (type === 'REMOVE_MEDIA') {
    const { mediaId, url } = event.data;
    event.waitUntil(
      removeMedia(mediaId, url)
        .then(() => port?.postMessage({ ok: true }))
        .catch((err) => port?.postMessage({ ok: false, error: err.message || 'Remove failed' }))
    );
  }
});

async function cacheMedia(url, mediaId, title) {
  const cache = await caches.open(CACHE_MEDIA);
  const key = streamCacheKey(url);
  const response = await fetch(url);
  if (!response.ok) throw new Error('Fetch failed');
  await cache.put(key, response.clone());

  const meta = await getMeta();
  const size = parseInt(response.headers.get('Content-Length') || '0', 10);
  meta[String(mediaId)] = { title, url, savedAt: Date.now(), size };
  await setMeta(meta);
}

async function removeMedia(mediaId, url) {
  const cache = await caches.open(CACHE_MEDIA);
  await cache.delete(streamCacheKey(url));
  const meta = await getMeta();
  delete meta[String(mediaId)];
  await setMeta(meta);
}

async function getMeta() {
  try {
    const cache = await caches.open(CACHE_MEDIA);
    const res = await cache.match(META_KEY);
    if (!res) return {};
    return await res.json();
  } catch {
    return {};
  }
}

async function setMeta(meta) {
  const cache = await caches.open(CACHE_MEDIA);
  await cache.put(META_KEY, new Response(JSON.stringify(meta), {
    headers: { 'Content-Type': 'application/json' },
  }));
}
