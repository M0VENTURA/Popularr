# Custom Album: its own Save button at the top of the Edit Album tab (2026-10-14)

## What was reported
> When selecting this and saving, it time out as it tries to update all the
> files. As it's a setting only, there should be a separate save button under
> the custom album setting. It may be better placed to move this to the top of
> the Edit Album Metadata page, then the info below it.

## Root cause
The "Do not search album release" checkbox lived **inside
`#albumMetadataForm`**, so ticking one boolean cost the WHOLE save: the
album → tracks DB fan-out plus a concurrent write of every file tag
(`_write_album_track_files_concurrently`) — a timeout on a large album for a
value that is a single album-wide `UPDATE tracks SET album_lookup_skip = '1'`.
The write itself was never the problem; the form it rode on was.

## The change (test_site tree; the live tree never had this block)

1. **Own card, own Save button.** The block moved OUT of
   `#albumMetadataForm` into its own card with a **Save Custom Album Setting**
   button posting to the new `POST /api/album/lookup-skip` — one album-wide
   UPDATE, no fan-out, no file writes. Its help text no longer claims
   "Saved with the rest of this form."
2. **Moved to the top** of the Edit Album Metadata tab: the card now renders
   BEFORE the big form, with the rest of the metadata below it. The CSV
   tracklist uploader travels with the card (it was already an independent
   `type="button"` action).
3. **The big save is now presence-gated** (`routes/ui_routes.py`): the flag is
   only written when `album_lookup_skip` was actually POSTED. Without this,
   every big save — which no longer carries the field — would have computed
   `False` and **cleared** the flag. It also fixes a latent cross-tree bug:
   the live tree's form NEVER had the field, so a live-mode save would have
   cleared a custom album marked from the rebuilt page.
4. **New endpoint** `POST /api/album/lookup-skip` (`routes/album_routes.py`),
   modelled on `/tracklist-csv`: validates artist/album (a 400 must not reach
   the service — scoping to an empty artist would update every row) and
   delegates to the same `set_album_lookup_skip` the album save uses (one
   writer, one scope rule).

## Tests
- **New** `tests/test_album_lookup_skip_separate_save.py` (15): placement
  (checkbox/button/CSV card all BEFORE `#albumMetadataForm`, exactly one
  checkbox after the move, old wording gone); the presence gate (exists,
  precedes the write, `album_data` still carries the flag); endpoint contract
  (route + delegation + page posts + exported global + busy state); behavioural
  via the test client (saves `True`, saves `False`, empty artist → 400).
- **Updated** `tests/test_album_lookup_skip.py`'s markup assertion: it pinned
  `name="album_lookup_skip"` — the attribute is deliberately GONE (a form name
  outside a form is dead markup that wrongly implies the field posts with the
  form). Rewritten to assert the id + absence of the name.

## Verification
`test_album_lookup_skip*` 37 passed. Regression sweep (one process per file,
all suites reading the edited template/JS/routes): dead-buttons 6, template
tag integrity 120, album-save suites (genre-precedence 29, live-markers 51,
phases 25, reports-changes 18, edit-tab 5, review-persists 29), album page
suites (missing-rows 22, missing-list 18, disc-headers 35, bulk-edit 10,
type-select 28, art-upload 4, duration 10, findings 19, release-picker 3,
lookup 8, findings 30, metadata-review 26, save-return 5) — **all green, 0
regressions**. `node --check` + `ast.parse` clean on the edited files.
