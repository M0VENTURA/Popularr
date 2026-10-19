# Zero popularity source no longer freezes; album save writes the album in one commit

**Date:** 2026-10-19 · **Area:** `popularity` · `db` · `ui`

Two independent fixes from the same scan log: a track whose ListenBrainz score
was stuck at `0.0`, and a slow synchronous DB loop on a concurrent album save.

---

## 1 — A zero source could freeze into a cached score forever

### Reported

> A track (*The Infection*) kept showing `LB: 0.0` on every non-forced scan even
> though its Last.fm side was healthy.

### Why the cache kept it frozen

The cached branch in `track_stage` reused the stored score when two gates both
passed:

* `should_use_cached_score(track)` — only checks the freshness **timestamp**
  (`lastfm_last_updated` / `listenbrainz_last_updated` within the TTL);
* `_has_credible_data` — accepts **either** source (`lf >= 25 or lb >= 25`).

So `lastfm_listeners = 21000`, `listenbrainz_listens = 0` passed both: the score
was "fresh" and "credible", the stale `final_score` was reused, and the zeroed
ListenBrainz source was **never re-queried**. Only the fresh path re-fetches a
zeroed source.

### The change

The missing half of the gate now lives next to the other freshness rules:
`popularity_cache_policy.popularity_sources_are_complete(track)` is `True` only
when **neither** source is zero. The cached branch requires it:

```python
_sources_complete = popularity_sources_are_complete(effective_track)
_cached = (
    not _force
    and (frozen_track or should_use_cached_score(effective_track))
    and bool(effective_track.get("final_score") and _has_credible_data)
    and (frozen_track or _sources_complete)
)
```

⚠️ **An explicitly FROZEN track still reuses its stored score.** Freezing is a
deliberate user choice, so the zero check is bypassed for it — the same escape
hatch as before.

⚠️ **The trade is intentional:** a genuinely popular track with no ListenBrainz
presence re-queries LB once per scan instead of never. `0` can no longer be
sticky.

---

## 2 — The album save opened one DB session + commit PER TRACK

### Reported

> A 14-track album (*Bigbang — MADE*) spent ~68 s in the synchronous DB loop
> while the file-tag phase took ~1 s.

### Why it was slow

Every track in the save loop called `_persist_album_track_payload(track_id,
payload)` → `insert_or_update_track` → `save_to_db` → a fresh `db_session()` and
**one COMMIT per row**. An album therefore cost N transactions, and that per-row
commit was the wall clock.

### The change

The loop now **collects** payloads (`_db_payloads.append(payload)`) and the album
is committed in a single phase after the loop:

```python
_updated, _failed = _persist_album_track_payloads(_db_payloads)
updated_count += _updated
db_failures += _failed
```

`_persist_album_track_payloads` hands the whole list to
`upsert_tracks_bulk`, which runs the album through **one session** and wraps each
row in a SAVEPOINT (so one malformed row cannot abort the rest — PostgreSQL
aborts the whole transaction on a failed statement otherwise).

⚠️ **The DB write still lands BEFORE the file phase.** The persist call sits
after the loop and before `_run_album_file_sync_phases`, so the
"database first, then files" invariant is unchanged — pinned by
`test_album_save_phases.py`.

⚠️ **Counting is all-or-nothing at the batch level.** `upsert_tracks_bulk`
returns `False` when ANY row failed (each bad row is logged with its `track_id`
by the repository), so a partial batch is reported as a **failure**, never as
"No changes were made." Reporting a refused write as a no-op is the exact
misreport this file's reporting tests exist to prevent.

---

## Tests

* `tests/test_popularity_cache_zero_source.py` (**new**) — the helper, the full
  reconstructed decision and the `track_stage` wiring.
* `tests/test_album_save_phases.py` — the persist phase now has batch tests
  (one bulk call, empty batch, refused row, partial batch) and the phase-order
  anchors follow the batched write.
* Stub sites updated from the retired per-track seam (`ui.insert_or_update_track`)
  to `ui.upsert_tracks_bulk`:
  `test_album_review_save_persists.py`, `test_album_save_genre_precedence.py`,
  `test_navidrome_rescan_after_save.py`.

All affected suites pass (203 tests across the fix-related files).
