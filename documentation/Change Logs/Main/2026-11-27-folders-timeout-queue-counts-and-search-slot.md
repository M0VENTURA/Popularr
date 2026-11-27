# Folders timeout, queue count mismatch, and the Soulseek search slot

**Date:** 2026-11-27
**Commit:** `fix(downloads): bound the folders scan, align queue counts, yield the search slot`

Three separate reports in one message. Two were bugs; the third was a
regression I introduced earlier today.

## 1. "Could not load folders: Request timed out after 30s"

Both halves of this endpoint were slow, but only one had ever been bounded.

**The tag reads were already capped at 6 s.** **The directory walk was not.**
`_iter_matched_folder_candidates` recursed three levels with an
`os.path.isfile()` probe per directory entry, and then `_get_files_in_folder`
ran a full `os.walk` with one `stat()` per file **for every candidate folder**.
On a network-mounted `/downloads` (the normal Docker deployment) a few thousand
of those is 20–60 s on its own — the endpoint could not answer inside 30 s no
matter how cheap the tags were.

Worse, the walk probed `isfile()` **before** checking the extension, so a
folder's cover art cost as many syscalls as its music.

Fixes:

- `_FOLDERS_SCAN_BUDGET_SECONDS = 20` — a wall-time deadline shared by the walk
  and the per-folder listing. Past it the scan stops adding candidates and
  returns what it has.
- `_MAX_FOLDER_CANDIDATES = 400` — a hard ceiling so even a fast local disk
  cannot walk a pathological tree.
- Extension checked **before** `stat()` in `_has_direct_audio`,
  `_has_disc_audio` and `_folder_holds_audio`.
- The duplicate `get_active_releases_with_progress()` call is gone (it ran the
  same aggregate twice per request).
- `truncated: true` in the response, rendered as **"this list is PARTIAL"** —
  distinct from the existing `tags_truncated`, which means "names came from
  filenames". Only the second was reported before, so a partial list read as
  "these are all your folders".
- The route is now `async def` + `asyncio.to_thread`, like every other heavy
  route in `downloads.py`.
- Client: `UNMATCHED_TIMEOUT_MS = 35000`, and `UNMATCHED_REFRESH_MS` raised from
  30 000 to 60 000.

⚠️ **The poll interval equalled the request timeout.** Every scan was abandoned
at 30 s and the next began immediately, so the endpoint was under continuous
load while the page was open. That alone could keep it permanently red.

## 2. Processor card "64 queued" vs pills "145 Queued"

Neither number was wrong — they answered different questions under the same
label.

Both read the **same** query, `SELECT status, COUNT(*) FROM download_queue
GROUP BY status`:

| | Card (was) | Pill |
|---|---|---|
| Key | `histogram['queued']` | `ACTIVE_SECTION − histogram['downloading']` |
| Predicate | `status = 'queued'` | nine active statuses minus `downloading` |
| Value | 64 | 145 |

The pill's 145 was the one a user could reconcile against the list beneath it,
and its label is "Queued" — so the card now reports the same figure. The narrow
count survives as `queued_raw` and appears in the card's hover text.

Two related inconsistencies fixed in the same function:

- `failed` used `FAILED_STATUSES` (which includes `removed`/`cancelled`/
  `deleted` tombstones) while the pill uses `FAILED_SECTION` (`failed` only).
  They read equal today only because no tombstones exist.
- `ready` used `COMPLETED_QUEUE_STATUSES`; the pill uses `READY_SECTION`. Same
  set today, different definition — now literally the same constant.

Also: the banner said **"scheduler: scheduler not initialised in this process"**.
That is normal — only the leader worker builds a scheduler, the queue is driven
by the standalone worker, and the endpoint is load-balanced, so ~3 in 4 polls
reported it. It now reads "processor driven by the standalone worker" whenever
the heartbeat is fresh.

## 3. "Search slot did not free up. Try again shortly."

This one is mine, and the report was exact: **"It used to wait until it freed
up."**

Two independent faults:

**a. The client gave up too early.** `SLOT_MAX_RETRIES = 30 × 2 s = 60 s`, while
a **single** queue search can run 145 s. It was timing out by arithmetic, not
because the slot was stuck. Raised to 90 × 2 s = 3 minutes.

**b. Nothing told the worker to stand aside.** slskd serves one search at a
time, and the queue worker holds it almost continuously — one search per track,
35–145 s each, next cycle immediately when more rows are ready. An interactive
search had to win a race it could not win.

New `services/downloads/manual_search_priority.py` — a cross-process claim file
in the runtime-state directory (the same pattern as `search_service_state.json`,
because the queue runs in a separate process). The search route claims it; the
worker checks it before every search and sleeps the remaining window.

Bounded in both directions: the claim **expires** after 90 s, and the worker
caps its wait, so a closed tab or a crashed browser can never park the queue.
The claim is released as soon as the search completes, or when it fails to
start.

**Order of operations** — the claim is written before `start_search`, so the
worker yields before it next competes for the slot.

## Tests

`tests/test_folders_timeout_queue_counts_and_search_priority.py` — 23 cases:
the scan budget exists and reaches the walk, a candidate cap applies,
truncation is reported, extensions are checked before `stat`, the route is
offloaded, the interval exceeds the timeout, the duplicate query is gone; the
card's queued/ready/failed all equal the pills' for a realistic histogram;
the priority claim expires, releases, and is honoured by both the route and the
worker, and the client's total wait exceeds one queue search.
