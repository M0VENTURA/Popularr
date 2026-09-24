# Soulseek download/retry wiring: a lost batch contract and two false successes

**Date:** 2026-09-24
**Area:** `routes/download_search_routes.py`, `routes/downloads.py`,
`services/downloads/slskd_service.py`, `api_clients/slskd_http.py`

## Context

An end-to-end audit of the queue pipeline — add to queue → search Soulseek →
select match → download file → match file → update track metadata → copy to
music folder, plus the retry after a failed search or download.

**Most of the chain was verified sound.** Add-to-queue persists the MBIDs the
post-download matcher needs (`add_release_tracks_to_queue_detailed`), the
scheduler ticks the worker (`download_queue_processor`, 30 s), failures return
to the queue rather than parking, the backoff never gives up, and the
completion step tags the file before copying it to the library
(`_apply_stored_metadata` → `move_track_to_library`).

Three defects were found in the manual Soulseek path, plus one in the
upcoming-release path. All four are fixed here.

---

## 1. The batch `files` contract was lost (broke every multi-file download)

`old_system/app.py:21443` had a batch branch:

```python
files_payload = payload.get("files")
if files_payload:
    ...client.download_files(normalized_files)...
```

When the routes were split out into `routes/download_search_routes.py` that
branch was dropped. `slskd_download` read only `username`/`filename` and
rejected everything else:

```python
if not username or not filename:
    return jsonify({"error": "username and filename required"}), 400
```

Two frontends still send the batch shape:

| Caller | Body |
|---|---|
| `static/js/downloads_page.js:433` (`downloadSlskdBatch`) | `{ files }` |
| `test_site/static/js/services/slskd.js:529` (`download`) | `{ files: files }` |

`downloads_page.js` drives **Download selected** and whole-album download, so
every one of those 400'd. `services/slskd.js` is loaded by six rebuilt pages
(`base.html`, `artist_detail.html`, `downloads/queue.html`,
`downloads/search.html`, `downloads/monitor.html`, `_release_section.html`),
so single downloads on the rebuilt UI broke too.

**Fix:** `slskd_download` accepts both shapes and funnels them through a new
`SlskdService.download_files`, which batches by peer username. A single
download is a batch of one.

## 2. A rejected download reported success

```python
result = await asyncio.to_thread(slskd.download_file, username, filename, size=size)

if result is None:                     # <-- unreachable
    result = await asyncio.to_thread(client.enqueue_download, ...)

return jsonify({"success": True, "result": result})
```

`download_file` returns `bool` — never `None` — so the fallback guard could not
be satisfied and the route returned `{"success": True, "result": False}`. The
UI branches on `data.success`, so a dead download produced a "Download
enqueued" toast. This is the same defect shape fixed in `df72718e`
(*a hard-coded `success: True` where the payload count is items ATTEMPTED but
the UI reads it as items COMPLETED*).

`slskd_queue_download` had the same problem in a worse form: it wrote
`status='downloading'` to the queue row **unconditionally**, so a rejected
request left the row looking active forever while the completion matcher waited
for a transfer that was never requested.

**Fix:**

* `download_files` returns `requested` (files *accepted* by slskd, explicitly
  not files that will complete) plus a per-peer `success` flag.
* `slskd_download` reports `success` only when `requested > 0`, returning HTTP
  500 with an explicit `success: false` otherwise.
* `slskd_queue_download` returns HTTP 502 **without touching the queue row**
  when slskd accepts nothing.
* `SlskdHttpClient.enqueue_downloads` gained `raise_on_error=False`. This is
  load-bearing: an empty transfer-id list means both "accepted, older slskd
  returned no body" and "the request failed", so the service asks for an
  exception to distinguish them. The default keeps existing best-effort callers
  unchanged.

## 3. Queueing an upcoming release reported success with zero tracks

`start_release_download` returns `success: True` once the MusicBrainz fetch
succeeds — including when every track was already in the library or queue.
`routes/downloads.py` only checked that flag, so it answered
`success: True, queued_tracks: 0` and flipped `upcoming_releases.status` to
`'queued'`. The dashboard then showed a release that looked queued with nothing
downloading behind it.

The service already exposes `queued`, `queue_reason` and `queue_message` —
the route simply ignored them.

**Fix:** the route inspects `queue_items_created`, and at zero returns HTTP 409
with the service's human-readable sentence in `error` (the key the UI toasts)
and the machine code in `reason`. The release row is not marked.

## 4. `/api/slskd/retry` passed a filename where slskd needs a transfer id

```python
result = await asyncio.to_thread(client.retry_download, username, filename)
```

but the signature is `retry_download(self, username, transfer_id, ...)` — slskd
scopes a transfer by username **plus** the server-assigned transfer id
(`POST /transfers/downloads/{username}/{transfer_id}/retry`). A filename could
never match.

**Fix:** requires `transfer_id`, rejects a bare `filename` with a 400 that
explains why, and reports a rejected retry as a 502 rather than success.

> No frontend calls `/api/slskd/retry` (verified by grep), so this was latent.
> The working manual retry is `/api/queue/<id>/requeue` →
> `requeue_queue_item`, which correctly clears `next_retry_at` and
> `retry_count`. It was left alone.

---

## Retry semantics — verified, NOT changed

The audit initially suspected the failed-download retry surfaces were dead
because `mark_failed` never writes the terminal `'failed'` status. **That
suspicion was disproved.** `'failed'` is reachable by design:

* `cleanup_stuck_items` parks items stuck in `downloading` for **6 h** as
  `'failed'` with reason `"Stuck in downloading state"`.
* `queue_cancel` writes `status="failed", failure_reason="Cancelled by user"`.

so the Failed-Downloads card, "Retry all failed" and
`requeue_due_failed_items` all have rows to act on. Two distinct failure
classes exist and both return to the queue:

| Class | Written by | Result |
|---|---|---|
| search miss | `_schedule_search_retry` | `backed_off` + escalating window (4/12/24 h, capped) |
| download failure / exception | `mark_failed` | `queued`, or preserves an existing `backed_off` / `pending_release` via `GREATEST` |

No production change was made here.

## Known-but-deliberately-unfixed

* **`max_retries` is dead.** `_queue_retry_defaults()` reads it but
  `mark_failed` uses only the delay. Retries are effectively unbounded. This is
  consistent with the product rule that a track must never be abandoned, so it
  was left as-is rather than "fixed" — but the column is misleading.
* **`process_pending_completed_items` has no callers.** The automatic path is
  `download_completion_service.check_completed_downloads` (a maintenance hook);
  only `/api/downloads/process-one` and `/process-albums` reach
  `process_completed_queue_item`.
* **`services/downloads/download_queue_service.py:26` imports a module that
  does not exist** (`download_queue_normalizer`), so the module is unimportable.
  It has no importers today, so it is latent.
* `slskd_username` is written only by the manual route; nothing reads it.
* `services/queue/queue_orchestrator.py` names two nonexistent modules in its
  candidate lists, but resolves them through `try/except`, so the fallbacks are
  harmless.

## Tests

| File | Tests | Covers |
|---|---|---|
| `tests/test_slskd_download_contract.py` | 24 | both payload shapes, per-peer batching, rejected downloads, queue-row linking, transfer-id retry, `download_files`, and guards pinning what each frontend still posts |
| `tests/test_upcoming_queue_zero_track_success.py` | 9 | zero-track 409, the release row stays unmarked, the reason reaches the user, the happy path still queues |
| `tests/test_queue_retry_chain_wiring.py` | 27 | failures return to the queue, pending rows are re-picked, parked rows are recovered, backoff never abandons, the worker registers and runs the retry hooks |

All three pass. The routes are driven for real (only the HTTP client is
faked), so the contract is exercised rather than mocked away.

**Mutation-verified:** 11 mutations, 11 caught — including reverting the batch
branch, hard-coding `overall_success = True`, passing a filename as a transfer
id, dropping `raise_on_error`, marking a queue row despite a failed enqueue,
disabling the zero-track guard, parking `mark_failed` as `'failed'`, breaking
the `requeue_due_failed_items` query, keeping a stale backoff window on manual
requeue, unregistering the retry hook, and removing the backoff-tier clamp.

**Oracle sweep:** the full suite was run with and without these changes and the
failing sets compared — **0 new failures**.

## Note on the `files` contract

The legacy `old_system/` implementation was used as the reference for the batch
response shape (`success` = any peer accepted), but the new tree follows its own
conventions: Postgres-only SQL, `structlog`, and a `requested` count that is
explicitly *accepted*, not *completed*, so the UI cannot repeat the
attempted-vs-completed confusion.
