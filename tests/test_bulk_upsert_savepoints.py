"""One bad track must not throw away the whole album's writes.

Reported
--------
> The error `InFailedSqlTransaction` happens when a bulk insert triggers an
> underlying SQL violation … PostgreSQL immediately rejects all subsequent
> rows in that same batch … one bad track is causing your scanner to skip
> inserting every other song on that album into your local database!

The *culprit* in that report was wrong — it blamed ``last_scanned``, which is a
**TEXT** column (``db/models.py:194``, ``db/schema.py:303``), so an ISO string
carrying ``+11:00`` cannot be rejected. The *mechanism* is right though, and
the defect is in ``upsert_tracks_bulk``:

```python
with db_session() as session:
    for payload in track_payloads:
        try:
            _execute_save(session, payload)
        except Exception as exc:
            logger.warning("… row skipped …")   # ← then CONTINUES
```

Every row shares ONE transaction. In PostgreSQL a failed statement puts that
transaction into an aborted state, so **every later row fails with
``InFailedSqlTransaction``** and the album's remaining writes are lost — while
the docstring promised *"a single malformed payload never aborts the album's
remaining writes."* SQLite does not behave this way (a failed statement leaves
the transaction usable), which is exactly why the suite never caught it.

The fix is a SAVEPOINT per row: ``session.begin_nested()`` issues
``SAVEPOINT`` / ``ROLLBACK TO SAVEPOINT``, so the bad row is undone and the
transaction stays usable for the rest of the album.

The session model below reproduces Postgres's rule faithfully enough to make
that contract testable: a failed record aborts the transaction, and only a
savepoint rollback clears it.
"""

from __future__ import annotations

from typing import Any

import pytest

import db.repositories.popularity_repository as repo


class _AbortError(RuntimeError):
    """Stands in for psycopg2's InFailedSqlTransaction."""


class _Savepoint:
    def __init__(self, session: "_PgLikeSession") -> None:
        self._session = session

    def __enter__(self) -> "_Savepoint":
        self._session.savepoints += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            # ROLLBACK TO SAVEPOINT — the transaction becomes usable again.
            self._session.aborted = False
            self._session.rollbacks += 1
        return False


class _PgLikeSession:
    """PostgreSQL's rule: a failed statement aborts the transaction."""

    def __init__(self) -> None:
        self.aborted = False
        self.savepoints = 0
        self.rollbacks = 0
        self.written: list[str] = []

    def begin_nested(self) -> _Savepoint:
        return _Savepoint(self)

    def record(self, track_id: str) -> None:
        if self.aborted:
            raise _AbortError(
                "current transaction is aborted, commands ignored until end "
                "of transaction block"
            )
        self.written.append(str(track_id))


class _CM:
    def __init__(self, session: _PgLikeSession) -> None:
        self._session = session

    def __enter__(self) -> _PgLikeSession:
        return self._session

    def __exit__(self, *exc: Any) -> bool:
        return False


def _wire(monkeypatch: pytest.MonkeyPatch, session: _PgLikeSession) -> None:
    """Route upsert_tracks_bulk at our session and a row-level save."""
    monkeypatch.setattr(repo, "db_session", lambda: _CM(session))

    def _save(_session: Any, payload: dict) -> bool:
        track_id = str(payload.get("id") or "")
        if payload.get("_boom"):
            # The failing statement itself: it aborts, then raises.
            session.aborted = True
            session.record(track_id)  # raises InFailedSqlTransaction-style
            raise RuntimeError("null value in column title violates not-null constraint")
        session.record(track_id)
        return True

    monkeypatch.setattr(repo, "_execute_save", _save)


# ===========================================================================
# The contract: one bad row must not discard the rest
# ===========================================================================
class TestOneBadRowDoesNotAbortTheAlbum:
    def test_the_remaining_rows_are_still_written(self, monkeypatch):
        session = _PgLikeSession()
        _wire(monkeypatch, session)

        ok = repo.upsert_tracks_bulk([
            {"id": "bad", "_boom": True},
            {"id": "good-1"},
            {"id": "good-2"},
        ])

        assert ok is False, "the caller must still be told a row failed"
        assert session.written == ["good-1", "good-2"], (
            "without a savepoint the failed statement aborts the shared "
            f"transaction and every later row is rejected; written={session.written}"
        )

    def test_each_row_runs_inside_its_own_savepoint(self, monkeypatch):
        session = _PgLikeSession()
        _wire(monkeypatch, session)

        repo.upsert_tracks_bulk([
            {"id": "bad", "_boom": True},
            {"id": "good-1"},
        ])

        assert session.savepoints >= 2, (
            f"expected one SAVEPOINT per row, saw {session.savepoints}"
        )
        assert session.rollbacks == 1, (
            "the failed row must be rolled back to its savepoint, not to the "
            "start of the transaction (that would discard earlier rows too)"
        )

    def test_a_healthy_batch_is_unaffected(self, monkeypatch):
        """CONTROL — savepoints must not change the happy path."""
        session = _PgLikeSession()
        _wire(monkeypatch, session)

        ok = repo.upsert_tracks_bulk([{"id": "a"}, {"id": "b"}])

        assert ok is True
        assert session.written == ["a", "b"]
        assert session.rollbacks == 0

    def test_an_empty_batch_is_still_a_no_op(self):
        assert repo.upsert_tracks_bulk([]) is False
