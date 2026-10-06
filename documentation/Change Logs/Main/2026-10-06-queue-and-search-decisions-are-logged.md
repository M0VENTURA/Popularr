# Queue and search decisions actually reach their log files

**Date:** 2026-10-06 · **Area:** queue / search / logging
**Commit:** `fix(queue): the match that succeeded is logged, and the queue processor writes to queue.log`

## Reported

> Check through the queue and search and make sure all parts of it are logged
> including the matching of songs. They should be going to the search.log or
> the queue.log.

## How routing actually works

`helpers/logging_config.py::dictConfig`:

| file | fed by |
|---|---|
| `queue.log` | `services.queue`, `services.downloads`, `db.repositories.queue`, `db.repositories.queue_admin` (INFO+, `propagate: False`) **plus** explicit `log_queue()` → `popularr.queue` |
| `search.log` | **only** `log_search()` → `popularr.search` — two call sites in the entire codebase |

A statement reaches the right file only if its **module logger** is routed or
it calls the helper. Three things failed that test:

### 1. A successful match was never logged

`_select_best_result` logged `Best result found` at **DEBUG** in a module whose
level is INFO — so the line was discarded. The *failure* path was visible (the
`_note_reject` WARNING carrying `rejected={…}` per gate, `top_score`,
`top_candidate`, `candidates`), but **what the pipeline chose when it
succeeded** never appeared. That is "the matching of songs" being absent.

Now `Selected download candidate` at **INFO**, with `artist`, `title`, `album`,
`score`, `filename` and `candidates` — a bare filename cannot be attributed to
a track when you are reading a log tail.

The per-candidate rejection lines stay DEBUG **on purpose**: they fire
hundreds of times per search, and their counts already arrive in the WARNING.

### 2. The queue processor's failures never reached queue.log

`services/scheduler/scheduler_service.py` is **not** in the routing table, so
`download_queue_processor cycle failed` / `spawn failed` landed only in
`unified` — you had to grep two different files to answer "did the queue even
run?". Both now also call `log_queue()`.

### 3. No test pinned any of it

`tests/test_queue_search_logging.py` (6 tests) now guards the routing itself:
the winner is INFO *and* names the track, the old DEBUG line is gone, the
gate-summary WARNING and its four `_note_reject` calls survive (the
`below_floor` gate counts directly rather than through the helper), the
queue-processor failures reach `log_queue`, and both search feeders plus the
`popularr.search` route still exist.

**Oracle:** stashing the two source files → **3 failed, 3 controls passed**;
with them, 6/6.

## What search.log still does not contain

The two summaries carry query → result count → duration → outcome (the lines
quoted in the report). Not yet logged there: each *attempted fallback query*
and its individual result count. That is the next change, paired with using
the **album name** in queries for single-word titles (see below).

## Verification

- new suite: **6 passed**
- **Oracle:** stashing the two source files → **3 failed, 3 controls passed**;
  with them, 6/6
- affected set (41 files): 574 passed / 27 failed → only the known native-flaky
  `test_sibling_torrents_root_is_searched` is new
- full suite: **225 failed, 4687 passed, 2 skipped** vs the 303-failure
  baseline → **79 fixed, 0 real regressions** (the only ID absent from the
  baseline is that same known flaky test)

## Files

- `services/downloads/download_pipeline_service.py`
- `services/scheduler/scheduler_service.py`
- `tests/test_queue_search_logging.py` (new)
