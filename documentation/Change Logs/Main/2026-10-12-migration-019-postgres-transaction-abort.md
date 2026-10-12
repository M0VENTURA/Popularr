# 2026-10-12 — migration 019 aborts the PostgreSQL transaction on boot

## Report

`docker compose up` boot log:

```
INFO  [alembic.runtime.migration] Running upgrade 018... -> 019_upcoming_releases_type_in_key
sqlalchemy.exc.InternalError: (psycopg2.errors.InFailedSqlTransaction)
current transaction is aborted, commands ignored until end of transaction block
[SQL: DROP INDEX IF EXISTS uq_upcoming_artist_album]
(stderr) ERROR: constraint "uq_upcoming_artist_album" of relation
"upcoming_releases" does not exist
STATEMENT: ALTER TABLE upcoming_releases DROP CONSTRAINT uq_upcoming_artist_album
```

The Postgres log shows the sequence: the `ALTER TABLE … DROP CONSTRAINT` fails
(the constraint doesn't exist — the old key is a bare unique INDEX on this
database), and **because PostgreSQL migrations are transactional, that failed
statement aborts the whole transaction** — so the very next statement,
`DROP INDEX IF EXISTS`, fails with `InFailedSqlTransaction` even though the
index drop itself is safe.

## Root cause

```python
# 019_upcoming_releases_type_in_key.upgrade()
try:
    op.drop_constraint(OLD_KEY, TABLE, type_="unique")
except Exception:
    pass
op.execute(sa.text(f"DROP INDEX IF EXISTS {OLD_KEY}"))
```

The old two-column key on `upcoming_releases` has existed in **two forms**: a
real table constraint (created by the early migration chain) and a **bare
unique INDEX** with the same name (the form the schema bootstrap used to
create). This production database has the index form — so `DROP CONSTRAINT`
raises. The Python `try/except` swallows the exception, but on PostgreSQL the
*server-side* transaction is already aborted; `DROP INDEX IF EXISTS` then
fails for a completely different reason.

⭐ **Lesson: an `except Exception: pass` around DDL cannot rescue a failed
PostgreSQL statement — the transaction is poisoned server-side. Never attempt
an unconditional DROP CONSTRAINT; inspect the catalog first, and only for the
CONSTRAINT form.** This is also why the SQLite test suite never caught it:
SQLite does not abort its transaction on a swallowed DDL failure, and
`op.drop_constraint` on a missing unique constraint is a no-op there. A
PostgreSQL-only defect needs a source-level structural guard.

## Fix

`019_upcoming_releases_type_in_key.py`:

- New `_has_table_constraint(bind, name)` — inspects `get_unique_constraints`
  ONLY (the constraint form), so it cannot be satisfied by the same-named
  index.
- `upgrade()` drops the constraint **only when that check passes AND the
  dialect supports `ALTER … DROP CONSTRAINT`** (`!= "sqlite"` — SQLite raises
  `NotImplementedError: No support for ALTER of constraints`; the old code
  swallowed that too). The bare `DROP INDEX IF EXISTS` handles the index form
  and is safe either way.

`017_add_download_queue_release_group.py` (the companion latent defect the
bare-tracks harness hit):

- `upgrade()`/`downgrade()` now start with
  `if not sa.inspect(op.get_bind()).has_table(_TABLE): return` — the house
  pattern `014/016/018/020` use. With only the inspector guarded, `ADD COLUMN`
  fired on a missing table (`no such table: download_queue`) in any harness or
  legacy DB without it. Production always has the table (the bootstrap creates
  it), so this is pure portability hardening, in line with every sibling
  revision.

## Tests

`tests/test_migrations_idempotent.py` — new `TestMigration019OldKeyForms`:

- Structural discriminator (`test_the_constraint_drop_is_gated_on_a_constraint_inspector_check`):
  comment-stripped source must contain exactly ONE `op.drop_constraint`, and
  it must be preceded by the `_has_table_constraint(bind, OLD_KEY)` guard.
  This is the only check that can see the PG-only abort on a SQLite suite.
  **Oracle**: restoring the old unconditional `try/except` drop makes it fail
  (`ValueError: substring not found`) while the behavioural test still passes.
- Behavioural guard (`test_the_index_form_of_the_old_key_upgrades_cleanly`):
  creates the production shape — `upcoming_releases` with a bare unique INDEX
  `uq_upcoming_artist_album` and no constraint — stamps 018, `upgrade head`
  must succeed, the old index must be GONE and the new 3-column key present.

Also completed in this commit (same file, migration-harness scope):

- **E1** (from the 2026-10-12 audit): the 5 hard-coded
  `"011_add_tracks_album_artist"` head pins now read the REAL chain head via
  `_chain_head()` (chain is at 020; a magic string rots silently).
- **E2** (from the audit): `download_queue` is pre-created in the partial
  harness **and** revision 017 is now `has_table`-guarded — the full E2 fix
  (the audit's harness-only recommendation would have left the bare-tracks
  tests failing).

## Validation

- `tests/test_migrations_idempotent.py`: **13 passed** (was 6 failed).
- `tests/test_single_detection_source_gates.py` + `test_live_4star_requires_single`: 26 passed.
- Class-A singles suites: 73 passed. `test_no_python_syntax_errors`: 6 passed.
- `git diff --check` clean.
- Oracle run: structural test fails against the OLD 019, passes with the fix.

## Deployment note

The failing container's `alembic_version` stays at 018 (the 019 transaction
rolled back). On the next boot the fixed 019 applies, then 020
(`track_popularity_cache.release_track_title`) applies immediately after —
the schema itself was already fine via the bootstrap's column sync, so this
fix simply lets the migration chain advance to head.