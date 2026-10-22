# One track's holiday tag no longer makes the whole album seasonal, and the family merges to `Holiday`

**Date:** 2026-10-22
**Area:** `services/enrichment/genre_aggregation_service.py` ·
`services/popularity/stages/finalise_stage.py`
**Commit:** `fix(genres): one track's holiday tag no longer makes the whole album seasonal`

## Report

> "So much for the afterglow keeps detecting as a holiday album. I think the
> holiday genre from the one track is propegating to the whole album."

The diagnosis was exactly right, and the mechanism was deliberate code.

> "Can you continue and also make sure that anything with the holiday genre
> tag is merged Christmas. Maybe having Holiday as the default to also cover
> other Jewish holidays."

## Root cause — the exemption that protects a Christmas album is the hole

The holiday family is deliberately **exempt** from
`_TRACK_AFFILIATION_FILTER_TAGS` (Cover/Live/Acoustic/…): a Christmas album IS
an album property and the genre-playlist builder needs the tag outside its
3-genre window. So unlike every other filter tag it had **no protection**
against spreading:

1. `get_track_recommendations` pools **every** track's sources and
   `aggregate_genres` appends every filter tag it finds — one track's Last.fm
   `holiday` reached the album blend;
2. the affiliation filter let it through;
3. `sync_album_file_tags` writes that blend **album-wide**
   (`UPDATE tracks SET genres = …`), so every sibling claimed it.

Measured on the reported shape (1 of 10 tracks tagged), the blend collapsed to
**`['Holiday']`** — the real genres were condensed away entirely, and the
album-wide write then stamped the whole record. The context boost only fires
for an album/title KEYWORD, and "Afterglow" gets none, so the sole signal was
the one tag.

The trace also exposed two further defects:

* every spelling of the family pooled **separately**, so "Christmas" on one
  track and "Holiday" on another produced two near-duplicate playlists;
* a track whose only tag was `Holiday` took the `elif is_christmas` branch and
  landed in a plain **`Holiday`** pool — which the configured
  `christmas_playlist_marker` ("christmas") does **not** match, so the seasonal
  exclusion did not cover it.

## The fix

**1. The family merges to ONE umbrella token, `Holiday`** (the user's choice —
calling it "Christmas" would label a Hanukkah track as Christmas). New
`HOLIDAY_CANONICAL_TOKEN`, `HOLIDAY_FAMILY_ALIASES` (kept equal to
`album_classification_service.CHRISTMAS_GENRE_TOKENS` by a test),
`is_holiday_family_token`, `canonical_holiday_token` and `merge_holiday_tokens`
(order-preserving, so the playlist builder's 3-genre window is unaffected).

**2. The album blend only keeps the token when the ALBUM is seasonal.** New
`album_is_holiday_album` — TRUE when the album TITLE names it (album-scoped
evidence, so it always counts) or when a **majority** of the album's tagged
tracks carry a holiday token. A majority, not "any": one seasonal track on an
otherwise ordinary record is a per-TRACK fact. A per-track title alone never
qualifies.

**3. Every seasonal spelling pools under ONE marker-matched key.**
`_HOLIDAY_POOL_KEY = "christmas holiday"` / display `"Holiday"`. ⚠️ The key
must contain the default marker, because the marker is what decides whether a
playlist may hold seasonal music — a "Holiday"-only pool would have been
excluded-when-it-should-not-be and invisible to the user.

## Verified before/after (the same probe, pre-fix tree vs fixed tree)

| Scenario | Before | After |
| --- | --- | --- |
| **1 of 6 tracks tagged** (the report) | `['Christmas']` — whole album stamped | `[]` — token dropped |
| Genuine seasonal album (6 of 6) | `['Rock', 'Christmas']` | `['Rock', 'Holiday']` |
| Christmas-titled album, no tags | `['Rock']` | `['Rock', 'Holiday']` |
| One row of one (`test_christmas_is_still_album_valid`) | `['Rock', 'Christmas']` | `['Rock', 'Holiday']` |

## What was NOT changed

* A genuine Christmas album still carries the tag — now as `Holiday`, and for a
  title-named album it survives even with **no** genre tags at all (an
  improvement: it previously needed a tag to be detected).
* Cover / Live / Acoustic stay per-track affiliations, exactly as before.
* The Christmas **exclusion** policy (marker, Essential Collection, New Music)
  is unchanged; only the pool key it matches was corrected.

## Tests

`tests/test_holiday_album_propagation.py` (new, 25): the family vocabulary and
merge, the album-level verdict (majority rule, title authority, the 50% tie,
the zero-denominator guard, the per-track-title exclusion), the end-to-end
album blend for the reported album, and the pool-key/marker contract.

**Oracle:** the new module cannot even import against the pre-fix code (the
symbols are new), and the behavioural probe above shows the propagation plainly
on the pre-fix tree.

`tests/test_genre_filter_tags_do_not_leak.py` — one assertion **changed**:
`test_christmas_is_still_album_valid` now expects `Holiday`. The property it
pins (a seasonal album is album-valid) is unchanged; only the merged spelling
moved, and its fixture is a 1-of-1 album so the majority rule keeps the token.
The change is documented in the docstring.

Sweep of 19 genre/playlist/album suites diffed against a pristine clone —
**identical failure sets, 0 new failures** (the 5 pre-existing failures are in
`test_genre_consensus_aggregation.py` and `test_genre_backslash_splitting.py`,
unrelated). `import app` → 425 routes.

## Follow-up (same day) — the denominator was still wrong

Reported again with the SAME album, plus the log evidence:

```
Excluded Christmas tracks from playlist 'Everclear - Essential Collection'
    excluded=7 kept=24
[GENRE_COVER] cover applied genre='Holiday' playlist='Holiday - Top Tracks'
```

⭐⭐ **THE MAJORITY RULE WAS COMPUTED OVER THE WRONG DENOMINATOR.**
`_album_holiday_votes` counted only the rows that HELD a genre, so an album
where ONE track carried data (the Christmas one) and the other thirteen had
NONE scored **1-of-1 = 100%** → seasonal → the Holiday token stayed in the
blend, and the album-wide write then stamped all 14 rows. Measured:

| Shape | Before | After |
| --- | --- | --- |
| 14 tracks, 1 seasonal, 13 with NO genre data | **SEASONAL** | not seasonal |
| 14 tracks, 1 seasonal, all 14 tagged | not seasonal | not seasonal |
| 1-track release, 1 seasonal | seasonal | seasonal (correct) |
| 12 tracks, 10 seasonal | seasonal | seasonal (correct) |

The album blend for the reported shape went `['Hard Rock', 'Holiday']` →
`['Hard Rock']`.

⭐⭐ **A MAJORITY MUST BE OVER THE ALBUM'S REAL TRACK COUNT.** A denominator
that shrinks to fit the evidence converts "one track of fourteen" into "every
track we looked at" — a minority becomes a majority.

**The existing damage is repaired.** With the blend now non-empty and
Holiday-free, the album-wide write fires and replaces the stale value:
verified end to end, **14/14 rows carrying Holiday → 0/14** after one scan.

⚠️ A first attempt ALSO imposed an absolute minimum of 2 seasonal tracks. That
was wrong — a one-track seasonal release IS seasonal — and it broke the pinned
`test_christmas_is_still_album_valid`. Removed; the corrected denominator alone
is the rule.

Added to the same suite: the denominator algebra (every row counted, untagged
rows not inflating the holiday count), the reported album's blend, and the
end-to-end repair. **Oracle: the 4 new tests fail on the previous commit.**
Sweep of 18 genre/playlist suites vs a pristine clone — identical failure sets,
0 new failures.

## Files

* `services/enrichment/genre_aggregation_service.py` — the family vocabulary,
  `merge_holiday_tokens`, `album_is_holiday_album`, `_album_holiday_votes`, and
  the album-blend holiday gate in `get_track_recommendations`.
* `services/popularity/stages/finalise_stage.py` — `_HOLIDAY_POOL_KEY` /
  `_HOLIDAY_POOL_DISPLAY` and the pool intercept.
* `tests/test_holiday_album_propagation.py` — new.
* `tests/test_genre_filter_tags_do_not_leak.py` — the one changed assertion.
