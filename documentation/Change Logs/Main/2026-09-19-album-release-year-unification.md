# Album release year unification during metadata scans

**Date:** 2026-09-19
**Area:** `popularity` / `db` / `ui`

## Reported symptom

> During metadata scans, different years are being attached to songs on the one
> album which is splitting the album up. All tracks on an album should have the
> correct release year attached to all tracks.

One folder in the library rendered as **two or three separate albums**, each
holding a subset of the folder's tracks and labelled with a different year.

## Root cause

`tracks` has TWO album-scoped year columns, and **both were written per track**:

| Column | Meaning |
|---|---|
| `year` | the album's **ORIGINAL** year (the release group) |
| `release_year` | **THIS EDITION's** year |

`track_stage._resolve_track_mb_metadata` returns them for a single recording
(`year` ← `original_release_year`, `release_year` ← the specific release's
date). MusicBrainz resolves each recording to whichever release lists it
first, so on a multi-edition album ("Last Of Us" vs "Last Of Us (2018
Version)") different tracks of the **same folder** resolved to different
editions — and therefore to different years.

The album-year unification in `process_track` (§5.5) only covered `year`, and
only lowered it toward `min()`. **`release_year` was never unified at all**, so
each track kept whatever edition its own recording resolved to.

The UI then grouped on (name, year):

```python
# routes/ui_routes.py — artist page
album_key = f"{album_name.lower().strip()}::{track_year or ''}"
# _leading_year() prefers ``year`` and FALLS BACK to ``release_year``
```

```sql
-- routes/ui_routes.py — dashboard "recently added" group key
GROUP BY COALESCE(NULLIF(album_artist,''), artist), album,
         COALESCE(NULLIF(SUBSTRING(year FROM '^[0-9]{4}'), ''),
                  NULLIF(CAST(release_year AS TEXT), ''))
```

So tracks carrying `{2018, 2020}` for one folder produced two distinct
`album_key`s — two albums.

## Fix

The year pair is now resolved **ONCE per album** and forced onto every track.

### `services/popularity/scan_stage_runner.py`

- New `_year_of_value(value)` — leading 4-digit year of `"2018"`,
  `"2018-04-20"` or `2018`, else `None`.
- New `_resolve_album_authoritative_year(tracks, mb_batch)` →
  `(original_year, edition_year)`:
  - `original_year` = **earliest** year seen (`year` is the album's original
    release, so a reissue must group with the original pressing).
  - `edition_year` = **most common** `release_year` — the edition belongs to
    the release, not to the individual recording. Ties go to the **earliest**
    year so the result is deterministic and cannot depend on row order.
  - **Stored track values are preferred**; the MusicBrainz batch is only
    consulted when the tracks carry no year at all. A year already in the DB
    may be a deliberate edit, and sourcing from it first keeps the verdict
    identical to the previous track-local `min()` instead of silently re-dating
    every album from MusicBrainz.
  - Returns `(None, None)` when nothing is known, so the caller leaves the
    columns alone rather than inventing a year.
- The pair is computed in the album loop (before `_track_jobs` is built) and
  stashed on `album_context` as `authoritative_year` /
  `authoritative_release_year`, so every track of the folder reads the same
  values. Gated on `not _mode_pop and not _mode_singles` — a popularity-only or
  singles pass writes no album identity.

### `services/popularity/stages/track_stage.py`

Section 5.5 now pins **both** columns:

- `year` — the authoritative original year, falling back to the album-wide
  `min()` (unchanged behaviour for direct callers that pass no
  `album_context`, e.g. tests).
- `release_year` — the authoritative edition year, falling back to the
  album-wide **majority** `release_year`.
- The comparison is now `!=` rather than `>`: a track whose year is *earlier*
  than the album's target is also wrong and is corrected upward. Previously
  `>` meant an under-year was never repaired.
- `release_year` is only written when known — never clobbered to `NULL`, and
  never invented from the original year.
- Added the `Counter` import.

## Self-healing

Both columns are rewritten uniformly on the **next metadata scan**, so albums
already split across several years re-merge on the artist page without any
manual repair or migration.

## Tests

`tests/test_album_release_year_unification.py` — 12 tests.

- `TestResolveAlbumAuthoritativeYear` (7): conflicting track years collapse to
  the earliest; conflicting edition years take the majority; ties break to the
  earliest; unknown years return `None` rather than a fabricated value; full
  dates reduce to their year; stored values win over the MusicBrainz batch; the
  batch fills a missing original year.
- `TestProcessTrackUnifiesBothYearColumns` (5): the reported defect (one folder,
  years `2018` + `2020` → both persist `2018`, and both `year` **and**
  `release_year` are pinned); majority edition unification; the
  `album_context` authoritative pair overrides the per-track scan; unknown
  years are not invented; a popularity-only pass leaves the year untouched.

Verified by reverting only the source fix: **10 of the 12 fail**, with the
exact reported signature `assert '2020' == '2018'`. With the fix, all 12 pass.

Regression: `tests/test_metadata_update_album_split.py` +
`tests/test_mb_batch_version_resolution.py` show **13 failed / 10 passed both
before and after** the change — identical, so no regressions. Those 13 failures
are pre-existing on `origin/develop` and unrelated to this work.

## Note — unrelated latent bug found

`_collapse_album_mb_batch` is defined in `scan_stage_runner.py` but has **no
production call site** in `origin/develop`; only the regression tests call it
directly. Its guard ("a metadata scan must not split an album across sibling
editions") is therefore **not running in production**. The album-name half of
that defect is out of scope here — this change covers the *year* half — but the
missing call site should be restored separately.
