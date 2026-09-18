# Full scan progress: "0/?" counter and no stage detail

## Symptom

During a dashboard **"All" (full) scan** the Active Progress panel showed:

```
Active Progress
Popularity Scan                                       0/?
Venues - Aspire
```

The counter never advanced past `0/?`, and the panel did not convey where the
scan actually was.

## Root causes — five, all independent

### 1. The counter key the writer used is not the key the renderer reads

- **Writer** `_run_full_scan_as_artist_pipeline`
  (`services/scanning/pipelines/popularity_pipeline.py`) recorded
  `processed_artists` / `total_artists`.
- **Renderer** `static/js/dashboard.js` renders
  `` `${scan.processed_items || 0}/${scan.total_items || "?"}` ``.
- **Reader** `_normalise_entry` (`progress_service.py`) read only the
  ``*_items`` spellings, so both came back `None` → `0/?`.

`routes/scan_routes/api.py` already did this artist→item fallback for its own
`/api/popularity/status` response, which is why the bug hid: the compat
endpoint looked right while the entry the panel consumed did not.

### 2. The tracker merge skipped `full_scan`

The in-memory tracker *does* hold processed/total counts, but
`_merge_tracker_into_entry` only merged for
`{"popularity_scan", "library_scan", "combined_scan"}` — never `full_scan`.
Verified: identical state rendered `7/9` on a `popularity_scan` row and `0/?`
on a `full_scan` row.

### 3. The merge used `or`, and a blanket `update()`

```python
"percent_complete": tracker.get("progress") or entry.get("percent_complete"),
```

A legitimate `0` is falsy, so a stale percentage beat a real one. And
`entry.update({...})` copied `None` straight over good values, so a
partially-populated tracker erased the stage/item the DB row had set.

### 4. Two writers, two percentage scales, one row

This is the "doesn't properly detail where the scan is" half of the report:

| writer | value written to `full_scan.percent_complete` |
|---|---|
| orchestrator `_cb` | `_base + si*_sw + frac*_sw` — **0-100 across ALL artists**, monotonic |
| per-album checkpoint (`scan_stage_runner`) | `5 + album_index/total_albums*90` — **5-95 for the CURRENT artist** |

Both write the same row (`effective_stop_file` is `"full_scan"` when a
progress callback is wired). So every time a new artist started, the bar fell
back to ~5%.

### 5. The rebuilt JS had no display name for `full_scan`

`test_site/static/js/pages/dashboard.js`'s `SCAN_TYPE_DISPLAY_NAMES` lacked
`full_scan`, so that tree rendered the raw string `full_scan`. The live tree
had it.

## Fixes

- `_normalise_entry` now aliases the artist counters onto the item counters
  (`processed_items` ← `processed_artists`, `total_items` ← `total_artists`),
  using explicit `is None` checks so a real `0` is never replaced. Done at the
  single point the API contract is built, so every consumer benefits and the
  renderer no longer has to guess.
- The full-scan orchestrator writes **both** spellings (`*_items` and
  `*_artists`), including at start (`0/80` from the first frame, with
  `current_stage: "Metadata"`) and at completion.
- The per-album checkpoint no longer writes `percent_complete` to the
  `full_scan` row — it updates only artist/item there, since the orchestrator
  owns that percentage. The `popularity_scan` branch (a standalone scan) keeps
  its own percentage and now also records stage/counters.
- `_merge_tracker_into_entry` merges per-field and only when the tracker has a
  value; `percent_complete` prefers the tracker only when it has advanced past
  `0`. **`full_scan` stays excluded** — with a documented reason — because
  merging would overwrite the overall percentage with a per-artist one and
  downgrade display stage labels ("Popularity") to technical ones ("album").
- Both dashboard JS trees use `??` instead of `||` for the counter, so a
  legitimate `0` total renders as `0` rather than `?`.
- The rebuilt tree gained `full_scan`, `library_scan` and
  `missing_releases_scan` display names.

## Tests

`tests/test_full_scan_progress_counters.py` — 20 tests covering all five
causes: the `0/?` → `12/80` reproduction, item-counters winning when present,
zero-preservation, unknown-total still `?`, the tracker not overriding
`full_scan` (percentage, stage and item all survive), the tracker still
enriching `popularity_scan`, `None` not clobbering good values, plus
source-level guards that the checkpoint writes no percentage to the full_scan
row and that the writer records the `*_items` keys.

Verified against the pinned revision: the counter renders `12/80` where it
previously rendered `0/?`.

## Regression check

`test_full_scan_as_artist_pipeline`, `test_scan_log_filter`,
`test_scan_continuation_guard`, `test_scan_stale_stop_flag`: **identical**
before and after (44 passed, 3 failed either way).

### Pre-existing failures (NOT caused by this change)

These 3 fail identically on the unpatched tree:

- `test_full_scan_as_artist_pipeline.py::test_per_track_heartbeat_updates_current_item`
  — patches `popularity_pipeline.get_all_artists`, which the module no longer
  has (it imports it *inside* the function), so `monkeypatch.setattr` raises
  `AttributeError`. Stale test.
- `test_scan_stale_stop_flag.py::test_stale_full_scan_stop_cleared_before_loop`
- `test_scan_stale_stop_flag.py::test_stale_stop_cleared_via_clear_stop_request`

## Config

No new config keys.
