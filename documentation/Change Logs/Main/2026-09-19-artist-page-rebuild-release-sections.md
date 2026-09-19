# Artist page rebuild — release sections, shared component, and the wiring that was missing

**Date:** 2026-09-19
**Area:** `templates` / `test_site/templates` / `static` / `test_site/static`
**Status:** implemented, guarded by `tests/test_artist_page_contract.py`

---

## What was asked

Install the supplied artist-page redesign as a full replacement, **under
`test_site/`**, and migrate the v2-only blocks into the new UI.

## Why this was not a copy-and-paste

The four supplied files were all committed, but **none of them were wired into
anything**. Seven independent defects, each of which fails silently or only at
render time:

| # | Defect | Effect |
|---|--------|--------|
| 1 | `templates/pages/artist_detail.html` used **bare** `url_for` names (`dashboard`, `artists`, `scan_artist_custom`, `track_detail`, `album_detail`, `api_artist_image`) while every blueprint is namespaced | `BuildError: Could not build url for endpoint 'dashboard'` on first render |
| 2 | `test_site/templates/components/_release_section.html` was a **333-line full artist PAGE** (byte-identical to `artist_detail.html`, same MD5) defining **no macro** | `ImportError: cannot import name 'render_release_section'` — and being in the *preferred* loader, it shadowed the correct live macro |
| 3 | `static/js/pages/artist-releases.js` did not exist — only `test_site/.../artist_releases.js` (underscore) | `<script src>` 404'd; all release UI dead |
| 4 | `static/css/artist.css` (live) had **zero** `.release-*` rules; all 16 lived only in `test_site/` | Release rows unstyled in **live mode — the DEFAULT** (only the rebuilt tree resolved them) |
| 5 | The macro read `album.title`, but **library** albums carry `album`, not `title` | Empty album name for every owned release. Silent: Jinja's `Undefined` is falsy, so a bad key yields a blank cell, not an error |
| 6 | `test_site` had no `Pages/artist_detail_v2.html`, only `Pages/artist_detail.html` | The route asks for `artist_detail_v2.html`, so the rebuilt page was **never served** however often it was edited |
| 7 | `test_site` page included `components/modals/_artist_modals.html` (plural) while the tree only had `_artist_modal.html` (singular) | `TemplateNotFound` — the page could not render at all |

Additionally, `static/js/artist_detail.js` is **not JavaScript**: it is a
5346-line Jinja page template with `{% extends "base.html" %}` on line 1, loaded
via `<script src>`. The browser throws `SyntaxError` and discards the whole file,
so *most* of the artist page's JavaScript (`openArtistImageModal`,
`fetchArtistCountry`, `openEditArtistIdsModal`, `playArtistTopTracks`,
`loadArtistCoveredBy`, `checkMissingReleases`, …) was already dead in the live
tree and only ever worked under `features.use_test_site`.

## What changed

### Shared component (both trees)

`templates/components/_release_section.html` and its rebuilt-tree twin are now
equivalent macro libraries exporting `render_release_section` and
`render_release_row`.

- Every field read is **defensive** (`album.album or album.title`), because the
  route builds `albums_by_category` from two different shapes and no key exists
  on both.
- The filter moved **into each section** (All / Library / Missing with counts).
  The old page-wide bar could only express one intent for the whole page: picking
  "Missing" hid every owned album in every category, so an artist with one missing
  single and twenty owned albums looked like they owned nothing.
- Rows gained an "In Library" / "Missing" badge, an edition tagline when
  `release_title` differs from the release-group name, a gap badge for owned
  albums with missing tracks, and an "open album" link.
- Tracklist ids are still assigned **client-side** (`safeForDomId`), because a
  release-group title is not unique on its own (three self-titled Weezer albums).
- The rebuilt copy keeps its superseded 333-line page snapshot wrapped in
  `{% raw %}` so it is unambiguously inert and cannot shadow the macro again.

### Page (both trees)

`templates/pages/artist_detail_v2.html` and
`test_site/templates/Pages/artist_detail_v2.html`

- The inline 100-line release macro is gone; the page imports the shared component.
- The page-wide filter bar is replaced by a hint plus **Expand All / Collapse All
  / Check Missing**.
- Every `url_for` is namespaced (`ui.`, `scans.`, `artist.`).
- v2-only blocks were **migrated, not dropped**: PLAY TOP TRACKS (fed by
  `window._artistPlaylist`), the Actions dropdown (Corrections, Genre Management,
  Edit External IDs, Force Metadata Refresh), the `_scan_selector.html` partial
  (scan type + Restart), the tabbed Albums / Top / About layout, and Appears On.

### JavaScript

- **`static/js/artist-releases.js`** (new) — release sections, tracklists, the
  per-section filter, import, MB search, gap badges, expand/collapse.
- **`static/js/artist-page.js`** (new, live tree) — the hero functions that were
  trapped inside the dead Jinja file: bio, country, external IDs, image picker,
  favourite, PLAY TOP TRACKS, covers, similar artists, bio clamp.
- **`test_site/static/js/pages/artist-detail-extras.js`** (new) — the eight
  handlers `pages/artist.js` does not define (`openArtistImageModal`,
  `setArtistImage`, `fetchArtistCountry`, `editArtistCountry`,
  `forceArtistMetadataRefresh`, `toggleArtistBio`, `goToArtistAbout`,
  `checkMissingReleases`, `playArtistTopTracks`). Without it every one of those
  buttons threw `ReferenceError` in the rebuilt tree.
- **`test_site/static/js/pages/artist-releases.js`** (new) — byte-identical to the
  live copy, asserted by test. The module touches no tree-specific global: it
  feature-detects `toast` / `api` / `ui.modal` and falls back to
  `showToast` / `fetch` / `bootstrap.Modal`, so one implementation serves both.
- **`static/js/artist-album-filter.js`** — retargeted from
  `.category-section .album-row[data-status]` to the new `.release-item[data-status]`
  markup. Left as-is it would have become inert while looking entirely correct,
  which is the failure mode its own header warns about.

### Route / config

- `helpers/test_site_mode.py` — documented why `components/_release_section.html`
  is **not** shadowed (it was repaired in place instead).

### Note on file paths

The two trees lay `static/` out differently — the live tree is flat
(`static/js/*.js`) while the rebuilt tree is foldered
(`static/js/pages/*.js`). The tests therefore compare module *contents* across a
declared path pair rather than assuming identical relative paths.

## Guards added

`tests/test_artist_page_contract.py` — 18 tests covering each defect above:

1. the shared component exists in both trees and defines the macros it is imported for
2. it does not `extends` (a macro library must not render a page)
3. no `album.title` read without an `album.album` fallback
4. every `{% include %}` target exists in that page's own tree
5. every `url_for` endpoint is namespaced with a known blueprint prefix
6. both trees ship the page under the **routed** name
7. every `versioned_static('js/…')` exists in the serving tree
8. every artist page links `css/artist.css` and carries no inline `<style>`
9. release layout classes are styled in **both** stylesheet copies
10. and — the inverse — no `.release-*` rule exists without matching markup
    (dead CSS, the trap `test_site/static/css/artist.css` fell into)
11. the two `artist-releases.js` copies are byte-identical
12. neither release module contains Jinja in code

Verified against the pre-fix baseline: **6 of the 18 fail**, each naming a real
defect. Against the fixed tree: all pass.

### Guards retargeted (their contract legitimately changed)

Two guards from the earlier layout audit pinned markup this work deliberately
replaced. Leaving them would have meant either a permanently red suite or a
false "regression":

- `test_layout_css_js_template_integrity.py`
  - `test_artist_css_has_no_dead_album_row_selectors` now **strips CSS comments
    first**. The stylesheets quote `tr.album-row` in their own headers to explain
    that it can never match, so a raw scan failed on a correct file. (This was a
    pre-existing failure on `origin/develop` — the guard's own documentation
    tripped it.)
  - `test_artist_page_markup_matches_that_assertion` →
    `test_album_row_markup_matches_that_assertion`, retargeted from the artist
    page to `components/_album_category_section.html`. The artist page no longer
    renders `.album-row` at all (its rows are `.release-item`), so asserting
    against it tested the ABSENCE of moved markup rather than the element type of
    markup that exists.
- `test_static_js_is_not_jinja.py`
  - `test_artist_filter_module_is_loaded_and_publishes_globals` no longer asserts
    the page calls `setArtistFilter()` from an onclick — that page-wide bar was
    replaced by per-section filters. It now pins that
    `artist-album-filter.js` was **retargeted to `.release-item`** (the old
    `.category-section .album-row` selector matches nothing after the migration,
    leaving the module inert while looking correct) and that
    `#releases-sections` still exists, since that marker is what initialises
    `js/artist-releases.js`.

### Measured result

Run in a clean worktree of `origin/develop` (all three guard suites together):

| | failed | passed | skipped |
|---|---|---|---|
| **baseline** (pre-fix) | 2 | 324 | 1 |
| **after this work** | 1 | 351 | 1 |

The single remaining failure is the pre-existing `static/js/artist_detail.js`
contains Jinja" — the dead 5346-line file that this work replaces, and which
`origin/develop` already failed. **This change introduces zero new failures.**

## Follow-ups (not done here)

- `test_site/templates/Pages/artist_detail.html` and
  `components/_release_section.html` still carry dead page bodies as inert
  `{% raw %}` blocks and should be **deleted** once the environment allows
  destructive edits. `Pages/artist_detail.html` is an alias extending the real
  page so it can never fail to build.
- `static/js/artist_detail.js` (the 5346-line Jinja file) is still loaded by
  nothing on the artist page. It should be deleted or converted; the artist page
  no longer depends on it.
- Pre-existing test failures in unrelated suites are unchanged by this work.
