# Mobile layout density — Album & Artist pages

**Date:** 2026-10-14
**Scope:** `test_site/` **only** (plus its tests)

## Why the scope is `test_site/` only

The mobile review targeted the rebuilt pages, which are what `features.use_test_site`
serves. The live tree (`templates/` + `static/`) was left byte-identical, so switching
the cutover flag off still yields the old layout — no half-migrated state in between.

Consequence to keep in mind: `versioned_static()` searches `test_site/static` **first**,
so a rule added here and *not* to the live copy is invisible only when the flag is off.
If the cutover is ever reversed, these rules must be ported.

## The five findings, and what changed

### 1. Artist page: the release-row actions were 24px wide

Three tight icon buttons (`open`, `edit`, `tracklist`) at ~24×24px — well under the
44px touch minimum, and adjacent, so mis-taps were constant.

- `test_site/templates/components/_release_section.html`: every inline button gains
  `release-inline-only`, and a `.release-actions-overflow.d-md-none` **⋮ dropdown** is
  appended carrying the same actions. Its items carry the **same classes and data
  attributes** as the inline buttons.
- `test_site/static/css/artist.css` (`@media (max-width: 767.98px)`):
  `.release-inline-only { display: none }` and
  `.release-actions-overflow > .btn { min-width: 44px; min-height: 44px }`.

### 2. …and the ⋮ menu needed wiring without touching the shared module

`static/js/artist-releases.js` and `test_site/static/js/pages/artist-releases.js` are
pinned **byte-identical** by
`tests/test_artist_page_contract.py::test_shared_module_copies_do_not_drift`. Wiring the
menu from there would either drift the two trees apart or force a live-tree edit — both
out of scope.

Instead `test_site/static/js/pages/artist-detail-extras.js` (test_site-only, already
loaded on the artist page) **forwards** a click on `.release-actions-overflow .dropdown-item`
to the matching `.release-inline-only.<class>` button in the same `.release-item`, via
`twin.click()`. The shared module's existing bindings then do the work — one set of
bindings, two UIs.

`twin.click()` works even though the inline button is `display:none` on phones;
visibility does not gate a programmatic click. The `<a>` twin (Open album) is followed
by the same call, so `e.preventDefault()` keeps the dropdown's own href from firing twice.

### 3. Artist page: "In Library" was pure noise on phones

In a library app "In Library" is the default state, not news. `Missing` / `Upcoming`
are the exceptions and keep their badges.

- The single badge span was split into **two plain spans** so each class sits literally
  in its attribute — the release-CSS agreement guard cannot see a class that only
  appears inside a `{{ }}` conditional, and would flag the rule as dead CSS.
- `release-badge-library` (the success branch only) is hidden in the phone media query.

### 4. Artist page: jump chips wrapped into three lines

The nowrap/scroll-snap rule existed only below **576px**, so a 600px phone still
stacked the category chips above the album list. Moved into the 767.98px block —
**one swipeable row** on phones and small tablets.

### 5. Bios were a wall of text in both places

The rebuilt tree's hero used `.bio-clamp-2`, which this stylesheet **never defined**, so
the bio rendered at full length; the About tab's Read More toggled `.expanded` against
no release rule (a decorative button).

- `.bio-clamp, .bio-clamp > p` → 3-line clamp. The **child** selector is load-bearing:
  `-webkit-line-clamp` does not apply through a block `<p>`, so clamping only the
  wrapper was a no-op.
- `.bio-clamp.expanded, .bio-clamp.expanded > p` → `-webkit-line-clamp: unset`.
- `.bio-clamp-2` (2-line) and `.bio-clamp-4` (4-line) both defined;
  `artist_detail_v2.html`'s hero now uses the 4-line clamp, matching the requested
  "3 or 4 lines before [more]".

## Tests

`tests/test_mobile_layout_density.py` — **33 tests**.

- The CSS parametrizations are **test_site-only**; the artist-CSS agreement check is a
  single assertion against the rebuilt copy, since the live copy legitimately differs.
- `TestTheOverflowMenuIsForwarded::test_the_shared_module_is_untouched` is a deliberate
  **CONTROL**: it asserts both copies of `artist-releases.js` are unchanged *and* carry
  no mobile wiring. It passes in both the baseline and the fixed state — the oracle
  therefore reports 32 failed / 1 passed, and that one pass is expected, not a gap.

Oracle (sources reverted, suite kept): **32 failed, 1 passed**.

Sweeps green afterwards: `test_artist_page_contract` 26 · `test_release_section_auto_collapse` 37 ·
`test_layout_css_js_template_integrity` 260+1skip · `test_rebuilt_pages_have_no_dead_buttons` 6 ·
`test_templates_tag_attribute_integrity` 120 · `test_test_site_mobile_and_pwa` 72 ·
`test_album_missing_rows_line_up` 22 · `test_album_page_missing_tracks_list` 18 ·
`test_missing_track_queue_state_ui` 20 · `test_album_disc_group_headers` 35 ·
`test_album_bulk_rename` 17 · `test_artist_edit_release_modal` 21 ·
`test_artist_import_release_queues_a_download` 7.

## Files

- `test_site/templates/Pages/album_detail.html`
- `test_site/templates/Pages/artist_detail_v2.html`
- `test_site/templates/components/_release_section.html`
- `test_site/static/css/popularr.css`
- `test_site/static/css/artist.css`
- `test_site/static/js/pages/album.js`
- `test_site/static/js/pages/artist-detail-extras.js`
- `tests/test_mobile_layout_density.py`
