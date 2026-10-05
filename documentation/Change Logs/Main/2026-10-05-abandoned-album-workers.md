# Abandoned album phases now hand their worker to the drain (2026-10-05)

**Report:** popularity-scan log review —
`[SCAN] section abandoned (timeout) section='album_track_phase' elapsed_s=900.002`
followed by `abandoned_albums=1` and *Popularity scan processed 0 tracks*.

## What the budget actually does

`_bounded_call_report` stops **waiting** at the budget and returns
`abandoned` — it does not stop the work. The thread it spawned is a daemon and
is *still running* when the scan moves on, still holding:

- a connection out of the DB pool (`pool_size=10`, `pool_timeout=30`), and
- a slot on the shared HTTP rate limiter.

The artist-level path has always handed those stragglers to
`_drain_abandoned_workers` (cap `features.full_scan_max_abandoned_workers`,
default 1, clamped 0–16). The **album-level** path did not: it propagated only
the sentinel, so album stragglers accumulated unchecked — the same cascade one
level down, with the next album waiting on a pool the previous one had not
released.

## Changes

- **New `services/scanning/abandoned_workers.py`** — the bounded-drain
  machinery moved out of `popularity_pipeline.py` so *both* callers can use it
  without importing each other (that file already imports
  `_bounded_call_report` from the scan runner, so runner → pipeline would have
  closed the loop). `popularity_pipeline` re-exports the underscore names, so
  `pp._drain_abandoned_workers`, the constants and the existing monkeypatches
  keep working — asserted by `TestTheArtistAndAlbumPathsShareOneImplementation`.
- **`_track_abandoned_album_worker(report)`** in `scan_stage_runner.py`, called
  from the abandoned branch of `_bounded_album_phase`. The registry is
  module-level on purpose: the three `_bounded_album_phase` call sites live in
  different scopes and a per-scope list would reset the cap to zero each time.
- The caller-visible sentinel is unchanged (no `thread` key) — every album call
  site only tests `result.get("abandoned")`.
- A drain failure is logged at debug and swallowed, and the worker **stays**
  registered: dropping it would hide the very thing the cap exists to bound.

The wait itself is unchanged: grace 5s, hard ceiling 60s, two independent
bounds (deadline + iteration ceiling), so draining can never become a new way
for one album to stall the scan. `0` still means "never wait".

## Tests

`tests/test_abandoned_album_workers.py` — 14 tests: registration (and the
sentinel never leaking the handle), the drain receiving the worker with the
configured cap, a drain failure neither breaking the phase nor forgetting the
worker, module-level registry semantics, single-definition drift guard, plus
the scheduler assertions below.

`tests/test_abandoned_artists_are_stopped.py` — all **34** still green after
the extraction.
