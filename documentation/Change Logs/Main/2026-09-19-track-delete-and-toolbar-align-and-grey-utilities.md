# Track-delete 404, scan-toolbar alignment, and the grey/blue patches

Three unrelated-looking reports, all traced to separate root causes. Two of
them had latent second defects underneath.

---

## 1. `404` on `/track/<track_id>/delete`

### Symptom

Clicking the trash icon on a track row on the album page navigated to
`/track/<id>/delete` and 404'd.

### Root cause

**No route with that shape has ever existed.** Every delete in this app is a
`POST /api/...` endpoint:

| Endpoint | Used by |
|---|---|
| `POST /api/artist/corrections/delete-track` | artist corrections page |
| `POST /api/album/bulk-delete` | album bulk flow |
| `POST /api/v1/albums/<a>/<al>/bulk-delete` | album bulk flow (v1) |
| `POST /api/downloads/folder/<path>/track/delete` | queue folders |

There was **no single-track delete endpoint at all**, and both album-page
controllers navigated the browser to a URL that was never registered:

- `static/js/album_detail.js` — `window.location.href = \`/track/${id}/delete\``
- `test_site/static/js/pages/album.js` — `global.location.href = \`/track/${encodeURIComponent(id)}/delete\``

The comment left above the live version ("Fallback directly to the server's
delete route view") suggests an endpoint was assumed to exist. A stale comment
in `routes/api_v1/albums.py` referring to "the existing helper used by the
single-track `/track/<id>/delete` route" is the same stale assumption.

### Second defect found underneath

`routes/api_v1/albums.py` **was never imported** by `routes/api_v1/__init__.py`,
which only did `from . import tracks, artists`. The module's own docstring asks
for the import. Consequence: **none** of its routes were registered, so
`/api/v1/albums/.../bulk-delete` and `/api/v1/albums/.../musicbrainz-compare`
404'd too, despite the file existing and the album page calling them.

### Fix

- New `POST /api/v1/tracks/<track_id>/delete` in `routes/api_v1/tracks.py`,
  delegating to `services.metadata.artist_service.delete_track` — the same
  service the artist corrections route uses, so single-track delete behaves
  identically on both pages (file removal, the `__queued_for_download__` stub
  guard, the logging) instead of growing a second implementation. Body
  `{"delete_file": bool}`, defaulting to `true` to match the album bulk flow.
- Added the missing `albums` import in `routes/api_v1/__init__.py`.
- Both album controllers now POST to the new endpoint and reload.

### Why POST, not GET

The old code made a destructive state change reachable by a **GET** — i.e. by
a link prefetch, a crawler, or a bookmark. The endpoint is POST-only and the
browser-navigation pattern is gone. A test asserts GET is not accepted, because
"re-add a convenient GET alias" is the most likely way this regresses.

---

## 2. Force / Restart / scan buttons misaligned

### Root cause — two independent layout bugs

**a) The `.scan-toolbar` class was missing from the album form.**
`popularr.css` pins scan-toolbar checkboxes to `1.25rem × 1.25rem` via
`.scan-toolbar .form-check-input` (and `form .form-check-input[name="force"]`).
That rule exists precisely because Bootstrap's `form-check-input` is `1em`, so
it shrinks with the surrounding label — the album Force label carried
`small`, and the tickbox shrank to match. The artist form had `scan-toolbar`;
the album form did not.

**b) A fixed width squashed the Restart checkbox.**
`components/_scan_selector.html` emits **both** the scan-type `<select>` **and**
the "Restart" checkbox. Both pages wrapped that include in a narrow container
(`width: 140px` on the album; `flex-grow-1 max-width: 160px` on the artist), so
Restart was squeezed into a few pixels and wrapped onto its own line. The
select already sets its own `min-width: 140px`, so the wrapper was redundant.

**c) The album Force control was also a box.**
`px-2 py-1 bg-dark rounded border border-secondary` made it taller than every
sibling in the row, so it could not align regardless of the checkbox size.

### Fix

- Added `scan-toolbar` to `#albumScanForm` in both album templates.
- Removed the fixed-width wrappers on both pages (album and artist).
- Album Force is now a plain `form-check form-check-inline m-0`, matching the
  artist page; Run is `btn-sm` to match.
- The artist form gained `flex-wrap` so it degrades instead of overflowing.

---

## 3. Grey patches on the artist and album pages

### Root cause — `bg-secondary` was the one un-themed utility

This theme's surfaces are `--primary-bg #121212`, `--secondary-bg #1e1e1e`,
`--tertiary-bg #282828`. Bootstrap's `bg-secondary` is **`#6c757d`** — lighter
than all three — and `border-secondary` is the same mid-grey but carries
`!important`, so it beat this file's plain `border-color` rules. `bg-dark`
(`#212529`) is blue-tinted and also `!important`.

Affected on the reported pages:

- `artist_detail_v2.html` — the tab strip was `nav-pills nav-fill bg-secondary
  p-1 rounded` (a pale grey band) and the "Covers by N" / band-member badges are
  `badge bg-secondary`.
- `album_detail.html` (both trees) — `badge bg-secondary` on the Spotify album
  type, the `border-end border-secondary` hero metric dividers, and every
  `form-control ... border-secondary` outline.

### Fix (both `popularr.css` trees)

- `.bg-secondary` → `var(--tertiary-bg)`, so "neutral" means "matches the UI".
- `.bg-dark` → `var(--secondary-bg)`, **not** `--primary-bg`: each call site is
  already a card/modal/control surface, so `--primary-bg` would have made them
  darker than the panel they sit on — trading a grey patch for a black one.
  (`.card.bg-dark` / `.card-header.bg-dark` were never affected — the file's own
  `!important` rule already won those; they are not part of this fix.)
- `.border-secondary` → `color-mix(in srgb, white 12%, transparent)`, the same
  hairline `.card` uses.
- `.text-secondary` — overrode **`--bs-secondary-rgb`**, not the `color`
  shorthand. Bootstrap declares
  `.text-secondary { color: rgba(var(--bs-secondary-rgb), var(--bs-text-opacity)) !important }`,
  so re-pointing the triple fixes the colour while `text-opacity-75` (used by
  the playlist track rows) keeps working. A plain `color: … !important` would
  have silently disabled text-opacity app-wide.

### Second defect found underneath — the live `.nav-pills` block was incomplete

The LIVE `static/css/popularr.css` had **only** `.nav-pills .nav-link.active`.
An **inactive** pill therefore had no colour of its own and fell through to
Bootstrap's default link blue. Two pages had already worked around this locally
(`album_detail.html`'s `#albumPageTabs .nav-link`, `track_detail.html`'s inline
block) — which is why it went unnoticed — but `artist_detail_v2.html` had no
local rule, so its unselected tabs rendered blue.

Fixed by porting the rebuilt tree's complete `.nav-pills` block (resting,
hover, focus-visible, disabled, active, active:hover, active:focus-visible).
The `active:hover` rule needs `!important` because `!important` outranks
specificity — a plain declaration there would never apply. Also added the
`.nav-pills.nav-segmented` block for the same reason: `artist_detail_v2.html`
lives in the LIVE tree but is rendered in **both** modes, so the rule must exist
on both sides or the artist page loses its tab styling when the rebuilt UI is
switched off.

The artist page's tab strip was switched from `bg-secondary p-1 rounded` to
`nav-segmented`, matching `album_detail.html`, `dashboard.html` and every other
tabbed page.

---

## Tests

`tests/test_track_delete_route.py` — pins down all of it:

- `POST /api/v1/tracks/<track_id>/delete` is registered, and is **POST-only**
- no GET route exists at the old `/track/<id>/delete` path
- `routes/api_v1/albums.py`'s routes are registered, and the package still
  imports `albums` (the silent-404 bug, guarded against regression)
- unknown track → 404 (status passed through from the service, not flattened)
- empty body / no body at all → clean 404
- **both** album JS controllers no longer navigate to the dead URL and do call
  the real endpoint — both trees are parametrised, since fixing one tree and
  not the other is how this bug happened in the first place

## Config

No new config keys.

## Known follow-ups (not in this change)

- `test_site/templates/Pages/artist_detail.html` and the live
  `templates/pages/artist_detail.html` both use **bare** endpoint names
  (`url_for('artists')` instead of `'ui.artists'`). This is the same class of
  bug as the shadowed `downloads/monitor.html` page, but shadowing is not a
  valid fix because **both** trees are wrong. It is latent only because the
  route renders `artist_detail_v2.html`. Needs namespacing across ~15 call
  sites; deliberately left alone here.
- `test_site/templates/Pages/downloads/monitor.html` still needs deleting (the
  environment cannot delete from the vfs tree; it is shadowed for now).
- `test_site/static/js/pages/artist.js` still holds a static
  `CATEGORY_TO_SECTION` map (6 keys) for JS-injected missing releases, which
  will not track `services/catalog/release_categories.py`.
- `static/css/popularr.css` can now drop the local `#albumPageTabs .nav-link`
  workarounds and `track_detail.html` its inline `.nav-pills` block, since both
  are now covered globally.
