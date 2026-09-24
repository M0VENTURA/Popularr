# Buttons that did nothing — a template has no compiler, so nothing watched them

**Date:** 2026-09-24
**Area:** `test_site/templates/**`, `test_site/static/js/services/album-soulseek.js`,
`static/js/album-soulseek.js`, both `base.html`

## Reported

> "Can you check the manual soulseek search on the test_site? The search button
> doesn't do anything when clicked."

## First: the reported button works

The **"Manual Soulseek Search"** modal's Search button
(`#soulseekManualSearchBtn`) is bound correctly. I checked rather than read,
because a click that does nothing is exactly what a *missing* binding looks
like:

1. Loaded `services/slskd.js` into a stubbed DOM, fired `DOMContentLoaded`,
   confirmed one `click` listener attached, clicked it, and observed
   `POST /api/slskd/search {"query":"Daft Punk Get Lucky"}`.
2. Rendered `/downloads` with test-site mode active and confirmed the modal,
   the button and `js/services/slskd.js` are all served, and that the served
   `slskd.js` contains the binding.

`slskd.js` and that modal partial both date from `105a7ae1` (2026-09-18). **If
the running build predates that commit it serves the legacy
`static/js/downloads.js` instead**, which is the most likely explanation for the
symptom — flagged to the user rather than assumed.

## But the question found the real problem

Rather than stop at "your build is stale", I audited **every inline handler on
every rebuilt page**: walk the Jinja include/extends graph, work out which JS
the page actually loads, harvest the function names those files define, and
report any `onclick="fn()"` whose `fn` is not among them.

**A template has no compiler.** TypeScript catches a misspelt import; a Jinja
template plus `onclick="fn()"` has nothing watching it, so the failure is silent
— the browser throws `ReferenceError` into a console nobody has open and the
click is simply inert.

The scan found **29 inert buttons across 5 pages**, in three distinct shapes:

| Shape | Example | Fix |
|---|---|---|
| Defined and served, just not loaded *by this page* | `performSlskdSearch` | load the script |
| Defined in a script a **shared partial's** host page doesn't load | `createPlaylist` in `_playlists.html` | needs a page-owned controller |
| Defined nowhere in either tree | `openAlbumArtModal`, `alignTracklist`, `alignMbids`… | product decision |

### Fixed here

**1. `performSlskdSearch` — the Soulseek search buttons (the reported area).**
It is published as `global.performSlskdSearch` by
`js/pages/soulseek-search.js` and reads `#slskdSearchInput` / `#slskdResults`,
which is exactly what the download modal's Soulseek tab renders. But
`artist_detail.html`, `artist_detail_v2.html` and `downloads/monitor.html` all
render a Search button calling it and **none of them loaded that file** — three
pages with a dead Soulseek search, while the LIVE tree worked because
`static/js/artist_detail.js` still defines it. A refactor regression, invisible
until someone clicked. All three now load `soulseek-search.js`.

⚠️ Verified `soulseek-search.js` is SAFE to load on those pages: every element
lookup is guarded (`updateSelectedButton`, `setCounts`, `setStatusText`,
`renderResponses`, `showError` all no-op when the node is absent), and
`monitor.html` does **not** include `_soulseek.html`, so there is no
double-binding of `#slskdSearchBtn`.

**2. `openSlskdSearchAlbum` — the album page's "Search Soulseek".**
The call site existed in BOTH `templates/pages/album_detail.html` and the
rebuilt twin, but the function was defined only in
`old_system/templates/album.html` and never carried across — so the Actions-menu
item threw `ReferenceError`. New `js/services/album-soulseek.js` (rebuilt tree,
loaded from `base.html`) plus a `js/album-soulseek.js` mirror for the live tree.

It navigates to `/downloads/search?q=…`, not the original's
`/downloads?search=…`: that page no longer reads a `search` parameter, and `?q=`
is what every other "Search Soulseek" link in the app already uses
(`pages/track.js:374`), with `pages/search.js` prefilling and firing from it.
Keeping this file's own ampersand stripping rather than inventing a second
matcher means the `&` handling (slskd returns ZERO results for `&`, not an
error) behaves identically no matter which link was clicked.

### Tracking the rest

The 25 remaining inert buttons need **product decisions, not mechanical edits**
— e.g. album art editing was never ported at all. They are listed in
`_OBSOLETE_BUTTONS` in the new test, each with the evidence it is dead.

Two properties stop that register becoming a blanket suppression:

* **`test_the_register_has_no_stale_entries`** fails when an entry is no longer
  dead, so implementing one of these functions forces the entry to be deleted.
  This caught my own bogus `track_detail.html` entry during development.
* **`test_the_register_only_covers_real_dead_buttons`** fails on a typo'd page
  path, which would otherwise silently suppress a live button elsewhere.

## Tests

`tests/test_rebuilt_pages_have_no_dead_buttons.py` (**6**):

* the regression guard — any NEW unreachable handler fails the build, naming the
  function and the template line;
* the register cannot rot (stale-entry check);
* the register cannot misfire (unknown-page check);
* the harness can find the pages at all, so the assertions cannot pass vacuously;
* the script resolver applies the live-tree fallback — ⚠️ `js/downloads.js`
  exists ONLY in `static/`, so ignoring the fallback reports working pages as
  broken (an earlier JS version of this audit did exactly that, and also counted
  handlers inside comments: 38 reported vs 29 real);
* comment stripping — an assertion over raw source matches the comment
  documenting the trap it guards, a mistake this codebase has made four times.

Mutation-verified: injecting a new dead button fails naming it; unregistering
`alignTracklist` fails three ways; implementing `alignTracklist` fails the
stale-entry check.
