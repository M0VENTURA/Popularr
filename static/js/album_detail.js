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

/** In-flight lookup popup. Module-scoped: opened by the picker, released by
 *  the match callback, which is a separate function. */
let _albumLookupBusy = null;

/** Release the lookup popup. Safe to call repeatedly / with no popup. */
function _endAlbumLookupBusy() {
    if (window.busyPopup) window.busyPopup.hide(_albumLookupBusy);
    _albumLookupBusy = null;
}

window.openAlbumLookupModal = function () {
    const artist = window._pageData ? window._pageData.artistName : '';
    const album = window._pageData ? window._pageData.albumName : '';

    if (typeof window.openGlobalMbSearch !== 'function') {
        console.error('openGlobalMbSearch is unavailable — is main.js loaded?');
        alert('MusicBrainz search is unavailable on this page.');
        return;
    }

    // The lookup is a network round trip that continues after the user picks
    // a release (applyAlbumMbid resolves the edition), so the popup is opened
    // here and released in applyAlbumMbid rather than wrapped. "Looking up
    // a match" was previously entirely silent.
    _albumLookupBusy = window.busyPopup
        ? window.busyPopup.show('Looking up MusicBrainz match…')
        : null;

    window.openGlobalMbSearch(artist, album, function (selected) {
        if (selected && selected.id) {
            // Release even if the apply throws, so the popup cannot strand.
            Promise.resolve()
                .then(() => window.applyAlbumMbid(selected.id))
                .catch(function (error) {
                    console.error('Applying the MusicBrainz match failed', error);
                    alert('Could not apply the MusicBrainz match.');
                })
                .finally(_endAlbumLookupBusy);
        } else {
            _endAlbumLookupBusy();
        }
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
//
// ⚠️ NOTHING IS WRITTEN HERE. The id goes into the form and the full metadata
// preview (album fields + per-track changes, see js/metadata-review.js) is
// staged into #staged_track_updates; the form's own submit persists it. A bad
// match is therefore undone by simply reloading the page.
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

    // Full metadata preview: every album-level field plus each per-track
    // change a metadata import would write, shown as orange bars. Staged only.
    const review = window.albumMetadataReview;
    if (!review || typeof review.applyProposal !== 'function') {
        alert('MusicBrainz ID applied! Click "Save Metadata" to persist changes.');
        return;
    }

    review.applyProposal(mbid).then(function (staged) {
        if (!staged) {
            // The preview could not be built — the id is still applied, so the
            // user can save it and run Compare with MusicBrainz manually.
            alert('MusicBrainz ID applied! Click "Save Metadata" to persist changes.');
            return;
        }
        const counts = staged.counts || {};
        alert(
            'Release matched. ' + (counts.album_changes || 0) + ' album field(s) and ' +
            (counts.tracks_changed || 0) + ' track(s) have MusicBrainz updates to review.\n\n' +
            'Check the Edit Album tab and the orange bars, then click "Save Metadata".'
        );
    }).catch(function (error) {
        console.error('Could not build the metadata preview', error);
        alert('MusicBrainz ID applied! Click "Save Metadata" to persist changes.');
    });
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
    // ⚠️ Defensive on purpose. `loadAlbumMissingTracks` calls this INSIDE A LOOP
    // over the missing rows, so a single throw here (a missing input, or one
    // without a .value) would abort the whole loop and lose every row — the
    // album page would look complete while actually showing nothing.
    const valueOf = function (id) {
        const el = document.getElementById(id);
        const raw = el && el.value;
        return typeof raw === 'string' ? raw.trim() : '';
    };
    return valueOf('album_release_group_mbid') || valueOf('album_mbid') || '';
}

/**
 * Refresh the album page after a mutation.
 *
 * ⚠️ THIS FUNCTION WAS REFERENCED BUT NEVER DEFINED, in either tree. Callers
 * guarded with `typeof window.refreshAlbumPage === 'function'`, which turned
 * the missing definition into a SILENT no-op: after changing the album art or
 * auto-linking MBIDs the page kept showing the old state and reported no error.
 *
 * The full reload is deliberate. Applying a new cover rewrites the art file and
 * `/art` is streamed from a BytesIO with no ETag/Cache-Control, so reloading
 * reliably re-fetches it rather than needing a `?t=` cache-buster. And
 * auto-linking can change several things at once (per-track MB status, the
 * comparison banner), so patching one element's `src` would leave the rest
 * stale.
 */
window.refreshAlbumPage = function () {
    window.location.reload();
};

/**
 * Auto-link Recording MBIDs for this album's unlinked tracks.
 *
 * ⚠️ THIS FUNCTION WAS MISSING ENTIRELY. Both the Actions dropdown item
 * ("Auto-Link MBIDs") and the inline "Link" button already called
 * `autoLinkAllMbids()` — four call sites across the two trees — but nothing
 * defined it, so every click threw `ReferenceError: autoLinkAllMbids is not
 * defined`. A template has no compiler, so the broken button shipped.
 *
 * The endpoint it should have called already existed and was reachable:
 * POST /api/musicbrainz/link-album-mbids, which matches the local (unlinked)
 * tracklist against an MB release's recordings and writes
 * musicbrainz_trackid + recording_mbid onto each matched row.
 *
 * Needs a release MBID to fetch a tracklist from. `_getLinkedReleaseMbid()`
 * returns the release-group id when the concrete release id is empty, and the
 * endpoint only acts on the latter (it validates a UUID and fetches the
 * release) — so a group-only album gets the endpoint's own explanatory
 * message rather than a silent no-op.
 */
window.autoLinkAllMbids = function () {
    const artist = window._pageData ? window._pageData.artistName : '';
    const album = window._pageData ? window._pageData.albumName : '';
    const releaseId = _getLinkedReleaseMbid();

    const run = function () {
        return fetch('/api/musicbrainz/link-album-mbids', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artist: artist, album: album, release_id: releaseId })
        })
            .then(r => r.json())
            .then(data => {
                if (!data || data.success !== true) {
                    alert('Auto-linking MBIDs failed: ' + ((data && data.error) || 'Unknown error'));
                    return;
                }
                // `linked: 0` with a message is the normal "nothing left to do"
                // case, so the server's own sentence is the best thing to show.
                alert(data.message || ('Linked ' + (data.linked || 0) + ' track(s).'));
                if (typeof window.refreshAlbumPage === 'function') window.refreshAlbumPage();
            })
            .catch(err => alert('Error: ' + err.message));
    };

    if (window.busyPopup) {
        // showAndRun releases the popup in a finally, so a throw cannot strand
        // it, and its own rejection is already surfaced by the catch above.
        return window.busyPopup.showAndRun('Auto-linking MusicBrainz IDs…', run);
    }
    return run();
};

/**
 * Download EVERY track this album is missing from the library.
 *
 * ⚠️ THIS FUNCTION WAS MISSING ENTIRELY — `onclick="downloadMissingTracks()"`
 * on the Actions dropdown threw ReferenceError.
 *
 * The missing tracks are already rendered with their own handlers, and each
 * carries its payload either in `data-*` attributes (live tree) or in a
 * closure (rebuilt tree). A closure payload cannot be read back out of the
 * DOM, so this clicks the same buttons the user would — one code path, so the
 * MBID/duration/source handling is identical and cannot drift.
 */

//: Missing-track rows exist in BOTH trees but with different markup:
//:   live    — `<button ... onclick="queueMissingTrack(this)">`, payload in data-*
//:   rebuilt — `<button class="... mb-queue-missing">`, payload in a closure
//: Matching either keeps this working in both trees with no feature flag.
const MISSING_QUEUE_SELECTOR = '.mb-queue-missing, [onclick*="queueMissingTrack("]';

//: A settled SUCCESS. button-state.js (rebuilt) sets this deliberately
//: non-reverting class; the live tree sets the same class by hand.
function _missingQueued(btn) { return btn.classList.contains('btn-success'); }

//: Still in flight. `_popularrBusy` covers the rebuilt tree even if its
//: handler has not yet disabled the button; `disabled` covers the live tree,
//: whose handler disables up front and only re-enables on FAILURE.
function _missingPending(btn) {
    return Boolean(btn._popularrBusy) || (btn.disabled && !_missingQueued(btn));
}
window.downloadMissingTracks = function () {
    const rows = Array.from(document.querySelectorAll(MISSING_QUEUE_SELECTOR));
    const pending = rows.filter(function (b) { return !b.disabled && !_missingQueued(b); });

    if (!pending.length) {
        alert(rows.length
            ? 'Every missing track has already been queued.'
            : 'No missing tracks to queue — run "Compare with MusicBrainz" first.');
        return;
    }

    if (!confirm('Add ' + pending.length + ' missing track(s) to the download queue?')) return;

    const ordered = pending.slice();
    const endBusy = window.busyPopup
        ? window.busyPopup.show('Adding missing tracks to queue…')
        : null;

    // Click the same buttons the user would, so the payload/MBID/source
    // handling is identical to the per-row path and cannot drift from it.
    ordered.forEach(function (b) { b.click(); });

    // Poll the buttons' own state rather than duplicating the queue logic.
    // This deliberately only *stops* when nothing is pending, so an unsettled
    // click is waited for rather than being misreported as a failure.
    const deadline = Date.now() + 60000;
    const tick = function () {
        const stillPending = ordered.filter(_missingPending).length;
        if (stillPending && Date.now() < deadline) { setTimeout(tick, 200); return; }

        if (window.busyPopup) window.busyPopup.hide(endBusy);
        const queued = ordered.filter(_missingQueued).length;
        if (!queued) {
            alert('No track could be queued — see the error shown on the row.');
        } else if (stillPending) {
            alert('Queued ' + queued + ' of ' + ordered.length + ' track(s). ' +
                  stillPending + ' still processing — watch the Downloads page.');
        } else {
            alert('Queued ' + queued + ' of ' + ordered.length + ' track(s).');
        }
    };
    setTimeout(tick, 200);
};

/**
 * Rename this album's files to match the configured naming format.
 *
 * ⚠️ THIS FUNCTION WAS MISSING ENTIRELY — `renameAlbumFiles(...)` on the
 * Actions dropdown threw ReferenceError. The endpoint already existed and was
 * reachable: POST /api/album/{artist}/{album}/rename-files.
 */
window.renameAlbumFiles = function (artistArg, albumArg) {
    const artist = artistArg || (window._pageData ? window._pageData.artistName : '');
    const album = albumArg || (window._pageData ? window._pageData.albumName : '');
    if (!artist || !album) {
        alert('Cannot rename files without an artist and album.');
        return;
    }
    if (!confirm('Rename every file in "' + album + '" to the configured naming format?\n\n' +
                 'Tags are re-read from the files, and the file paths in the database are updated.')) return;

    const run = function () {
        return fetch('/api/album/' + encodeURIComponent(artist) + '/' + encodeURIComponent(album) + '/rename-files', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || data.success !== true) {
                    alert('Rename failed: ' + ((data && data.error) || 'Unknown error'));
                    return;
                }
                alert(data.message || ('Renamed ' + (data.renamed_count || 0) + ' file(s).'));
                if (Array.isArray(data.errors) && data.errors.length) {
                    alert(data.errors.length + ' file(s) could not be renamed — see the logs.');
                }
            })
            .catch(function (err) { alert('Error: ' + err.message); });
    };

    if (window.busyPopup) return window.busyPopup.showAndRun('Renaming album files…', run);
    return run();
};

/**
 * Open the "Change Album Art" dialog: search external sources, paste a URL, or
 * upload a file.
 *
 * ⚠️ THIS FUNCTION WAS MISSING ENTIRELY — three call sites (the Actions
 * dropdown item AND the pencil over the album art) threw ReferenceError. All
 * three backend endpoints already existed:
 *   GET  /api/album/search-art?artist=&album=&source=
 *   POST /api/album/set-art      {artist, album, image_url}
 *   POST /api/album/upload-art   multipart: artist, album, image
 *
 * NOTE: search-art returns base64 `data:` URLs in `images[].url` (it embeds the
 * fetched bytes rather than exposing a remote link), and set-art accepts a
 * `data:` URL — so a search hit can be applied directly with no re-download.
 */
window.openAlbumArtModal = function () {
    const artist = window._pageData ? window._pageData.artistName : '';
    const album = window._pageData ? window._pageData.albumName : '';
    if (!artist || !album) {
        alert('Cannot change album art without an artist and album.');
        return;
    }

    const MODAL_ID = 'albumArtChangeModal';
    const existing = document.getElementById(MODAL_ID);
    if (existing) {
        if (window.bootstrap) window.bootstrap.Modal.getOrCreateInstance(existing).show();
        return;
    }

    const wrap = document.createElement('div');
    wrap.className = 'modal fade';
    wrap.id = MODAL_ID;
    wrap.tabIndex = -1;
    wrap.innerHTML =
        '<div class="modal-dialog modal-lg modal-dialog-centered">' +
        '  <div class="modal-content bg-dark text-light border-secondary">' +
        '    <div class="modal-header border-secondary">' +
        '      <h5 class="modal-title"><i class="bi bi-image me-2"></i>Change Album Art</h5>' +
        '      <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>' +
        '    </div>' +
        '    <div class="modal-body">' +
        '      <div class="d-flex gap-2 mb-3">' +
        '        <button type="button" class="btn btn-sm btn-outline-info" id="albumArtSearchBtn">' +
        '          <i class="bi bi-search me-1"></i>Search external sources</button>' +
        '        <select class="form-select form-select-sm bg-dark text-light border-secondary" id="albumArtSourceSel" style="max-width:12rem">' +
        '          <option value="musicbrainz" selected>MusicBrainz</option>' +
        '          <option value="discogs">Discogs</option>' +
        '          <option value="itunes">iTunes</option>' +
        '          <option value="audiodb">AudioDB</option>' +
        '        </select>' +
        '      </div>' +
        '      <div id="albumArtStatus" class="small text-muted mb-2"></div>' +
        '      <div id="albumArtResults" class="row g-2 mb-3"></div>' +
        '      <hr class="border-secondary">' +
        '      <label for="albumArtUrlInput" class="form-label small">…or paste an image URL</label>' +
        '      <div class="input-group input-group-sm mb-3">' +
        '        <input type="url" class="form-control bg-dark text-light border-secondary" id="albumArtUrlInput" placeholder="https://…/cover.jpg">' +
        '        <button class="btn btn-outline-success" type="button" id="albumArtUrlApplyBtn">Apply</button>' +
        '      </div>' +
        '      <label for="albumArtFileInput" class="form-label small">…or upload a file</label>' +
        '      <div class="input-group input-group-sm">' +
        '        <input type="file" class="form-control bg-dark text-light border-secondary" id="albumArtFileInput" accept="image/*">' +
        '        <button class="btn btn-outline-success" type="button" id="albumArtUploadBtn">Upload</button>' +
        '      </div>' +
        '    </div>' +
        '    <div class="modal-footer border-secondary">' +
        '      <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Close</button>' +
        '    </div>' +
        '  </div>' +
        '</div>';
    document.body.appendChild(wrap);

    const modal = window.bootstrap ? new window.bootstrap.Modal(wrap) : null;
    const statusEl = wrap.querySelector('#albumArtStatus');
    const resultsEl = wrap.querySelector('#albumArtResults');

    const setStatus = function (msg, isError) {
        statusEl.textContent = msg || '';
        statusEl.className = 'small mb-2 ' + (isError ? 'text-danger' : 'text-muted');
    };

    // Local busy affordance. This must NOT use `window.buttonState`: the live
    // tree does not load button-state.js at all, so depending on it would make
    // every one of these buttons silently skip its busy state there.
    const withSpinner = function (btn, label, fn) {
        if (!btn) return fn();
        const originalHtml = btn.innerHTML;
        const wasDisabled = btn.disabled;
        btn.disabled = true;
        if (label) {
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>' + label;
        }
        return Promise.resolve()
            .then(fn)
            .finally(function () { btn.disabled = wasDisabled; btn.innerHTML = originalHtml; });
    };

    const applyUrl = function (url, btn) {
        if (!url) return;
        const send = function () {
            return fetch('/api/album/set-art', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ artist: artist, album: album, image_url: url })
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data || data.success !== true) {
                        setStatus((data && data.error) || 'Could not set album art.', true);
                        return;
                    }
                    if (modal) modal.hide();
                    if (typeof window.refreshAlbumPage === 'function') window.refreshAlbumPage();
                })
                .catch(function (err) { setStatus('Error: ' + err.message, true); });
        };
        return withSpinner(btn, '', send);
    };

    wrap.querySelector('#albumArtSearchBtn').addEventListener('click', function () {
        const source = wrap.querySelector('#albumArtSourceSel').value;
        const btn = this;
        const search = function () {
            setStatus('Searching…');
            resultsEl.innerHTML = '';
            const url = '/api/album/search-art?artist=' + encodeURIComponent(artist) +
                        '&album=' + encodeURIComponent(album) +
                        '&source=' + encodeURIComponent(source);
            return fetch(url)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    const images = (data && data.images) || [];
                    if (!images.length) {
                        setStatus((data && data.error) || 'No images found on that source.', true);
                        return;
                    }
                    setStatus(images.length + ' image(s) found — click one to apply.');
                    resultsEl.innerHTML = images.map(function (img, i) {
                        const u = (typeof img === 'string') ? img : (img.url || img.image_url || '');
                        return '<div class="col-4 col-md-3">' +
                            '<button type="button" class="btn p-0 border-0 w-100 album-art-pick" data-url="' +
                            String(u).replace(/"/g, '&quot;') + '" title="Use this image">' +
                            '<img src="' + u + '" class="img-fluid rounded" style="aspect-ratio:1;object-fit:cover" alt="Album art candidate ' + (i + 1) + '">' +
                            '</button></div>';
                    }).join('');
                    resultsEl.querySelectorAll('.album-art-pick').forEach(function (b) {
                        b.addEventListener('click', function () { applyUrl(b.dataset.url, b); });
                    });
                })
                .catch(function (err) { setStatus('Error: ' + err.message, true); });
        };
        return withSpinner(btn, 'Searching…', search);
    });

    wrap.querySelector('#albumArtUrlApplyBtn').addEventListener('click', function () {
        applyUrl(wrap.querySelector('#albumArtUrlInput').value.trim(), this);
    });

    wrap.querySelector('#albumArtUploadBtn').addEventListener('click', function () {
        const input = wrap.querySelector('#albumArtFileInput');
        const file = input.files && input.files[0];
        if (!file) {
            setStatus('Choose an image file first.', true);
            return;
        }
        const btn = this;
        const upload = function () {
            setStatus('Uploading…');
            const form = new FormData();
            form.append('artist', artist);
            form.append('album', album);
            form.append('image', file);
            // Multipart: a JSON helper would break this, so raw fetch.
            return fetch('/api/album/upload-art', { method: 'POST', body: form })
                .then(function (r) {
                    return r.json().catch(function () { return {}; })
                        .then(function (data) { return { ok: r.ok, status: r.status, data: data }; });
                })
                .then(function (out) {
                    if (!out.ok || (out.data && out.data.success === false)) {
                        setStatus((out.data && out.data.error) || ('Upload failed (HTTP ' + out.status + ').'), true);
                        return;
                    }
                    if (modal) modal.hide();
                    if (typeof window.refreshAlbumPage === 'function') window.refreshAlbumPage();
                })
                .catch(function (err) { setStatus('Error: ' + err.message, true); });
        };
        return withSpinner(btn, 'Uploading…', upload);
    });

    // Tear the markup down so a later open starts clean and ids cannot go stale.
    wrap.addEventListener('hidden.bs.modal', function () { wrap.remove(); });

    if (modal) modal.show();
};

/**
 * Align the tracklist: renumber the album's tracks from the current
 * MusicBrainz comparison.
 *
 * ⚠️ THIS FUNCTION WAS MISSING ENTIRELY — the "Align" button threw
 * ReferenceError.
 *
 * There is no bulk "align" endpoint, and inventing one that rewrites files
 * would bypass the existing per-track apply path. Instead this applies the
 * track-number suggestion through the SAME endpoint the per-track Apply button
 * uses (`/api/v1/tracks/<id>/apply-mb-field`), so the DB row and the file tags
 * are updated identically and nothing new can drift.
 *
 * Track numbers are DISPLAY-ONLY cells on this page (not form inputs), so
 * writing to the DOM would have done nothing — the POST is what renumbers.
 */
window.alignTracklist = function () {
    const comparison = (window._mbComparisonData && window._mbComparisonData.comparison) || [];
    if (!comparison.length) {
        alert('Nothing to align to — run "Lookup MBID" or "Compare with MusicBrainz" first.');
        return;
    }

    const needsNumber = comparison.filter(function (c) {
        return c && c.matched && c.library_track_id && c.mb_track_number != null &&
               String(c.library_track_number == null ? '' : c.library_track_number) !== String(c.mb_track_number);
    });
    if (!needsNumber.length) {
        alert('Track numbers already match the MusicBrainz order.');
        return;
    }
    if (!confirm('Renumber ' + needsNumber.length + ' track(s) to the MusicBrainz order?\n\n' +
                 'This writes the number to the database and the audio file tags for each track.')) return;

    const run = function () {
        let applied = 0, failed = 0;
        // Sequential: each call updates the DB and rewrites file tags.
        const step = function (i) {
            if (i >= needsNumber.length) return Promise.resolve();
            const comp = needsNumber[i];
            return fetch(_API_V1_PREFIX + '/tracks/' + encodeURIComponent(String(comp.library_track_id)) + '/apply-mb-field', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ field: 'track_number', value: String(comp.mb_track_number) })
            })
                .then(function (r) { return r.json().catch(function () { return {}; }); })
                .then(function (data) {
                    if (data && data.success) {
                        applied += 1;
                        document.querySelectorAll('.track-number-display-' + CSS.escape(String(comp.library_track_id)))
                            .forEach(function (el) { el.textContent = comp.mb_track_number; });
                    } else {
                        failed += 1;
                    }
                })
                .catch(function () { failed += 1; })
                .then(function () { return step(i + 1); });
        };
        return step(0).then(function () {
            alert(applied
                ? ('Aligned ' + applied + ' track number(s)' + (failed ? '; ' + failed + ' failed.' : '.'))
                : ('Could not align any tracks (' + failed + ' failed).'));
        });
    };

    if (window.busyPopup) return window.busyPopup.showAndRun('Aligning tracklist…', run);
    return run();
};

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

    // Also raise the popup: the button spinner alone is easy to miss when the
    // row is scrolled or the click came from a dense tracklist.
    const queueBusy = window.busyPopup
        ? window.busyPopup.show('Adding to download queue…')
        : null;
    const endQueueBusy = function () {
        if (window.busyPopup) window.busyPopup.hide(queueBusy);
    };

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
                // ⚠️ A dedupe is NOT an insert. ``success: true`` with
                // ``already_queued: true`` means the request was handled but no
                // row was added — marking the button "done" for it is the same
                // false success that made added tracks look like they vanished.
                // Only a real insert earns the green tick.
                if (data.already_queued) {
                    btn.disabled = false;
                    btn.innerHTML = origHtml;
                    btn.title = data.message || 'Already in the download queue';
                    alert(data.message || 'Already in the download queue.');
                    return;
                }
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
        })
        .finally(endQueueBusy);
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
// Persisted missing tracks (loaded on page load)
// ---------------------------------------------------------------------------
/*
  ⚠️ THE ALBUM PAGE NEVER SHOWED ITS MISSING TRACKS.

  `missing_album_tracks` holds them per album, and the scan refreshes it — but
  the only thing that ever RENDERED them was `_injectMissingTrackRows`, which
  runs exclusively from the manual "Compare with MusicBrainz" result. So the
  artist page advertised "3 missing" on a row, the user opened the album to
  download them, and the album page listed nothing to click. The list existed in
  the DB the whole time.

  This loads the persisted list on page load, so those flagged tracks are
  actually selectable. It reuses `_buildMissingTrackRow`, so the per-row controls
  (queue / match / hide) are the SAME ones the Compare path builds and cannot
  drift from it.
*/

/** Shape a `missing_album_tracks` row as the `mb_*` comparison object. */
function _missingRowToTrackComp(row) {
    return {
        matched: false,
        mb_title: row.title || '',
        mb_track_number: row.track_number,
        mb_disc_number: row.disc_number != null ? row.disc_number : 1,
        mb_recording_mbid: row.recording_mbid || '',
        mb_duration: row.duration,
        // Kept for the manual match flow, which reads the track artist.
        track_artist: row.track_artist || '',
    };
}

/**
 * Append a missing row AFTER the last real track row.
 *
 * Deliberately not `_injectMissingTrackRows`: that positions each row relative
 * to `data.comparison`, and these rows are in no comparison (they come from the
 * DB, not from a Compare run). Its `indexOf` returns -1, which leaves it
 * inserting before the FIRST row every time — which REVERSES the list.
 */
function _appendMissingRow(row, tbody) {
    const rows = tbody.querySelectorAll('tr[data-track-id]');
    const last = rows.length ? rows[rows.length - 1] : null;
    if (last) last.insertAdjacentElement('afterend', row);
    else tbody.appendChild(row);
}

/** Load and render this album's persisted missing tracks. */
window.loadAlbumMissingTracks = function () {
    const tbody = document.getElementById('albumTracksTbody');
    if (!tbody) return;
    const artist = window._pageData ? window._pageData.artistName : '';
    const album = window._pageData ? window._pageData.albumName : '';
    if (!artist || !album) return;

    // Already rendered from a Compare run — do not duplicate the list.
    if (tbody.querySelector('.mb-missing-row')) return;

    fetch('/api/album/missing-tracks?artist=' + encodeURIComponent(artist)
        + '&album=' + encodeURIComponent(album))
        .then(r => (r.ok ? r.json() : {}))
        .then(data => {
            const missing = (data && data.missing_tracks) || [];
            if (!missing.length) return;

            const seen = new Set();
            missing.forEach(row => {
                const comp = _missingRowToTrackComp(row);
                // The endpoint returns distinct rows, but a duplicate title +
                // position would render two identical rows.
                const key = [comp.mb_disc_number, comp.mb_track_number,
                             String(comp.mb_title).toLowerCase()].join('\u0000');
                if (seen.has(key)) return;
                seen.add(key);

                // Context is built PER ROW: each persisted row carries its own
                // release id and year, which is more accurate than a page-level
                // guess, and is what the queue payload needs.
                const ctx = {
                    release_mbid: row.release_id || _getLinkedReleaseMbid(),
                    mb_year: row.year || '',
                };
                _appendMissingRow(_buildMissingTrackRow(comp, ctx), tbody);
            });

            // Advertise the count in the header, and reveal the
            // "search missing tracks" affordance the template hides until a
            // count is known.
            const badge = document.getElementById('albumMissingHeaderBadge');
            if (badge) {
                badge.textContent = missing.length + ' track'
                    + (missing.length === 1 ? '' : 's') + ' missing';
                badge.classList.remove('d-none');
            }
            const searchBtn = document.querySelector('.album-search-missing-btn');
            if (searchBtn) searchBtn.style.display = '';
        })
        .catch(err => {
            // Non-fatal for the PAGE (the owned tracklist still renders), but
            // deliberately LOUD: a silent swallow here is what would hide a
            // row-builder error behind an album that simply looks complete.
            console.error('Could not load missing tracks:', err);
        });
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
    // Persisted missing tracks. Fired here so a plain page load shows the same
    // flagged tracks the artist page counted, without needing a Compare run.
    if (typeof window.loadAlbumMissingTracks === 'function') {
        window.loadAlbumMissingTracks();
    }

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
