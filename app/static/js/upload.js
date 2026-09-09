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
  const cancelBtn = document.getElementById('upload-cancel');
  const messageEl = document.getElementById('upload-message');
  const errorEl = document.getElementById('upload-error');
  const statusSection = document.getElementById('upload-status-section');
  const statusEl = document.getElementById('upload-status');

  const ALLOWED_EXT = ['.mp4', '.mov', '.m4a', '.mp3', '.webm', '.ogg'];
  const CHUNK_SIZE = 8 * 1024 * 1024;
  const STALL_MS = 30000;
  const STALL_CHECK_MS = 5000;

  let activeUpload = null;
  let pendingMediaId = null;
  let wakeLock = null;
  let stallTimer = null;
  let lastProgressAt = 0;
  let lastMediaId = null;
  let cancelled = false;

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

  function setUploading(active) {
    if (submitBtn) {
      submitBtn.disabled = active;
      submitBtn.classList.toggle('is-disabled', active);
    }
    if (fileInput) fileInput.disabled = active;
    dropZone?.classList.toggle('is-disabled', active);
  }

  function resetProgress() {
    if (progressBar) {
      progressBar.value = 0;
      progressBar.removeAttribute('value');
    }
    if (progressText) progressText.textContent = '0%';
    hide(progressWrap);
  }

  function updateProgress(loaded, total) {
    if (!progressBar || !progressText || !total) return;
    const pct = Math.min(100, Math.round((loaded / total) * 100));
    progressBar.value = pct;
    progressBar.setAttribute('aria-valuenow', String(pct));
    progressText.textContent = `${pct}%`;
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

  async function notePreviousUpload(file) {
    if (!file || typeof tus === 'undefined') return;
    try {
      const dummy = new tus.Upload(file, { endpoint: '/files' });
      const previous = await dummy.findPreviousUploads();
      if (previous.length) {
        setMessage('Incomplete upload found for this file. Submit to resume.');
      }
    } catch {
      /* ignore fingerprint lookup errors */
    }
  }

  function handleFileSelected(file) {
    if (!file) {
      clearFilenameDisplay();
      return;
    }
    showFilename(file.name);
    const fileError = validateFile(file);
    if (fileError) {
      setError(fileError);
      setMessage('');
    } else {
      setError('');
      notePreviousUpload(file);
    }
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
    lastProgressAt = Date.now();
    stallTimer = setInterval(() => {
      if (!activeUpload) {
        stopStallWatch();
        return;
      }
      if (Date.now() - lastProgressAt < STALL_MS) return;
      stopStallWatch();
      try {
        upload.abort();
      } catch {
        /* ignore */
      }
      handleFailure('Upload stalled. Submit again to resume from the last chunk.');
    }, STALL_CHECK_MS);
  }

  function handleSuccess(mediaId) {
    stopStallWatch();
    releaseWakeLock();
    resetProgress();
    setUploading(false);
    setError('');
    pendingMediaId = mediaId ?? null;
    setMessage('Upload complete — processing…');
    if (fileInput) fileInput.value = '';
    clearFilenameDisplay();
    refreshUploadStatus();
    activeUpload = null;
  }

  function handleFailure(msg) {
    stopStallWatch();
    releaseWakeLock();
    resetProgress();
    setUploading(false);
    setMessage('');
    setError(msg || 'Upload failed. Please try again.');
    activeUpload = null;
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
    if (!activeUpload) return;
    cancelled = true;
    try {
      activeUpload.abort(true);
    } catch {
      try {
        activeUpload.abort();
      } catch {
        /* ignore */
      }
    }
    handleFailure('Upload cancelled.');
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (activeUpload) return;

    setError('');
    setMessage('');
    pendingMediaId = null;
    lastMediaId = null;
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

    const description = form.elements.namedItem('description')?.value || '';
    const publishedAt = form.elements.namedItem('published_at')?.value || '';

    const upload = new tus.Upload(file, {
      endpoint: '/files',
      chunkSize: CHUNK_SIZE,
      retryDelays: [0, 1000, 3000, 5000],
      storeFingerprintForResuming: true,
      removeFingerprintOnSuccess: true,
      metadata: {
        filename: file.name,
        filetype: file.type || 'application/octet-stream',
        title,
        description,
        published_at: publishedAt,
      },
      onError(error) {
        if (cancelled) {
          handleFailure('Upload cancelled.');
          return;
        }
        handleFailure(errorMessage(error));
      },
      onProgress(bytesUploaded, bytesTotal) {
        lastProgressAt = Date.now();
        updateProgress(bytesUploaded, bytesTotal);
      },
      onAfterResponse(_req, res) {
        const mediaId = res.getHeader('X-Media-Id') || res.getHeader('x-media-id');
        if (mediaId) lastMediaId = mediaId;
      },
      onSuccess() {
        handleSuccess(lastMediaId);
      },
    });

    activeUpload = upload;
    setUploading(true);
    show(progressWrap);
    updateProgress(0, file.size || 1);
    await requestWakeLock();
    startStallWatch(upload);

    try {
      const previous = await upload.findPreviousUploads();
      if (previous.length) {
        upload.resumeFromPreviousUpload(previous[0]);
        setMessage('Resuming previous upload…');
      }
      upload.start();
    } catch (err) {
      handleFailure(err?.message || 'Could not start upload.');
    }
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
