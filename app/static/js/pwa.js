const META_KEY = 'qas-offline-meta';
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
      await this.postToSW({ type: 'CACHE_MEDIA', url, mediaId, title, size: fileSize });
      if (btn) btn.textContent = 'Saved';
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
    const res = await cache.match(META_KEY);
    if (!res) return {};
    return res.json();
  },

  async isSaved(mediaId) {
    const meta = await this.getMeta();
    return Boolean(meta[String(mediaId)]);
  },

  setSaveButtonsReady(ready) {
    this.swReady = ready;
    const hint = ready ? '' : 'Offline save loading…';
    const status = document.getElementById('offline-status');
    if (status && !status.textContent.includes('Available offline')) {
      status.textContent = hint;
    }
    document.querySelectorAll('.save-offline-btn').forEach((btn) => {
      if (btn.textContent === 'Saved offline' || btn.textContent === 'Saved') return;
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
      btn.textContent = saved ? 'Saved offline' : (btn.dataset.list ? 'Save offline' : 'Save for offline');
      btn.disabled = saved || !this.swReady;
      btn.title = (!this.swReady && !saved) ? 'Offline save loading…' : '';
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

    const meta = await this.getMeta();
    const entries = Object.entries(meta);
    list.innerHTML = '';

    if (!entries.length) {
      empty?.classList.remove('hidden');
      this.renderStorageInfo();
      return;
    }
    empty?.classList.add('hidden');

    for (const [id, info] of entries) {
      const li = document.createElement('li');
      li.className = 'media-item';
      const sizeLabel = info.size ? ` (${this.formatBytes(info.size)})` : '';
      li.innerHTML = `
        <a href="/media/${id}" class="media-link">
          <div class="media-info">
            <span class="media-title">${escapeHtml(info.title)}${sizeLabel}</span>
          </div>
        </a>
        <button type="button" class="btn-secondary btn-sm" data-remove="${id}">Remove</button>
      `;
      li.querySelector('[data-remove]')?.addEventListener('click', () => this.removeMedia(id));
      list.appendChild(li);
    }
    this.renderStorageInfo();
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
      btn.dataset.list = btn.closest('.media-list') ? '1' : '';
      btn.addEventListener('click', () => {
        this.saveMedia(btn.dataset.mediaId, btn.dataset.title, btn);
      });
    });
    document.querySelectorAll('.remove-offline-btn').forEach((btn) => {
      btn.addEventListener('click', () => this.removeMedia(btn.dataset.mediaId));
    });
  },
};

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

document.addEventListener('DOMContentLoaded', async () => {
  QASOffline.setupOfflineBanner();
  QASOffline.bindButtons();
  QASOffline.setSaveButtonsReady(false);
  QASOffline.renderStorageInfo();

  navigator.serviceWorker?.addEventListener('controllerchange', () => {
    QASOffline.setSaveButtonsReady(!!QASOffline.getWorker());
  });

  const ready = await QASOffline.waitForWorker();
  QASOffline.setSaveButtonsReady(ready);
});

window.QASOffline = QASOffline;
