# Album lookup fails on a release-name title with a duplicated annotation

**Date:** 2026-09-23
**Area:** `metadata` / `scan`

## What was asked

> "When an album has an album title of a release name such as below
> `Jomsviking (jewelcase version with "Vengeance Is My Name" as bonus track (no. 09))))) (jewelcase version with "Vengeance Is My Name" as bonus track (no. 09)) (2016)`
> It fails to do a lookup for the album when matching in the album scan and fails to match the mbid."

## Three independent defects, all feeding one unanswerable query

Each was reproduced separately before fixing.

### 1. The edition stripper was `$`-anchored, so a trailing **year** blocked it

`strip_album_edition_marker` only matched a marker that was the **last** thing in
the string. Taggers routinely write the year as its own trailing group — and
that silently disabled marker stripping for **every** keyword, not just this
album:

| Input | Before | After |
|---|---|---|
| `Weezer (Deluxe Edition) (2016)` | unchanged ❌ | `Weezer (2016)` ✅ |
| `Slipknot (Clean) (2016)` | unchanged ❌ | `Slipknot (2016)` ✅ |
| `Abbey Road (Anniversary Edition) (2009)` | unchanged ❌ | `Abbey Road (2009)` ✅ |
| `Some Album (tour edition) (tour edition) (2011)` | unchanged ❌ | `Some Album (2011)` ✅ |

The year is now set aside, the markers stripped, and the year **re-appended** —
it is not an edition marker, so discarding it would be data loss.

### 2. A corrupted bracket blob defeated every balanced-group rule

The `(no. 09)))))` run leaves more closers than openers. Every bracket regex in
the module matches a balanced `(…)`, so the annotation was skipped entirely and
survived into the lookup key.

New `repair_malformed_annotations()` reduces an **unbalanced** title to its plain
leading title. The test is deliberately the simple one — do the bracket **counts**
agree? — and an earlier attempt of mine is worth recording: truncating at the
first `(` whose *remainder* held more closers than openers wrongly fired on a
valid **nested** group, because in `(a (b))` the remainder from the inner opener
is `(b))`. Only a whole-string imbalance means the title is genuinely malformed.

### 3. `strip_search_keywords` was a **no-op by default**

It returned its input unchanged whenever the config list `search.strip_keywords`
was empty — and that key is **unset on a default install**. The album scan's
lookup path calls it, so the search ran with the raw annotated name. The standard
marker stripping now always runs; the config list is purely **additive**.

### The resulting query

`MusicBrainzService.search_releasegroup_matches()` built:

```
artist:"Amon Amarth" AND releasegroup:"Jomsviking (jewelcase version with … (no. 09))))) … (2016)"
```

and `lookup_musicbrainz_album()` was worse — it did **no** cleaning at all and
quoted the raw album as `release:"<Album>"`. Now both clean first:

```
artist:"Amon Amarth" AND releasegroup:"Jomsviking"
```

which is a title MusicBrainz actually holds, so the album can match its MBID.

## Files

* `helpers/normalization_service.py` — `_TRAILING_YEAR_RE`,
  `repair_malformed_annotations`, trailing-year handling in
  `strip_album_edition_marker`, always-strip behaviour in `strip_search_keywords`.
* `services/enrichment/musicbrainz_service.py` — `lookup_musicbrainz_album` now
  cleans the album before quoting it.

Form markers (`Live`, `Remix`, `Acoustic`, `Unplugged`, `Instrumental`, `Demo`,
`Karaoke`) and a bare placeholder like `Some Album (Album)` are still preserved —
they name a different **recording**, not a different pressing.

## Tests

**NEW `tests/test_album_lookup_malformed_release_name.py`** (39 tests), using the
**exact** reported title, plus the trailing-year and
"must-not-touch-balanced-titles" contracts.

**Oracle:** with the fix stashed, **29 failed / 10 passed**; restored,
**39 passed**.

**Regression sweep** (11 album/normalization suites, 285 tests): failing set
**identical** before and after — `Compare-Object` diff **EMPTY**, i.e.
**0 regressions**. The three failures are pre-existing:

* `test_album_musicbrainz_matching.py::test_best_release_confidence_scales_down_on_count_mismatch`
* `test_album_type_persistence.py::TestEnsureAlbumTypeReturnsVerdict::test_detects_when_missing`
* `test_release_categories.py::TestAlbumRow::test_no_type_falls_back_to_title`

## Note for the existing library

Albums already stored with one of these mangled names will not be renamed
retroactively by this change — it fixes the **lookup**, so a scan can now match
them. Re-running the album scan (or the album-name clean, which uses
`metadata_update.album_name_source`) will repair the stored names.
