"""Upcoming releases: include primary_type in the dedup key.

The table was keyed on (artist_name, album_name) alone, so the ubiquitous
"album named after its lead single" pattern collapsed a same-titled SINGLE
and ALBUM into one row: MusicBrainz reports both (e.g. Clutch: single
"Wasted Lands" 2026-09-25 and album "Wasted Lands" 2027-01-22), the first
write INSERTed, the second UPDATEd the same row and the earliest-date rule
discarded the album's future date — the upcoming album vanished.

The identity becomes (artist_name, album_name, primary_type) with
primary_type normalised to '' when absent (Wikipedia rows carry no type,
and remain absorbable by a later typed MusicBrainz write).

On a fresh database the table is created by the schema bootstrap with the
new key already in place, so — like 016/018 — this upgrade skips when the
table is absent.

Revision ID: 019_upcoming_releases_type_in_key
Revises: 018_track_popularity_cache_recording_mbid
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "019_upcoming_releases_type_in_key"
down_revision = "018_track_popularity_cache_recording_mbid"
branch_labels = None
depends_on = None

TABLE = "upcoming_releases"
# The old key has existed both as a table constraint and (via the schema
# bootstrap) as a bare unique index under this name.
OLD_KEY = "uq_upcoming_artist_album"
NEW_KEY = "uq_upcoming_artist_album_type"


def _has_unique_key(bind: sa.Connection, name: str) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return False
    names = {c["name"] for c in inspector.get_unique_constraints(TABLE)}
    names |= {i["name"] for i in inspector.get_indexes(TABLE) if i.get("unique")}
    return name in names


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        # Fresh chain-run database: bootstrap creates the table with the
        # new key directly. Nothing to upgrade.
        return

    # Normalise NULL types to '' so the column can be NOT NULL.
    op.execute(sa.text(f"UPDATE {TABLE} SET primary_type = '' WHERE primary_type IS NULL"))

    # Drop the old two-column key — as a constraint and/or as the bare
    # unique index the bootstrap used to create.
    try:
        op.drop_constraint(OLD_KEY, TABLE, type_="unique")
    except Exception:
        pass
    op.execute(sa.text(f"DROP INDEX IF EXISTS {OLD_KEY}"))

    if bind.dialect.name == "postgresql":
        op.execute(sa.text(f"ALTER TABLE {TABLE} ALTER COLUMN primary_type SET NOT NULL"))

    if not _has_unique_key(bind, NEW_KEY):
        op.execute(sa.text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {NEW_KEY} "
            f"ON {TABLE} (artist_name, album_name, primary_type)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return

    op.execute(sa.text(f"DROP INDEX IF EXISTS {NEW_KEY}"))

    # Collapse same-titled rows back to one per (artist, album): keep the
    # earliest row and drop the rest, then restore NULL types.
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(f"""
            DELETE FROM {TABLE} a USING {TABLE} b
            WHERE a.artist_name = b.artist_name
              AND a.album_name = b.album_name
              AND a.id > b.id
        """))
        op.execute(sa.text(f"ALTER TABLE {TABLE} ALTER COLUMN primary_type DROP NOT NULL"))
    op.execute(sa.text(f"UPDATE {TABLE} SET primary_type = NULL"))

    if not _has_unique_key(bind, OLD_KEY):
        op.execute(sa.text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {OLD_KEY} "
            f"ON {TABLE} (artist_name, album_name)"
        ))
