"""Add ``tracks.vocalists``.

A confirmed recording-level vocalist list (JSON array) used by the new Essential
Vocalist collection. Populated from MusicBrainz recording performer/vocal
relations, with the recording artist credit as the fallback; a guest vocalist on
another artist's release is included even when the host act is the album artist.

Idempotent: only the column is added.
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


def upgrade() -> None:
    bind = op.get_bind()
    if "vocalists" not in _columns(bind):
        op.add_column("tracks", sa.Column("vocalists", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "vocalists" in _columns(bind):
        op.drop_column("tracks", "vocalists")
