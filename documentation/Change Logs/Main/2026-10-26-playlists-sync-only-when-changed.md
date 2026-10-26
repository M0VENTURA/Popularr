# Only a playlist whose content changed is pushed again

**Date:** 2026-10-26
**Area:** `services/popularity/stages/finalise_stage.py` · `tests/conftest.py`
**Commit:** `fix(playlists): remember pushes on disk so unchanged playlists are skipped`

## Reported

> "The only playlists that sync after an album scan should be the ones that are changed."

A scan log showed `Genre playlists: 69 playlist(s) synced` with a run of
`Created Navidrome playlist …` lines — i.e. Navidrome was being handed every
generated playlist although nothing about the library had changed.

## Cause — the skip existed, but its memory did not

`_sync_playlist_to_navidrome` already skipped an unchanged push:

```python
signature = _playlist_signature(song_ids)
if _playlist_already_pushed(playlist_name, signature):
    synced["skipped"] = 1
```

But the only store was `_PLAYLIST_SIGNATURES`: a **per-process dict with a
one-hour TTL**. Measured:

| scenario | remembered? |
|---|---|
| same process, within the hour | yes |
| after clearing the dict (a restart) | **no** — measured |
| a scan longer than the hour | **no** |
| a different hypercorn worker / the queue worker | **no** — each keeps its own dict |

So a redeploy, a long scan, or simply which process ran the rebuild decided
whether anything was skipped. A playlist's content does not become stale because
time passed or a process restarted, so the record now lives on disk beside the
other runtime state files.

## What changed

**A durable store** — `<SCAN_STATE_DIR>/playlist_pushes.json`, holding
`{playlist name: {sig, at}}`:

* every record/forget is a **read-modify-write under an exclusive `flock`** plus
  an atomic `os.replace`, so the four web workers and the queue worker cannot
  drop each other's entries (a dropped entry means an unnecessary re-push);
* the in-memory dict stays as a fast path, but its TTL now bounds MEMORY only —
  it no longer invalidates the durable record;
* a corrupt or missing file reads as `{}`, which can only cause a redundant
  push, **never a skipped one**;
* the map is bounded by dropping the OLDEST pushes.

**A bounded age (7 days)** replaces the one-hour TTL as the answer to "a playlist
was edited by hand in Navidrome, when do we take it back?". Seven days keeps the
promise while still self-healing eventually.

**Deletion is immediate, not aged.** `_sync_playlist_to_navidrome` gained
`server_confirmed_exists`, and the genre builder passes the `playlist_exists`
value it had ALREADY computed from the server's playlist list. When the server
does not have the playlist, the remembered push is dropped and it is re-created
on that pass — the age bound is not involved.

**Config saves still reset everything.** `reset_playlist_push_cache()` now
clears the file as well as the dict, because a naming/limit/ordering change
makes every previous signature meaningless.

## ⚠️ Test-harness fix that came with it

`tests/conftest.py` never set `SCAN_STATE_DIR`, so the suite used the default
`/state` — which on this machine resolves to a real `C:\state` that already
exists. Any test writing runtime state either polluted it or read another run's
leftovers. It is now a fresh temp directory per run.

## Verification

`tests/test_playlist_sync_only_when_changed.py` (20).

Oracle — durable store reverted with the tests present: **10 failed / 10 passed**;
restored → 20 passed. The 10 that pass unpatched are the guards that describe the
existing behaviour (different tracks push, reorder pushes, case-insensitive name).

Sweep, playlist/genre/navidrome/undefined-name suites, one process per file:
patched **53** failing ids vs baseline **65**, `Compare-Object` NEW **empty**.

⚠️ The 12 "gone" entries are NOT attributable to this change. They are
pre-existing order/interaction-dependent failures that flip between runs — the
same two files fail when run together (3 failed) and pass when run separately.
Recorded here so nobody reads that delta as a fix.

## Deliberately NOT changed

`_sync_essential_playlist` and the curated builders pass no existence hint, so a
playlist deleted on the server for one of those comes back within the 7-day age
bound rather than immediately. Adding a hint there means a `fetch_all_playlists`
call per artist; the genre playlists (69 of ~70 in the reported log) already get
it for free, so the remaining exposure is one playlist per artist.
