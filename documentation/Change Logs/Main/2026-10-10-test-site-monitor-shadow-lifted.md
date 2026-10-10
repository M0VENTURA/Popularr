# Test-site Downloads monitor rebuilt — shadow lifted (2026-10-10)

**REPORT:** a template shadowing warning on every boot:
`[helpers.test_site_mode] Template shadowed in the rebuilt tree — serving the
live version requested='pages/downloads/monitor.html'`.

## Root cause

`test_site/templates/Pages/downloads/monitor.html` was a **stray artist-page
snapshot** (title `{{ artist_name }}`, thousands of lines, none of the monitor
ids) that raised `BuildError` when served, so it was put on the rebuilt tree's
shadow list and the LIVE monitor page was served instead. That is exactly why
the earlier "Downloads layout reset to the original site" report existed and
why this warning printed on every scan/boot.

Meanwhile the rebuilt tree's **controllers were complete** — `pages/monitor.js`
(unmatched folders, upcoming releases, disk actions), `pages/soulseek-search.js`
(the Soulseek tab), `pages/download-queue.js` (the queue) — but had **no
template to drive**. They only ever ran through the LIVE page's legacy
`js/downloads.js` + `js/monitor.js` wiring.

## What changed

1. **Built the real test_site monitor page** (`test_site/templates/Pages/downloads/monitor.html`):
   the LIVE page's structure (Soulseek search / Playlist Downloads / Active
   Queue & Monitor tabs + Matched & Unmatched Folders + Upcoming Releases) but
   loading the rebuilt tree's modern controllers — `pages/monitor.js`,
   `pages/download-queue.js`, `pages/soulseek-search.js`, `services/slskd.js`,
   `services/item-groups.js`. No inline `window.AppConfig` (nothing reads it),
   no bare `url_for('dashboard')` endpoints (the old 500), and the
   jump-to-upcoming button is a `data-action` bound by `monitor.js`.
2. **Lifted the shadow** — `helpers/test_site_mode.py::_SHADOWED_TEMPLATES` is
   now `frozenset()`. The rebuilt monitor page is served in test_site mode.
3. **Updated the tests that pinned the shadow:**
   - `test_test_site_shadowing.py::TestShadowListContents` asserts the list is
     EMPTY (deliberately fails if someone re-shadows a fixed page);
   - `test_rebuilt_pages_have_no_dead_buttons.py`: the monitor page's register
     entries are the shared-partial ones (`saveEditedTrack` /
     `addEditTrackGenre` / `importPlaylistFromCSV` / `createPlaylist`,
     matching the queue page); the stale artist-page entries
     (`saveEditedTrackFromArtistPage` / `toggleMissing` /
     `addEditArtistTrackGenre` / `fetchArtistCountry` / `editArtistCountry`)
     are DELETED;
   - `test_manual_search_button_works_in_test_site.py`'s premise block now
     pins that the rebuilt monitor page loads `services/slskd.js` and
     `pages/monitor.js` and is no longer shadowed (the manual-search modal's
     delegate now resolves on slskd.js on BOTH downloads pages).

## Verification

- Render probe: the test-site loader now resolves
  `pages/downloads/monitor.html` to the REBUILT tree; all modern section ids
  (`unmatchedFoldersSection`, `upcomingReleasesSection`,
  `upcomingFilterCollectionMonitor`, …) and script mounts present.
- 27 tests pass across `test_test_site_shadowing`,
  `test_rebuilt_pages_have_no_dead_buttons`,
  `test_manual_search_button_works_in_test_site`, plus the template/artist/
  queue wiring suites (26+6+16+32+6+55). `import app` → 394 routes.