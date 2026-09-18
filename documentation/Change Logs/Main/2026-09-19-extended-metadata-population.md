# Extended Metadata not populated during a scan

**Date:** 2026-09-19
**Area:** `popularity` / `musicbrainz` / `db`

## Reported symptom

> The extended metadata doesn't get populated during a scan
>
> ###### Extended Metadata
> Record Label / Catalog Number / Barcode / Release Date / Media Format /
> Release Country

All six fields on the album page's **Extended Metadata** panel stayed blank
after a scan, even for albums that had a MusicBrainz release matched.

## Root cause — two independent gaps

### 1. The values were never extracted from MusicBrainz

`fetch_musicbrainz_release_metadata` (`services/enrichment/musicbrainz_service.py`)
requested:

```python
inc="recordings+artist-credits+release-groups+media"
```

`labels` is **absent**, so MusicBrainz never returns `label-info` — and because
passing an explicit `inc` **bypasses** the HTTP client's
`_RELEASE_INC_SUPERSET` fallback (which *does* include `labels`), the label data
was not fetched even though the client knows how to.

The function also had no parsing for any of the six fields. A repo-wide search
confirmed it:

```
git grep "label-info\|catalog-number" origin/develop -- services/ api_clients/
→ zero hits
```

So `recordlabel`, `catalognumber`, `barcode`, `releasedate`, `media` and
`releasecountry` were never produced in the first place.

### 2. Nothing in the scan wrote them

Searching the whole popularity pipeline for those column names returned **one**
hit: `album_stage`'s `releasecountry` backfill — and that set the column from
the **ARTIST's area**, not from the release:

```python
"UPDATE tracks SET releasecountry = :country ..."   # metadata["country"] = artist area
```

So "Release Country" showed the artist's country (or nothing), and the other
five were never written at all.

The UI side was already correct — `routes/ui_routes.py` reads
`first_value("recordlabel")` etc. and the template renders
`album_data.<field>`. The gap was entirely upstream.

## Fix

### `services/enrichment/musicbrainz_service.py`

- Added `labels` to the release `inc`.
- New `_release_extended_fields(release, media)` reads all six, named after the
  `tracks` columns so the scan can persist them without a mapping table:

  | Column | Source |
  |---|---|
  | `recordlabel` | `label-info[].label.name` |
  | `catalognumber` | `label-info[].catalog-number` |
  | `barcode` | `release.barcode` |
  | `releasedate` | `release.date` |
  | `media` | `media[].format` |
  | `releasecountry` | `release-events[].area.name` |

- `_unique_join` de-duplicates with `" / "`, because MusicBrainz repeats a value
  per medium (a 2-CD release yields `"CD"` twice) and per label entry, while the
  columns are single text fields.
- `_release_event_country` picks the **earliest** release event (a release can
  be issued in several countries), falling back to the singular `country` field
  that partial payloads still use.
- Missing data yields `""`, never a placeholder — a placeholder would be written
  to the audio files as a real tag.

### `services/popularity/stages/album_stage.py`

- New `_persist_release_extended_fields(artist, album, release_mbid="")` writes
  all six onto **every track of the album**, so they agree. They describe the
  RELEASE, not an individual track, so an album-wide write is correct and also
  satisfies the project rule that album-level updates fan out to all tracks.
- **Fill-only**: each column is written only when currently empty
  (`CASE WHEN COALESCE(NULLIF(TRIM(col), ''), '') = '' THEN :col ELSE col END`),
  so a manual edit on the album page is never clobbered by a rescan. Columns are
  considered independently, so a partially-filled album still gains the rest.
- New `_resolve_album_release_mbid(artist, album)` reads the release id from the
  DB when it is not passed in. This matters because the existing release-MBID
  block **early-returns** when every track already has an MBID — i.e. on every
  rescan. Without this lookup the fields would only ever fill on a first scan.
- Called from `_run_full_enrichment` **before** the artist-country backfill, so a
  real release country wins over the artist's area.

## Tests

`tests/test_release_extended_metadata.py` — 16 tests.

- `TestReleaseExtendedFieldsExtraction` (8): all six extracted; release country
  uses the earliest event; falls back to the singular `country`; repeated media
  formats de-duplicated; multiple labels/catalog numbers joined; missing fields
  are `""` **not** placeholders; snake_case `label_info` variants accepted;
  malformed `label-info` does not crash.
- `TestFetchReturnsAndRequestsExtendedFields` (2): `labels` is requested **and**
  all six are returned; the pre-existing identity contract (`release_mbid`,
  `release_group_mbid`, `album_artist_mbid`, `disc_count`, `tracks`) is intact.
- `TestPersistExtendedFields` (6): all six written album-wide; the UPDATE is
  fill-only; no release id anywhere → no write; the release id is resolved from
  the DB when not supplied (the rescan case); an empty release → no write; a
  release with no extended values → no write.

**Oracle verified:** with the two pre-fix modules restored the suite fails; with
the fix it passes 16/16.

**Regression check:** `test_mb_cover_art_and_lookup_fixes.py`,
`test_album_missing_and_disc_cleanup.py` and
`test_compilation_tracklist_guard.py` give **3 failed / 60 passed both with and
without** this change — identical, so no regressions. Those 3 failures are
pre-existing on `origin/develop`.

## Separate pre-existing defect found

`tests/test_mb_cover_art_and_lookup_fixes.py::TestFetchMusicbrainzReleaseMetadataChecklist`
fails with `KeyError: 'compilation'`. Commit `714b2fcb` added a rich return
contract (`compilation`, `original_date`, `absolute_track_number`,
`composer`, `lyricist`, `iswc`, `original_title`, `work_mbid`) **and** that test,
but the contract is absent from the current
`fetch_musicbrainz_release_metadata`. The richer version still exists in the
older tree, so the function regressed. Tracked separately — not addressed here.
