"""Persist a per-album Navidrome summary/fingerprint for fast unchanged skips.

The import can then answer, before fetching a tracklist:
  known album?
  summary + scan marker unchanged since the last successful import?
  -> fast skip

Anything ambiguous still fetches the tracklist and falls back to the exact
track-ID + metadata-fingerprint comparison.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "035_navidrome_album_state"
down_revision: Union[str, None] = "034_tracks_vocalists"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("navidrome_album_state"):
        return
    op.create_table(
        "navidrome_album_state",
        sa.Column("album_id", sa.Text(), primary_key=True),
        sa.Column("album_key", sa.Text()),
        sa.Column("album_name", sa.Text()),
        sa.Column("album_artist", sa.Text()),
        sa.Column("song_count", sa.Integer()),
        sa.Column("summary_hash", sa.Text()),
        sa.Column("track_ids_hash", sa.Text()),
        sa.Column("metadata_fingerprint", sa.Text()),
        sa.Column("last_scan_marker", sa.Text()),
        sa.Column(
            "last_imported_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_navidrome_album_state_key",
        "navidrome_album_state",
        ["album_key"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("navidrome_album_state"):
        op.drop_table("navidrome_album_state")
