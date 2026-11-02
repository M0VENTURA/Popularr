"""Add ``missing_album_tracks.is_bonus``.

A missing MusicBrainz track that sits AFTER the library's last track on a disc
(and tracks whose own title says "bonus") is a bonus/extra cut, not an
incomplete-album correction. The flag is persisted so the artist page can show
a green "Bonus Tracks to Download" label and keep those rows out of the
"needs fixing" filter.

Idempotent: the table is created by migration 009, so only the column is added.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "033_missing_album_tracks_is_bonus"
down_revision: Union[str, None] = "032_drop_tracks_recording_release_mbid"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind) -> set[str]:
    inspector = sa.inspect(bind)
    if not inspector.has_table("missing_album_tracks"):
        return set()
    return {
        str(column.get("name"))
        for column in inspector.get_columns("missing_album_tracks")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "is_bonus" not in _columns(bind):
        op.add_column(
            "missing_album_tracks",
            sa.Column(
                "is_bonus",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("FALSE"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "is_bonus" in _columns(bind):
        op.drop_column("missing_album_tracks", "is_bonus")
