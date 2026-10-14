"""Artist alternate-name (alias) repository queries.

The table is tiny (renamed artists, not rows), so the hot path loads it WHOLE
into the resolution cache (``services.metadata.artist_alias_service``) instead
of querying per comparison. These functions serve the cache load and the
page's CRUD.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import inspect, text

from db.engine import db_session


def _table_exists(session: Any) -> bool:
    """True when ``artist_aliases`` is present.

    ⭐ Reads are guarded because of the ENGINE-DISPOSE trap: ``db_session``
    DISPOSES the shared engine on a transient error, and for an in-memory
    test database that erases EVERY table — one "no such table" from this
    module used to wipe ``tracks`` mid-test and fail an unrelated suite five
    queries later. A missing table is a QUIET answer ("" / None / []), not an
    error: production creates it at bootstrap, and a pre-bootstrap reader must
    degrade to the pre-alias behaviour rather than poison the pool.
    """
    try:
        return inspect(session.get_bind()).has_table("artist_aliases")
    except Exception:
        return False


def load_all_aliases() -> list[dict[str, Any]]:
    """Every alias row — one SELECT that fills the whole resolution cache.

    Empty when the table is absent (see ``_table_exists``).
    """
    with db_session() as session:
        if not _table_exists(session):
            return []
        rows = session.execute(
            text(
                "SELECT alias_key, alias, artist_key, artist "
                "FROM artist_aliases"
            )
        ).mappings().all()
        return [dict(row) for row in rows]


def find_alias(alias_key: str) -> dict[str, Any] | None:
    """The row claiming this name, if any — used for conflict checks.

    ``None`` both when unclaimed and when the table is absent (a pre-bootstrap
    reader must not trigger the engine-dispose path; see ``_table_exists``).
    """
    with db_session() as session:
        if not _table_exists(session):
            return None
        row = session.execute(
            text(
                "SELECT alias_key, alias, artist_key, artist "
                "FROM artist_aliases "
                "WHERE alias_key = :alias_key"
            ),
            {"alias_key": alias_key},
        ).mappings().first()
        return dict(row) if row else None


def insert_alias(
    *, alias_key: str, alias: str, artist_key: str, artist: str
) -> bool:
    """Insert one alias. False when the key was already taken (a race).

    ``ON CONFLICT DO NOTHING`` rather than an upsert: silently RE-pointing an
    existing alias would let a second artist steal a name the user saw listed
    under the first one — the conflict is reported and the UI shows it.
    """
    with db_session() as session:
        result = session.execute(
            text(
                "INSERT INTO artist_aliases (alias_key, alias, artist_key, artist) "
                "VALUES (:alias_key, :alias, :artist_key, :artist) "
                "ON CONFLICT (alias_key) DO NOTHING"
            ),
            {"alias_key": alias_key, "alias": alias,
             "artist_key": artist_key, "artist": artist},
        )
        return bool(result.rowcount)


def delete_alias_by_key(alias_key: str) -> int:
    """Remove the row registered under this name. Idempotent (0 = already gone).

    Key-only on purpose: under an alias CHAIN (A→B, B→C) a name displayed in
    C's group does not necessarily point AT C — constraining the delete by
    ``artist_key`` would match nothing and the UI would remove the badge while
    the row survived. The name is only ever displayed inside the group it
    resolves into, so the key alone identifies exactly the row the user means.
    """
    with db_session() as session:
        result = session.execute(
            text("DELETE FROM artist_aliases WHERE alias_key = :alias_key"),
            {"alias_key": alias_key},
        )
        return int(result.rowcount or 0)
