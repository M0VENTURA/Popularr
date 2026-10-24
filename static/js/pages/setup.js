/*
  First-run setup wizard (/setup).

  This page is STANDALONE — it does not extend base.html, because the wizard runs
  before the app shell is usable. It therefore has no global.api / global.toast /
  global.escapeHtml, so everything it needs is defined locally.

  Server endpoints:
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

  /* ── Toast ────────────────────────────────────────────────────────────── */
  function showToast(message, type) {
    var el = $('setupToast');
    if (!el) return;
    var body = $('toastMessage');
    if (body) body.textContent = message;

    el.classList.remove('bg-success', 'bg-danger', 'bg-warning', 'text-white', 'text-dark');
    if (type === 'success') el.classList.add('bg-success', 'text-white');
    else if (type === 'error') el.classList.add('bg-danger', 'text-white');
    else if (type === 'warning') el.classList.add('bg-warning', 'text-dark');

    if (global.bootstrap && global.bootstrap.Toast) {
      global.bootstrap.Toast.getOrCreateInstance(el).show();
    }
  }

  /* ── Step navigation ─────────────────────────────────────────────────── */

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
    var btn = card && card.querySelector('.wizard-next-btn');
    if (!btn) return;
    btn.innerHTML = stepIsDirty(card)
      ? 'Save &amp; Continue <i class="bi bi-arrow-right"></i>'
      : 'Skip Step <i class="bi bi-arrow-right"></i>';
  }

  function goToStep(n) {
    if (!n || n < 1 || n > TOTAL_STEPS) return;
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
        base_url: val('navUrl'),
        username: val('navUser'),
        password: $('navPass') ? $('navPass').value : ''
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

  function numVal(id, fallback) {
    var raw = parseFloat(val(id));
    return isNaN(raw) ? fallback : raw;
  }

  function intVal(id, fallback) {
    var raw = parseInt(val(id), 10);
    return isNaN(raw) ? fallback : raw;
  }

  function checked(id) {
    var el = $(id);
    return !!(el && el.checked);
  }

  function getAllowedFormats() {
    var formats = [];
    document.querySelectorAll('.setup-format-check:checked').forEach(function (el) {
      if (el.value) formats.push(String(el.value).toLowerCase());
    });
    return formats.length ? formats : ['flac', 'mp3'];
  }

  function buildSetupConfig() {
    var pgHost = val('pgHost');
    var lfmKey = val('lfmApiKey');
    var lfmUser = val('lfmUsername');
    var dgToken = val('dgToken');
    var lbToken = val('lbToken');
    var audiodbKey = val('audiodbApiKey');
    var geminiKey = val('geminiApiKey');
    var geminiModel = val('geminiModel') || 'gemini-2.5-flash-image';
    var slskdKey = val('slskdApiKey');
    var navUser = val('navUser');
    var displayName = val('navDisplayName') || navUser;

    var config = {
      navidrome_users: [{
        user: navUser,
        display_name: displayName,
        base_url: val('navUrl'),
        pass: $('navPass') ? $('navPass').value : '',
        lastfm_username: lfmUser,
        listenbrainz_user_token: lbToken
      }],
      navidrome: {
        auto_public_playlists: checked('setup_nav_public_playlists'),
        playlist_cover_art: checked('setup_nav_playlist_covers')
      },
      api_integrations: {
        lastfm: { enabled: !!lfmKey, api_key: lfmKey },
        discogs: { enabled: !!dgToken, token: dgToken },
        musicbrainz: { enabled: checked('mbEnabled') },
        listenbrainz: { enabled: !!lbToken, token: lbToken },
        audiodb: { enabled: !!audiodbKey, api_key: audiodbKey },
        gemini: { enabled: !!geminiKey, api_key: geminiKey, model: geminiModel }
      },
      slskd: {
        enabled: !!slskdKey,
        web_url: val('slskdUrl') || 'http://slskd:5030',
        api_key: slskdKey
      }
    };

    if (pgHost) {
      config.PG_HOST = pgHost;
      config.PG_PORT = val('pgPort') || '5432';
      config.PG_USER = val('pgUser') || 'popularr';
      config.PG_PASSWORD = $('pgPass') ? $('pgPass').value : '';
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
        num_genres: intVal('essentiaNumGenres', 3),
        genre_format: val('essentiaGenreFormat') || 'parent_child',
        parse_json_features: checked('essentiaJsonFeatures'),
        delete_json_after_import: true
      };
    }

    config.downloads = {
      folder: val('setupDownloadsFolder') || '/downloads/Music',
      file_name_format: val('setupFileNameFormat') ||
        '{album_artist}/{year} - {album}/{track_number}. {artist} - {title}',
      conversion: {
        enabled: checked('setupConversionEnabled'),
        mode: val('setupConversionMode') || 'flac_to_mp3',
        mp3_bitrate_kbps: intVal('setupConversionBitrate', 320),
        original_handling: val('setupConversionOriginalHandling') || 'move_to_original',
        original_subfolder: 'Original'
      },
      quality: {
        min_bitrate: intVal('setupMinBitrate', 192),
        allowed_formats: getAllowedFormats()
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

    config.tagging = {
      write_tags_to_file: checked('setup_write_tags'),
      sync_album_tags_on_scan: checked('setup_sync_album_scan'),
      skip_unchanged_ratings: checked('setup_skip_unchanged_ratings'),
      preserve_file_timestamps: true,
      write_options: {
        ratings_only: checked('setup_ratings_only'),
        fill_missing_only: checked('setup_fill_missing_only')
      }
    };

    config.metadata_update = {
      apply_during_scan: val('setup_apply_metadata_during_scan') !== 'false',
      album_name_source: val('setup_album_name_source') || 'dedupe',
      album_name_update_target: val('setup_album_name_target') || 'db'
    };

    config.popularity = {
      weights: {
        lastfm: numVal('setupPopWeightLastfm', 0.55),
        listenbrainz: numVal('setupPopWeightListenbrainz', 0.35),
        age: numVal('setupPopWeightAge', 0.10)
      },
      scan_threads: intVal('setupScanThreads', 4)
    };

    config.genres = {
      selection: {
        enabled: checked('setup_canonical_genres')
      }
    };

    config.watcher = {
      auto_import_enabled: checked('setup_auto_import'),
      auto_popularity_scan: checked('setup_auto_popularity'),
      downloads_watcher_enabled: checked('setup_downloads_watcher')
    };

    config.features = {
      album_skip_days: intVal('setupAlbumSkipDays', 7),
      cover_detection_enabled: checked('setup_cover_detection'),
      deep_genre_search: checked('setup_deep_genre_search'),
      upcoming_releases_scan_enabled: checked('setup_upcoming_scans'),
      sync_ratings_to_all_users: checked('setup_sync_all_users')
    };

    config.playlists = {
      essential_playlists_enabled: checked('setup_essential_playlists'),
      essential_include_featured: checked('setup_essential_include_featured'),
      genre_playlists_enabled: checked('setup_genre_playlists'),
      new_music_playlist_enabled: checked('setup_new_music_playlist'),
      favourite_playlists_enabled: checked('setup_favourite_playlists'),
      decade_playlists_enabled: checked('setup_decade_playlists'),
      hit_singles_playlist_enabled: checked('setup_hit_singles_playlist'),
      hidden_gems_playlist_enabled: checked('setup_hidden_gems_playlist'),
      new_releases_playlist_enabled: checked('setup_new_releases_playlist'),
      covers_playlist_enabled: checked('setup_covers_playlist'),
      acoustic_playlist_enabled: checked('setup_acoustic_playlist'),
      high_energy_playlist_enabled: checked('setup_high_energy_playlist'),
      mood_playlists_enabled: checked('setup_mood_playlists'),
      exclude_christmas_from_playlists: checked('setup_exclude_christmas')
    };

    return config;
  }

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
    toggleEssentiaGenreOptions();
    if (on) checkEssentiaModels();
  }

  function toggleEssentiaGenreOptions() {
    var gOpts = $('essentiaGenreOptions');
    if (gOpts) gOpts.classList.toggle('d-none', !(checked('essentiaEnabled') && checked('essentiaGenres')));
  }

  function toggleConversionOptions() {
    var opts = $('setupConversionOptions');
    if (opts) opts.classList.toggle('d-none', !checked('setupConversionEnabled'));
  }

  function toggleTagWritingOptions() {
    var opts = $('setupTagWritingOptions');
    if (opts) opts.classList.toggle('opacity-50', !checked('setup_write_tags'));
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
      var navExtras = [];
      if (checked('setup_nav_public_playlists')) navExtras.push('public playlists');
      if (checked('setup_nav_playlist_covers')) navExtras.push('playlist cover art');
      nav.textContent = (val('navUser') || 'username') + ' @ ' + (val('navUrl') || 'not set') +
        (navExtras.length ? ' (' + navExtras.join(', ') + ')' : '');
    }

    var db = $('summaryDatabase');
    if (db) {
      var pgHost = val('pgHost');
      db.textContent = pgHost
        ? 'PostgreSQL @ ' + pgHost + '/' + (val('pgDb') || 'popularr')
        : 'PostgreSQL (container defaults)';
    }

    var apis = [];
    if (checked('mbEnabled')) apis.push('MusicBrainz');
    if (val('lfmApiKey')) apis.push('Last.fm' + (val('lfmUsername') ? ' (' + val('lfmUsername') + ')' : ''));
    if (val('lbToken')) apis.push('ListenBrainz');
    if (val('dgToken')) apis.push('Discogs');
    if (val('audiodbApiKey')) apis.push('TheAudioDB');
    if (val('geminiApiKey')) apis.push('Google Gemini');
    if (val('slskdApiKey')) apis.push('Soulseek (slskd)');
    setText($('summaryApis'), apis.length ? apis.join(', ') : 'None enabled');

    var essentiaDesc = 'Not enabled';
    if (checked('essentiaEnabled')) {
      var eParts = [];
      if (checked('essentiaMoods')) eParts.push('moods');
      if (checked('essentiaJsonFeatures')) eParts.push('BPM/danceability');
      if (checked('essentiaGenres')) eParts.push('genres');
      essentiaDesc = 'Enabled (' + (eParts.length ? eParts.join(', ') : 'models only') + ')';
    }
    setText($('summaryEssentia'), essentiaDesc);

    var files = $('summaryFiles');
    if (files) {
      var fmt = val('setupFileNameFormat') ||
        '{album_artist}/{year} - {album}/{track_number}. {artist} - {title}';
      var conv = checked('setupConversionEnabled')
        ? 'converts FLAC (' + val('setupConversionMode') + ', ' + val('setupConversionBitrate') + 'kbps)'
        : 'no conversion';
      var tagMode = 'Read-only (DB only)';
      if (checked('setup_write_tags')) {
        if (checked('setup_ratings_only')) tagMode = 'Write ratings only to files';
        else if (checked('setup_fill_missing_only')) tagMode = 'Fill missing file tags only';
        else tagMode = 'Write tags to audio files';
      }
      files.textContent = tagMode + ' · ' + fmt + ' · ' + conv + ' · Formats: ' + getAllowedFormats().join(', ').toUpperCase();
    }

    var automation = [];
    automation.push('Weights: LFM ' + numVal('setupPopWeightLastfm', 0.55) +
      ' / LB ' + numVal('setupPopWeightListenbrainz', 0.35) +
      ' / Age ' + numVal('setupPopWeightAge', 0.10));
    automation.push(intVal('setupScanThreads', 4) + ' scan threads');
    if (checked('setup_auto_import')) automation.push('Auto Import');
    if (checked('setup_auto_popularity')) automation.push('Auto Popularity Scan');
    if (checked('setup_downloads_watcher')) automation.push('Downloads Watcher');
    if (checked('setup_cover_detection')) automation.push('Cover Detection');
    if (checked('setup_deep_genre_search')) automation.push('Deep Genre Search');
    if (checked('setup_canonical_genres')) automation.push('Canonical Genres');
    if (checked('setup_upcoming_scans')) automation.push('Upcoming Releases');
    if (checked('setup_sync_all_users')) automation.push('Sync All Users');

    var playlists = [];
    if (checked('setup_essential_playlists')) playlists.push('Essential');
    if (checked('setup_genre_playlists')) playlists.push('Genre Top Tracks');
    if (checked('setup_new_music_playlist')) playlists.push('New Music');
    if (checked('setup_favourite_playlists')) playlists.push('Favourites');
    if (checked('setup_decade_playlists')) playlists.push('Decades');
    if (checked('setup_hit_singles_playlist')) playlists.push('Hit Singles');
    if (checked('setup_hidden_gems_playlist')) playlists.push('Hidden Gems');
    if (checked('setup_new_releases_playlist')) playlists.push('New Releases');
    if (checked('setup_covers_playlist')) playlists.push('Covers');
    if (checked('setup_acoustic_playlist')) playlists.push('Acoustic');
    if (checked('setup_high_energy_playlist')) playlists.push('High Energy');
    if (checked('setup_mood_playlists')) playlists.push('Mood Mixes');
    if (playlists.length) {
      automation.push('Playlists: ' + playlists.join(', '));
    }

    setText($('summaryAutomation'),
      automation.length ? automation.join(' · ') : 'All automation off');
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

      if (!checked('setupStartImportOnSave')) {
        showToast('Config saved! Redirecting to dashboard…', 'success');
        setTimeout(function () { window.location.href = '/'; }, 1000);
        return;
      }

      showToast('Config saved! Starting Navidrome import…', 'success');

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
          showToast('Config saved (import note: ' +
            ((importData && importData.error) || 'could not start automatically') + ')', 'warning');
        }
        setTimeout(function () { window.location.href = '/'; }, 1500);
      }).catch(function () {
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
    } else if (el.id === 'essentiaGenres') {
      toggleEssentiaGenreOptions();
    } else if (el.id === 'setupConversionEnabled') {
      toggleConversionOptions();
    } else if (el.id === 'setup_write_tags') {
      toggleTagWritingOptions();
    }
  }

  function init() {
    document.addEventListener('click', onClick);
    document.addEventListener('change', onChange);

    toggleEssentiaOptions();
    toggleConversionOptions();
    toggleTagWritingOptions();

    document.querySelectorAll('.step-card').forEach(function (card) {
      if (!card.querySelector('.wizard-next-btn')) return;
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
