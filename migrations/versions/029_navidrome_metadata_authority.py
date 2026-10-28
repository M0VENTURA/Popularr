"""Local metadata authority, Navidrome import provenance and playlist ID rejections.

Why
---
Popularr can correct the DB and the physical file tags, but a later Navidrome
import used to write Navidrome's older indexed view back over the correction.
There was no durable record that a local correction was newer than the last
verified import, and no place to remember a song ID Navidrome rejected from a
playlist.

This migration adds the smallest state needed:

* ``tracks.local_tags_written_at`` — when Popularr last wrote this row's file tags.
* ``tracks.metadata_dirty_for_navidrome`` — a durable "Navidrome must re-read
  this file before its metadata may overwrite ours" flag.
* ``tracks.navidrome_imported_at`` — last VERIFIED import that observed the
  expected values (never advanced on a deferred/stale import).
* ``tracks.metadata_source`` — diagnostic provenance.
* ``playlist_song_rejections`` — the song IDs a specific Navidrome server
  rejected, so a playlist is not rewritten with the same invalid ID forever.

Nullable/defaulted on purpose: existing rows remain valid and are treated as
"no local correction recorded yet".
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "029_navidrome_metadata_authority"
down_revision: Union[str, None] = "028_playlist_managed_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("tracks"):
        columns = {str(column.get("name")) for column in inspector.get_columns("tracks")}
        additions = (
            ("local_tags_written_at", sa.DateTime()),
            (
                "metadata_dirty_for_navidrome",
                sa.Boolean(),
            ),
            ("navidrome_imported_at", sa.DateTime()),
            ("metadata_source", sa.Text()),
        )
        for name, column_type in additions:
            if name not in columns:
                op.add_column(
                    "tracks",
                    sa.Column(
                        name,
                        column_type,
                        server_default=(
                            sa.text("FALSE")
                            if name == "metadata_dirty_for_navidrome"
                            else None
                        ),
                    ),
                )

    if not inspector.has_table("playlist_song_rejections"):
        op.create_table(
            "playlist_song_rejections",
            sa.Column("server_key", sa.Text(), nullable=False),
            sa.Column("playlist_name", sa.Text(), nullable=False),
            sa.Column("song_id", sa.Text(), nullable=False),
            sa.Column("track_hint", sa.Text()),
            sa.Column("first_seen", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_seen", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("server_key", "playlist_name", "song_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("playlist_song_rejections"):
        op.drop_table("playlist_song_rejections")
    if inspector.has_table("tracks"):
        columns = {str(column.get("name")) for column in inspector.get_columns("tracks")}
        for name in (
            "metadata_source",
            "navidrome_imported_at",
            "metadata_dirty_for_navidrome",
            "local_tags_written_at",
        ):
            if name in columns:
                op.drop_column("tracks", name)
