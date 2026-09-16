// ===== Artist Detail Page JS =====

// Fallback utility initializations
if (typeof toggleGenreCheckbox === 'undefined') {
    window.toggleGenreCheckbox = function(containerId, buttonId) {
        const button = document.getElementById(buttonId);
        const container = document.getElementById(containerId);
        if (!container || !button) return;
        const checkboxes = container.querySelectorAll('input[type="checkbox"]');
        const checkedBoxes = Array.from(checkboxes).filter(cb => cb.checked);
        if (checkedBoxes.length > 0) {
            button.style.display = 'inline-block';
            button.textContent = `Remove ${checkedBoxes.length} Selected Genre${checkedBoxes.length > 1 ? 's' : ''}`;
        } else {
            button.style.display = 'none';
        }
    };
}

if (typeof getSelectedGenres === 'undefined') {
    window.getSelectedGenres = function(containerId) {
        const container = document.getElementById(containerId);
        if (!container) return [];
        return Array.from(container.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
    };
}

if (typeof handleGenreRemoval === 'undefined') {
    window.handleGenreRemoval = function(artistName, albumName, genres, contextType) {
        alert(`Would remove genres: ${genres.join(', ')}`);
    };
}

// Utility and Feature Functions
window.escapeHtml = function(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
};

// Fixed Filtering Logic with Exact Visual States
window.setArtistFilter = function(filter) {
    document.querySelectorAll('.artist-filter-btn').forEach(btn => {
        if (btn.dataset.filter === filter) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    
    const mainContainer = document.getElementById('artistMainPageContainer');
    if (!mainContainer) return;

    mainContainer.classList.remove('filter-hide-missing', 'filter-hide-library');

    if (filter === 'library') {
        mainContainer.classList.add('filter-hide-missing');
    } else if (filter === 'missing') {
        mainContainer.classList.add('filter-hide-library');
    }

    document.querySelectorAll('.category-section').forEach(section => {
        const rows = Array.from(section.querySelectorAll('.album-row'));
        if (!rows.length) return;

        let hasVisible = false;
        rows.forEach(row => {
            const isMissing = row.classList.contains('missing-album-item');
            if (filter === 'all') hasVisible = true;
            if (filter === 'library' && !isMissing) hasVisible = true;
            if (filter === 'missing' && isMissing) hasVisible = true;
        });

        section.style.display = hasVisible ? '' : 'none';
    });
};

window.toggleMissingReleasesForCategory = function(btn, catId) {
    const section = document.getElementById(catId + '-section');
    if (!section) return;
    
    const isCurrentlyHidden = btn.getAttribute('data-hidden') === 'true';

    if (isCurrentlyHidden) {
        section.classList.remove('category-hide-missing');
        btn.setAttribute('data-hidden', 'false');
    } else {
        section.classList.add('category-hide-missing');
        btn.setAttribute('data-hidden', 'true');
    }
};

window.forceArtistMetadataRefresh = function() {
  const form = document.getElementById('artistScanForm');
  if (!form) return;
  const force = document.getElementById('artistForceScan');
  if (force) force.checked = true;
  form.submit();
};

window.toggleArtistBio = function() {
  const clamp = document.getElementById('artistBioClamp');
  const btn = document.getElementById('artistBioToggle');
  if (!clamp || !btn) return;
  const expanded = clamp.classList.toggle('expanded');
  btn.innerHTML = expanded
    ? '<i class="bi bi-chevron-contract me-1"></i>Read Less'
    : '<i class="bi bi-chevron-expand me-1"></i>Read More';
};

window.playArtistTopTracks = function() {
  const tracks = window._artistPlaylist || [];
  if (typeof Player === 'undefined' || typeof Player.playQueue !== 'function') {
    alert('Player unavailable');
    return;
  }
  if (!tracks.length) {
    alert('No playable tracks for this artist');
    return;
  }
  Player.playQueue(tracks);
};

window.goToArtistAbout = function() {
  var btn = document.querySelector('#artistPageTabs [data-bs-target="#tab-about"]');
  if (btn) {
    if (window.bootstrap && bootstrap.Tab) {
      bootstrap.Tab.getOrCreateInstance(btn).show();
    } else {
      btn.click();
    }
  }
};

window.loadArtistCoveredBy = async function(artistName) {
  const container = document.getElementById('artistCoveredByContainer');
  const countEl = document.getElementById('artistCoveredByCount');
  if (!container) return;

  try {
    const resp = await fetch('/api/artist/covered-by?artist=' + encodeURIComponent(artistName));
    const data = await resp.json();
    const covers = data.covers || [];

    if (covers.length === 0) {
      container.innerHTML = `<div class="p-3 text-muted text-center"><i class="bi bi-info-circle"></i> No covers found in library.</div>`;
      if (countEl) countEl.textContent = 'No covers found';
      return;
    }

    if (countEl) countEl.textContent = `${covers.length} cover(s) found`;
    let html = '<div class="table-responsive"><table class="table table-hover mb-0"><thead><tr><th>Covering Artist</th><th>Song Title</th><th>Album</th><th class="text-center">Year</th></tr></thead><tbody>';
    
    covers.forEach(cover => {
      html += `<tr>
        <td>${window.escapeHtml(cover.artist)}</td>
        <td>${window.escapeHtml(cover.title)}</td>
        <td>${window.escapeHtml(cover.album || '—')}</td>
        <td class="text-center">${cover.year || '—'}</td>
      </tr>`;
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `<div class="p-3 text-danger text-center">Error loading covers</div>`;
  }
};

window.editArtistCountry = function() {
  const artistName = window._pd ? window._pd.artistName : (window.artistName || '');
  const badgeEl = document.getElementById('artistCountryBadgeDisplay');
  const currentCountry = badgeEl ? badgeEl.textContent.trim() : '';
  
  const modalHtml = `
    <div class="modal fade" id="editCountryModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content bg-dark text-light border-secondary">
          <div class="modal-header border-secondary">
            <h5 class="modal-title"><i class="bi bi-pencil"></i> Edit Artist Country</h5>
            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
              <label for="countryInput" class="form-label">Country/Origin</label>
              <input type="text" class="form-control bg-dark text-light border-secondary" id="countryInput" value="${window.escapeHtml(currentCountry === 'Unknown Origin' ? '' : currentCountry)}">
            </div>
          </div>
          <div class="modal-footer border-secondary">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
            <button type="button" class="btn btn-primary" onclick="saveArtistCountry()">Save</button>
          </div>
        </div>
      </div>
    </div>
  `;
  const existing = document.getElementById('editCountryModal');
  if (existing) existing.remove();
  document.body.insertAdjacentHTML('beforeend', modalHtml);
  
  if (typeof bootstrap !== 'undefined') {
      new bootstrap.Modal(document.getElementById('editCountryModal')).show();
  }
};

window.saveArtistCountry = function() {
  const artistName = window._pd ? window._pd.artistName : (window.artistName || '');
  const country = document.getElementById('countryInput').value.trim();
  fetch('/api/artist/country/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist_name: artistName, country: country })
  })
  .then(r => r.json())
  .then(data => {
    if (data.success) {
      if (typeof bootstrap !== 'undefined') {
          bootstrap.Modal.getInstance(document.getElementById('editCountryModal'))?.hide();
      }
      const badgeEl = document.getElementById('artistCountryBadgeDisplay');
      if (badgeEl) badgeEl.textContent = country;
      alert('✅ Country updated successfully!');
    } else {
      alert('❌ Error: ' + (data.error || 'Failed'));
    }
  })
  .catch(err => alert('❌ Network error: ' + err.message));
};

window.fetchArtistCountry = function() {
  const artistName = window._pd ? window._pd.artistName : (window.artistName || '');
  const btn = document.getElementById('fetchArtistCountryBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Fetching...';
  
  fetch('/api/artist/country', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist_name: artistName })
  })
  .then(r => r.json())
  .then(data => {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get from MusicBrainz';
    if (data.success && data.country) {
      const badgeEl = document.getElementById('artistCountryBadgeDisplay');
      if (badgeEl) badgeEl.textContent = data.country;
      alert('✅ ' + data.message);
    } else {
      alert('❌ ' + (data.error || 'No country found'));
    }
  })
  .catch(err => {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get from MusicBrainz';
    alert('❌ Network error: ' + err.message);
  });
};

// ===== Artist IDs Modal Functions =====
window.openEditArtistIdsModal = function() {
  const artistName = window._pd ? window._pd.artistName : (window.artistName || '');
  const mbEl = document.getElementById('musicbrainzArtistId');
  const dcEl = document.getElementById('discogsArtistId');
  const musicbrainzId = mbEl ? mbEl.textContent.trim() : '';
  const discogsId = dcEl ? dcEl.textContent.trim() : '';
  
  const modalHtml = `
    <div class="modal fade" id="editArtistIdsModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content bg-dark text-light border-secondary">
          <div class="modal-header border-secondary">
            <h5 class="modal-title"><i class="bi bi-pencil"></i> Edit Artist IDs</h5>
            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
              <label for="editMusicbrainzArtistId" class="form-label">MusicBrainz Artist ID</label>
              <input type="text" class="form-control bg-dark text-light border-secondary" id="editMusicbrainzArtistId" value="${window.escapeHtml(musicbrainzId === 'Not linked' ? '' : musicbrainzId)}" placeholder="e.g., a74b1b7f-71a5-4011-9441-d0b5e4122711">
              <div class="form-text">
                <a href="https://musicbrainz.org/search?query=${encodeURIComponent(artistName)}&type=artist" target="_blank">Search MusicBrainz</a> to find the artist ID
              </div>
            </div>
            <div class="mb-3">
              <label for="editDiscogsArtistId" class="form-label">Discogs Artist ID</label>
              <input type="text" class="form-control bg-dark text-light border-secondary" id="editDiscogsArtistId" value="${window.escapeHtml(discogsId === 'Not linked' ? '' : discogsId)}" placeholder="e.g., 123456">
              <div class="form-text">
                <a href="https://www.discogs.com/search/?q=${encodeURIComponent(artistName)}&type=artist" target="_blank">Search Discogs</a> to find the artist ID
              </div>
            </div>
          </div>
          <div class="modal-footer border-secondary">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
            <button type="button" class="btn btn-outline-info" onclick="window.lookupAndSaveArtistIds(this)">
              <i class="bi bi-cloud-download"></i> Lookup and Save
            </button>
            <button type="button" class="btn btn-primary" onclick="window.saveArtistIds()">
              <i class="bi bi-save"></i> Save Changes
            </button>
          </div>
        </div>
      </div>
    </div>
  `;
  
  const existingModal = document.getElementById('editArtistIdsModal');
  if (existingModal) existingModal.remove();
  
  document.body.insertAdjacentHTML('beforeend', modalHtml);
  
  if (typeof bootstrap !== 'undefined') {
      const modal = new bootstrap.Modal(document.getElementById('editArtistIdsModal'));
      modal.show();
  } else {
      console.error("Bootstrap is missing. Cannot open modal.");
  }
};

window.saveArtistIds = function() {
  const artistName = window._pd ? window._pd.artistName : (window.artistName || '');
  const musicbrainzId = document.getElementById('editMusicbrainzArtistId').value.trim();
  const discogsId = document.getElementById('editDiscogsArtistId').value.trim();
  const saveBtn = document.querySelector('#editArtistIdsModal .btn.btn-primary');
  const originalBtnHtml = saveBtn ? saveBtn.innerHTML : null;
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" style="width:0.8rem;height:0.8rem;"></span> Saving...';
  }
  
  fetch('/api/artist/update-ids', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      artist: artistName,
      lastfm_artist_mbid: musicbrainzId,
      musicbrainz_artist_id: musicbrainzId,
      discogs_artist_id: discogsId
    })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        const mbEl = document.getElementById('musicbrainzArtistId');
        const dcEl = document.getElementById('discogsArtistId');
        if (mbEl) mbEl.textContent = musicbrainzId || 'Not linked';
        if (dcEl) dcEl.textContent = discogsId || 'Not linked';
        
        if (typeof bootstrap !== 'undefined') {
            const modalInstance = bootstrap.Modal.getInstance(document.getElementById('editArtistIdsModal'));
            if (modalInstance) modalInstance.hide();
        }
        
        alert('✅ Artist IDs updated successfully!');
        setTimeout(() => location.reload(), 1000);
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to update IDs'));
      }
    })
    .catch(err => {
      console.error('Error:', err);
      alert('❌ Network error: ' + err.message);
    })
    .finally(() => {
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.innerHTML = originalBtnHtml;
      }
    });
};

window.lookupAndSaveArtistIds = function(button) {
  const artistName = window._pd ? window._pd.artistName : (window.artistName || '');
  const originalHtml = button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" style="width:0.8rem;height:0.8rem;"></span> Looking up...';

  fetch('/api/artist/lookup-ids', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artistName })
  })
    .then(r => r.json())
    .then(data => {
      if (!data.success) {
        alert('❌ Error: ' + (data.error || 'Lookup failed'));
        return;
      }

      const mbid = data.musicbrainz_artist_id || '';
      const discogs = data.discogs_artist_id || '';

      const editMb = document.getElementById('editMusicbrainzArtistId');
      const editDc = document.getElementById('editDiscogsArtistId');
      if (editMb && mbid) editMb.value = mbid;
      if (editDc && discogs) editDc.value = discogs;

      const readonlyMb = document.getElementById('musicbrainzArtistId');
      const readonlyDc = document.getElementById('discogsArtistId');
      if (readonlyMb && mbid) readonlyMb.textContent = mbid;
      if (readonlyDc && discogs) readonlyDc.textContent = discogs;

      alert('✅ Lookup complete and IDs saved.');
    })
    .catch(err => {
      alert('❌ Network error: ' + err.message);
    })
    .finally(() => {
      button.disabled = false;
      button.innerHTML = originalHtml;
    });
};

// ---------------------------------------------------------------------------
// Import Release from Missing Grid
// ---------------------------------------------------------------------------
window.importReleaseFromEncoded = function(btn, encArtist, encReleaseId, encAlbum) {
    const artist = decodeURIComponent(encArtist);
    const releaseId = decodeURIComponent(encReleaseId);
    const album = decodeURIComponent(encAlbum);

    const origHtml = btn.innerHTML;
    
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';

    fetch('/api/queue/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            artist: artist,
            album: album,
            title: album,
            release_mbid: releaseId,
            release_id: releaseId,
            release_source: releaseId ? 'musicbrainz' : null,
            source: 'musicbrainz',
            type: 'album'
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            btn.innerHTML = '<i class="bi bi-check-lg"></i> <span class="d-none d-sm-inline ms-1">Queued</span>';
            btn.classList.remove('btn-outline-success');
            btn.classList.add('btn-success');
            btn.title = 'Added to download queue';
        } else {
            btn.disabled = false;
            btn.innerHTML = origHtml;
            alert('Failed to queue album: ' + (data.error || 'Unknown error'));
        }
    })
    .catch(err => {
        btn.disabled = false;
        btn.innerHTML = origHtml;
        alert('Network error: ' + err.message);
    });
};
