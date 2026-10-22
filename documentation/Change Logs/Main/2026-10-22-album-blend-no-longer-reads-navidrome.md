# The album genre blend was reading Navidrome — the tags Popularr itself writes

**Date:** 2026-10-22 · **Area:** `genres` · `navidrome`

## Reported

> "It's still assigning holiday to all tracks. Is it using current Navidrome
> genres as it's not meant to use that as a genre source when doing the final
> genre apply?"

**Yes. Exactly that.** The user diagnosed it from the symptom alone, and it
explains why the previous fix (`ed70e757`, the majority rule) did not help.

---

## ⭐⭐ A TAG PIPELINE WAS FEEDING ON ITS OWN OUTPUT

`navidrome_genres` is **not an independent genre source**. Navidrome serves the
tags of the files **on disk**, and this pipeline is what writes them:

```
scan writes tracks.genres
  → sync_album_file_tags pushes genres into each FILE's tags (TCON / genre)
    → Navidrome re-reads those files and serves the genre back
      → the next import stores it as navidrome_genres
        → get_track_recommendations counted it as genre EVIDENCE
```

Whatever the blend got **wrong** in one pass returned in the next as **proof it
was right**. Measured on a clean 14-track album with one Christmas song and no
holiday tag anywhere:

| | blend | `_album_holiday_votes` | `album_is_holiday_album` |
|---|---|---|---|
| before the round trip | `['Alternative Rock']` | — | — |
| **after** the round trip | `['Alternative Rock', **'Holiday'**]` | **14/14** | **True** |

### Why the majority rule could not save this

`ed70e757` fixed the DENOMINATOR — it stopped counting only the rows that had
genre data. That was correct and remains correct. But the **evidence** was
poisoned: after the round trip, 14 of 14 tracks genuinely carried a holiday
token, so a strict majority test was *satisfied*. No threshold, no quorum and no
"minimum number of tracks" rule can recover from evidence that is a reflection
of the writer's own output — the tag can never be observed to go away.

> ⭐ The denominator was right. The evidence was the problem. Those are
> different bugs and only the first one had been fixed.

### The convention already existed

It was already written down twice in this codebase:

- `genre_selection` weights `navidrome` **0.30** — *"local file tags: a
  tie-breaker, never a sole source"*.
- the VA path calls `aggregate_genres(…, nav_genres=None)` **on purpose** —
  *"passing nav_genres would let Navidrome decide a tie on an album"*.

The album blend was the one caller that ignored it.

---

## The fix

`get_track_recommendations` no longer reads `navidrome_genres` at all — not from
the `SELECT`, and not in `column_mapping`.

Navidrome is **still available to the selection engine** through the separate
`nav_genres` parameter, so nothing loses the tie-breaker it is entitled to. It
just must not sit in the source map as if it were evidence.

| before | after |
|---|---|
| `SELECT … navidrome_genres …` (10 cols) | 9 cols |
| `("navidrome", 7)` | removed; the mappings after it renumbered |

⚠️ **The mappings are POSITIONAL**, so dropping a column renumbers every index
after it. A stale index silently reads `audiodb_genres` where
`navidrome_genres` used to be — a wrong answer **with no error**. That is why
`test_column_indices_still_align_with_the_select` asserts each mapped name still
addresses the column it names.

---

## Measured

Same probe, same inputs, before and after:

```
CLEAN album (one Christmas song, no holiday tags anywhere):
  before: blend = ['Alternative Rock']
  after:  blend = ['Alternative Rock']

AFTER the write round-trips through Navidrome:
  before: blend = ['Alternative Rock', 'Holiday']
          _album_holiday_votes → 14/14      album_is_holiday_album → True
  after:  blend = ['Alternative Rock']
          _album_holiday_votes →  0/14      album_is_holiday_album → False
```

---

## Tests

`tests/test_album_blend_does_not_read_navidrome.py` — **9 tests, 5 fail
pre-fix**:

| test | pins |
|---|---|
| `test_holiday_arriving_only_via_navidrome_is_ignored` | the regression itself |
| `test_repeated_scans_do_not_ratchet` | ⭐ the loop's *defining property* — a fixed point must be stable |
| `test_real_genres_survive_the_round_trip` | this is not a blanket ban on genres |
| `test_christmas_still_propagates_from_a_title` | the genuine seasonal signal still works |
| `test_navidrome_not_in_the_source_map` / `_column_not_in_the_select` | the mechanism, not just the symptom |
| `test_column_indices_still_align_with_the_select` | the positional-mapping trap above |

### Fixture completed, not weakened

`tests/test_various_artists_genre_isolation.py` seeded each VA track's genres
into **both** `musicbrainz_genres` and `navidrome_genres`, and relied on that
second (duplicate) vote to lift each performer's genre over the confidence
floor. Removing Navidrome dropped the blend to `['Thrash Metal']`, and the
fixture's own positive precondition caught it — *"fixture cannot exercise the
defect"*.

⭐ That duplicate vote was **modelling the contaminated state the blend no longer
trusts**. The fixture now supplies a second **independent** source
(`discogs_genres`), which is also a truer model of a library with more than one
provider configured. 7 passed.

## Regression sweep

`genre|holiday|christmas|navidrome|album_blend|va_track` (1179 tests):
**baseline 17 failed / 1153 passed → fixed 17 failed / 1162 passed.** Identical
failure set; the +9 are the new tests. **Zero regressions.**

The 17 are pre-existing and unrelated (Navidrome playlist POST fakes, genre
consensus config, login fallback, `test_album_genre_fallback` — the dead
`_album_top_genres` chain, see `126901fc`).
