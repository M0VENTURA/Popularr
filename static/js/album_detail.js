// ===== Album Detail Page JS =====
//
// The MusicBrainz compare / apply-field / ignore-field / bulk-delete calls
// below hit the api_v1 blueprint (see api_v1/albums.py, api_v1/tracks.py),
// which is assumed to be registered under this prefix. VERIFY against
// __init__.py's register_blueprint(..., url_prefix=...) call and update if
// different — every other endpoint on this page (favourite, recommend-
// genres, musicbrainz search, queue/add) uses unversioned "/api/..." paths
// from a separate, older blueprint, so this page intentionally mixes both.
const _API_V1_PREFIX = '/api/v1';
//
// SCOPE: this file contains ONLY album-detail behaviour.
//
// It was previously 2,917 lines, of which ~200 were album code. The rest was
// copy-pasted wholesale from two other modules:
//
//   * artist_detail.js  (~370 lines) — setArtistFilter, editArtistCountry,
//     saveArtistIds, lookupAndSaveArtistIds, toggleArtistBio, ...
//     None of it can run here: every function reads `window._pd`, which only
//     the ARTIST page defines. The album page defines `window._pageData`.
//
//   * downloads.js (~2,349 lines) — performMbSearch, downloadMbRelease, the
//     entire queue manager, Soulseek search, the organize-group modal, ...
//     Duplicating it meant two copies of `searchMBForOrganize`,
//     `window.doLookup`, `window.escapeHtml` and `document.addEventListener`
//     registrations fighting each other at runtime.
//
// Both are deleted here. Anything this page needs from them is loaded from
// its real home — see the <script> tags in album_detail.html (downloads.js
// is now loaded there too, for performMbSearch / handleGlobalMbSelect /
// confirmReleaseSelection, which the MusicBrainz lookup modal depends on).

// ---------------------------------------------------------------------------
// Album favourite
// ---------------------------------------------------------------------------
// NOTE: main.js also defines `window.toggleAlbumFavourite`, backed by
// /api/bookmarks. This one is backed by /api/album/favourite and wins on the
// album page because album_detail.js loads after main.js. The two endpoints
// are not interchangeable — the heart on the album page and the heart
// elsewhere write to different tables. Pick one server-side and delete the
// other; until then this override is deliberate, not accidental.
window.toggleAlbumFavourite = function (artist, album) {
    fetch('/api/album/favourite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ artist: artist, album: album })
    })
        .then(r => r.json())
        .then(data => {
            const icon = document.getElementById('albumFavouriteIcon');
            if (!icon || !data.success) return;
            icon.classList.toggle('bi-heart-fill', !!data.is_favourite);
            icon.classList.toggle('bi-heart', !data.is_favourite);
        })
        .catch(err => console.error('Error toggling album favourite:', err));
};

// ---------------------------------------------------------------------------
// Playback
// ---------------------------------------------------------------------------
window.playAlbum = function () {
    const playBtns = document.querySelectorAll('.player-play-btn');
    if (playBtns.length === 0) {
        alert('No playable tracks found for this album.');
        return;
    }
    playBtns[0].click();
};

window.playTrackFromAlbum = function (btn) {
    const trackId = btn.getAttribute('data-track-id');
    const title = btn.getAttribute('data-title');
    const artist = btn.getAttribute('data-artist');
    const art = btn.getAttribute('data-art');

    if (typeof Player !== 'undefined' && typeof Player.playTrack === 'function') {
        Player.playTrack({ id: trackId, title: title, artist: artist, albumArtUrl: art });
    } else {
        alert(`Playing track: ${title}`);
    }
};

// ---------------------------------------------------------------------------
// Track row actions
// ---------------------------------------------------------------------------
window.openEditTrackFromAlbum = function (trackId) {
    const modalEl = document.getElementById('trackEditModal');
    if (modalEl && typeof bootstrap !== 'undefined') {
        bootstrap.Modal.getOrCreateInstance(modalEl).show();
    } else {
        // The route is /track/<id> — NOT /track/<id>/edit. The latter has never
        // been registered (same class of bug as the delete button below, which
        // navigated to /track/<id>/delete), so this fallback 404'd whenever the
        // edit modal was absent from the page.
        window.location.href = `/track/${encodeURIComponent(trackId)}`;
    }
};

// Delete one track. This used to be
//     window.location.href = `/track/${trackId}/delete`
// which 404'd on every click: no route with that URL shape has ever existed
// (the deletes are POST /api/... endpoints). It also made a destructive
// change reachable by a GET, so a prefetch or a stray link could have fired
// it. Now it POSTs to the api_v1 track-delete route and reloads.
window.deleteTrack = function (trackId) {
    if (!confirm(`Delete this track?\n\nIt is removed from the database, and its file is deleted from disk too, if it exists.\n\nThis cannot be undone.`)) return;

    fetch(`${_API_V1_PREFIX}/tracks/${encodeURIComponent(trackId)}/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delete_file: true })
    })
        .then(r => r.json())
        .then(data => {
            if (!data.success) {
                alert('❌ Error: ' + (data.error || 'Failed to delete track'));
                return;
            }
            alert(`✅ Deleted track${data.deleted_file ? ' and its file' : ' (no file on disk)'}`);
            window.location.reload();
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        });
};

// ---------------------------------------------------------------------------
// Edit-metadata helpers
// ---------------------------------------------------------------------------
window.populateMajorityArtist = function () {
    const rows = document.querySelectorAll('#albumTracksTbody tr');
    const artists = [];
    rows.forEach(r => {
        const artist = r.getAttribute('data-track-artist');
        if (artist) artists.push(artist);
    });
    if (artists.length === 0) return;

    const counts = artists.reduce((acc, curr) => (acc[curr] = (acc[curr] || 0) + 1, acc), {});
    const majority = Object.keys(counts).reduce((a, b) => (counts[a] > counts[b] ? a : b));
    const input = document.getElementById('track_artist');
    if (input) input.value = majority;
};

// ---------------------------------------------------------------------------
// Album genres (staged — written to all tracks on save)
// ---------------------------------------------------------------------------
function updateHiddenGenres() {
    const container = document.getElementById('albumGenresContainer');
    const hiddenInput = document.getElementById('album_genres');
    if (!container || !hiddenInput) return;
    const genres = Array.from(container.querySelectorAll('.badge'))
        .map(b => b.textContent.replace('×', '').trim())
        .filter(Boolean);
    hiddenInput.value = genres.join(', ');

    // The sticky save bar only appears on `input`/`change`; genre chips are
    // added by JS, which fires neither. Without this the bar stays hidden and
    // the staged genres look saved when they are not.
    if (typeof window.markFormDirty === 'function') {
        window.markFormDirty('albumMetadataForm');
    }
}

window.addAlbumGenre = function () {
    const input = document.getElementById('newAlbumGenreInput');
    const container = document.getElementById('albumGenresContainer');
    if (!input || !container) return;

    const genre = input.value.trim();
    if (!genre) return;

    // Don't stage the same genre twice.
    const existing = Array.from(container.querySelectorAll('.badge'))
        .map(b => b.textContent.replace('×', '').trim().toLowerCase());
    if (existing.includes(genre.toLowerCase())) {
        input.value = '';
        return;
    }

    // Placeholder text ("No genres set") is not a badge — clear it on first add.
    const placeholder = container.querySelector('.text-muted');
    if (placeholder && !placeholder.classList.contains('badge')) placeholder.remove();

    const badge = document.createElement('span');
    badge.className = 'badge bg-primary me-1 mb-1';

    // textContent, not innerHTML — a genre name is user input and must never
    // be parsed as markup.
    badge.textContent = genre;

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close btn-close-white ms-1';
    close.style.fontSize = '0.6rem';
    close.addEventListener('click', function () { window.stageRemoveAlbumGenre(this); });
    badge.appendChild(close);

    container.appendChild(badge);
    input.value = '';
    updateHiddenGenres();
};

window.stageRemoveAlbumGenre = function (element) {
    if (typeof element === 'string') {
        // Called from the Jinja-rendered chips, which pass the genre name.
        const target = element.trim().toLowerCase();
        document.querySelectorAll('#albumGenresContainer .badge').forEach(b => {
            if (b.textContent.replace('×', '').trim().toLowerCase() === target) b.remove();
        });
    } else if (element && element.closest) {
        const badge = element.closest('.badge');
        if (badge) badge.remove();
    }
    updateHiddenGenres();
};

window.goToAlbumGenres = function () {
    const btn = document.querySelector('#albumPageTabs [data-bs-target="#tab-genres"]');
    if (btn && window.bootstrap) bootstrap.Tab.getOrCreateInstance(btn).show();
};

// ---------------------------------------------------------------------------
// Online genre recommendations
// ---------------------------------------------------------------------------
window.fetchGenreRecommendations = function () {
    const btn = document.getElementById('fetchGenresBtn');
    const section = document.getElementById('recommendedGenresSection');
    const container = document.getElementById('recommendedGenres');
    if (!btn || !section || !container) return;

    const restore = '<i class="bi bi-cloud-download me-1"></i> Get Online Suggestions';
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Fetching...';

    const artist = window._pageData ? window._pageData.artistName : '';
    const album = window._pageData ? window._pageData.albumName : '';

    fetch(`/api/album/recommend-genres?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`)
        .then(r => r.json())
        .then(data => {
            btn.disabled = false;
            btn.innerHTML = restore;

            if (!data.success || !data.genres || !data.genres.length) {
                alert('No recommendations found.');
                return;
            }

            section.style.display = 'block';
            container.innerHTML = '';

            // Built with createElement rather than an innerHTML template:
            // the previous version interpolated the genre straight into an
            // onclick="addRecommendedGenre('...')" attribute, so a genre
            // containing an apostrophe (e.g. "rock 'n' roll") broke the
            // handler, and a hostile tag name was executable.
            data.genres.forEach(g => {
                const chip = document.createElement('span');
                chip.className = 'badge bg-secondary';
                chip.style.cursor = 'pointer';
                chip.textContent = g + ' +';
                chip.addEventListener('click', () => window.addRecommendedGenre(g));
                container.appendChild(chip);
                container.appendChild(document.createTextNode(' '));
            });
        })
        .catch(err => {
            btn.disabled = false;
            btn.innerHTML = restore;
            alert('Network error: ' + err.message);
        });
};

window.addRecommendedGenre = function (genre) {
    const input = document.getElementById('newAlbumGenreInput');
    if (!input) return;
    input.value = genre;
    window.addAlbumGenre();
};

window.applySelectedAlbumSourceTags = function () {
    const checks = document.querySelectorAll('.album-source-tag-check:checked');
    const input = document.getElementById('newAlbumGenreInput');
    if (!input) return;

    checks.forEach(c => {
        input.value = c.value;
        window.addAlbumGenre();
        c.checked = false;
    });

    // Re-hide every "Add Selected" button now that nothing is checked; the
    // per-pane change handler below only fires on user interaction.
    document.querySelectorAll('.album-apply-source-tags-btn').forEach(b => {
        b.style.display = 'none';
    });
};

document.addEventListener('change', function (e) {
    if (!e.target.classList || !e.target.classList.contains('album-source-tag-check')) return;
    const pane = e.target.closest('.tab-pane');
    if (!pane) return;
    const anyChecked = pane.querySelectorAll('.album-source-tag-check:checked').length > 0;
    const applyBtn = pane.querySelector('.album-apply-source-tags-btn');
    if (applyBtn) applyBtn.style.display = anyChecked ? 'inline-block' : 'none';
});

// ---------------------------------------------------------------------------
// MusicBrainz lookup
// ---------------------------------------------------------------------------
// Opens the SHARED modal from base.html. This used to open a page-local
// `#albumLookupModal` that included the search component a second time —
// producing duplicate #mbSearchArtist / #mbSearchResults ids, so
// `performMbSearch()` (which resolves them with getElementById, i.e. the
// FIRST match) wrote its results into base.html's hidden global modal while
// the visible one sat on its placeholder.
window.openAlbumLookupModal = function () {
    const artist = window._pageData ? window._pageData.artistName : '';
    const album = window._pageData ? window._pageData.albumName : '';

    if (typeof window.openGlobalMbSearch !== 'function') {
        console.error('openGlobalMbSearch is unavailable — is main.js loaded?');
        alert('MusicBrainz search is unavailable on this page.');
        return;
    }

    window.openGlobalMbSearch(artist, album, function (selected) {
        if (selected && selected.id) window.applyAlbumMbid(selected.id);
    });
};

// Applies a chosen release MBID to the Edit Album form.
//
// Replaces the old `confirmReleaseSelection()`, which read
// `#mbSelectedReleaseId` — an element that exists on no page in this app —
// then fell back to `window._selectedMbReleaseId`, which nothing ever set.
// It therefore always alerted "No release selected or MBID not found".
// The value now arrives directly from the shared modal's selection callback
// (see downloads.js's handleGlobalMbSelect / confirmReleaseSelection).
window.applyAlbumMbid = function (mbid) {
    if (!mbid) {
        alert('No release selected.');
        return;
    }

    const formMbid = document.getElementById('album_mbid');
    if (!formMbid) return;

    formMbid.value = mbid;
    formMbid.style.transition = 'background-color 0.3s';
    formMbid.style.backgroundColor = '#198754';
    setTimeout(() => { formMbid.style.backgroundColor = ''; }, 500);

    if (typeof window.markFormDirty === 'function') {
        window.markFormDirty('albumMetadataForm');
    }

    // Surface the Edit Album tab so the change is visible before saving.
    const tabBtn = document.querySelector('#albumPageTabs [data-bs-target="#tab-details"]');
    if (tabBtn && window.bootstrap) bootstrap.Tab.getOrCreateInstance(tabBtn).show();

    alert('MusicBrainz ID applied! Click "Save Metadata" to persist changes.');
};

// ---------------------------------------------------------------------------
// Compare with MusicBrainz — inline per-track/per-field review
// ---------------------------------------------------------------------------
// Restores the old album page's comparison UI (replacing the earlier blunt
// "Link"/"Align" buttons, which wrote changes with no chance to review them
// first). Requires a MusicBrainz release already linked to this album (the
// "MusicBrainz Release Group ID" / "MusicBrainz Release ID" fields on the
// Edit Album tab — populate these via "Lookup on MusicBrainz" then Save
// Metadata first).
//
// Backed by the server's compare_musicbrainz_release(), which matches MB
// tracklist entries to library rows (by disc+track number, falling back to
// fuzzy title match) and reports per-track diff_fields (title, track_number,
// disc_number, mbid — duration is reported but informational only, since a
// file's actual duration isn't something metadata edits can change).
//
// Field names below (mb_recording_mbid, mb_title, mb_track_number, ...)
// match the server's response shape exactly — see musicbrainz_service.py.
//
// escapeHtml/escapeJsString are defined in downloads.js, loaded before this
// file as a plain <script> (not a module), so they're already global here —
// no need to redefine them.
function _getLinkedReleaseMbid() {
    const releaseGroupField = document.getElementById('album_release_group_mbid');
    const releaseField = document.getElementById('album_mbid');
    return (
        (releaseGroupField && releaseGroupField.value.trim()) ||
        (releaseField && releaseField.value.trim()) ||
        ''
    );
}

window._mbComparisonData = null;

window.compareWithMusicBrainz = function () {
    const artist = window._pageData ? window._pageData.artistName : '';
    const album = window._pageData ? window._pageData.albumName : '';
    const releaseMbid = _getLinkedReleaseMbid();

    if (!releaseMbid) {
        alert('No MusicBrainz release linked yet. Use "Lookup on MusicBrainz" first, then Save Metadata.');
        return;
    }

    const btn = document.getElementById('albumCompareMbBtn');
    const restore = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Comparing...';
    }
    _showMBCompareBanner(
        '<span class="spinner-border spinner-border-sm me-2" role="status"></span> Comparing tracks with MusicBrainz…',
        'info',
        false
    );

    // Path-based, matching api_v1's /albums/<path:artist>/<path:album>/...
    // convention (mirrors artists.py's <path:name>). If your api_v1
    // blueprint is registered under a prefix other than /api/v1, update
    // _API_V1_PREFIX below to match.
    fetch(`${_API_V1_PREFIX}/albums/${encodeURIComponent(artist)}/${encodeURIComponent(album)}/musicbrainz-compare`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ release_mbid: releaseMbid })
    })
        .then(r => r.json())
        .then(data => {
            if (btn) { btn.disabled = false; btn.innerHTML = restore; }
            if (!data.success) {
                _showMBCompareBanner(
                    '<i class="bi bi-exclamation-triangle-fill me-2"></i>Could not compare tracks: ' + escapeHtml(data.error || 'Unknown error'),
                    'warning',
                    true
                );
                return;
            }
            window._mbComparisonData = data;
            _displayMBComparison(data);
        })
        .catch(err => {
            if (btn) { btn.disabled = false; btn.innerHTML = restore; }
            _showMBCompareBanner(
                '<i class="bi bi-x-circle-fill me-2"></i>Network error while comparing tracks: ' + escapeHtml(err.message),
                'danger',
                true
            );
        });
};

function _getMBCompareBannerContainer() {
    const tableResponsive = document.querySelector('#album-tracks-section .table-responsive');
    return tableResponsive ? tableResponsive.parentElement : null;
}

function _showMBCompareBanner(htmlContent, type, showDismiss) {
    let banner = document.getElementById('mb-compare-banner');
    if (!banner) {
        const container = _getMBCompareBannerContainer();
        if (!container) return;
        banner = document.createElement('div');
        banner.id = 'mb-compare-banner';
        const tableResponsive = document.querySelector('#album-tracks-section .table-responsive');
        container.insertBefore(banner, tableResponsive);
    }
    const dismissBtn = showDismiss
        ? `<button type="button" class="btn-close ms-auto" onclick="clearMBComparison()" aria-label="Dismiss"></button>`
        : '';
    banner.innerHTML = `<div class="alert alert-${escapeHtml(type)} d-flex align-items-center mb-2 py-2">${htmlContent}${dismissBtn}</div>`;
}

window.clearMBComparison = function () {
    document.querySelectorAll('.mb-update-row').forEach(el => el.remove());
    document.querySelectorAll('.mb-missing-row').forEach(el => el.remove());
    document.querySelectorAll('.mb-extra-row').forEach(el => el.remove());
    document.querySelectorAll('.mb-extra-badge-dynamic').forEach(el => el.remove());
    const banner = document.getElementById('mb-compare-banner');
    if (banner) banner.remove();
    window._mbComparisonData = null;
};

const _MB_FIELD_LABELS = {
    title: (c) => `Title: <em>${escapeHtml(c.library_title || '')}</em> → <strong>${escapeHtml(c.mb_title || '')}</strong>`,
    track_number: (c) => `Track #: ${escapeHtml(String(c.library_track_number ?? '—'))} → ${escapeHtml(String(c.mb_track_number))}`,
    disc_number: (c) => `Disc: ${escapeHtml(String(c.library_disc_number ?? 1))} → ${escapeHtml(String(c.mb_disc_number))}`,
    mbid: (c) => `MusicBrainz Recording ID: ${c.library_mbid ? '<em>' + escapeHtml(c.library_mbid) + '</em>' : '<em>missing</em>'} → <strong>added</strong>`,
    duration: (c) => `Length: ${escapeHtml(String(c.library_duration ?? '—'))} → ${escapeHtml(String(c.mb_duration ?? '—'))} <span class="text-muted">(informational — not editable)</span>`,
};

function _displayMBComparison(data) {
    document.querySelectorAll('.mb-update-row').forEach(el => el.remove());
    document.querySelectorAll('.mb-missing-row').forEach(el => el.remove());
    document.querySelectorAll('.mb-extra-row').forEach(el => el.remove());
    document.querySelectorAll('.mb-extra-badge-dynamic').forEach(el => el.remove());

    const needsUpdate = data.comparison.filter(c => c.needs_update && c.library_track_id);
    const missingTracks = data.comparison.filter(c => !c.matched);
    const extraTracks = data.extra_tracks || [];

    if (needsUpdate.length === 0 && missingTracks.length === 0 && extraTracks.length === 0) {
        _showMBCompareBanner(
            `<i class="bi bi-check-circle-fill text-success me-2"></i>All ${escapeHtml(String(data.total_tracks))} tracks match MusicBrainz metadata — no updates needed.`,
            'success',
            true
        );
        return;
    }

    if (missingTracks.length > 0) _injectMissingTrackRows(missingTracks, data);
    if (extraTracks.length > 0) _markExtraTracks(extraTracks);

    if (needsUpdate.length === 0) {
        let onlyMsg = '';
        if (missingTracks.length > 0 && extraTracks.length > 0) {
            onlyMsg = `<strong>${escapeHtml(String(missingTracks.length))}</strong> ${missingTracks.length === 1 ? 'track is' : 'tracks are'} missing from the library and <strong>${escapeHtml(String(extraTracks.length))}</strong> ${extraTracks.length === 1 ? 'track is' : 'tracks are'} not in the MusicBrainz tracklist.`;
        } else if (missingTracks.length > 0) {
            onlyMsg = `<strong>${escapeHtml(String(missingTracks.length))} of ${escapeHtml(String(data.total_tracks))} tracks</strong> are missing from the library.`;
        } else {
            onlyMsg = `<strong>${escapeHtml(String(extraTracks.length))}</strong> ${extraTracks.length === 1 ? 'track in the library is' : 'tracks in the library are'} not found in the MusicBrainz tracklist for this release.`;
        }
        _showMBCompareBanner(`<i class="bi bi-exclamation-triangle-fill me-2"></i>${onlyMsg}`, 'warning', true);
        return;
    }

    let bannerMsg = `<strong>${escapeHtml(String(needsUpdate.length))} of ${escapeHtml(String(data.total_tracks))} tracks</strong> have metadata that can be updated from MusicBrainz.`;
    if (missingTracks.length > 0) bannerMsg += ` <strong>${escapeHtml(String(missingTracks.length))}</strong> ${missingTracks.length === 1 ? 'track is' : 'tracks are'} missing from the library.`;
    if (extraTracks.length > 0) bannerMsg += ` <strong>${escapeHtml(String(extraTracks.length))}</strong> ${extraTracks.length === 1 ? 'track is' : 'tracks are'} not in the MusicBrainz tracklist.`;

    let banner = document.getElementById('mb-compare-banner');
    if (!banner) {
        const container = _getMBCompareBannerContainer();
        if (!container) return;
        banner = document.createElement('div');
        banner.id = 'mb-compare-banner';
        const tableResponsive = document.querySelector('#album-tracks-section .table-responsive');
        container.insertBefore(banner, tableResponsive);
    }
    banner.innerHTML = `
        <div class="alert alert-warning d-flex align-items-center gap-2 mb-2 py-2 flex-wrap">
            <i class="bi bi-exclamation-triangle-fill"></i>
            <div>${bannerMsg}</div>
            <button class="btn btn-warning btn-sm ms-auto" onclick="updateAllTracksFromMB()"><i class="bi bi-arrow-repeat"></i> Update All</button>
            <button type="button" class="btn-close" onclick="clearMBComparison()" aria-label="Dismiss"></button>
        </div>`;

    const tbody = document.getElementById('albumTracksTbody');
    for (const trackComp of data.comparison) {
        if (!trackComp.needs_update || !trackComp.library_track_id) continue;
        const trackId = String(trackComp.library_track_id);
        const trackRow = tbody ? tbody.querySelector(`tr[data-track-id="${CSS.escape(trackId)}"]`) : null;
        if (!trackRow) continue;

        let insertAfter = trackRow;
        let sib = insertAfter.nextElementSibling;
        while (sib && (sib.classList.contains('mb-update-row') || sib.classList.contains('mb-missing-row'))) {
            insertAfter = sib;
            sib = sib.nextElementSibling;
        }

        for (const field of trackComp.diff_fields) {
            if (!_MB_FIELD_LABELS[field]) continue;
            const updateRow = document.createElement('tr');
            updateRow.className = 'mb-update-row';
            updateRow.dataset.trackId = trackId;
            updateRow.dataset.mbComp = JSON.stringify(trackComp);
            updateRow.dataset.mbField = field;
            updateRow.innerHTML = `
                <td colspan="6" style="padding: 0.3rem 0.75rem; border-top: none;">
                    <div class="d-flex align-items-center gap-2 flex-wrap rounded px-2 py-1" style="background: rgba(255,193,7,0.12); border: 1px solid rgba(255,193,7,0.35);">
                        <small class="text-warning-emphasis"><i class="bi bi-lightning-fill me-1"></i><strong>MusicBrainz:</strong></small>
                        <small class="text-muted">${_MB_FIELD_LABELS[field](trackComp)}</small>
                        <button class="btn btn-warning btn-sm ms-auto py-0 px-2" style="font-size: 0.75rem; white-space: nowrap;" onclick="applyMBField(this)"><i class="bi bi-check-lg"></i> Apply</button>
                        <button class="btn btn-outline-secondary btn-sm py-0 px-2" style="font-size: 0.75rem; white-space: nowrap;" onclick="ignoreMBField(this)"><i class="bi bi-x-lg"></i> Ignore</button>
                    </div>
                </td>`;
            insertAfter.insertAdjacentElement('afterend', updateRow);
            insertAfter = updateRow;
        }
    }
}

function _buildMissingTrackRow(trackComp, data) {
    const pageArtist = window._pageData ? window._pageData.artistName : '';
    const pageAlbum = window._pageData ? window._pageData.albumName : '';
    const safeTitle = escapeHtml(trackComp.mb_title || '');
    const safeTrackNum = escapeHtml(String(trackComp.mb_track_number || ''));
    const safeDiscNum = escapeHtml(String(trackComp.mb_disc_number || 1));
    const safeRecordingMbid = escapeHtml(String(trackComp.mb_recording_mbid || ''));
    const safeDuration = trackComp.mb_duration != null ? String(trackComp.mb_duration) : '';
    const safeYear = escapeHtml(String(data.mb_year || ''));
    const safeReleaseId = escapeHtml(String(data.release_mbid || ''));

    const row = document.createElement('tr');
    row.className = 'text-muted missing-track-row mb-missing-row';
    row.style.opacity = '0.6';
    row.innerHTML = `
        <td></td>
        <td class="fst-italic">${safeTrackNum || '?'}</td>
        <td colspan="3" class="fst-italic">
            ${safeTitle}
            <span class="badge bg-warning text-dark ms-2" style="font-size: 0.65rem;">Missing</span>
        </td>
        <td class="text-end">
            <div class="btn-group btn-group-sm">
                <button class="btn btn-outline-success py-0 px-2" title="Add to download queue"
                    data-artist="${escapeHtml(pageArtist)}" data-album-artist="${escapeHtml(pageArtist)}"
                    data-title="${safeTitle}" data-album="${escapeHtml(pageAlbum)}"
                    data-track-number="${safeTrackNum}" data-disc-number="${safeDiscNum}"
                    data-year="${safeYear}" data-release-id="${safeReleaseId}"
                    data-recording-mbid="${safeRecordingMbid}" data-duration="${escapeHtml(safeDuration)}"
                    onclick="queueMissingTrack(this)"><i class="bi bi-download"></i></button>
                <button class="btn btn-outline-primary py-0 px-2" title="Match to an existing track in the library"
                    data-title="${safeTitle}" data-track-number="${safeTrackNum}" data-disc-number="${safeDiscNum}"
                    data-recording-mbid="${safeRecordingMbid}"
                    onclick="openAlbumMatchModal(this)"><i class="bi bi-link-45deg"></i></button>
                <button class="btn btn-outline-secondary py-0 px-2" title="Hide this track from the missing list"
                    onclick="ignoreMissingTrack(this)"><i class="bi bi-x-lg"></i></button>
            </div>
        </td>`;
    return row;
}

function _injectMissingTrackRows(missingTracks, data) {
    const tbody = document.getElementById('albumTracksTbody');
    if (!tbody) return;
    missingTracks.forEach(trackComp => {
        const idx = data.comparison.indexOf(trackComp);
        let insertAfterRow = null;
        for (let i = idx - 1; i >= 0; i--) {
            const prev = data.comparison[i];
            if (prev.matched && prev.library_track_id) {
                const candidate = tbody.querySelector(`tr[data-track-id="${CSS.escape(String(prev.library_track_id))}"]`);
                if (candidate) { insertAfterRow = candidate; break; }
            }
        }
        const row = _buildMissingTrackRow(trackComp, data);
        if (insertAfterRow) {
            let next = insertAfterRow.nextElementSibling;
            while (next && (next.classList.contains('mb-update-row') || next.classList.contains('mb-missing-row'))) {
                insertAfterRow = next;
                next = next.nextElementSibling;
            }
            insertAfterRow.insertAdjacentElement('afterend', row);
        } else {
            const firstRow = tbody.querySelector('tr[data-track-id]');
            if (firstRow) firstRow.insertAdjacentElement('beforebegin', row);
            else tbody.appendChild(row);
        }
    });
}

function _markExtraTracks(extraTracks) {
    const tbody = document.getElementById('albumTracksTbody');
    if (!tbody) return;
    for (const extra of extraTracks) {
        const trackId = String(extra.library_track_id);
        const trackRow = tbody.querySelector(`tr[data-track-id="${CSS.escape(trackId)}"]`);
        if (!trackRow) continue;

        if (!trackRow.querySelector('.mb-extra-badge')) {
            const titleLink = trackRow.querySelector('.track-title-display-' + trackId);
            if (titleLink) {
                const badge = document.createElement('span');
                badge.className = 'badge bg-secondary ms-1 mb-extra-badge mb-extra-badge-dynamic';
                badge.title = 'This track was not found in the MusicBrainz tracklist for this release';
                badge.textContent = 'Extra';
                titleLink.insertAdjacentElement('afterend', badge);
            }
        }

        const subRow = document.createElement('tr');
        subRow.className = 'mb-extra-row';
        subRow.innerHTML = `
            <td colspan="6" style="padding: 0.3rem 0.75rem; border-top: none;">
                <div class="d-flex align-items-center gap-2 flex-wrap rounded px-2 py-1" style="background: rgba(108,117,125,0.12); border: 1px solid rgba(108,117,125,0.35);">
                    <small class="text-secondary"><i class="bi bi-question-circle-fill me-1"></i><strong>MusicBrainz:</strong></small>
                    <small class="text-muted">Not found in the MusicBrainz tracklist for this release</small>
                </div>
            </td>`;
        let insertAfter = trackRow;
        let sib = insertAfter.nextElementSibling;
        while (sib && (sib.classList.contains('mb-update-row') || sib.classList.contains('mb-missing-row') || sib.classList.contains('mb-extra-row'))) {
            insertAfter = sib;
            sib = sib.nextElementSibling;
        }
        insertAfter.insertAdjacentElement('afterend', subRow);
    }
}

// Applies (or dismisses, for the informational "duration" field) a single
// diff field for one track. Shared by the per-row Apply button and Update All.
async function _applyOneMBField(updateRow) {
    const trackComp = JSON.parse(updateRow.dataset.mbComp || '{}');
    const trackId = String(trackComp.library_track_id || updateRow.dataset.trackId || '');
    const field = updateRow.dataset.mbField || '';

    if (field === 'duration') {
        // Duration is intrinsic to the audio file, not an editable metadata
        // field — there is nothing to write. Just dismiss the row.
        updateRow.remove();
        return true;
    }

    let value = null;
    if (field === 'title') value = trackComp.mb_title;
    else if (field === 'track_number') value = String(trackComp.mb_track_number);
    else if (field === 'disc_number') value = String(trackComp.mb_disc_number);
    else if (field === 'mbid') value = trackComp.mb_recording_mbid;
    if (value === null) { updateRow.remove(); return true; }

    try {
        const resp = await fetch(`${_API_V1_PREFIX}/tracks/${encodeURIComponent(trackId)}/apply-mb-field`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ field, value })
        });
        const result = await resp.json();
        if (!result.success) return false;

        updateRow.remove();
        if (field === 'title') {
            document.querySelectorAll(`.track-title-display-${CSS.escape(trackId)}`).forEach(el => { el.textContent = trackComp.mb_title; });
        } else if (field === 'track_number') {
            document.querySelectorAll(`.track-number-display-${CSS.escape(trackId)}`).forEach(el => { el.textContent = trackComp.mb_track_number; });
        }
        return true;
    } catch (_e) {
        return false;
    }
}

window.applyMBField = async function (btn) {
    const updateRow = btn.closest('.mb-update-row');
    if (!updateRow) return;
    const origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

    const ok = await _applyOneMBField(updateRow);
    if (ok) {
        if (!document.querySelector('.mb-update-row')) {
            _showMBCompareBanner('<i class="bi bi-check-circle-fill text-success me-2"></i>All MusicBrainz suggestions have been applied.', 'success', true);
        }
    } else {
        btn.disabled = false;
        btn.innerHTML = origHtml;
        alert('Failed to apply MusicBrainz update.');
    }
};

window.ignoreMBField = async function (btn) {
    const updateRow = btn.closest('.mb-update-row');
    if (!updateRow) return;
    const trackComp = JSON.parse(updateRow.dataset.mbComp || '{}');
    const trackId = String(trackComp.library_track_id || updateRow.dataset.trackId || '');
    const field = updateRow.dataset.mbField || '';
    const origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

    try {
        // Persists into the tracks.mb_ignored_fields column — the SAME column
        // compare_musicbrainz_release() already reads to suppress diff_fields,
        // so an ignored field stays ignored on future comparisons too.
        const resp = await fetch(`${_API_V1_PREFIX}/tracks/${encodeURIComponent(trackId)}/ignore-mb-field`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ field })
        });
        const result = await resp.json();
        if (result.success) {
            updateRow.remove();
            if (!document.querySelector('.mb-update-row')) {
                _showMBCompareBanner('<i class="bi bi-check-circle-fill text-success me-2"></i>All MusicBrainz suggestions have been handled.', 'success', true);
            }
        } else {
            btn.disabled = false;
            btn.innerHTML = origHtml;
            alert('Failed to ignore MusicBrainz field: ' + (result.error || 'Unknown error'));
        }
    } catch (err) {
        btn.disabled = false;
        btn.innerHTML = origHtml;
        alert('Network error: ' + err.message);
    }
};

window.updateAllTracksFromMB = async function () {
    const rows = Array.from(document.querySelectorAll('.mb-update-row'));
    if (rows.length === 0) return;
    if (!confirm(`Apply ${rows.length} MusicBrainz suggestion(s)?`)) return;

    let applied = 0, failed = 0;
    for (const row of rows) {
        // Row may have already been removed by a preceding same-track apply
        // (e.g. Update All processing several fields for the same track).
        if (!row.isConnected) continue;
        const ok = await _applyOneMBField(row);
        if (ok) applied++; else failed++;
    }

    const type = failed > 0 ? 'warning' : 'success';
    const icon = failed > 0 ? 'exclamation-triangle-fill' : 'check-circle-fill';
    const msg = `Applied ${applied} update(s)` + (failed > 0 ? `, ${failed} failed.` : ' successfully.');
    _showMBCompareBanner(`<i class="bi bi-${icon} me-2"></i>${escapeHtml(msg)}`, type, true);
};

// ---------------------------------------------------------------------------
// Missing tracks: queue via Soulseek, match to an existing track, or hide
// ---------------------------------------------------------------------------
window.queueMissingTrack = function (btn) {
    const artist = btn.dataset.artist || btn.dataset.albumArtist;
    const title = btn.dataset.title;
    const album = btn.dataset.album;
    const trackNumber = btn.dataset.trackNumber || null;
    const discNumber = btn.dataset.discNumber || null;
    const year = btn.dataset.year || null;
    const recordingMbid = btn.dataset.recordingMbid || null;
    const duration = btn.dataset.duration ? parseInt(btn.dataset.duration, 10) : null;
    const releaseId = btn.dataset.releaseId || _getLinkedReleaseMbid() || null;

    const origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

    fetch('/api/queue/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            artist: artist,
            title: title,
            album: album,
            album_artist: btn.dataset.albumArtist || null,
            track_number: trackNumber,
            disc_number: discNumber,
            year: year,
            release_id: releaseId,
            release_mbid: releaseId,
            release_source: releaseId ? 'musicbrainz' : null,
            recording_mbid: recordingMbid,
            duration: duration,
            source: 'soulseek'
        })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                btn.innerHTML = '<i class="bi bi-check-lg"></i>';
                btn.classList.remove('btn-outline-success');
                btn.classList.add('btn-success');
                btn.title = 'Added to download queue';
            } else {
                btn.disabled = false;
                btn.innerHTML = origHtml;
                alert('Failed to queue track: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => {
            btn.disabled = false;
            btn.innerHTML = origHtml;
            alert('Network error: ' + err.message);
        });
};

// Hides a missing-track row for this viewing session only. Unlike ignoring a
// diff FIELD (which persists to tracks.mb_ignored_fields), there is no
// existing table for persisting "this missing recording is expected to stay
// missing" — so this intentionally does not survive a page reload / re-run
// of Compare. If you want that to persist, it needs a small server-side
// table (e.g. ignored_missing_recordings(artist, album, recording_mbid)) and
// an endpoint for it.
window.ignoreMissingTrack = function (btn) {
    const row = btn.closest('tr');
    if (row) row.remove();
};

// ---------------------------------------------------------------------------
// Match a missing MusicBrainz track to an existing (unmatched) library track
// ---------------------------------------------------------------------------
// Candidates are the CURRENT comparison's extra_tracks — library rows the
// comparison could not match to anything in the MB tracklist. This avoids
// needing a separate "list all tracks" endpoint: the exact set of orphaned
// local tracks is already sitting in window._mbComparisonData from the last
// Compare run.
let _matchMbTrack = null;

window.openAlbumMatchModal = function (btn) {
    _matchMbTrack = {
        title: btn.dataset.title,
        track_number: btn.dataset.trackNumber,
        disc_number: btn.dataset.discNumber,
        recording_mbid: btn.dataset.recordingMbid,
    };
    document.getElementById('albumMatchMbTitle').textContent = _matchMbTrack.title;
    document.getElementById('albumMatchMbTrackNum').textContent = _matchMbTrack.track_number || '?';

    const tbody = document.getElementById('albumMatchCandidatesTbody');
    const candidates = (window._mbComparisonData && window._mbComparisonData.extra_tracks) || [];
    if (candidates.length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" class="text-center text-muted">No unmatched library tracks available to match against. Run Compare with MusicBrainz again if you expect one here.</td></tr>';
    } else {
        tbody.innerHTML = candidates.map(t => `
            <tr>
                <td class="text-muted">${escapeHtml(String(t.library_track_number || '—'))}</td>
                <td>${escapeHtml(t.library_title || '—')}</td>
                <td class="text-center">
                    <button class="btn btn-sm btn-primary" onclick="doAlbumMatchTrack('${escapeJsString(String(t.library_track_id))}')">
                        <i class="bi bi-check-lg"></i> Match
                    </button>
                </td>
            </tr>`).join('');
    }

    const modalEl = document.getElementById('albumMatchTrackModal');
    if (modalEl && window.bootstrap) bootstrap.Modal.getOrCreateInstance(modalEl).show();
};

window.doAlbumMatchTrack = async function (trackId) {
    if (!_matchMbTrack) return;
    if (!confirm(`Apply "${_matchMbTrack.title}" (track ${_matchMbTrack.track_number || '?'}) to this track?\n\nThis updates its title, track number and MusicBrainz recording ID.`)) return;

    const fields = [
        ['title', _matchMbTrack.title],
        ['track_number', _matchMbTrack.track_number],
        ['disc_number', _matchMbTrack.disc_number],
        ['mbid', _matchMbTrack.recording_mbid],
    ].filter(([, value]) => value !== undefined && value !== null && value !== '');

    let failed = false;
    for (const [field, value] of fields) {
        try {
            const resp = await fetch(`${_API_V1_PREFIX}/tracks/${encodeURIComponent(trackId)}/apply-mb-field`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ field, value })
            });
            const result = await resp.json();
            if (!result.success) failed = true;
        } catch (_e) {
            failed = true;
        }
    }

    const modalEl = document.getElementById('albumMatchTrackModal');
    if (modalEl && window.bootstrap) {
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();
    }

    if (failed) {
        alert('Some fields could not be applied. Reloading to show the current state.');
    }
    window.location.reload();
};

// ---------------------------------------------------------------------------
// Bulk track selection + delete
// ---------------------------------------------------------------------------
window.toggleSelectAll = function (checkbox) {
    document.querySelectorAll('.track-checkbox').forEach(cb => { cb.checked = checkbox.checked; });
    window.updateBulkActionsUI();
};

window.selectAllTracks = function () {
    document.querySelectorAll('.track-checkbox').forEach(cb => { cb.checked = true; });
    const selectAll = document.getElementById('selectAllTracksCheckbox');
    if (selectAll) selectAll.checked = true;
    window.updateBulkActionsUI();
};

window.clearAllTracks = function () {
    document.querySelectorAll('.track-checkbox').forEach(cb => { cb.checked = false; });
    const selectAll = document.getElementById('selectAllTracksCheckbox');
    if (selectAll) selectAll.checked = false;
    window.updateBulkActionsUI();
};

window.updateBulkActionsUI = function () {
    const checked = document.querySelectorAll('.track-checkbox:checked');
    const toolbar = document.getElementById('bulkActionsToolbar');
    const countEl = document.getElementById('selectedCount');
    if (!toolbar) return;
    if (checked.length > 0) {
        toolbar.classList.remove('d-none');
        toolbar.classList.add('d-flex');
        if (countEl) countEl.textContent = String(checked.length);
    } else {
        toolbar.classList.add('d-none');
        toolbar.classList.remove('d-flex');
    }
};

function _getSelectedTrackIds() {
    return Array.from(document.querySelectorAll('.track-checkbox:checked')).map(cb => cb.dataset.trackId);
}

window.confirmBulkDeleteTracks = function () {
    const ids = _getSelectedTrackIds();
    if (ids.length === 0) { alert('Please select at least one track.'); return; }
    document.getElementById('deleteTrackCount').textContent = String(ids.length);
    const modalEl = document.getElementById('bulkDeleteModal');
    if (modalEl && window.bootstrap) bootstrap.Modal.getOrCreateInstance(modalEl).show();
};

window.deleteDatabaseOnly = function () {
    const modalEl = document.getElementById('bulkDeleteModal');
    if (modalEl && window.bootstrap) { const m = bootstrap.Modal.getInstance(modalEl); if (m) m.hide(); }
    _performBulkDelete(false);
};

window.deleteWithFiles = function () {
    const modalEl = document.getElementById('bulkDeleteModal');
    if (modalEl && window.bootstrap) { const m = bootstrap.Modal.getInstance(modalEl); if (m) m.hide(); }
    const ids = _getSelectedTrackIds();
    if (!confirm(`⚠️ Are you absolutely sure?\n\nThis will PERMANENTLY delete ${ids.length} file(s) from disk.\n\nThis action cannot be undone.`)) return;
    _performBulkDelete(true);
};

function _performBulkDelete(deleteFiles) {
    const ids = _getSelectedTrackIds();
    if (ids.length === 0) return;
    const artist = window._pageData ? window._pageData.artistName : '';
    const album = window._pageData ? window._pageData.albumName : '';

    fetch(`${_API_V1_PREFIX}/albums/${encodeURIComponent(artist)}/${encodeURIComponent(album)}/bulk-delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_ids: ids, delete_files: deleteFiles })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                alert(`✅ Deleted ${data.deleted_count} track(s)${deleteFiles ? ' from files and database' : ' from database'}`);
                window.location.reload();
            } else {
                alert('❌ Error: ' + (data.error || 'Failed to delete tracks'));
            }
        })
        .catch(err => {
            alert('❌ Network error: ' + err.message);
        });
}

// ---------------------------------------------------------------------------
// Similar artists
// ---------------------------------------------------------------------------
// The album page renders #albumSimilarArtistsContainer with a permanent
// spinner and nothing ever populated it — the fetch lived only on the TRACK
// page's inline script. The spinner span therefore never stopped.
document.addEventListener('DOMContentLoaded', function () {
    const container = document.getElementById('albumSimilarArtistsContainer');
    if (!container) return;

    const artist = window._pageData ? window._pageData.artistName : '';
    if (!artist) {
        container.innerHTML = '<div class="text-center py-3 text-muted small">No artist context.</div>';
        return;
    }

    fetch(`/api/artist/${encodeURIComponent(artist)}/similar`)
        .then(r => r.json())
        .then(data => {
            const sim = data.similar_artists || {};
            const merged = [...(sim.lastfm || []), ...(sim.listenbrainz || [])]
                .map(a => (typeof a === 'string' ? a : a.name))
                .filter(Boolean)
                .filter((v, i, arr) => arr.indexOf(v) === i)
                .slice(0, 10);

            if (!merged.length) {
                container.innerHTML = '<div class="text-center py-3 text-muted small">No similar artists data available.</div>';
                return;
            }

            const wrap = document.createElement('div');
            wrap.className = 'd-flex flex-wrap gap-2';
            merged.forEach(name => {
                const a = document.createElement('a');
                a.href = '/artist/' + encodeURIComponent(name);
                a.className = 'badge bg-secondary text-decoration-none text-light border border-secondary';
                a.style.cssText = 'font-size:0.8rem; padding:0.4rem 0.6rem;';
                a.textContent = name;
                wrap.appendChild(a);
            });
            container.innerHTML = '';
            container.appendChild(wrap);
        })
        .catch(() => {
            container.innerHTML = '<div class="text-danger small text-center py-3">Failed to load similar artists.</div>';
        });
});
