"""Drop ``tracks.recording_release_mbid``.

031 added a dedicated column for the release the per-track MusicBrainz lookup
resolved. That was redundant: the track page already stores a per-track release
in **MusicBrainz Album ID** (``musicbrainz_albumid``), and the scan now writes
the lookup's release there on an unmatched VA compilation. Keeping a second
column meant two sources could disagree and cover detection had to know which
was authoritative.

Idempotent, like the other migrations: a database that never ran 031 simply sees
no column to drop.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "032_drop_tracks_recording_release_mbid"
down_revision: Union[str, None] = "031_tracks_recording_release_mbid"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _track_columns(bind) -> set[str]:
    inspector = sa.inspect(bind)
    if not inspector.has_table("tracks"):
        return set()
    return {str(column.get("name")) for column in inspector.get_columns("tracks")}


def upgrade() -> None:
    bind = op.get_bind()
    if "recording_release_mbid" in _track_columns(bind):
        op.drop_column("tracks", "recording_release_mbid")


def downgrade() -> None:
    bind = op.get_bind()
    if "recording_release_mbid" not in _track_columns(bind):
        op.add_column(
            "tracks",
            sa.Column("recording_release_mbid", sa.String()),
        )
