const META_URL = '/__qas_offline_meta__';
const CACHE_MEDIA = 'qas-media-v1';
const QUOTA_MARGIN = 0.9;
const SAVE_TIMEOUT_MS = 5 * 60 * 1000;

const QASOffline = {
  swReady: false,
  registration: null,

  getWorker() {
    return navigator.serviceWorker?.controller || this.registration?.active || null;
  },

  async waitForWorker() {
    if (!('serviceWorker' in navigator)) return false;
    try {
      this.registration = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
      await navigator.serviceWorker.ready;
      return !!this.registration?.active;
    } catch (e) {
      console.warn('SW registration failed', e);
      return false;
    }
  },

  streamUrl(mediaId) {
    return `/stream/${mediaId}`;
  },

  formatBytes(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let n = bytes;
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i += 1;
    }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  },

  async checkStorageQuota(additionalBytes) {
    if (!navigator.storage?.estimate) return { ok: true };
    const { usage = 0, quota = 0 } = await navigator.storage.estimate();
    if (!quota) return { ok: true };
    const needed = usage + additionalBytes;
    if (needed > quota * QUOTA_MARGIN) {
      return {
        ok: false,
        usage,
        quota,
        message: `Not enough storage (${this.formatBytes(usage)} used of ${this.formatBytes(quota)}). Remove offline media or free device space.`,
      };
    }
    return { ok: true, usage, quota };
  },

  postToSW(message, timeoutMs = SAVE_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      const worker = this.getWorker();
      if (!worker) {
        reject(new Error('Service worker not ready. Refresh and try again.'));
        return;
      }
      const channel = new MessageChannel();
      const timer = setTimeout(() => {
        reject(new Error('Save timed out. Try again on a stable connection.'));
      }, timeoutMs);
      channel.port1.onmessage = (event) => {
        clearTimeout(timer);
        if (event.data?.ok) resolve(event.data);
        else reject(new Error(event.data?.error || 'Operation failed'));
      };
      worker.postMessage(message, [channel.port2]);
    });
  },

  async saveMedia(mediaId, title, btn) {
    if (!this.getWorker()) {
      alert('Offline save is still loading. Wait a moment and try again.');
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Saving…';
    }
    try {
      const fileSize = parseInt(btn?.dataset.fileSize || '0', 10);
      if (!fileSize) {
        throw new Error('File size unknown. Refresh the page and try again.');
      }

      const quotaCheck = await this.checkStorageQuota(fileSize);
      if (!quotaCheck.ok) {
        alert(quotaCheck.message);
        if (btn) {
          btn.disabled = false;
          btn.textContent = btn.dataset.list ? 'Save offline' : 'Save for offline';
        }
        return;
      }

      const url = this.streamUrl(mediaId);
      const mediaType = btn?.dataset.mediaType || null;
      const durationSeconds = btn?.dataset.duration ? parseInt(btn.dataset.duration, 10) : null;
      const publishedAt = btn?.dataset.publishedAt || null;
      const hasThumbnail = btn?.dataset.hasThumbnail === '1';
      await this.postToSW({
        type: 'CACHE_MEDIA',
        url,
        mediaId,
        title,
        size: fileSize,
        mediaType,
        durationSeconds,
        publishedAt,
        hasThumbnail,
      });
      if (btn) {
        btn.textContent = 'Saved offline';
        btn.classList.add('is-saved');
        btn.disabled = true;
      }
      await this.updatePlayerButtons(mediaId);
      this.renderStorageInfo();
    } catch (e) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = btn.dataset.list ? 'Save offline' : 'Save for offline';
      }
      alert(e.message || 'Could not save for offline. Check connection and try again.');
    }
  },

  async removeMedia(mediaId) {
    const url = this.streamUrl(mediaId);
    try {
      if (this.getWorker()) {
        await this.postToSW({ type: 'REMOVE_MEDIA', mediaId, url });
      }
      await this.updatePlayerButtons(mediaId);
      await this.renderOfflineList();
      this.renderStorageInfo();
    } catch (e) {
      alert(e.message || 'Could not remove offline media.');
    }
  },

  async getMeta() {
    if (!('caches' in window)) return {};
    const cache = await caches.open(CACHE_MEDIA);
    const res = await cache.match(META_URL);
    if (!res) return {};
    return res.json();
  },

  async isSaved(mediaId) {
    const meta = await this.getMeta();
    return Boolean(meta[String(mediaId)]);
  },

  setSaveButtonState(btn, saved) {
    const onList = btn.dataset.list === '1';
    btn.textContent = saved ? 'Saved offline' : (onList ? 'Save offline' : 'Save for offline');
    btn.disabled = saved || !this.swReady;
    btn.classList.toggle('is-saved', saved);
    btn.title = (!this.swReady && !saved) ? 'Offline save loading…' : '';
  },

  setSaveButtonsReady(ready) {
    this.swReady = ready;
    const hint = ready ? '' : 'Offline save loading…';
    const status = document.getElementById('offline-status');
    if (status && !status.textContent.includes('Available offline')) {
      status.textContent = hint;
    }
    document.querySelectorAll('.save-offline-btn').forEach((btn) => {
      if (btn.classList.contains('is-saved')) return;
      btn.disabled = !ready;
      btn.textContent = ready
        ? (btn.dataset.list ? 'Save offline' : 'Save for offline')
        : 'Loading…';
      btn.title = ready ? '' : hint;
    });
  },

  async updatePlayerButtons(mediaId) {
    const saved = await this.isSaved(mediaId);
    document.querySelectorAll(`.save-offline-btn[data-media-id="${mediaId}"]`).forEach((btn) => {
      this.setSaveButtonState(btn, saved);
    });
    document.querySelectorAll(`.remove-offline-btn[data-media-id="${mediaId}"]`).forEach((btn) => {
      btn.classList.toggle('hidden', !saved);
    });
    const status = document.getElementById('offline-status');
    if (status) status.textContent = saved ? 'Available offline' : (this.swReady ? '' : 'Offline save loading…');
  },

  async renderStorageInfo() {
    const el = document.getElementById('offline-storage-info');
    if (!el) return;

    const meta = await this.getMeta();
    const offlineBytes = Object.values(meta).reduce((sum, item) => sum + (item.size || 0), 0);
    const count = Object.keys(meta).length;

    if (navigator.storage?.estimate) {
      const { usage = 0, quota = 0 } = await navigator.storage.estimate();
      el.textContent = count
        ? `${count} item${count === 1 ? '' : 's'} saved (${this.formatBytes(offlineBytes)}). Device: ${this.formatBytes(usage)} / ${this.formatBytes(quota)} used.`
        : `Device storage: ${this.formatBytes(usage)} / ${this.formatBytes(quota)} used.`;
    } else if (count) {
      el.textContent = `${count} item${count === 1 ? '' : 's'} saved (${this.formatBytes(offlineBytes)}).`;
    } else {
      el.textContent = '';
    }
  },

  async renderOfflineList() {
    const list = document.getElementById('offline-list');
    const empty = document.getElementById('offline-empty');
    if (!list) return;

    const catalog = await loadMediaCatalog();
    let meta = await this.getMeta();
    const { meta: enriched, changed } = mergeMetaWithCatalog(meta, catalog);
    meta = enriched;

    const entries = Object.entries(meta);
    entries.sort(([idA, a], [idB, b]) => {
      const da = publishedAtMs(idA, a, catalog);
      const db = publishedAtMs(idB, b, catalog);
      if (db !== da) return db - da;
      return Number(idB) - Number(idA);
    });
    list.innerHTML = '';

    if (!entries.length) {
      empty?.classList.remove('hidden');
      this.renderStorageInfo();
      return;
    }
    empty?.classList.add('hidden');

    for (const [id, info] of entries) {
      list.insertAdjacentHTML('beforeend', mediaListItemHtml(id, info, { mode: 'offline' }));
    }
    this.bindButtons();
    this.renderStorageInfo();
    void persistEnrichedMeta(enriched, changed);
  },

  setupOfflineBanner() {
    const banner = document.getElementById('offline-banner');
    const update = () => banner?.classList.toggle('hidden', navigator.onLine);
    window.addEventListener('online', update);
    window.addEventListener('offline', update);
    update();
  },

  bindButtons() {
    document.querySelectorAll('.save-offline-btn').forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      if (!btn.dataset.list && btn.closest('.media-list')) btn.dataset.list = '1';
      btn.addEventListener('click', () => {
        if (btn.classList.contains('is-saved')) return;
        this.saveMedia(btn.dataset.mediaId, btn.dataset.title, btn);
      });
    });
    document.querySelectorAll('.remove-offline-btn').forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', () => this.removeMedia(btn.dataset.mediaId));
    });
  },

  async syncSavedButtons() {
    const meta = await this.getMeta();
    for (const btn of document.querySelectorAll('.save-offline-btn')) {
      const saved = Boolean(meta[String(btn.dataset.mediaId)]);
      this.setSaveButtonState(btn, saved);
    }
  },
};

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function formatDuration(seconds) {
  if (!seconds) return '';
  const total = Math.floor(Number(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  return `${minutes}:${String(secs).padStart(2, '0')}`;
}

function formatPublishedDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function catalogFromItems(items) {
  const catalog = {};
  for (const item of items) {
    catalog[String(item.id)] = {
      title: item.title,
      mediaType: item.media_type,
      durationSeconds: item.duration_seconds,
      publishedAt: item.published_at,
      size: item.file_size,
      hasThumbnail: Boolean(item.thumbnail_key),
    };
  }
  return catalog;
}

function getEmbeddedMediaCatalog() {
  const el = document.getElementById('media-catalog');
  if (!el) return {};
  try {
    return catalogFromItems(JSON.parse(el.textContent));
  } catch {
    return {};
  }
}

async function loadMediaCatalog() {
  const embedded = getEmbeddedMediaCatalog();
  if (!navigator.onLine) return embedded;
  try {
    const res = await fetch('/offline/catalog.json', { cache: 'no-store', credentials: 'same-origin' });
    if (!res.ok) return embedded;
    const items = await res.json();
    return { ...embedded, ...catalogFromItems(items) };
  } catch {
    return embedded;
  }
}

function publishedAtMs(id, info, catalog) {
  const raw = info.publishedAt || catalog[String(id)]?.publishedAt;
  if (!raw) return 0;
  const t = new Date(raw).getTime();
  return Number.isFinite(t) ? t : 0;
}

async function persistEnrichedMeta(enriched, changed) {
  if (!changed || !navigator.onLine || !QASOffline.getWorker()) return;
  try {
    await QASOffline.postToSW({ type: 'ENRICH_META', meta: enriched }, 10000);
  } catch (e) {
    console.warn('Could not persist enriched offline meta', e);
  }
}

function enrichMetaEntry(cached, catalogEntry) {
  if (!catalogEntry) return cached;
  const merged = { ...cached };
  if (!merged.title && catalogEntry.title) merged.title = catalogEntry.title;
  if (!merged.mediaType && catalogEntry.mediaType) merged.mediaType = catalogEntry.mediaType;
  if (!merged.durationSeconds && catalogEntry.durationSeconds) {
    merged.durationSeconds = catalogEntry.durationSeconds;
  }
  if (!merged.publishedAt && catalogEntry.publishedAt) merged.publishedAt = catalogEntry.publishedAt;
  if (!merged.hasThumbnail && catalogEntry.hasThumbnail) merged.hasThumbnail = catalogEntry.hasThumbnail;
  if (!merged.size && catalogEntry.size) merged.size = catalogEntry.size;
  return merged;
}

function mergeMetaWithCatalog(meta, catalog) {
  const enriched = {};
  let changed = false;
  for (const [id, info] of Object.entries(meta)) {
    const merged = enrichMetaEntry(info, catalog[id]);
    enriched[id] = merged;
    if (JSON.stringify(merged) !== JSON.stringify(info)) changed = true;
  }
  return { meta: enriched, changed };
}

function mediaListItemHtml(id, info, { mode = 'online', saved = false } = {}) {
  const mediaType = info.mediaType || '';
  const hasThumbnail = info.hasThumbnail;
  const thumb = hasThumbnail
    ? `<img src="/thumbnail/${id}" alt="" class="thumb" loading="lazy">`
    : `<div class="thumb thumb-placeholder">${(mediaType || 'm')[0].toUpperCase()}</div>`;
  const typeBadge = mediaType
    ? `<span class="media-type-badge media-type-${escapeHtml(mediaType)}">${escapeHtml(mediaType)}</span>`
    : '';
  const duration = info.durationSeconds ? formatDuration(info.durationSeconds) : '';
  const durationHtml = duration ? `<span class="media-duration">${duration}</span>` : '';
  const date = info.publishedAt ? formatPublishedDate(info.publishedAt) : '';
  const dateHtml = date ? `<span class="media-date">${date}</span>` : '';
  let actionBtn;
  if (mode === 'offline') {
    actionBtn = `<button type="button" class="btn-secondary btn-sm remove-offline-btn" data-media-id="${id}">Remove</button>`;
  } else {
    const saveLabel = saved ? 'Saved offline' : 'Save offline';
    const saveClass = saved ? 'btn-secondary btn-sm save-offline-btn is-saved' : 'btn-secondary btn-sm save-offline-btn';
    const saveDisabled = saved ? ' disabled' : '';
    const dataAttrs = [
      `data-media-id="${id}"`,
      `data-title="${escapeHtml(info.title)}"`,
      info.size ? `data-file-size="${info.size}"` : '',
      mediaType ? `data-media-type="${escapeHtml(mediaType)}"` : '',
      info.durationSeconds ? `data-duration="${info.durationSeconds}"` : '',
      info.publishedAt ? `data-published-at="${info.publishedAt}"` : '',
      hasThumbnail ? 'data-has-thumbnail="1"' : '',
      'data-list="1"',
    ].filter(Boolean).join(' ');
    actionBtn = `<button type="button" class="${saveClass}" ${dataAttrs}${saveDisabled}>${saveLabel}</button>`;
  }

  return `
    <li class="media-item">
      <a href="/media/${id}" class="media-link">
        ${thumb}
        <div class="media-info">
          <span class="media-title">${escapeHtml(info.title)}</span>
          <span class="media-meta">
            ${typeBadge}
            ${durationHtml}
            ${dateHtml}
          </span>
        </div>
      </a>
      ${actionBtn}
    </li>
  `;
}

async function refreshOfflinePage() {
  if (document.getElementById('offline-list')) {
    await QASOffline.renderOfflineList();
  } else {
    await QASOffline.renderStorageInfo();
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  QASOffline.setupOfflineBanner();
  QASOffline.bindButtons();
  QASOffline.setSaveButtonsReady(false);
  QASOffline.renderStorageInfo();

  navigator.serviceWorker?.addEventListener('controllerchange', async () => {
    QASOffline.setSaveButtonsReady(!!QASOffline.getWorker());
    await QASOffline.syncSavedButtons();
  });

  window.addEventListener('pageshow', async () => {
    await QASOffline.syncSavedButtons();
    await refreshOfflinePage();
  });

  const ready = await QASOffline.waitForWorker();
  QASOffline.setSaveButtonsReady(ready);
  await QASOffline.syncSavedButtons();
  await refreshOfflinePage();
});

window.QASOffline = QASOffline;
