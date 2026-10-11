# Queue log: one min_score warning per search run; a slot-less peer is skipped, not fatal

Date: 2026-10-11
Branch: develop

## Reported

The queue log "seems to be showing issues" (search itself is fine). Read
against the 2026-10-10 `queue.log` (2.6MB in one day), three defects — all
pre-existing, none introduced this week:

## ① Per-query WARNING spam buried the log

A search run fans out over fallback queries and bitrate tiers — the log
shows `queries=18`…`21` per item — and `_select_best_result` emitted
**"No result met min_score" at WARNING for EVERY fruitless query**. One
doomed item produced ~20 identical lines every ~15s while holding the queue
cycle lock: that was most of queue.log's volume, and the repetition made the
one useful signal (WHY nothing matched) indistinguishable from noise.

Fix: `_select_best_result` gained a `miss_sink`; the run collects each
query's miss and **`_warn_closest_search_miss` warns ONCE** with the closest
attempt (most candidates, then highest top score) plus `queries_tried`. A
run where every query returned zero candidates emits no line at all — the
`no_results` search event already says it. Per-query misses remain visible
at DEBUG. Callers that pass no sink get the old WARNING unchanged.

## ② A slot-less BEST candidate failed the whole item (real bug)

The Netherwilds "Peasant Pit" case in the log:

```
Selected download candidate artist='Netherwilds' title='Peasant Pit' score=77.9 … candidates=2
Best match peer has 0 free slots — skipping download username='ccrackkk'
[FAILED] Netherwilds - Peasant Pit → failed: peer has no free upload slots
```

…while a **second qualifying candidate sat untried**. A retry ~25 minutes
later succeeded from the other peer (`danishKing`). The candidate loop
directly below already knew how to skip slot-less peers and try the next
one — the early `return` for the BEST candidate's slots simply never let
that loop run.

Fix: the early failure is gone; the loop is extracted as
`_attempt_candidate_downloads` (returns `(success, chosen, fail_reason)`)
and is now the one path. Behaviour matrix:
* best slot-less + open second candidate → **downloads the second**;
* all candidates slot-less → fails with `peer_no_free_slots` (the exact
  reason `mark_failed` and the retry chain already expect — unchanged);
* a failed download still falls through and its peer+file is remembered;
* item removed mid-attempt → `item_removed`, nothing downloaded.
Also fixed en route: `fail_reason` now names the LAST failure mode (the
old loop logged a stale earlier reason after a later failure), and the
search event carries `fail_reason` instead of a hardcoded
`"download_failed"` that hid `peer_no_free_slots` from search.log.

## ③ Dual-driver lock-skip lines at INFO

"Another queue cycle holds the lock — skipping cycle" fired ~4×/minute
forever — the standalone `queue_worker` and the APScheduler thread both
driving cycles is the DELIBERATE design (the cycle lock is what keeps their
maintenance from running twice), so the line is expected contention, not an
event. ~5,700 identical INFO lines/day. Both sites (cycle + batch) are now
DEBUG.

## NOT changed (examined, judged sound)

* The mismatch peer block ("Blocked peer after mismatched download" for the
  BIGBANG files whose filenames matched exactly): `_block_peer` is scoped to
  **(peer, filename) with a 2h in-memory TTL** — it stops the futile
  retry-same-file loop without banning the peer's other files. Sound as-is.
* The per-item search duration (~5 min: one 150s first-query wait + ~20×20s
  fallback waits) is the configured design; only the logging was wrong.

## Tests

* New `tests/test_queue_search_miss_and_slot_fallback.py` (16): miss-sink
  semantics (one warning per run, closest miss wins, zero-candidate runs
  silent, sink-less back-compat control); the slot-fallback matrix above
  with a fake slskd (the Netherwilds case pinned literally); wiring probes
  (comment-stripped) for the sink threading, the removed early-fail, the
  real fail reason, and the DEBUG lock skips; controls (a qualifying best
  still selected and logged; blocked peers still filtered).
* **Oracle** at `95f92a2c`: **13 failed / 3 passed** (3 = either-way
  controls).
* **Sweep** 24 queue/slskd/pipeline suites: base `18 failed / 307 passed`
  vs new `18 failed / 307 passed`, failing sets **identical** → **0
  regressions** (the pinned `mark_failed(…, "peer_no_free_slots")` retry
  contracts and the `No result met min_score` log literal both survive).

## What the log will look like after the rebuild

Per doomed item: ONE enriched "No result met min_score … queries_tried=N"
warning instead of ~20; no lock-skip lines; and the slot-less case reads
"Best match peer has no free slots — trying next candidate" followed by an
actual download when the next candidate exists.
