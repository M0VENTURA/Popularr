# A compilation marking is no longer dropped by a disagreeing album type (2026-10-07)

## Reported

> This was marked as album+compilation but was stuck with the star ratings

On `Powderfinger – Fingerprints: The Best of Powderfinger 1994–2000` (17
tracks, a single-artist best-of). The album page showed **Album (Compilation)**
while the tracks carried ratings computed as if it were a plain studio album.

## Root cause — the type was resolved from the WRONG column

`services/popularity/stages/finalise_stage.py::_resolve_compilation_flags` is
the fallback that decides `(is_compilation, is_va_compilation)` for the
end-of-run `finalise_scan` grouping path — the call site that passes **no**
compilation verdict (`post_album_star_ratings(...)` with no
`is_compilation=`), so this helper is the only thing that decides, and it runs
**last** in a scan.

It resolved the type with an `or` chain that read **`spotify_album_type`
FIRST**:

```python
type_text = str(
    row.get("spotify_album_type")
    or row.get("musicbrainz_album_type")   # never reached when Spotify is set
    or album_type
    or detected_album_type
    or ""
).strip()
```

`classify_compilation_category` was then handed `"album"` — no `compilation`
token — and with one credited artist (`Powderfinger`, not a VA placeholder) it
returned `""`, i.e. **not a compilation**. Measured with the shipped helper:

| spotify_album_type | musicbrainz_album_type | returned |
|---|---|---|
| `album` | `album+compilation` | **`(False, False)`** ← the shipped combination |
| *(empty)* | `album+compilation` | `(True, False)` |
| `album` | `album` | `(False, False)` |

So a best-of whose **Spotify** match is a plain album (Spotify routinely files
best-ofs as `album`) but whose **MusicBrainz** release is a compilation was
re-rated as a studio album by `finalise_scan` — while the album page displayed
"Album (Compilation)", because every other consumer puts MusicBrainz FIRST:

* `routes/ui_routes.py::first_value("musicbrainz_albumtype", "spotify_album_type", "album_type")` — the album page's type select;
* `services/catalog/release_categories.py::category_for_album_row` — the artist-page bucket.

`_resolve_compilation_flags` was the odd one out, and its precedence decided the
star rating.

## The change

`_resolve_compilation_flags` now **JOINS** every type column instead of
`or`-chaining them, and reads both key spellings (`musicbrainz_albumtype` is the
DB column; `musicbrainz_album_type` is the in-memory key `load_stage` builds):

```python
type_text = " ".join(
    str(value or "").strip()
    for value in (
        row.get("musicbrainz_album_type"),
        row.get("musicbrainz_albumtype"),
        row.get("spotify_album_type"),
        row.get("album_type"),
        row.get("detected_album_type"),
    )
).strip()
```

Joining cannot invent a classification the shared helper would reject:
`classify_compilation_category` **already** joins Spotify + MusicBrainz
internally, so any source that reports "compilation" now wins, whichever column
carries it. An album whose sources agree is unaffected.

## Files

* `services/popularity/stages/finalise_stage.py` — `_resolve_compilation_flags`
* `tests/test_compilation_cover_and_rating.py` — +4 tests

## Tests

`TestTheCompilationMarkingIsNotDroppedByADisagreeingType` (4):

* a MusicBrainz `album+compilation` survives a plain Spotify `album`;
* the DB column spelling `musicbrainz_albumtype` is read too;
* a Spotify `album+compilation` survives a plain MusicBrainz `album`;
* CONTROL — agreeing plain types stay plain.

**Oracle:** reverting the source file → **2 failed / 2 passed** (the two
inversions), the two controls green; restored → 4 passed.

**Suite sweep** (15 suites: genre playlists, playlist sync/cap, Essential
sections, Essential refresh/dedup, Christmas exclusion, prominence era
benchmark, playlist dedupe, dropped-ids, per-album star posting, finalise scan
mode, compilation 5★, compilation online catalogue, LB cohort, playlist
ordering, compilation cover/rating): **base = 21 failed, new = 21 failed,
`Compare-Object` = EMPTY** → identical failing sets, all pre-existing.

## NOT changed (deliberately) — needs your call

`_assign_stars`'s compilation branch still contains a hard-coded
`has_usable_catalogue = False` ("Force online catalogue lookup by bypassing the
local threshold"), which:

1. makes the Config page's **"Min Local Catalogue"** option dead, and
2. leaves `test_compilation_online_catalogue.py::…::test_healthy_local_catalogue_skips_the_online_lookup`
   **failing** at `origin/develop` (it pins the documented behaviour).

I implemented the obvious fix (use the local artist catalogue when it holds
≥ `min_local_catalogue` scores) and **reverted it**, because it is a RATING
POLICY change, not a bug fix, and it traded one documented behaviour for
another: it fixed the failing test but broke
`test_compilation_more_5star.py::TestCompilationSlotCapSkipped::test_many_hits_all_stay_five`
(the 2026-08-20 "single-artist compilations get more 5★ tracks" contract).

The reason it conflicts: for a single-artist best-of the artist's catalogue
INCLUDES the album's own 17 tracks, so the z-score is self-referential — every
track sits near its own median, z ≈ 0, and the whole best-of lands on 3★. Using
the local catalogue for a best-of therefore needs a decision about EXCLUDING the
compilation's own tracks from the reference distribution first. Flagged rather
than guessed.
