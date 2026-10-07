# Dashboard: a Finalise scan option (2026-10-07)

## Requested

> Can we also add a Finalise scan option to the dashboard? This will just run
> the finalise scan for all files using the existing database information from
> the files, only running the popularity and singles detection for files that
> are missing the information.

## What the mode does

A new `finalise` entry in the dashboard's scan selector (live + test_site),
dispatched through the existing `/api/popularity/run` machinery as
**`singles_only=True` + `finalise_only=True`** — the singles machinery is what
"reuse what exists, compute what is missing" already looks like in this
codebase, and the new flag adds the finalise-specific gates:

| Promise | Mechanism |
|---|---|
| every file is finalised | the album skip gates (freshness, skip-unchanged, completeness override) are bypassed for `finalise_only`, so every album reaches the per-album file-tag sync and the end-of-run `finalise_scan` |
| popularity only where missing | `track_stage`'s `_has_stored_popularity` branch reuses the stored score under the singles pass, and the runner forces `refresh_popularity_if_due` off (`_pop_due = False`) so a staleness window never refreshes a score that exists; a track with **no** score computes regardless |
| singles only where missing | `track_stage`'s `_sd_fresh` gate (timestamp + evidence) already skips fresh detection for every mode |
| DB information only | full enrichment stays deferred (`_full_pass` False), `enrich_album` takes the stored/heuristic type branch (`_detect_album_type`) instead of `_resolve_album_type` + `_persist_album_type_to_tracks`, annotation repair and cover detection are off (they key off the singles-pass flags), and the metadata-recommend stash + missing-track recompute gates gained `and not _mode_finalise` (both reach MusicBrainz) |

Star ratings (`_post_album_stars`) and `finalise_scan` itself still run —
recomputed from the scores in the database, which is exactly "using the
existing database information".

## Plumbing

- `routes/schemas.py`: `ScanRequest.mode` pattern += `finalise`.
- `popularity_pipeline.run_popularity_mode`: `elif mode == "finalise"` →
  `scan_type = "popularity_scan"` (same checkpoint/duplicate-guard family as
  every other non-full mode) + `singles_only`, `finalise_only`.
- `scan_stage_runner.run_popularity_scan`: new `finalise_only` parameter →
  `options`; `_mode_finalise`; progress label `"Finalise Pass"`.
- Dashboards: `<option value="finalise">Finalise</option>` (both trees) and
  `finalise`/`finalise_scan` labels in `SCAN_LABELS` / `SCAN_TYPE_DISPLAY_NAMES`
  (both trees), so the preflight confirm and recent-scan history name it.

## Tests

`tests/test_finalise_scan_mode.py` (23): the schema accepts `finalise` (and
still rejects junk), dispatch passes `finalise_only` + `singles_only` (with a
popularity-mode control), options threading, the skip-gate bypass, `_pop_due`
forced off, both network gates (`stash`, missing-track recompute) closed,
**the file-tag sync still open** (control — the pass is nothing without it),
the light enrich branch, `_full_pass` still False, and the dashboard option +
labels in all six UI files.

One existing source-anchor updated: `test_album_scan_budget.py` pinned
`if not force and not album_filter:` — retargeted to the new gate line
(the force-gating contract it defends is unchanged).

## Oracle

Stashed the ten source/UI files → **15 failed / 8 passed** (the 8 are the
intended controls: other modes still validate, junk still rejected,
popularity dispatch unchanged, `_full_pass` contract) → popped → markers
present → stash list back to 3.

## Verification

- Affected set (45 files, `scan_stage_runner` / `popularity_pipeline` /
  `schemas` / `album_stage` / dashboards): its only NEW failure was the anchor
  test above, fixed in place; remaining failures are baseline ids.
- Full suite → baseline reconciliation (`_final_full.txt`, shared with the
  Lookup-genre change — disjoint file sets, one run validates the combined
  tree).

## Follow-ups worth knowing

- The mode's history rows ride the `popularity_scan` family with
  `mode: "finalise"` recorded in the progress extra — recent-scans shows
  "Popularity Scan" for them; renaming the whole progress family would
  disturb the checkpoint/duplicate guards for every mode.
- `singles_detection_only` still exists as a legacy boolean flag path; finalise
  deliberately uses `singles_only` + its own flag rather than adding a third
  legacy flag.
