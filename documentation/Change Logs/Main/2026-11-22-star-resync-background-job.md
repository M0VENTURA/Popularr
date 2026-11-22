# Star Rating Resync runs in the background (was a blocking request)

**Date:** 2026-11-22
**Area:** `services/popularity/star_resync_service.py` ·
`routes/misc_routes.py` · `services/scanning/pipelines/progress_service.py` ·
`static/js/activity-feed.js` · `static/js/config.js` ·
`test_site/static/js/pages/config.js` ·
`templates/pages/config.html` · `test_site/templates/Pages/config.html`
**Commit:** `feat(popularity): run the star rating resync as a background job`

## Reported / observed

The Config page's **Star Rating Resync** had Preview and Apply buttons, and it is
the correct tool for the question *"apply yesterday's scoring change to all
currently scored tracks"* — it re-rates every scored track from stored
popularity data with no Last.fm / ListenBrainz / Discogs calls, and it applies
the age-adjusted velocity + collision dampening pass
(`_apply_velocity_adjustments`) that the resync is the only place to run
catalogue-wide.

The problem was **how** it ran: Apply was a synchronous request —

```python
# routes/misc_routes.py, before
result = await asyncio.to_thread(resync_star_ratings, ...)   # held until done
```

On a real library the re-rate outlives the reverse proxy's ~100s origin
timeout. The proxy answers with its **own HTML error page**, so the card's
`resp.json()` dies on `Unexpected token '<'` — the same failure the genre →
playlist resync had already fixed. The work *completed*; only the response was
lost, and there was no progress to show while it ran.

## What changed

The apply moves to a **daemon thread** and publishes progress to a shared
`scan_states` row, mirroring `genre_playlist_resync_service`:

* `start_star_resync()` — single-flight claim, spawns a `star-resync` thread,
  returns `{"ok": true, "started": true}` immediately. Returns
  `started=False` (not an error) when a run is already in flight, and refuses
  when the resync is disabled in config.
* Progress frames (`phase`, `percent_complete`, `processed/total_items`,
  current album) are written to the `star_resync` row, so both the Config card
  and the dashboard render the run live.
* `POST /api/ratings/resync/apply` → `202 Accepted` + thread (new).
* `GET  /api/ratings/resync/status` → the progress row (new).
* `POST /api/ratings/resync` is **kept** for the read-only Preview and for
  back-compat.
* `star_resync` added to `progress_service.SCAN_TYPES` and given a display name
  in `activity-feed.js`, so the dashboard's existing poller surfaces it.

Verified end-to-end against a stubbed rater: Apply returned **202 in 0.039s**
(previously it blocked for the whole run), and polling went
`running → complete` with the velocity report intact.

## `albums_per_batch` now means something

`single_detection.star_resync.albums_per_batch` was read by the config getter
and collected by the UI, but `resync_star_ratings()` never used it — it looked
like a throttle and did nothing.

It is now the **progress-report cadence** (one frame per N albums), **not** a
load chunk. This is deliberate: `_apply_velocity_adjustments` normalises each
track against its artist's *fastest* track and finds ListenBrainz collisions
**across the whole catalogue**, so splitting the load into batches would
silently change ratings. A test pins that the load is not chunked
(`test_the_load_is_not_chunked_by_albums_per_batch`) and that velocity runs
before rating.

## The card

Both trees' collectors now treat Apply as fire-and-poll:

* POST `/api/ratings/resync/apply`, then `pollStarResync()` every 1.5s while
  `is_running`, showing phase / percent / current album.
* 409 (already running) is shown as a warning, not an error.
* On completion it reports ratings changed, tracks, albums and — when velocity
  is on — how many tracks were velocity-weighted and how many collisions were
  damped.
* A run started from another tab (or still running from before a reload)
  **resumes polling on page load** rather than leaving an idle Apply button.

Preview is unchanged and still inline: it is read-only, usually quicker, and a
rejected preview must not start a job.

## Files

* `services/popularity/star_resync_service.py` — `SCAN_TYPE` / `PROGRESS_FILE`,
  `_write_progress`, `_progress_row_is_stale`, `is_resync_running`,
  `start_star_resync`, plus progress frames in `resync_star_ratings`
* `routes/misc_routes.py` — `/ratings/resync/apply` (202) and
  `/ratings/resync/status`
* `services/scanning/pipelines/progress_service.py` — `star_resync` in
  `SCAN_TYPES`
* `static/js/activity-feed.js` — display name for `star_resync`
* `static/js/config.js`, `test_site/static/js/pages/config.js` —
  `runStarResync` fire-and-poll, `pollStarResync`, load-time resume
* `templates/pages/config.html`, `test_site/templates/Pages/config.html` —
  card copy ("Apply runs in the background")
* `tests/test_star_resync_background.py` — new (18 tests)

## Tests

`tests/test_star_resync_background.py` → **18 passed**. It covers:

* visibility: `SCAN_TYPE` in `SCAN_TYPES`, a dashboard display name, the
  progress file matching the scan type;
* the start/poll contract: `start` returns at once and does **not** run the
  rating pass inline, refuses when disabled, is single-flight, and a stale row
  is treated as not-running rather than blocking forever;
* the maths guard: the load is not chunked by `albums_per_batch`, and velocity
  runs before `_compute_stars_for_album`;
* both collectors POST to the background endpoint, poll status, and resume on
  load; and both routes are registered while the old inline route is kept.

The related suites (`test_album_star_curve_and_resync`,
`test_resync_rebuilds_playlists`, `test_velocity_scoring`,
`test_genre_playlist_resync`, `test_config_collector_scope`) pass, and the
config/star/playlist/genre sweep's failure set is byte-identical to the
`c9acd427` baseline (all pre-existing).
