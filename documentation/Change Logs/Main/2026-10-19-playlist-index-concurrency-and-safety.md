# Playlist index sync: 5½ minutes → under one

**Date:** 2026-10-19 · **Area:** `playlists`

## What the reported run showed

```
Genre playlists: 31 playlist(s) synced         06:58:17
Excluded Christmas tracks ... New Music        06:58:17
Playlist index synced playlists=615 changed=66 07:03:53
entries=79434 skipped=549 pruned=0 unresolved=10 errors=0
```

**5 m 36 s inside `sync_playlist_index()`**, starting the instant the two
playlist builders finished.

The split is the diagnosis: **549 playlists were skipped** (their `changed` stamp
hadn't moved, so no request and no write — the fast path works), and **66 were
re-read**. 66 playlists × ~1,200 entries = 79,434. Each needed one `getPlaylist`
round trip in which Navidrome builds and serialises the whole entry list
(~5 s), and they were issued **strictly one after another**.

Measured on the database side first, to rule it out: 330 SQL statements for
79,398 rows (inserts are batched, ~240 rows/statement). **The DB was never the
problem** — reproducible via `tests/_measure_playlist_index.py`.

⚠️ Two theories that had to be discarded: the `http_utils` 1.05 s/domain throttle
explicitly **skips private hosts**, and Navidrome is at `192.168.1.200`; and
`_DEFAULT_RETRIES = 0`, so there was no retry amplification (`errors=0` agrees).

---

## 1. Bounded concurrency on the changed-playlist fetches

The sync is now two phases, and the split is the design:

| phase | what | why |
|---|---|---|
| 1 | fetch every CHANGED playlist's contents in a bounded pool | independent, read-only, latency-bound |
| 2 | resolve the remainder and write, one playlist at a time | one `db_session` per call; the in-memory test engine is a single shared connection; one transaction per playlist |

**Reads parallel, writes sequential.** Parallel writes would serialise anyway, and
on SQLite they contend. Only Navidrome entries are prefetched — file-backed ones
read a local file and a DB query, and those must stay on the calling thread so
every DB access happens on one connection.

`PLAYLIST_INDEX_FETCH_WORKERS` (default 4, clamped 1–16) tunes it. Four is
deliberately conservative: the same order as the tag-write pool, and it does not
ask a small NAS for more than a handful of simultaneous large queries.

### Measured

Same workload (66 playlists × 1,200 entries), scaled latency:

| workers | wall clock | playlists written | entries | errors |
|---|---|---|---|---|
| 1 (before) | 3.67 s | 66 | 79,200 | 0 |
| 4 (default) | **1.12 s** | 66 | 79,200 | 0 |
| 6 | **0.87 s** | 66 | 79,200 | 0 |

Same output, ~3.3× faster at the default. On the real ~5 s/playlist latency that
is **5 m 36 s → roughly one minute** (≈40 s at 6 workers).

---

## 2. One playlist listing per sync

`_track_ids_for` resolved a rule-based `.nsp` with
`client.find_playlist_by_name(name)` — which calls `fetch_all_playlists()`,
**re-downloading all 615 playlists' metadata to answer one name lookup**, once
per such file.

`_enumerate` already fetches that listing, so it now **returns** it
(`entries, errors, listings`) and the lookup goes through the new
`_find_in_listings`, which uses the same case-insensitive matching rule. Inside
one sync the listing cannot change, so this is free and exact.

`listings` is aligned with the client list — a server that could not be listed
contributes an empty slot, so indices never shift.

---

## 3. An unreadable playlist is no longer recorded as empty ⚠️ the dangerous one

`NavidromeClient.fetch_playlist` **swallows every error and returns `{}`**. The
old loop treated that as "0 tracks", then wrote it — and `replace_playlist`
**deletes the playlist's entries first**. So a single transient failure erased
every indexed row for that playlist and dropped it from the artist page until a
later sync happened to succeed.

New `_navidrome_ids_from_detail()` draws the line the old code could not:

| payload | meaning |
|---|---|
| `{}`, or no `tracks`/`entry` and no `songCount` | **UNREADABLE** → `None` |
| `{"tracks": []}` or `{"songCount": 0}` | genuinely empty → `[]` |
| `{"tracks": [{...}]}` | ids |

`_track_ids_for` now returns `(None, 0)` for unreadable, and the sync **skips the
write**, keeps the previous rows, and counts it in `errors`. It also keeps trying
the remaining clients instead of stopping at the first empty payload.

Same principle as the existing prune rule — *null is not a decision* — applied one
level down to a single playlist.

---

## 4. "Stop rewriting bodies that haven't changed" — already implemented

**No code change; here is the evidence, because the premise is worth checking
rather than building on.**

`sync_playlist_by_name` already compares the live ordered song list against the
requested one and returns `unchanged=True` **without issuing a write**
(`playlist_navidrome_service.py`). So an unchanged playlist does not bump
Navidrome's `changed`, and the index's stamp fast path does hit — which is what
**`skipped=549`** in the log proves.

The 66 that *were* changed are genuine content churn: a finalise pass recomputes
star ratings, which reorders `{Genre} - Top Tracks`, and New Music is rolling by
definition. No cache can remove that, because the content really did move.

There is a smaller residual win available — `sync_playlist_by_name` must fetch the
current ids to compare, and the in-process signature cache
(`_PLAYLIST_SIGNATURES`) cannot help across the 4 hypercorn workers or a restart.
Persisting that signature in `playlists` would save ~1 request per generated
playlist per pass. **Not done**: it needs a schema migration for a modest gain,
and item 1 already reduces the cost of the reads that remain.

---

## Tests

`tests/test_playlist_index_concurrency.py` — **18 passed**:

- every changed playlist is prefetched; the pool is **bounded** (asserted by
  measuring peak in-flight, with a control that it does overlap at all);
- the worker count is clamped from env (0 → 1, 9999 → 16, junk → default);
- only Navidrome entries are prefetched;
- a failed fetch is **absent** from the prefetch map, not empty;
- a payload for the wrong playlist id is ignored;
- `_enumerate` returns the listings, aligned per client;
- a rule-based `.nsp` resolves **without calling `find_playlist_by_name`**;
- the unreadable-vs-empty table above;
- the sync does **not write** over good rows when contents are unreadable;
- controls: a genuinely empty playlist IS written, a normal one still indexes,
  an unchanged one costs no fetch, and pruning still happens on a clean run.

⚠️ One existing test asserted the **bug**: `test_a_fetch_failure_is_counted_not_
raised` pinned `unresolved == 2` and `entries == 0` — i.e. the data loss. It is
**rewritten, not deleted**, with the superseded contract recorded in its
docstring, plus a new end-to-end test that seeds two rows, makes the server fail,
and asserts the rows survive.

## Verification

| sweep | result |
|---|---|
| playlist index + concurrency | 29 + 18 passed |
| 16 playlist/cover/scan suites | **384 tests** |

The only failing file, `test_navidrome_playlist_post_body.py` (5 of 7), fails
**identically on pristine `develop`** — a `_FakeResponse` stub with no `.content`
attribute, unrelated to this work.

**Zero regressions.**
