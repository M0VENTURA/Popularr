# Large album saves tag their files in the background

**Date:** 2026-10-15
**Files:** `services/metadata/album_file_sync_service.py` (new),
`routes/ui_routes.py`, `routes/maintenance_routes.py`,
`helpers/config_helpers.py`, both Config pages + their JS

Requested:

> When saving an album that has a lot of tracks, it times out due to cloudflare
> time out, can this be added to a background service so the app can continue and
> not wait for the save to complete

## Why it timed out

Cloudflare gives the origin **100 seconds** and then returns **524**. An album
save is a synchronous form POST, so the browser waits for the redirect and the
whole save has to finish inside that window.

Earlier rounds on this problem already took the save apart — the MusicBrainz
backfill is gated on the release actually changing, bounded by a deadline, and
runs off the event loop; the per-track file writes were collected into one
concurrent phase. What remained is simply **volume**: that phase touches every
audio file of the album, and a large album costs more wall clock than the window
allows however well it is parallelised.

## The split: the database half stays, the disk half moves

The save has two halves with very different costs:

| half | cost | kept in the request? |
|---|---|---|
| album fields + every track's row | fast (local SQL) | **yes** — it is what makes the edit *visible* |
| rewrite each audio file's tags, embed the cover art, ask Navidrome to re-scan | N file passes | **no** — backgrounded past a threshold |

So a backgrounded save is still a **durable** save: the edit is committed before
the redirect, and a later failure costs the file tags, not the edit. That is the
deliberate difference from backgrounding the whole save, which would leave the
user unable to tell a slow save from a lost one.

Past `album_save.background.track_threshold` (default **40**) files, the disk
phase is handed to a background service and the response returns immediately.
Below it the save is exactly what it always was — small albums finish well inside
the window, and keeping them synchronous means the response still confirms the
file tags.

### ⚠️ The re-scan moved with it, and that is not optional

`request_rescan` exists because Navidrome serves its own database and only
refreshes when it re-scans. It used to run at the end of the handler, after the
file writes — for the good reason recorded in its own comment: without it the next
Popularr import reads Navidrome's pre-save rows and the upsert writes the old
metadata back over the edit.

For a **backgrounded** save, that point in the handler is reached *before* the
files have been written. Leaving the rescan there would have scanned the old tags
and reintroduced exactly the bug `navidrome_rescan_service` was written to
prevent. The rescan is therefore the disk phase's **last step**, and a
parametrised test asserts it runs after both the tag writes and the cover embed.

The *decision* whether to rescan still comes from the request — `should_rescan`
is computed from the database half's counters (`updated_count`,
`reverted_live_count`, whether art was fetched), which the background thread
cannot see.

### What else moved, and what did not

- **The cover art split in two.** The download and the `album_art` row stay in
  the request: one HTTP fetch plus one small insert is nothing next to N file
  writes, and the row is what makes the new art **visible on the page**. Only the
  *embed* (a pass over every file) moves. A test asserts the store is neither
  ordered after the branch **nor gated on it** — a store guarded by
  `and not _file_backgrounded` still sits before the branch textually while doing
  nothing for exactly the saves that need it.
- **One implementation of the disk phase.** `_run_album_file_sync_phases` is
  sync and reached two ways: `asyncio.to_thread` inline, and the service's own
  thread in the background. The tag-write policy gates, the concurrency cap and
  the crash isolation are therefore the same code for a small album and a large
  one. The old `async` helper became `_write_album_track_files_bounded`, a
  `ThreadPoolExecutor` with the same `_FILE_WRITE_CONCURRENCY` cap — an `async`
  version would have forced the background path to spin up a nested event loop to
  reach it.
- **Per-file failure logging now happens in the disk phase**, so a backgrounded
  save keeps the `File tag write SKIPPED - DB updated only` diagnostic that used
  to disappear with the response.

## The background service

Modelled on `navidrome_rescan_service`, which already solved "run this after the
response, coalesced":

- **One job per album.** A save arriving while that album's job is running
  replaces the payload the running thread picks up next, so the LATEST save wins
  and two threads never rewrite the same files from two different payloads. The
  worker loops until nothing is left, then removes its own slot.
- **The exit decision and the enqueue decision share one lock**, and the exit
  *removes the key*. Without that, a save landing exactly as the worker exited
  would see a live thread, coalesce onto it, and be stranded when it returned.
- **A dead worker slot is restarted** rather than leaving the album permanently
  unprocessable.
- **A payload with no runner is reported, not ignored** — it is a programming
  error, and silence would look like a successful no-op.
- **Failures are recorded and surfaced**: `log_unified`, a `recent` tail
  (bounded, newest first) on `GET /api/maintenance/album-file-sync`, and a
  `POST .../drain` so a script can wait for one album before doing something else
  — which is why those routes live beside safe shutdown rather than on the album
  page.
- **If the enqueue itself fails, the save falls back to inline.** The safe
  direction: the database half is already durable, and doing the disk work here is
  exactly what the user asked for — it just risks the timeout it always did.
- **Ships enabled**, with `enabled: false` or `track_threshold: 0`-style settings
  available to keep every save inline (the pre-change behaviour) if the service
  ever misbehaves.

The flash message says so explicitly — *"Tagging N audio file(s) in the
background — this page does not need to stay open"* — because otherwise the user
cannot tell whether the file tags were written.

## Verification

- `tests/test_album_save_background_phase.py` (41) — the disk phase, the service,
  the route's decision, the config and UI parity.
- **Oracle: 7 mutations, all 7 caught** — rescan moved before the file writes,
  the route rescanning by itself again, the threshold gate removed, coalescing
  removed, a runner failure swallowed, the cover store gated on the branch, and a
  payload without a runner going silent.
  - The cover mutation was **not caught first time**, and the reason is worth
    recording: the test asserted source *order* only, so a runtime guard on the
    store slipped through. It now also asserts the guarding condition does not
    mention `_file_backgrounded`.
- Existing phase tests updated rather than deleted:
  `test_the_three_phases_are_called_in_one_pass` and
  `test_the_route_collects_file_jobs_instead_of_writing_inline` now anchor on
  `_run_album_file_sync_phases` (the order invariant they pin is unchanged), the
  concurrency tests call the new sync helper, and
  `test_the_file_phase_still_writes_through_the_phase_helper` additionally pins
  the `ThreadPoolExecutor(max_workers=_FILE_WRITE_CONCURRENCY)` bound.
- Sweep (`album|save|metadata|ui|config|template|static_js|maintenance`) run on
  BOTH trees: **67 failures at baseline, 67 with the change, the two lists
  identical** — zero regressions. `import app` → 414 routes (2 new). 727 Python
  files parse.
