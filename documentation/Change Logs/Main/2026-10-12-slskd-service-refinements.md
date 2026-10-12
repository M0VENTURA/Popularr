# 2026-10-12 — slskd service refinements (review addendum)

## Context

A follow-up to `2026-10-12-slskd-service-hardening.md` (commit `d08415d5`).
These are the six additional recommendations from the review addendum, on top
of the P0/P1 hardening. Each was checked against the CURRENT code first; two
were already correct from the hardening pass and one hid a real defect.

## Changes

### 1. A dropped search (404) counts as COMPLETE (`get_search_results`)
slskd can drop a search mid-poll (404). Both `except` arms now return
`is_complete = True` (state `"Deleted"`) via `_is_not_found_error`, so
`search_and_filter` stops polling a dead search until the hard timeout and
drains what it already has. The helper matches a 404 on either
`exc.status_code` or `exc.response.status_code` (the httpx shape).

### 2. Path-aware relevance ranking (`_rank_qualified`) — real defect fixed
The addendum asked for basename-weighted ranking. The hardening pass had
introduced it, but the folder half was WRONG:
`folder_tokens = token_set - basename_tokens` then counting "those in
token_set" always counted every non-basename query token — so the folder term
was a constant and genuine directory matches earned **no** credit.
Now the basename is tokenised with `os.path.basename` and the directory with
`os.path.dirname`, and a basename match weighs **3x** a folder match
(`relevance = basename_hits * 3 + folder_hits`). A file NAMED "Song" always
outranks a folder called "Song" holding a different track, while a real folder
match (e.g. `Arctic Monkeys - AM/01 Whatever.flac`) still beats a file that
matches nowhere.

### 3. Case-insensitive dedupe (accumulate key)
The `seen` key is `(username, item["filename"].casefold())` — Soulseek is
dominated by Windows peers, so the same remote file differing only in case is
no longer evaluated (and queued) twice. `casefold()` (not `lower()`) is the
stronger fold.

### 4. Upload-slot prioritisation above bitrate
`_rank_qualified` puts `has_free_upload_slot` BEFORE bitrate/sample rate: an
open-slot 320k MP3 downloads instantly, a perfect FLAC behind a 4000-deep
queue sits in `Queued, Remotely`. An unknown slot (`None`) is NOT treated as
full — a missing value is not evidence the peer is busy.

### 5. Higher 429 backoff ceiling (`_retry_after_or_backoff`)
The fallback backoff (no usable `Retry-After`) is now `min(10.0, 1.0 * attempt)`
— a hard Soulseek rate-limit window is rarely cleared by a 2s wait. A
valid `Retry-After` (seconds **or** HTTP-date) still wins. Status-code based,
never body-text based.

Note: the `min(2.0, 0.4 * attempt)` the addendum quoted is the **transient
error** (timeout/connect) backoff in `start_search`, not the 429 path; it is
left as-is deliberately (a connect blip clears fast, and the idempotency
recovery via `_search_exists` runs first).

### 6. Null-safe transfer progress (`parse_transfers_response`)
slskd frequently returns `None`/non-numeric `size`/`bytesTransferred` in the
first moments of a transfer. Both now go through `_to_int` (→ 0); progress is
computed only when `size` is truthy (no `ZeroDivisionError`) and otherwise
falls back to `percentComplete`, clamped to `0..100`.

## Tests

`tests/test_slskd_service_hardening.py` +12:
- 404 completion ×3 (state half, responses half, and the httpx
  `exc.response.status_code` shape)
- slot-above-bitrate ×2 (open low-bitrate beats full high-bitrate; unknown slot
  not full)
- casefold dedupe ×1 (same file, two cases → one result)
- 429 ceiling ×2 (reaches 10s; `Retry-After` header still wins)
- null progress ×2 (None/garbage sizes; `percentComplete` fallback)
- ranking ×2 (a folder-only match still beats no match — the discriminator
  for the folder-tokenisation fix; basename match beats a folder-only match)

**Oracle:** reverting just the `folder_tokens` line to the old
`token_set - basename_tokens` makes
`test_a_folder_match_still_beats_no_match_at_all` FAIL (expects the folder
match first, gets the no-match file) while the pre-existing basename test still
passes; restored → 40 passed.

**Sweep** (11 slskd/queue-search suites): 6 failures, IDENTICAL to a pristine
HEAD checkout — the 4 `test_slskd_backslash_sanitisation` Windows path-separator
cases and the 2 documented `test_slskd_wrong_artist_rejection` artist-gate env
failures → **0 regressions**.

## Notes

- The addendum's Rule 1 (live-instance API contract, `docs/slskd_api_contract.md`)
  is still open: no slskd instance is reachable from this workspace, so the
  fixtures remain synthetic shapes of the documented API.
