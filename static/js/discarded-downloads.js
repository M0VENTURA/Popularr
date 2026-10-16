/**
 * Discarded downloads — the queue page's "why was this thrown away?" list.
 *
 * Reads /api/queue/rejections. A rejected download is DELETED from disk and its
 * queue row is returned to `queued` (where the next attempt overwrites
 * `failure_reason`), so the live queue cannot answer "what was discarded, and
 * why" — this list is the only place that record exists.
 *
 * ⚠️ The card stays hidden while there is nothing to show. This is an ERROR
 * surface, not a status panel: a permanent "0 discarded" box on a healthy queue
 * is noise the operator learns to ignore, which is exactly what makes a real
 * discard go unnoticed.
 */
(function () {
    'use strict';

    var ENDPOINT = '/api/queue/rejections';

    function esc(value) {
        if (typeof window.escapeHtml === 'function') return window.escapeHtml(String(value == null ? '' : value));
        // The page always loads downloads.js, which defines escapeHtml. Falling
        // back to a real escaper rather than an identity function matters: every
        // value below comes from an ID3 tag or a filename.
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function relativeTime(value) {
        if (!value) return '';
        // The server sends a SQL timestamp; Safari rejects "YYYY-MM-DD HH:MM:SS"
        // without a T separator, so normalise it before parsing.
        var text = String(value).trim().replace(' ', 'T');
        if (!/Z|[+-]\d{2}:?\d{2}$/.test(text)) text += 'Z';
        var then = new Date(text);
        if (isNaN(then.getTime())) return '';
        var seconds = Math.floor((Date.now() - then.getTime()) / 1000);
        if (seconds < 60) return seconds <= 1 ? 'just now' : seconds + 's ago';
        var minutes = Math.floor(seconds / 60);
        if (minutes < 60) return minutes + 'm ago';
        var hours = Math.floor(minutes / 60);
        if (hours < 24) return hours + 'h ago';
        return Math.floor(hours / 24) + 'd ago';
    }

    function rowHtml(item) {
        var subject = [item.artist, item.title].filter(Boolean).join(' – ');
        var reason = item.detail || item.reason || 'Did not match the queue item';
        var meta = [];
        // `detail` already names the artist/length, so only add what it does NOT
        // say — repeating it reads as two different facts.
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
        var card = document.getElementById('discardedDownloadsCard');
        if (card) card.classList.toggle('d-none', !visible);
    }

    window.refreshDiscardedDownloads = function () {
        return fetch(ENDPOINT + '?limit=25', { headers: { 'Accept': 'application/json' } })
            .then(function (response) { return response.json(); })
            .then(function (data) {
                var rows = (data && data.rejections) || [];
                var list = document.getElementById('discardedList');
                var count = document.getElementById('discardedCount');
                if (count) count.textContent = String(rows.length);
                if (list) list.innerHTML = rows.map(rowHtml).join('');
                setVisible(rows.length > 0);
                return rows;
            })
            .catch(function (error) {
                // A log that cannot be read must not break the queue page, and
                // must not reveal an empty section as if nothing were discarded.
                console.error('Could not load discarded downloads', error);
                setVisible(false);
                return [];
            });
    };

    window.clearDiscardedDownloads = function () {
        // An explicit operator action. Clearing on READ would make the list
        // vanish the moment anyone looked at it.
        return fetch(ENDPOINT + '?clear=1&limit=25', { headers: { 'Accept': 'application/json' } })
            .then(function (response) { return response.json(); })
            .then(function () {
                var list = document.getElementById('discardedList');
                if (list) list.innerHTML = '';
                var count = document.getElementById('discardedCount');
                if (count) count.textContent = '0';
                setVisible(false);
            })
            .catch(function (error) {
                console.error('Could not clear discarded downloads', error);
            });
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', window.refreshDiscardedDownloads);
    } else {
        window.refreshDiscardedDownloads();
    }
})();
