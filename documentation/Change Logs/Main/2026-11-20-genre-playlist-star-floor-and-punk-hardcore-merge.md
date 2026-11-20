# Top genre playlists are 4★/5★ only; Punk Rock merges into Punk, Hardcore splits from Hardcore Punk

**Date:** 2026-11-20
**Area:** `services/enrichment/genre_vocabulary.json` ·
`services/enrichment/genre_selection.py` ·
`services/enrichment/genre_aggregation_service.py` ·
`services/popularity/stages/finalise_stage.py`
**Commit:** `fix(genre): enforce the star floor in Top Tracks; merge punk rock
into punk; make Hardcore a metal genre`

## Reported

> "Tracks rated only 3 star are showing in top genre tracks playlists, also punk
> and punk rock are showing as separate genres, hardcore and hardcore punk are
> also separate genres, hardcore is closely aligned to metalcore"

Clarified:

> "Hardcore Punk and Hardcore are different genres. Hardcore Punk is Punk,
> Hardcore is Metal. Currently Hardcore has Hardcore punk included which was the
> problem"

So the two punk cases are **not** symmetric:

| pair | wanted |
|---|---|
| Punk / Punk Rock | ONE genre |
| Hardcore Punk / Hardcore | TWO genres, different families |

## 1. The 3★ tracks — the star floor lived in exactly one place

The pool query has always said

```sql
WHERE COALESCE(stars, star_rating) >= :min_stars
```

with `playlists.genre_playlists_min_stars`, default 4. That predicate was the
**only** gate, so it held for a normal global rebuild but was not a property of
the BUILDER. Three routes reached the selection loop without it:

* **`rows_override`** — the genre/playlist resync PREVIEW substitutes an
  arbitrary row set and never calls the query;
* **`_GENRE_ROWS_CACHE`** — serves rows for 120 s keyed on `min_stars` alone, so
  a rating DEMOTED to 3★ between the fetch and the rebuild was still pooled at
  its old tier;
* **`COALESCE(stars, star_rating)`** — a stale second rating column could satisfy
  the threshold while the displayed `stars` reads 3.

`_create_genre_top_track_playlists` now re-asserts the floor on the row's own
`stars` after it has resolved its rows, whatever route they came from — the same
"hard membership floor" the Essential Collection already applied
(`rows = [row for row in rows if int(row.get("stars") or 0) >= 4`).

`_verify_genre_playlist` gained a **`BELOW_STAR_FLOOR`** check. Nothing used to
test membership against the floor: `LOWER_STAR_USED` compares tiers *within* the
already-filtered pool, so a leaked track produced no log line at all.

Ordering already put 3★ last (`_playlist_order_key` leads on the star tier,
2026-10-07), which is why the symptom was "in the playlist" rather than "at the
top" once the pool exceeded the cap.

## 2. Punk Rock → Punk (one genre)

The vocabulary listed both spellings as **separate entries**, so
`_pool_genre_key` built two keys and the builder wrote two playlists.

* removed the `punk_rock` entry; `"punk rock"` / `"punk-rock"` are aliases of
  `punk`
* `pop_punk` / `ska_punk` / `riot_grrrl` reparent from `punk_rock` to `punk`

Because the merge removes the edge that used to absorb the Rock umbrella,
`("punk", "rock")` is now stated explicitly in `mutually_exclusive` — the report
that drove "rock removed by punk rock" (175 tracks in the export) still holds,
but no longer by a naming coincidence.

## 3. Hardcore is METAL; Hardcore Punk is PUNK (two genres)

The defect was that `"hardcore"` was listed as an **alias of `hardcore_punk`**,
so a metal Hardcore tag classified as the punk genre and the track was pooled
into the Hardcore Punk playlist.

* `hardcore_punk` is its own entry again: `"Hardcore Punk"`, `parent: punk`,
  family **punk**, no alias
* `hardcore` is a NEW entry: `"Hardcore"`, `parent: metal`, family **metal**
* `melodic_hardcore` / `post_hardcore` / `grindcore` reparent back to
  `hardcore_punk`
* `metalcore` descends from `metal` + `hardcore_punk` (its real fusion) and stays
  metal-family — which is the "closely aligned to metalcore" the report asked
  for, without making it the same genre as Hardcore

`DEFAULT_CONDITIONAL_KEEP` keeps `("metalcore", "hardcore_punk")` (both survive
when Metalcore has its own track evidence). `("metalcore", "hardcore")` is
deliberately NOT a pair — they are siblings in the metal family.

The legacy vote engine mirrors the split: `_BUILTIN_SYNONYMS` maps only
`"punk rock"` → `"punk"`, and `_SPECIFIC_TO_GENERIC` has `"hardcore punk"` → punk
plus `"hardcore"` → metal/heavy metal.

## Files

* `services/enrichment/genre_vocabulary.json` — punk_rock alias, hardcore as a
  metal genre, hardcore_punk restored, reparenting
* `services/enrichment/genre_selection.py` — `DEFAULT_MUTUALLY_EXCLUSIVE`,
  `DEFAULT_CONDITIONAL_KEEP`, doc examples
* `services/enrichment/genre_aggregation_service.py` — legacy mirror tables
* `services/popularity/stages/finalise_stage.py` — builder floor + self-check
* `tests/test_genre_playlist_star_floor.py` — new (10 tests)
* `tests/test_genre_redundancy_cleanup.py` — `TestPunkRockMergeAndTheHardcoreSplit`
* `tests/test_genre_selection_engine.py`, `test_genre_metal_over_rock_policy.py`,
  `test_genre_selection_evidence_order.py`, `test_rock_metal_conflict_arbitration.py`
  — assertions updated to the canonical names

## Tests

* **Oracle:** with the builder floor disabled, `test_genre_playlist_star_floor`
  → **3 failed / 4 passed** (the three membership tests; the self-check still
  fires and names the track). Restored → **all pass**.
* Genre/playlist suites (`test_genre_redundancy_cleanup`,
  `test_genre_selection_engine`, `test_genre_metal_over_rock_policy`,
  `test_genre_selection_evidence_order`, `test_rock_metal_conflict_arbitration`,
  `test_genre_pooling_and_weekly_rotation`, `test_playlist_ordering`,
  `test_playlist_selection_integrity`, `test_covers_genre_playlists`) → all pass
  except one pre-existing failure
  (`test_a_weakly_supported_companion_still_loses_its_slot`, identical on a
  clean `HEAD` worktree).

## If a 3★ track still appears

Set `playlists.genre_playlists_min_stars` to 4 and rebuild the genre playlists.
If a track rated 3★ in the app is still admitted, its stored `stars` is ≥4 and
the app is displaying something else (`star_rating`, or Navidrome's own
`setRating` sync) — the builder now filters on the same column the playlist
query selects.
