# Lookup MBID: measure against an edition that can still be missing (2026-10-09)

**Area:** enrichment (MusicBrainz best release) + album missing tracks
**Commit:** `fix(album): pick the fullest edition and stop letting a position prove presence`

## Reported

> When doing a Lookup MBID on the album page, it's not correctly pulling in the
> missing tracks for albums that are sometimes double cd and also longer single
> cd.

Two answers pinned it: **the missing ones aren't shown** and **it picked the
wrong edition**. Both causes were self-defeating.

## 1. The edition was chosen by proximity to what is already in the database

`get_musicbrainz_best_release` scored every release in the group with:

```python
Value -= abs(local_track_count - release_track_count) * 100.0
```

`× 100` outweighs **every other signal combined** (official `+50`, date `+2.1`,
title `+30` ≈ 82), so **one track of difference flipped the entire choice** —
and the target was the count of rows we *already have*. The edition closest to
what we own is the edition with nothing left to find:

| album | library rows | editions | picked |
|---|---|---|---|
| **double CD**, disc 1 imported first | 12 | 12 / 24 | 12 ✅ → **24** |
| **longer single CD**, rows still partial | 13 | 13 / 16 | 13 ✅ → **16** |

**Fix:** the count now **leads** (`track_count × 1000`, an unknown count
ranking below any known one) and the previous signals become **tie-breaks
between editions of equal completeness**. An edition can only measure what is
missing if it can still *hold* what we have plus what we do not.

`local_track_count` still drives **confidence** — unchanged — so an edition
larger than the library reports LOW confidence, which is the honest answer and
is exactly what makes the album page offer the release picker instead of
silently claiming a match.

## 2. A position counted as proof on its own

`get_missing_tracks` excluded a MusicBrainz track when *any* local row sat at
the same `(disc, track)`, regardless of title or length. On a rip whose own
numbering differs from MusicBrainz's — a double-CD imported disc by disc, a rip
numbered across the whole disc — an **unrelated** track at that number claimed a
genuinely-missing one as present.

**Fix:** reuse `_track_number_pairing_allowed`, the **one** track-number rule
both queue matchers already use: *a position is a tie-breaker, never proof.*
The local `duration` now travels with the position so the rule can tell "same
recording, differently titled" from "a different song at the same number":

* title agrees → still present (a wording difference is not a missing track);
* titles differ but both lengths are known and differ → **reported missing**;
* an unknown length stays permissive, so an untagged file is not lost.

## Tests

`tests/test_missing_tracks_double_cd_and_long_single.py` — **13**:

* `TestTheMostCompleteEditionIsChosen` — the double-CD and long-single-CD
  shapes, an unknown count ranking last, and **controls**: equal editions still
  break ties on status/date/title, a single edition is unchanged, and
  `local_track_count` still drives confidence;
* `TestAPositionNoLongerProvesPresence` — a different song at the same number
  is now reported missing, and **controls**: a renamed track, an exact title, a
  title with no position, and an untagged neighbour all stay present; the
  `missing + excluded == mb_total` invariant still holds.

## Verification

* New suite → **13 passed**.
* **Oracle** — reverting `musicbrainz_service.py` + `album_missing_service.py`
  → **4 failed / 9 passed**: the two edition shapes, the low-confidence signal
  and the eaten track. The 9 that pass either way are the controls. Restored →
  13 passed.
* **Sweep** — the 52 test files referencing the MusicBrainz service / missing
  tracks / best release, clean `origin/develop` vs this change: **baseline 35
  failures, changed 35**, `Compare-Object` on the sorted `^(FAILED|ERROR)
  tests/` lines = **identical, 0 regressions**.
* `import app` → **392 routes**.

## Not changed

* **`local_track_count` still drives confidence**, only no longer the *choice*.
* `_search_releasegroup_matches`' per-recording track-count penalty is a
  `× 0.05` tie-break on a similarity score — an order of magnitude below its
  context, so it does not dominate anything.
* The title path, `_title_match_key`, the queue coverage split and the
  rejected-titles behaviour.
* **No release list is re-fetched when only the library changed**: the
  `_best_release_cache` key is unchanged.
