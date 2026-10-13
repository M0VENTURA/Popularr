# Mobile app shell stays visible; main-site search promoted; artists page speeds up

**Date:** 2026-10-13
**Area:** test_site UI (mobile bottom nav, main-site navbar search, artists page)

## The reports

> 1. "When going to the Artists button on the bottom row, the toolbar disappears
>    while the page loads. Can this be kept open all the time when on mobile view."
> 2. "The artists page also takes a while to generate, can the 'needs repair'
>    function wait until the page is loaded to start?"
> 3. "Can you recommend a better way for the search to display so it's more
>    prominent on the toolbar? Even if the toolbar is made bigger?" — clarified:
>    the bigger search is for the MAIN toolbar (the top navbar), not the mobile
>    bottom bar.

## 1 — The bottom row no longer vanishes while a slow page loads

**Root cause:** the bottom nav was the LAST element of `<body>`. The artists
page server-renders ~1000 rows, so on a slow load the browser painted the bar
LAST, seconds after everything else — reading as "the toolbar disappears while
the page loads". The top navbar is first in the DOM, which is why ONLY the
bottom row vanished. **Paint order follows parse order.**

**Fix:** the bar now sits BEFORE `<main>` in the document (`base.html`) — so it
parses/paints in the very first chunk — and is `position: fixed; bottom: 0`
(shown below lg), so it is glued to the viewport no matter how far `main`
scrolls. The old flex-footer design let `main` shrink; the compensation now
lives in `body { padding-bottom: calc(var(--bottom-nav-height) + …) }` inside
the same below-lg media query, so content scrolls ABOVE the bar and can never
hide behind it. `--footer-stack-height` (sticky-save-bar, dashboard min-height)
still includes the nav height, so its maths is unchanged.

The five destinations (Home/Artists/Downloads/Playlists/Search) are unchanged.

## 2 — The "needs repair" (corrections) fetch waits for the page to load

**Root cause:** `pages/artists.js` called `loadCorrections()` at
`DOMContentLoaded`, so the heavy aggregate (`GET /api/artists/corrections`)
competed with first paint AND with the page's ~1000 avatar image requests.

**Fix:** the fetch now runs on `window.load` inside a `requestIdleCallback`
(fallback: a macrotask), so the page renders first and the "needs fixing"
badges pop in quietly afterwards. The toggle is still wired immediately.

**Perf companion:** the ~1000 avatar `<img>`s are now `loading="lazy"
decoding="async"`, so above-the-fold rows paint fast and `window.load` fires
without waiting for every off-screen image — which the corrections deferral
also depends on for its "page is loaded" signal.

## 3 — The main-site search is the toolbar's hero

The navbar's persistent search field was `input-group-sm` — visually a
secondary control. It is now `input-group-lg` with a new `.nav-search-input-group`
pill wrapper: rounded-cap corners, a soft green glow on focus, and the icon +
button sized to match the field. The toolbar is allowed to grow taller to fit
it: the mobile `--navbar-height` fallbacks are bumped (132px / 124px) to clear
the larger field at first paint (main.js re-measures and overwrites the token
immediately anyway). The mobile bottom bar is deliberately UNCHANGED in this
respect — the user asked for the bigger search on the main toolbar.

## Tests

`tests/test_test_site_mobile_and_pwa.py`:

* `TestTheBottomNavIsPaintedFirst` — the bar parses before `<main>`
  (⚠️ matched against the real `<main class="container-fluid">` opening tag,
  not the bare `<main` — the explanatory comment above the bar says "before
  <main>" and a bare substring search hits the comment); the bar is
  `position: fixed; bottom: 0`; `body` reserves its height below lg.
* `TestTheMainSiteSearchIsProminent` — the navbar field is `input-group-lg`
  with the pill wrapper and focus glow; mobile navbar-height fallbacks cover
  the larger field.
* `TestArtistsCorrectionsWaitForLoad` — the fetch isn't called synchronously
  from `initCorrections`; it's bound to `window.load` + `requestIdleCallback`;
  the avatars are lazy.
* Updated `TestMobileBottomNav::test_it_is_fixed_but_the_body_reserves_its_height`
  (was "flex footer item, not fixed") and its class docstring to the new design.

## Regression notes

Sweep: `test_test_site_mobile_and_pwa`, `test_mobile_navbar_no_flicker`,
`test_layout_css_js_template_integrity`, `test_navbar_system_menu_and_search_shortcut`,
`test_rebuilt_pages_have_no_dead_buttons`, `test_artist_page_contract`,
`test_corrections_disc_and_featured_artists`, `test_static_js_is_not_jinja`,
`test_unified_search_enter_submits`, `test_dashboard_pending_approval`,
`test_test_site_shadowing`, `test_scan_preflight_gate` → **620 passed, 1 skipped,
1 failed = the long-documented pre-existing `test_static_js_is_not_jinja[
static\js\artist_detail.js]` (LIVE tree, untouched by this change)** →
**0 regressions.**

`node --check` clean on `artists.js`. The navbar-height feedback-loop probe
(`test_mobile_navbar_no_flicker`) still stabilises with the larger field.