# Top genre playlists are 4★/5★ only; Punk Rock and Hardcore merge

**Date:** 2026-11-20
**Area:** `services/enrichment/genre_vocabulary.json` ·
`services/enrichment/genre_selection.py` ·
`services/enrichment/genre_aggregation_service.py` ·
`services/popularity/stages/finalise_stage.py`
**Commit:** `fix(genre): enforce the star floor in Top Tracks, merge punk rock
into punk and hardcore punk into hardcore`

## Reported

> "Tracks rated only 3 star are showing in top genre tracks playlists, also punk
> and punk rock are showing as separate genres, hardcore and hardcore punk are
> also separate genres, hardcore is closely aligned to metalcore"

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
  its old tier (only the resync apply and the star-persist path invalidated it,
  not an ordinary finalise);
* **`COALESCE(stars, star_rating)`** — a stale second rating column could satisfy
  the threshold while the displayed `stars` reads 3.

`_create_genre_top_track_playlists` now re-asserts the floor on the row's own
`stars` after it has resolved its rows, whatever route they came from — the same
"hard membership floor" the Essential Collection already applied
(`rows = [row for row in rows if int(row.get("stars") or 0) >= 4]`).

`_verify_genre_playlist` gained a **`BELOW_STAR_FLOOR`** check. Nothing used to
test membership against the floor: `LOWER_STAR_USED` compares tiers *within* the
already-filtered pool, so a leaked track produced no log line at all.

Ordering already put 3★ last (`_playlist_order_key` leads on the star tier,
2026-10-07), which is why the symptom was "in the playlist" rather than "at the
top" once the pool exceeded the cap.

## 2. Punk Rock → Punk, Hardcore Punk → Hardcore

The vocabulary is the authority for "is this one genre or two", and it listed
both spellings as **separate entries** — so `_pool_genre_key` built two keys and
the builder wrote two playlists.

| was | now |
|---|---|
| `punk` + `punk_rock` (specificity 1) | one `punk`, `"punk rock"` / `"punk-rock"` aliases |
| `hardcore_punk` (`"hardcore"` alias) | one `hardcore`, `"hardcore punk"` / `"hardcore-punk"` aliases |

`pop_punk` / `ska_punk` / `riot_grrrl` reparent from `punk_rock` to `punk`;
`melodic_hardcore` / `post_hardcore` / `grindcore` / `metalcore` reparent from
`hardcore_punk` to `hardcore`.

Because the merge removes the ancestry edge that used to absorb the umbrella,
`("punk", "rock")` is now stated explicitly in `mutually_exclusive` — the report
that previously drove "rock removed by punk rock" (175 tracks in the export)
still holds, but no longer by a naming coincidence.

The legacy vote engine mirrors the same merge in `_BUILTIN_SYNONYMS`,
`_SPECIFIC_TO_GENERIC` and `_GENERIC_ROOTS`, or it would emit the two spellings
the vocabulary just collapsed.

## 3. Hardcore is aligned with Metalcore

`metalcore` descends from `hardcore` (and stays **metal-family**, so the
metal-over-rock policy still suppresses rock around it). The pair moved from
`conditional_keep` ("both survive when the narrower has track evidence") to
`mutually_exclusive` — the same rule `alternative_metal` / `alternative_rock`
uses: one identity, and the ANCESTOR is dropped. Metalcore is the more specific
reading of that sound.

## Files

* `services/enrichment/genre_vocabulary.json` — the two merges and reparenting
* `services/enrichment/genre_selection.py` — `DEFAULT_MUTUALLY_EXCLUSIVE`,
  `DEFAULT_CONDITIONAL_KEEP`, doc examples
* `services/enrichment/genre_aggregation_service.py` — legacy mirror tables
* `services/popularity/stages/finalise_stage.py` — builder floor + self-check
* `tests/test_genre_playlist_star_floor.py` — new (10 tests)
* `tests/test_genre_redundancy_cleanup.py` — `TestPunkAndHardcoreAreOneGenre`
* `tests/test_genre_selection_engine.py`, `test_genre_metal_over_rock_policy.py`,
  `test_genre_selection_evidence_order.py`, `test_rock_metal_conflict_arbitration.py`,
  `test_genre_pooling_and_weekly_rotation.py` — assertions updated to the
  canonical names

## Tests

* **Oracle:** with the builder floor disabled, `test_genre_playlist_star_floor`
  → **3 failed / 4 passed** (the three membership tests; the self-check still
  fires and names the track). Restored → **all pass**.
* `test_genre_selection_engine`, `test_genre_redundancy_cleanup`,
  `test_genre_metal_over_rock_policy`, `test_genre_selection_evidence_order`,
  `test_rock_metal_conflict_arbitration`, `test_genre_pooling_and_weekly_rotation`,
  `test_playlist_ordering`, `test_playlist_selection_integrity`,
  `test_covers_genre_playlists` → **416 passed**.
* Sweep of the genre/playlist/star selection vs a clean `HEAD` worktree: the
  pre-existing failure set is **identical** (network-dependent and unrelated
  suites); the only delta is the intended inversion documented above.

## If a 3★ track still appears

Set `playlists.genre_playlists_min_stars` to 4 and rebuild the genre playlists.
If a track rated 3★ in the app is still admitted, its stored `stars` is ≥4 and
the app is displaying something else (`star_rating`, or Navidrome's own
`setRating` sync) — the builder now filters on the same column the playlist
query selects.
