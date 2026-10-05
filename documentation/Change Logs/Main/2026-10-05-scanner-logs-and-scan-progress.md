# Scanner logs going quiet, and a status bar stuck on 0% (2026-10-05)

**Report:**

> full scan — 0% · Singles Detection · Afi - The Art of Drowning — Initiation
>
> This is showing that it's running, but there is nothing in the scanner system
> logs … but info log is showing it correctly

Asked what the Scanner tab actually showed, the answer was:

> It shows sometimes, but seems to break when multiple scans run at the same time

## Root cause 1 — several processes rotate the same file out from under each other

Popularr does not have one process writing its logs:

| writer | where |
|---|---|
| Hypercorn worker × 4 | `app.py` calls `setup_logging("WebUI")`, `entrypoint.sh` runs `--workers 4` |
| queue worker | `services/queue/queue_worker.py` calls `setup_logging("QueueWorker")` |

All of them attach the **root** logger to the same `unified_scan.log`,
`info.log`, `debug.log` and `error.log` — five independent
`RotatingFileHandler` instances, each with its own byte counter and its own
open descriptor.

The stock handler **renames** the live file to `<base>.1` when it rotates:

1. Process A rotates → `unified_scan.log` becomes `unified_scan.log.1`.
2. Process B still holds a descriptor on that (now renamed) inode, so every line
   it writes from then on goes into `.1` and **never reaches a reader that opens
   the live file by name** — which is exactly what
   `get_log_file_content("unified_scan.log")` does for the Scanner tab.
3. When B finally rotates in turn it renames the *new* live file over `.1`,
   destroying A's history.

The info log keeps filling because it is a different file with a different
rotation moment — so one file can look perfectly healthy while the other looks
dead. That is the "shows sometimes, but breaks when multiple scans run at the
same time" symptom exactly.

A second defect sat underneath it: `shouldRollover()` trusts `stream.tell()`,
this process's own byte count. `entrypoint.sh` copytruncates some logs, after
which that counter is stale and high while the file on disk is empty — the
handler then rotates a near-empty file over a perfectly good backup.

### The filter was *not* the problem

A scratch probe configured real logging, emitted one line through each of the
four paths the scan actually uses — `log_unified()`, a stdlib `services.*`
logger, and structlog loggers for `services.enrichment.musicbrainz_service`
and `services.popularity.popularity_sources` — and read the result back through
both `get_unified_log()` and `get_log_file_content()`. Every line reached
`unified_scan.log` and every line survived `_scan_activity_filter()`, so
widening or loosening the filter would have been a fix for a problem that does
not exist.

## Changes

- `helpers/logging_config.py`
  - New `SharedRotatingFileHandler` replaces
    `logging.handlers.RotatingFileHandler` for **all six** file handlers:
    - `shouldRollover()` decides from **`os.path.getsize(base)`** — the only
      value every process agrees on, and immune to a stale `stream.tell()`
      after an external copytruncate;
    - `doRollover()` **copies to `.1` and truncates in place** instead of
      renaming, so no descriptor is ever orphaned. This is safe because every
      writer opens the file with mode `"a"` (`O_APPEND`), which forces each
      write to the current end of file regardless of the descriptor's offset —
      no NUL-padded sparse files either.
    - Rotation is serialised across processes with an `O_CREAT|O_EXCL` lock
      that is stolen after `_ROTATE_LOCK_STALE_SECONDS`, so a crash mid-rotate
      can never leave a log unbounded.
    - The lock is named `.<basename>.rotate.lock` on purpose: `log_service`
      tails `<base> + ".*"` to find rotated backups, so a lock that matched
      that glob would be read back as if it were one.
  - The dictConfig body moved into `_build_logging_config(...)` so a test can
    assert which handler class every file handler uses — reverting to the stock
    class would otherwise re-introduce the bug silently.

## Root cause 2 — Singles Detection never narrated itself

- `services/popularity/stages/finalise_stage.py` logged the per-album result
  **only when an album actually yielded a single**:

  ```python
  if singles_detected:
      log_unified(f"Singles Detection - Detected {len(singles_detected)} single(s) ...")
  ```

  An album with no singles therefore left *no trace at all* in the Scanner tab,
  which is indistinguishable from a hung stage. It now reports every album, and
  says `skipped (popularity only)` rather than implying zero singles were found
  in a run that never looked for them.

- `services/scanning/pipeline.py` — the album loop derives the dashboard's
  stage bands (`metadata` → first quarter, `popularity` → middle half,
  `singles` → last quarter) but never logged a transition, so the status bar
  could say "Singles Detection" while the log said nothing. The band logic is
  now `stage_for_album_index()` and each transition is announced once per
  artist by `announce_stage_change()` (the callback fires per track as well as
  per album, so the state dict keeps it to one line per band):

  ```
  [SCAN_PIPELINE] Stage: Singles Detection — Afi / The Art of Drowning
  ```

  Both new lines pass `_scan_activity_filter()`, so they are visible in the
  Scanner tab.

## Root cause 3 — the percentage was truncated away

`_cb` in `services/scanning/pipelines/popularity_pipeline.py` computed:

```python
overall = int(_base + si * _sw + frac * _sw)
```

One artist's share of a full scan is `100 / total_artists` and its stage width
is a quarter of that. Artist 0 starts at `_base = 0`, so even at the *end* of
its last stage the value is `3 × 100 / (4 × total)` — for a 400-artist library
that is `0.19%`, and `int()` turns it into **0**. The bar read `0%` for whole
artists while real work was happening.

`services/scanning/pipelines/progress_service.py` then did
`int(state.get("percent_complete", 0) or 0)` on the way to the API, so even a
float payload would have been flattened again.

### Changes

- `_cb` keeps the value as a float (rounded to 3 dp, still clamped to 0–100,
  still forced to exactly `100` on the final callback).
- New `_as_percent()` in `progress_service.py` replaces the `int()` cast:
  clamps to 0–100, rounds to **3 dp**, and returns `0.0` for junk. Three
  decimals, not two: a 5 000-artist library puts the first album boundary at
  0.005%, which 2 dp would collapse straight back to `0`. Whole numbers
  stay whole, so existing consumers asserting `== 17` still hold.
- The status-bar **label** (not the progress bar's width) now formats the
  `(0, 1)` band as `<1%` via `formatScanPercent()`, added identically to
  `static/js/dashboard.js`, `static/js/main.js`,
  `test_site/static/js/pages/dashboard.js` and
  `test_site/static/js/main.js`. Progress bars keep using the raw number for
  `width:`.

## Tests

- `tests/test_shared_log_rotation.py` — 12 tests: every configured handler is
  the shared class; rotation is decided by the size on disk; an external
  copytruncate does not displace a good backup; **a rotation by one writer
  leaves the other writers still recording to the live file**; both writers
  survive several rotations; the file stays bounded and its backups chain; the
  lock path escapes the reader's backup glob. Oracle: reverting the handler to
  stock behaviour fails **4** of them.
- `tests/test_scan_progress_and_stage_logging.py` — 32 tests: stage-band math,
  one announcement per band, both new lines surviving the Scanner filter, the
  per-album singles result no longer AST-gated on `singles_detected`, `_as_percent`
  precision/clamping, and `formatScanPercent` driven through **node** against
  all four UI copies. Oracle: reverting the sources fails **25** of them.
- `tests/test_full_scan_as_artist_pipeline.py` — new
  `test_the_first_artist_of_a_large_scan_is_not_stuck_at_zero` runs a
  400-artist scan and asserts the first write is a real fraction
  (`0 < pct < 1`), still monotonic, still ending exactly on 100.

## Not changed

- `_scan_activity_filter()` itself — proven correct by probe (see above), so the
  Scanner tab's 300-line window was left alone.
- `popularr.unified` writes only to `unified_scan.log` (`propagate: False`),
  which is why that file is busier than `info.log` for the same period. That is
  intentional: `info.log` is the verbose fallback, not the scan feed.
