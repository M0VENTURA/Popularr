# Saving metadata no longer stalls the worker (the timeouts)

**Date:** 2026-10-06 · **Area:** ui / album
**Commit:** `fix(ui): the album save stops blocking the event loop`

## Reported

> I keep getting timeouts when saving metadata.

## Cause

`routes/ui_routes.py::album_detail` is an `async` handler that ran blocking
work inline — and the codebase already knew it: `ui_routes.py::album_detail` sat
in `_KNOWN_OFFENDERS` in `tests/test_async_routes_do_not_block_event_loop.py`
under the instruction *"Offload one → delete its line here."*

Every save with a release MBID performed **two synchronous MusicBrainz calls**:

```python
_mb_release = fetch_musicbrainz_release_metadata(album_mbid)
_raw = get_shared_mb_client().get_release(album_mbid, inc="artist-credits+release-groups")
```

Both go through `api_clients/musicbrainz_http.py::_strict_throttle`, which
**sleeps** to reserve a slot in a global **1 req/s** budget shared with any
running popularity scan — the same guard test documents *"30-40s MusicBrainz
calls"* in production. And a second network call sat further down:
`_httpx.get(cover_url, timeout=10)` (or a Cover Art Archive fetch).

Because hypercorn serves each worker on a single asyncio loop, that work
stalled **every** request in the worker — so the save's own fetch timed out,
and the rest of the app appeared frozen behind it.

## The change

1. **The backfill only runs when the release actually changed.**
   `_prev_mbid` is read from the tracks already loaded for the page, and the
   fetch happens only when `album_mbid != _prev_mbid`. The block's own comment
   says it exists *"when saving from the release picker"* — the values it
   fills are exactly the ones a newly-linked release contributes. Re-fetching
   the same release while editing only the type, year or genres bought
   nothing.
2. **What remains runs off the loop.** Two plain functions now hold the
   blocking work and are called with `await asyncio.to_thread(...)` — the
   pattern already used in `routes/album_routes.py`, `routes/api_v1/albums.py`
   and `routes/download_search_routes.py`:
   * `_fetch_album_mb_backfill(album_mbid, album_type, release_values)` — the
     two MusicBrainz calls, returning the album-level values plus the
     per-recording track map;
   * `_resolve_album_cover_bytes(cover_url, album_mbid, album_rg_mbid)` — the
     `data:` / HTTP(S) / Cover Art Archive branches, returning
     `(bytes, mime_type)`.
3. **`ui_routes.py::album_detail` deleted from `_KNOWN_OFFENDERS`** — the
   ratchet now enforces it: `test_known_offender_list_has_no_stale_entries`
   fails if the line is left behind, and `test_no_new_async_handler_blocks_the_event_loop`
   fails if the handler ever blocks again.

Ordinary saves now do **no network I/O at all**; a release change still gets
its full backfill, just on a worker thread instead of the loop.

## Tests

* `tests/test_async_routes_do_not_block_event_loop.py` — new
  `test_album_detail_is_offloaded_and_not_an_offender` (both helpers exist,
  both are called through `asyncio.to_thread`, and the allow-list entry is
  gone). The call assertions use `re.search(r"asyncio\.to_thread\(\s*…")`
  because the arguments wrap onto the next line — an exact substring missed it.
* `tests/test_album_save_reports_changes.py` — new
  `TestTheMusicBrainzBackfillIsGated`: the gate (`album_mbid != _prev_mbid`)
  and the offload are both present in the save's own window.

**Oracle:** stashing `routes/ui_routes.py` + `db/bootstrap.py` → **6 failed,
1 passed** = exactly the tests depending on these changes (the 1 pass is the
entrypoint control, which reads `entrypoint.sh`); with them, 7/7.

## Not fixed here

`album_routes.py::api_album_metadata` still blocks the loop with `db_session`
— a pre-existing failure already recorded in the baseline, untouched by this
change.

## Verification

- targeted: **61 passed** (the 1 failure is that pre-existing offender)
- affected set (27 files): 534 passed / 7 failed → **0 new vs baseline**

## Files

- `routes/ui_routes.py`
- `tests/test_async_routes_do_not_block_event_loop.py`
- `tests/test_album_save_reports_changes.py`
