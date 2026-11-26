# Popularity scan engine — audit findings

Audited ~35k lines across 34 modules in `services/popularity` plus its caches and
the MusicBrainz client. Everything below is either FIXED or deliberately left,
with the evidence needed to act on the remainder.

## Fixed — round 1 (commit 43624f9f)

| # | Defect | Where | Evidence |
|---|---|---|---|
| 1 | `mbid_cache.json` written every 30 s and on exit, **never read** | `musicbrainz_service.py` | `CACHE_FILE` only ever appeared in an `open(..., "w")`. Every restart re-paid a rate-limited (1 req/s) recording search per track — up to 5000 entries ≈ 83 min. |
| 2 | `_build_album_listener_distributions` recomputed per track | `track_stage.py` | Benchmarked **1287 µs → 0.5 µs** per call once memoised on `album_context`. Reached 3× per track. |
| 3 | `_album_edition_annotation` walked every album title, per track | `track_stage.py` | Album-shaped pure function; memoised on `album_context`. |
| 4 | `_artist_dominant_genres` — artist-level LIMIT-500 query per genre-less **track**, no cache | `track_stage.py` | Now bounded + lock-guarded, keyed `(artist, max_genres)`. |
| 5 | Genre caches evicted **FIFO**, not LRU | `track_stage.py` | `next(iter(dict))` is insertion order; the scan inserts in album order, so the hottest keys were discarded first. `_MB_RECORDING_GENRE_CACHE` is per-recording, so any library >4000 thrashed at 1 req/s. |
| 6 | Two service instances each wrote only their own map to the shared file | `musicbrainz_service.py` | Last flush deleted the other's entries. Now merges with disk content and writes atomically. |

## Fixed — round 2

| # | Defect | Where | Evidence |
|---|---|---|---|
| 7 | **O(tracks × catalogue) inside the album curve** | `finalise_stage.py` `_assign_stars` / `post_album_star_ratings` | The artist-catalogue reference was rebuilt per track (a list comprehension over the whole catalogue), and both percentile helpers re-filtered and re-sorted their pool on every call. Benchmarked **1977 ms → 8.4 ms per album** (12 tracks / 600-track catalogue) — **234×**. Runs TWICE per album, so ≈30 min of pure CPU saved per 1000-album scan. Stars verified byte-identical. |
| 8 | Two per-row UPDATE loops, twice per album | `scan_stage_runner.py` `_persist_album_relative_scores` / `_persist_popularity_marking` | One statement per row → one executemany per album. 2T statements became 2. The per-row `continue` guards still filter, before execution. |
| 9 | `_run_full_enrichment` ran **twice per metadata scan** | `album_stage.py` | `enrich_album` calls it when not deferred; `enrich_album_extras` calls it unconditionally. `defer_full_enrichment` is only set for a `_full_pass`, so metadata-only (the default mode) paid everything twice: the online album-art hunt (Navidrome → CAA → AudioDB → Discogs, uncached when the album has no art), the release MB fetch, and per-track genre writes on remix albums. Now memoised on `album_context`, returning the **values** — a bare "already done" flag would blank `similar_artists` / `artist_metadata`. |
| 10 | Cover detection had **no freshness gate** | `track_stage.py`, `popularity_cache_policy.py` | Every non-forced scan re-ran `detect_cover_song` for every track. A track with a `work_mbid` builds a fresh `CoverDetector` and fans out work-rels / recording-rels / Last.fm lookups — up to ~50 requests, with 6 per-instance caches discarded each time. Added `cover_verdict_is_fresh` (mirrors `singles_detection_is_fresh`), and the quick path now STAMPS `cover_last_checked` on a settled negative — without the stamp nothing could ever be fresh. |
| 11 | End-of-scan ISRC sync ran **after** the deferral was cleared | `popularity_pipeline.py` | An abandoned artist worker (the hazard `scan_scope` exists for) could reach its own `finalise_scan`, see deferred False, and run the whole-library self-join a second time, concurrently. |
| 12 | The **legacy full-library path never ran the ISRC sync at all** | `scanning/pipeline.py` | It deferred the same way as the dashboard "All" scan but never called it at the end, so ISRC popularity was simply never propagated on that path. |
| 13 | `_sync_essential_playlist`'s 4-arm OR had one **unindexed** arm | `db/schema.py`, migration `038` | Arms 1–2 use `idx_tracks_album_artist_trim` (migration 007). `LOWER(TRIM(artist))` had no index, so the planner could not bitmap-OR and fell back to a **sequential scan of `tracks` per artist** (~1000/scan). |
| 14 | `LIMIT 500` on the popularity-cache read silently truncated **deep cuts** | `db/repositories/popularity_cache.py` | The cap is on the artist-wide sweep, ordered by listener count — so a deep cut was invisible and re-fetched every scan. Always hit for "Various Artists". The requested titles are now fetched separately and cannot be truncated. |
| 15 | Unbounded per-process caches | `musicbrainz_service.py`, `track_stage.py` | `_artist_singles_cache`, `_album_year_cache`, `_single_result_cache` grew by library size forever; `_TRACK_ARTIST_REGISTRY_CACHE` likewise. Now bounded (LRU / size-capped). |

## Corrected audit premises

Three claims from the first pass were wrong and are recorded so they are not
re-derived:

- **"`_sync_isrc_popularity` runs twice per full scan"** — false. The two call
  sites are mutually exclusive via `scan_scope._deferred`. The real defects were
  the ordering (#11) and the missing legacy call (#12).
- **"`_assign_stars` is the enclosing loop"** — it is per-track; the loop is in
  `post_album_star_ratings`. The fix belongs there.
- **"`_sync_essential_playlist` predicates cannot be indexed"** — two of the four
  arms already were; only `LOWER(TRIM(artist))` was missing.

## Measured and deliberately NOT changed

- **`get_features_config()` caching.** ~10 calls/album in the skip gate plus one
  per track via `should_freeze_track`, so it looks hot — but it benchmarks at
  **0.37 µs/call both ways**, 3.7 ms across 1000 albums. Reverted; pinned by a
  test so it is not "fixed" again.

## Fixed — round 3

| # | Defect | Where | Notes |
|---|---|---|---|
| 16 | `_fetch_essential_featured_rows` ran **eighteen leading-wildcard `ILIKE`s** over three columns | `finalise_stage.py` | No index serves `%x%`, so every call seq-scanned `tracks`; eight comparisons per column is also far more work than one. Now six portable `LOWER(col) LIKE` terms (`%feat%`, `%ft%`) — **broader**, which is safe because every row is re-validated in Python by `_track_has_featured_artist`. `LOWER() LIKE` rather than `~*` so it runs on SQLite too. |

## Found, not fixed (ranked)

1. **`_post_album_stars` runs twice per album** (provisional + reconcile). The
   second pass is needed — the global 5★ lock and marking cutoffs are
   artist-wide — but the star UPDATE deliberately rewrites every row on pass 2 to
   flip `stars_reconciled`, so the "unchanged skip" does not save writes.
   *Fix:* reconcile only albums whose inputs changed.

2. **Per-track config getters** (~18-20/track: `resolve_weights`,
   `get_single_boost`, `get_metadata_score_floor`, `get_log_ratio_config`,
   `get_interlude_lb_outlier_config`, `get_genre_track_limit` ×3,
   `get_tagging_config`, `get_metadata_update_config` ×3). Constant per scan.
   *Not measured* — likely small individually.

3. **`_run_full_enrichment` re-UPDATEs release-extended fields** on every album
   pass with no "already filled" WHERE (`album_stage.py` ~2016), so it writes
   identical values. Cheap per row, but unconditional.

4. **Discogs / Last.fm per-album calls with no cross-album cache:**
   `_fetch_external_genres`' AudioDB artist call and `_record_discogs_album_id`
   are per album even though both are artist-level facts.

5. **`_load_track_artist_scores`** (`scan_stage_runner.py` ~1743) is an uncached
   artist-catalogue SELECT per credited artist; its cache lives for one call.
   The artist-catalogue query exists in 4–5 overlapping forms.

6. **`_fetch_essential_vocalist_rows`** selects the whole 4★+ slice once per
   scan (`finalise_stage.py` ~3997), and
   **`_refresh_all_essential_vocalist_collections`** runs unconditionally when
   results are non-empty. No dirty check.

7. **`_album_curve_star` still rebuilds `valid_album` per track** inside the
   compiled-pool path when a caller passes a raw (unmarked) list. The scan's own
   path now passes `_SortedPool`, so this only affects direct callers.

## What already works well

- The album skip gate (`scan_stage_runner.py` ~2969-3170) is genuinely good:
  pure in-memory over already-loaded rows, runs BEFORE any network call, and
  `skip_unchanged_albums` + `was_album_scanned` short-circuit whole albums.
- Score reuse (`track_stage.py` ~2361) is correctly strict: fresh AND credible
  AND both sources non-zero.
- Per-album write batching (`DeferredPersistSink` → one `upsert_tracks_bulk`),
  and now the two stragglers.
- `get_recording_identity_popularity` seeding from sibling rows of the same
  recording is correct (album-scoped copy, not the artist-level dict).
