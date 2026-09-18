/*
  Metadata Comparison (/metadata-compare).

  Ported from the inline <script> in templates/pages/metadata_compare.html.

  WHAT CHANGED
  ------------
  * Hand-rolled JSON-through-onclick is GONE. The original serialised each
    result with `JSON.stringify(result).replace(/'/g, "\\'")` and interpolated it
    into an onclick string, so any result containing a quote, a backslash or
    `</script>` produced broken JS or an injection. Results are now held in a
    module-local registry keyed by index, and the row carries
    `data-result-index` — nothing user-derived is ever parsed as code.
  * `alert()` / `confirm()` replaced with global.toast / global.ui.confirm, so
    failures surface consistently with the rest of the tree.
  * Uses global.api.postJson instead of raw fetch (adds error handling and the
    error envelope the other modules rely on).
  * `expandComparison()` was a no-op stub — the Bootstrap collapse attribute
    already did the work — so it is deleted rather than carried over.

  Server endpoints (unchanged):
    POST /api/metadata-compare/search-musicbrainz
    POST /api/metadata-compare/apply-musicbrainz
    POST /api/metadata-compare/accept-navidrome
*/
(function (global) {
  'use strict';

  var esc = function (value) {
    return (global.escapeHtml || function (v) { return String(v == null ? '' : v); })(value);
  };

  /* Search results for the currently-open comparison, by index. Kept out of the
     DOM so a malformed title can never become executable markup. */
  var searchResults = [];

  function api() {
    return global.api || {};
  }

  function getModal() {
    var el = document.getElementById('mbSearchModal');
    if (!el || !global.bootstrap) return null;
    return global.bootstrap.Modal.getOrCreateInstance(el);
  }

  function setResults(html) {
    var el = document.getElementById('mbSearchResults');
    if (el) el.innerHTML = html;
  }

  function errorBox(message) {
    return '<div class="alert alert-danger py-2 mb-0">' + esc(message) + '</div>';
  }

  function renderResults(artist, album, results) {
    if (!results.length) {
      return (
        '<div class="alert alert-warning mb-0">' +
          '<p class="mb-2">No matches found on MusicBrainz for ' +
            esc(artist) + ' – ' + esc(album) + '</p>' +
          '<button type="button" class="btn btn-sm btn-primary" ' +
            'data-action="submit-new" data-artist="' + esc(artist) + '" data-album="' + esc(album) + '">' +
            '<i class="bi bi-plus-circle"></i> Submit as New Item</button>' +
        '</div>'
      );
    }

    var rows = results.map(function (result, idx) {
      var firstDate = result['first-release-date'] || '';
      var year = firstDate ? String(firstDate).split('-')[0] : 'N/A';
      var genres = Array.isArray(result.genres) ? result.genres : [];
      var genresHtml = genres.length
        ? '<div class="mb-2"><strong>Genres:</strong> ' +
            genres.map(function (g) {
              return '<span class="badge bg-secondary">' + esc(g) + '</span>';
            }).join(' ') + '</div>'
        : '';

      return (
        '<div class="list-group-item">' +
          '<div class="d-flex justify-content-between align-items-start gap-2">' +
            '<div>' +
              '<h6 class="mb-1">' + esc(result.title || result.album || 'Unknown') + '</h6>' +
              '<p class="mb-1 text-muted">' +
                '<strong>Artist:</strong> ' + esc(result['artist-credit'] || result.artist || 'Unknown') + '<br>' +
                '<strong>Year:</strong> ' + esc(year) +
              '</p>' + genresHtml +
            '</div>' +
            '<button type="button" class="btn btn-sm btn-success flex-shrink-0" ' +
              'data-action="apply" data-result-index="' + idx + '">' +
              '<i class="bi bi-check-circle"></i> Use This</button>' +
          '</div>' +
        '</div>'
      );
    });

    return '<div class="list-group">' + rows.join('') + '</div>';
  }

  function searchMusicBrainz(artist, album) {
    var modal = getModal();
    searchResults = [];
    openSearch = { artist: artist, album: album };
    setResults('<div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div>');
    if (modal) modal.show();

    return api().postJson('/api/metadata-compare/search-musicbrainz', {
      artist: artist,
      album: album
    }).then(function (data) {
      if (!data) return;
      if (data.error) {
        setResults(errorBox(data.error));
        return;
      }
      searchResults = Array.isArray(data.results) ? data.results : [];
      setResults(renderResults(artist, album, searchResults));
    }).catch(function (error) {
      setResults(errorBox(error && error.message ? error.message : String(error)));
    });
  }

  /* The (artist, album) the modal's results belong to, so "Use This" needs no
     arguments — and cannot be fed a stale pair from a previous search. */
  var openSearch = { artist: '', album: '' };

  function applyMusicBrainzData(artist, album, mbData) {
    return api().postJson('/api/metadata-compare/apply-musicbrainz', {
      artist: artist,
      album: album,
      mb_data: mbData
    }).then(function (data) {
      if (data && data.success) {
        toast(data.message || 'Metadata applied.', 'success');
        setTimeout(function () { location.reload(); }, 800);
      } else {
        toast((data && data.error) || 'Could not apply MusicBrainz data.', 'error');
      }
    }).catch(function (error) {
      toast('Error: ' + (error && error.message ? error.message : error), 'error');
    });
  }

  function acceptNavidromeData(artist, album) {
    var ui = global.ui || {};
    /* global.ui.confirm returns a PROMISE (see ui/confirm.js); window.confirm is
       the synchronous fallback, so both are normalised to a Promise here. */
    var proceed = (typeof ui.confirm === 'function')
      ? ui.confirm({
          title: 'Lock Navidrome data?',
          message: artist + ' – ' + album,
          detail: 'This will prevent Beets from overwriting this metadata.',
          tone: 'primary',
          confirmLabel: 'Lock Data'
        })
      : Promise.resolve(window.confirm(
          'Lock Navidrome data for "' + artist + ' - ' + album + '"?\n\n' +
          'This will prevent Beets from overwriting this metadata.'
        ));

    return Promise.resolve(proceed).then(function (ok) {
      if (!ok) return;
      return api().postJson('/api/metadata-compare/accept-navidrome', {
        artist: artist,
        album: album
      }).then(function (data) {
        if (data && data.success) {
          toast(data.message || 'Navidrome data locked.', 'success');
          setTimeout(function () { location.reload(); }, 800);
        } else {
          toast((data && data.error) || 'Could not lock Navidrome data.', 'error');
        }
      });
    }).catch(function (error) {
      toast('Error: ' + (error && error.message ? error.message : error), 'error');
    });
  }

  function submitNewToMusicBrainz(artist, album) {
    var url = 'https://musicbrainz.org/login?redirect=/release/create' +
      '?artist-credit.names.0.artist.name=' + encodeURIComponent(artist) +
      '&name=' + encodeURIComponent(album);
    window.open(url, '_blank', 'noopener');
  }

  function toast(message, kind) {
    var t = global.toast;
    if (t && typeof t[kind] === 'function') {
      t[kind](message);
    } else if (t && typeof t.info === 'function') {
      t.info(message);
    } else {
      // Last resort only — the shared toast is loaded from base.html.
      console.log('[metadata-compare]', kind, message);
    }
  }

  /* One delegated listener for the whole page: the comparison table and the
     modal results are both rendered/injected, so per-element binding would
     break on re-render. */
  function onClick(event) {
    var button = event.target.closest('button[data-action]');
    if (!button) return;
    var action = button.getAttribute('data-action');

    if (action === 'search') {
      event.preventDefault();
      searchMusicBrainz(button.getAttribute('data-artist') || '',
                        button.getAttribute('data-album') || '');
      return;
    }
    if (action === 'accept-navidrome') {
      event.preventDefault();
      acceptNavidromeData(button.getAttribute('data-artist') || '',
                          button.getAttribute('data-album') || '');
      return;
    }
    if (action === 'submit-new') {
      event.preventDefault();
      submitNewToMusicBrainz(button.getAttribute('data-artist') || '',
                             button.getAttribute('data-album') || '');
      return;
    }
    if (action === 'apply') {
      event.preventDefault();
      var idx = parseInt(button.getAttribute('data-result-index'), 10);
      var result = Number.isNaN(idx) ? null : searchResults[idx];
      if (!result) {
        toast('That search result is no longer available — search again.', 'error');
        return;
      }
      applyMusicBrainzData(openSearch.artist, openSearch.album, result);
    }
  }

  function init() {
    document.addEventListener('click', onClick);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  global.metadataCompare = {
    searchMusicBrainz: searchMusicBrainz,
    applyMusicBrainzData: applyMusicBrainzData,
    acceptNavidromeData: acceptNavidromeData,
    submitNewToMusicBrainz: submitNewToMusicBrainz
  };
})(window);
