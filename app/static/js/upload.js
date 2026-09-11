(function () {
  const form = document.getElementById('upload-form');
  if (!form) return;

  const fileInput = document.getElementById('upload-file');
  const dropZone = document.getElementById('upload-drop-zone');
  const filenameEl = document.getElementById('upload-filename');
  const promptEl = dropZone?.querySelector('.drop-zone-prompt');
  const hintEl = dropZone?.querySelector('.drop-zone-hint');
  const submitBtn = document.getElementById('upload-submit');
  const progressWrap = document.getElementById('upload-progress');
  const progressBar = document.getElementById('upload-progress-bar');
  const progressText = document.getElementById('upload-progress-text');
  const etaEl = document.getElementById('upload-eta');
  const cancelBtn = document.getElementById('upload-cancel');
  const messageEl = document.getElementById('upload-message');
  const errorEl = document.getElementById('upload-error');
  const statusSection = document.getElementById('upload-status-section');
  const statusEl = document.getElementById('upload-status');

  const ALLOWED_EXT = ['.mp4', '.mov', '.m4a', '.mp3', '.webm', '.ogg'];
  const CHUNK_SIZE = 8 * 1024 * 1024;
  const PARALLEL_MIN_SIZE = 16 * 1024 * 1024;
  const STALL_MS = 90000;
  const STALL_CHECK_MS = 5000;
  const STALL_RESUME_MAX = 2;
  const ETA_WINDOW_MS = 8000;
  const ETA_MIN_SPAN_MS = 1000;

  let activeUpload = null;
  let activeFile = null;
  let pendingMediaId = null;
  let wakeLock = null;
  let stallTimer = null;
  let lastProgressAt = 0;
  let lastLoaded = 0;
  let lastTotal = 0;
  let progressSamples = [];
  let cancelled = false;
  let bytesComplete = false;
  let pendingCommit = false;
  let commitInFlight = false;
  let backgroundError = null;
  let startToken = 0;
  let stallResumes = 0;
  let ignoreAbort = false;
  let commitMeta = { title: '', description: '', published_at: '' };

  function show(el) {
    el?.classList.remove('hidden');
  }

  function hide(el) {
    el?.classList.add('hidden');
  }

  function setError(msg) {
    if (!errorEl) return;
    if (msg) {
      errorEl.textContent = msg;
      show(errorEl);
    } else {
      errorEl.textContent = '';
      hide(errorEl);
    }
  }

  function setMessage(msg) {
    if (!messageEl) return;
    if (msg) {
      messageEl.textContent = msg;
      show(messageEl);
    } else {
      messageEl.textContent = '';
      hide(messageEl);
    }
  }

  function clearFilenameDisplay() {
    if (filenameEl) {
      filenameEl.textContent = '';
      hide(filenameEl);
    }
    show(promptEl);
    show(hintEl);
  }

  function showFilename(name) {
    if (!filenameEl) return;
    filenameEl.textContent = name;
    show(filenameEl);
    hide(promptEl);
    hide(hintEl);
  }

  function setSubmitting(active) {
    if (submitBtn) {
      submitBtn.disabled = active;
      submitBtn.classList.toggle('is-disabled', active);
    }
    if (fileInput) fileInput.disabled = active;
    dropZone?.classList.toggle('is-disabled', active);
  }

  function hideEta() {
    progressSamples = [];
    if (!etaEl) return;
    etaEl.textContent = '';
    hide(etaEl);
  }

  function formatEta(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '';
    const s = Math.max(1, Math.round(seconds));
    if (s < 60) return `About ${s} sec left`;
    const min = Math.round(s / 60);
    if (min < 90) return `About ${min} min left`;
    const hr = Math.max(1, Math.round(min / 60));
    return `About ${hr} hr left`;
  }

  function updateEta(loaded, total) {
    if (!etaEl || !pendingCommit || commitInFlight || !total || loaded >= total) {
      hideEta();
      return;
    }
    const now = Date.now();
    progressSamples.push({ t: now, loaded });
    const cutoff = now - ETA_WINDOW_MS;
    progressSamples = progressSamples.filter((sample) => sample.t >= cutoff);
    if (progressSamples.length < 2) {
      hide(etaEl);
      return;
    }
    const first = progressSamples[0];
    const last = progressSamples[progressSamples.length - 1];
    const dt = last.t - first.t;
    const db = last.loaded - first.loaded;
    if (dt < ETA_MIN_SPAN_MS || db <= 0) {
      hide(etaEl);
      return;
    }
    const remainingSec = (total - loaded) / (db / dt) / 1000;
    const text = formatEta(remainingSec);
    if (!text) {
      hide(etaEl);
      return;
    }
    etaEl.textContent = text;
    show(etaEl);
  }

  function resetProgress() {
    if (progressBar) {
      progressBar.value = 0;
      progressBar.removeAttribute('value');
    }
    if (progressText) progressText.textContent = '0%';
    hideEta();
    hide(progressWrap);
  }

  function updateProgress(loaded, total) {
    lastLoaded = loaded;
    lastTotal = total;
    if (!pendingCommit || !progressBar || !progressText || !total) return;
    const pct = Math.min(100, Math.round((loaded / total) * 100));
    progressBar.value = pct;
    progressBar.setAttribute('aria-valuenow', String(pct));
    progressText.textContent = `${pct}%`;
    if (pct >= 100) {
      hideEta();
      stopStallWatch();
      setMessage('Finishing upload…');
      return;
    }
    updateEta(loaded, total);
  }

  function validateFile(file) {
    const name = file.name.toLowerCase();
    const dot = name.lastIndexOf('.');
    const ext = dot >= 0 ? name.slice(dot) : '';
    if (!ALLOWED_EXT.includes(ext)) {
      return 'Unsupported file type. Use mp4, mov, m4a, mp3, webm, or ogg.';
    }
    return null;
  }

  function assignFile(file) {
    if (!fileInput || !file) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
  }

  function sameFile(a, b) {
    return Boolean(
      a &&
        b &&
        a.name === b.name &&
        a.size === b.size &&
        a.lastModified === b.lastModified
    );
  }

  function uploadUrlUid(url) {
    if (!url) return null;
    const marker = '/files/';
    const idx = url.lastIndexOf(marker);
    if (idx < 0) return null;
    const uid = url.slice(idx + marker.length).split(/[?#]/)[0].replace(/\/$/, '');
    return uid || null;
  }

  function previousUploadUrls(prev) {
    if (Array.isArray(prev?.parallelUploadUrls) && prev.parallelUploadUrls.length) {
      return prev.parallelUploadUrls.filter(Boolean);
    }
    return prev?.url ? [prev.url] : [];
  }

  function dropStoredUpload(prev) {
    if (!prev?.urlStorageKey) return;
    try {
      localStorage.removeItem(prev.urlStorageKey);
    } catch {
      /* ignore */
    }
  }

  async function previousUploadAlive(prev) {
    const urls = previousUploadUrls(prev);
    if (!urls.length) return false;
    try {
      const heads = await Promise.all(
        urls.map((url) =>
          fetch(url, {
            method: 'HEAD',
            headers: { 'Tus-Resumable': '1.0.0' },
          })
        )
      );
      return heads.every((res) => res.ok);
    } catch {
      return false;
    }
  }

  function applyCommitMetadata(upload) {
    if (!upload?.options) return;
    upload.options.metadata = {
      ...(upload.options.metadata || {}),
      filename: activeFile?.name || upload.options.metadata?.filename || '',
      filetype: activeFile?.type || upload.options.metadata?.filetype || 'application/octet-stream',
      title: commitMeta.title,
      description: commitMeta.description,
      published_at: commitMeta.published_at,
    };
  }

  // Pause /upload/status while tus chunks are in flight so HTMX polls
  // do not steal the browser's per-host connections (Chrome runtime.lastError
  // on this page is an extension, not tus).
  function setStatusPolling(on) {
    if (!statusEl || typeof htmx === 'undefined') return;
    if (!on) htmx.trigger(statusEl, 'htmx:abort');
    statusEl.setAttribute('hx-trigger', on ? 'every 3s' : 'none');
    htmx.process(statusEl);
  }

  function refreshUploadStatus() {
    if (!statusEl || typeof htmx === 'undefined') return;
    show(statusSection);
    htmx.ajax('GET', '/upload/status', { target: '#upload-status', swap: 'innerHTML' });
  }

  function clearPendingIfReady(statusRoot) {
    if (!pendingMediaId || !statusRoot) return;
    const row = statusRoot.querySelector(`[data-media-id="${pendingMediaId}"]`);
    if (!row || row.querySelector('.badge-processing')) return;
    setMessage('');
    pendingMediaId = null;
  }

  async function requestWakeLock() {
    if (!navigator.wakeLock?.request) return;
    if (wakeLock) return;
    try {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => {
        wakeLock = null;
      });
    } catch {
      wakeLock = null;
    }
  }

  async function releaseWakeLock() {
    const lock = wakeLock;
    wakeLock = null;
    if (!lock) return;
    try {
      await lock.release();
    } catch {
      /* already released */
    }
  }

  function stopStallWatch() {
    if (stallTimer) {
      clearInterval(stallTimer);
      stallTimer = null;
    }
  }

  function startStallWatch(upload) {
    stopStallWatch();
    if (!upload || bytesComplete || commitInFlight) return;
    lastProgressAt = Date.now();
    stallTimer = setInterval(() => {
      if (!activeUpload || !pendingCommit) {
        stopStallWatch();
        return;
      }
      if (bytesComplete || commitInFlight) {
        stopStallWatch();
        return;
      }
      if (Date.now() - lastProgressAt < STALL_MS) return;
      stopStallWatch();
      resumeAfterStall(upload);
    }, STALL_CHECK_MS);
  }

  function resumeAfterStall(upload) {
    if (cancelled || bytesComplete || commitInFlight || !pendingCommit) return;
    stallResumes += 1;
    if (stallResumes > STALL_RESUME_MAX) {
      try {
        upload.abort();
      } catch {
        /* ignore */
      }
      handleFailure('Upload stalled. Submit again to resume from the last chunk.');
      return;
    }
    hideEta();
    setMessage('Upload stalled — resuming…');
    ignoreAbort = true;
    try {
      upload.abort();
    } catch {
      /* ignore */
    }
    queueMicrotask(() => {
      ignoreAbort = false;
    });
    lastProgressAt = Date.now();
    startStallWatch(upload);
    try {
      upload.start();
    } catch (err) {
      ignoreAbort = false;
      handleFailure(err?.message || 'Upload stalled. Submit again to resume from the last chunk.');
    }
  }

  function handleSuccess(mediaId) {
    stopStallWatch();
    releaseWakeLock();
    resetProgress();
    setSubmitting(false);
    setError('');
    pendingMediaId = mediaId ?? null;
    pendingCommit = false;
    commitInFlight = false;
    bytesComplete = false;
    backgroundError = null;
    stallResumes = 0;
    ignoreAbort = false;
    activeUpload = null;
    activeFile = null;
    setMessage('Upload complete. You can leave this page — processing will finish in the background.');
    form.reset();
    clearFilenameDisplay();
    refreshUploadStatus();
    setStatusPolling(true);
  }

  function handleFailure(msg) {
    stopStallWatch();
    releaseWakeLock();
    resetProgress();
    setSubmitting(false);
    pendingCommit = false;
    commitInFlight = false;
    activeUpload = null;
    backgroundError = null;
    stallResumes = 0;
    ignoreAbort = false;
    setMessage('');
    setError(msg || 'Upload failed. Please try again.');
    setStatusPolling(true);
  }

  function errorMessage(error) {
    const body = error?.originalResponse?.getBody?.();
    if (body) {
      try {
        const data = JSON.parse(body);
        if (data?.error) return data.error;
      } catch {
        /* not json */
      }
    }
    if (error?.message) return error.message;
    return 'Network error during upload. Submit again to resume.';
  }

  function invalidateUpload() {
    startToken += 1;
    cancelled = true;
    const upload = activeUpload;
    activeUpload = null;
    activeFile = null;
    bytesComplete = false;
    backgroundError = null;
    pendingCommit = false;
    commitInFlight = false;
    stallResumes = 0;
    ignoreAbort = false;
    lastLoaded = 0;
    lastTotal = 0;
    hideEta();
    stopStallWatch();
    setStatusPolling(true);
    if (!upload) return;
    Promise.resolve(upload.abort(true)).catch(() => {});
  }

  async function commitUpload(token) {
    if (token !== startToken || cancelled || commitInFlight) return;
    const uid = uploadUrlUid(activeUpload?.url);
    if (!uid) {
      handleFailure('Upload finished without a URL.');
      return;
    }
    commitInFlight = true;
    stopStallWatch();
    hideEta();
    setMessage('Finishing upload…');
    try {
      const res = await fetch(`/files/${uid}/commit`, {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(commitMeta),
      });
      if (token !== startToken || cancelled) return;
      if (!res.ok) {
        let msg = 'Could not finish upload.';
        try {
          const data = await res.json();
          if (data?.error) msg = data.error;
        } catch {
          /* ignore */
        }
        handleFailure(msg);
        return;
      }
      const mediaId = res.headers.get('X-Media-Id') || res.headers.get('x-media-id');
      handleSuccess(mediaId);
    } catch {
      if (token !== startToken || cancelled) return;
      handleFailure('Could not finish upload.');
    }
  }

  async function startBackground(file) {
    const token = ++startToken;
    cancelled = true;
    const previous = activeUpload;
    activeUpload = null;
    if (previous) {
      try {
        await previous.abort(true);
      } catch {
        /* ignore */
      }
    }
    if (token !== startToken) return;

    cancelled = false;
    bytesComplete = false;
    backgroundError = null;
    commitInFlight = false;
    stallResumes = 0;
    ignoreAbort = false;
    lastLoaded = 0;
    lastTotal = file.size || 1;
    activeFile = file;

    if (typeof tus === 'undefined') return;

    setStatusPolling(false);

    const upload = new tus.Upload(file, {
      endpoint: '/files',
      chunkSize: CHUNK_SIZE,
      parallelUploads: file.size > PARALLEL_MIN_SIZE ? 2 : 1,
      retryDelays: [1000, 3000, 5000, 10000, 20000],
      storeFingerprintForResuming: true,
      removeFingerprintOnSuccess: true,
      metadata: {
        filename: file.name,
        filetype: file.type || 'application/octet-stream',
      },
      onError(error) {
        if (token !== startToken || cancelled || ignoreAbort) return;
        backgroundError = error;
        if (pendingCommit) {
          handleFailure(errorMessage(error));
        } else {
          setStatusPolling(true);
        }
      },
      onProgress(bytesUploaded, bytesTotal) {
        if (token !== startToken) return;
        lastProgressAt = Date.now();
        updateProgress(bytesUploaded, bytesTotal);
      },
      onSuccess() {
        if (token !== startToken || cancelled) return;
        bytesComplete = true;
        stopStallWatch();
        if (pendingCommit) {
          setMessage('Finishing upload…');
          commitUpload(token);
        } else setStatusPolling(true);
      },
    });

    if (pendingCommit) applyCommitMetadata(upload);

    activeUpload = upload;
    try {
      const found = await upload.findPreviousUploads();
      let resume = null;
      for (const prev of found) {
        if (await previousUploadAlive(prev)) {
          resume = prev;
          break;
        }
        dropStoredUpload(prev);
      }
      if (token !== startToken) {
        try {
          await upload.abort(true);
        } catch {
          /* ignore */
        }
        return;
      }
      if (resume) {
        upload.resumeFromPreviousUpload(resume);
        if (pendingCommit) setMessage('Resuming previous upload…');
      }
      upload.start();
      requestWakeLock();
    } catch (err) {
      if (token !== startToken || cancelled) return;
      backgroundError = err;
      if (pendingCommit) {
        handleFailure(err?.message || 'Could not start upload.');
      } else {
        setStatusPolling(true);
      }
    }
  }

  function handleFileSelected(file) {
    if (!file) {
      invalidateUpload();
      clearFilenameDisplay();
      return;
    }
    showFilename(file.name);
    const fileError = validateFile(file);
    if (fileError) {
      invalidateUpload();
      setError(fileError);
      setMessage('');
      return;
    }
    setError('');
    if (
      activeFile &&
      sameFile(activeFile, file) &&
      activeUpload &&
      !backgroundError
    ) {
      return;
    }
    startBackground(file);
  }

  fileInput?.addEventListener('change', () => {
    handleFileSelected(fileInput.files?.[0] || null);
  });

  dropZone?.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });

  dropZone?.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });

  dropZone?.addEventListener('dragleave', () => {
    dropZone.classList.remove('drag-over');
  });

  dropZone?.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    assignFile(file);
    handleFileSelected(file);
  });

  cancelBtn?.addEventListener('click', () => {
    if (!activeUpload && !pendingCommit) return;
    cancelled = true;
    startToken += 1;
    const upload = activeUpload;
    activeUpload = null;
    try {
      upload?.abort(true);
    } catch {
      try {
        upload?.abort();
      } catch {
        /* ignore */
      }
    }
    handleFailure('Upload cancelled.');
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (pendingCommit) return;

    setError('');
    setMessage('');
    pendingMediaId = null;
    cancelled = false;

    const file = fileInput?.files?.[0];
    if (!file) {
      setError('Please choose a file to upload.');
      return;
    }

    const fileError = validateFile(file);
    if (fileError) {
      setError(fileError);
      return;
    }

    if (typeof tus === 'undefined') {
      setError('Uploader failed to load. Refresh and try again.');
      return;
    }

    const title = (form.elements.namedItem('title')?.value || '').trim();
    if (!title) {
      setError('Title is required.');
      return;
    }

    commitMeta = {
      title,
      description: form.elements.namedItem('description')?.value || '',
      published_at: form.elements.namedItem('published_at')?.value || '',
    };

    pendingCommit = true;
    setSubmitting(true);
    show(progressWrap);
    updateProgress(lastLoaded, lastTotal || file.size || 1);
    await requestWakeLock();

    if (backgroundError || !sameFile(activeFile, file) || !activeUpload) {
      await startBackground(file);
      if (activeUpload && !bytesComplete) startStallWatch(activeUpload);
      else if (bytesComplete) setMessage('Finishing upload…');
      return;
    }

    applyCommitMetadata(activeUpload);
    if (bytesComplete) {
      stopStallWatch();
      setMessage('Finishing upload…');
      await commitUpload(startToken);
      return;
    }
    startStallWatch(activeUpload);
  });

  document.body.addEventListener('htmx:afterSwap', (event) => {
    const target = event.detail?.target;
    if (!target || target.id !== 'upload-status') return;
    clearPendingIfReady(target);
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && activeUpload) {
      requestWakeLock();
    }
  });
})();
