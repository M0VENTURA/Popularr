# Navidrome re-import no longer blanks file-only tags, and the missing-fields mode works again (2026-10-04)

**Report:** "Can you confirm that this is the information being pulled from
Navidrome during a Navidrome scan? Does it import missing fields if the
release is already there from navidrome?"

While confirming field-by-field against the wire, two defects turned up —
both verified with a real DB round-trip before fixing.

## Root cause

### 1. Re-import BLANKED tags Navidrome's API can never carry

Navidrome stores the file's raw tags internally (`model.MediaFile.Tags`) but
serialises only the Subsonic `Child` / `AlbumID3` structs
(`server/subsonic/responses/responses.go`). `barcode`, `tracktotal`,
`disctotal`, `copyright`, `language`, `asin`, `work`,
`musicbrainz_albumstatus` and `albumversion` are **not** in those structs, so
`extract_track_metadata` always produced `""` for them — and
`build_track_payload` sent `""`, which `_execute_save` writes as
`col=EXCLUDED.col`.

They were missing from `PRESERVE_WHEN_EMPTY_FIELDS`, so every import wiped the
stored value. Reproduced: a row seeded with barcode `8809928957340`,
tracktotal `17`, disctotal `2`, copyright, language, asin, work, status and
version came back **all `''`** after re-importing the same track.

This silently undid album-page/download-import enrichment — including the
track totals the tag-parity work (`006902ed`) had just started applying.

### 2. The "missing fields" scan mode imported nothing

`prefetch_artist_state` returned `albums_needing_reimport: set()`
unconditionally — the port from `old_system/deprecated/navidrome_import.py`
dropped the population half. With `filter_missing=True`,
`should_skip_album` hit its "not in albums_needing_reimport" branch and
skipped **every** album, so `mode=missing` was a silent no-op.

### 3. Two wire keys were never read

- Navidrome sends `explicitStatus` (camelCase) on `Child`; the extractor read
  only `explicitstatus`/`explicit`/`itunesadvisory`, so the column could
  never refresh.
- `AlbumID3.version` carries the album version
  (`helpers.go`: `dir.Version = album.Tags.First(model.TagAlbumVersion)`),
  but only the *song* was searched — so `albumversion` could never refresh
  either.

## Changes

- `services/scanning/payload_builder.py` — `PRESERVE_WHEN_EMPTY_FIELDS` now
  also covers `barcode`, `asin`, `tracktotal`, `disctotal`, `copyright`,
  `language`, `discsubtitle`, `albumversion`, `musicbrainz_albumstatus`,
  `work`, with a comment naming the wire set (`Child` + `AlbumID3`) as the
  authority for what is unreachable.
- `services/scanning/navidrome_import.py` — `prefetch_artist_state` now
  selects `duration, track_number, year, file_path` and flags albums where
  any of them is NULL/empty/non-positive (all four ARE on the wire, so a
  re-import genuinely fills them). The flag is keyed by
  `strip_album_edition_marker`, matching `should_skip_album`.
- `services/scanning/metadata_extractor.py` — reads `explicitStatus` as a
  wire key; `extract_album_metadata` collects `album.version` →
  `albumversion`.

## Notes

- Fields that ARE on the wire are still refreshed normally: the probe
  confirmed `isrc` (`Child.isrc[]`) and `explicitStatus` overwrite stale
  stored values after the fix, and identity/year/duration still update.
- `isrc` was deliberately NOT added to the preserve set — it rides
  `Child.isrc[]`, so Navidrome can re-read it.

## Tests

- `tests/test_navidrome_reimport_preserves_tags.py` — 12 tests:
  `TestReimportPreservesUnreachableTags` (round-trip: file-only tags survive,
  wire fields still refresh, identity still updates),
  `TestExplicitStatusWireKey`, `TestAlbumVersionComesFromAlbumID3`,
  `TestMissingFieldsMode` (flagged/not-flagged, isolation from other artists,
  `should_skip_album` contract).
- Oracle: stashing the three sources fails 6 of the 12 (the rest are guards
  already true at baseline); all markers restored after pop.
- Scan/navidrome test subset: 42 failures — an exact match for the 42
  baseline failures in the same files (**0 new, 0 fixed**).
