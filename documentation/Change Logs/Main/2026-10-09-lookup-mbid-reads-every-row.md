# Lookup MBID: review every album field against what the album actually holds

**Date:** 2026-10-09 - **Area:** album page / metadata review
**Reported:**

> Can you confirm during the comparison when doing the lookup mbid, that all
> these fields are being correctly checked and reviewed? Some seem to be seen
> as empty even when there is information in them.

## Answer to "are they checked?"

Every field the Edit Album page shows **is** in `_ALBUM_FIELD_SPECS` (or is
handled as `album_genres`), so Lookup MBID reviews it:

`Album Title` - `Album Artist` - `Release Name` - `Original Year` -
`Release Year` - `Album Type` - `MB Release ID` - `MB Release Group ID` -
`MB Artist ID` - `Album Genres` - `Record Label` - `Catalog Number` -
`Barcode` - `Release Date` - `Media Format` - `Release Country`
(plus `Track Artist`, which is a *per-track* proposal).

**Not reviewed, by design:** `Discogs Release ID` and `Last.fm Release` - a
Lookup MBID compares against **MusicBrainz**, and neither is a MusicBrainz
field. They are shown for reference only.

The MusicBrainz side is genuinely produced too: `musicbrainz_service` extracts
`recordlabel` / `catalognumber` / `barcode` / `releasedate` / `media` /
`releasecountry` from the release (`label-info`, `barcode`, `date`, media
formats, release events). A test now asserts every key the spec names actually
appears in that module - a spec key nothing emits could never propose.

## Why some read "(empty)"

`_album_level_proposals` built its **current** values from `local_tracks[0]` -
the **first track row only**.

Album-level values are *meant* to be duplicated onto every row, but they
routinely are not: a row imported later, a partial re-tag, a scan that filled
some rows and not others. So the review reported `(empty)` for a field the
album page was visibly displaying - and then **proposed a value the album
already had**, because `_norm(proposed) == _norm(current)` cannot match an
empty string.

### Fix

New `_first_present(rows, keys)` - the first **non-empty** value across every
row, row-major then the field's own key order. That is the same algorithm the
album page's `first_value` uses, so the review and the form cannot disagree
about what the album currently holds. All sixteen album-level fields read
through it.

Nothing about *what* is proposed changed: only what counts as "current".

## Tests

`tests/test_album_proposal_reads_every_row.py` - **10 new**:

* `_first_present` - value on a later row, empty-everywhere stays empty, row
  order beats key order, a later row fills a key no earlier row had,
  whitespace-only counts as empty;
* the report itself - a label only on row 2 is **not** proposed as a change,
  and a genuine change reports `current` as the value the album holds;
* every one of the 15 album-level fields reads across rows (row 0 carries
  nothing - exactly the shape that used to read "(empty)");
* coverage - every field shown on the album page is reviewed, and every
  MusicBrainz key the spec names is produced by `musicbrainz_service`.

The new helper is imported **lazily** (a module-scope `from ... import
_first_present` made the whole file a COLLECTION ERROR on an unpatched tree,
hiding every verdict); the fallback reproduces the old row-0 read so each
assertion fails for its own reason.

**Oracle** - the service reverted: **6 failed, 4 passed** (the 4 are the
coverage guards, which must hold either way); restored: **74 passed** across
four suites.

**Sweep** - 14 test files touching `metadata_proposal_service` /
`propose_album_metadata` / `_ALBUM_FIELD_SPECS` / `metadata-review`: baseline
**0** -> changed **0**, regressions **(none)**.

`ast.parse` clean.

## Files

- `services/metadata/metadata_proposal_service.py`
- `tests/test_album_proposal_reads_every_row.py` (new)
