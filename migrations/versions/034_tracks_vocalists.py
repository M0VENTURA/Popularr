"""Add ``tracks.vocalists``.

A confirmed recording-level vocalist list (JSON array) used by the new Essential
Vocalist collection. Populated from MusicBrainz recording performer/vocal
relations, with the recording artist credit as the fallback; a guest vocalist on
another artist's release is included even when the host act is the album artist.

Idempotent: only the column is added.

⚠️ A MISSING TABLE IS A SKIP, NOT AN ADD — see migration 033's note. ``tracks``
is created by the bootstrap schema rather than by this chain, so a database
stamped past its creation revision can still have no table here, and the
``ALTER`` would fail with ``no such table``.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "034_tracks_vocalists"
down_revision: Union[str, None] = "033_missing_album_tracks_is_bonus"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind) -> set[str]:
    inspector = sa.inspect(bind)
    if not inspector.has_table("tracks"):
        return set()
    return {str(column.get("name")) for column in inspector.get_columns("tracks")}


def _table_exists(bind) -> bool:
    return sa.inspect(bind).has_table("tracks")


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind) and "vocalists" not in _columns(bind):
        op.add_column("tracks", sa.Column("vocalists", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind) and "vocalists" in _columns(bind):
        op.drop_column("tracks", "vocalists")
