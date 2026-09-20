# Scan-time track identity + the file year-tag split

**Date:** 2026-09-21 · **Area:** normalisation / scan / metadata tag sync

Four related behaviours, all decided by the **scan** (`services/popularity/scan_hooks.py`)
so that the DB row and the physical file tags share one canonical record.

## 1. A featured credit found in the TITLE moves onto the ARTIST

```
Evanescence - Bring Me To Life feat 12 stones
  -> artist        Evanescence feat. 12 Stones
  -> title         Bring Me To Life
  -> album_artist  Evanescence            (unchanged)

Various-artists compilation
  -> album_artist left completely alone
```

* `feat./ft./featuring` only. `with`, `w/` and `&` are **deliberately excluded**
  from titles — "Me & You" and "Dance with the Devil" are song titles, not
  credits. `strip_featured_artist` stays permissive on the *artist* field, where
  "X & Y" really is a second credited artist.
* An artist that already carries a credit is never given a second one.
* Guest casing is preserved, except that an entirely lower-case credit is
  title-cased (`12 stones` → `12 Stones`). `str.title()` would have destroyed
  `MC Solaar` and `will.i.am`, so the guard skips any credit containing a
  character other than a word character, space, apostrophe or hyphen.
* The title is never emptied — a title that is nothing but a credit is left alone.

### The album key cannot move

The album key used throughout the app is `COALESCE(NULLIF(album_artist,''), artist)`,
and the album artist is additionally protected from stale overwrites by
`track_stage._STALE_PROTECTED_COLUMNS`. So `album_artist` is written back
**only** when the row had none *and* the incoming artist carried no credit of its
own:

* filling an empty album artist with the **primary** artist leaves the key
  untouched whenever the artist was credit-free ("Evanescence" before the move
  from the title, "Evanescence" after) — the case this pass exists for;
* for a credit-laden artist, filling (or stripping) it *would* move the album to
  a new key mid-scan, and every album-scoped lookup issued with the old spelling
  (file-tag sync, release MBID, extended metadata) would silently find nothing.
  Nothing is written, so that album behaves exactly as before.

`helpers.normalization_service.album_artist_key_variants()` is the belt-and-braces
counterpart, now used by `album_tag_sync_service._load_fresh_tracks`, so an
album-scoped read accepts both spellings regardless.

## 2. A false "(X Cover)" attribution is removed **and cleared**

`detect_cover_and_normalize_title` tested `"cover" in title.lower()`, so **every**
title merely containing the word was a cover — "Cover Me" included, and
"Song (Disturbed Cover)", which is the reported false cover. It is now
**attribution-shaped**: only a trailing bracketed `(X Cover)` / `[Cover Version]`
counts.

`track_stage`'s cover block only ever wrote a *positive* verdict, so a stored
false one could never be undone — and `detect_cover_song` short-circuits on an
"already confirmed" verdict anyway. When the scan reports that cover wording was
removed, the verdict is now **re-evaluated with `force=True`** and, if negative,
written explicitly (`is_cover = False`, `is_cover_reason = "cover attribution
removed from title"`), with the `Cover` genre the false verdict had added
stripped out. `cover_manual_override` still wins — that flag records a decision
the **user** made.

## 3. "Remastered" / "Remastered 2026" is removed from the title

`REMASTER_SUFFIX_RE` gained the trailing `version` form, which it previously
missed entirely: `"Song (Remastered Version)"` was silently left intact. Handled:
`(Remastered)`, `(Remastered 2026)`, `(2026 Remaster)`, `(2011 Remastered)`,
`- 2026 Remastered`, `(Remastered Version)`, `[Remastered]`, and stacked
markers (`"Song (Disturbed Cover) (2011 Remastered)"` → `"Song"`).

**The year inside the marker is discarded** (agreed rule): years come from
MusicBrainz, never from a title. A degenerate title like `"(Remastered)"` is never
emptied.

## 4. FILE year tags: DATE = EDITION year, ORIGINALDATE/YEAR = ORIGINAL year

The DB already separated the two — `year` is the album's **original** year (the
release group's first release year, which album sorting reads) and `release_year`
is **this edition's**. The file writers did not: `year` → the `DATE`/`YEAR` tag,
and `release_year` was mapped to **no tag at all**. So every remaster claimed in
its own tags to *be* the original release, and Navidrome/Picard — which read
`DATE` as "the release this file is from" — saw the wrong date.

Now, for **MP3 (ID3v2.4 `TDRC`)** and **FLAC (Vorbis `DATE`)** alike:

| Tag | Value |
|---|---|
| `DATE` / `YEAR` | the EDITION's year (`release_year`), falling back to the original so a file is never left undated |
| `ORIGINALDATE` / `ORIGINALYEAR` | the album's ORIGINAL year |

* `album_tag_sync_service`: new `_resolve_album_edition_year()` (majority vote on
  `release_year`, mirroring the existing `_resolve_album_year()` for the
  original); `_db_tag_candidates()` now emits the `originalyear`/`originaldate`
  pair; `_read_file_values()` reads it back for both formats (without which the
  sync would rewrite it on every scan).
* `tag_file_service.build_tag_updates()` — used by the album page Save and the
  metadata update route — gives `release_year` precedence for the DATE tag and
  fills the original pair from `year` when the payload carries no explicit
  `originalyear`. `update_file_metadata()` (the download/queue import path) does
  the same, so a file's DATE cannot mean different things depending on which
  writer touched it.
* **Self-heal**: `sync_album_file_tags` is fill-if-missing, so it could never
  repair a DATE the old writer had stamped with the original year. When the
  file's DATE equals the album's ORIGINAL year while the DB knows a different
  EDITION year, it is now corrected. A user-edited date (matching neither) is
  left alone, as is an already-correct one.
* Album sorting is unchanged and stays on the ORIGINAL year — the album listing
  already prefers `year` over `release_year`, and a test now pins that
  precedence so a remaster cannot start sorting as its re-release year.

## Also fixed (found while working)

`services/popularity/stages/track_stage.py` line ~874 called
`lookup_recording_metadata(..., edition_annotation=_album_annotation)`. That name
is a local of `process_track`, not of `_resolve_track_mb_metadata` where the call
sits, so it raised `NameError` — swallowed by the caller's debug-level handler,
which turned per-track MusicBrainz resolution off **silently**. It now passes the
function's own `edition_annotation` parameter, which `process_track` already
supplies as that same value.

## Verification

`tests/test_scan_identity_and_year_tags.py` (new, 54 tests) covers all four
behaviours, the reported examples, the key-stability rule and the ID3/Vorbis tag
routing.

**Probe (before the commit landed).** A 50-check logic probe ran against the
**real** helpers (`helpers.normalization_service` from a checkout, with only the
edited regex monkeypatched, so there is no copy drift) — **all passed**,
including the edge cases that matter: "Cover Me" and "Me & You" untouched,
`will.i.am` casing preserved, a credit-only title never emptied,
`(Remastered Version)` now handled, stacked markers, and the year precedence in
both tag services.

**First full pytest run** (against `54997a39`): **48 passed / 6 failed**. All six
were defects in the new test file, not the change — and the run captured the
product behaviour working:

```
[info] [TRACK] false cover flag cleared detector_verdict=no_match title=Song
[debug] Cover check skipped: manual override track=Song
```

| Test defect | Cause | Fix |
|---|---|---|
| 3 × `TestCoverFlagIsCleared` | the `_deferred_persist` sink was a `set`, and a dict is unhashable (`Deferred persist enqueue failed error=unhashable type: 'dict'`), so nothing was captured | list-backed sink implementing the real `.add(row)` contract |
| `test_the_year_in_the_marker_is_discarded` | `isinstance(True, int)` is `True` — a flag looked like a year | assert no field's text contains the year |
| `test_mp3_writer_routes_year_to_tdrc…` | the map is `_MP3_FRAME_FOR_FIELD`, not `_ID3_FIELD_MAP` | corrected (verified: `year` → `TDRC`) |
| `test_the_album_listing_prefers_year_over_release_year` | the regex pinned the SQL's exact formatting | positional check instead (verified: `COALESCE(year` at 663 precedes `release_year` at 746) |

**Regression check** — 8 suites around the changed files
(`album_tag_sync_service`, `metadata_fanout_to_files`,
`tag_name_standardisation`, `album_mb_tags_to_files`,
`album_missing_and_disc_cleanup`, `async_routes_do_not_block_event_loop`,
`no_python_syntax_errors`, `album_title_and_version_matching`):

| Tree | Result |
|---|---|
| Before (`54997a39^` = `a432acac`) | **6 failed / 103 passed** |
| After (`54997a39`) | **4 failed / 105 passed** |

**0 new failures** — and the two disappeared ones are
`test_album_tag_sync_service::test_db_tag_candidates_perfect_includes_mbids` and
`…_imperfect_excludes_mbids`, the long-standing "signature drift" failures, which
the new default arguments fix. The remaining 4 fail identically on both trees
(pre-existing: a mutagen double without `delall`, and the album-id fan-out).

