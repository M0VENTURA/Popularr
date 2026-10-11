"""The popularity-cache upsert must not be blocked by a missing column.

Live error:

```
[ERROR] [db.repositories.popularity_cache] Failed to bulk upsert track popularity
artist='Nct Dream' error='(psycopg2.errors.UndefinedColumn) column "recording_mbid"
of relation "track_popularity_cache" does not exist'
```

``track_popularity_cache.recording_mbid`` is added by migration 018, but the
bootstrap's ``COLUMN_REGISTRY`` never listed it — and the entrypoint swallows a
failed ``alembic upgrade head`` with ``|| true``. So a database whose migration
chain broke (or has not yet reached 018) never gains the column, and the
popularity-cache upsert — which references it unconditionally in both the
INSERT column list and the ``ON CONFLICT`` clause — fails wholesale for the
whole artist batch. No popularity data is persisted at all.

The registry is the belt: the bootstrap's ``_ensure_columns`` runs on every
startup with ``ADD COLUMN IF NOT EXISTS``, so listing the column there repairs
the table independently of the migration's outcome.
"""

from __future__ import annotations


def test_registry_lists_recording_mbid_for_popularity_cache() -> None:
    """The column must be in the bootstrap sync, not only in migration 018."""
    from db.schema import COLUMN_REGISTRY

    cols = COLUMN_REGISTRY.get("track_popularity_cache") or {}
    assert "recording_mbid" in cols, (
        "track_popularity_cache.recording_mbid is missing from "
        "COLUMN_REGISTRY — a database whose migration 018 never ran would "
        "keep failing the popularity upsert with UndefinedColumn"
    )
    assert "TEXT" in cols["recording_mbid"].upper()


def test_registry_includes_lastfm_tags_still() -> None:
    """The pre-existing registry entry is preserved."""
    from db.schema import COLUMN_REGISTRY

    cols = COLUMN_REGISTRY.get("track_popularity_cache") or {}
    assert "lastfm_tags" in cols


def test_the_upsert_references_the_column_it_needs() -> None:
    """Guard: the upsert really does depend on the column (why the gap bit).

    If a future change makes the upsert tolerate a missing column, this test
    should be revisited rather than left asserting a stale premise.
    """
    from db.repositories import popularity_cache as repo

    import inspect

    src = inspect.getsource(repo)
    assert "recording_mbid" in src, (
        "the popularity-cache upsert no longer mentions recording_mbid — "
        "revisit whether the registry entry is still required"
    )
