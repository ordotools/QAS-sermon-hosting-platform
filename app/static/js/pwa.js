const META_KEY = 'qas-offline-meta';
const CACHE_MEDIA = 'qas-media-v1';
const QUOTA_MARGIN = 0.9;

const QASOffline = {
  async registerSW() {
    if (!('serviceWorker' in navigator)) return;
    try {
      await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    } catch (e) {
      console.warn('SW registration failed', e);
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

  async getFileSize(url) {
    const res = await fetch(url, { method: 'HEAD' });
    if (!res.ok) throw new Error('Could not read file size');
    return parseInt(res.headers.get('Content-Length') || '0', 10);
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

  postToSW(message) {
    return new Promise((resolve, reject) => {
      const controller = navigator.serviceWorker?.controller;
      if (!controller) {
        reject(new Error('Service worker not ready'));
        return;
      }
      const channel = new MessageChannel();
      channel.port1.onmessage = (event) => {
        if (event.data?.ok) resolve(event.data);
        else reject(new Error(event.data?.error || 'Operation failed'));
      };
      controller.postMessage(message, [channel.port2]);
    });
  },

  async saveMedia(mediaId, title, btn) {
    if (!navigator.serviceWorker?.controller) {
      alert('Service worker not ready. Refresh and try again.');
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Saving…';
    }
    try {
      const url = this.streamUrl(mediaId);
      const fileSize = await this.getFileSize(url);
      const quotaCheck = await this.checkStorageQuota(fileSize);
      if (!quotaCheck.ok) {
        alert(quotaCheck.message);
        if (btn) {
          btn.disabled = false;
          btn.textContent = btn.dataset.list ? 'Save offline' : 'Save for offline';
        }
        return;
      }

      await this.postToSW({ type: 'CACHE_MEDIA', url, mediaId, title });
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
      if (navigator.serviceWorker?.controller) {
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

  async updatePlayerButtons(mediaId) {
    const saved = await this.isSaved(mediaId);
    document.querySelectorAll(`.save-offline-btn[data-media-id="${mediaId}"]`).forEach((btn) => {
      btn.textContent = saved ? 'Saved offline' : (btn.dataset.list ? 'Save offline' : 'Save for offline');
      btn.disabled = saved;
    });
    document.querySelectorAll(`.remove-offline-btn[data-media-id="${mediaId}"]`).forEach((btn) => {
      btn.classList.toggle('hidden', !saved);
    });
    const status = document.getElementById('offline-status');
    if (status) status.textContent = saved ? 'Available offline' : '';
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

document.addEventListener('DOMContentLoaded', () => {
  QASOffline.registerSW();
  QASOffline.setupOfflineBanner();
  QASOffline.bindButtons();
  QASOffline.renderStorageInfo();
});

window.QASOffline = QASOffline;
