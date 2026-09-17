/* ==========================================================================
   static/js/ui/player.js
   Popularr built-in audio player.

   Requires: utils/dom.js

   Public API (window.Player):
     Player.playTrack({ id, title, artist, albumArtUrl })
     Player.playQueue([{ id, title, artist, albumArtUrl }, …])
     Player.toggle()
     Player.stop()
     Player.toggleQueue()
     Player.clearQueue()

   ── CHANGES FROM THE PREVIOUS VERSION ─────────────────────────────────────
   1. `clearQueue` RENAMED to `clearPlaybackQueue` internally, and is no
      longer a top-level global.

      downloads.js ALSO defines a global `clearQueue`, for the DOWNLOAD queue
      (`/api/queue/clear`, deleting queued/failed/completed rows). Two
      unrelated destructive actions shared one global name; on any page
      loading both, whichever script parsed last owned it. Which one won
      decided whether "clear the queue" emptied the playback list or wiped
      download history.

      Nothing in the templates calls the player's version by the bare name —
      it is wired to #playerQueueClear via addEventListener — so this rename
      is safe. Rename downloads.js's to `clearDownloadQueue` when you get to
      that file.

   2. `esc()` deleted — a third copy of escapeHtml (after `_atpEsc` and the
      one in unified_search.js). Now from utils/dom.js.

   3. `fmtTime` KEPT, deliberately. It is NOT interchangeable with
      formatDuration from utils/dom.js:
        fmtTime(0)            -> '0:00'      (audio at position zero)
        formatDuration(0)     -> 'N/A'
        fmtTime(15000)        -> '250:00'    (15000 seconds)
        formatDuration(15000) -> '15:00'     (guesses milliseconds)
      The player feeds it `audioEl.currentTime` — plain seconds, frequently
      0 — so the magnitude-guessing version would mis-format both ends.

   4. `playTrack` now takes a SINGLE track object. The old signature was
      `playTrack(trackId, title, artist, albumArtUrl)`, but album_detail.js
      already calls it as
          Player.playTrack({ id, title, artist, albumArtUrl })
      so every positional argument except the first arrived undefined and
      the player bar showed "Unknown" with no artist. Both forms now work.
   ========================================================================== */

(function (global) {
  'use strict';

  // ── State ───────────────────────────────────────────────────────────────
  let queue = [];
  let currentIndex = -1;
  let isVisible = false;

  // ── DOM refs (populated on DOMContentLoaded) ────────────────────────────
  let playerBar, audioEl, artEl, titleEl, artistEl;
  let playPauseBtn, prevBtn, nextBtn;
  let progressEl, progressBar, currentTimeEl, totalTimeEl;
  let volumeEl;
  let queuePanel, queueList, queueCountEl, stopBtn;

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  /**
   * Format a playback position. Plain seconds, and 0 renders as "0:00"
   * rather than "N/A" — see note 3 in the header.
   * @param {number} sec
   * @returns {string}
   */
  function fmtTime(sec) {
    if (!sec || isNaN(sec)) return '0:00';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function setPlayPauseIcons(playing) {
    if (!playPauseBtn) return;
    playPauseBtn.innerHTML = playing
      ? '<i class="bi bi-pause-fill"></i>'
      : '<i class="bi bi-play-fill"></i>';
    playPauseBtn.title = playing ? 'Pause' : 'Play';
  }

  // ── Queue panel ─────────────────────────────────────────────────────────

  function renderQueue() {
    if (!queuePanel) return;
    queueCountEl.textContent = queue.length;

    if (!queue.length) {
      queueList.innerHTML =
        '<div class="text-muted small px-1 py-1">Queue is empty — play an album or artist from any page.</div>';
      return;
    }

    queueList.innerHTML = queue.map(function (track, i) {
      const active = i === currentIndex;
      return '<div class="player-queue-row' + (active ? ' active' : '') + '" data-index="' + i + '"' +
        ' title="' + esc(track.title) + '">' +
        '<span class="text-muted" style="min-width:1.2rem;">' + (i + 1) + '</span>' +
        '<div style="min-width:0;flex:1 1 auto;">' +
          '<div class="text-truncate">' + esc(track.title || 'Unknown') + '</div>' +
          '<div class="text-muted text-truncate" style="font-size:0.72rem;">' +
            esc(track.artist || '') + '</div>' +
        '</div>' +
        (active ? '<i class="bi bi-volume-up-fill"></i>' : '') +
      '</div>';
    }).join('');

    queueList.querySelectorAll('.player-queue-row').forEach(function (row) {
      row.addEventListener('click', function () {
        loadAndPlay(parseInt(row.dataset.index, 10));
      });
    });
  }

  function toggleQueue() {
    if (!queuePanel) return;
    queuePanel.classList.toggle('d-none');
    if (!queuePanel.classList.contains('d-none')) renderQueue();
  }

  /** Empty the PLAYBACK queue. Not the download queue — see header note 1. */
  function clearPlaybackQueue() {
    queue = [];
    currentIndex = -1;
    renderQueue();
  }

  // ── Core playback ───────────────────────────────────────────────────────

  function loadAndPlay(index) {
    if (index < 0 || index >= queue.length) return;
    currentIndex = index;
    const track = queue[index];

    audioEl.src = `/api/track/${encodeURIComponent(track.id)}/audio`;
    audioEl.load();
    audioEl.play().catch(function (err) {
      console.warn('[Player] play() rejected:', err);
    });

    titleEl.textContent = track.title || 'Unknown';
    artistEl.textContent = track.artist || '';
    artEl.src = track.albumArtUrl || '';
    artEl.style.display = track.albumArtUrl ? 'block' : 'none';

    renderQueue();
    if (!isVisible) show();
    highlightActiveTrack(track.id);
  }

  function show() {
    playerBar.classList.remove('d-none');
    document.body.style.paddingBottom = '72px';
    // Lifts the sticky scan-status bar above the player.
    document.body.classList.add('player-visible');
    isVisible = true;
  }

  function hide() {
    playerBar.classList.add('d-none');
    document.body.style.paddingBottom = '';
    document.body.classList.remove('player-visible');
    isVisible = false;
  }

  function stopPlayback() {
    audioEl.pause();
    audioEl.removeAttribute('src');
    audioEl.load();
    queue = [];
    currentIndex = -1;
    highlightActiveTrack(null);
    renderQueue();
    hide();
  }

  function togglePlayPause() {
    if (!audioEl) return;
    if (audioEl.paused) {
      if (audioEl.src) audioEl.play();
    } else {
      audioEl.pause();
    }
  }

  function highlightActiveTrack(trackId) {
    document.querySelectorAll('.player-play-btn').forEach(function (btn) {
      const active = String(btn.dataset.trackId) === String(trackId);
      btn.classList.toggle('btn-success', active);
      btn.classList.toggle('btn-outline-success', !active);
      const icon = btn.querySelector('i');
      if (icon) icon.className = active ? 'bi bi-volume-up-fill' : 'bi bi-play-fill';
    });
  }

  // ── Event wiring ────────────────────────────────────────────────────────

  function bindAudioEvents() {
    audioEl.addEventListener('play', function () { setPlayPauseIcons(true); });
    audioEl.addEventListener('pause', function () { setPlayPauseIcons(false); });

    audioEl.addEventListener('ended', function () {
      if (currentIndex < queue.length - 1) {
        loadAndPlay(currentIndex + 1);
      } else {
        setPlayPauseIcons(false);
        highlightActiveTrack(null);
      }
    });

    audioEl.addEventListener('timeupdate', function () {
      if (!audioEl.duration) return;
      progressBar.style.width = ((audioEl.currentTime / audioEl.duration) * 100) + '%';
      currentTimeEl.textContent = fmtTime(audioEl.currentTime);
    });

    audioEl.addEventListener('loadedmetadata', function () {
      totalTimeEl.textContent = fmtTime(audioEl.duration);
    });

    audioEl.addEventListener('error', function () {
      titleEl.textContent = 'Playback error';
      setPlayPauseIcons(false);
    });
  }

  function bindProgressClick() {
    progressEl.addEventListener('click', function (e) {
      if (!audioEl.duration) return;
      const rect = progressEl.getBoundingClientRect();
      audioEl.currentTime = ((e.clientX - rect.left) / rect.width) * audioEl.duration;
    });
  }

  function bindVolumeControl() {
    volumeEl.addEventListener('input', function () {
      audioEl.volume = volumeEl.value;
    });
  }

  function bindControlButtons() {
    playPauseBtn.addEventListener('click', togglePlayPause);
    prevBtn.addEventListener('click', function () {
      if (currentIndex > 0) loadAndPlay(currentIndex - 1);
    });
    nextBtn.addEventListener('click', function () {
      if (currentIndex < queue.length - 1) loadAndPlay(currentIndex + 1);
    });
    stopBtn.addEventListener('click', stopPlayback);
  }

  // ── Init ────────────────────────────────────────────────────────────────

  function init() {
    playerBar     = document.getElementById('globalPlayerBar');
    audioEl       = document.getElementById('globalAudioEl');
    artEl         = document.getElementById('playerArt');
    titleEl       = document.getElementById('playerTitle');
    artistEl      = document.getElementById('playerArtist');
    playPauseBtn  = document.getElementById('playerPlayPause');
    prevBtn       = document.getElementById('playerPrev');
    nextBtn       = document.getElementById('playerNext');
    progressEl    = document.getElementById('playerProgress');
    progressBar   = document.getElementById('playerProgressBar');
    currentTimeEl = document.getElementById('playerCurrentTime');
    totalTimeEl   = document.getElementById('playerTotalTime');
    volumeEl      = document.getElementById('playerVolume');
    queuePanel    = document.getElementById('playerQueuePanel');
    queueList     = document.getElementById('playerQueueList');
    queueCountEl  = document.getElementById('playerQueueCount');
    stopBtn       = document.getElementById('playerStop');

    if (!playerBar || !audioEl) return;

    const queueClearBtn = document.getElementById('playerQueueClear');
    if (queueClearBtn) queueClearBtn.addEventListener('click', clearPlaybackQueue);

    const queueToggleBtn = document.getElementById('playerQueueToggle');
    if (queueToggleBtn) queueToggleBtn.addEventListener('click', toggleQueue);

    bindAudioEvents();
    bindProgressClick();
    bindVolumeControl();
    bindControlButtons();

    isVisible = !playerBar.classList.contains('d-none');
    document.body.classList.toggle('player-visible', isVisible);
  }

  document.addEventListener('DOMContentLoaded', init);

  // ── Public API ──────────────────────────────────────────────────────────

  global.Player = {
    /**
     * Play a single track. Accepts an object, or the legacy positional form.
     * @param {Object|string|number} trackOrId
     */
    playTrack: function (trackOrId, title, artist, albumArtUrl) {
      const track = (trackOrId && typeof trackOrId === 'object')
        ? trackOrId
        : { id: trackOrId, title: title, artist: artist, albumArtUrl: albumArtUrl };
      if (!track || !track.id) return;
      queue = [track];
      loadAndPlay(0);
    },

    playQueue: function (tracks) {
      if (!tracks || !tracks.length) return;
      queue = tracks;
      loadAndPlay(0);
    },

    toggle: togglePlayPause,
    toggleQueue: toggleQueue,
    clearQueue: clearPlaybackQueue,
    stop: stopPlayback,
  };
})(window);
