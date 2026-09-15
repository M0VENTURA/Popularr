// ===== Album Detail Page JS =====
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
// its real home — see the <script> tags in album_detail.html.

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
        window.location.href = `/track/${trackId}/edit`;
    }
};

window.deleteTrack = function (trackId) {
    if (!confirm('Are you sure you want to delete this track?')) return;
    // Fallback directly to the server's delete route view
    window.location.href = `/track/${trackId}/delete`;
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
// The value now arrives directly from the shared modal's selection callback.
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
