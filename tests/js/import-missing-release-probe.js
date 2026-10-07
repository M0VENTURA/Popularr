/**
 * The artist page's Import button must QUEUE A DOWNLOAD, not fabricate rows.
 *
 * Usage:  node tests/js/import-missing-release-probe.js
 *
 * Extracts the SHIPPED `importMissingRelease` from BOTH artist-releases.js
 * copies (the module `pages/artist_detail_v2.html` actually loads) and drives
 * it against stubs. It must:
 *
 *   * open the shared MusicBrainz picker prepopulated with (artist, album);
 *   * queue the picked release via `downloadMbRelease`, falling back to
 *     `downloadReleaseViaSoulseek` (neither is loaded on the artist page, so
 *     the fallback is the path that really runs there);
 *   * do nothing when the picker is dismissed (falsy selection);
 *   * degrade loudly with `toastError` when the picker is unavailable;
 *   * NEVER POST `/api/artist/import-release`.
 *
 * WHY THAT ENDPOINT MUST NOT BE WIRED ANY MORE
 * --------------------------------------------
 * `artist_scan_service.import_release()` is documented as "Import a missing
 * release as PLACEHOLDER track records": it writes one `tracks` row per track
 * with `file_path: NULL`, then DELETES the `missing_releases` row. The artist
 * page builds its OWNED releases from `SELECT * FROM tracks` with no file-path
 * guard, so those rows make the release appear under Releases INSTANTLY —
 * before a single file exists — while its missing entry is already gone. The
 * toast still said "Import queued for …", so a click that queued nothing looked
 * like success. This is the reported bug:
 * "When tracks are added to the download queue from the artist, it's
 *  automatically adding them to the releases before the download has completed."
 *
 * The 2026-08-19 change log retired the endpoint from every button, but only in
 * the files it touched; this module is the one the routed artist page loads.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..', '..');
const FILES = [
  'static/js/artist-releases.js',
  'test_site/static/js/pages/artist-releases.js',
];

let failures = 0;
function check(label, ok, detail) {
  if (ok) {
    console.log(`  ok   ${label}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${label}${detail ? ' — ' + detail : ''}`);
  }
}

function extractFunction(file) {
  const full = path.join(REPO, file);
  const source = fs.readFileSync(full, 'utf-8');
  const start = source.indexOf('function importMissingRelease(');
  if (start === -1) throw new Error(`${file}: ANCHOR NOT FOUND: function importMissingRelease(`);

  // Sibling functions sit at two-space indent inside the IIFE.
  const next = source.indexOf('\n  function ', start + 1);
  if (next === -1) throw new Error(`${file}: ANCHOR NOT FOUND: next sibling function`);

  return source.slice(start, next);
}

/** Strip JS block comments so prose that NAMES the retired endpoint is not code. */
function stripBlockComments(code) {
  return code.replace(/\/\*[\s\S]*?\*\//g, '');
}

/**
 * Build the shipped function against stubs, mirroring how the module runs:
 * an IIFE parameter called `global`, NO 'use strict' (the module has none).
 */
function build(code, globals) {
  const state = {
    posts: [],
    toastsError: [],
    toastsSuccess: [],
    pickerArgs: null,
  };

  const postJson = (url, body) => {
    state.posts.push({ url, body });
    return Promise.resolve({});
  };
  const toastError = (m) => state.toastsError.push(m);
  const toastSuccess = (m) => state.toastsSuccess.push(m);

  // The picker is optional: pass globals.openGlobalMbSearch yourself.
  const factory = new Function(
    'global', 'toastError', 'toastSuccess', 'postJson',
    `${code}\nreturn importMissingRelease;`
  );
  state.fn = factory(globals, toastError, toastSuccess, postJson);
  return state;
}

const BUTTON = {
  disabled: false,
  innerHTML: 'Import',
  dataset: { releaseId: 'rg-0001' },
};
const SUMMARY = {
  getAttribute: (name) => (name === 'data-release-id' ? 'rg-0001' : ''),
};
const RELEASE = { id: 'mbid-release-1', title: 'Fingerprints', artist: 'Powderfinger' };

function scenarios(file, code) {
  console.log(`\n${file}`);

  // ── 1. It must not touch the placeholder-row endpoint ──────────────────
  {
    let opened = null;
    const globals = {
      openGlobalMbSearch: (artist, album, cb) => { opened = { artist, album, cb }; },
    };
    const s = build(code, globals);
    s.fn('Powderfinger', 'Fingerprints: The Best of Powderfinger 1994-2000', BUTTON, SUMMARY);

    check('never POSTs /api/artist/import-release',
      s.posts.length === 0,
      `posts=${JSON.stringify(s.posts)}`);
    check('endpoint absent from the shipped CODE (comments stripped)',
      !stripBlockComments(code).includes('import-release'));
    check('opens the shared MB picker prepopulated with artist + album',
      opened !== null
      && opened.artist === 'Powderfinger'
      && opened.album === 'Fingerprints: The Best of Powderfinger 1994-2000'
      && typeof opened.cb === 'function',
      JSON.stringify(opened && { artist: opened.artist, album: opened.album }));
    check('no toast claiming a queue it never created',
      s.toastsSuccess.length === 0 && s.toastsError.length === 0);
  }

  // ── 2. A picked release is queued via downloadMbRelease ────────────────
  {
    let cb = null;
    const calls = [];
    const globals = {
      openGlobalMbSearch: (a, al, c) => { cb = c; },
      downloadMbRelease: (...args) => calls.push(args),
    };
    const s = build(code, globals);
    s.fn('Powderfinger', 'Fingerprints', BUTTON, SUMMARY);
    cb(RELEASE);

    check('queues the picked release through downloadMbRelease(.., "slskd")',
      calls.length === 1
      && calls[0][0] === RELEASE.id
      && calls[0][1] === RELEASE.title
      && calls[0][2] === RELEASE.artist
      && calls[0][3] === 'slskd',
      JSON.stringify(calls));
  }

  // ── 3. Fallback: downloadMbRelease is NOT on the artist page ───────────
  {
    let cb = null;
    const calls = [];
    const globals = {
      openGlobalMbSearch: (a, al, c) => { cb = c; },
      downloadReleaseViaSoulseek: (...args) => calls.push(args),
    };
    const s = build(code, globals);
    s.fn('Powderfinger', 'Fingerprints', BUTTON, SUMMARY);
    cb(RELEASE);

    check('falls back to downloadReleaseViaSoulseek',
      calls.length === 1
      && calls[0][0] === RELEASE.id
      && calls[0][1] === RELEASE.title
      && calls[0][2] === RELEASE.artist,
      JSON.stringify(calls));
  }

  // ── 4. A dismissed picker queues nothing ───────────────────────────────
  {
    let cb = null;
    const calls = [];
    const globals = {
      openGlobalMbSearch: (a, al, c) => { cb = c; },
      downloadMbRelease: (...args) => calls.push(args),
      downloadReleaseViaSoulseek: (...args) => calls.push(args),
    };
    const s = build(code, globals);
    s.fn('Powderfinger', 'Fingerprints', BUTTON, SUMMARY);
    cb(null);

    check('queues nothing when the picker is dismissed',
      calls.length === 0 && s.posts.length === 0,
      JSON.stringify(calls));
  }

  // ── 5. No picker available → loud, no throw ────────────────────────────
  {
    const s = build(code, {});
    let threw = null;
    try { s.fn('Powderfinger', 'Fingerprints', BUTTON, SUMMARY); } catch (e) { threw = e; }
    check('reports a missing picker instead of throwing',
      threw === null && s.toastsError.length === 1,
      threw ? String(threw) : `toasts=${s.toastsError.length}`);
  }

  // ── 6. Neither download helper available → loud, no throw ──────────────
  {
    let cb = null;
    const globals = { openGlobalMbSearch: (a, al, c) => { cb = c; } };
    const s = build(code, globals);
    let threw = null;
    try {
      s.fn('Powderfinger', 'Fingerprints', BUTTON, SUMMARY);
      cb(RELEASE);
    } catch (e) { threw = e; }
    check('reports a missing download helper instead of throwing',
      threw === null && s.toastsError.length === 1,
      threw ? String(threw) : `toasts=${s.toastsError.length}`);
  }

  // ── 7. The button must not be left disabled ────────────────────────────
  {
    let cb = null;
    const globals = { openGlobalMbSearch: (a, al, c) => { cb = c; } };
    const s = build(code, globals);
    const btn = { disabled: false, innerHTML: 'Import', dataset: {} };
    s.fn('Powderfinger', 'Fingerprints', btn, SUMMARY);
    check('does not disable the import button (the modal is the feedback)',
      btn.disabled === false && btn.innerHTML === 'Import',
      JSON.stringify({ disabled: btn.disabled, innerHTML: btn.innerHTML }));
    void cb;
  }
}

for (const file of FILES) {
  const code = extractFunction(file);
  scenarios(file, code);
}

console.log(`\nfailed=${failures}`);
process.exit(failures === 0 ? 0 : 1);
