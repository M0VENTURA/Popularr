# Corrections area: full audit, applied fixes, and recommended flows

**Date:** 2026-11-24
**Area:** `routes/misc_routes.py` · `routes/artist_routes.py` · `routes/metadata.py` ·
`routes/ui_routes.py` · `services/metadata/correction_service.py` ·
`services/metadata/pending_update_service.py` ·
`services/popularity/stages/album_stage.py` · `db/engine.py` ·
`test_site/static/js/pages/corrections.js` · `.../artist-corrections.js`
**Commit:** `perf(corrections): offload blocking work, batch fix-all, retry deadlocks`

## The corrections surfaces (inventory)

| Surface | Route | What it does | Nav entry? |
|---|---|---|---|
| **Tag Corrections** | `/correcting` | album-level tag inconsistencies (21 fields) + pending provider conflicts | yes |
| **Artist corrections** | `/artist/<name>/corrections` | 6 panels: duplicate artists, duplicate tracks, missing core metadata, album-MBID conflicts, disc inconsistencies, album splits, title mismatches, missing MB tracks | from the artist page |
| **Metadata Comparison** | `/metadata-compare` | "Keep This Data" / "Use MB release" per album | **no nav entry — orphaned** |
| **Album review** | `/album/<a>/<b>` Lookup MBID | propose → edit → Save Metadata | yes |
| **Dashboard "needs approval"** | `/api/metadata/pending-recommendations` | the same stashed proposals, grouped by album | yes |

All three "pending proposal" renderers read the same `tracks.pending_mb_updates`
envelope, which is good — one store, three views.

## What caused the timeouts (traced, then fixed)

### 1. Blocking work on the event loop — **fixed**

The apply handlers were `async def` but called synchronous services inline:
`fix_album_field` (DB + rewrite every audio file), `apply_album_mbid`,
`merge_albums`, `clear_disc_number`, `delete_track`, and every `/api/conflicts/*`
handler. On hypercorn one worker runs one event loop, so the whole album's disk
phase froze **every other request in that worker** — the reported timeouts.

Every one now uses `await asyncio.to_thread(...)`. The guard test
(`test_async_routes_do_not_block_event_loop.py`) missed them because the blocking
call was one frame deeper than its hard-coded name list.

### 2. "Fix all (majority)" issued N requests, each rewriting every file — **fixed**

`fixAllMajority` POSTed once per inconsistent field; each POST re-selected the
album, ran an `UPDATE`, and rewrote **all** of the album's audio files. A 12-track
album with 10 fields = 120 tag rewrites (3–4 full file passes each) instead of 12.
New `POST /api/correcting/fix-album-fields` applies every field in one statement
and one file pass; the page uses it and passes a 5-minute timeout instead of the
30 s default.

### 3. The corrections page computed the whole library to draw 20 rows — **fixed**

`/correcting` ran a library-wide `GROUP BY` with 21 `COUNT(DISTINCT)` plus **one
detail query per flagged album**, then sliced to 20 in Python. `limit` now bounds
the per-album detail queries and the route fetches at most 201 albums, off-thread.

### 4. Deadlocks between album-wide writers — **fixed (retry)**

Postgres log:

```
ERROR: deadlock detected
Process 1413: INSERT INTO tracks … ON CONFLICT (id) DO UPDATE …
Process 1378: UPDATE tracks SET musicbrainz_album_mbid = … WHERE … album='Planets'
```

Five writers target "every row of one album" by predicate and take row locks in
planner order. `db_session` retries only *session acquisition*, not the body, so a
deadlock propagated. `db.engine.run_with_deadlock_retry` now retries the whole
unit of work on SQLSTATE 40P01, applied to the tag stamp, `_clear_album`, and both
MBID fan-outs. `upsert_tracks_bulk` already retried (its "lock" substring test
covers "deadlock").

⚠️ **An occasional `deadlock detected` in the Postgres log is normal** — Postgres
picks a victim; the app retrying is the fix. What matters is that no request
fails. Note the 15:05 log line still showed the pre-fix bulk `WHERE id IN (...)`
stamp, so that server was a build behind.

## Recommendations (not yet implemented)

Ordered by value.

1. **Make the corrections writes finish the loop.** `fix_album_field`,
   `apply_album_mbid`, `clear_disc_number` and `merge_albums` rewrite file tags but
   none call `request_rescan(...)`. Navidrome serves its own DB, so the split the
   page repaired can reappear until an unrelated scan happens. Call the rescan
   service (the album save already does).

2. **Retire `/metadata-compare`.** No nav entry; its "Beets" column is actually
   `musicbrainz_genres`; it writes `beets_year` / `mb_override` / `metadata_locked`
   which are absent from the ORM model and (for `metadata_locked`) never read. It
   duplicates the album review with a worse flow.

3. **Prune `metadata_conflicts`.** Resolved/ignored rows are never deleted and the
   `ON CONFLICT … WHERE status='pending'` clause means a resolved row can never be
   re-opened by a later scan — a genuine regression is silently dropped. Add a
   retention sweep (like the queue tombstones) and allow re-opening.

4. **Fix `/api/track/match-missing`.** The handler takes only `mb_title`, writes
   only `title`, returns `updated_title`; both callers read `old_title` /
   `new_title` / `updated_file` and claim "(audio tags updated)". The toast shows
   `undefined → undefined` and no file changes.

5. **Bound the artist corrections page.** Six sequential full scans of the artist
   plus a quadratic duplicate detector, and the page then fires two requests per
   MB album with unbounded `Promise.allSettled` concurrency. Add a cap and a
   pooled, batched fetch.

6. **One dedupe engine.** Duplicate tracks are computed by both
   `artist-corrections` and `GET /api/album/duplicate-tracks` from the same
   definition; missing MB tracks appear in four places. Consolidate.

7. **Dead code to delete**: `POST /api/conflicts/resolve-batch`,
   `GET /api/artist/corrections-albums`, `GET /api/correcting/albums` — no caller
   in either controller.

## Tests

`tests/test_deadlock_retry.py` — detection (message, 40P01, not 40001), retry
succeeds, persistent deadlock raises after N attempts, non-deadlock is not retried.
