(function () {
  const el = document.getElementById('media-player');
  if (!el || typeof videojs === 'undefined') return;

  const isAudio = el.dataset.mediaType === 'audio';
  const wrap = el.closest('.player-wrap');
  const mediaId = el.dataset.mediaId;

  const RESUME_KEY_PREFIX = 'qas-playback:';
  const MIN_RESUME_SECONDS = 5;
  const END_THRESHOLD_SECONDS = 30;
  const SAVE_INTERVAL_MS = 5000;

  function resumeKey() {
    return mediaId ? `${RESUME_KEY_PREFIX}${mediaId}` : null;
  }

  function readSavedPosition() {
    const key = resumeKey();
    if (!key) return null;
    try {
      const raw = localStorage.getItem(key);
      if (raw == null) return null;
      const time = parseFloat(raw);
      return Number.isFinite(time) && time > 0 ? time : null;
    } catch {
      return null;
    }
  }

  function savePosition(time) {
    const key = resumeKey();
    if (!key || !Number.isFinite(time) || time < MIN_RESUME_SECONDS) return;
    try {
      localStorage.setItem(key, String(time));
    } catch {
      // private mode / quota
    }
  }

  function clearSavedPosition() {
    const key = resumeKey();
    if (!key) return;
    try {
      localStorage.removeItem(key);
    } catch {
      // ignore
    }
  }

  function shouldResume(saved, duration) {
    if (saved == null || saved < MIN_RESUME_SECONDS) return false;
    if (!Number.isFinite(duration) || duration <= 0) return true;
    if (saved >= duration - END_THRESHOLD_SECONDS) return false;
    return saved / duration < 0.95;
  }

  const player = videojs(el, {
    controls: true,
    preload: 'metadata',
    playbackRates: [],
    audioOnlyMode: isAudio,
    playsinline: true,
    fluid: false,
    responsive: true,
  });

  wrap?.addEventListener('contextmenu', (e) => e.preventDefault());

  player.ready(() => {
    el.addEventListener('contextmenu', (e) => e.preventDefault());
    el.setAttribute('controlsList', 'nodownload noplaybackrate');
    if (!isAudio && 'disablePictureInPicture' in el) {
      el.disablePictureInPicture = true;
    }
  });

  if (mediaId) {
    let lastSavedAt = 0;
    let resumeApplied = false;

    const tryResume = () => {
      if (resumeApplied) return;
      const saved = readSavedPosition();
      const duration = player.duration();
      if (!shouldResume(saved, duration)) return;
      resumeApplied = true;
      player.currentTime(saved);
    };

    player.on('loadedmetadata', tryResume);
    player.on('durationchange', tryResume);

    player.on('timeupdate', () => {
      const now = Date.now();
      if (now - lastSavedAt < SAVE_INTERVAL_MS) return;
      lastSavedAt = now;
      savePosition(player.currentTime());
    });

    player.on('pause', () => savePosition(player.currentTime()));
    player.on('ended', clearSavedPosition);
    window.addEventListener('beforeunload', () => savePosition(player.currentTime()));
  }

  if (mediaId && window.QASOffline) {
    QASOffline.updatePlayerButtons(Number(mediaId));
  }
})();
