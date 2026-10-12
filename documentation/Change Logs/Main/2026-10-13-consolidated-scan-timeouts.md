# Consolidated scan timeouts: ONE knob (2026-10-13)

## Report / request

> "Should we remove these two settings [Per-Album Scan Timeout, Per-Artist Scan
> Timeout]? The track timeout should be the main timeout option, then the album
> could be the track timeout x how many tracks in the album."

## What changed

The scan-timeout model was three independent dials, one of which (`track_timeout_seconds`)
was DEAD — imported in `scan_stage_runner` but never called (per-track timers were
deliberately removed to stop zombie threads). Now there is ONE knob:

`popularity.track_timeout_seconds` (Config page **Per-Track Scan Timeout**,
default **30 s**, clamped 10–600, `0` = disable the whole safety net).

| Budget | Old | New (derived) |
| --- | --- | --- |
| Per-album track phase | `max(album_timeout 900, 20 s × tracks)` | `track_timeout × track count` (default 30 ⇒ the reported 80-track no-MBID compilation gets 2400 s instead of the 1600 s it had to work with) |
| Per-album fast phases (enrich, post-singles) | flat `album_timeout` | flat `track_timeout` |
| Per-artist (full scan) | `artist_timeout + album_timeout × album_count` | `1800 s fixed overhead + track_timeout × artist TOTAL TRACK count` — new `library.get_track_counts_by_artist()` |

`0` semantics: `track_timeout_seconds = 0` disables everything (no album ever
skipped, no artist ever abandoned) — the old album/artist `0`-disable contracts
fold into the one knob.

## Why this is safe

- The "Various Artists is ONE artist holding every compilation" fix is preserved:
  the artist budget scales by the artist's real work (total tracks), never a flat size cap.
- The 80-track-album-cut-off-mid-progress regression stays fixed: a large album is
  bounded by `track_timeout × tracks`, with the per-track unit now operator-controlled.
- The dead `track_timeout_seconds` becomes the single source of truth instead of a
  config option that did nothing (its own defect class).

## Removed / changed surface

- `config_helpers.get_album_timeout_seconds()` / `get_artist_timeout_seconds()` DELETED (no callers after this change); `get_track_timeout_seconds()` rewritten (default 30, 0=disabled).
- `scan_stage_runner`: `_resolve_album_budget()` reads the track knob; `_scale_album_budget()`
  is now `base * track_count`; the hardcoded `_ALBUM_BUDGET_PER_TRACK_SECONDS = 20.0` constant is GONE.
- `popularity_pipeline`: artist budget = `_ARTIST_FIXED_OVERHEAD_SECONDS (1800) + track_timeout × tracks`.
- `_zero_track_summary` advice now names the **Per-Track Scan Timeout** label.
- Deleted the **Per-Album Scan Timeout** and **Per-Artist Scan Timeout** cards from the Config
  page (both trees) and their collectors from both `config.js` copies; the track card's
  label/help/default updated to describe the derived budgets.
- Tests re-pinned: `test_track_timeout_config.py` (default 30, clamp 10–600, 0=disabled),
  `test_album_scan_budget.py` (track-knob resolution, artist budget scales by TRACK count,
  Config-page contract = track input present / old dials absent),
  `test_album_budget_scales_with_track_count.py` (base × count, no 20 s constant),
  `test_abandoned_artists_are_stopped.py` (pipeline reads the track knob; artist bound =
  fixed overhead when the count is unknown/0).

## Notes

- Existing `config.yaml` files may still carry `popularity.album_timeout_seconds` /
  `artist_timeout_seconds`; they are inert after this change and are dropped by the next
  Config-page save.
- The prefetch budget (`prefetch_budget_seconds`) is a different concern (per-artist bulk
  network work) and is unchanged.