/* ==========================================================================
   static/js/pages/artist-corrections.js
   Per-artist corrections console — duplicate artists/tracks, album MBID
   conflicts, disc-number inconsistencies, duplicate album splits, title
   mismatches vs MusicBrainz, and missing MusicBrainz tracks.

   Load order: utils/dom.js → utils/api.js → utils/poller.js → ui/toast.js →
               ui/modal.js → ui/confirm.js → ui/button-state.js, then this file.
   All of those come from base.html.

   Page data is read from the template's <script id="page-data"> block
   ({artistName, mbAlbums}) — the same contract the original inline script used.

   ── WHAT WAS REMOVED FROM THE TEMPLATE ────────────────────────────────────
   templates/pages/artist_corrections.html carried a 602-line inline <script>
   with 19 functions and its own escapeHtml. The script is this file; the local
   escapeHtml is gone in favour of utils/dom.js's (equivalent — the local one
   did escape quotes, which is why its attribute interpolation was safe).

   ── BUGS FIXED ────────────────────────────────────────────────────────────
   1. `document.getElementById('confirmMergeBtn').addEventListener(...)` RAN AT
      PARSE TIME, unguarded. The script sits after the markup so the element
      existed — but any layout change that moved or renamed #confirmMergeBtn
      would have thrown a TypeError and killed EVERY function below it in the
      same script block. It is now a guarded delegated handler.

   2. INLINE onclick WITH JSON+"ESCAPED" ARGUMENTS. Two call sites assembled
      JavaScript source into an attribute:

          showMergeDialog('${dup.mbid}', '${dup.canonical_mb}', ${total},
                          ${affected}, ${JSON.stringify(sourceVariations)
                                         .replace(/"/g, '&quot;')})

          mergeAlbums({{ loop.index }},
                      {{ grp.albums | map(attribute='album') | list | tojson | safe }})

      The first hand-rolled quote escaping around a JSON array; the second
      injected raw JSON with `| safe`. An artist or album name containing a
      quote or a `</script>`-style sequence breaks them. Both now pass a
      generated KEY, with the payload held in a module registry — the same
      pattern the original already used successfully for the other tables.

   3. `alert()` / `confirm()` / `window.prompt()` → toast / ui.confirm. The
      MusicBrainz release picker still uses a prompt (see the note on
      searchAndApplyAlbumMbid) because replacing it with a modal is a UI change
      rather than a migration.

   4. Raw `fetch` + `.json()` → api.getJson/postJson. The old calls checked
      `result.success` but had no handling for a non-JSON body, so a session
      expiry surfaced as "Unexpected token '<'".

   5. `location.reload()` after every mutation stays — the page is entirely
      server-rendered, so re-loading is what re-runs the corrections queries.

   ── DESTRUCTIVE ACTIONS ───────────────────────────────────────────────────
   deleteDuplicateTrack, mergeAlbums, mergeArtists and clearDiscNumber rewrite
   database rows AND audio file tags (and the artist merge moves files on disk).
   Each goes through ui.confirm with a danger tone and the affected names in the
   body; do not weaken those confirmations.
   ========================================================================== */

(function (global) {
  'use strict';

  const pageData = (function readPageData() {
    const el = document.getElementById('page-data');
    if (!el) {
      console.error('[artist-corrections] #page-data is missing — the template must emit it.');
      return {};
    }
    try {
      return JSON.parse(el.textContent) || {};
    } catch (error) {
      console.error('[artist-corrections] #page-data is not valid JSON:', error.message);
      return {};
    }
  })();

  const artistName = pageData.artistName || '';
  const mbAlbums = Array.isArray(pageData.mbAlbums) ? pageData.mbAlbums : [];

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function reloadSoon(delay) {
    setTimeout(() => global.location.reload(), delay || 600);
  }

  // ── Registries ──────────────────────────────────────────────────────────
  //
  // Payloads are held here and referenced by a generated key, so nothing has to
  // be serialised into an attribute. The original used this approach for the
  // tables and inline JS for the two merge buttons; now it is uniform.

  const albumTracks = Object.create(null);
  const pendingMatchTracks = Object.create(null);
  const pendingQueueTracks = Object.create(null);
  const pendingRenameTracks = Object.create(null);
  const pendingTitleQueueTracks = Object.create(null);
  const pendingCandidateIds = Object.create(null);
  const pendingArtistMerges = Object.create(null);

  let currentMbTrack = null;

  // ── Duplicate artists (same MBID, different names) ──────────────────────

  async function loadDuplicateArtists() {
    try {
      const data = await global.api.getJson(
        `/api/duplicate-artists/${encodeURIComponent(artistName)}`
      );
      const duplicates = data.duplicates || [];
      if (duplicates.length) displayDuplicateArtists(duplicates);
    } catch (error) {
      console.error('[artist-corrections] duplicate artists failed:', error);
    }
  }

  function displayDuplicateArtists(duplicates) {
    const section = document.getElementById('duplicate-artists-section');
    const tbody = document.getElementById('duplicate-artists-tbody');
    const badge = document.getElementById('duplicate-artists-badge');
    if (!section || !tbody) return;

    if (!duplicates.length) {
      section.style.display = 'none';
      return;
    }

    tbody.innerHTML = '';

    duplicates.forEach((dup, idx) => {
      const trackCounts = dup.track_counts || {};
      const totalTracks = Object.values(trackCounts).reduce((a, b) => a + b, 0);
      const canonical = dup.canonical_mb || '';
      const variations = dup.variations || [];
      // Only the non-canonical spellings need rewriting, so the count of
      // affected tracks excludes rows already using the recommended name.
      const sourceVariations = variations.filter(
        (v) => String(v).toLowerCase() !== String(canonical).toLowerCase()
      );
      const affectedTracks = sourceVariations.reduce(
        (sum, v) => sum + (trackCounts[v] || 0), 0
      );

      const mergeKey = `merge_${idx}`;
      pendingArtistMerges[mergeKey] = {
        mbid: dup.mbid,
        canonicalName: canonical,
        sourceVariations,
      };

      const variationsHtml = variations.map((v) =>
        `<span class="badge bg-secondary me-1 mb-1">${esc(v)} ` +
        `<span class="opacity-50 ms-1">(${esc(String(trackCounts[v] || 0))})</span></span>`
      ).join('');

      const row = document.createElement('tr');
      row.innerHTML = `
        <td class="small font-monospace text-muted" data-label="MBID">${esc(String(dup.mbid || '').substring(0, 8))}...</td>
        <td data-label="Current Variations"><div class="d-flex flex-wrap">${variationsHtml}</div></td>
        <td data-label="Recommended Name"><strong class="text-success">${esc(canonical)}</strong></td>
        <td class="text-center" data-label="Total Tracks"><span class="badge bg-info text-dark">${totalTracks}</span></td>
        <td class="text-end" data-label="Action">
          <button type="button" class="btn btn-sm btn-primary fw-bold"
                  data-action="ac-merge-artist"
                  data-merge-key="${esc(mergeKey)}"
                  data-total="${totalTracks}"
                  data-affected="${affectedTracks}">
            <i class="bi bi-union me-1"></i> Merge
          </button>
        </td>`;
      tbody.appendChild(row);
    });

    section.style.display = 'block';
    if (badge) {
      badge.textContent = `${duplicates.length} duplicate artist${duplicates.length !== 1 ? 's' : ''}`;
      badge.style.display = 'inline-block';
    }
  }

  function showMergeDialog(mergeKey, totalTracks, affectedTracks) {
    const entry = pendingArtistMerges[mergeKey];
    if (!entry) {
      global.toast.error('Could not open the merge dialog — please reload and try again.');
      return;
    }

    const oldLabel = entry.sourceVariations.length
      ? entry.sourceVariations.join(', ')
      : 'No non-canonical variants found';

    const set = (id, text) => {
      const el = document.getElementById(id);
      if (el) el.textContent = text;
    };
    set('mergeOldArtist', oldLabel);
    set('mergeNewArtist', entry.canonicalName);
    set('mergeTotalTracks', String(affectedTracks != null ? affectedTracks : totalTracks));

    const btn = document.getElementById('confirmMergeBtn');
    if (btn) {
      btn.dataset.mergeKey = mergeKey;
      // Kept for backwards compatibility with any other reader.
      btn.dataset.mbid = entry.mbid || '';
      btn.dataset.newArtist = entry.canonicalName;
    }

    const modalEl = document.getElementById('mergeModal');
    if (modalEl) global.modal.show(modalEl);
  }

  async function confirmArtistMerge(mergeKey) {
    const entry = pendingArtistMerges[mergeKey];
    if (!entry) {
      global.toast.error('Merge details are missing — please reload and try again.');
      return;
    }

    const modalEl = document.getElementById('mergeModal');
    if (modalEl) global.modal.hide(modalEl);

    const progressEl = document.getElementById('mergeProgressModal');
    if (progressEl) global.modal.show(progressEl);

    try {
      const result = await global.api.postJson('/api/duplicate-artists/merge', {
        new_artist: entry.canonicalName,
        source_artists: entry.sourceVariations,
        mbid: entry.mbid,
        dry_run: false,
      });

      if (progressEl) global.modal.hide(progressEl);

      if (result.success) {
        global.toast.success(
          `Database: ${result.updated_db} · MP3 tags: ${result.updated_files} · Files moved: ${result.moved_files}`,
          'Merge complete'
        );
        reloadSoon(1200);
      } else {
        const errors = Array.isArray(result.errors) ? result.errors.join('; ') : '';
        global.toast.error(`Merge failed: ${result.error || 'unknown error'}${errors ? ' — ' + errors : ''}`);
      }
    } catch (error) {
      if (progressEl) global.modal.hide(progressEl);
      global.toast.error('Merge failed: ' + error.message);
    }
  }

  // ── Title mismatches vs MusicBrainz ─────────────────────────────────────

  async function checkAllAlbumsTitleMismatches() {
    const results = await Promise.allSettled(
      mbAlbums.map((alb) => checkAlbumTitleMismatches(alb))
    );
    let total = 0;
    results.forEach((r) => { if (r.status === 'fulfilled') total += (r.value || 0); });

    const loadingRow = document.getElementById('title-mismatches-loading-row');
    if (loadingRow) loadingRow.remove();

    const badge = document.getElementById('title-mismatches-badge');
    if (badge) {
      if (total > 0) {
        badge.textContent = `${total} title mismatch${total !== 1 ? 'es' : ''}`;
        badge.className = 'badge bg-warning text-dark shadow-sm py-2';
      } else {
        badge.textContent = 'No title mismatches';
        badge.className = 'badge bg-success shadow-sm py-2';
      }
      badge.style.display = 'inline-block';
    }
  }

  async function checkAlbumTitleMismatches(alb) {
    const tbody = document.getElementById('title-mismatches-tbody');
    if (!tbody || !alb || !alb.album) return 0;

    try {
      const data = await global.api.getJson(
        `/api/album/title-mismatches?artist=${encodeURIComponent(artistName)}` +
        `&album=${encodeURIComponent(alb.album)}`
      );
      const mismatches = data.mismatches || [];

      mismatches.forEach((m, idx) => {
        const renameKey = `rename_${encodeURIComponent(alb.album)}_${idx}`;
        const queueKey = `titlequeue_${encodeURIComponent(alb.album)}_${idx}`;
        pendingRenameTracks[renameKey] = m;
        pendingTitleQueueTracks[queueKey] = m;

        // A length difference means the local file is a different edit, so
        // renaming alone would be wrong — hence the extra Queue button.
        const isLengthMismatch = m.mismatch_type === 'title_and_length';
        const lengthClass = isLengthMismatch ? 'text-warning fw-bold' : 'text-muted';

        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td class="fw-semibold" data-label="Album">${esc(alb.album)}</td>
          <td class="text-center" data-label="Track #">${esc(m.track_number || '—')}</td>
          <td data-label="Library Title">${esc(m.library_title || '—')}</td>
          <td class="text-success fw-bold" data-label="MB Title">${esc(m.mb_title || '—')}</td>
          <td class="text-center small ${lengthClass}" data-label="Local Len">${esc(m.library_duration || '—')}</td>
          <td class="text-center small ${lengthClass}" data-label="MB Len">${esc(m.mb_duration || '—')}</td>
          <td class="text-end" data-label="Action">
            <button type="button" class="btn btn-sm btn-outline-info me-1 fw-bold"
                    data-action="ac-title-rename" data-rename-key="${esc(renameKey)}"
                    title="Rename to the MusicBrainz title">
              <i class="bi bi-pencil-square"></i> Rename
            </button>
            ${isLengthMismatch ? `
            <button type="button" class="btn btn-sm btn-outline-success fw-bold"
                    data-action="ac-title-queue" data-queue-key="${esc(queueKey)}"
                    title="Add to the download queue to replace with the correct version">
              <i class="bi bi-download"></i> Queue
            </button>` : ''}
          </td>`;
        tbody.appendChild(tr);
      });

      return mismatches.length;
    } catch (error) {
      console.error(`[artist-corrections] title mismatch check failed for ${alb.album}:`, error);
      return 0;
    }
  }

  async function renameTitleMismatch(renameKey, button) {
    const m = pendingRenameTracks[renameKey];
    if (!m) {
      global.toast.error('Could not find the track data — please reload.');
      return;
    }

    const accepted = await global.ui.confirm({
      title: 'Rename track',
      message: 'Rename this track to the MusicBrainz title?',
      detail: `Current: "${m.library_title}"\nNew: "${m.mb_title}"`,
      tone: 'primary',
      confirmLabel: 'Rename',
    });
    if (!accepted) return;

    return global.buttonState.withBusy(button, '', async () => {
      try {
        const result = await global.api.postJson('/api/track/match-missing', {
          track_id: String(m.track_id),
          mb_title: m.mb_title,
          mb_track_number: m.track_number,
          mb_release_id: m.release_id,
        });
        if (result.success) {
          global.toast.success(
            `"${result.old_title}" → "${result.new_title}"` +
            (result.updated_file ? ' (audio tags updated)' : '')
          );
          reloadSoon(1000);
        } else {
          global.toast.error(result.error || 'Rename failed');
        }
      } catch (error) {
        global.toast.error('Rename failed: ' + error.message);
      }
    });
  }

  async function queueTitleMismatch(queueKey, button) {
    const m = pendingTitleQueueTracks[queueKey];
    if (!m) {
      global.toast.error('Could not find the track data — please reload.');
      return;
    }

    return global.buttonState.withBusy(button, '', async () => {
      try {
        const result = await global.api.postJson('/api/queue/add', {
          artist: m.album_artist || m.artist,
          album: m.album,
          title: m.mb_title,
          track_number: m.track_number,
          year: m.year,
          release_id: m.release_id,
        });
        if (result.success || result.id) {
          global.toast.success(`"${m.mb_title}" added to the download queue.`);
        } else {
          global.toast.error('Could not queue: ' + (result.error || 'unknown error'));
        }
      } catch (error) {
        global.toast.error('Could not queue: ' + error.message);
      }
    });
  }

  // ── Missing MusicBrainz tracks ──────────────────────────────────────────

  async function checkAllAlbumsMbTracks(button) {
    const run = async () => {
      const results = await Promise.allSettled(
        mbAlbums.map((alb, idx) => checkAlbumMbTracks(alb, idx + 1))
      );
      let total = 0;
      results.forEach((r) => { if (r.status === 'fulfilled') total += (r.value || 0); });

      const badge = document.getElementById('missing-tracks-badge');
      if (badge) {
        if (total > 0) {
          badge.textContent = `${total} missing MB track${total !== 1 ? 's' : ''}`;
          badge.className = 'badge bg-warning text-dark shadow-sm py-2';
        } else {
          badge.textContent = 'No missing MB tracks';
          badge.className = 'badge bg-success shadow-sm py-2';
        }
        badge.style.display = 'inline-block';
      }
    };

    // withBusy restores the button in a `finally`, so an exception mid-check
    // cannot leave "Checking…" pinned on screen.
    return global.buttonState.withBusy(button, 'Checking…', run);
  }

  async function checkAlbumMbTracks(alb, rowIdx) {
    const spinner = document.getElementById(`mb-spinner-${rowIdx}`);
    const badgeEl = document.getElementById(`mb-missing-badge-${rowIdx}`);
    const totalEl = document.getElementById(`mb-total-${rowIdx}`);
    const detailEl = document.getElementById(`mb-missing-detail-${rowIdx}`);

    if (spinner) spinner.style.display = 'inline-block';
    if (badgeEl) badgeEl.textContent = '';

    try {
      const data = await global.api.getJson(
        `/api/album/missing-tracks?artist=${encodeURIComponent(artistName)}` +
        `&album=${encodeURIComponent(alb.album)}`
      );
      if (spinner) spinner.style.display = 'none';

      const mbTotal = data.mb_total || 0;
      const missingCount = data.missing_count || 0;
      if (totalEl) totalEl.textContent = mbTotal || '—';

      if (missingCount > 0) {
        if (badgeEl) {
          badgeEl.innerHTML = `<span class="badge bg-warning text-dark">${missingCount} missing</span>`;
        }
        if (detailEl) {
          const missingTracks = data.missing_tracks || [];
          // Warms the cache the Match modal reads — awaited deliberately.
          await fetchAlbumLibraryTracks(alb.album);

          const rows = missingTracks.map((t, tIdx) => {
            const matchKey = `match_${rowIdx}_${tIdx}`;
            const queueKey = `queue_${rowIdx}_${tIdx}`;
            pendingMatchTracks[matchKey] = { mbTrack: t, albumName: alb.album };
            pendingQueueTracks[queueKey] = t;

            let durationStr = '';
            if (t.duration) {
              const mins = Math.floor(t.duration / 60);
              const secs = t.duration % 60;
              durationStr = `<span class="badge bg-dark border border-secondary text-muted ms-2">` +
                `<i class="bi bi-clock me-1"></i>${mins}:${String(secs).padStart(2, '0')}</span>`;
            }

            return `
              <div class="list-group-item bg-transparent px-2 py-2 border-secondary d-flex flex-wrap align-items-center justify-content-between gap-2">
                <div class="min-w-0">
                  <span class="badge bg-secondary me-2"># ${esc(String(t.track_number || '?'))}</span>
                  <span class="fw-semibold">${esc(t.title)}</span>${durationStr}
                </div>
                <div class="d-flex gap-1 flex-shrink-0">
                  <button type="button" class="btn btn-sm btn-outline-success py-0 px-2"
                          data-action="ac-missing-queue" data-queue-key="${esc(queueKey)}"
                          title="Add to the download queue">
                    <i class="bi bi-download"></i> Queue
                  </button>
                  <button type="button" class="btn btn-sm btn-outline-info py-0 px-2"
                          data-action="ac-missing-match" data-match-key="${esc(matchKey)}"
                          title="Match to an existing song in the library">
                    <i class="bi bi-link-45deg"></i> Match
                  </button>
                </div>
              </div>`;
          }).join('');

          detailEl.innerHTML =
            `<div class="list-group list-group-flush border border-secondary rounded mt-2 mb-2">${rows}</div>`;
        }
      } else if (data.reason === 'no_mbid') {
        if (badgeEl) badgeEl.innerHTML = '<span class="badge bg-secondary">no MBID</span>';
      } else if (data.reason === 'mb_not_found') {
        if (badgeEl) badgeEl.innerHTML = '<span class="badge bg-secondary">no MB data</span>';
      } else if (badgeEl) {
        badgeEl.innerHTML = '<span class="badge bg-success">Complete</span>';
      }

      return missingCount;
    } catch (error) {
      if (spinner) spinner.style.display = 'none';
      if (badgeEl) badgeEl.innerHTML = '<span class="badge bg-danger">Error</span>';
      console.error(`[artist-corrections] MB track check failed for ${alb.album}:`, error);
      return 0;
    }
  }

  async function fetchAlbumLibraryTracks(albumName) {
    if (albumName in albumTracks) return;
    try {
      const data = await global.api.getJson(
        `/api/album/library-tracks?artist=${encodeURIComponent(artistName)}` +
        `&album=${encodeURIComponent(albumName)}`
      );
      albumTracks[albumName] = data.tracks || [];
    } catch (error) {
      console.error(`[artist-corrections] library tracks failed for "${albumName}":`, error);
      albumTracks[albumName] = [];
    }
  }

  // ── Match-to-existing modal ─────────────────────────────────────────────

  function openMatchModal(matchKey) {
    const entry = pendingMatchTracks[matchKey];
    if (!entry) {
      global.toast.error('Could not open the match dialog — please reload and try again.');
      return;
    }

    const { mbTrack, albumName } = entry;
    currentMbTrack = mbTrack;

    const set = (id, text) => {
      const el = document.getElementById(id);
      if (el) el.textContent = text;
    };
    set('matchMbTitle', mbTrack.title || '?');
    set('matchMbTrackNum', String(mbTrack.track_number || '?'));

    const tbody = document.getElementById('matchCandidatesTbody');
    const candidates = albumTracks[albumName] || [];

    if (tbody) {
      if (!candidates.length) {
        tbody.innerHTML =
          '<tr><td colspan="4" class="text-center text-muted py-4">' +
          '<i class="bi bi-info-circle fs-4 d-block mb-2"></i>No tracks found in the library for this album.</td></tr>';
      } else {
        tbody.innerHTML = candidates.map((c, cIdx) => {
          const candKey = `cand_${matchKey}_${cIdx}`;
          pendingCandidateIds[candKey] = c.id;
          return `
            <tr>
              <td class="text-center text-muted" data-label="#">${esc(c.track_number || '—')}</td>
              <td class="fw-semibold text-light" data-label="Current Title">${esc(c.title || '—')}</td>
              <td class="small text-muted font-monospace text-break" data-label="File">${esc(c.file_path || '—')}</td>
              <td class="text-end" data-label="Action">
                <button type="button" class="btn btn-sm btn-info fw-bold"
                        data-action="ac-match-confirm" data-cand-key="${esc(candKey)}">
                  <i class="bi bi-check-lg"></i> Match
                </button>
              </td>
            </tr>`;
        }).join('');
      }
    }

    const modalEl = document.getElementById('matchTrackModal');
    if (modalEl) global.modal.show(modalEl);
  }

  async function confirmMatchTrack(candKey, button) {
    if (!currentMbTrack) return;

    const trackId = pendingCandidateIds[candKey];
    if (!trackId) {
      global.toast.error('Could not identify the selected track — please reload and try again.');
      return;
    }

    const accepted = await global.ui.confirm({
      title: 'Sync MusicBrainz metadata',
      message: 'Sync MusicBrainz metadata to this library track?',
      detail: `New title: "${currentMbTrack.title}"`,
      tone: 'primary',
      confirmLabel: 'Match',
    });
    if (!accepted) return;

    return global.buttonState.withBusy(button, '', async () => {
      try {
        const result = await global.api.postJson('/api/track/match-missing', {
          track_id: String(trackId),
          mb_title: currentMbTrack.title,
          mb_track_number: currentMbTrack.track_number,
          mb_release_id: currentMbTrack.release_id,
        });

        const modalEl = document.getElementById('matchTrackModal');
        if (modalEl) global.modal.hide(modalEl);

        if (result.success) {
          global.toast.success(
            `"${result.old_title}" → "${result.new_title}"` +
            (result.updated_file ? ' (file tags updated)' : '')
          );
          reloadSoon(1000);
        } else {
          global.toast.error(result.error || 'Match failed');
        }
      } catch (error) {
        global.toast.error('Match failed: ' + error.message);
      }
    });
  }

  async function queueMissingTrackFromCorrections(queueKey, button) {
    const track = pendingQueueTracks[queueKey];
    if (!track) {
      global.toast.error('Could not queue the track — please reload and try again.');
      return;
    }

    return global.buttonState.withBusy(button, '', async () => {
      try {
        const result = await global.api.postJson('/api/queue/add', {
          artist: track.album_artist || track.artist,
          album: track.album,
          title: track.title,
          track_number: track.track_number,
          year: track.year,
          release_id: track.release_id,
        });
        if (result.success || result.id) {
          global.toast.success(`"${track.title}" added to the download queue.`);
        } else {
          global.toast.error('Could not queue: ' + (result.error || 'unknown error'));
        }
      } catch (error) {
        global.toast.error('Could not queue: ' + error.message);
      }
    });
  }

  // ── Duplicate track rows ────────────────────────────────────────────────

  async function deleteDuplicateTrack(trackId, isRecommendedDelete, button) {
    const recommendationNote = isRecommendedDelete
      ? 'This is the recommended duplicate to delete.\n\n'
      : '';

    const accepted = await global.ui.confirm({
      title: 'Delete duplicate track',
      message: `${recommendationNote}Delete track ${trackId} from the database?`,
      detail: 'Its file is deleted from disk too, if it exists. This cannot be undone.',
      tone: 'danger',
      confirmLabel: 'Delete',
    });
    if (!accepted) return;

    return global.buttonState.withBusy(button, '', async () => {
      try {
        const result = await global.api.postJson('/api/artist/corrections/delete-track', {
          track_id: String(trackId),
          delete_file: true,
        });
        if (!result.success) throw new Error(result.error || 'Failed to delete track');
        global.toast.success(`Deleted track ${trackId}${result.deleted_file ? ' and its file' : ''}.`);
        reloadSoon(1000);
      } catch (error) {
        global.toast.error(`Could not delete track ${trackId}: ${error.message}`);
      }
    });
  }

  // ── Album MBID conflicts ────────────────────────────────────────────────

  async function applyAlbumMbidCorrection(album, rowIndex, mbidOverride, button) {
    const input = document.getElementById(`mbid-input-${rowIndex}`);
    const mbid = String(mbidOverride || (input ? input.value : '') || '').trim();

    if (!mbid) {
      global.toast.warning('Please enter a MusicBrainz album MBID.');
      return;
    }

    const accepted = await global.ui.confirm({
      title: 'Apply album MBID',
      message: `Apply this MBID to all tracks in "${album}"?`,
      detail: `MBID: ${mbid}\n\nThis updates database values and file tags.`,
      tone: 'primary',
      confirmLabel: 'Apply',
    });
    if (!accepted) return;

    return global.buttonState.withBusy(button, '', async () => {
      try {
        const result = await global.api.postJson('/api/artist/corrections/apply-album-mbid', {
          artist: artistName,
          album,
          mbid,
        });
        if (!result.success) throw new Error(result.error || 'Failed to apply MBID');
        global.toast.success(
          `Applied MBID to ${result.rows_updated || 0} track(s) in "${album}" ` +
          `(${result.files_updated || 0} files updated).`
        );
        reloadSoon(1000);
      } catch (error) {
        global.toast.error('Could not apply MBID: ' + error.message);
      }
    });
  }

  /**
   * Search MusicBrainz and let the user pick a release, then apply it.
   *
   * NOTE: this keeps `window.prompt` for the release choice. The alternative —
   * a proper picker modal — is a UI change rather than a migration, and the
   * page already has enough destructive machinery. The candidate list is capped
   * at 8, matching the original.
   */
  async function searchAndApplyAlbumMbid(album, rowIndex, button) {
    return global.buttonState.withBusy(button, '', async () => {
      try {
        const data = await global.api.getJson(
          `/api/album/musicbrainz?artist=${encodeURIComponent(artistName)}` +
          `&album=${encodeURIComponent(album)}`
        );
        const results = Array.isArray(data.releases) ? data.releases : [];
        if (!results.length) {
          global.toast.info(`No MusicBrainz releases found for ${artistName} — ${album}`);
          return;
        }

        const shown = results.slice(0, 8);
        const options = shown.map((release, idx) => {
          const date = release.date ? ` (${release.date})` : '';
          return `${idx + 1}. ${release.title || album}${date}\n   ${release.id || ''}`;
        }).join('\n\n');

        const choice = global.prompt(`Select the release number to apply:\n\n${options}\n\nEnter number:`);
        if (!choice) return;

        const selectedIdx = parseInt(choice, 10) - 1;
        if (isNaN(selectedIdx) || selectedIdx < 0 || selectedIdx >= shown.length) {
          global.toast.warning('Invalid selection.');
          return;
        }

        const selected = shown[selectedIdx];
        if (!selected || !selected.id) {
          global.toast.warning('The selected release has no MBID.');
          return;
        }

        // withBusy already owns the button, so call the core apply directly
        // rather than nesting another withBusy inside it.
        await applyAlbumMbidNow(album, selected.id);
      } catch (error) {
        global.toast.error('MusicBrainz search failed: ' + error.message);
      }
    });
  }

  /** The apply request without the confirmation/button wrapper. */
  async function applyAlbumMbidNow(album, mbid) {
    try {
      const result = await global.api.postJson('/api/artist/corrections/apply-album-mbid', {
        artist: artistName,
        album,
        mbid,
      });
      if (!result.success) throw new Error(result.error || 'Failed to apply MBID');
      global.toast.success(
        `Applied MBID to ${result.rows_updated || 0} track(s) in "${album}" ` +
        `(${result.files_updated || 0} files updated).`
      );
      reloadSoon(1000);
    } catch (error) {
      global.toast.error('Could not apply MBID: ' + error.message);
    }
  }

  // ── Duplicate album splits ──────────────────────────────────────────────

  async function mergeAlbums(rowIndex, button) {
    const input = document.getElementById(`dup-album-canonical-${rowIndex}`);
    const canonicalName = String(input ? input.value : '').trim();

    if (!canonicalName) {
      global.toast.warning('Please enter the canonical album name to merge into.');
      return;
    }

    // The album list travels as a JSON string in a data attribute, HTML-escaped
    // by Jinja's `|e`, so quotes and angle brackets in album names are inert.
    // The original injected the same JSON straight into an onclick with `|safe`.
    let sourceAlbums = [];
    try {
      sourceAlbums = JSON.parse((button && button.dataset.sourceAlbums) || '[]');
    } catch (error) {
      console.error('[artist-corrections] could not parse the album list:', error);
    }
    if (!Array.isArray(sourceAlbums) || !sourceAlbums.length) {
      global.toast.error('Could not find the album list — please reload and try again.');
      return;
    }

    const accepted = await global.ui.confirm({
      title: 'Merge album splits',
      message: `Merge ${sourceAlbums.length} album name(s) into "${canonicalName}"?`,
      items: sourceAlbums,
      detail: 'This updates the database and rewrites the album tag in each audio file, so the fix survives the next Navidrome rescan.',
      tone: 'danger',
      confirmLabel: 'Merge',
    });
    if (!accepted) return;

    return global.buttonState.withBusy(button, 'Merging…', async () => {
      try {
        const result = await global.api.postJson('/api/artist/corrections/merge-albums', {
          artist: artistName,
          source_albums: sourceAlbums,
          canonical_name: canonicalName,
        });
        if (!result.success) throw new Error(result.error || 'Failed to merge albums');
        global.toast.success(result.message || 'Albums merged.');
        reloadSoon(1200);
      } catch (error) {
        global.toast.error('Could not merge albums: ' + error.message);
      }
    });
  }

  // ── Disc number inconsistencies ─────────────────────────────────────────

  async function clearDiscNumber(album, button) {
    const accepted = await global.ui.confirm({
      title: 'Clear disc number',
      message: `Remove the disc number from tracks in "${album}" that have one?`,
      detail: 'Use this for single-disc albums where some tracks carry a disc number and others do not. Updates database values and file tags.',
      tone: 'warning',
      confirmLabel: 'Clear',
    });
    if (!accepted) return;

    return global.buttonState.withBusy(button, 'Clearing…', async () => {
      try {
        const result = await global.api.postJson('/api/artist/corrections/clear-disc-number', {
          artist: artistName,
          album,
        });
        if (!result.success) throw new Error(result.error || 'Failed to clear disc number');
        global.toast.success(
          `Disc number cleared from ${result.cleared || 0} track(s) in "${album}" ` +
          `(${result.files_updated || 0} file(s) updated).`
        );
        reloadSoon(1200);
      } catch (error) {
        // The backend refuses this for genuine multi-disc releases.
        if (String(error.message || '').includes('multi-disc')) {
          global.toast.warning(
            `"${album}" looks like a multi-disc release — review its track disc numbers manually.`
          );
          return;
        }
        global.toast.error(`Could not clear the disc number for "${album}": ${error.message}`);
      }
    });
  }

  // ── Wiring ──────────────────────────────────────────────────────────────
  //
  // One delegated listener. The merge/album/mbid payloads come from registries
  // or data-* attributes, never from serialised JavaScript in an attribute.

  const ACTIONS = {
    'ac-merge-artist': (el) =>
      showMergeDialog(el.dataset.mergeKey, el.dataset.total, el.dataset.affected),
    'ac-merge-artist-confirm': (el) => confirmArtistMerge(el.dataset.mergeKey),
    'ac-title-rename': (el) => renameTitleMismatch(el.dataset.renameKey, el),
    'ac-title-queue': (el) => queueTitleMismatch(el.dataset.queueKey, el),
    'ac-missing-queue': (el) => queueMissingTrackFromCorrections(el.dataset.queueKey, el),
    'ac-missing-match': (el) => openMatchModal(el.dataset.matchKey),
    'ac-match-confirm': (el) => confirmMatchTrack(el.dataset.candKey, el),
    'ac-delete-track': (el) =>
      deleteDuplicateTrack(el.dataset.trackId, el.dataset.recommended === 'true', el),
    'ac-apply-mbid': (el) =>
      applyAlbumMbidCorrection(el.dataset.album, el.dataset.rowIndex, null, el),
    'ac-find-mbid': (el) =>
      searchAndApplyAlbumMbid(el.dataset.album, el.dataset.rowIndex, el),
    'ac-merge-albums': (el) => mergeAlbums(el.dataset.rowIndex, el),
    'ac-clear-disc': (el) => clearDiscNumber(el.dataset.album, el),
    'ac-check-tracks': (el) => checkAllAlbumsMbTracks(el),
  };

  document.addEventListener('click', function (event) {
    if (!event.target.closest) return;
    const el = event.target.closest('[data-action]');
    if (!el) return;

    const handler = ACTIONS[el.getAttribute('data-action')];
    if (!handler) return;
    event.preventDefault();
    handler(el);
  });

  document.addEventListener('DOMContentLoaded', function () {
    loadDuplicateArtists();
    // The two async checks only make sense when the artist has MB-linked albums.
    if (mbAlbums.length) {
      checkAllAlbumsTitleMismatches();
      checkAllAlbumsMbTracks(document.getElementById('checkAllMbTracksBtn'));
    }
  });

  global.artistCorrections = {
    loadDuplicateArtists,
    displayDuplicateArtists,
    showMergeDialog,
    confirmArtistMerge,
    checkAllAlbumsTitleMismatches,
    checkAlbumTitleMismatches,
    renameTitleMismatch,
    queueTitleMismatch,
    checkAllAlbumsMbTracks,
    checkAlbumMbTracks,
    openMatchModal,
    confirmMatchTrack,
    queueMissingTrackFromCorrections,
    deleteDuplicateTrack,
    applyAlbumMbidCorrection,
    searchAndApplyAlbumMbid,
    mergeAlbums,
    clearDiscNumber,
  };
})(window);
