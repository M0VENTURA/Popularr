# Diacritic folding on the Last.fm lookup path (accented artist names)

## Symptom

Every track on *Throwing Copper* was scored from a few hundred Last.fm
listeners despite the band having millions of global plays:

```
Lightning Crashes  | Score: 68.2 | LF: 589
Selling the Drama  | Score: 62.1 | LF: 381
All Over You       | Score: 61.7 | LF: 368
I Alone            | Score: 60.5 | LF: 480
```

Those numbers are not the track's global listeners — they are a near-empty
fallback object. `Lightning Crashes` alone has ~1.85M listeners.

## Root cause — three independent points all compared accented spelling verbatim

The band is credited `Lïve`.  Last.fm's catalogue indexes that artist under the
ASCII spelling **`Live`**.  Nothing in the lookup chain folded the accent
before comparing, so the correct global row was rejected at every stage:

| # | Location | Defect |
|---|----------|--------|
| 1 | `lastfm_service.artist_match_score` | Compared `'lïve'` vs `'live'` → score **0**. The caller discards any candidate scoring `< 60`, so the real row was thrown away as a "mismatch". |
| 2 | `lastfm_service.build_artist_lookup_candidates` | Built only `['Lïve']` — never offered `Live`, so no retry could succeed. |
| 3 | `popularity_matching.normalize_for_aggregation` | The `[^a-z0-9]+` sweep turned the combining mark into a **space** and split the word: `'Café'` → `'caf '`, `'Hoppípolla'` → `'hopp polla'`. |

Also affected: `make_artist_match_key` / `make_track_match_key` (NFKC does
**not** remove combining marks, so `Lïve` and `Live` occupied separate cache
buckets), and the two Last.fm catalogue cache keys
(`popularity_sources._lastfm_artist_catalog_cache`,
`popularity_cache_service._lf_top_tracks_cache`).

The chain then fell through to the bare `get_track_info` fallback, which
returned the few-hundred-listener object, and the rating maths consumed it.

**This is generic, not Lïve-specific.** `Motörhead`→`Motorhead`,
`Sigur Rós`→`Sigur Ros`, `Björk`→`Bjork` and `Beyoncé`→`Beyonce` all scored 0
on the same gate.

## Fix

A single shared helper — `helpers/normalization_service.strip_diacritics()` —
folds combining marks to their ASCII base letters.  It removes **only**
combining marks, so casing and punctuation survive and it is safe to apply
ahead of any other comparison.

Applied at every point on the chain:

1. **`artist_match_score`** and **`normalize_artist_for_compare`** fold before
   comparing, so the accented and ASCII spellings match.
2. **`build_artist_lookup_candidates`** offers the folded spelling as its own
   candidate (and folds the bracket-stripped / featured-stripped variants).
3. **`get_artist_top_tracks`** — the single source of an artist's global
   listener counts — retries the folded spelling when the verbatim query comes
   back empty.  A successful first query costs no extra request.
4. **`normalize_for_aggregation`** folds *before* the punctuation sweep, so
   accented words are no longer split at the accent.
5. **`make_artist_match_key` / `make_track_match_key`** fold, so both spellings
   share one cache bucket.
6. Both **catalogue cache keys** fold, so a spelling change does not re-fetch
   the same catalogue twice.

`normalize_string` was refactored to use the new helper (identical behaviour).

## Files

- `helpers/normalization_service.py` — new `strip_diacritics()`; `normalize_string` reuses it.
- `services/enrichment/lastfm_service.py` — fold in `normalize_artist_for_compare`;
  folded candidates in `build_artist_lookup_candidates`; folded retry in `get_artist_top_tracks`.
- `services/popularity/popularity_matching.py` — fold in `normalize_for_aggregation`,
  `make_artist_match_key`, `make_track_match_key`.
- `services/popularity/popularity_sources.py` — folded catalogue cache key.
- `services/popularity/popularity_cache_service.py` — folded catalogue/tag cache keys.
- `tests/test_lastfm_diacritic_folding.py` — 42 tests.

## Tests

`tests/test_lastfm_diacritic_folding.py` covers the helper itself, the
match-score gate (including that folding does **not** make it permissive —
`Lïve` vs `Nirvana` still scores 0), candidate construction, the folded retry
(and that an ASCII artist makes no second request), the title key no longer
splitting on the accent, the cache keys, and an end-to-end run asserting
`Lightning Crashes` resolves **1,850,000** listeners rather than 589.

Regression sweep over 15 Last.fm/popularity suites: 25 failures before and
after, identical lists — **0 new failures**.  `import app` still resolves all
387 routes.
