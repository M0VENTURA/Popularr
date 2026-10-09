# `(repress)` glued onto the album name on every Navidrome import

**Date:** 2026-10-09
**Area:** `config` / `ui` → album naming · `helpers/normalization_service.py`
**Reported:** *"This keeps importing from Navidrome as Nine Destinies and a
Downfall (repress). I match it to another album and it updates, but then on
the next Navidrome scan it goes back. Navidrome doesn't have it as that."*

## Symptom

The album page showed `Nine Destinies and a Downfall (repress)`. Renaming it
stuck until the next Navidrome scan, which restored the edition form. Every
scan did it again, so the album could never be kept clean — and because the
name changed, Popularr treated it as a *different* album, which is what made
it look like a second record had been created alongside the original.

## Root cause

Navidrome builds `AlbumID3.name` as `Title + " (" + Edition + ")"`. Its own
UI shows the two separately — the title `Nine Destinies and a Downfall` with
`repress` on the line beneath it (that line **is** `Album.Edition`) — but the
Subsonic API only ever hands out the concatenated form.

`scan_artist_to_db` strips that with `strip_album_edition_marker()` before
storing `tracks.album`. The edition vocabulary carried `reissue`, `press`,
`first press`, `limited production` ... but **not `repress`**, so the marker
survived the strip, was written into `tracks.album`, and unconditionally
overwrote the user's rename on the next sync (`album` is deliberately not in
`_POPULARITY_PROTECTED_COLUMNS`).

Two knock-on effects made this look like a much bigger bug:

* **The corrupted row could never self-heal.** `compute_artist_album_diff`
  skips an artist whose stripped Navidrome names match its stripped DB names.
  Before the fix neither side stripped `(repress)`, so the two matched and
  diff mode returned `skip_artist=True` — the import that would have
  repaired the name never ran.
* **A manual rename looked like it reverted.** The rename was fine; the next
  import simply re-wrote the edition form over it.

## Fix

`repress` joins the one shared edition vocabulary in
`helpers/normalization_service.py` (the module's own note: a single keyword
list drives *every* edition decision, so the lists must not drift):

1. `_EDITION_ANNOTATION_KEYWORDS` — so `extract_edition_annotation()`
   recognises the marker.
2. `_TRAILING_MARKER_TOKENS` — so an unbracketed `X Repress` is rewritten
   into bracketed form first, then stripped by the same rules.
3. `_ALBUM_EDITION_STRIP_RE` — both the pinned alternation
   (`reissue|repress(?:ed|ing)?|...`) and the generic modifier-prefixed tail
   (`...|reissue|repress(?:ed|ing)?`), which is what catches
   `(2011 Repress)`.

Verified behaviour:

| input | output |
|---|---|
| `Nine Destinies and a Downfall (repress)` | `Nine Destinies and a Downfall` |
| `Nine Destinies and a Downfall (Repress)` | `Nine Destinies and a Downfall` |
| `Nine Destinies and a Downfall (2011 Repress)` | `Nine Destinies and a Downfall` (year preserved) |
| `Nine Destinies and a Downfall Repress` | `Nine Destinies and a Downfall` |
| `Nine Destinies and a Downfall (2016)` | unchanged — a year is not an edition |
| `Repress` | unchanged — a title that *is* the marker survives |
| `Unplugged (Live)` | unchanged — recordings are never stripped |

Nothing else changes: the strip only removes *bracketed trailing* annotations
naming a pressing/edition, and an all-marker name falls back to the original.

## Self-heal

A row stored before this fix keeps `(repress)`. After the fix
`compute_artist_album_diff` sees raw DB `...(repress)` against stripped Navidrome
`Nine Destinies and a Downfall`, reports it **changed**, and the import runs
and rewrites `tracks.album` to the clean title. A diff scan therefore repairs
existing rows with no manual action.

## Tests

`tests/test_album_release_title_naming.py`

* `TestStripAlbumEditionMarker::test_strips_repress_marker` — bracketed,
  capitalised, dated, unbracketed, idempotent, and marker-only forms.
* `TestStripAlbumEditionMarker::test_preserves_recordings_and_plain_year_markers`
  — guards against the keyword widening past pressings.
* `TestArtistAlbumNameDiffEditionStripping::test_db_row_still_carrying_repress_is_flagged_changed`
  — the diff must flag the corrupted row as changed (before the fix this
  assertion fails with `skip is True`: the row could never be repaired).

Oracle: reverting `helpers/normalization_service.py` fails exactly those two
new tests; restoring makes them pass.
Sweep: 19 normalization-touching test files, baseline vs changed —
`Compare-Object` on the sorted failure lists = `(none)`.
