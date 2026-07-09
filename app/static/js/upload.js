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

  let activeXhr = null;
  let pendingMediaId = null;

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

  function handleFileSelected(file) {
    if (!file) {
      clearFilenameDisplay();
      return;
    }
    showFilename(file.name);
    const fileError = validateFile(file);
    if (fileError) {
      setError(fileError);
    } else {
      setError('');
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

  function handleSuccess(mediaId) {
    resetProgress();
    setUploading(false);
    setError('');
    pendingMediaId = mediaId ?? null;
    setMessage('Upload complete — processing…');
    if (fileInput) fileInput.value = '';
    clearFilenameDisplay();
    refreshUploadStatus();
    activeXhr = null;
  }

  function handleFailure(msg) {
    resetProgress();
    setUploading(false);
    setMessage('');
    setError(msg || 'Upload failed. Please try again.');
    activeXhr = null;
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
    if (activeXhr) activeXhr.abort();
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (activeXhr) return;

    setError('');
    setMessage('');
    pendingMediaId = null;

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

    const formData = new FormData(form);
    const xhr = new XMLHttpRequest();
    activeXhr = xhr;

    setUploading(true);
    show(progressWrap);
    updateProgress(0, 1);

    xhr.open('POST', form.action || '/upload');
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.responseType = 'text';

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) updateProgress(event.loaded, event.total);
    };

    xhr.onload = () => {
      let data = null;
      const ct = xhr.getResponseHeader('Content-Type') || '';
      if (ct.includes('application/json')) {
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          handleFailure('Invalid server response.');
          return;
        }
      }

      if (xhr.status >= 200 && xhr.status < 300 && data?.ok) {
        handleSuccess(data.media_id);
        return;
      }

      const err =
        data?.error ||
        (xhr.status === 413 ? 'File exceeds the upload size limit.' : null) ||
        `Upload failed (${xhr.status}).`;
      handleFailure(err);
    };

    xhr.onerror = () => handleFailure('Network error during upload.');
    xhr.onabort = () => handleFailure('Upload cancelled.');

    xhr.send(formData);
  });

  document.body.addEventListener('htmx:afterSwap', (event) => {
    const target = event.detail?.target;
    if (!target || target.id !== 'upload-status') return;
    clearPendingIfReady(target);
  });
})();
