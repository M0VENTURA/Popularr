# Folder match: fetch the release's MusicBrainz metadata and match it to the tracks

**Date:** 2026-09-23
**Area:** `services/enrichment/musicbrainz_service.py`,
`services/downloads/download_folder_service.py`, `routes/ui_routes.py`

## Reported

> "When an album is in the matched and unmatched folders, when I match it with a
> release, it doesn't seem to be downloading the metadata for the release from
> Musicbrainz and matching it to the tracks before importing into the collection."

> "Also after saving, it shows 'no changes are made'."

## 1. The folder match never fetched the release tracklist

`match_folder_to_release` (the "Confirm" action in **Matched & Unmatched
Folders**, and `/api/downloads/confirm-match`) resolved the release, read each
file's **own** tags, and moved them. It never fetched the release's tracklist
and never wrote MusicBrainz values to the files.

`move_track_to_library` only builds a destination path and `shutil.move`s the
file — **it writes no tags at all**. So a release the user had just picked was
filed under whatever the existing tags happened to say, and a missing title tag
meant the app derived a title from the **filename**.

### The internal fallback could never run

The inline loop that was supposed to cover this was unreachable:

* its keys were `title` / `number`, but the tracks it iterated come from
  `inc=artist-credits+recordings+media` and carry `title` / **`position`** —
  **no MusicBrainz track has a `number` key**, so every comparison failed;
* it only ran at all under `if not title or number is None`, i.e. for files with
  no track number — in which case `number` stayed `None`.

It was dead code covering its own dead case.

### Fix

* **New `match_mb_tracks_to_files(release_metadata, files)`** in
  `services/enrichment/musicbrainz_service.py` — pairs one release's tracklist
  with the folder's files. Per MusicBrainz track:
  1. same track number (and disc, when *both* sides state one),
  2. same normalized title,
  3. fuzzy title similarity ≥ `_TRACKLIST_TITLE_FLOOR` (0.55).

  A local file is claimed at most once, so two identical titles on one release
  resolve in tracklist order instead of both collapsing onto the first file. It
  never invents metadata: an unmatched MusicBrainz track returns
  `matched=False`, and a local file matching no track is simply not represented.

* **New `_apply_release_metadata_to_files`** in `download_folder_service.py` —
  fetches the release, matches it, and writes the result to each file's tags via
  `update_file_metadata`. Returns `{file_path: applied_values}` so the move step
  reuses the **same** values: the tags, the DB row and the destination filename
  all come from one source and cannot disagree. Entirely best-effort — a
  MusicBrainz outage or an unwritable file falls back to the file's own tags.

* `match_folder_to_release` now collects the folder's files once, applies the
  metadata **before** the move, and uses the applied values for the move. The
  response gains `metadata_updated` so a caller can distinguish "matched the
  release but could not write the tags" from "everything went to plan".

### Blank disc numbers are a wildcard

`helpers/metadata_reader` populates `disc_number` for **neither** FLAC nor MP3,
so requiring the disc to match would reject every disc-2 file of a 2-disc
release. A blank disc on the **file** is a wildcard; a disc both sides state must
agree.

### ⚠️ Stored albums are not repaired retroactively

This fixes the import path. Albums already imported with the wrong titles keep
them until they are re-imported or edited.

## 2. "No changes were made." after a save that DID save

In `routes/ui_routes.py::album_detail`, the flash was driven by `updated_count`,
which only counts writes made through the per-track `payload`. Genres are written
by a separate `update_track_genres` call, so:

* **A genres-only save wrote the genres and reported "No changes were made."** —
  now counted (`genre_only_writes`) and reported as
  `"Album genres saved — N track(s) updated."`, with the genre branch placed
  *before* the fallbacks so it cannot be shadowed.
* **An album matching no track rows at all** got the same message, which claimed
  the values were already correct when nothing had even been considered. Now
  warns `"⚠️ No tracks found for this album — nothing could be saved."`
  `"No changes were made."` is retained as the final, genuine no-op case.

### Deliberately NOT "fixed"

`track_comment` is read from the form and never used — the same shape of bug.
It is **not** wired up, because there is no `comment` column on `tracks`
(`db/schema.py`), so `save_to_db` would filter the key out and the change would
be a silent no-op that only *looked* like a fix. The form field is also absent
from every current template (it existed under `old_system/` only). A regression
test pins both facts so nobody "fixes" it by writing to a non-existent column.

## Tests

* `tests/test_folder_match_release_metadata.py` (15) — matcher, tag application,
  and `match_folder_to_release` end-to-end (order of tag-write vs move, fallback
  to own tags, MusicBrainz failure survival).
* `tests/test_album_save_reports_changes.py` (15) — genre counting and branch
  ordering, the empty-album warning, the absent `comment` column, and the
  album-level fields that *are* applied (including `original_year` winning over
  `release_year`).

**Oracle:** fixes stashed → 17 failed / 3 passed / 10 errors; restored → 30 passed.

**Regression sweep** (23 suites, 397 tests): pre-existing failing set IDENTICAL
before and after — 0 regressions. The 10 pre-existing failures are `mutagen`
environment issues in this harness (`'object' object has no attribute 'delall'`),
unrelated to these changes.

## Note

Touches no JS/CSS/templates, so no `test_site` mirror was required. This sits on
top of `6c03adf6` (album-lookup malformed release names).
