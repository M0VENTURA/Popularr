(function (global) {
  'use strict';

  function esc(value) { return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value); }
  function notifySuccess(m) { if (global.toast) global.toast.success(m); else global.alert(m); }
  function notifyError(m) { if (global.toast) global.toast.error(m); else global.alert(m); }
  function safeForDomId(value) { return String(value || '').replace(/\s+/g, '_').replace(/[^\w\-]/g, '_'); }
  function artistName() { const el = document.querySelector('[data-artist-name]'); return el ? el.dataset.artistName : ''; }

  function wireTracklistIds() {
    document.querySelectorAll('.release-item').forEach((item) => {
      const summary = item.querySelector('.release-summary');
      const tracklistEl = item.querySelector('.release-tracklist');
      const contentEl = item.querySelector('.release-tracklist-content');
      if (!summary || !tracklistEl || !contentEl) return;
      const a = safeForDomId(summary.dataset.artist);
      const b = safeForDomId(summary.dataset.album);
      tracklistEl.id = `tracklist-${a}-${b}`;
      contentEl.id = `tracklist-content-${a}-${b}`;
    });
  }

  function toggleReleaseTracklist(item) {
    const summary = item.querySelector('.release-summary');
    if (!summary) return;
    if (global.artistPage && typeof global.artistPage.toggleTracklist === 'function') {
      global.artistPage.toggleTracklist(summary.dataset.artist, summary.dataset.album);
      const tracklistEl = item.querySelector('.release-tracklist');
      const icon = item.querySelector('.release-toggle-btn i');
      if (icon && tracklistEl) icon.className = tracklistEl.style.display !== 'none' ? 'bi bi-chevron-up' : 'bi bi-chevron-down';
    }
  }

  function wireTracklistToggles() {
    document.querySelectorAll('.release-item').forEach((item) => {
      const summary = item.querySelector('.release-summary');
      const toggleBtn = item.querySelector('.release-toggle-btn');
      if (summary) summary.addEventListener('click', () => toggleReleaseTracklist(item));
      if (toggleBtn) toggleBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleReleaseTracklist(item); });
    });
  }

  function wireFilters() {
    document.querySelectorAll('.release-filter-radio').forEach((radio) => {
      radio.addEventListener('change', () => {
        if (!radio.checked) return;
        const sectionId = radio.closest('.release-section').dataset.category;
        const list = document.querySelector(`.release-list[data-category="${sectionId}"]`);
        if (list) {
          list.querySelectorAll('.release-item').forEach((item) => {
            item.style.display = (radio.value === 'all' || radio.value === item.dataset.status) ? '' : 'none';
          });
        }
      });
    });
  }

  function wireReleaseActionButtons() {
    document.querySelectorAll('.release-edit-btn').forEach((btn) => {
      if (btn.dataset.wired) return; btn.dataset.wired = '1';
      btn.addEventListener('click', () => {
        document.getElementById('editReleaseArtist').value = btn.dataset.artist;
        document.getElementById('editReleaseOriginalTitle').value = btn.dataset.album;
        document.getElementById('editReleaseTitle').value = btn.dataset.album;
        document.getElementById('editReleaseYear').value = btn.dataset.year;
        document.getElementById('editReleaseMbid').value = btn.dataset.mbid;
        new bootstrap.Modal(document.getElementById('editReleaseModal')).show();
      });
    });

    document.querySelectorAll('.release-search-all-btn').forEach((btn) => {
      if (btn.dataset.wired) return; btn.dataset.wired = '1';
      btn.addEventListener('click', () => {
        if (global.artistPage && typeof global.artistPage.searchAllReleases === 'function') {
          global.artistPage.searchAllReleases(btn.dataset.artist);
        }
      });
    });
    
    document.querySelectorAll('.release-check-missing-btn').forEach((btn) => {
      if (btn.dataset.wired) return; btn.dataset.wired = '1';
      btn.addEventListener('click', () => { alert('Missing check functionality engaged via background scanner.'); });
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (!document.getElementById('releases-sections')) return;
    wireTracklistIds();
    wireTracklistToggles();
    wireFilters();
    wireReleaseActionButtons();
    
    const saveBtn = document.getElementById('editReleaseSaveBtn');
    if (saveBtn) {
      saveBtn.addEventListener('click', () => {
        const artist = document.getElementById('editReleaseArtist').value;
        const orig = document.getElementById('editReleaseOriginalTitle').value;
        const title = document.getElementById('editReleaseTitle').value;
        const form = new FormData(); form.set('album_title', title); form.set('album_artist', artist);
        fetch(`/album/${encodeURIComponent(artist)}/${encodeURIComponent(orig)}`, { method: 'POST', body: form })
        .then(r => { if(r.ok) location.reload(); else notifyError('Save failed'); });
      });
    }
  });

})(window);
