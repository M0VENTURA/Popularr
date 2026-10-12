# The per-album scan budget scales with the album's track count (2026-10-12)

> **SUPERSEDED 2026-10-13** by
> `2026-10-13-consolidated-scan-timeouts.md`: the flat 900 s floor and the
> hardcoded 20 s/track constant are gone — `track_timeout_seconds` is now the
> single knob and the album budget is `track_timeout × track count`. The
> 80-track-album regression this change fixed stays fixed; the per-track unit
> is simply operator-configurable now.

## Report

"Scanned albums with no mb/match seem to fail at the end before hitting the
finalise stage." Log for a forced scan of **Various Artists - Alternative 90s**
(80 tracks, no release-group MBID):

```
[SCAN] section abandoned (timeout) section='album_track_phase' elapsed_s=900.001 budget_seconds=900.0 artist='Various Artists' album='Alternative 90s'
[POPULARITY] Album 'Various Artists - Alternative 90s' skipped (exceeded 900.0s budget) — continuing with the artist's remaining albums
Popularity Scan - no tracks processed: 1 album(s) exceeded their per-album time budget ...
[FINALISE_STAGE] Finalising scan — 0 tracks processed
```

## Root cause — the flat 900 s cap confused "slow album" with "stuck album"

The album's track phase ran the full **900 s budget** while **still
progressing** — 65 tracks logged `[TRACK]` lines before the cut (17:02:53 … →
17:12:11), and after `✅ Scan complete` the abandoned worker kept grinding the
remaining ~15 tracks (17:13:33–17:15:06). This was not a hang. The album has
no release-group MBID, so every track did a FULL per-track online lookup
(MusicBrainz 1 req/s + Last.fm + Discogs + per-track cover detection) —
~14 s/track aggregate across 4 workers, i.e. ~20 minutes for 80 tracks.

When the budget tripped, the runner's album-skip path (pinned by
`test_album_scan_budget`: deliberately NOT a `request_cancel`, only that
album is skipped) **discarded the whole album's results** — none of the 65+
completed track results reached star posting or `finalise_scan` → "0 tracks
processed", `[FINALISE_STAGE] Finalising scan — 0 tracks processed`, no star
ratings for the album. The "no MBID match" correlation is exact: albums
without MBIDs are precisely the ones whose tracks each require the full
online path; well-matched albums hit the DB-first caches and finish fast.

## Fix — the configured value is a FLOOR; large albums scale up

`services/popularity/scan_stage_runner.py`:

* **`_ALBUM_BUDGET_PER_TRACK_SECONDS = 20.0`** and **`_scale_album_budget
  (base, track_count)`** — pure helper: `base` stays the budget for albums
  whose `track_count * 20s ≤ base`; larger albums get `track_count * 20s`.
  `None` (album_timeout_seconds=0 = "disable the budget") passes through
  untouched — `0` still means never skip.
* The **track-phase** `_bounded_album_phase` call now passes
  `seconds=_scale_album_budget(_album_budget, len(_track_jobs))` — 80 tracks
  → **1600 s** instead of a 900 s cut. `enrich_album` and
  `post_singles_enrichment` keep the base `_album_budget` (their cost is not
  per-track; the post-singles phase has its own prefetch budget).
* `_resolve_album_budget()` and the `"0 = disabled"` semantics are unchanged.

A genuinely HUNG call is still bounded — the budget remains a finite safety
net, it just no longer doubles as an accidental size limit. A fast album
never reaches the ceiling, so scaling up is free for the common case.

Config help text (both `templates/pages/config.html` + test_site) now reads:
"**base (minimum)** budget … a large album **scales up** (about 20 s per
track) so a big compilation … is never cut off mid-progress."

## Tests — `tests/test_album_budget_scales_with_track_count.py` (14)

* pure helper: small/empty albums keep the base, 80 tracks → 1600 s, the
  configured value is a floor, `None` passes through (disabled), monotonic in
  track count, boundary at 45/46 tracks;
* wiring: the track-phase call scales, `enrich_album` keeps the base, the
  budget is still resolved once per scan, every bounded album phase still
  carries `seconds=`, the per-track constant is referenced by the helper;
* end-to-end on the reported album: 80 tracks → 1600 s (> flat 900 s), yet
  finite (a genuine hang is still bounded).

**Oracle** (revert the track-phase call to `seconds=_album_budget,`):
`test_the_track_phase_call_scales` FAILS; the pure-helper tests pass (the
helper is new code — the discriminator is the wiring).

**Sweep** — 46 scan/budget/config/popularity suites: 17 failing, **identical
set at pristine `29b94d19`** (SQLite `no such table: scan_states/scan_
history` harness flakes, `test_scan_stall_fixes` source pins,
`test_popularity_marking`/`math` pre-existing, `test_full_scan_as_artist_
pipeline` heartbeat) → 0 regressions. `import app` OK (399 routes).

## Note for the user

* No config change needed — the new behaviour is a strictly-larger budget for
  large albums at the same `album_timeout_seconds`. If a particular giant
  album still exceeds even the scaled allowance, raise
  **Per-Album Scan Timeout** (it remains the floor), or set **0** to disable.
* An 80-track no-MBID album will now take its full ~20 min to scan (it's
  doing a real online lookup per track) — that is expected, and it will now
  complete and reach the finalise stage instead of being cut at 15 min.