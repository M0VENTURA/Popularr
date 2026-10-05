# Schema bootstrap defers cleanly when PostgreSQL is not up yet

**Date:** 2026-10-06 · **Area:** db / bootstrap
**Commit:** `fix(db): the schema bootstrap defers instead of crashing`

## Reported

```
File "/app/db/bootstrap.py", line 456, in <module>
    result = verify_all_tables_exist()
File "/app/db/schema_helpers.py", line 23, in table_exists
    ...
File ".../psycopg2/__init__.py", line 122, in connect
    conn = _connect(dsn, ...)
```

## Why this is the common case, not an edge case

* `entrypoint.sh::wait_for_db` **returns immediately** when no `PG_HOST` is
  configured — which is exactly the `DATABASE_URL`-driven setup — so *nothing*
  waits for the server before the bootstrap runs.
* `init_database_and_schema()` handles that correctly: four attempts, a 5s
  sleep, and `is_transient_pg_startup_error()` (which treats any
  `OperationalError` as transient) swallowing the failure and returning
  `False`.
* …but the **second** connection it triggers — `verify_all_tables_exist()` in
  the `__main__` block — was unguarded, so the run died with a full
  SQLAlchemy/psycopg2 traceback.
* The entrypoint's fallback branch is what surfaced it: on failure it re-runs
  the module **without** `>/dev/null`
  (`python3 -m db.bootstrap || true`), printing everything.

Not fatal to the container — `run_schema_bootstrap || true` — but alarming
enough to look like a broken install, and it means the "verified" step never
actually ran.

## The change

`db/bootstrap.py::__main__` now wraps the verification:

```python
try:
    result = verify_all_tables_exist()
except Exception as exc:
    if not is_transient_pg_startup_error(exc):
        raise
    print("  ⚠ PostgreSQL not reachable yet — table verification deferred "
          "to the runtime bootstrap (it retries once connected): "
          f"{type(exc).__name__}")
    raise SystemExit(1) from None
```

Three properties matter:

1. **Non-transient errors still raise** — a genuinely broken database is not
   papered over with a friendly message.
2. **The exit code stays non-zero.** Exiting 0 would make `entrypoint.sh`
   print *"All 9 table groups verified"* when nothing was verified — a lie
   about the schema.
3. **No traceback** for the expected case: the operator gets one line naming
   what happens next (the runtime bootstrap retries once connected).

## Tests

`tests/test_schema_bootstrap_defers_when_db_is_down.py` — **5**:

* the `__main__` block performs the transient check;
* a deferred verification exits non-zero (`raise SystemExit(1) from None`);
* a real fault is still raised (the text after the guard still contains
  `raise`);
* the deferred message names the next actor;
* CONTROL (`TestTheEntrypointToleratesIt`): `run_schema_bootstrap || true` is
  still present, so a not-yet-ready PostgreSQL cannot abort the container.

**Oracle:** stashing `routes/ui_routes.py` + `db/bootstrap.py` → the four
guard tests fail and the entrypoint control passes; with them, 7/7.

## Verification

- targeted: 5 new tests + the ratchet/save suites → **61 passed**
- affected set (27 files): 534 passed / 7 failed → **0 new vs baseline**

## Files

- `db/bootstrap.py`
- `tests/test_schema_bootstrap_defers_when_db_is_down.py` (new)
