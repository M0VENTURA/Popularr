# The migration chain can now be tested on Windows (and 033/034 stop breaking on an absent table)

Date: 2026-11-28
Scope: `tests/test_migrations_idempotent.py`,
`migrations/versions/033_missing_album_tracks_is_bonus.py`,
`migrations/versions/034_tracks_vocalists.py`.

---

## Why the suite had never run

`tests/test_migrations_idempotent.py` was excluded from every full-suite run
this week with "`ModuleNotFoundError: alembic`". Installing `alembic` (declared
in `requirements.txt` but absent from the interpreter) exposed a second
problem, and it was not the missing package:

```
ValueError: invalid interpolation syntax in
'sqlite:///C%3A%5CUsers%5C…\migrations_test.db'
```

`_make_alembic_config()` handed the engine URL to Alembic's `Config`, which is a
configparser. In configparser a bare `%` starts an interpolation, and SQLAlchemy
percent-encodes a Windows temp path (`C:\Users\…` → `C%3A%5CUsers…`). So the
suite could not run on Windows **whether or not alembic was installed** — nine
tests degraded to failures at the first fixture.

Fixed by escaping percent signs on the way in (`engine_url.replace("%", "%%")`,
configparser's literal percent). That took the module from "9 failed, 4 passed"
to "9 failed, 4 passed" — with the failures now coming from the **migrations
themselves**, which is how the next problem surfaced.

## The real defect those runs found

Once the URL was accepted, the chain itself failed:

```
sqlalchemy.exc.OperationalError: no such table: missing_album_tracks
  [SQL: ALTER TABLE missing_album_tracks ADD COLUMN is_bonus …]
```

Migrations **033** and **034** guarded on the COLUMN, not the table:

```python
def _columns(bind) -> set[str]:
    inspector = sa.inspect(bind)
    if not inspector.has_table("missing_album_tracks"):
        return set()          # ← empty set when the table is absent
    ...

if "is_bonus" not in _columns(bind):      # ← True for an ABSENT table
    op.add_column("missing_album_tracks", …)   # ← ALTER on nothing
```

An absent table has no columns, so "the column is missing" is true — and the
`ALTER` then runs against a table that is not there.

This matters because this repo explicitly supports a **partially-bootstrapped**
database: `db/schema.py` creates the tables and Alembic only records revisions,
so a database can be stamped past a table's creation revision without holding
the table. The house pattern for that is an early return, as migration 023
already does:

```python
if not sa.inspect(op.get_bind()).has_table("tracks"):
    return
```

033 and 034 were the two outliers. Both now ask whether the table exists before
asking whether the column does, in `upgrade` **and** `downgrade`.

## What was checked and left alone

Twenty-one migrations contain `op.add_column`. Six older ones
(003, 011, 012, 013, 014, 016) call `inspector.get_columns("tracks")` without a
`has_table` guard first, which would raise on a database with no `tracks` table.

They are **not** changed, because that state is unreachable in this chain:
`tracks` is created by `001_initial_schema`, and every revision that adds a
column to it runs after 001 has either created the table or the bootstrap has.
The tested legacy shapes — bare `tracks (id TEXT PRIMARY KEY)` — still have the
table, which is why `TestMigration011AddsTracksAlbumArtist` passes. Changing six
migrations to defend against a state the chain cannot produce would be noise.

If a future bootstrap ever skips `tracks` entirely, those six would need the
same treatment 033/034 now have.

## Verification

`tests/test_migrations_idempotent.py` — **13 passed** (was: 9 failed, 4 passed;
previously: not collectable at all).

Full suite, now including that file:

| | Failed | Passed | Skipped | Errors |
|---|---|---|---|---|
| Before (migrations excluded) | 411 | 10,722 | 317 | 29 |
| After | **406** | **10,740** | 317 | 29 |

Five fewer failures and eighteen more passes, with the migrations module now
included rather than skipped. The 29 errors remain pre-existing and unrelated
(`test_navidrome_*` / `test_folder_match_*` rely on a table the conftest fixture
does not create).
