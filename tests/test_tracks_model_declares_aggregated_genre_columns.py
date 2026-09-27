"""The ORM must declare every column the genre aggregation SELECTs.

WHY THIS EXISTS
    ``db/schema.py``'s ``COLUMN_REGISTRY`` is the schema a live install gets,
    but the unit suite builds its ``tracks`` table from the ORM
    (``Track.__table__.create`` in ``tests/conftest.py``).  When the two
    disagree, the ORM is silently the smaller one: a query naming a column that
    only the registry has raises ``OperationalError`` under SQLite.

    That is not a hypothetical. ``audiodb_genres`` and ``wikidata_genres`` were
    declared in the registry (JSONB, with GIN indexes) and MISSING from the
    model, so ``get_track_recommendations`` — which selects both — failed on
    every unit test. The caller wraps that query in a broad
    ``except Exception`` that logs a warning, so the failure presented as
    "genre aggregation returned nothing" rather than as an error. This is why
    the Various Artists genre defect could not be reproduced in a test until
    the columns were declared.

WHAT IS PINNED
    The exact columns named by ``get_track_recommendations``, so the two cannot
    drift apart again in the direction that matters. The SQLite variant is
    asserted because a bare ``JSONB`` cannot even be COMPILED for SQLite — the
    declaration has to be portable for the suite to be able to build the table.

    NOTE: this is deliberately narrower than full registry parity.
    ``spotify_album_type`` and other registry columns are still absent from the
    model; asserting parity would fail on pre-existing drift that is outside
    this change. This test passes TODAY and fails if these two regress.
"""
from __future__ import annotations

from db.models import Track

#: Spelled exactly as ``genre_aggregation_service.get_track_recommendations``
#: names them in its SELECT. Kept as a literal so the test fails loudly if the
#: query grows a column the model does not carry.
GENRE_QUERY_COLUMNS = (
    "lastfm_tags",
    "musicbrainz_genres",
    "discogs_genres",
    "listenbrainz_genres",
    "spotify_genres",
    "essentia_genres",
    "manual_genres",
    "navidrome_genres",
    "audiodb_genres",
    "wikidata_genres",
)


def test_orm_declares_every_column_the_genre_query_selects():
    columns = set(Track.__table__.columns.keys())
    missing = [c for c in GENRE_QUERY_COLUMNS if c not in columns]
    assert not missing, (
        f"db/models.py does not declare {missing}; the unit suite builds its "
        "schema from the ORM, so get_track_recommendations() would raise "
        "OperationalError and its caller would swallow it as a warning"
    )


def test_aggregated_jsonb_genre_columns_compile_for_sqlite():
    """A bare JSONB cannot compile for SQLite; the declaration must be portable.

    ``audiodb_genres``/``wikidata_genres`` are genuinely JSONB in a live
    database (``_ensure_columns`` added them with the registry type), so the
    model keeps JSONB for PostgreSQL and falls back to JSON for SQLite.
    """
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.dialects.postgresql import JSONB

    for name in ("audiodb_genres", "wikidata_genres"):
        column = Track.__table__.columns[name]
        assert (
            column.type.compile(dialect=postgresql.dialect()) == "JSONB"
        ), f"{name} should still be JSONB on PostgreSQL"
        # The assertion that matters: this must not raise CompileError.
        assert column.type.compile(dialect=sqlite.dialect()) == "JSON"
        assert isinstance(column.type, JSONB)


def test_genre_columns_hold_a_list_not_a_delimited_string():
    """``payload_builder`` seeds these with ``JSON_EMPTY_LIST``.

    They are a JSON LIST, unlike the older genre columns that hold a
    comma-delimited string, so they must not be declared as ``Text``.
    """
    from sqlalchemy import Text

    for name in ("audiodb_genres", "wikidata_genres"):
        column_type = Track.__table__.columns[name].type
        assert not isinstance(column_type, Text), (
            f"{name} holds a JSON list (see services/scanning/payload_builder.py) "
            "and must not be declared as Text"
        )
