"""Add ``discogs_album_id`` to the tracks table.

Discogs release identifiers were being written by the album-edit flow and the
Discogs enrichment service, but the column itself was never created.  Because
the persistence layer silently drops keys that have no matching column, the
value vanished without an error and the album page always rendered an empty
Discogs ID field.

Every DDL statement is existence-guarded and dialect-portable, so the chain
also runs cleanly against the SQLite test engine.

Revision ID: 013_add_tracks_discogs_album_id
Revises: 012_add_tracks_mb_composer_lyricist_iswc
Create Date: 2026-09-01
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "013_add_tracks_discogs_album_id"
down_revision: Union[str, None] = "012_add_tracks_mb_composer_lyricist_iswc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns() -> set[str]:
    """Return the current column set of the ``tracks`` table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns("tracks")}


def upgrade() -> None:
    existing = _existing_columns()

    if "discogs_album_id" not in existing:
        op.add_column("tracks", sa.Column("discogs_album_id", sa.Text(), nullable=True))


def downgrade() -> None:
    existing = _existing_columns()

    if "discogs_album_id" in existing:
        op.drop_column("tracks", "discogs_album_id")
