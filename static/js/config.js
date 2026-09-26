/**
 * Config page JavaScript
 * Handles all config page interactions: saving, user management, search filters,
 * upcoming releases sources, features/weights, and scheduler controls.
 */

const DEFAULT_STRIP_KEYWORDS = [
  'remastered', 'remaster', 'remastered 2022', 'remastered 2023', 'remastered 2024',
  'remastered 2025', 'remastered 2021', 'remastered 2020', 'remastered 2019',
  'remastered 2018', 'remastered 2017', 'remastered 2016', 'remastered 2015',
  'remastered 2014', 'remastered 2013', 'remastered 2012', 'remastered 2011',
  'remastered 2010', 'remastered 2009', 'remastered 2008', 'remastered 2007',
  'remastered 2006', 'remastered 2005', 'remastered 2004', 'remastered 2003',
  'remastered 2002', 'remastered 2001', 'remastered 2000', 'remastered 1999',
  'remastered 1998', 'remastered 1997', 'remastered 1996', 'remastered 1995',
  'remastered 1994', 'remastered 1993', 'remastered 1992', 'remastered 1991',
  'remastered 1990', 'remastered 1989', 'remastered 1988', 'remastered 1987',
  'remastered 1986', 'remastered 1985', 'remastered 1984', 'remastered 1983',
  'remastered 1982', 'remastered 1981', 'remastered 1980',
  'radio edit', 'radio version', 'single edit', 'album version',
  'extended version', 'extended mix', 'club mix', 'dub mix',
  'instrumental', 'a cappella', 'karaoke version', 'backing track',
  'explicit', 'clean', 'bonus track', 'bonus', 'edit'
];

// ===== UTILITY FUNCTIONS =====

function escapeHtml(text) {
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
  return String(text).replace(/[&<>"']/g, m => map[m]);
}

function showToast(title, message, type) {
  const toastEl = document.getElementById('configToast');
  if (!toastEl) return;
  const toast = new bootstrap.Toast(toastEl);
  document.getElementById('toastTitle').textContent = title;
  document.getElementById('toastMessage').textContent = message;
  toastEl.classList.remove('bg-success', 'bg-danger', 'bg-warning', 'text-white', 'text-dark');
  if (type === 'success') toastEl.classList.add('bg-success', 'text-white');
  else if (type === 'error' || type === 'danger') toastEl.classList.add('bg-danger', 'text-white');
  else if (type === 'warning') toastEl.classList.add('bg-warning', 'text-dark');
  toast.show();
}

// ===== USER CONTEXT =====

function setActiveUserContext(userIndex) {
  localStorage.setItem('selectedNavidromeUser', userIndex);
  const notice = document.getElementById('userContextNotice');
  if (notice) {
    notice.textContent = 'User context updated for this browser.';
    notice.classList.remove('d-none');
  }
}

// ===== SEARCH FILTER KEYWORDS =====

function initializeStripKeywords() {
  const config = window.pageConfig || {};
  const keywords = config.strip_parentheses_filters || [];
  const keywordsList = document.getElementById('stripKeywordsList');
  if (!keywordsList) return;
  const displayKeywords = keywords.length > 0 ? keywords : DEFAULT_STRIP_KEYWORDS;
  displayKeywords.forEach(keyword => addKeywordBadge(keyword));
}

function addKeywordBadge(keyword) {
  const list = document.getElementById('stripKeywordsList');
  if (!list) return;
  const badge = document.createElement('span');
  badge.className = 'badge bg-primary d-flex align-items-center gap-2';
  badge.innerHTML = `
    ${escapeHtml(keyword)}
    <button type="button" class="btn-close btn-close-white" aria-label="Remove" onclick="removeKeywordBadge(this)"></button>
  `;
  list.appendChild(badge);
}

function removeKeywordBadge(btn) {
  const badge = btn.closest('.badge');
  if (badge) badge.remove();
}

function addStripKeyword() {
  const input = document.getElementById('newKeywordInput');
  if (!input) return;
  const keyword = input.value.trim().toLowerCase();
  if (!keyword) {
    showToast('Error', 'Please enter a keyword', 'warning');
    return;
  }
  const existingKeywords = Array.from(document.querySelectorAll('#stripKeywordsList .badge'))
    .map(el => {
      const text = el.textContent.trim();
      return text.substring(0, text.length - 1).trim();
    });
  if (existingKeywords.map(k => k.toLowerCase()).includes(keyword.toLowerCase())) {
    showToast('Warning', 'This keyword already exists', 'warning');
    return;
  }
  addKeywordBadge(keyword);
  input.value = '';
  input.focus();
}

function resetStripKeywords() {
  if (!confirm('Reset search filter keywords to system defaults?')) return;
  const list = document.getElementById('stripKeywordsList');
  if (!list) return;
  list.innerHTML = '';
  DEFAULT_STRIP_KEYWORDS.forEach(keyword => addKeywordBadge(keyword));
  showToast('Success', 'Keywords reset to defaults', 'success');
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && document.activeElement && document.activeElement.id === 'newKeywordInput') {
    e.preventDefault();
    addStripKeyword();
  }
});

// ===== BUILD CONFIG OBJECT =====

function collectFeatureFieldOverrides() {
  // Reads the Features & Weights card's ``.feature-field`` inputs (rendered
  // by config.html from config.features).  Merged into the saved config so a
  // change there survives EITHER save button — before this, the main Save
  // Configuration button silently discarded feature-card edits.
  const overrides = {};
  document.querySelectorAll('.feature-field').forEach(input => {
    const key = input.name.replace('feature_', '');
    let val = input.value;
    if (input.tagName === 'SELECT') {
      if (val === 'true' || val === 'false') val = (val === 'true');
    } else if (window.pageConfig && window.pageConfig.features && Array.isArray(window.pageConfig.features[key])) {
      val = val.split(',').map(s => s.trim()).filter(Boolean);
    } else if (!isNaN(val) && val !== '') {
      val = Number(val);
    }
    overrides[key] = val;
  });
  return overrides;
}

// Features keys inherited from the old system — hidden from the UI and
// dropped from the saved config so they stop accumulating in config.yaml.
const LEGACY_FEATURE_KEYS = ['dry_run', 'sync', 'force', 'verbose', 'perpetual', 'batchrate', 'full_scan', 'auto_boot_navidrome_scan', 'artist'];

function buildConfigObject() {
  function getValue(id, defaultValue) {
    const el = document.getElementById(id);
    return el ? el.value : (defaultValue || '');
  }
  function getChecked(id, defaultValue) {
    const el = document.getElementById(id);
    return el ? el.checked : (defaultValue || false);
  }
  function parseNumber(id, defaultValue) {
    const el = document.getElementById(id);
    if (!el) return defaultValue;
    const v = parseFloat(el.value);
    return Number.isNaN(v) ? defaultValue : v;
  }

  const navidrome_users = [];
  document.querySelectorAll('.user-card').forEach(card => {
    const user = {
      user: card.querySelector('.user-nav-username').value,
      display_name: card.querySelector('.user-display-name').value,
      base_url: card.querySelector('.user-nav-url').value,
      pass: card.querySelector('.user-nav-password').value,
      lastfm_username: card.querySelector('.user-lastfm-username').value,
      listenbrainz_user_token: card.querySelector('.user-listenbrainz-token').value
    };
    if (user.user && user.base_url) {
      navidrome_users.push(user);
    }
  });

  const config = {
    navidrome_users: navidrome_users,
    matching: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.matching) || {},
      {
        fuzzy_threshold: parseFloat(getValue('matching_fuzzy_threshold', '0.80')) || 0.80,
        score_threshold: parseFloat(getValue('matching_score_threshold', '0.60')) || 0.60
      }
    ),
    logging: {
      level: getValue('log_level', 'info').toLowerCase()
    },
    slskd: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.slskd) || {},
      {
        enabled: getChecked('slskd_enabled'),
        web_url: getValue('slskd_web_url'),
        api_key: getValue('slskd_api_key'),
        // Stale-search cleanup cadence: run it once every N searches rather
        // than before every one.  0 DISABLES it ("never"), which is why this
        // is NOT guarded with `|| 10` — that would turn 0 back into 10.
        search_cleanup_interval: (() => {
          const raw = parseInt(getValue('slskd_search_cleanup_interval', '10'), 10);
          return Number.isFinite(raw) ? Math.max(0, raw) : 10;
        })(),
        timeouts: Object.assign(
          {},
          ((window.pageConfig && window.pageConfig.slskd && window.pageConfig.slskd.timeouts) || {}),
          {
            min_retry_delay_minutes: parseInt(getValue('slskd_min_retry_delay', '60')) || 60,
            long_retry_delay_minutes: parseInt(getValue('slskd_long_retry_delay', '1440')) || 1440,
            remotely_queued_timeout_minutes: parseInt(getValue('slskd_remotely_queued_timeout', '60')) || 60,
            active_state_timeout_minutes: parseInt(getValue('slskd_active_timeout', '240')) || 240,
            inter_item_delay_seconds: parseInt(getValue('slskd_inter_item_delay', '5')) || 5
          }
        )
      }
    ),
    wikidata: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.wikidata) || {},
      {
        musician_terms: getValue('wikidata_musician_terms', '')
          .split(',')
          .map(term => term.trim())
          .filter(Boolean)
      }
    ),
    queue: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.queue) || {},
      {
        matching: Object.assign(
          {},
          ((window.pageConfig && window.pageConfig.queue && window.pageConfig.queue.matching) || {}),
          {
            threshold: parseFloat(getValue('queue_match_threshold', '0.65')) || 0.65,
            partial_match: parseFloat(getValue('queue_match_partial', '0.7')) || 0.7,
            strict_duration_sec: parseInt(getValue('queue_strict_duration', '2')) || 2,
            tolerance_duration_sec: parseInt(getValue('queue_tolerance_duration', '5')) || 5,
            detect_live_tracks: getChecked('queue_detect_live', true),
            detect_remix_tracks: getChecked('queue_detect_remix', true),
            detect_compilations: getChecked('queue_detect_compilations', true)
          }
        ),
        search_backoff_hours: getValue('queue_search_backoff_hours', '4, 12, 24')
          .split(',').map(s => parseInt(s.trim(), 10))
          .filter(n => !isNaN(n) && n > 0),
        failure_retry_delay_minutes: parseInt(getValue('queue_failure_retry_delay_minutes', '30')) || 30,
        pre_release_retry_weekday: parseInt(getValue('queue_pre_release_retry_weekday', '4')) || 4
      }
    ),
    lastfm: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.lastfm) || {},
      {
        min_artist_plays: parseInt(getValue('lastfm_min_artist_plays', '20')) || 20,
        min_similarity_score: parseFloat(getValue('lastfm_min_similarity', '0.46')) || 0.46,
        max_similar_per_artist: parseInt(getValue('lastfm_max_similar', '5')) || 5,
        max_albums_per_artist: parseInt(getValue('lastfm_max_albums', '5')) || 5,
        recent_months: parseInt(getValue('lastfm_recent_months', '3')) || 3,
        cache_ttl_hours: parseInt(getValue('lastfm_cache_ttl', '24')) || 24,
        max_retries: parseInt(getValue('lastfm_max_retries', '3')) || 3,
        retry_backoff: parseFloat(getValue('lastfm_retry_backoff', '1.5')) || 1.5,
        rate_limit_delay: parseFloat(getValue('lastfm_rate_limit', '0.5')) || 0.5
      }
    ),
    filesystem: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.filesystem) || {},
      {
        audio_formats: getValue('filesystem_audio_formats', '.mp3, .flac, .m4a, .ogg, .wav, .aac, .wma')
          .split(',')
          .map(ext => ext.trim())
          .filter(Boolean)
      }
    ),
    playlists: {
      essential_playlists_enabled: getChecked('playlists_essential_enabled', true),
      essential_include_featured: getChecked('playlists_essential_include_featured', true),
      essential_name_template: getValue('playlists_essential_name_template', '{artist} - Essential Collection'),
      genre_playlists_enabled: getChecked('playlists_genre_playlists_enabled', true),
      new_music_playlist_enabled: getChecked('playlists_new_music_enabled', true),
      genre_playlists_delete_enabled: getChecked('playlists_genre_delete_enabled', true),
      genre_playlists_create_threshold: parseInt(getValue('playlists_genre_create_threshold', '100'), 10) || 100,
      genre_playlists_delete_threshold: parseInt(getValue('playlists_genre_delete_threshold', '80'), 10) || 80,
      genre_playlists_min_stars: parseInt(getValue('playlists_genre_min_stars', '4'), 10) || 4,
      genre_playlists_max_genres: parseInt(getValue('playlists_genre_max_genres', '3'), 10) || 3,
      exclude_christmas_from_playlists: getChecked('playlists_exclude_christmas', true),
      christmas_playlist_marker: getValue('playlists_christmas_marker', 'christmas') || 'christmas',
      genre_playlists_name_template: getValue('playlists_genre_name_template', '{genre} - Top Tracks')
    },
    navidrome: {
      auto_public_playlists: getChecked('api_navidrome_auto_public_playlists', false),
      playlist_cover_art: getChecked('api_navidrome_playlist_cover_art', false)
    },
    downloads: {
      folder: getValue('downloads_folder'),
      external_export_path: getValue('external_export_path'),
      file_name_format: getValue('downloads_file_name_format', '{album_artist}/{year} - {album}/{track_number}. {artist} - {title}'),
      quality_filter: (() => {
        const priorities = [];
        if (getChecked('downloads_quality_allow_mp3_320', true)) priorities.push({ format: 'mp3', bitrate_kbps: 320 });
        if (getChecked('downloads_quality_allow_flac', true)) priorities.push({ format: 'flac', bitrate_kbps: null });
        return {
          enabled: getChecked('downloads_quality_enabled'),
          reject_others: getChecked('downloads_quality_reject_others', true),
          bitrate_tolerance: parseInt(getValue('downloads_quality_bitrate_tolerance', '5')) || 5,
          priorities: priorities
        };
      })(),
      // Soulseek search quality: bitrate floor for the first pass + optional
      // low-quality fallback tier (rare releases only exist as 128kbps MP3s).
      quality: Object.assign(
        {},
        (window.pageConfig && window.pageConfig.downloads && window.pageConfig.downloads.quality) || {},
        {
          min_bitrate: parseInt(getValue('downloads_search_min_bitrate', '192')) || 192,
          min_sample_rate: parseInt(getValue('downloads_search_min_sample_rate', '44100')) || 44100,
          min_length_seconds: parseInt(getValue('downloads_search_min_length', '30')) || 30,
          allow_low_quality_fallback: getChecked('downloads_search_quality_fallback'),
          fallback_min_bitrate: parseInt(getValue('downloads_search_fallback_bitrate', '128')) || 128
        }
      ),
      conversion: {
        enabled: getChecked('downloads_conversion_enabled'),
        mode: getValue('downloads_conversion_mode', 'flac_to_mp3'),
        mp3_bitrate_kbps: parseInt(getValue('downloads_conversion_mp3_bitrate', '320')) || 320,
        original_handling: getValue('downloads_conversion_original_handling', 'move_to_original'),
        original_subfolder: getValue('downloads_conversion_original_subfolder', 'Original')
      }
    },
    watcher: {
      scan_interval: parseInt(getValue('watcher_scan_interval', '30')) || 30,
      auto_import_enabled: getChecked('watcher_auto_import', true),
      auto_popularity_scan: getChecked('watcher_auto_popularity', true),
      downloads_watcher_enabled: getChecked('watcher_downloads_enabled', true)
    },
    features: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.features) || {},
      {
        retry_scheduler: {
          interval_seconds: parseInt(getValue('retry_scheduler_interval', '60')) || 60,
          auto_start: getChecked('retry_scheduler_enabled', true)
        },
        download_queue_cleanup_scheduler: {
          enabled: getChecked('queue_cleanup_scheduler_enabled', true),
          interval_minutes: parseInt(getValue('queue_cleanup_scheduler_interval_minutes', '60')) || 60
        },
        downloads_duplicate_cleanup: {
          delete_duplicate_files: getChecked('downloads_delete_duplicate_files', true),
          prune_empty_folders: getChecked('downloads_prune_empty_folders', true)
        },
        mature_track_min_age_years: parseInt(getValue('mature_track_min_age_years', '2')) || 2,
        // How many abandoned artists may still be winding down before the
        // full scan waits for them. 0 = never wait, so it is NOT guarded
        // with `|| 1` (that would turn "never wait" back into "wait for 1").
        full_scan_max_abandoned_workers: (() => {
          const raw = parseInt(getValue('pop_max_abandoned_workers', '1'), 10);
          return Number.isFinite(raw) ? Math.max(0, raw) : 1;
        })(),
        daily_musicbrainz_release_scan_enabled: getChecked('daily_musicbrainz_release_scan_enabled', true),
        daily_musicbrainz_release_lookback_days: parseInt(getValue('daily_musicbrainz_release_lookback_days', '42')) || 42,
        daily_musicbrainz_release_lookahead_days: parseInt(getValue('daily_musicbrainz_release_lookahead_days', '120')) || 120,
        daily_musicbrainz_release_max_artists: parseInt(getValue('daily_musicbrainz_release_max_artists', '5000')) || 5000,
        daily_musicbrainz_release_per_artist_limit: parseInt(getValue('daily_musicbrainz_release_per_artist_limit', '100')) || 100,
        daily_musicbrainz_release_global_limit: parseInt(getValue('daily_musicbrainz_release_global_limit', '50')) || 0,
        upcoming_releases_scan_enabled: getChecked('upcoming_releases_scan_enabled', true),
        upcoming_releases_purge_days: parseInt(getValue('upcoming_releases_purge_days', '30')) || 30,
        // parseNumber, NOT `parseInt(...) || 28`: 0 is a legitimate value here
        // ("show nothing older / nothing future-dated") and `||` would replace
        // it with the default. parseNumber only falls back on NaN.
        upcoming_releases_lookback_days: parseNumber('upcoming_releases_lookback_days', 28),
        upcoming_releases_lookahead_days: parseNumber('upcoming_releases_lookahead_days', 14)
      },
      collectFeatureFieldOverrides()
    ),
    single_detection: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.single_detection) || {},
      {
        zscore_high_threshold: parseFloat(getValue('zscore_high_threshold', '1.0')) || 1.0,
        zscore_medium_threshold: parseFloat(getValue('zscore_medium_threshold', '0.6')) || 0.6,
        star_epsilon_score_points: parseFloat(getValue('star_epsilon_score_points', '0.5')) || 0.5,
        artist_top_percentile: parseFloat(getValue('sd_artist_pct', '0.10')) || 0.10,
        artist_top_percentile_large: parseFloat(getValue('sd_artist_pct_large', '0.25')) || 0.25,
        artist_catalog_large_threshold: parseInt(getValue('sd_artist_pct_large_threshold', '30'), 10) || 30,
        artist_medium_bump_percentile: parseFloat(getValue('sd_artist_medium_pct', '0.20')) || 0.20,
        artist_top_percentile_force_5_star: parseFloat(getValue('sd_force_5star_pct', '0.03')) || 0,
        artist_top_percentile_force_4_star: parseFloat(getValue('sd_force_4star_pct', '0.10')) || 0,
        listener_5star_z_threshold: parseFloat(getValue('listener_5star_z_threshold', '1.0')) || 1.0,
        always_check_discogs_video: getChecked('always_check_discogs_video', false),
        single_boost: parseFloat(getValue('single_boost', '1.15')) || 1.15,
        metadata_score_floor: parseFloat(getValue('metadata_score_floor', '5.0')) || 5.0,
        live_weight_penalty: parseFloat(getValue('live_weight_penalty', '0.5')) || 0.5,
        instrumental_weight_penalty: parseFloat(getValue('instrumental_weight_penalty', '0.8')) || 0.8,
        live_4star_requires_single: getChecked('live_4star_requires_single', true),
        single_organic_floor_score: parseFloat(getValue('single_organic_floor_score', '45.0')) || 45.0,
        single_organic_floor_listeners: parseFloat(getValue('single_organic_floor_listeners', '1000')) || 1000,
        log_ratio_enabled: getChecked('log_ratio_enabled', true),
        log_ratio_divergence_threshold: parseFloat(getValue('log_ratio_divergence_threshold', '0.85')) || 0.85,
        log_ratio_reject_lf_min_lb: parseInt(getValue('log_ratio_reject_lf_min_lb', '50'), 10) || 50,
        log_ratio_reject_lb_min_lf: parseInt(getValue('log_ratio_reject_lb_min_lf', '100'), 10) || 100,
        interlude_lb_outlier_enabled: getChecked('interlude_lb_outlier_enabled', true),
        interlude_lb_max_duration_s: parseInt(getValue('interlude_lb_max_duration_s', '180'), 10) || 180,
        interlude_lb_ratio_factor: parseFloat(getValue('interlude_lb_ratio_factor', '3.0')) || 3.0,
        interlude_lb_min_count: parseInt(getValue('interlude_lb_min_count', '500'), 10) || 500,
        // Various Artists compilation guard. Without these two the checkboxes
        // would appear to save and then silently revert on reload — the same
        // class of bug as every other field omitted from this object.
        compilation_tracklist_guard: getChecked('compilation_tracklist_guard', true),
        compilation_tracklist_floor: parseFloat(getValue('compilation_tracklist_floor', '0.6')) || 0.6,
        star_5: {
          album_z: parseNumber('star5_album_z', 1.0),
          artist_z: parseNumber('star5_artist_z', 1.2)
        },
        star_4: {
          album_z: parseNumber('star4_album_z', 0.5),
          artist_z: parseNumber('star4_artist_z', 1.0)
        },
        star_3: {
          album_z: parseNumber('star3_album_z', -0.5)
        },
        star_2: {
          album_z: parseNumber('star2_album_z', -1.2)
        },
        album_scaling: {
          peak_catalog_top_pct: parseFloat(getValue('era_peak_catalog_top_pct', '0.20')) || 0.20,
          peak_album_top_n: parseInt(getValue('era_peak_album_top_n', '3')) || 3,
          peak_max_5star_slots: parseInt(getValue('era_peak_max_5star_slots', '4')) || 4,
          solid_catalog_top_pct: parseFloat(getValue('era_solid_catalog_top_pct', '0.15')) || 0.15,
          solid_album_top_n: parseInt(getValue('era_solid_album_top_n', '3')) || 3,
          solid_max_5star_slots: parseInt(getValue('era_solid_max_5star_slots', '3')) || 3,
          minor_catalog_top_pct: parseFloat(getValue('era_minor_catalog_top_pct', '0.10')) || 0.10,
          minor_album_top_n: parseInt(getValue('era_minor_album_top_n', '3')) || 3,
          minor_max_5star_slots: parseInt(getValue('era_minor_max_5star_slots', '2')) || 2,
          peak_era_min_ratio: parseFloat(getValue('peak_era_min_ratio', '0.75')) || 0.75,
          solid_era_min_ratio: parseFloat(getValue('solid_era_min_ratio', '0.40')) || 0.40
        },
        // The album RATIO standout gate — the "shape of popularity" 5★ test.
        // Every field must be listed or it saves, appears to work, then
        // silently reverts on reload (the same bug class as omitting a field
        // from any other payload object here).
        album_ratio_standout: {
          enabled: getChecked('album_ratio_standout_enabled', true),
          runner_up_min: parseFloat(getValue('album_ratio_runner_up_min', '1.5')) || 1.5,
          median_min: parseFloat(getValue('album_ratio_median_min', '3.0')) || 3.0,
          floor_min: parseFloat(getValue('album_ratio_floor_min', '10.0')) || 10.0,
          min_passed: parseInt(getValue('album_ratio_min_passed', '3'), 10) || 3,
          live_runner_up_min: parseFloat(getValue('album_ratio_live_runner_up_min', '1.5')) || 1.5,
          live_median_min: parseFloat(getValue('album_ratio_live_median_min', '2.0')) || 2.0,
          live_floor_min: parseFloat(getValue('album_ratio_live_floor_min', '5.0')) || 5.0,
          live_min_passed: parseInt(getValue('album_ratio_live_min_passed', '2'), 10) || 2
        },
        // Compilation rating against the artist's ONLINE catalogue. Used only
        // when the local library is too thin to rank a credited artist's
        // tracks (the soundtrack / Various Artists case), where an artist's
        // global #1 otherwise cannot be told apart from filler.
        compilation_online_catalogue: {
          enabled: getChecked('compilation_online_catalogue_enabled', true),
          min_local_catalogue: parseInt(getValue('compilation_online_min_local_catalogue', '5'), 10) || 5,
          min_catalogue_size: parseInt(getValue('compilation_online_min_catalogue_size', '5'), 10) || 5,
          rank_percentile_5star: parseFloat(getValue('compilation_online_rank_percentile_5star', '0.02')) || 0.02,
          rank_percentile_4star: parseFloat(getValue('compilation_online_rank_percentile_4star', '0.10')) || 0.10,
          rank_percentile_3star: parseFloat(getValue('compilation_online_rank_percentile_3star', '0.35')) || 0.35,
          rank_percentile_2star: parseFloat(getValue('compilation_online_rank_percentile_2star', '0.65')) || 0.65
        }
      }
    ),
    metadata_update: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.metadata_update) || {},
      {
        album_name_source: getValue('metadata_update_album_name_source', 'dedupe'),
        album_name_update_target: getValue('metadata_update_album_name_target', 'db'),
        apply_during_scan: getValue('metadata_update_apply_during_scan', 'true') !== 'false',
        album_mbid_guard: Object.assign(
          {},
          ((window.pageConfig && window.pageConfig.metadata_update && window.pageConfig.metadata_update.album_mbid_guard) || {}),
          {
            enabled: getValue('metadata_update_mbid_guard_enabled', 'true') !== 'false',
            min_similarity: parseFloat(getValue('metadata_update_mbid_guard_min_similarity', '0.65')) || 0.65,
            allow_disc_folders: getValue('metadata_update_mbid_guard_allow_disc_folders', 'true') !== 'false'
          }
        ),
        update_on_files: Object.assign(
          {},
          ((window.pageConfig && window.pageConfig.metadata_update && window.pageConfig.metadata_update.update_on_files) || {}),
          {
            album_name: getChecked('metadata_update_field_album_name', false),
            year: getChecked('metadata_update_field_year', false),
            album_artist: getChecked('metadata_update_field_album_artist', false),
            genres: getChecked('metadata_update_field_genres', false),
            cover_art: getChecked('metadata_update_field_cover_art', false),
            lyrics: getChecked('metadata_update_field_lyrics', false)
          }
        )
      }
    ),
    popularity: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.popularity) || {},
      {
        weights: {
          lastfm: parseFloat(getValue('pop_weight_lastfm', '0.55')) || 0.55,
          listenbrainz: parseFloat(getValue('pop_weight_listenbrainz', '0.35')) || 0.35,
          age: parseFloat(getValue('pop_weight_age', '0.10')) || 0.10
        },
        track_timeout_seconds: parseInt(getValue('pop_track_timeout_seconds', '600'), 10) || 600,
        scan_threads: parseInt(getValue('pop_scan_threads', '4'), 10) || 4,
        prefetch_budget_seconds: parseInt(getValue('pop_prefetch_budget_seconds', '360'), 10) || 360,
        // Per-ALBUM full-scan budget. 0 DISABLES it (never skip an album), so
        // this is NOT guarded with `|| 900` — that would turn the "disabled"
        // value back into 15 minutes.
        album_timeout_seconds: (() => {
          const raw = parseInt(getValue('pop_album_timeout_seconds', '900'), 10);
          return Number.isFinite(raw) ? Math.max(0, raw) : 900;
        })(),
        // Per-artist full-scan budget. 0 DISABLES the timeout (never abandon
        // an artist), so this is NOT guarded with `|| 1800` — that would turn
        // the "disabled" value back into 30 minutes.
        artist_timeout_seconds: (() => {
          const raw = parseInt(getValue('pop_artist_timeout_seconds', '1800'), 10);
          return Number.isFinite(raw) ? Math.max(0, raw) : 1800;
        })()
      }
    ),
    statistics: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.statistics) || {},
      {
        exclude_from_median_below_seconds: parseFloat(getValue('statistics_exclude_below_seconds', '60')) || 0,
        short_track_star_cap: {
          enabled: getChecked('short_track_star_cap_enabled', true),
          max_duration_seconds: parseFloat(getValue('short_track_star_cap_max_duration_seconds', '70')) || 0,
          max_stars: parseInt(getValue('short_track_star_cap_max_stars', '2'), 10) || 2
        }
      }
    ),
    filters: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.filters) || {},
      {
        exclude_title_regex: getValue('statistics_exclude_title_regex', '')
      }
    ),
    tagging: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.tagging) || {},
      {
        write_tags_to_file: getChecked('tagging_write_enabled', true),
        skip_unchanged_ratings: getChecked('tagging_skip_unchanged', true),
        sync_album_tags_on_scan: getChecked('tagging_sync_album_scan', true),
        write_options: {
          ratings_only: getChecked('tagging_ratings_only', false),
          fill_missing_only: getChecked('tagging_fill_missing_only', false),
          embed_lyrics: getChecked('tagging_embed_lyrics', false)
        },
        preserve_file_timestamps: getChecked('tagging_preserve_timestamps', true)
      }
    ),
    genres: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.genres) || {},
      {
        min_weight: parseFloat(getValue('genre_min_weight', '0.25')) ?? 0.25,
        junk_filter: getChecked('genre_junk_filter', true),
        weights: {
          musicbrainz: parseFloat(getValue('genre_weight_musicbrainz', '0.40')) || 0.40,
          discogs: parseFloat(getValue('genre_weight_discogs', '0.25')) || 0.25,
          audiodb: parseFloat(getValue('genre_weight_audiodb', '0.20')) || 0.20,
          essentia: parseFloat(getValue('genre_weight_essentia', '0.20')) || 0.20,
          lastfm: parseFloat(getValue('genre_weight_lastfm', '0.10')) || 0.10,
          listenbrainz: parseFloat(getValue('genre_weight_listenbrainz', '0.15')) || 0.15,
          navidrome: parseFloat(getValue('genre_weight_navidrome', '0.30')) || 0.30,
          manual: parseFloat(getValue('genre_weight_manual', '0.30')) || 0.30
        },
        synonyms: (() => {
          const synonyms = {};
          getValue('genre_synonyms', '').split('\n').forEach(line => {
            const colonIndex = line.indexOf(':');
            if (colonIndex > 0) {
              const from = line.slice(0, colonIndex).trim().toLowerCase();
              const to = line.slice(colonIndex + 1).trim().toLowerCase();
              if (from && to) {
                synonyms[from] = to;
              }
            }
          });
          return synonyms;
        })()
      }
    ),
    api_integrations: {
      lastfm: {
        enabled: getChecked('api_lastfm_enabled'),
        api_key: getValue('api_lastfm_api_key')
      },
      listenbrainz: {
        enabled: getChecked('api_listenbrainz_enabled'),
        token: getValue('api_listenbrainz_token')
      },
      discogs: {
        enabled: getChecked('api_discogs_enabled'),
        token: getValue('api_discogs_token')
      },
      musicbrainz: { enabled: getChecked('api_musicbrainz_enabled', true) },
      audiodb: {
        enabled: getChecked('api_audiodb_enabled'),
        api_key: getValue('api_audiodb_api_key')
      }
    },
    strip_parentheses_filters: Array.from(document.querySelectorAll('#stripKeywordsList .badge'))
      .map(badge => badge.childNodes[0].textContent.trim())
      .filter(Boolean),
    upcoming_releases: { sources: getUpcomingSourcesForSave() },
    essentia: {
      script_path: getValue('essentia_script_path'),
      models_dir: getValue('essentia_models_dir'),
      mood_threshold: parseFloat(getValue('essentia_mood_threshold', '0.005')) || 0.005,
      per_file_timeout: parseInt(getValue('essentia_per_file_timeout', '300')) || 300,
      json_output_dir: getValue('essentia_json_output_dir'),
      tag_moods: getChecked('essentia_tag_moods', true),
      parse_json_features: getChecked('essentia_parse_json_features', true),
      delete_json_after_import: getChecked('essentia_delete_json_after_import', true),
      tag_genres: getChecked('essentia_tag_genres'),
      num_genres: parseInt(getValue('essentia_num_genres', '3')) || 3,
      genre_threshold: parseFloat(getValue('essentia_genre_threshold', '15')) || 15.0,
      genre_format: getValue('essentia_genre_format', 'parent_child')
    }
  };

  // Legacy feature keys have no readers in the current codebase — never
  // persist them (they were previously re-saved from pageConfig on every save).
  LEGACY_FEATURE_KEYS.forEach(key => delete config.features[key]);
  return config;
}

// ===== FILE NAME FORMAT PREVIEW =====

const FORMAT_PREVIEW_TRACK = {
  album_artist: 'Imagine Dragons',
  year: '2015',
  album: 'Smoke + Mirrors',
  track_number: '4',
  disc_number: '1',
  artist: 'Imagine Dragons',
  title: 'Radioactive',
};

function _formatTrackNumberPreview(trackNumber, discNumber) {
  const disc = discNumber ? parseInt(String(discNumber).split('/')[0], 10) : 1;
  const track = trackNumber ? parseInt(String(trackNumber).split('/')[0], 10) : 0;
  if (Number.isNaN(track)) return '00';
  if (disc > 1) return `${disc}${String(track).padStart(2, '0')}`;
  return String(track).padStart(2, '0');
}

function _extractYearPreview(value) {
  const m = String(value || '').match(/(19|20)\d{2}/);
  return m ? m[0] : 'Unknown';
}

function _sanitizeSegmentPreview(value) {
  return String(value || '').replace(/[<>:"|?*\\]/g, '_').trim().replace(/^\.+|\.+$/g, '');
}

function updateFileNameFormatPreview() {
  const input = document.getElementById('downloads_file_name_format');
  const panel = document.getElementById('fileNameFormatPreview');
  const pathEl = document.getElementById('fileNameFormatPreviewPath');
  if (!input || !panel || !pathEl) return;

  const fmt = input.value.trim() || '{album_artist}/{year} - {album}/{track_number}. {artist} - {title}';
  const t = FORMAT_PREVIEW_TRACK;
  const vars = {
    album_artist: _sanitizeSegmentPreview(t.album_artist),
    year: _extractYearPreview(t.year),
    album: _sanitizeSegmentPreview(t.album),
    track_number: _formatTrackNumberPreview(t.track_number, t.disc_number),
    artist: _sanitizeSegmentPreview(t.artist),
    title: _sanitizeSegmentPreview(t.title),
  };

  let rendered;
  try {
    rendered = fmt.replace(/\{(\w+)\}/g, (_, key) => (key in vars ? vars[key] : `{${key}}`));
  } catch (err) {
    rendered = fmt;
  }

  // Sanitize each path segment, mirroring the backend's _build_target_path.
  const parts = rendered
    .replace(/\\/g, '/')
    .replace(/^\/+/, '')
    .split('/')
    .map(seg => _sanitizeSegmentPreview(seg))
    .filter(seg => seg && seg !== '.' && seg !== '..');

  const relativePath = parts.join('/') || 'Unknown Artist';
  pathEl.textContent = '/music/' + relativePath + '.flac';
  panel.classList.remove('d-none');
}

// ===== SAVE CONFIG =====

const SECTION_LABELS = {
  navidrome_users: 'Music Users',
  matching: 'Track Matching',
  logging: 'Logging',
  slskd: 'slskd / Soulseek',
  wikidata: 'Artist Biography',
  queue: 'Queue Matching',
  lastfm: 'Last.fm Advanced',
  filesystem: 'File System',
  playlists: 'Essential Playlists',
  downloads: 'Downloads',
  watcher: 'Automation Services',
  features: 'Features & Schedulers',
  single_detection: 'Single Detection & Star Ratings',
  popularity: 'Popularity Weights',
  tagging: 'File Metadata (Tag Writing)',
  genres: 'Genre Aggregation',
  api_integrations: 'API Integrations',
  strip_parentheses_filters: 'Search Filters',
  upcoming_releases: 'Upcoming Releases Sources',
  essentia: 'Essentia Mood & Genre Scan',
};

function summarizeChangedSections(nextConfig, prevConfig) {
  const changed = [];
  const prev = prevConfig || {};
  Object.keys(nextConfig || {}).forEach(key => {
    if (JSON.stringify(nextConfig[key] ?? null) !== JSON.stringify(prev[key] ?? null)) {
      changed.push(SECTION_LABELS[key] || key);
    }
  });
  return changed;
}

function validateNumericBounds() {
  const problems = [];
  document.querySelectorAll('#configForm input[type="number"]').forEach(input => {
    if (!input.value && !input.required) return;
    const val = parseFloat(input.value);
    if (Number.isNaN(val)) return;
    const min = input.min !== '' && input.min != null ? parseFloat(input.min) : null;
    const max = input.max !== '' && input.max != null ? parseFloat(input.max) : null;
    const labelEl = input.closest('.col-12, .col-md-3, .col-md-4, .col-md-6, .col-sm-6, .col-lg-3, .col-lg-4, .col-6')?.querySelector('label');
    const label = (labelEl ? labelEl.textContent.trim() : input.id || 'field').replace(/\s+/g, ' ');
    if (min != null && val < min) problems.push(`${label}: ${val} is below the minimum ${min}`);
    if (max != null && val > max) problems.push(`${label}: ${val} exceeds the maximum ${max}`);
  });
  return problems;
}

function saveConfig() {
  const saveBtn = document.getElementById('saveBtn');
  
  if (!saveBtn) {
    console.error('[saveConfig] Save button not found!');
    showToast('Error', 'Save button not found in DOM', 'error');
    return;
  }
  
  // Capture original state before any modifications
  const originalDisabled = saveBtn.disabled;
  const originalHTML = saveBtn.innerHTML;
  
  // Set button to loading state
  saveBtn.disabled = true;
  saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Saving...';
  
  // Safety timeout to force reset button after 60 seconds (longer than fetch timeout)
  // This prevents the button from being stuck forever if the promise chain breaks
  const safetyReset = setTimeout(() => {
    console.warn('[saveConfig] Safety timeout reached - forcing button reset');
    saveBtn.disabled = originalDisabled;
    saveBtn.innerHTML = originalHTML;
    showToast('Error', 'Save operation timed out. Please check the application logs and try again.', 'error');
  }, 60000);

  // The button must ALWAYS be restored — if config collection throws (a
  // missing input, malformed field) the request never starts and without
  // this guard the page would sit on "Saving..." forever.
  let config;
  try {
    config = buildConfigObject();
  } catch (err) {
    console.error('[saveConfig] buildConfigObject failed:', err);
    clearTimeout(safetyReset);
    saveBtn.disabled = originalDisabled;
    saveBtn.innerHTML = originalHTML;
    showToast('Error', 'Could not collect config: ' + (err && err.message ? err.message : err), 'error');
    return;
  }

  // Client-side bounds check before the payload hits the server — an
  // out-of-range value (e.g. a retry interval over the max) would otherwise
  // be silently clamped by the backend or reject the whole save.
  const numericProblems = validateNumericBounds();
  if (numericProblems.length > 0) {
    clearTimeout(safetyReset);
    saveBtn.disabled = originalDisabled;
    saveBtn.innerHTML = originalHTML;
    showToast('Error', 'Please fix these values: ' + numericProblems.join(' • '), 'error');
    return;
  }

  const urlElement = document.querySelector('form#configForm')?.dataset?.saveUrl || '/config/save-json';

  console.log('[saveConfig] Sending request to:', urlElement);

  // Give the server a hard deadline so a hung request (e.g. a background
  // task blocking the worker) surfaces as an error instead of an eternal
  // spinner.
  const controller = new AbortController();
  const saveTimeout = setTimeout(() => controller.abort(), 45000);

  fetch(urlElement, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
    signal: controller.signal
  })
  .then(async response => {
    console.log('[saveConfig] Response status:', response.status);
    const contentType = response.headers.get('content-type') || '';
    console.log('[saveConfig] Response content-type:', contentType);
    
    if (!contentType.includes('application/json')) {
      const text = await response.text().catch(() => '');
      console.error('[saveConfig] Non-JSON response:', text.substring(0, 200));
      throw new Error('Server returned non-JSON response: ' + text.substring(0, 100));
    }
    
    const data = await response.json().catch(err => {
      console.error('[saveConfig] JSON parse failed:', err);
      throw new Error('Failed to parse server response');
    });
    
    return { status: response.status, data };
  })
  .then(result => {
    console.log('[saveConfig] Result:', result);
    if (result.status !== 200) {
      showToast('Error', result.data.error || 'Failed to save configuration', 'error');
    } else if (result.data.success) {
      const changed = summarizeChangedSections(config, window.pageConfig);
      const summary = changed.length
        ? 'Configuration saved — updated: ' + changed.join(', ')
        : 'Configuration saved (no sections changed)';
      showToast('Success', summary, 'success');
      window.pageConfig = config;
    } else {
      showToast('Error', result.data.error || 'Failed to save configuration', 'error');
    }
  })
  .catch(error => {
    console.error('[saveConfig] Fetch error:', error);
    if (error && error.name === 'AbortError') {
      showToast('Error', 'Save timed out after 45s — the server did not respond. Check the app logs.', 'error');
    } else {
      showToast('Error', 'Network error: ' + (error.message || 'Unknown error'), 'error');
    }
  })
  .finally(() => {
    console.log('[saveConfig] Resetting button state');
    clearTimeout(saveTimeout);
    clearTimeout(safetyReset);
    // Always reset button to its original state
    saveBtn.disabled = originalDisabled;
    saveBtn.innerHTML = originalHTML;
  });
}

// ===== RAW YAML =====

function saveRawYaml() {
  const saveBtn = document.querySelector('#rawYamlModal .btn-primary');
  saveBtn.disabled = true;
  const originalText = saveBtn.textContent;
  saveBtn.textContent = 'Saving...';

  const configContent = document.getElementById('config_content').value;
  const formData = new FormData();
  formData.append('config_content', configContent);

  const urlElement = document.querySelector('#rawYamlModal')?.dataset?.saveUrl || '/config/editor';

  fetch(urlElement, {
    method: 'POST',
    body: formData
  })
  .then(() => {
    showToast('Success', 'Configuration saved successfully', 'success');
    const modalEl = document.getElementById('rawYamlModal');
    if (modalEl) bootstrap.Modal.getInstance(modalEl)?.hide();
  })
  .catch(error => {
    showToast('Error', 'Network error: ' + error.message, 'error');
  })
  .finally(() => {
    saveBtn.disabled = false;
    saveBtn.textContent = originalText;
  });
}

// ===== SYNC RATINGS =====

function syncRatingsNow() {
  const btn = document.getElementById('syncRatingsNowBtn');
  if (!btn) return;
  btn.disabled = true;
  const originalHtml = btn.innerHTML;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Syncing...';

  fetch('/api/navidrome/ratings/sync-now', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({})
  })
  .then(async response => {
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, data };
  })
  .then(result => {
    const data = result.data || {};
    if (!result.ok || data.success === false) {
      showToast('Sync Failed', data.error || 'Failed to sync ratings', 'error');
      return;
    }
    const users = Array.isArray(data.users) ? data.users : [];
    const usersWithErrors = users.filter(u => (u.failed || 0) > 0);
    const summary = `Synced ${data.synced_total || 0}/${data.attempted_total || 0} rating updates across ${data.users_total || 0} user(s)`;
    if (usersWithErrors.length > 0) {
      const failingNames = usersWithErrors.map(u => u.display_name || u.username || 'Unknown user').join(', ');
      showToast('Partial Sync Complete', `${summary}. Issues: ${failingNames}`, 'warning');
    } else {
      showToast('Sync Complete', summary, 'success');
    }
  })
  .catch(error => {
    showToast('Sync Failed', error.message || 'Network error while syncing ratings', 'error');
  })
  .finally(() => {
    btn.disabled = false;
    btn.innerHTML = originalHtml;
  });
}

function toggleSyncRatingsToAllUsers(enabled) {
  fetch('/api/features/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sync_ratings_to_all_users: enabled })
  })
  .then(async response => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.success === false) {
      showToast('Error', data.error || 'Failed to update setting', 'error');
      const toggle = document.getElementById('syncRatingsToAllUsersToggle');
      if (toggle) toggle.checked = !enabled;
      return;
    }
    showToast('Setting Saved', enabled
      ? 'Star ratings will be synced to all Navidrome users after each scan and for individual track ratings.'
      : 'Star ratings will only be synced to the primary Navidrome user.', 'success');
  })
  .catch(error => {
    showToast('Error', error.message || 'Network error', 'error');
    const toggle = document.getElementById('syncRatingsToAllUsersToggle');
    if (toggle) toggle.checked = !enabled;
  });
}

// ===== MULTI-USER MANAGEMENT =====

function addNewUser() {
  const container = document.getElementById('usersContainer');
  const emptyMessage = document.getElementById('emptyUsersMessage');
  if (emptyMessage) emptyMessage.remove();
  const existingCards = container.querySelectorAll('.user-card');
  const userIndex = existingCards.length;

  const userCard = document.createElement('div');
  userCard.className = 'user-card mb-4 p-3 border rounded';
  userCard.setAttribute('data-user-index', userIndex);
  userCard.innerHTML = `
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h6 class="mb-0">User ${userIndex + 1}: <span class="user-display-value">New User</span></h6>
      <button type="button" class="btn btn-sm btn-outline-danger" onclick="removeUser(${userIndex})">
        <i class="bi bi-trash"></i> Remove
      </button>
    </div>
    <div class="mb-3">
      <h6 class="text-primary mb-2"><i class="bi bi-music-note-beamed"></i> Navidrome</h6>
      <div class="row g-2">
        <div class="col-12">
          <label class="form-label form-label-sm">Display Name</label>
          <input type="text" class="form-control form-control-sm user-display-name" placeholder="My Name" onchange="updateUserTitle(this)">
        </div>
        <div class="col-12">
          <label class="form-label form-label-sm">Navidrome Base URL</label>
          <input type="text" class="form-control form-control-sm user-nav-url" placeholder="http://localhost:4533">
        </div>
        <div class="col-12 col-sm-6">
          <label class="form-label form-label-sm">Username</label>
          <input type="text" class="form-control form-control-sm user-nav-username" placeholder="username">
        </div>
        <div class="col-12 col-sm-6">
          <label class="form-label form-label-sm">Password</label>
          <input type="password" class="form-control form-control-sm user-nav-password" placeholder="••••••••">
        </div>
      </div>
    </div>
    <div class="mb-3">
      <h6 class="text-info mb-2"><i class="bi bi-database-fill"></i> Last.fm</h6>
      <div class="row g-2">
        <div class="col-12">
          <label class="form-label form-label-sm">Last.fm Username</label>
          <input type="text" class="form-control form-control-sm user-lastfm-username" placeholder="lastfm_username">
          <small class="form-text text-muted">Your Last.fm username (for personalized recommendations)</small>
        </div>
      </div>
    </div>
    <div class="mb-3">
      <h6 class="text-secondary mb-2"><i class="bi bi-headphones"></i> ListenBrainz</h6>
      <div class="row g-2">
        <div class="col-12">
          <label class="form-label form-label-sm">ListenBrainz User Token</label>
          <input type="text" class="form-control form-control-sm user-listenbrainz-token" placeholder="listenbrainz_user_token">
          <small class="form-text text-muted">Your ListenBrainz personal token from <a href="https://listenbrainz.org/settings/" target="_blank">listenbrainz.org/settings/</a></small>
        </div>
      </div>
    </div>
  `;
  container.appendChild(userCard);
}

function removeUser(idx) {
  const userCards = document.querySelectorAll('.user-card');
  if (userCards[idx]) userCards[idx].remove();
}

function updateUserTitle(displayNameInput) {
  const userCard = displayNameInput.closest('.user-card');
  if (!userCard) return;
  const displayValue = userCard.querySelector('.user-display-value');
  if (displayValue) displayValue.textContent = displayNameInput.value.trim() || 'New User';
}

// ===== ESSENTIA =====

function toggleEssentiaGenreSettings() {
  const cb = document.getElementById('essentia_tag_genres');
  const panel = document.getElementById('essentia-genre-settings');
  if (panel) panel.classList.toggle('d-none', !cb || !cb.checked);
}

let _essentiaDownloadPollTimer = null;

function startEssentiaDownload() {
  const btn = document.getElementById('btn-essentia-download');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span> Starting…';

  fetch('/api/essentia/download-models', { method: 'POST' })
    .then(r => {
      const contentType = r.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) {
        throw new Error('Server returned non-JSON response (HTML or plain text)');
      }
      return r.json();
    })
    .then(data => {
      if (data.status === 'already_running') {
        _essentiaSetStatusText('Download already in progress…');
        document.getElementById('essentia-download-status').classList.remove('d-none');
      }
      pollEssentiaDownloadStatus();
    })
    .catch(err => {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Download Models & Script';
      showToast('Error', 'Could not start Essentia download: ' + err.message, 'error');
    });
}

function pollEssentiaDownloadStatus() {
  if (_essentiaDownloadPollTimer) clearTimeout(_essentiaDownloadPollTimer);
  const CLONE_PCT = 10;
  const DOWNLOAD_RANGE_PCT = 85;
  const EXPECTED_FILES = 5;

  fetch('/api/essentia/download-status')
    .then(r => {
      const contentType = r.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) {
        throw new Error('Server returned non-JSON response (HTML or plain text)');
      }
      return r.json();
    })
    .then(data => {
      const statusDiv = document.getElementById('essentia-download-status');
      const bar = document.getElementById('essentia-download-bar');
      const btn = document.getElementById('btn-essentia-download');
      if (!data || data.status === 'idle') {
        if (statusDiv) statusDiv.classList.add('d-none');
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-cloud-download"></i> Download Models &amp; Script';
        return;
      }
      if (statusDiv) statusDiv.classList.remove('d-none');
      const total = data.files_total || EXPECTED_FILES;
      const done = data.files_done || 0;
      bar.classList.remove('bg-success', 'bg-danger');
      bar.classList.add('progress-bar-striped', 'progress-bar-animated');

      if (data.status === 'complete' || data.status === 'installed') {
        bar.style.width = '100%';
        bar.classList.remove('progress-bar-animated');
        bar.classList.add('bg-success');
        const where = data.models_dir ? ' — models saved to ' + data.models_dir : '';
        _essentiaSetStatusText('✅ ' + (data.status === 'installed' ? 'Models already installed' : 'Download complete') + (where || ''));
        btn.disabled = true;
        btn.innerHTML = '<i class="bi bi-check-circle"></i> Models Installed';
      } else if (data.status === 'error') {
        bar.classList.remove('progress-bar-animated');
        bar.classList.add('bg-danger');
        bar.style.width = '100%';
        _essentiaSetStatusText('❌ Error: ' + (data.error || 'unknown error'));
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-cloud-download"></i> Retry Download';
      } else {
        const pct = data.status === 'cloning_script'
          ? CLONE_PCT
          : Math.max(CLONE_PCT, Math.round(CLONE_PCT + (done / total) * DOWNLOAD_RANGE_PCT));
        bar.style.width = pct + '%';
        _essentiaSetStatusText(data.current_step || 'Downloading…');
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span> Downloading…';
        _essentiaDownloadPollTimer = setTimeout(pollEssentiaDownloadStatus, 2000);
      }
    })
    .catch(() => {
      _essentiaDownloadPollTimer = setTimeout(pollEssentiaDownloadStatus, 3000);
    });
}

function _essentiaSetStatusText(msg) {
  const el = document.getElementById('essentia-download-status-text');
  if (el) el.textContent = msg;
}

// ===== UPCOMING RELEASES SOURCES =====

let upcomingSources = [];

// Valid Wikipedia column types (order matters: position in the table).
const UPCOMING_COLUMN_TYPES = ['day', 'artist', 'album', 'genre'];

function buildColumnSelects(prefix, values) {
  const current = Array.isArray(values) && values.length ? values : ['day', 'artist', 'album'];
  let html = '<div class="row g-2">';
  for (let i = 0; i < 4; i++) {
    html += `<div class="col-6 col-md-3">
      <label class="form-label small fw-normal text-muted">Column ${i + 1}</label>
      <select class="form-select form-select-sm" id="${prefix}col${i}">
        <option value="">— none —</option>`;
    UPCOMING_COLUMN_TYPES.forEach(t => {
      const sel = current[i] === t ? ' selected' : '';
      html += `<option value="${t}"${sel}>${t}</option>`;
    });
    html += '</select></div>';
  }
  return html + '</div>';
}

function readColumnSelects(prefix) {
  const cols = [];
  for (let i = 0; i < 4; i++) {
    const el = document.getElementById(prefix + 'col' + i);
    if (el && el.value) cols.push(el.value);
  }
  return cols;
}

function validateSourceKey(key, existingKeys) {
  if (!key) return 'Source key is required';
  if (!/^[a-zA-Z0-9_-]+$/.test(key)) return 'Source key may only contain letters, numbers, underscores and dashes';
  if (!/(19|20)\d{2}/.test(key)) return 'Source key must include a 4-digit year (e.g. 2026_jazz) for date parsing';
  if (existingKeys && existingKeys.includes(key)) return `Source key "${key}" already exists`;
  return null;
}

function initUpcomingSourcesUI() {
  const config = window.pageConfig || {};
  const savedSources = ((config.upcoming_releases || {}).sources || []);
  if (Array.isArray(savedSources) && savedSources.length > 0) {
    upcomingSources = savedSources.map(s => Object.assign({}, s));
  } else {
    const y = new Date().getFullYear();
    upcomingSources = [
      { key: `${y}_albums`,      name: `General ${y} Albums`,        url: `https://en.wikipedia.org/wiki/List_of_${y}_albums`,            columns: ['day','artist','album','genre'], enabled: true },
      { key: `${y}_heavy_metal`, name: `Heavy Metal ${y}`,           url: `https://en.wikipedia.org/wiki/${y}_in_heavy_metal_music`,      columns: ['day','artist','album'],         enabled: true },
      { key: `${y}_rock`,        name: `Rock Music ${y}`,            url: `https://en.wikipedia.org/wiki/${y}_in_rock_music`,             columns: ['day','artist','album'],         enabled: true },
      { key: `${y}_kpop`,        name: `K-Pop/Korean Music ${y}`,    url: `https://en.wikipedia.org/wiki/${y}_in_South_Korean_music`,     columns: ['day','album','artist'],         enabled: true },
      { key: `${y}_american`,    name: `American Music ${y}`,        url: `https://en.wikipedia.org/wiki/${y}_in_American_music`,         columns: ['day','album','artist'],         enabled: true },
    ];
  }
  renderUpcomingSourcesList();
}

function renderUpcomingSourcesList() {
  const container = document.getElementById('upcomingSourcesList');
  if (!container) return;
  if (upcomingSources.length === 0) {
    container.innerHTML = '<p class="text-muted fst-italic">No sources configured. Add a source below.</p>';
    return;
  }
  container.innerHTML = upcomingSources.map((src, idx) => buildUpcomingSourceRow(src, idx)).join('');
}

function buildUpcomingSourceRow(src, idx) {
  const enabled = src.enabled !== false;
  const cols = Array.isArray(src.columns) ? src.columns : ['day','artist','album'];
  const colBadges = cols.map(c => {
    if (c === 'genre') {
      return `<span class="badge bg-secondary" title="Skipped during parsing" style="text-decoration:line-through;">${escapeHtml(c)}</span>`;
    }
    return `<span class="badge bg-primary">${escapeHtml(c)}</span>`;
  }).join(' <i class="bi bi-arrow-right-short text-muted"></i> ');

  return `<div class="border rounded p-3 mb-2" id="srcRow_${idx}" style="background: var(--secondary-bg, #1e1e1e);">
    <div class="d-flex flex-column flex-md-row align-items-md-start gap-3">
      <div class="form-check form-switch pt-1 flex-shrink-0">
        <input class="form-check-input" type="checkbox" role="switch" id="srcEnabled_${idx}"
               ${enabled ? 'checked' : ''} onchange="toggleUpcomingSourceEnabled(${idx})">
      </div>
      <div class="flex-grow-1 overflow-hidden">
        <div class="d-flex flex-wrap align-items-center gap-2 mb-1">
          <strong>${escapeHtml(src.name)}</strong>
          <code class="text-muted small">${escapeHtml(src.key)}</code>
          ${!enabled ? '<span class="badge bg-secondary">Disabled</span>' : ''}
        </div>
        <div class="text-muted small mb-2 text-truncate">
          <i class="bi bi-link-45deg"></i>
          <a href="${escapeHtml(src.url)}" target="_blank" rel="noopener noreferrer" class="text-muted">${escapeHtml(src.url)}</a>
        </div>
        <div class="d-flex flex-wrap align-items-center gap-1">
          <small class="text-muted me-1">Column order:</small>
          ${colBadges}
        </div>
      </div>
      <div class="d-flex gap-2 flex-shrink-0">
        <button type="button" class="btn btn-outline-secondary btn-sm" onclick="editUpcomingSource(${idx})" title="Edit source">
          <i class="bi bi-pencil"></i>
        </button>
        <button type="button" class="btn btn-outline-danger btn-sm" onclick="removeUpcomingSource(${idx})" title="Remove source">
          <i class="bi bi-trash"></i>
        </button>
      </div>
    </div>
  </div>`;
}

function toggleUpcomingSourceEnabled(idx) {
  const cb = document.getElementById('srcEnabled_' + idx);
  if (cb && upcomingSources[idx]) upcomingSources[idx].enabled = cb.checked;
}

function editUpcomingSource(idx) {
  const src = upcomingSources[idx];
  if (!src) return;
  const row = document.getElementById('srcRow_' + idx);
  if (!row) return;
  const cols = Array.isArray(src.columns) ? src.columns : ['day', 'artist', 'album'];
  row.innerHTML = `<div class="row g-2 mb-2">
    <div class="col-12 col-md-4">
      <label class="form-label small fw-bold">Source Key</label>
      <input type="text" class="form-control form-control-sm" id="editSrcKey_${idx}" value="${escapeHtml(src.key)}" placeholder="e.g. 2026_jazz">
      <div class="form-text">Must include a 4-digit year</div>
    </div>
    <div class="col-12 col-md-4">
      <label class="form-label small fw-bold">Display Name</label>
      <input type="text" class="form-control form-control-sm" id="editSrcName_${idx}" value="${escapeHtml(src.name)}">
    </div>
    <div class="col-12 col-md-4">
      <label class="form-label small fw-bold">Column Order</label>
      ${buildColumnSelects('editSrcCols_' + idx + '_', cols)}
    </div>
    <div class="col-12">
      <label class="form-label small fw-bold">Wikipedia URL</label>
      <input type="url" class="form-control form-control-sm" id="editSrcUrl_${idx}" value="${escapeHtml(src.url)}">
    </div>
  </div>
  <div class="d-flex gap-2">
    <button type="button" class="btn btn-primary btn-sm" onclick="saveUpcomingSourceEdit(${idx})">
      <i class="bi bi-check-circle"></i> Save
    </button>
    <button type="button" class="btn btn-outline-secondary btn-sm" onclick="renderUpcomingSourcesList()">Cancel</button>
  </div>`;
}

function saveUpcomingSourceEdit(idx) {
  const key  = (document.getElementById('editSrcKey_' + idx)?.value  || '').trim();
  const name = (document.getElementById('editSrcName_' + idx)?.value || '').trim();
  const url  = (document.getElementById('editSrcUrl_' + idx)?.value  || '').trim();
  if (!key || !name || !url) { showToast('Error', 'Key, name, and URL are required', 'warning'); return; }
  const keyErr = validateSourceKey(key, upcomingSources.map(s => s.key).filter((_, i) => i !== idx));
  if (keyErr) { showToast('Error', keyErr, 'warning'); return; }
  const cols = readColumnSelects('editSrcCols_' + idx + '_');
  if (!cols.includes('artist') || !cols.includes('album')) {
    showToast('Error', 'Column order must include both "artist" and "album"', 'warning'); return;
  }
  upcomingSources[idx] = { ...upcomingSources[idx], key, name, url, columns: cols };
  renderUpcomingSourcesList();
}

function removeUpcomingSource(idx) {
  const src = upcomingSources[idx];
  if (!src) return;
  if (!confirm('Remove source "' + src.name + '"?')) return;
  upcomingSources.splice(idx, 1);
  renderUpcomingSourcesList();
}

function toggleAddUpcomingSourceForm() {
  const form = document.getElementById('addUpcomingSourceForm');
  if (!form) return;
  const willShow = form.classList.contains('d-none');
  form.classList.toggle('d-none');
  if (willShow) {
    const container = document.getElementById('newSrcColumns');
    if (container) container.innerHTML = buildColumnSelects('newSrcCol', ['day', 'artist', 'album']);
  }
}

function addUpcomingSource() {
  const key     = (document.getElementById('newSrcKey')?.value     || '').trim();
  const name    = (document.getElementById('newSrcName')?.value    || '').trim();
  const url     = (document.getElementById('newSrcUrl')?.value     || '').trim();
  if (!key || !name || !url) { showToast('Error', 'Key, name, and URL are required', 'warning'); return; }
  const keyErr = validateSourceKey(key, upcomingSources.map(s => s.key));
  if (keyErr) { showToast('Error', keyErr, 'warning'); return; }
  const cols = readColumnSelects('newSrcCol');
  if (!cols.includes('artist') || !cols.includes('album')) {
    showToast('Error', 'Column order must include both "artist" and "album"', 'warning'); return;
  }
  upcomingSources.push({ key, name, url, columns: cols, enabled: true });
  document.getElementById('newSrcKey').value = '';
  document.getElementById('newSrcName').value = '';
  document.getElementById('newSrcUrl').value = '';
  const container = document.getElementById('newSrcColumns');
  if (container) container.innerHTML = buildColumnSelects('newSrcCol', ['day', 'artist', 'album']);
  toggleAddUpcomingSourceForm();
  renderUpcomingSourcesList();
}

function parseUpcomingCols(raw) {
  const valid = ['day','artist','album','genre'];
  return raw.split(',').map(c => c.trim().toLowerCase()).filter(c => valid.includes(c));
}

function getUpcomingSourcesForSave() {
  upcomingSources.forEach((src, idx) => {
    const cb = document.getElementById('srcEnabled_' + idx);
    if (cb) src.enabled = cb.checked;
  });
  return upcomingSources.map(s => ({
    key: s.key,
    name: s.name,
    url: s.url,
    columns: Array.isArray(s.columns) ? s.columns : ['day','artist','album'],
    enabled: s.enabled !== false,
  }));
}

// ===== RETRY SCHEDULER =====

function getRetrySchedulerStatus() {
  fetch('/api/downloads/scheduler/status')
    .then(r => {
      const contentType = r.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) {
        throw new Error('Server returned non-JSON response (HTML or plain text)');
      }
      return r.json();
    })
    .then(data => {
      const statusIcon = document.getElementById('schedulerStatusIcon');
      const statusText = document.getElementById('schedulerStatusText');
      const statusDetail = document.getElementById('schedulerStatusDetail');
      const startBtn = document.getElementById('startSchedulerBtn');
      const stopBtn = document.getElementById('stopSchedulerBtn');
      const isRunning = data.running || false;
      if (statusIcon) {
        statusIcon.className = isRunning ? 'bi bi-circle-fill text-success' : 'bi bi-circle-fill text-muted';
        statusIcon.title = isRunning ? 'Running' : 'Stopped';
      }
      if (statusText) statusText.textContent = isRunning ? 'Running' : 'Stopped';
      if (statusDetail) {
        statusDetail.innerHTML = isRunning
          ? '<span class="badge bg-success"><i class="bi bi-play-circle"></i> Running</span> - Retry scheduler is actively monitoring and retrying failed downloads every configured interval.'
          : '<span class="badge bg-secondary"><i class="bi bi-stop-circle"></i> Stopped</span> - Retry scheduler is not running. Click "Start" to begin automatic retry management.';
      }
      if (startBtn) startBtn.disabled = isRunning;
      if (stopBtn) stopBtn.disabled = !isRunning;
    })
    .catch(e => {
      console.error('Error getting scheduler status:', e);
      const statusDetail = document.getElementById('schedulerStatusDetail');
      if (statusDetail) statusDetail.textContent = 'Unable to load status';
    });
}

function startRetryScheduler() {
  const btn = document.getElementById('startSchedulerBtn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Starting...';
  }
  fetch('/api/downloads/scheduler/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' }
  })
  .then(r => {
    const contentType = r.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      throw new Error('Server returned non-JSON response (HTML or plain text)');
    }
    return r.json();
  })
  .then(data => {
    if (data.success) {
      showToast('Success', 'Retry scheduler started', 'success');
      setTimeout(getRetrySchedulerStatus, 500);
    } else {
      showToast('Error', data.error || 'Failed to start scheduler', 'error');
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> Start'; }
    }
  })
  .catch(e => {
    showToast('Error', 'Network error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> Start'; }
  });
}

function stopRetryScheduler() {
  const btn = document.getElementById('stopSchedulerBtn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Stopping...';
  }
  fetch('/api/downloads/scheduler/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' }
  })
  .then(r => {
    const contentType = r.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      throw new Error('Server returned non-JSON response (HTML or plain text)');
    }
    return r.json();
  })
  .then(data => {
    if (data.success) {
      showToast('Warning', 'Retry scheduler stopped', 'warning');
      setTimeout(getRetrySchedulerStatus, 500);
    } else {
      showToast('Error', data.error || 'Failed to stop scheduler', 'error');
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-stop-fill"></i> Stop'; }
    }
  })
  .catch(e => {
    showToast('Error', 'Network error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-stop-fill"></i> Stop'; }
  });
}

// ===== INIT =====

document.addEventListener('DOMContentLoaded', function() {
  if (document.getElementById('stripKeywordsList')) {
    initializeStripKeywords();
  }
  if (document.getElementById('upcomingSourcesList')) {
    initUpcomingSourcesUI();
  }
  if (document.getElementById('downloads_file_name_format')) {
    updateFileNameFormatPreview();
  }
  if (document.getElementById('btn-essentia-download')) {
    // Reflect the Essentia install state on page load (hide/enable the
    // download button when models are already present).
    pollEssentiaDownloadStatus();
  }
  getRetrySchedulerStatus();
});
