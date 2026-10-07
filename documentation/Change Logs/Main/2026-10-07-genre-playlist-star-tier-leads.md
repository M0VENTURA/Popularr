# Genre playlists: the star tier now leads the order (2026-10-07)

## Reported

> The top genre playlist ordering didn't work as well as I had expected with
> 3 star songs being added to the playlists now

## Root cause — the clamp I removed in `da0575d3` was supplying the star preference

`da0575d3` (monotonic ordering key) replaced `album_prominence_score` with
`playlist_popularity_score`: the maximum of the two raw log scores, **unclamped**.

That fix was correct — the old measure was non-monotonic (it rewarded *missing*
ListenBrainz data) and saturated at 100 from ~1.78M Last.fm listeners — but the
saturation was **also** what ordered the head of every popular playlist by star
tier:

* everything above ~1.78M listeners scored exactly `100.0`, so it TIED;
* the next key was `-stars`, so 5★ came first, then 4★, then 3★.

Remove the clamp and there are no ties left: raw listener counts decide, and a
3★ track with the most listeners genuinely outranks a 5★ track with fewer. It
was landing at position 1.

So `da0575d3` fixed one defect and silently discarded a preference the pool
already depended on. **The star tier has to be stated explicitly, not inherited
from a rounding rule.**

## Measured (600-track synthetic pool → the first 300 tracks, spread on)

| final playlist | OLD (clamped) | FLAT (`da0575d3`) | **TIER (this fix)** |
|---|---|---|---|
| 5★ | 86 | 60 | **181** |
| 4★ | 147 | 119 | 119 |
| 3★ | 67 | **121** | **0** |
| tier inversions | 19 | 45 | **0** |
| first 6 tracks | `4*59 3*08 3*51 5*52 4*33 5*47` | `3*46 5*31 3*00 3*57 4*52 4*45` | `5*46 5*45 5*44 5*43 5*42 5*41` |

FLAT vs OLD is the "54 more 3★ / 26 fewer 5★" figure quoted in the docstrings
(121 − 67 = 54, 86 − 60 = 26).

> ⚠️ Two earlier readings of these numbers were WORTHLESS: printing `★` to a
> cp1252 console raises `UnicodeEncodeError` mid-run and truncates the output,
> so a partially-printed table looked like a real one. Re-measured with
> ASCII-only output (`5*`/`4*`/`3*`).

## The fix

`finalise_stage._playlist_order_key` now sorts on the **star tier first**:

```python
return (
    -int(row.get("stars") or 0),          # tier gates the position
    -float(_effective_order_score(row, mode)),   # unclamped popularity ranks INSIDE it
    -float(row.get("popularity_score") or row.get("score") or 0),
    str(row.get("title") or "").casefold(),
)
```

* the tier gates **position**, the cross-album popularity score ranks **within**
  the tier (where stars separate nothing — they are equal by construction);
* no inversions remain, so a 4★ track can never precede a 5★ track;
* determinism is preserved (`title` last), so the Navidrome push cache settles;
* the Essential Collection already orders this way
  (`_ordered_by_percentile_then_prominence`), so the two playlists now agree.

The old objection to a star key is documented rather than deleted: stars ARE
album-relative (`_assign_stars` rates against the track's own album and artist),
so they are a **coarse tier**, not a precise cross-album measure — which is
exactly why they gate position and the unclamped score does the ranking.

## Files

* `services/popularity/stages/finalise_stage.py` — `_playlist_order_key`, and
  the stars caveat in `_effective_order_score`
* `tests/test_playlist_ordering.py` — 67 tests (62 → 67)

## Tests

**One existing test inverted** — `test_stars_are_not_used_as_the_fallback`
asserted "Loud 4 Star" first. It encoded the old contract, and its rationale
(a star tier "cannot rescue a missing signal") was correct as a *tie-break* rule
under the clamp — the behaviour it pinned was an artefact of saturation, not a
design. It is now `test_star_tier_leads_and_popularity_ranks_inside_it`, with
the inversion and its reason recorded in the docstring, plus a control that
popularity still ranks inside a tier.

New class `TestStarTierLeadsThePlaylist` (5): no 3★ track ever leads (both
order modes), popularity orders within a tier, the head of a mixed pool is all
5★ with no 3★ before position 13, and determinism survives the tier key.

**Oracle:** reverting `finalise_stage.py` → **4 failed / 63 passed** — exactly
the four tier discriminators; the controls (`within a tier`, `determinism`)
stay green as they must. Restored → 67 passed.

**Sweep:** 15 playlist/popularity suites, clean `37a57011` worktree vs this
change — **BASE `21 failed, 288 passed` → NEW `21 failed, 293 passed`**
(+5 = the new tests), `Compare-Object` **empty** → identical failing sets, all
pre-existing.

## If 3★ tracks are in the pool at all

Ordering now puts them last (and a capped playlist excludes them entirely), but
**membership** is a separate gate: the pool query is

```sql
WHERE COALESCE(stars, star_rating) >= :min_stars
```

with `playlists.genre_playlists_min_stars`, **default 4** (Config page →
"Minimum Stars"). If 3★ tracks are still being admitted, one of these is true:

1. `genre_playlists_min_stars` is set to `3` in `config.yaml` — set it to `4`;
2. the stored `stars` for those rows is ≥4 while something else *displays* 3★ —
   the playlist reads the DB, and the artist/album pages read the same
   `COALESCE(stars, star_rating)`, so check what Navidrome itself shows (its
   stars come from `setRating`, a separate sync);
3. it is a different playlist — the Essential Collection queries use a
   hard-coded `>= 4`.
