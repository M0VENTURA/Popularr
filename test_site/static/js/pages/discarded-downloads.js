/**
 * static/js/pages/discarded-downloads.js
 * Discarded downloads — the queue page's "why was this thrown away?" list.
 *
 * Reads GET /api/queue/rejections. A rejected download is DELETED from disk and
 * its queue row is returned to `queued` (where the next attempt overwrites
 * `failure_reason`), so the live queue cannot answer "what was discarded, and
 * why" — this list is the only place that record exists.
 *
 * ⚠️ The card stays hidden while there is nothing to show. This is an ERROR
 * surface, not a status panel: a permanent "0 discarded" box on a healthy queue
 * is noise the operator learns to ignore, which is exactly what makes a real
 * discard go unnoticed.
 *
 * BINDING: `data-action` + addEventListener, not inline `onclick`. This tree
 * moved away from string-assembled inline handlers deliberately — see the
 * release picker's Queue button, whose onclick was assembled into invalid
 * JavaScript. A delegated listener also survives the row markup being replaced,
 * which matters here because the list re-renders on every refresh.
 */
(function (global) {
  'use strict';

  const ENDPOINT = '/api/queue/rejections';

  function esc(value) {
    if (typeof global.escapeHtml === 'function') {
      return global.escapeHtml(String(value == null ? '' : value));
    }
    // Every value below comes from an ID3 tag or a filename, so the fallback is
    // a REAL escaper rather than an identity function.
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /**
   * A compact relative time.
   *
   * ⚠️ The server sends a SQL timestamp ("YYYY-MM-DD HH:MM:SS"). Safari rejects
   * that spelling in the Date constructor, so the space is normalised to a `T`
   * and a UTC designator appended before parsing — otherwise every row reads
   * "Invalid Date" on iOS, which is the platform this page is used from.
   */
  function relativeTime(value) {
    if (!value) return '';
    let text = String(value).trim().replace(' ', 'T');
    if (!/Z|[+-]\d{2}:?\d{2}$/.test(text)) text += 'Z';
    const then = new Date(text);
    if (isNaN(then.getTime())) return '';
    const seconds = Math.floor((Date.now() - then.getTime()) / 1000);
    if (seconds < 60) return seconds <= 1 ? 'just now' : seconds + 's ago';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + 'm ago';
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + 'h ago';
    return Math.floor(hours / 24) + 'd ago';
  }

  function rowHtml(item) {
    const subject = [item.artist, item.title].filter(Boolean).join(' – ');
    const reason = item.detail || item.reason || 'Did not match the queue item';

    // `detail` already names the artist and the lengths, so only add what it does
    // NOT say — repeating it reads as two separate facts.
    const meta = [];
    if (item.found_artist && reason.indexOf(item.found_artist) === -1) {
      meta.push('credited to ' + item.found_artist);
    }
    if (item.filename) meta.push(item.filename);

    return '' +
      '<div class="list-group-item bg-transparent py-2">' +
        '<div class="d-flex align-items-start justify-content-between gap-2">' +
          '<div class="min-w-0">' +
            '<div class="small fw-semibold text-truncate">' +
              (subject ? esc(subject) : '<span class="text-muted">(unknown item)</span>') +
            '</div>' +
            '<div class="small text-warning-emphasis">' + esc(reason) + '</div>' +
            (meta.length
              ? '<div class="text-muted small text-truncate">' + esc(meta.join(' · ')) + '</div>'
              : '') +
          '</div>' +
          '<span class="text-muted small flex-shrink-0 text-end" style="min-width:3.5rem;">' +
            esc(relativeTime(item.created_at)) +
          '</span>' +
        '</div>' +
      '</div>';
  }

  function setVisible(visible) {
    const card = document.getElementById('discardedDownloadsCard');
    if (card) card.classList.toggle('d-none', !visible);
  }

  async function refresh() {
    try {
      const response = await fetch(ENDPOINT + '?limit=25', {
        headers: { Accept: 'application/json' },
      });
      const data = await response.json();
      const rows = (data && data.rejections) || [];
      const list = document.getElementById('discardedList');
      const count = document.getElementById('discardedCount');
      if (count) count.textContent = String(rows.length);
      if (list) list.innerHTML = rows.map(rowHtml).join('');
      setVisible(rows.length > 0);
      return rows;
    } catch (error) {
      // A log that cannot be read must not break the queue page — and must not
      // reveal an empty section as though nothing had been discarded.
      console.error('Could not load discarded downloads', error);
      setVisible(false);
      return [];
    }
  }

  async function clear() {
    // An explicit operator action. Clearing on READ would make the list vanish
    // the moment anyone looked at it.
    try {
      await fetch(ENDPOINT + '?clear=1&limit=25', { headers: { Accept: 'application/json' } });
      const list = document.getElementById('discardedList');
      const count = document.getElementById('discardedCount');
      if (list) list.innerHTML = '';
      if (count) count.textContent = '0';
      setVisible(false);
    } catch (error) {
      console.error('Could not clear discarded downloads', error);
    }
  }

  // Delegated, so re-rendering the rows cannot orphan a handler.
  document.addEventListener('click', function (event) {
    const target = event.target && event.target.closest
      ? event.target.closest('[data-action]')
      : null;
    if (!target) return;
    const action = target.getAttribute('data-action');
    if (action === 'refresh-discarded') refresh();
    else if (action === 'clear-discarded') clear();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', refresh);
  } else {
    refresh();
  }

  global.DiscardedDownloads = {
    refresh: refresh,
    clear: clear,
    // Exposed so a test can drive the renderer without a DOM fixture.
    rowHtml: rowHtml,
  };
})(window);
