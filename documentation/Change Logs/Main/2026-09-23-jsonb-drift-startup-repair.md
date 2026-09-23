# Startup JSONB/TEXT drift check-repair (test-site only)

**Date:** 2026-09-23
**Area:** `db/jsonb_drift_repair.py` (new), `db/bootstrap.py`

## The drift

`db/schema.py`'s `COLUMN_REGISTRY` **declares** columns as JSONB, while
`migrations/versions/001_initial_schema.py` **created** several of them as
`sa.Text()`. Only *some* ever received an `ALTER COLUMN … TYPE JSONB`. The two
mechanisms drifted apart, leaving six columns that every Python consumer
believes are JSONB while PostgreSQL still treats them as TEXT:

| Table | Columns |
|---|---|
| `tracks` | `manual_genres`, `navidrome_genres`, `spotify_genres`, `listenbrainz_genres`, `essentia_genres` |
| `missing_releases` | `lastfm_tags` |

`_ensure_columns` cannot converge them **by design** — it only ever runs
`ALTER TABLE … ADD COLUMN IF NOT EXISTS`, which is a no-op on a column that
already exists, whatever its type. That is the whole reason a startup pass is
needed rather than a registry fix.

This list is *derived*, not guessed: `test_drift_list_matches_the_two_sources_of_truth`
re-computes it as

```
declared JSONB  ∩  created TEXT by migration 001  −  directly ALTERed
```

so the hard-coded literal cannot silently go stale.

## What actually breaks — verified, not assumed

**Not the readers.** `genre_tag_aggregator.parse_json_tags` deliberately falls
back to `parse_delimited_tags` when its input is not JSON, so a TEXT column
holding `rock, metal` and a JSONB column holding `["rock","metal"]` are *both*
read correctly. That tolerance is exactly why the drift went unnoticed.

What breaks is code relying on JSONB **semantics**:

* `CAST(:value AS JSONB)` on a CSV string raises `invalid input syntax for type
  json`. This is the confirmed cause of an album save reporting **"No changes
  were made."** — the INSERT raised, `ui_routes.py` caught it at **DEBUG**,
  `updated_count` stayed 0, and the whole transaction (including the album type
  change) rolled back.
* `coerce_track_value_for_pg_type` has no JSONB branch, so a CSV string is
  handed to PostgreSQL unchanged.

## The pass

`db/jsonb_drift_repair.py::run_startup_check_repair()`:

* **Gated on `config_enables_test_site()`** — the same helper the UI cutover
  uses, so "test site" means one thing in this codebase. A config read that
  raises resolves to *disabled*: a repair must never run because a read failed.
* **Idempotent** — a column already JSONB is skipped, never rewritten. A second
  boot issues no DDL.
* **Only the six known columns**, spelled as a literal and *not* derived from
  the registry, so a future JSONB declaration cannot silently pull a new column
  into a destructive conversion.
* **Data-preserving**, mirroring the proven ALTER block already in
  `db/schema.py`: `NULL/''` → `'[]'::jsonb`, a leading `[`/`{` is cast as-is
  (**not** re-split — that would shred an existing JSON literal into one
  element per token), anything else → `to_jsonb(string_to_array(v, ','))`.
* **One column per transaction** — a failure leaves that column exactly as
  found, never half-converted.
* **Never raises** — a failure is logged and the next column attempted.
* **Creates no schema.** ⚠️ An earlier draft of this module invented five
  `idx_tracks_*_genres_gin` index names. Verified against the codebase: **no GIN
  index exists on any drifted column.** The JSONB GIN indexes in
  `INDEXES_TO_ENSURE` cover `musicbrainz_genres` / `discogs_genres` /
  `lastfm_tags` / `audiodb_genres` / `wikidata_genres` — all of which *did* get
  their ALTER. Creating indexes here would have invented schema.

## Wiring

Called from `_repair_jsonb_drift_at_boot()` in `db/bootstrap.py`, on **both**
startup paths (the immediate one and `_run_deferred_startup_migrations`), in a
**daemon thread** — it issues DDL and must never delay a boot. It is kept out of
`ensure_full_schema` deliberately: that path runs under an advisory lock and is
allowed to fail hard, whereas this is best-effort and must never be able to
prevent startup.

On a live install the thread starts, reads the flag, and exits. Verified:

```
test-site enabled in this tree: False
ran: False | reason: test-site mode is off
converted: []
```

## Tests

`tests/test_jsonb_drift_repair.py` (24) — the emphasis is on guard rails rather
than the happy path, since this issues DDL at boot:

1. it declines when test-site is off, and executes **no SQL**;
2. idempotency — correct columns are skipped, with `errors == []` *and*
   all six confirmed as *seen*;
3. unexpected types are reported, never converted;
4. the conversion expression matches `db/schema.py`'s own ALTER;
5. it never raises (probe failure and ALTER failure both captured);
6. it creates no indexes, and no GIN index targets a drifted column;
7. the drift list still matches its two sources of truth;
8. boot wiring: present, on both paths, off-thread, and unable to raise
   (asserted on the **AST**, since the docstring legitimately discusses
   `raise`).

**Mutation-tested** — a suite that only passes when the module exists proves
little, so four mutations were applied and all four must be caught:

| Mutation | Result |
|---|---|
| gate always open (DDL on a live install) | CAUGHT |
| idempotency check removed | CAUGHT |
| unexpected types converted anyway | CAUGHT |
| JSON `[`/`{` guard removed | CAUGHT |

⚠️ Mutation 2 initially **escaped**. With the `_is_jsonb` early-continue gone,
a JSONB column falls through to the unexpected-type branch, so `converted`
still ends up empty and the test passed — while every correct column was
reported as an error on every boot. The test now asserts `errors == []` and
that all six columns were *checked*, which closes the hole. Recorded here
because "the test passed" and "the test would fail if the code broke" are not
the same claim.

**Regression sweep:** 18 suites / 248 tests, pre-existing failing set
**IDENTICAL** (47) before and after — 0 regressions. Those 47 are pre-existing
SQLite-harness issues in this environment (`genres`/`manual_genres` declared
TEXT in the test schema, `EXTRACT(EPOCH FROM …)` being Postgres-only).

## ⚠️ Not fixed here

This pass repairs the **column types**. It does not fix the two code defects
that the drift exposed, which are still outstanding:

1. `coerce_track_value_for_pg_type` (`db/repositories/popularity_repository.py:90`)
   has no JSONB branch, so any writer pushing a CSV string still fails;
2. the `except Exception` at `ui_routes.py:1763` logs a **failed** save at
   DEBUG and lets the handler report *"No changes were made."* — a message that
   is factually wrong when the write raised.

Until (1) and (2) are done, an album save that carries MusicBrainz genres can
still fail — the pass only removes the type mismatch that made it *guaranteed*.

Also confirmed **not** drifted (so deliberately excluded): `audiodb_genres`,
`wikidata_genres` and `download_queue.metadata` are declared JSONB but were
added by `_ensure_columns` with their declared type, so they are genuinely
JSONB already.
