# Unified scan log: lifecycle, heartbeat, final results and named playlist outcomes

**Date:** 2026-10-31
**Area:** scan logging / presentation only
**Files:** `helpers/scan_log.py` (new), `helpers/logging_config.py`,
`helpers/config_helpers.py`, `services/log_service.py`,
`services/popularity/scan_stage_runner.py`,
`services/popularity/stages/track_stage.py`,
`services/popularity/stages/finalise_stage.py`,
`tests/test_scan_log_events.py` (new)

## Scope

Presentation only. No provider matching, popularity formula, star threshold,
cover rule, genre acceptance rule, playlist selection or frozen-score behaviour
was changed. The detailed instrumentation remains behind `logging.level: debug`.

## Event layer

`helpers/scan_log.py` provides one emitter:

```
[SCAN] Checked 4/12: Hysteria (event=track.checked, track_id=…)
[SCAN] [WARN] Could not update playlist: "Rock - Top Tracks" (event=playlist.failed, failed=1)
[SCAN] [DEBUG] Provisional score: Hysteria (event=track.provisional_score, score=85.0)
```

* Stable `event=` names (scan.started/completed, album.started/final_results,
  track.started/checked/preserved/skipped, saving.*, files.tags,
  ratings.updated, playlist.created/updated/unchanged/skipped/failed/summary,
  scan.heartbeat, genres.unavailable).
* Structured context with sensitive keys (`token`, `password`, `api_key`,
  `authorization`, cookies/session) redacted.
* INFO uses the existing `log_unified`; WARN/ERROR use `log_unified_level`;
  DEBUG evidence uses `log_scan_detail`.
* Every emit is fail-safe; a logging fault cannot break a scan.
* `[SCAN]`/`[PLAYLIST]` were added to the Scanner panel's
  `_scan_activity_filter`, so the new lines are visible there.

## Lifecycle

* **Start**: `Starting scan: <scope>` with mode, album count, candidate track
  count, scan id and force flag (once).
* **Album**: `[1/2] Artist — Album · 12 tracks`.
* **Track**: `Checking: <title>` (config-gated) and
  `Checked n/total: <title>`, `Preserved n/total: <title> · popularity frozen`,
  or `Skipped n/total: <title>`.
* **Results**: one authoritative per-album block after stars are final:
  `Final results: <album>` with `★ score title` and either a shared genre line,
  album defaults + accurate exceptions, or `no genres resolved`.
* **Persistence**: `Saving results...`, `Results saved: N track(s)`,
  `Results NOT fully saved...` (WARN), `File tags updated: N changed`,
  `Navidrome ratings: N synced · N failed · N unchanged`.
* **Completion**: `Finished: <scope> in Xm Ys — <outcome>`, with album/track/
  preserved/failed and playlist counts. Distinct outcomes: `completed`,
  `completed_with_errors`, `cancelled`, `failed`.

The decorative `ARTIST SCAN STARTED` / `ALBUM 1 OF 2` banners and the
`ALBUM SUMMARY` block (whose genre counters were always 0) are no longer
emitted; the helpers remain for compatibility/tests.

## Heartbeat

One coordinator heartbeat (`ScanHeartbeat`), not one per worker. After
`logging.progress.heartbeat_seconds` (default 15) without `touch()`, it emits
`Still <activity>... · elapsed Ns`, repeats while active, resets on progress and
stops on completion/cancellation/failure. It never labels work "stalled".

## Playlist outcomes

`_sync_playlist_to_navidrome` records one outcome per playlist:
`created` / `updated` / `unchanged` / `skipped` / `failed`. Created and failed
are named at INFO/WARN. Updated is named at INFO outside a batch (manual
Regenerate) and DEBUG inside a batch; unchanged names are DEBUG. A batch
`Playlist refresh complete: N created · N updated · N unchanged · N skipped ·
N failed` summary is emitted at scan completion, global Essential reconciliation
and per-playlist Regenerate. Skipped is not counted as unchanged.

## Warning aggregation

`WarningAggregator` keeps the first detailed warning, reports repeats every N
occurrences and emits a recovery line. The Navidrome rating sync uses the shared
`SCAN_WARNINGS`; the scan completion emits the unresolved total.

## Configuration

```yaml
logging:
  level: INFO
  progress:
    show_track_starts: true
    show_track_completions: true
    heartbeat_seconds: 15
  results:
    show_final_ratings: true
    show_final_genres: true
    genre_display: defaults_with_exceptions   # or per_track / none
    show_provisional_scores: false
  playlists:
    show_created: true
    show_updated: true
    show_failed: true
    show_unchanged_individually: false
  diagnostics:
    provider_payloads: false
```

Read by `get_scan_logging_config()`; partial overrides keep the other defaults.

## Representative sample

> Constructed from the shipped message shapes; not captured from a live scan.

Before (excerpt):

```
══════════════════════════════════════════════════════════════════════
🎵 ARTIST SCAN STARTED
══════════════════════════════════════════════════════════════════════
Artist: Silent Civilian
Albums Found: 2
Mode: Forced Scan
──────────────────────────────────────────────────────────────────────
ALBUM 1 OF 2
Ghost Stories
──────────────────────────────────────────────────────────────────────
[POPULARITY] Album 1/2 (Forced Scan): Silent Civilian - Ghost Stories (11 tracks)
[TRACK] 🎵 "Hysteria" | Score: 85.0 ...
[TRACK_RESULT] 'Hysteria' -> Final: 85.0 (LF: 80.0 | LB: 90.0)
[ALBUM SUMMARY] ... Genres Added: 0 ... Genres Removed: 0 ...
```

After:

```
[SCAN] Starting scan: Silent Civilian (event=scan.started, mode='Forced Scan', albums=2, tracks=24, force=True)
[SCAN] [1/2] Silent Civilian — Ghost Stories · 11 tracks (event=album.started, artist='Silent Civilian')
[SCAN] Checking: Hysteria (event=track.started, artist='Silent Civilian', album='Ghost Stories')
[SCAN] Checked 1/11: Hysteria (event=track.checked, track_id='…')
[SCAN] Checking: Another Track (event=track.started, ...)
[SCAN] Still checking 1/2 albums · Silent Civilian — Ghost Stories... · elapsed 15s (event=scan.heartbeat, ...)
[SCAN] Results saved: 11 track(s) for Ghost Stories. (event=saving.completed, count=11)
[SCAN] File tags updated: 8 changed · 3 correction(s) for Ghost Stories. (event=files.tags, ...)
[SCAN] 🔗 Navidrome ratings: 11 synced · 0 failed · 0 unchanged for 'Silent Civilian'. (event=ratings.updated, ...)
[SCAN] Final results: Ghost Stories (event=album.final_results, tracks=11)

[SCAN]   5★   85.0  Hysteria
[SCAN]   4★   72.4  Another Track
[SCAN] Album genre defaults: Power Metal, Progressive Metal
[SCAN] Track genre differences:
[SCAN]   Another Track: Power Metal only
[SCAN] Created playlist: "Silent Civilian - Essential Collection" · 12 tracks (event=playlist.created, ...)
[SCAN] Playlist refresh complete: 1 created · 3 updated · 8 unchanged · 0 skipped · 0 failed (event=playlist.summary, ...)
[SCAN] Finished: Silent Civilian in 2m 14s — completed (event=scan.completed, outcome='completed')
[SCAN]   2 albums · 24 tracks processed · 2 preserved · 0 failed
[SCAN]   Playlists: 1 created · 3 updated · 8 unchanged · 0 skipped · 0 failed
```

## Unavailable diagnostics (not fabricated)

These are not measured today and need separate instrumentation:

* provider candidate counts and `_lastfm_candidate_ok` rejection reasons;
* provider payloads / cache hit-miss counters;
* per-playlist added/removed track deltas and ordering-only changes;
* genre added/removed counts (the old summary hard-coded 0);
* a "ListenBrainz measured vs zero" flag (Last.fm has
  `lastfm_playcount_available`);
* effective weights/ceilings per track (the score payload carries the values,
  not which constants were applied).

## Tests

`tests/test_scan_log_events.py`: event format/redaction, heartbeat emission and
stop, touch reset, config interval, warning first/repeat/recovery + unresolved
summary, playlist outcome mapping/summary, final results (shared genres,
defaults+exceptions, unresolved, ratings hidden), config defaults/overrides,
wiring and the Scanner filter prefixes.
