# Manual Soulseek search from a queue row: flat queue-download payload (2026-10-13)

## Report

Clicking "Download" on a result in a manual Soulseek search opened **from an
active queue row** (the download page) failed with:

```
Error: HTTP 400: queue_id, username, and filename required
```

## Root cause — the rebuilt frontend posted the batch shape to a flat endpoint

Two different contract shapes for "download a file":

* `/api/slskd/download` — the PLAIN endpoint accepts the **batch** shape
  `{"files": [{username, filename, size}]}` (and a legacy flat single).
* `/api/slskd/queue-download` — the QUEUE-LINKED endpoint reads **FLAT**
  `{username, filename, size, queue_id}`; it has never understood `files`.

`test_site/static/js/services/slskd.js::download()` always posted the nested
shape, and when a `queueId` was present it sent
`{ files: [...], queue_id }` to `/api/slskd/queue-download` — so the route
saw empty `username`/`filename` and returned the 400. The LIVE tree
(`static/js/downloads.js::downloadSoulseekManualResult`) already builds the
flat body for that endpoint, so the live page worked and only the rebuilt
(test_site) manual-search modal broke.

The rebuilt modal forwards `queueId` via the per-file Download button
(`renderTable` → `download([...], { queueId })`), so every manual-search
download opened from a queue row hit the 400.

## Fix (`test_site/static/js/services/slskd.js::download`)

When `opts.queueId` is set, build the **flat** body —
`{username, filename, size, queue_id}` from `files[0]` — mirroring the
live `downloads.js` (a queue row is a single file). The plain
`/api/slskd/download` path keeps `{ files }` (batch). A >1-file batch with
a queueId (never happens in practice — each row is one file) falls back to
the batch endpoint with a console warning so no file is dropped.

## Tests — `test_slskd_download_contract.py` (24 → 25)

New guard `test_rebuilt_slskd_service_posts_flat_shape_for_queue_download`:
the queue-linked branch must send `username`/`filename`/`size` + `queue_id`
(never the nested `files` list). The existing batch-shape pin
(`{ files: files }`) still passes — the plain endpoint contract is kept.

**Oracle**: reverting the queue-download branch to `{ files, queue_id }`
makes the new guard FAIL while the 3 existing pins stay green.

**Sweep** — 54 download/queue/slskd/peer/moving suites: 22 failures
**identical to the pristine baseline** (pre-existing: caplog-dead in the
full run, torrents hash flake, backslash ×4, wrong-artist ×2, etc.) → 0
regressions. `node --check` clean; the slskd-related suites (incl. the
manual-search contract + button suites) all pass (127).

## Note for the user

test_site fix — the live tree already sent the flat shape. No rebuild of the
backend needed; the JS ships with the next build. Opening a manual Soulseek
search from a queue row and clicking Download will now link the file to that
row again.