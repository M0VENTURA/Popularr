# Album fingerprint converges; no artist-normalise oscillation; no deadlock

**Date:** 2026-11-24
**Area:** `services/scanning/reconciliation.py` ·
`db/repositories/scan_repository.py` ·
`services/metadata/pending_update_service.py`
**Commit:** `fix(scan): make the album fingerprint converge; stop the artist no-op rewrite; order row locks`

## Reported

Thousands of lines like:

```
INFO services.scanning.filters  Re-importing album — song IDs match but metadata
     changed or cannot be proven unchanged album='Roots' fingerprint_equal=False dirty=False
```

plus `Normalized existing artist rows canonical_artist='SOiL' alias='SOiL' updated_rows=40`,
and in PostgreSQL: multi-minute checkpoints, a 40-minute autovacuum of `tracks`
(1926 tuples removed), and

```
ERROR: deadlock detected
Process 26841: UPDATE tracks SET local_tags_written_at = CURRENT_TIMESTAMP ... WHERE id IN (…)
Process 26869: UPDATE tracks SET pending_mb_updates = '{…}' WHERE id = '0Tr3u5…'
```

`dirty=False` on every line was the tell: nothing had been edited — the
comparison itself could never succeed. The consequences were all consequences of
that: constant full re-imports → constant writes → checkpoint/autovacuum
pressure → a deadlock between two writers of the same rows.

## 1. Two asymmetries made the fingerprint permanently unequal

`album_fingerprint` hashes a per-field record built by
`navidrome_row_record` (incoming) and `db_row_record` (stored). Two fields were
built differently:

**`year`** — Navidrome's `Child.year` is the file's DATE tag, the **edition**
year. `payload_builder` deliberately stores it in `release_year` and puts the
**original** year in `year`. The fingerprint compared the incoming *edition*
year against the stored *original* year, so every remaster/reissue (1987 vs 2013)
was unequal forever. `release_year` is now a fingerprint field and both records
compare the edition side (falling back to `year` when no edition year exists).

**`artist`/`album_artist`** — the incoming side applied
`clean_artist_name_for_storage`; the stored side read the column raw. A case-only
divergence ("Soil" vs "SOiL") never settled. Both sides now clean the same way.

## 2. The artist normaliser oscillated with the payload

`normalize_existing_artist_rows` added `.lower()`/`.upper()`/`.title()` of the
canonical name **unconditionally**. For a mixed-case act that is a *different*
spelling — `"SOiL".title() == "Soil"` — so the scan rewrote `"Soil"` → `"SOiL"`
while `payload_builder` wrote `"Soil"` back on the very same pass: 40 rows
rewritten per artist per scan, forever.

Only the caller's real aliases are rewritten now (still key-checked against the
canonical). A second run is a no-op, which the new test pins.

## 3. The deadlock: deterministic row-lock order

`mark_local_tags_written_for_track_ids` issued one multi-row `UPDATE … WHERE id
IN (…)` — row locks taken in scan order — while
`pending_update_service` updates the same rows one at a time. Two orders, one
cycle, and PostgreSQL killed a session.

The stamp now writes **one row per statement, in ascending id order** (also
avoiding the Postgres-only `ORDER BY` that SQLite rejects), and the pending
writer iterates `sorted(track_ids)`. Both acquire in the same order, so the
cycle cannot form. The album-level recommendation block still lands on a stable
row (`min(track_ids)`) rather than whatever the query happened to return first.

## Tests

`test_album_fingerprint_matches_production_shapes.py` — the real production
shapes (original 1987 / edition 2013; mixed-case and payload-cleaned artist
names) match, a genuine edition or artist change still differs, and
`should_skip_cached_album` actually skips.
`test_artist_normalise_does_not_oscillate.py` — `"SOiL"` is left alone, a real
alias is still merged, a second run is a no-op, and the stamp writes in sorted
order.
