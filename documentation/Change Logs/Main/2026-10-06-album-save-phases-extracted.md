# The album save's per-track writes become three explicit phases

**Date:** 2026-10-06 · **Area:** album / ui
**Commit:** `refactor(album): the save's per-track writes become three phases`

## Why

Step 1 of the agreed direction for a **phased save** (the long-term choice):
the album save should eventually run as separate, separately-confirmed requests
so a modal can report progress per phase — *and* so a failure names the phase
that failed and can be retried on its own instead of re-POSTing the whole
album.

You cannot split a transport you haven't decomposed, so this commit decomposes
it with **zero behaviour change**: the route's loop still does the same writes,
for the same tracks, in the same order, with the same counters.

## What changed

`routes/ui_routes.py` — three module-level functions now own the writes:

| Phase | Function | Returns |
|---|---|---|
| 1 — genres | `_apply_album_track_genres(track_id, track, genres_str)` | `(rows, failed)` |
| 2 — persist | `_persist_album_track_payload(track_id, payload)` | `(ok, error)` |
| 3 — file tags | `_write_album_track_file_tags(...)` | `(ok, file_path)` |

The loop calls them in that order and keeps counting `genre_only_writes`,
`genre_write_failures`, `updated_count`, `db_failures`, `file_sync_failures`
exactly as before. The payload build stays inline for now: it needs ~15
handler locals, and extracting it belongs with the endpoint split (step 2),
once it is clear what context both callers share.

**Genres stay one phase on purpose.** The genre write touches *both* stores —
the JSONB columns and the file's genre tag — inside one block. Splitting them
into a "DB pass" and a "file pass" would silently drop the file half, which
would surface much later as *"my genre edit didn't stick to the files"*.

## The invariant, and the test that got it wrong first

The handler's comment says the staged review is *"written AFTER the
album-level values so a per-track value the user reviewed and kept wins over
the album-wide default."* My first test asserted that against the
`release_values` loop — and **failed**, because that loop actually sits *after*
the review.

Reading the code settled it: `release_values` writes the release/extended
fields (`recordlabel`, `catalognumber`, `barcode`, …), which are a **disjoint
set** from `_STAGED_WRITABLE` (`title`, `track_number`, `disc_number`, `mbid`,
`writer`, `musicbrainz_genres`, `is_cover`, `original_cover_artist`, `artist`).
The fields that actually collide — `writer`, `mbid` — are assigned *above* the
review, where the comment says. So the invariant to pin is
`payload["writer"] = track_composer` **before**
`_staged_for_track = _staged_updates.get(`, and nothing pinned it before this
change.

(The test also searched the whole file rather than the handler; `ROUTE_SOURCE`
now scopes every assertion to `async def album_detail(...)`.)

## Tests

`tests/test_album_save_phases.py` — **11 new**:

* **structure** — the album-wide value is written before the review; the three
  phases are called in order in one pass; the route itself does no writing
  inline (no `insert_or_update_track` / `build_tag_updates` /
  `update_track_genres` left in the handler);
* **persist phase** — success → `(True, "")`, failure → `(False, reason)` with
  the DB's own message reaching the caller;
* **genres phase** — rows and failures reported separately, empty input is a
  no-op, and **the file tag write still happens in this phase**;
* **file-tag phase** — no file → not written (counted), the "Cover" genre
  convention survives, and the single-disc `disc_number=""` clear applies only
  when the review did *not* stage a disc number.

`tests/test_album_save_reports_changes.py::TestGenresAreCounted` — its source
pin asserted the inline `_genre_rows = update_track_genres(` that this commit
moved into the phase; it now asserts the route **takes** the rowcount from the
phase and still counts failures. The behaviour it protects (a genres-only save
must not read as "no changes") is unchanged.

**Oracle:** stashing `routes/ui_routes.py` → **11 failed, 2 passed** = exactly
the tests that depend on the extraction (the 2 passes are the invariant tests,
which hold either way); with it, 13/13.

## Verification

- save-related suites (10 files): **231 passed, 0 failed**
- affected set (35 files): 664 passed / 32 failed → **0 new vs baseline**
- full suite: **225 failed, 4660 passed, 2 skipped** vs the 303-failure
  baseline → **79 fixed, 0 real regressions** (the only ID absent from the
  baseline is the known native-flaky `test_sibling_torrents_root_is_searched`)

## Next

Step 2: expose the phases as sequential JSON endpoints (keeping the form POST
as the no-JS fallback — both calling these same functions), then the modal in
both trees. The ordering invariant above is what that split must preserve.

## Files

- `routes/ui_routes.py`
- `tests/test_album_save_phases.py` (new)
- `tests/test_album_save_reports_changes.py`
