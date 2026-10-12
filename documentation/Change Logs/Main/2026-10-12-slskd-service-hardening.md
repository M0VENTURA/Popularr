# 2026-10-12 — slskd service hardening (review P0/P1: quality, idempotency, ownership)

## Report

An agent brief (`slskd_service_review_brief.md`) audited
`services/downloads/slskd_service.py`. Every finding was verified against the
CURRENT code (much had changed since the brief). The verified findings:

**P0 (correctness):**
- **2.1** missing `sampleRate`/`bitRate` rejected files — a 320k MP3 with no
  sample rate was silently dropped, hiding rare tracks (the `no_qualifying_result`
  day-after-day failures).
- **2.2/2.3** `start_search` (service) POSTed WITHOUT a client id and retried on
  ReadTimeout → possible duplicate searches; recovery matched by QUERY TEXT
  (`find_active_search_by_query`) → one worker could adopt + then delete another
  worker's search.
- **2.4** `clear_stale_searches` deleted EVERY terminal search + any active
  search >180s — including searches the user started in the slskd web UI.
- **2.5** the (new) empty-search early abort counted QUALIFIED results — a
  search streaming many files that ALL fail the quality gate was cancelled as
  "empty".
- **2.6** the final `_drain()` only ran when `accumulated` was empty → late
  arrivals lost on a deadline exit.
- **2.7** `filter_results_by_quality(max_results=50)` ran per poll → a correct
  match ranked below 50 in any single snapshot was never seen.
- **2.9** empty `transfer_ids` + no raise counted as success; `except TypeError`
  could double-enqueue; `download_file` bypassed `enqueue_downloads`.
- **2.10** `download_files` `.strip()`-ed `filename`/`username` — Soulseek
  matches remote paths exactly.
- **2.11** `extract_queue_position` or-chain dropped position 0; `queueLength`
  (peer total) was used as a fallback.

**P1:** state-set duplication; `cancel_search` treating 409/500-"concurrency"
as success; `.strip()` before enqueue; `int()` on non-numeric payloads dropping
whole responses; missing `extension` → lossless misclassified; `m4a` wrongly
never relevant but `ape`/`wv` missing; 429 by body-text not status; no server
state gate; transfer parser not deduping nested+top-level and using `path` as
local; `time.monotonic` / `poll_interval` / `timeout: Optional[int]` vs floats.

**Not changed:** 2.8 (errors-vs-empty: no caller currently re-queues on empty,
latent only — documented), 11 (dormant — `clear_completed_downloads` has no
live caller), 14 (cleanup cadence reset-to-1 is comment-documented + pinned by
a test — a monotonic counter would be simpler but isn't a bug). `docs/` API
contract needing the live instance (Rule 1: cannot verify from this workspace).

## Changes

### Quality gate (2.1/7)
`SearchFile.matches_quality` treats a missing attribute as UNKNOWN, not a
failure: lossless with no `sampleRate` passes; lossy with no `bitRate` passes;
lossy with no `sampleRate` passes. A NORNAL known value below the floor still
rejects, and rejects are counted by reason (`reject_reasons` dict) — surfaced
as one DEBUG `Search filtered files by reason` line at the end of
`search_and_filter`. `LOSSLESS_EXTENSIONS` adds `ape`/`wv`; `m4a` deliberately
excluded. `extension` falls back to the filename ext. `_to_int` replaces bare
`int()`.

### Idempotency + ownership (2.2/2.3/2.4/P1-3)
- `start_search` generates a client UUID, POSTs it as `id`, and recovery is BY
  THAT ID: after a transient failure it checks `GET /searches/{id}` before
  retrying (`_search_exists`). Query-text recovery
  (`find_active_search_by_query`) is REMOVED — it could adopt + delete another
  worker's search.
- Class-level `_OWNED_SEARCH_IDS` registry (`mark_search_owned` /
  `unmark_search_owned`). `clear_stale_searches` now only cancels OUR
  terminal/stuck searches, plus NON-owned searches older than
  `ORPHANED_SEARCH_TIMEOUT_MS` (30 min) — a terminal search the user started in
  the slskd web UI is left alone, as is a fresh foreign search.
- `cancel_search` = explicit `_stop_search` (PUT, 200/204/304) + `_delete_search`
  (DELETE, **404 = success** — the search is already gone). No more bare
  undocumented `PUT`. `is_terminal_search_state` parses flag-style strings
  (`Completed, ResponseLimitReached` etc.) instead of exact-match lists;
  `is_search_active_state` keeps the old "literal InProgress" semantics so an
  unknown state can never poll forever. Merged the duplicated search-state sets
  into `SEARCH_ACTIVE_STATES`/`SEARCH_TERMINAL_STATES`/`SEARCH_TERMINAL_FLAGS`.

### Polling (2.5/2.6/2.7/12/13/P1-1)
`search_and_filter`:
- polls with `max_results=None` (uncapped accumulation) and applies the 50-cap
  ONCE over the whole set (`_rank_qualified`).
- the early-abort now uses RAW file count (`_raw_file_count`) — files that all
  fail quality no longer read as "empty".
- ALWAYS one final `_drain()` (deduped via `seen`), so a deadline exit cannot
  throw away late arrivals.
- `time.monotonic()`; `poll_interval` honoured and the sleep never passes the
  deadline (a wait shorter than the first delay still polls once).
- `timeout: float | None` throughout.

### Ranking + peer availability (P1-4)
`_rank_qualified`: query relevance first (basename hits > folder hits,
NFKD-strip for `Ça ira`/`Mötley`, apostrophes removed so `don't` keys as
`dont`), then bitrate/quality, then peer availability (slot before queueLength
before uploadSpeed); a MISSING slot is not treated as full (unknown ≠ busy).

### Server-state gate (9)
`SlskdService.is_connected()` (from `SlskdHttpClient.is_connected` →
`GET application/state`). `search_and_filter` returns **`None`** when
disconnected (distinct from `[]`): the pipeline now maps `None` to a
`slskd_disconnected` outcome — the item returns to the queue WITHOUT a retry
backoff (no false `no_results` ladder). The manual `/api/slskd/search` route
returns 503 + `slskd_disconnected: True` instead of a misleading "slot busy".

### Enqueue funnel (2.9/2.10)
`download_files` and `download_file` now both go through one `_peer_enqueue`
(prefers `enqueue_downloads(..., raise_on_error=True)`; transient errors
PROPAGATE so `download_file`'s retry loop still works). `download_file` REFUSES
a `size=0` request (slskd needs the real size). Remote `filename`/`username`
are passed through UNCHANGED (only empty/None skipped).

### Position + transfer parsing (2.11/10)
`extract_queue_position` uses `is not None` in order and drops `queueLength`.
`parse_transfers_response` dedupes on `(username, id, filename.casefold())` and
no longer uses the remote `path` key as `localFilePath`.

## Tests

- `tests/test_slskd_service_hardening.py` (NEW, 28): quality gate ×7,
  extension/defensive parsing ×6, cap-once ×2, polling (disconnected→None,
  raw-abort, final drain) ×3, download funnel ×3, position ×3, transfer parsing
  ×2, tokeniser/ranking ×2.
- `tests/test_slskd_timeout_resilience.py` (updated): new UUID-idempotency
  contract (`start_search` returns the client-generated id, recovery by id,
  `find_active_search_by_query` absent), `size=0` refused, ownership-based
  `clear_stale_searches` (owned terminal/stuck cleaned, fresh owned kept,
  foreign terminal left, orphaned active cancelled).
- `tests/test_slskd_cleanup_cadence.py` (updated): registered owned ids in
  fixtures (ownership-aware cleanup).

**Oracle** (4 source files stashed, tests kept): `test_slskd_service_hardening`
failed fully (new symbols absent — collection errors), the updated timeout/
cadence suites failed their discriminators; restored → all green.

**Sweep** (48 queue/download/slskd suites, one process per file): all failure
sets IDENTICAL to a stashed baseline except the known nondeterministic Windows
flake `test_sibling_torrents_root_is_searched` (passes 1 of 3 runs on both
trees). `import app` → 399 routes. `git diff --check` clean.

## Notes

- `SlskdHttpClient.delete_search` now treats 404 as success (concurrent delete
  from another worker can legitimately win).
- The brief's Rule 1 (verify against live slskd instance + fixtures, write
  `docs/slskd_api_contract.md`) cannot be done from this workspace — no live
  instance. The fixtures here are synthetic shapes matching the documented API;
  a follow-up against the real instance would confirm the option names
  (`fileLimit`/`responseLimit`/`searchTimeout`/…) and add real-recorded
  fixtures.