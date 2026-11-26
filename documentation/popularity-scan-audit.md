# Popularity scan engine — audit findings

Audited ~35k lines across 34 modules in `services/popularity` plus its caches and
the MusicBrainz client. Split into FIXED (this change) and FOUND, NOT FIXED
(ranked by expected saving, with the evidence needed to pick them up).

## Fixed in this change

| # | Defect | Where | Evidence |
|---|---|---|---|
| 1 | `mbid_cache.json` written every 30 s and on exit, **never read** | `musicbrainz_service.py` `_maybe_flush_cache` / (missing) load | `git grep CACHE_FILE` showed only the `open(..., "w")` call. Every restart re-paid a rate-limited (1 req/s) recording search per track — up to 5000 entries ≈ 83 min. |
| 2 | `_build_album_listener_distributions` recomputed per track | `track_stage.py:1058`, `:3019`, and via `_reconstruct_raw_blend` `:2402` | Benchmarked: **1287 µs → 0.5 µs** per call once memoised on `album_context`. 3 calls per track → 1 per album. |
| 3 | `_album_edition_annotation` walked every album title, per track | `track_stage.py` (def 713) | Album-shaped pure function; memoised on `album_context`. |
| 4 | `_artist_dominant_genres` — artist-level LIMIT-500 query per genre-less **track**, no cache | `track_stage.py` (def 574) | Replaced with a bounded, lock-guarded cache keyed `(artist, max_genres)`. |
| 5 | Genre caches evicted **FIFO**, not LRU | `track_stage.py` `_bounded_cache_put` | `next(iter(dict))` is insertion order. `_MB_RECORDING_GENRE_CACHE` is keyed per recording, so any library >4000 recordings thrashed and each miss cost 1 req/s. Now promotes on read. |
| 6 | Two service instances each wrote only their own map to the shared file | `musicbrainz_service.py` | Last flush deleted the other's entries. Now merges with what is on disk and writes atomically (temp + `os.replace`). |

## Measured and deliberately NOT changed

- **`get_features_config()` caching.** Called ~10×/album in the skip gate and
  once per track via `should_freeze_track`, so it looks hot. Benchmarked at
  **0.37 µs/call both ways** — 3.7 ms across 1000 albums. A cache would add a
  second global to invalidate for no measurable gain. Reverted.

## Found, not fixed (ranked)

1. **`_sync_essential_playlist` runs one full `tracks` scan per artist.**
   `finalise_stage.py:3584-3593`. Four predicates all on
   `LOWER(TRIM(COALESCE(...)))` expressions, so no index can serve them → a
   sequential scan per artist per scan (~900 on a 900-artist library).
   *Fix shape:* a functional index on the album-artist expression, or load the
   4★+ rows once per scan and filter in memory (the set is small by definition).

2. **ISRC self-join runs twice per full scan.**
   `finalise_stage.py:9987` (per `run_scan`) and again at
   `pipelines/popularity_pipeline.py:919` (scan end). Full `tracks` self-join +
   `GROUP BY`. *Fix:* a scan-scoped "already synced" flag.

3. **`_fetch_essential_featured_rows` uses 14 leading-wildcard `ILIKE`s.**
   `finalise_stage.py:3497-3507` → guaranteed sequential scan, once per scan.
   *Fix:* normalise "feat" into a boolean column at write time.

4. **O(T×N) inside `_assign_stars`.**
   `finalise_stage.py:1992-1995` rebuilds the artist catalogue in curve-metric
   space **per track**, and runs **twice per album** (provisional at
   `scan_stage_runner.py:4742`, reconcile at `:2564`). 12 tracks × 600 catalogue
   entries = 7,200 conversions, twice. *Fix:* precompute the metric list once per
   album, as `album_curve_pool` already is.

5. **`_post_album_stars` runs twice per album** (provisional + reconcile), so
   `_apply_album_relative_normalization` and `_persist_popularity_marking` each
   run twice, and both do one `UPDATE` **per row** (`scan_stage_runner.py:2001`,
   `:2022`). *Fix:* reconcile only albums whose inputs actually changed, and
   batch the UPDATEs.

6. **Cover detection has no negative cache.**
   `track_stage.py:3345-3505` → `detect_cover_song` for every track on every
   non-forced scan, and for a track with a `work_mbid` it builds a **fresh
   `CoverDetector()`** per track (`cover_detection_service.py:684`), discarding
   its per-instance caches. Singles detection already got
   `singles_detection_is_fresh` for exactly this shape; covers never did.
   *Fix:* a `cover_checked_at` freshness gate mirroring the singles one.

7. **`_run_full_enrichment` runs twice per album on a metadata scan.**
   `album_stage.py:2804` (from `enrich_album`) and `:2561` (from
   `enrich_album_extras`, reached at `scan_stage_runner.py:4209`). Album art,
   MB release fetch, live/remix tagging and alternate takes all paid twice.

8. **Per-track config getters.** ~18-20 per track
   (`resolve_weights`, `get_single_boost`, `get_metadata_score_floor`,
   `get_log_ratio_config`, `get_interlude_lb_outlier_config`,
   `get_genre_track_limit` ×3, `get_tagging_config`, `get_metadata_update_config` ×3).
   Constant per scan. *Not measured* — likely small individually, but worth a
   single `_scan_config` snapshot if the track path is ever profiled.

9. **`LIMIT 500` on the popularity-cache read**
   (`db/repositories/popularity_cache.py:60-64`) silently drops deep cuts by
   listener count, so they are re-fetched. Always hit for "Various Artists".

10. **Unbounded memory caches that never expire:** `_artist_singles_cache`,
    `_single_result_cache`, `_album_year_cache` (`musicbrainz_service.py:769-771`),
    `_TRACK_ARTIST_REGISTRY_CACHE` (`track_stage.py:281`), and the Discogs
    release-tracklist caches. Growth is bounded by library size, so low risk —
    but nothing clears them between scans and `clear_prefetch_memory_caches()`
    is never called by the runner.

11. **`_load_track_artist_scores`** (`scan_stage_runner.py:1743`) is an
    uncached artist-catalogue SELECT per credited artist; its `catalogue_cache`
    (`:1703`) lives only for one call. The artist-catalogue query exists in 4-5
    overlapping forms across the codebase.

## What already works well

- The album skip gate (`scan_stage_runner.py:2969-3170`) is genuinely good: it
  is pure in-memory over already-loaded rows, runs BEFORE any network call, and
  `skip_unchanged_albums` + `was_album_scanned` short-circuit whole albums.
- Score reuse (`track_stage.py:2361-2366`) is correctly strict: fresh AND
  credible AND both sources non-zero.
- The per-album write batching (`DeferredPersistSink` → one `upsert_tracks_bulk`)
  is the right shape.
- `get_recording_identity_popularity` seeding from sibling rows of the same
  recording is a good idea implemented correctly (album-scoped copy, not the
  artist-level dict).
