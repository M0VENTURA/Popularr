"""The unit suite's test database must be ONE SHARED database.

⚠️ WHY THIS EXISTS. The engine was built with a ``QueuePool`` over
``sqlite:///:memory:``. For an in-memory SQLite URL every CONNECTION gets its
own, separate, empty database, so:

  * any test needing a SECOND connection during the same test saw
    ``no such table: tracks``; and
  * ``dispose()`` (which ``db_session()`` performs on a transient error, and
    which the fixtures perform on teardown) discarded the single connection that
    held the schema, leaving later tests with an empty database.

The visible symptom was not an obvious crash. It was an ORDER-DEPENDENT failure
in an unrelated test file — ``test_album_missing_and_disc_cleanup``'s
``test_year_prefixed_album_matches`` — that passed in isolation and failed only
when a particular other file had run first, which reads exactly like a bug in
that test. ``tests/conftest.py``'s docstring even claimed the engine used
StaticPool while the code built a QueuePool, so the comment could not be trusted
either.

These tests fail if the engine stops providing a single shared database.
"""
from __future__ import annotations

from sqlalchemy import text


class TestTheTestEngineSharesOneDatabase:
    def test_the_engine_uses_staticpool_for_in_memory_sqlite(self):
        """The whole suite depends on this; assert it directly."""
        from db.engine import get_engine

        engine = get_engine()
        url = str(engine.url)
        assert "sqlite" in url and ":memory:" in url, (
            "this suite is expected to run against in-memory SQLite"
        )
        assert type(engine.pool).__name__ == "StaticPool", (
            "an in-memory SQLite URL gives EVERY connection its own empty "
            "database, so a QueuePool makes the shared test DB order-dependent; "
            "StaticPool is what keeps ONE connection alive for everyone"
        )

    def test_a_second_connection_sees_the_same_data(self):
        """The actual property, measured rather than inferred from the pool name.

        This is the behaviour the suite needs and the thing that was broken: it
        is possible for a pool to be "shared" by name yet not by data, so the
        data is compared rather than trusting the class.
        """
        from db.engine import get_engine

        engine = get_engine()
        with engine.connect() as first:
            first.execute(text("DROP TABLE IF EXISTS pool_shared_probe"))
            first.execute(text("CREATE TABLE pool_shared_probe (id TEXT)"))
            first.execute(text("INSERT INTO pool_shared_probe VALUES ('x')"))
            first.commit()

            # Hold the first connection OPEN while asking for a second, so the
            # pool cannot simply hand back the same checked-out connection.
            with engine.connect() as second:
                seen = second.execute(
                    text("SELECT COUNT(*) FROM pool_shared_probe")
                ).scalar()
                assert seen == 1, (
                    "a second connection saw a different database — this is the "
                    "exact condition that made unrelated tests fail depending "
                    "on collection order"
                )

            first.execute(text("DROP TABLE pool_shared_probe"))
            first.commit()

    def test_the_schema_survives_a_dispose(self):
        """``db_session()`` disposes on a transient error; the suite survives it.

        The fixtures' ``_recreate_test_schema`` exists precisely because a
        dispose used to wipe the in-memory database. With StaticPool the schema
        is (re)creatable and, once created, still visible afterwards — the
        property every test relies on.
        """
        from db.engine import get_engine
        from db.models import Track

        engine = get_engine()
        Track.__table__.create(engine, checkfirst=True)
        engine.dispose()

        Track.__table__.create(engine, checkfirst=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT COUNT(*) FROM tracks")).scalar()
