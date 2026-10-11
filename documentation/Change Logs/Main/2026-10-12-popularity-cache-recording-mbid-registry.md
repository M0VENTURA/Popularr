# The popularity-cache `recording_mbid` column is now self-healing

**Date:** 2026-10-12
**Area:** db / scan
**Commit:** `fix(db): the popularity-cache recording_mbid column is repaired by the bootstrap`

## Reported

```
[ERROR] [db.repositories.popularity_cache] Failed to bulk upsert track popularity
artist='Nct Dream' error='(psycopg2.errors.UndefinedColumn) column "recording_mbid"
of relation "track_popularity_cache" does not exist'
```

## Root cause

`track_popularity_cache.recording_mbid` is added by **migration 018**
(`018_track_popularity_cache_recording_mbid`), but it was **never listed in
`db.schema.COLUMN_REGISTRY`** — the structure the bootstrap's
`_ensure_columns` sync reads to add missing columns to an existing table on
every startup.

That matters because the entrypoint swallows a failed migration:

```sh
run_alembic_migrations || true
```

So a database whose migration chain broke before 018 (or whose `upgrade
head` failed for any reason) never gains the column — and **nothing else
repairs it**. The popularity-cache upsert references the column
unconditionally, in both the `INSERT` column list and the `ON CONFLICT`
clause, so the whole artist batch fails with `UndefinedColumn` and **no
popularity data is persisted at all** for that artist (the reported "Nct
Dream" case).

The migration itself is correct and the chain (017 → 018 → 019) is intact;
the gap was the missing registry entry that would have repaired the table
independently of the migration's outcome.

## Fix

Add `recording_mbid TEXT` to
`COLUMN_REGISTRY["track_popularity_cache"]`. The bootstrap's `_ensure_columns`
runs on every startup with `ADD COLUMN IF NOT EXISTS`, so the column is now
added to any existing table regardless of whether migration 018 ran — a
belt-and-suspenders repair that closes the "migration failed and nothing
else fixed it" hole.

(For context: this is the same class of gap as the `artist_release_cache`
`updated_at` column fixed earlier — a column present in the `CREATE TABLE`
DDL but absent from the registry, so older databases never gained it.)

## Tests

`tests/test_popularity_cache_recording_mbid_registry.py` (3):

- `recording_mbid` is present in `COLUMN_REGISTRY["track_popularity_cache"]`
  (the primary contract).
- The pre-existing `lastfm_tags` entry is preserved.
- Guard: the upsert really does reference `recording_mbid` (so the registry
  entry's premise stays honest — if a future change makes the upsert tolerate
  a missing column, this test flags the registry entry for revisit).

**Oracle** at `1456c084`: the registry test fails, the 2 controls pass.
**Sweep**: `test_migrations_idempotent.py` → 6 failed / 50 passed, failing
set **identical** to baseline (the 6 are the pre-existing bare-tracks/011-era
chain failures); popularity-cache + scan + prefetch suites → all passing.

## Note for the live instance

This fix takes effect on the **next rebuild + restart** — the bootstrap
adds the column at startup. No manual SQL is required. If you want to
confirm before that, the migration log line
`Running upgrade 017 → 018_track_popularity_cache_recording_mbid` shows the
migration ran; with this fix the bootstrap also repairs it even if that
line is absent.
