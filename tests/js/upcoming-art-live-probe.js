/**
 * Drive the LIVE tree's artwork rule — `upcomingArtUrl` / `upcomingArtThumb`
 * out of `static/js/upcoming_releases.js`.
 *
 * That file is a self-contained IIFE that assigns `window.UpcomingReleasesService`,
 * so a stub `window` plus a stub `document` is enough to load the REAL shipped
 * module and call the REAL exported functions. Pattern-matching the markup they
 * produce would pass on a wrong URL as long as the shape looked plausible.
 *
 * ⚠️ The rule used to be written inline in `static/js/dashboard.js`; it now lives
 * here, in the service the dashboard, the dedicated page and the downloads
 * monitor all share, and the dashboard delegates to it. This probe follows it.
 *
 * Usage: node tests/js/upcoming-art-live-probe.js <path-to-upcoming_releases.js>
 * Output: one JSON line.
 */
'use strict';

const fs = require('fs');

const file = process.argv[2];
if (!file) {
  console.error('usage: node upcoming-art-live-probe.js <path-to-upcoming_releases.js>');
  process.exit(2);
}

const source = fs.readFileSync(file, 'utf8');

/**
 * Just enough DOM for this module to load.
 *
 * `injectSourceBadgeStyles()` runs at load time and needs `getElementById`,
 * `createElement` and `head.appendChild`; `escapeHtml()` needs createElement +
 * appendChild + createTextNode to come back out through `innerHTML`. The escaping
 * implemented here is what a browser does to a TEXT node, which is exactly what
 * the real `escapeHtml` relies on.
 */
function makeDom() {
  function escapeText(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  return {
    getElementById: () => null,
    createTextNode: (text) => ({ text: String(text) }),
    createElement: () => ({
      _text: '',
      appendChild(node) {
        this._text += node.text;
      },
      set innerHTML(value) {
        this._text = value;
      },
      get innerHTML() {
        return escapeText(this._text);
      },
      style: {},
    }),
    head: { appendChild: () => {} },
  };
}

const window_ = {};
const document_ = makeDom();

new Function('window', 'document', source)(window_, document_);

const service = window_.UpcomingReleasesService || {};
const url = service.upcomingArtUrl;
const thumbFn = service.upcomingArtThumb;

if (typeof url !== 'function' || typeof thumbFn !== 'function') {
  console.log(
    JSON.stringify({
      error: 'the artwork helpers are missing or not exported',
      hasUrl: typeof url === 'function',
      hasThumb: typeof thumbFn === 'function',
    })
  );
  process.exit(0);
}

const out = {};

// ── The priority order ────────────────────────────────────────────────────
// An owned album: the app's own endpoint, which prefers Navidrome.
out.owned = url({
  album_in_collection: true, artist_name: 'a-ha', album_name: 'Take On Me',
  release_group_mbid: 'rg-1',
}, 250);

// ⭐ The case this change exists for: NOT owned, but MATCHED.
out.matched = url({
  album_in_collection: false, artist_name: 'a-ha', album_name: 'Take On Me',
  release_group_mbid: 'rg-2',
}, 250);

// A candidate is a GUESS awaiting a human; its art must NOT be shown.
out.candidate_only = url({
  album_in_collection: false, mbid_match_status: 'candidate',
  candidate_release_group_mbid: 'cand-1',
}, 250);

// Nothing to ask for: the caller draws its placeholder and makes NO request.
out.neither = url({ album_in_collection: false }, 250);

// An mbid containing characters that would break out of an HTML attribute.
out.hostile = url({ release_group_mbid: 'a"b<c>d e' }, 250);

// The size must reach both branches.
out.owned_size = url({ album_in_collection: true, artist_name: 'A', album_name: 'B' }, 500);
out.matched_size = url({ release_group_mbid: 'rg-3' }, 500);

// ── The thumbnail element ─────────────────────────────────────────────────
// '' when there is nothing to ask for, so the caller renders its placeholder
// without an <img> and therefore without a request.
out.thumb_none = thumbFn({ album_in_collection: false }, 250, 'upcoming-thumb');

// ⚠️ Built from the HOSTILE payload, not a tidy one. With a tidy mbid the URL
// cannot contain a quote, so the `thumb_escapes` check below passes no matter
// what — it would pin nothing. The requirement being pinned is that the src
// attribute cannot be broken out of, and the hostile value is what tests it.
const thumb = thumbFn({ release_group_mbid: 'a"b<c>d e' }, 250, 'upcoming-thumb');
out.thumb_has_img = thumb.startsWith('<img ');
// A missing cover must not leave a broken-image glyph behind.
out.thumb_removes_on_error = thumb.includes('onerror="this.remove()"');
// Fifty covers must not all be fetched during the first paint.
out.thumb_is_lazy = thumb.includes('loading="lazy"');
// The src attribute closes cleanly, so nothing escaped into the markup.
out.thumb_escapes = /^<img src="[^"]*" alt=""/.test(thumb);

console.log(JSON.stringify(out));
