# One bad track no longer discards the whole album's writes

**Date:** 2026-10-06 · **Area:** db / scan
**Commit:** `fix(db): a failed row rolls back to its own savepoint`

## Reported

> The error `InFailedSqlTransaction` happens when a bulk insert triggers an
> underlying SQL violation … PostgreSQL immediately rejects all subsequent rows
> in that same batch … **one bad track is causing your scanner to skip inserting
> every other song on that album** into your local database!

## The suggested culprit is not it

The report blamed `last_scanned: '2026-10-06T07:41:29.996978+11:00'`, arguing
the `+11:00` offset would be rejected by a `timestamp without time zone`
column. **`last_scanned` is a `TEXT` column** — `db/models.py:194`
(`mapped_column(String)`) and `db/schema.py:303` (`"last_scanned": "TEXT"`) —
so an ISO-8601 string with an offset is a perfectly valid value for it. No
timestamp coercion happens anywhere on that path.

## The defect (mechanism was right)

`db/repositories/popularity_repository.py::upsert_tracks_bulk` runs the whole
batch — typically one album — through **one** session/transaction:

```python
with db_session() as session:
    for payload in track_payloads:
        try:
            _execute_save(session, payload)
        except Exception as exc:
            logger.warning("Bulk track upsert FAILED — row skipped …")
            # ← then CONTINUES to the next payload
```

In PostgreSQL a **failed statement aborts the transaction**. From that point
every later statement in the same transaction is rejected with

```
current transaction is aborted, commands ignored until end of transaction block
```

so the `except` logs "row skipped" for each remaining track and **nothing from
that album is written**. The loop's `except` never re-establishes a usable
transaction — which means the docstring's promise, *"Rows that fail validation
are logged and skipped so a single malformed payload never aborts the album's
remaining writes"*, was false on the database the app actually runs on.

**Why the suite never caught it:** SQLite does not abort the transaction on a
failed statement — the next statement just works — so every existing test
(including `TestBulkUpsertReportsFailures`, which asserts exactly that
behaviour) passes on SQLite while the production path loses the album.

## The fix

One **SAVEPOINT per row**:

```python
with session.begin_nested():          # SAVEPOINT
    _execute_save(session, payload)
```

An exception now issues `ROLLBACK TO SAVEPOINT`, undoing only the bad row and
leaving the transaction usable for the rest of the album. The failing row's
original error is still logged with its real Postgres reason (a WARNING naming
the track), so the actual culprit stays visible instead of being masked by a
cascade of `InFailedSqlTransaction` noise.

Two existing test fakes (`_FakeSession`, `_CountingSession`) gained
`begin_nested()`; five tests that relied on the old shape were the only
behaviour they broke, and they now model a savepoint like the real session.

## Tests

`tests/test_bulk_upsert_savepoints.py` — **4 new**, built around a session
model that reproduces Postgres's rule faithfully (a failed record aborts the
transaction, only a savepoint rollback clears it):

* the remaining rows are still written after one fails — **fails on the old
  code** (`written == []` because every later row raised `InFailedSqlTransaction`);
* each row runs inside its own savepoint, and the failed row rolls back *to its
  savepoint* rather than to the start of the transaction (which would discard
  earlier rows too);
* CONTROL — a healthy batch is unchanged;
* CONTROL — an empty batch is still a no-op.

**Oracle:** stashing `popularity_repository.py` → **2 failed, 2 passed** = the
two that depend on the fix (both controls pass); with it, 4/4.

## Verification

- savepoint tests + the 10 files touching `upsert_tracks_bulk` / `save_to_db`:
  back to the 21 pre-existing failures, **5 repaired** (the ones broken by the
  first draft's missing `begin_nested` on their fakes)
- affected set (19 files covering the upsert/save/import paths): **266 passed /
  21 failed → 0 new vs baseline**

## Files

- `db/repositories/popularity_repository.py`
- `tests/test_bulk_upsert_savepoints.py` (new)
- `tests/test_per_album_star_posting.py`
- `tests/test_track_write_failures_are_loud.py`
