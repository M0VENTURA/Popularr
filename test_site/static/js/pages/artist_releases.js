/**
 * pages/artist-releases.js
 * ─────────────────────────
 * Controller for the redesigned "Releases" area of the artist page.
 */
(function (global) {
  'use strict';

  function esc(value) {
    return (global.escapeHtml || ((v) => String(v == null ? '' : v)))(value);
  }

  function notifyError(message) {
    if (global.toast) global.toast.error(message);
    else global.alert(message);
  }

  function notifySuccess(message) {
    if (global.toast) global.toast.success(message);
    else global.alert(message);
  }

  function confirmFn(opts) {
    if (global.ui && global.ui.confirm) return global.ui.confirm(opts);
    const parts = [opts.message];
    if (opts.detail) parts.push(opts.detail);
    return Promise.resolve(global.confirm(parts.join('\n\n')));
  }

  function withBusy(btn, label, fn) {
    if (btn && global.buttonState) return global.buttonState.withBusy(btn, label, fn);
    return fn();
  }

  function safeForDomId(value) {
    return String(value || '').replace(/\s+/g, '_').replace(/[^\w\-]/g, '_');
  }

  function tracklistIds(artist, album) {
    const a = safeForDomId(artist);
    const b = safeForDomId(album);
    return { rowId: `tracklist-${a}-${b}`, contentId: `tracklist-content-${a}-${b}` };
  }

  function artistName() {
    const el = document.querySelector('[data-artist-name]');
    return el ? (el.dataset.artistName || '') : '';
  }

  function wireTracklistIds() {
    document.querySelectorAll('.release-item').forEach((item) => {
      const summary = item.querySelector('.release-summary');
      const tracklistEl = item.querySelector('.release-tracklist');
      const contentEl = item.querySelector('.release-tracklist-content');
      if (!summary || !tracklistEl || !contentEl) return;

      const artist = summary.dataset.artist || '';
      const album = summary.dataset.album || '';
      const { rowId, contentId } = tracklistIds(artist, album);
      tracklistEl.id = rowId;
      contentEl.id = contentId;
    });
  }

  function setToggleIcon(item, expanded) {
    const icon = item.querySelector('.release-toggle-btn i');
    if (icon) icon.className = expanded ? 'bi bi-chevron-up' : 'bi bi-chevron-down';
    const summary = item.querySelector('.release-summary');
    if (summary) summary.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  }

  function toggleReleaseTracklist(item) {
    const summary = item.querySelector('.release-summary');
    const tracklistEl = item.querySelector('.release-tracklist');
    if (!summary || !tracklistEl) return;

    const artist = summary.dataset.artist || '';
    const album = summary.dataset.album || '';
    const mbid = summary.dataset.mbid || null;

    if (global.artistPage && typeof global.artistPage.toggleTracklist === 'function') {
      global.artistPage.toggleTracklist(artist, album);
      setToggleIcon(item, tracklistEl.style.display !== 'none');
      return;
    }

    const isHidden = tracklistEl.style.display === 'none';
    tracklistEl.style.display = isHidden ? '' : 'none';
    setToggleIcon(item, isHidden);
    if (!isHidden) return;

    const contentEl = item.querySelector('.release-tracklist-content');
    if (!contentEl || contentEl.innerHTML.trim() !== '') return;
    if (global.artistPage && typeof global.artistPage.loadTracklist === 'function') {
      global.artistPage.loadTracklist(artist, album, null, mbid);
    }
  }

  function wireTracklistToggles() {
    document.querySelectorAll('.release-item').forEach((item) => {
      const summary = item.querySelector('.release-summary');
      const toggleBtn = item.querySelector('.release-toggle-btn');
      if (summary) {
        summary.addEventListener('click', () => toggleReleaseTracklist(item));
        summary.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            toggleReleaseTracklist(item);
          }
        });
      }
      if (toggleBtn) {
        toggleBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          toggleReleaseTracklist(item);
        });
      }
    });
  }

  function getCollapseInstance(bodyEl) {
    if (!global.bootstrap || !global.bootstrap.Collapse) return null;
    return global.bootstrap.Collapse.getOrCreateInstance(bodyEl, { toggle: false });
  }

  function setSectionCollapsed(sectionId, collapsed) {
    const body = document.getElementById(sectionId + '-body');
    const toggleBtn = document.querySelector(
      `.release-section-toggle[data-bs-target="#${sectionId}-body"]`
    );
    if (!body) return;
    const inst = getCollapseInstance(body);
    if (inst) {
      collapsed ? inst.hide() : inst.show();
    } else {
      body.classList.toggle('show', !collapsed);
    }
    if (toggleBtn) toggleBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  }

  function persistSectionState(sectionId, collapsed) {
    try {
      localStorage.setItem('releaseSection-collapsed-' + sectionId, collapsed ? '1' : '0');
    } catch (_e) { }
  }

  function restoreSectionStates() {
    document.querySelectorAll('.release-section-body').forEach((body) => {
      const sectionId = body.id.replace(/-body$/, '');
      let collapsed = false;
      try {
        collapsed = localStorage.getItem('releaseSection-collapsed-' + sectionId) === '1';
      } catch (_e) { }
      if (collapsed) setSectionCollapsed(sectionId, true);
    });
  }

  function wireSectionCollapse() {
    document.querySelectorAll('.release-section-body').forEach((body) => {
      const sectionId = body.id.replace(/-body$/, '');
      body.addEventListener('shown.bs.collapse', () => persistSectionState(sectionId, false));
      body.addEventListener('hidden.bs.collapse', () => persistSectionState(sectionId, true));
    });

    const expandAllBtn = document.getElementById('releasesExpandAllBtn');
    const collapseAllBtn = document.getElementById('releasesCollapseAllBtn');
    if (expandAllBtn) {
      expandAllBtn.addEventListener('click', () => {
        document.querySelectorAll('.release-section-body').forEach((body) => {
          setSectionCollapsed(body.id.replace(/-body$/, ''), false);
        });
      });
    }
    if (collapseAllBtn) {
      collapseAllBtn.addEventListener('click', () => {
        document.querySelectorAll('.release-section-body').forEach((body) => {
          setSectionCollapsed(body.id.replace(/-body$/, ''), true);
        });
      });
    }
  }

  function wireJumpChips() {
    document.querySelectorAll('.release-jump-chip').forEach((chip) => {
      chip.addEventListener('click', (e) => {
        const sectionId = chip.dataset.section;
        if (!sectionId) return;
        setSectionCollapsed(sectionId, false);
      });
    });
  }

  function applyFilter(sectionId, value) {
    const list = document.querySelector(`.release-list[data-category="${sectionId}"]`);
    if (!list) return;
    list.querySelectorAll('.release-item').forEach((item) => {
      const status = item.dataset.status;
      const show = value === 'all' || value === status;
      item.style.display = show ? '' : 'none';
    });
  }

  function persistFilter(sectionId, value) {
    try {
      localStorage.setItem('releaseFilter-' + sectionId, value);
    } catch (_e) { }
  }

  function wireFilters() {
    document.querySelectorAll('.release-filter-radio').forEach((radio) => {
      radio.addEventListener('change', () => {
        if (!radio.checked) return;
        const sectionId = radio.closest('.release-section').dataset.category;
        applyFilter(sectionId, radio.value);
        persistFilter(sectionId, radio.value);
      });
    });

    document.querySelectorAll('.release-section').forEach((section) => {
      const sectionId = section.dataset.category;
      let value = 'all';
      try {
        value = localStorage.getItem('releaseFilter-' + sectionId) || 'all';
      } catch (_e) { }
      const radio = section.querySelector(`.release-filter-radio[value="${value}"]`);
      if (radio) radio.checked = true;
      applyFilter(sectionId, value);
    });
  }

  function buildReleaseItemEl(artist, item, sectionId) {
    const year = (item.first_release_date || '').slice(0, 4) || '';
    const fallbackArt = '/api/album-art-placeholder';
    const artUrl = item.cover_art_url || fallbackArt;

    const el = document.createElement('div');
    el.className = 'release-item';
    el.dataset.status = 'missing';
    el.dataset.year = year || '0';
    el.dataset.title = item.title || '';
    el.dataset.mbid = item.id || '';
    el.dataset.source = 'live-missing';

    el.innerHTML = `
      <div class="release-summary" role="button" tabindex="0" aria-expanded="false"
           data-artist="${esc(artist)}" data-album="${esc(item.title)}" data-mbid="${esc(item.id || '')}">
        <img class="release-art" src="${esc(artUrl)}" alt="${esc(item.title)}" loading="lazy">
        <div class="release-meta">
          <div class="release-title-row">
            <span class="release-title">${esc(item.title)}</span>
            <span class="badge bg-warning text-dark release-status-badge"><i class="bi bi-cloud"></i> Missing</span>
          </div>
          <div class="release-sub text-muted small">${esc(year || '—')}</div>
        </div>
        <div class="release-actions" onclick="event.stopPropagation();">
          <button type="button" class="btn btn-sm btn-outline-success release-import-btn"
                  data-artist="${esc(artist)}" data-album="${esc(item.title)}" data-mbid="${esc(item.id || '')}"
                  title="Download / import this release"><i class="bi bi-download"></i></button>
          <button type="button" class="btn btn-sm btn-outline-info release-mb-search-btn"
                  data-artist="${esc(artist)}" data-album="${esc(item.title)}"
                  title="Search MusicBrainz"><i class="bi bi-search"></i></button>
          <button type="button" class="btn btn-sm btn-outline-info release-toggle-btn" title="Show tracklist">
            <i class="bi bi-chevron-down"></i>
          </button>
        </div>
      </div>
      <div class="release-tracklist" style="display:none;">
        <div class="release-tracklist-content pt-2"></div>
      </div>`;

    const img = el.querySelector('.release-art');
    img.addEventListener('error', function handle() {
      this.removeEventListener('error', handle);
      this.src = fallbackArt;
    });

    return el;
  }

  function rowExists(list, title) {
    const wanted = String(title || '').trim().toLowerCase();
    return Array.from(list.querySelectorAll('.release-item')).some((el) =>
      String(el.dataset.title || '').trim().toLowerCase() === wanted
    );
  }

  function sortListByYear(list) {
    const items = Array.from(list.querySelectorAll('.release-item'));
    items.sort((a, b) => (parseInt(b.dataset.year || '0', 10)) - (parseInt(a.dataset.year || '0', 10)));
    items.forEach((el) => list.appendChild(el));
  }

  const CATEGORY_TO_SECTION = {
    album: 'albums',
    live_album: 'live-albums',
    remix_album: 'remix-albums',
    ep: 'eps',
    single: 'singles',
    compilation: 'compilations',
  };

  function missingCategory(item) {
    const raw = String(item.category || item.primary_type || 'album').toLowerCase();
    if (raw.includes('compilation')) return 'compilation';
    if (raw.includes('live')) return 'live_album';
    if (raw.includes('remix')) return 'remix_album';
    if (raw.includes('single')) return 'single';
    if (raw.includes('ep')) return 'ep';

    const title = String(item.title || '').toLowerCase();
    if (title.includes('live') || title.includes('unplugged')) return 'live_album';
    if (title.includes('remix')) return 'remix_album';
    return 'album';
  }

  async function checkMissingForSection(artist, sectionId, btn) {
    return withBusy(btn, 'Checking…', async () => {
      try {
        const data = await global.api.getJson(
          '/api/artist/missing-releases?artist=' + encodeURIComponent(artist)
        );
        if (data.error) throw new Error(data.error);

        document.querySelectorAll('.release-item[data-source="live-missing"]').forEach((el) => el.remove());

        let added = 0;
        const touchedSections = new Set();
        (data.missing || []).forEach((item) => {
          const targetSection = CATEGORY_TO_SECTION[missingCategory(item)] || 'albums';
          const list = document.querySelector(`.release-list[data-category="${targetSection}"]`);
          if (!list || rowExists(list, item.title)) return;

          list.appendChild(buildReleaseItemEl(artist, item, targetSection));
          touchedSections.add(targetSection);
          added += 1;
        });

        touchedSections.forEach((id) => {
          const list = document.querySelector(`.release-list[data-category="${id}"]`);
          if (list) sortListByYear(list);
          updateSectionBadges(id);
          const section = document.getElementById(id + '-section');
          const activeFilter = section && section.querySelector('.release-filter-radio:checked');
          applyFilter(id, activeFilter ? activeFilter.value : 'all');
        });

        wireTracklistIds();
        wireTracklistToggles();
        wireReleaseActionButtons();

        if (btn) {
          notifySuccess(added > 0
            ? `Added ${added} missing release(s) from MusicBrainz`
            : 'No new missing releases found.');
        }
      } catch (error) {
        if (btn) notifyError('Error checking missing releases: ' + error.message);
      }
    });
  }

  function updateSectionBadges(sectionId) {
    const section = document.getElementById(sectionId + '-section');
    const list = document.querySelector(`.release-list[data-category="${sectionId}"]`);
    if (!section || !list) return;
    const items = Array.from(list.querySelectorAll('.release-item'));
    const discovered = items.filter((el) => el.dataset.status === 'library').length;
    const missing = items.filter((el) => el.dataset.status === 'missing').length;

    const toggleBtn = section.querySelector('.release-section-toggle');
    if (!toggleBtn) return;
    const badges = toggleBtn.querySelectorAll('.badge');
    if (badges[0]) badges[0].textContent = String(discovered);
    let missingBadge = toggleBtn.querySelector('.badge.bg-warning');
    if (missing > 0) {
      if (!missingBadge) {
        missingBadge = document.createElement('span');
        missingBadge.className = 'badge bg-warning text-dark';
        toggleBtn.appendChild(missingBadge);
      }
      missingBadge.textContent = `${missing} missing`;
    } else if (missingBadge) {
      missingBadge.remove();
    }
  }

  async function importRelease(artist, releaseId, title, btn) {
    const accepted = await confirmFn({
      title: 'Import release',
      message: `Import "${title}" by ${artist}?`,
      detail: 'The full tracklist will be fetched from MusicBrainz and added to your library.',
      tone: 'primary',
      confirmLabel: 'Import',
    });
    if (!accepted) return;

    return withBusy(btn, '', async () => {
      try {
        const data = await global.api.postJson('/api/artist/import-release', {
          artist, release_id: releaseId, title,
        });
        if (data.error) {
          notifyError('Error importing release: ' + data.error);
          return;
        }
        notifySuccess(data.message || `Imported ${data.tracks_imported || 0} tracks from "${title}"`);
        setTimeout(() => global.location.reload(), 1000);
      } catch (error) {
        notifyError('Error: ' + error.message);
      }
    });
  }

  function wireReleaseActionButtons() {
    document.querySelectorAll('.release-edit-btn').forEach((btn) => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', () => {
        openEditReleaseModal({
          artist: btn.dataset.artist,
          album: btn.dataset.album,
          mbid: btn.dataset.mbid,
          rgid: btn.dataset.rgid,
          year: btn.dataset.year,
        });
      });
    });

    document.querySelectorAll('.release-import-btn').forEach((btn) => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', function () {
        importRelease(btn.dataset.artist, btn.dataset.mbid, btn.dataset.album, this);
      });
    });

    document.querySelectorAll('.release-mb-search-btn').forEach((btn) => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', () => {
        if (global.musicbrainz && typeof global.musicbrainz.search === 'function') {
          global.musicbrainz.search(null, btn.dataset.artist, btn.dataset.album);
        }
      });
    });

    document.querySelectorAll('.release-check-missing-btn').forEach((btn) => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', function () {
        checkMissingForSection(btn.dataset.artist, btn.dataset.category, this);
      });
    });

    // Wire up the new Search All class-based buttons
    document.querySelectorAll('.release-search-all-btn').forEach((btn) => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', () => {
        if (global.artistPage && typeof global.artistPage.searchAllReleases === 'function') {
          global.artistPage.searchAllReleases(btn.dataset.artist);
        } else if (typeof global.searchMusicBrainzForAllReleases === 'function') {
          global.searchMusicBrainzForAllReleases(btn.dataset.artist);
        }
      });
    });
  }

  function openEditReleaseModal({ artist, album, mbid, rgid, year }) {
    document.getElementById('editReleaseArtist').value = artist || '';
    document.getElementById('editReleaseOriginalTitle').value = album || '';
    document.getElementById('editReleaseTitleLabel').textContent = album || '';
    document.getElementById('editReleaseTitle').value = album || '';
    document.getElementById('editReleaseYear').value = year || '';
    document.getElementById('editReleaseType').value = '';
    document.getElementById('editReleaseRecordLabel').value = '';
    document.getElementById('editReleaseMbid').value = mbid || '';
    document.getElementById('editReleaseGroupMbid').value = rgid || '';
    document.getElementById('editReleaseCatalogNumber').value = '';
    document.getElementById('editReleaseBarcode').value = '';
    document.getElementById('editReleaseCountry').value = '';
    document.getElementById('editReleaseDate').value = '';

    const fullPageLink = document.getElementById('editReleaseFullPageLink');
    if (fullPageLink) {
      fullPageLink.href = `/album/${encodeURIComponent(artist || '')}/${encodeURIComponent(album || '')}`;
    }

    const modalEl = document.getElementById('editReleaseModal');
    if (modalEl && global.modal) global.modal.show(modalEl);
  }

  async function saveEditRelease(btn) {
    const artist = document.getElementById('editReleaseArtist').value;
    const originalTitle = document.getElementById('editReleaseOriginalTitle').value;
    const newTitle = document.getElementById('editReleaseTitle').value.trim();
    if (!artist || !originalTitle || !newTitle) return;

    const form = new FormData();
    form.set('album_title', newTitle);
    form.set('album_artist', artist);
    form.set('album_originalyear', document.getElementById('editReleaseYear').value.trim());
    form.set('release_year', document.getElementById('editReleaseYear').value.trim());
    form.set('album_type', document.getElementById('editReleaseType').value);
    form.set('album_recordlabel', document.getElementById('editReleaseRecordLabel').value.trim());
    form.set('album_mbid', document.getElementById('editReleaseMbid').value.trim());
    form.set('album_release_group_mbid', document.getElementById('editReleaseGroupMbid').value.trim());
    form.set('album_catalognumber', document.getElementById('editReleaseCatalogNumber').value.trim());
    form.set('album_barcode', document.getElementById('editReleaseBarcode').value.trim());
    form.set('album_releasecountry', document.getElementById('editReleaseCountry').value.trim());
    form.set('album_releasedate', document.getElementById('editReleaseDate').value.trim());

    return withBusy(btn, 'Saving…', async () => {
      try {
        const url = `/album/${encodeURIComponent(artist)}/${encodeURIComponent(originalTitle)}`;
        const response = await fetch(url, { method: 'POST', body: form });
        if (!response.ok) throw new Error(`Server returned ${response.status}`);

        notifySuccess('Release updated');
        if (global.modal) global.modal.hide('editReleaseModal');
        setTimeout(() => global.location.reload(), 800);
      } catch (error) {
        notifyError('Error saving release: ' + error.message);
      }
    });
  }

  function wireEditReleaseModal() {
    const saveBtn = document.getElementById('editReleaseSaveBtn');
    if (saveBtn) saveBtn.addEventListener('click', function () { saveEditRelease(this); });

    const mbSearchBtn = document.getElementById('editReleaseMbSearchBtn');
    if (mbSearchBtn) {
      mbSearchBtn.addEventListener('click', () => {
        const artist = document.getElementById('editReleaseArtist').value;
        const title = document.getElementById('editReleaseOriginalTitle').value;
        if (global.musicbrainz && typeof global.musicbrainz.search === 'function') {
          global.musicbrainz.search(null, artist, title);
        }
      });
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (!document.getElementById('releases-sections')) return; 

    wireTracklistIds();
    wireTracklistToggles();
    wireSectionCollapse();
    restoreSectionStates();
    wireJumpChips();
    wireFilters();
    wireReleaseActionButtons();
    wireEditReleaseModal();

    if (document.querySelector('.release-section')) {
      checkMissingForSection(artistName(), null, null);
    }
  });

  global.artistReleases = {
    checkMissingForSection,
    openEditReleaseModal,
    saveEditRelease,
  };
})(window);
