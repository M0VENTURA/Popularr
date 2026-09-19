const SLSKD_MAX_POLL_ATTEMPTS = 60;
const SLSKD_POLL_INTERVAL_MS = 2000;
const BYTES_TO_MB = 1024 * 1024;

const getArtistName = () => document.querySelector('[data-artist-name]')?.dataset.artistName || '';

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function escapeJsString(str) {
  if (!str) return '';
  return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"').replace(/\n/g, '\\n').replace(/\r/g, '\\r');
}

function formatBytes(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}

document.addEventListener('DOMContentLoaded', () => {
  const artistName = getArtistName();
  if (artistName) {
    loadArtistBio(artistName);
    loadSinglesCount(artistName);
    loadSimilarArtists(artistName);
    loadArtistFavouriteState(artistName);
    loadArtistCoveredBy(artistName);
  }

  const qbitInput = document.getElementById('qbitSearchInput');
  if (qbitInput) qbitInput.addEventListener('keypress', e => { if (e.key === 'Enter') performQbitSearch(); });

  const slskdInput = document.getElementById('slskdSearchInput');
  if (slskdInput) slskdInput.addEventListener('keypress', e => { if (e.key === 'Enter') performSlskdSearch(); });
  
  initCorrectionsAndMissingTracks();
});

// Bio
function loadArtistBio(artist) {
  const bioContainer = document.getElementById('artistBio');
  if (!bioContainer) return;
  fetch(`/api/artist/bio?name=${encodeURIComponent(artist)}`).then(r => r.json()).then(data => {
    if (data.bio) bioContainer.innerHTML = `<p>${escapeHtml(data.bio)}</p>`;
    else bioContainer.innerHTML = '<p class="text-muted"><em>No biography available.</em></p>';
  });
}

function loadSinglesCount(artist) {
  fetch(`/api/artist/singles-count?name=${encodeURIComponent(artist)}`).then(r => r.json()).then(data => {
    const badge = document.getElementById('singlesCount');
    if (badge && data.count !== undefined) badge.textContent = data.count;
  });
}

// Tracklist Loader
window.artistPage = window.artistPage || {};
window.artistPage.loadTracklist = function(artist, album, button = null, mbid = null) {
  const safeArtist = artist.replace(/\s+/g, '_').replace(/[^\w\-]/g, '_');
  const safeAlbum = album.replace(/\s+/g, '_').replace(/[^\w\-]/g, '_');
  const contentId = `tracklist-content-${safeArtist}-${safeAlbum}`;
  const contentDiv = document.getElementById(contentId);
  
  if (!contentDiv) return;
  contentDiv.innerHTML = '<div class="text-center py-2"><span class="spinner-border spinner-border-sm"></span> Loading tracks...</div>';
  
  fetch(`/api/album/tracklist?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}&mbid=${encodeURIComponent(mbid || '')}`)
    .then(r => r.json())
    .then(data => {
      if (data.tracklist && data.tracklist.length > 0) {
        let html = `<div class="list-group list-group-flush small">`;
        data.tracklist.forEach(track => {
          html += `<div class="list-group-item d-flex justify-content-between text-muted bg-transparent border-secondary">
            <span>${escapeHtml(track.position || '-')} . ${escapeHtml(track.title)}</span>
          </div>`;
        });
        html += `</div>`;
        contentDiv.innerHTML = html;
      } else {
        contentDiv.innerHTML = '<div class="text-muted small">No tracks found.</div>';
      }
    }).catch(err => {
      contentDiv.innerHTML = `<div class="text-danger small">Error loading tracks.</div>`;
    });
};

window.artistPage.toggleTracklist = function(artist, album) {
  const safeArtist = artist.replace(/\s+/g, '_').replace(/[^\w\-]/g, '_');
  const safeAlbum = album.replace(/\s+/g, '_').replace(/[^\w\-]/g, '_');
  const rowId = `tracklist-${safeArtist}-${safeAlbum}`;
  const row = document.getElementById(rowId);
  
  if (row) {
    const isHidden = row.style.display === 'none';
    row.style.display = isHidden ? '' : 'none';
    if (isHidden) {
      window.artistPage.loadTracklist(artist, album);
    }
  }
};

// Search & Downloads
let currentDownloadAlbum = { artist: null, album: null };
function openDownloadSearch(artistName, albumName) {
  currentDownloadAlbum = { artist: artistName, album: albumName };
  document.getElementById('downloadArtistName').textContent = artistName + (albumName ? ' - ' + albumName : '');
  const query = albumName ? artistName + ' ' + albumName : artistName;
  const qbitInput = document.getElementById('qbitSearchInput');
  if (qbitInput) { qbitInput.value = query; document.getElementById('qbitResults').innerHTML = ''; }
  const slskdInput = document.getElementById('slskdSearchInput');
  if (slskdInput) { slskdInput.value = query; document.getElementById('slskdResults').innerHTML = ''; }
  new bootstrap.Modal(document.getElementById('downloadModal')).show();
}

function performQbitSearch() {
  const query = document.getElementById('qbitSearchInput').value;
  if (!query) return;
  document.getElementById('qbitLoading').style.display = 'block';
  fetch('/api/qbittorrent/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query }) })
  .then(r => r.json()).then(data => {
    document.getElementById('qbitLoading').style.display = 'none';
    if (!data.results || data.results.length === 0) { document.getElementById('qbitResults').innerHTML = 'No results.'; return; }
    let html = `<table class="table table-sm text-light"><tbody>`;
    data.results.forEach(res => {
      html += `<tr><td>${escapeHtml(res.fileName)}</td><td>${formatBytes(res.fileSize)}</td><td><button class="btn btn-sm btn-success" onclick="addTorrent('${escapeJsString(res.fileUrl)}')">Add</button></td></tr>`;
    });
    html += `</tbody></table>`;
    document.getElementById('qbitResults').innerHTML = html;
  });
}

function addTorrent(url) {
  fetch('/api/qbittorrent/add', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) })
  .then(r => r.json()).then(data => alert(data.success ? 'Added!' : 'Failed.'));
}

// Artist IDs Modal
function openEditArtistIdsModal() {
  new bootstrap.Modal(document.getElementById('editArtistIdsModal')).show();
}

function saveArtistIds() {
  const artist = getArtistName();
  const musicbrainz_artist_id = document.getElementById('editMusicbrainzArtistId').value.trim();
  const discogs_artist_id = document.getElementById('editDiscogsArtistId').value.trim();
  fetch('/api/artist/update-ids', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ artist, musicbrainz_artist_id, discogs_artist_id }) })
  .then(r => r.json()).then(data => { if (data.success) location.reload(); });
}

// Global MusicBrainz Search
window.artistPage.searchAllReleases = function(artist) {
  new bootstrap.Modal(document.getElementById('artistMbSearchModal')).show();
  document.getElementById('artistMbSearchArtist').textContent = artist;
  document.getElementById('artistMbSearchStatus').style.display = 'block';
  fetch('/api/musicbrainz/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: artist, artist_only: true }) })
  .then(r => r.json()).then(data => {
    document.getElementById('artistMbSearchStatus').style.display = 'none';
    let html = '<div class="list-group">';
    (data.releases || []).forEach(rel => {
      html += `<div class="list-group-item d-flex justify-content-between align-items-center bg-transparent border-secondary text-light">
        <span>${escapeHtml(rel.title)}</span>
        <button class="btn btn-sm btn-success" onclick="openDownloadSearch('${escapeJsString(artist)}', '${escapeJsString(rel.title)}')"><i class="bi bi-download"></i></button>
      </div>`;
    });
    document.getElementById('artistMbSearchResults').innerHTML = html + '</div>';
  });
};
window.searchMusicBrainzForAllReleases = window.artistPage.searchAllReleases;

// Misc Profile Data
async function loadSimilarArtists(artist) {
  fetch(`/api/artist/${encodeURIComponent(artist)}/similar`).then(r => r.json()).then(data => {
    const container = document.getElementById('artistSimilarArtistsContainer');
    if (!data.similar_artists) return;
    let html = `<div class="d-flex flex-wrap gap-2">`;
    data.similar_artists.lastfm.filter(a => a.in_collection).forEach(a => { html += `<a href="/artist/${encodeURIComponent(a.name)}" class="badge bg-success text-decoration-none">${escapeHtml(a.name)}</a>`; });
    container.innerHTML = html + `</div>`;
  });
}

async function loadArtistFavouriteState(artist) {
  fetch(`/api/artist/favourite?artist=${encodeURIComponent(artist)}`).then(r => r.json()).then(data => {
    if (data.is_favourite) document.getElementById('artistFavouriteBtn')?.classList.add('btn-danger');
  });
}

async function toggleArtistFavourite(artist) {
  await fetch('/api/artist/favourite', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ artist }) });
  location.reload();
}

async function loadArtistCoveredBy(artist) {
  fetch(`/api/artist/covered-by?artist=${encodeURIComponent(artist)}`).then(r => r.json()).then(data => {
    if (data.covers && data.covers.length > 0) {
      document.getElementById('artistCoveredByContainer').innerHTML = `<p class="p-3 mb-0">${data.covers.length} covers in library.</p>`;
    } else {
      document.getElementById('artistCoveredByContainer').innerHTML = `<p class="p-3 mb-0 text-muted">No covers found.</p>`;
    }
  });
}

function initCorrectionsAndMissingTracks() {
  const artist = getArtistName();
  if (!artist) return;
  
  const mbRows = document.querySelectorAll('.release-item[data-mbid]');
  mbRows.forEach(row => {
    const albumName = row.dataset.title;
    if (!albumName) return;
    fetch(`/api/album/missing-tracks?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(albumName)}`)
      .then(r => r.json()).then(data => {
        if (data.missing_count > 0) {
          const badge = row.querySelector('.album-missing-tracks-badge');
          if (badge) {
            badge.textContent = `${data.missing_count} tracks missing`;
            badge.style.display = 'inline-block';
          }
        }
      });
  });
}

function openEditTrackFromArtistModal(trackId, title) {
  document.getElementById('editArtistTrackId').value = trackId;
  document.getElementById('editArtistTrackTitleField').value = title;
  new bootstrap.Modal(document.getElementById('editTrackFromArtistModal')).show();
}

function saveArtistEditedTrack() {
  const payload = {
    track_id: document.getElementById('editArtistTrackId').value,
    title: document.getElementById('editArtistTrackTitleField').value.trim(),
    sync_to_file: true
  };
  fetch('/api/track/update-metadata', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) })
  .then(r => r.json()).then(data => { if (data.success) location.reload(); });
}
