# Stuck "succeeded but never imported" queue items are now self-limiting (2026-10-12)

## Report

One queue log, three stuck-forever patterns:

```
slskd succeeded but no local file found (will keep polling) queue_id=6271 local_file_path='(empty)'
    found_filename='music/Moonspell/Moonspell - Album - 2008 - Night Eternal/0108 - Spring of Rage.flac'
Transfer file appeared within the grace window queue_id=6704 path='/downloads/2019 - Kankyō .../3-02 - Breathing New Life.flac'
Transfer file appeared within the grace window queue_id=6704 ...   ← re-logged EVERY ~13-min cycle for 45+ min
Transfer file appeared within the grace window queue_id=6705 ...
Transfer file appeared within the grace window queue_id=6707 ...
```

* **6271** — every cycle for 45+ minutes. `found_filename` uses a DISC+TRACK
  number style (`0108` = disc 01 + track 08); the basename matcher keeps the
  leading digits (`0108springofrage` ≠ `08springofrage`), so a file that
  landed with a differently-prefixed name is never found.
* **6704/6705/6707** — every cycle for 45+ minutes. The file EXISTS under
  `/downloads` (the log prints its path), but the caller's landed-path claim
  was silently skipped (already claimed by another row / moved) and the item
  was never imported, never failed.
* The **Pet Shop Boys covers** (`no_qualifying_result`, `rejected=
  {'no_artist_evidence': ...}`) and **Teresa Rotschopf** (`no_results`) are
  REAL misses — every top candidate is the ORIGINAL Pet Shop Boys track (or
  another artist), correctly rejected on the artist gate; the covers
  genuinely aren't on Soulseek. **Pitch Shifter** (Catharsis, Brutal
  Cancroid) downloaded + imported fine — the pipeline works.

## Root causes (`services/downloads/download_completion_service.py`)

1. **The "no local file found" grace never expired for 6271.** The 15-minute
   gate keys on the ROW's `updated_at`, but `update_queue_item` always stamps
   `updated_at = CURRENT_TIMESTAMP` on every update — a refreshed row never
   trips it, so the item polls forever. (45+ minutes of "will keep polling".)
2. **The basename keys keep leading track numbers.** `0108 - Spring of
   Rage.flac` (remote) vs `08 - Spring of Rage.flac` / `1-08 - ...` (on disk)
   collapse to DIFFERENT punctuation-stripped keys because digits are kept.
3. **The landed-path claim silently skipped the already-claimed/moved case.**
   When the caller's `os.path.isfile(_landed_path)` was False or
   `_is_file_claimed` was True, the block fell through to the 60-min stale
   gate (also defeated by the `updated_at` refresh) → `stats["skipped"]` with
   no log → the item re-logged "appeared within the grace window" forever.

## Changes

* **`_TRACK_NUM_PREFIX_RE` / `_basename_without_track_prefix` /
  `_track_prefix_stripped_key`** — a secondary basename key that strips a
  leading 1-4 digit disc/track prefix (handles `0108`, `08`, `1-08`, `3-02 …`
  multi-segment forms). Used in the `_wait_for_transfer_file` walk AND the
  completed-transfers basename fallback. Exact/punct keys stay primary; the
  caller still verifies the file against the queue item before importing.
* **Consecutive-cycle grace** — `_SUCCEEDED_NOT_FOUND_CYCLES` /
  `_LANDED_NOT_IMPORTED_CYCLES` + `_bump_cycle_counter` /
  `_clear_cycle_counter`. After 3 consecutive observations the item is FAILED
  for retry regardless of `updated_at`. The counter resets when the file is
  found, so a late-landing file still imports.
* **Loud landed-path skip** — the claimed/moved case now logs a WARNING
  naming the state and fails for retry after 3 cycles instead of silently
  re-logging grace.

## Tests — `tests/test_download_completion_stuck_loops.py` (13)

* prefix keys: all disc-track styles collapse, non-numeric leading words and
  mid-title years untouched;
* cycle counters: independent per item, reset on clear, thresholds;
* 6271 class: a succeeded-but-unfound transfer stays in grace for 2
  observations then FAILS on the 3rd (marked + transfer cancelled) even with
  a fresh `updated_at`; a late-landing file returns its path AND resets the
  counter;
* downloads-walk FS tests: the walk finds `08 - Spring of Rage.flac` for a
  queue `found_filename` of `0108 - ...`; the exact remote path still wins
  over a prefix decoy; non-prefixed titles unaffected.

**Oracle**: removing the prefix term from the walk makes
`test_finds_disc_prefix_drift` FAIL; the cycle-counter tests would fail
against the old 15-min/updated_at gate.

**Sweep**: 54 download/queue/slskd/peer/moving suites — 22 failures
**identical at pristine `379fd002`** (pre-existing: caplog-dead in the full
run, torrents hash flake, backslash ×4, wrong-artist ×2, etc.) → 0
regressions. `import app` OK (399 routes).

## Note for the user

* 6271/6704/6705/6707 were service-caused loops — after this fix they
  self-resolve: 6271 fails for retry after ~3 cycles (instead of polling for
  45+ min), and the grace-window items either import or fail loudly.
* The Pet Shop Boys **cover** tracks (Olek Popka, Balayage, Düsseldorf
  Düsterboys, Henrike Werner, Alessandra, Teresa Rotschopf) are genuinely
  scarce on Soulseek — the searches return the ORIGINALS and the artist gate
  correctly rejects them. Those were not a service bug; they'll sit in the
  weekly-miss queue until (if ever) the covers appear.
* No config change needed.