/*
  First-run setup wizard (/setup).

  Extracted from the inline <script> in templates/auth/setup.html.

  This page is STANDALONE — it does not extend base.html, because the wizard runs
  before the app shell is usable. It therefore has no global.api / global.toast /
  global.escapeHtml, so everything it needs is defined locally.

  WHAT CHANGED
  ------------
  * BUG FIXED: `testConnection()` read the implicit global `event`. That only
    exists while the browser is dispatching a real click, so calling it any other
    way (or in a browser that does not expose `window.event`) threw
    "event is not defined". The button is now passed in explicitly.
  * Inline `onclick=`/`onchange=` handlers replaced with ONE delegated click
    listener plus a delegated change listener, matching the rest of the tree.
  * The partial-save call no longer swallows every failure silently — a failed
    partial save is logged. It is still non-fatal: the wizard must not block on a
    best-effort save.
  * The Essentia download poll is bounded and cancels on navigation, so leaving
    the page does not leave a timer running for five minutes.

  Server endpoints (unchanged):
    POST /api/test-navidrome-connection
    POST /api/setup/save-partial
    POST /api/setup/save
    POST /api/navidrome/import
    GET  /api/essentia/download-status
    POST /api/essentia/download-models
*/
(function (global) {
  'use strict';

  var TOTAL_STEPS = 6;

  /* Aborts the Essentia poll when the page unloads. */
  var pollAborted = false;

  function $(id) {
    return document.getElementById(id);
  }

  function setText(el, message, className) {
    if (!el) return;
    el.textContent = message;
    if (className) el.className = className;
  }

  /* ── Toast ──────────────────────────────────────────────────────────────
     The wizard has its own Bootstrap toast element (#setupToast) because
     global.toast lives in the app shell, which this page does not load. */
  function showToast(message, type) {
    var el = $('setupToast');
    if (!el) return;
    var body = $('toastMessage');
    if (body) body.textContent = message;

    el.classList.remove('bg-success', 'bg-danger', 'bg-warning', 'text-white');
    if (type === 'success') el.classList.add('bg-success', 'text-white');
    else if (type === 'error') el.classList.add('bg-danger', 'text-white');
    else if (type === 'warning') el.classList.add('bg-warning', 'text-dark');

    if (global.bootstrap && global.bootstrap.Toast) {
      global.bootstrap.Toast.getOrCreateInstance(el).show();
    }
  }

  /* ── Step navigation ─────────────────────────────────────────────────── */

  /* True when the user has entered anything on this step (vs the pre-filled
     defaults), which switches the button label from "Skip Step" to
     "Save & Continue". */
  function stepIsDirty(card) {
    var dirty = false;
    card.querySelectorAll('input,select,textarea').forEach(function (el) {
      if (el.type === 'checkbox' || el.type === 'radio') {
        if (el.checked !== el.defaultChecked) dirty = true;
      } else if (el.value !== el.defaultValue) {
        dirty = true;
      }
    });
    return dirty;
  }

  function refreshStepButton(card) {
    var btn = card && card.querySelector('[data-action="next-step"]');
    if (!btn) return;
    btn.innerHTML = stepIsDirty(card)
      ? 'Save &amp; Continue <i class="bi bi-arrow-right"></i>'
      : 'Skip Step <i class="bi bi-arrow-right"></i>';
  }

  function goToStep(n) {
    for (var i = 1; i <= TOTAL_STEPS; i++) {
      var card = $('step' + i);
      if (!card) continue;
      card.classList.toggle('d-none', i !== n);
      if (i === n) refreshStepButton(card);
    }
    document.querySelectorAll('.step-dot').forEach(function (dot) {
      var s = parseInt(dot.dataset.step, 10);
      dot.classList.toggle('active', s === n);
      dot.classList.toggle('done', s < n);
    });
    if (n === TOTAL_STEPS) updateSummary();
    /* Keep the focused step in view on mobile. */
    var active = $('step' + n);
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  /* ── Connection test ─────────────────────────────────────────────────── */

  function testConnection(btn) {
    var status = $('connectionStatus');
    if (btn) btn.disabled = true;
    setText(status, 'Testing…', 'ms-2 small text-muted');

    fetch('/api/test-navidrome-connection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_url: $('navUrl').value,
        username: $('navUser').value,
        password: $('navPass').value
      })
    }).then(function (r) {
      return r.json();
    }).then(function (d) {
      if (d && d.success) {
        setText(status, d.message, 'ms-2 small text-success');
        showToast(d.message || 'Connection successful', 'success');
      } else {
        var msg = (d && d.error) || 'Connection failed';
        if (d && d.detail) msg += ' (' + d.detail + ')';
        setText(status, msg, 'ms-2 small text-danger');
        showToast(msg, 'error');
      }
    }).catch(function (e) {
      var msg = 'Network error: ' + (e && e.message ? e.message : e);
      setText(status, msg, 'ms-2 small text-danger');
      showToast(msg, 'error');
    }).finally(function () {
      if (btn) btn.disabled = false;
    });
  }

  /* ── Config assembly ─────────────────────────────────────────────────── */

  function val(id) {
    var el = $(id);
    return el ? String(el.value || '').trim() : '';
  }

  function checked(id) {
    var el = $(id);
    return !!(el && el.checked);
  }

  function buildSetupConfig() {
    var pgHost = val('pgHost');
    var lfmKey = val('lfmApiKey');
    var dgToken = val('dgToken');
    var lbToken = val('lbToken');
    var slskdKey = val('slskdApiKey');

    /* Services are enabled by the PRESENCE of a key/token — no separate toggles. */
    var config = {
      navidrome_users: [{
        user: $('navUser').value,
        display_name: $('navUser').value,
        base_url: $('navUrl').value,
        pass: $('navPass').value,
        listenbrainz_user_token: lbToken
      }],
      api_integrations: {
        lastfm: { enabled: !!lfmKey, api_key: lfmKey },
        discogs: { enabled: !!dgToken, token: dgToken },
        musicbrainz: { enabled: true },
        listenbrainz: { enabled: !!lbToken },
        audiodb: { enabled: false }
      },
      slskd: {
        enabled: !!slskdKey,
        web_url: val('slskdUrl'),
        api_key: slskdKey
      }
    };

    if (pgHost) {
      config.PG_HOST = pgHost;
      config.PG_PORT = val('pgPort') || '5432';
      config.PG_USER = val('pgUser') || 'popularr';
      config.PG_PASSWORD = $('pgPass').value;
      config.PG_DATABASE = val('pgDb') || 'popularr';
    }

    if (checked('essentiaEnabled')) {
      config.essentia = {
        script_path: '/opt/Essentia-to-Metadata/tag_music.py',
        models_dir: '/opt/essentia_models',
        mood_threshold: 0.005,
        per_file_timeout: 300,
        tag_moods: checked('essentiaMoods'),
        tag_genres: checked('essentiaGenres'),
        parse_json_features: true,
        delete_json_after_import: true
      };
    }

    config.downloads = {
      file_name_format: val('setupFileNameFormat') ||
        '{album_artist}/{year} - {album}/{track_number}. {artist} - {title}',
      conversion: {
        enabled: checked('setupConversionEnabled'),
        mode: $('setupConversionMode').value,
        mp3_bitrate_kbps: parseInt($('setupConversionBitrate').value, 10) || 320,
        original_handling: 'move_to_original',
        original_subfolder: 'Original'
      },
      quality_filter: {
        enabled: checked('setup_quality_filter'),
        reject_others: true,
        bitrate_tolerance: 5,
        priorities: [
          { format: 'flac', bitrate_kbps: null },
          { format: 'mp3', bitrate_kbps: 320 }
        ]
      }
    };

    config.watcher = {
      auto_import_enabled: checked('setup_auto_import'),
      auto_popularity_scan: checked('setup_auto_popularity'),
      downloads_watcher_enabled: checked('setup_downloads_watcher')
    };

    config.features = {
      cover_detection_enabled: checked('setup_cover_detection'),
      upcoming_releases_scan_enabled: checked('setup_upcoming_scans'),
      sync_ratings_to_all_users: checked('setup_sync_all_users')
    };

    config.playlists = {
      essential_playlists_enabled: checked('setup_essential_playlists'),
      genre_playlists_enabled: checked('setup_genre_playlists'),
      new_music_playlist_enabled: checked('setup_new_music_playlist')
    };

    config.tagging = { write_tags_to_file: checked('setup_write_tags') };

    return config;
  }

  /* Best-effort: a failed partial save must never block the wizard, but it must
     not vanish either — the old empty `catch {}` hid real 500s. */
  function savePartialConfig() {
    return fetch('/api/setup/save-partial', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildSetupConfig())
    }).catch(function (e) {
      console.warn('[setup] partial save failed (continuing)', e);
    });
  }

  function goToNextStep(n, btn) {
    var restoreLabel = btn ? btn.innerHTML : '';
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Saving…';
    }
    return savePartialConfig().then(function () {
      goToStep(n);
      if (btn) {
        btn.disabled = false;
        if (btn.classList.contains('wizard-next-btn')) {
          refreshStepButton(btn.closest('.step-card'));
        } else {
          btn.innerHTML = restoreLabel;
        }
      }
    });
  }

  /* ── Optional sections ───────────────────────────────────────────────── */

  function toggleEssentiaOptions() {
    var on = checked('essentiaEnabled');
    var opts = $('essentiaOptions');
    if (opts) opts.classList.toggle('d-none', !on);
    if (on) checkEssentiaModels();
  }

  function toggleConversionOptions() {
    var opts = $('setupConversionOptions');
    if (opts) opts.classList.toggle('d-none', !checked('setupConversionEnabled'));
  }

  function checkEssentiaModels() {
    var btn = $('essentiaDownloadBtn');
    var status = $('essentiaDownloadStatus');
    return fetch('/api/essentia/download-status').then(function (r) {
      return r.json();
    }).then(function (d) {
      if (d && d.status === 'installed') {
        if (btn) {
          btn.disabled = true;
          btn.innerHTML = '<i class="bi bi-check-circle"></i> Models Installed';
          btn.classList.remove('btn-outline-primary');
          btn.classList.add('btn-outline-success');
        }
        if (status) status.textContent = '✅ ' + d.file_count + ' model files ready';
      } else if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-cloud-download"></i> Download Models (~87 MB)';
        btn.classList.remove('btn-outline-success');
        btn.classList.add('btn-outline-primary');
      }
    }).catch(function (e) {
      console.warn('[setup] essentia status check failed', e);
    });
  }

  function downloadEssentiaModels() {
    var btn = $('essentiaDownloadBtn');
    var status = $('essentiaDownloadStatus');
    var prog = $('essentiaProgress');
    var bar = $('essentiaProgressBar');

    /* Fade the button to a disabled state instead of removing it — the layout
       never shifts while the download is running. */
    if (btn) {
      btn.disabled = true;
      btn.classList.add('opacity-50');
      btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Downloading…';
    }
    if (prog) prog.classList.remove('d-none');

    function restoreBtn() {
      if (!btn) return;
      btn.disabled = false;
      btn.classList.remove('opacity-50');
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Download Models (~87 MB)';
    }

    function finish(message) {
      if (status) status.textContent = message;
      if (bar) {
        bar.style.width = '100%';
        bar.classList.add('bg-success');
      }
      restoreBtn();
      checkEssentiaModels();
    }

    return fetch('/api/essentia/download-status').then(function (r) {
      return r.json();
    }).then(function (pre) {
      if (pre && pre.status === 'installed') {
        finish('✅ Already installed (' + pre.file_count + ' files)');
        return null;
      }
      if (status) status.textContent = 'Starting download…';
      if (bar) bar.style.width = '10%';

      return fetch('/api/essentia/download-models', { method: 'POST' }).then(function () {
        if (status) status.textContent = 'Downloading…';
        if (bar) bar.style.width = '50%';
        return pollEssentia();
      });
    }).catch(function (e) {
      if (status) status.textContent = 'Failed: ' + (e && e.message ? e.message : e);
      if (bar) bar.classList.add('bg-danger');
      restoreBtn();
    });
  }

  /* Poll for completion. Bounded to ~5 minutes and stops immediately once the
     page is unloading, so the timer cannot outlive the wizard. */
  function pollEssentia() {
    var attempts = 60;
    var intervalMs = 5000;

    function tick(remaining) {
      if (pollAborted || remaining <= 0) {
        var status = $('essentiaDownloadStatus');
        if (status && remaining <= 0) status.textContent = 'Timed out — may continue in background.';
        var btn = $('essentiaDownloadBtn');
        if (btn) {
          btn.disabled = false;
          btn.classList.remove('opacity-50');
          btn.innerHTML = '<i class="bi bi-cloud-download"></i> Download Models (~87 MB)';
        }
        return;
      }
      setTimeout(function () {
        if (pollAborted) return;
        fetch('/api/essentia/download-status').then(function (r) {
          return r.json();
        }).then(function (d) {
          if (d && (d.status === 'installed' || d.status === 'complete' || d.status === 'idle')) {
            var status = $('essentiaDownloadStatus');
            if (status) status.textContent = '✅ Ready';
            var bar = $('essentiaProgressBar');
            if (bar) {
              bar.style.width = '100%';
              bar.classList.add('bg-success');
            }
            var btn = $('essentiaDownloadBtn');
            if (btn) {
              btn.disabled = false;
              btn.classList.remove('opacity-50');
              btn.innerHTML = '<i class="bi bi-cloud-download"></i> Download Models (~87 MB)';
            }
            checkEssentiaModels();
            return;
          }
          tick(remaining - 1);
        }).catch(function () {
          tick(remaining - 1);
        });
      }, intervalMs);
    }

    tick(attempts);
  }

  /* ── Summary ─────────────────────────────────────────────────────────── */

  function updateSummary() {
    var nav = $('summaryNavidrome');
    if (nav) {
      nav.innerHTML = '<strong>' + (val('navUser') || 'username') + '</strong> @ ' +
        (val('navUrl') || 'not set');
    }

    var db = $('summaryDatabase');
    if (db) {
      var pgHost = val('pgHost');
      db.innerHTML = pgHost
        ? '<strong>PostgreSQL</strong> @ ' + pgHost + '/' + (val('pgDb') || 'popularr')
        : '<span class="text-muted">PostgreSQL (container defaults)</span>';
    }

    var apis = [];
    if (val('lfmApiKey')) apis.push('Last.fm');
    if (val('dgToken')) apis.push('Discogs');
    if (val('lbToken')) apis.push('ListenBrainz');
    if (val('slskdApiKey')) apis.push('Soulseek');
    setText($('summaryApis'), apis.length ? apis.join(', ') : 'None enabled');

    setText($('summaryEssentia'),
      checked('essentiaEnabled') ? 'Will be enabled' : 'Not enabled');

    var files = $('summaryFiles');
    if (files) {
      var fmt = val('setupFileNameFormat') ||
        '{album_artist}/{year} - {album}/{track_number}. {artist} - {title}';
      var conv = checked('setupConversionEnabled')
        ? 'converts FLAC → MP3 (' + val('setupConversionBitrate') + 'kbps)'
        : 'no conversion';
      /* textContent-built parts only: the format string is user input. */
      files.textContent = fmt + ' — ' + conv;
    }

    var automation = [];
    if (checked('setup_auto_import')) automation.push('Auto Import');
    if (checked('setup_auto_popularity')) automation.push('Auto Popularity Scan');
    if (checked('setup_downloads_watcher')) automation.push('Downloads Watcher');
    if (checked('setup_cover_detection')) automation.push('Cover Detection');
    if (checked('setup_upcoming_scans')) automation.push('Upcoming Releases');
    if (checked('setup_essential_playlists')) automation.push('Essential Playlists');
    if (checked('setup_genre_playlists')) automation.push('Genre Playlists');
    if (checked('setup_new_music_playlist')) automation.push('New Music Playlist');
    if (checked('setup_write_tags')) automation.push('Tag Writing');
    if (checked('setup_sync_all_users')) automation.push('Sync All Users');
    if (checked('setup_quality_filter')) automation.push('Quality Filter');
    setText($('summaryAutomation'),
      automation.length ? automation.join(', ') : 'All automation off');
  }

  /* ── Save ────────────────────────────────────────────────────────────── */

  function saveSetup(btn) {
    var originalLabel = '<i class="bi bi-check-circle"></i> Save &amp; Go to Dashboard';
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Saving…';
    }

    function restore(fallbackMessage, kind) {
      if (fallbackMessage) showToast(fallbackMessage, kind || 'error');
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = originalLabel;
      }
    }

    return fetch('/api/setup/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildSetupConfig())
    }).then(function (r) {
      return r.json();
    }).then(function (d) {
      if (!d || !d.success) {
        restore((d && d.error) || 'Failed');
        return;
      }

      showToast('Config saved! Starting Navidrome import…', 'success');

      /* Trigger a full Navidrome import, then hand over to the dashboard. */
      return fetch('/api/navidrome/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'all' })
      }).then(function (r) {
        return r.json();
      }).then(function (importData) {
        if (importData && importData.success) {
          showToast('Navidrome import started — redirecting to dashboard…', 'success');
        } else {
          showToast('Config saved but import could not start: ' +
            ((importData && importData.error) || 'unknown'), 'warning');
        }
        setTimeout(function () { window.location.href = '/'; }, 1500);
      }).catch(function () {
        /* Config IS saved; only the import could not be kicked off. */
        showToast('Config saved. Start an import from the dashboard.', 'warning');
        setTimeout(function () { window.location.href = '/'; }, 1500);
      });
    }).catch(function (e) {
      restore('Network error: ' + (e && e.message ? e.message : e));
    });
  }

  /* ── Wiring ──────────────────────────────────────────────────────────── */

  function onClick(event) {
    var el = event.target.closest('[data-action]');
    if (!el) return;
    var action = el.getAttribute('data-action');

    if (action === 'test-connection') {
      event.preventDefault();
      testConnection(el);
    } else if (action === 'go-step') {
      event.preventDefault();
      goToStep(parseInt(el.getAttribute('data-step'), 10));
    } else if (action === 'next-step') {
      event.preventDefault();
      goToNextStep(parseInt(el.getAttribute('data-next-step'), 10), el);
    } else if (action === 'download-essentia') {
      event.preventDefault();
      downloadEssentiaModels();
    } else if (action === 'save-setup') {
      event.preventDefault();
      saveSetup(el);
    }
  }

  function onChange(event) {
    var el = event.target;
    if (!el || !el.id) return;
    if (el.id === 'essentiaEnabled') {
      toggleEssentiaOptions();
    } else if (el.id === 'setupConversionEnabled') {
      toggleConversionOptions();
    }
  }

  function init() {
    document.addEventListener('click', onClick);
    document.addEventListener('change', onChange);

    /* Reflect the pre-filled defaults immediately. */
    toggleEssentiaOptions();
    toggleConversionOptions();

    /* Dynamic "Skip Step" → "Save & Continue" labels. */
    document.querySelectorAll('.step-card').forEach(function (card) {
      if (!card.querySelector('[data-action="next-step"]')) return;
      var refresh = function () { refreshStepButton(card); };
      card.addEventListener('input', refresh);
      card.addEventListener('change', refresh);
      refreshStepButton(card);
    });

    window.addEventListener('beforeunload', function () { pollAborted = true; });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(window);
