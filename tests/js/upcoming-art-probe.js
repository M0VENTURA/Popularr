/**
 * Drive the real `releaseArtUrl` out of the shipped service.
 *
 * Loading the IIFE into a stub `window` and calling the exported function is the
 * only way to assert the RULE; pattern-matching the markup it produces would pass
 * on a wrong URL as long as the shape looked right.
 *
 * Usage: node tests/js/upcoming-art-probe.js <path-to-upcoming-releases.js>
 * Output: one JSON line.
 */
'use strict';

const fs = require('fs');

const file = process.argv[2];
if (!file) {
  console.error('usage: node upcoming-art-probe.js <path-to-upcoming-releases.js>');
  process.exit(2);
}

const source = fs.readFileSync(file, 'utf8');

/**
 * The stub must ESCAPE, like the real `escapeHtml` (which round-trips a text node
 * through `innerHTML`). An identity stub would make the `thumb_escapes` check
 * below pass on a helper that never escapes anything.
 */
function escapeHtmlStub(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// The module is an IIFE taking `window`, so a stub global is enough to load it.
const window_ = { escapeHtml: escapeHtmlStub };
new Function('window', source)(window_);

const service = window_.UpcomingReleasesService || {};
const url = service.releaseArtUrl;
if (typeof url !== 'function') {
  console.log(JSON.stringify({ error: 'releaseArtUrl was not exported' }));
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
// These five keys are emitted by the live tree's probe too, so the Python test
// can assert the two trees agree on the ELEMENT and not just on the URL.
const thumbFn = service.artThumb;
if (typeof thumbFn === 'function') {
  // ⚠️ Built from the HOSTILE payload, not a tidy one. With a tidy mbid the URL
  // cannot contain a quote, so the `thumb_escapes` check below passes no matter
  // what — it would pin nothing. The requirement being pinned is that the src
  // attribute cannot be broken out of, and the hostile value is what tests it.
  const thumb = thumbFn({ release_group_mbid: 'a"b<c>d e' }, 250);
  // '' when there is nothing to ask for, so no <img> and therefore no request.
  out.thumb_none = thumbFn({ album_in_collection: false }, 250);
  out.thumb_has_img = thumb.startsWith('<img ');
  // A missing cover must not leave a broken-image glyph behind.
  out.thumb_removes_on_error = thumb.includes('onerror="this.remove()"');
  // Fifty covers must not all be fetched during the first paint.
  out.thumb_is_lazy = thumb.includes('loading="lazy"');
  // The src attribute closes cleanly, so nothing escaped into the markup.
  out.thumb_escapes = /^<img src="[^"]*" alt=""/.test(thumb);
}

console.log(JSON.stringify(out));
