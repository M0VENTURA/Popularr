# test_site: mobile/touch + PWA pass, and the navbar covering the scrollbar (2026-10-13)

## Reported

> "the top bar covers part of the right scroll on the web browser, so it may
> need to be brought in slightly from the right hand side."

## Root cause of the scrollbar overlap

`body` is `overflow: hidden` and page scrolling lives on
`main.container-fluid`, so the scrollbar belongs to **main**. The navbar is
`position: fixed; left: 0; right: 0` with a higher z-index, so it spanned the
full viewport and lay **over the top of that scrollbar** — it could not be
dragged for the first ~`--navbar-height` pixels.

**Fix (two halves):**
- `main.container-fluid` gains `scrollbar-gutter: stable`, so the track is
  reserved whether or not it is currently needed (no sideways shift when a page
  grows past one screen).
- The navbar's right edge is pulled in by the measured gutter:
  `--scrollbar-width` is written by a new `syncScrollbarWidth()` in `main.js`
  (the single geometry writer, next to `syncNavbarHeight`), measured from
  **main** (`offsetWidth - clientWidth`) — not the document, whose client width
  says nothing because `body` does not scroll. On mobile the scrollbar is an
  overlay that occupies no layout width, so the token is 0 and the inset is a
  no-op.

## Everything else in this pass

**PWA shell** (`base.html` + new `static/manifest.webmanifest`,
`static/img/icon.svg`, `static/img/icon-maskable.svg`):
- `viewport-fit=cover` — **load-bearing**: without it iOS reports
  `env(safe-area-inset-*)` as **0**, which made the bottom sheet's existing
  `padding-bottom: env(safe-area-inset-bottom)` a no-op, so content sat under
  the home indicator on notched devices.
- `theme-color`, `apple-mobile-web-app-*`, `mobile-web-app-capable`, favicon and
  `apple-touch-icon`, and a `standalone` manifest with `any` + `maskable` icons
  → "Add to Home Screen" installs a chrome-less app.

**Mobile keyboard** — the three search inputs (`#navSearchInput`,
`#unifiedSearchInput`, `#slskdSearchInput`) become `type="search"` with
`inputmode="search"`, `enterkeyhint="search"`, `autocapitalize="off"`,
`autocorrect="off"`, `spellcheck="false"`. Previously a plain `type="text"` gave
a Return key and no clear button, and the OS could "correct" artist names as
they were typed.

**Dashboard — blank Upcoming card on mobile (real bug, fixed):**
`loadUpcomingReleasesTable()` rendered only into `#upcomingTableWrap`, which is
`d-none d-lg-block`. `#upcoming-releases-cards` (`d-lg-none`) was **never
populated**, so on a phone the card showed its header and footer around an empty
body. It now renders the same (responsive) accordion into **both** containers,
and shows a loading state while the fetch is in flight instead of reading as
"no releases".

**Dashboard accessibility:**
- `role="status" aria-live="polite"` on the scanner badge, the three pane
  status lines (`pop-status`/`nav-status`/`ess-status`) and the approval count —
  scan start/finish/failure and new approvals were never announced.
- `role="progressbar"` + `aria-valuenow/min/max` + an `aria-label` on the active
  progress bars.
- `aria-label="Stop all scans"` on the three icon-only stop buttons (`title`
  alone is not a reliable accessible name).

**Navbar:** "Missing" moved from a top-level item into the **Discover** dropdown
(a catalogue-gap report — the same "what am I not listening to yet?" job as
Discover/Upcoming) so it stops competing with Artists/Downloads for the bar.

## Deliberately NOT done (deferred, needs a decision)

- **Dashboard tabs for Recently Added / Recent Scans.** Both are historical logs
  rarely needed at once, so tabs are worthwhile — but `dashboard.css` has a
  desktop right-rail rule (`#recentScansCard { flex: 1 1 auto }`) tied to the
  two-card structure, so merging them changes the flex layout and wants its own
  commit + visual check.
- **Mobile bottom navigation.** The highest-impact mobile item, but it has to
  stack correctly with the fixed player bar (`--footer-stack-height` /
  `--player-bar-height`); worth doing deliberately rather than in this pass.
- **Inline help for Forced/Restart.** Their meaning lives in a `title=`, which
  touch devices cannot reach — but adding visible help text grows the Scanner
  card that the review asked to *condense*, so it needs a design call.

## Verified

- `tests/test_test_site_mobile_and_pwa.py` (new, 32) — scrollbar fix (CSS +
  measurement + wiring), PWA shell incl. manifest JSON parsing and icon
  existence on disk, the three search inputs, the both-container render +
  loading + error paths, the live regions, the progressbar roles, and the
  Missing→Discover move.
- `node --check` clean on `main.js` and `pages/dashboard.js`; manifest parses.
- Sweep of 13 layout/navbar/dashboard/template suites: the only failure is the
  long-documented pre-existing
  `test_static_js_is_not_jinja[static/js/artist_detail.js]` (a LIVE-tree file,
  untouched here — it genuinely is a Jinja page template served as JS, which is
  the bug its own guard exists for) → **0 regressions**.

## Notes

- test_site only, per the standing instruction. Live is the DEFAULT mode, so
  none of this is visible unless `features.use_test_site` is on.
- `test_shared_module_copies_do_not_drift.py` does not exist in this tree, so it
  was skipped in the sweep.
