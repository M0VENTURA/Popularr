"""Per-recording release MBID on ``tracks``.

Why
---
When a track is resolved, MusicBrainz hands back the recording's releases; the
metadata lookup already SELECTS one of them (``_select_primary_release``) for
the release title/year — but it never returned or saved that release's MBID.
On a Various Artists compilation that is not linked to a MusicBrainz release,
each track can belong to a DIFFERENT release, so the album-level
``musicbrainz_album_mbid`` cannot describe it.

The release MBID is the deterministic route cover detection was missing: it can
fetch that release's tracklist and resolve the recording by title/artist inside
it, instead of a broad artist+title search that produced false positives.

Storage
-------
``tracks.recording_release_mbid`` — TEXT, nullable. Nothing reads a value from
it that did not come from the recording lookup; the next metadata scan fills it.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "031_tracks_recording_release_mbid"
down_revision: Union[str, None] = "030_popularity_report_diagnostics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The tracks table is created by the schema BOOTSTRAP (db/schema.py); a
    # missing table means there is nothing to add.
    if not sa.inspect(op.get_bind()).has_table("tracks"):
        return
    bind = op.get_bind()
    existing = {col["name"] for col in sa.inspect(bind).get_columns("tracks")}
    if "recording_release_mbid" not in existing:
        op.add_column(
            "tracks", sa.Column("recording_release_mbid", sa.String(), nullable=True)
        )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("tracks"):
        return
    try:
        op.drop_column("tracks", "recording_release_mbid")
    except Exception:
        # SQLite cannot DROP a column; the chain only needs to be a no-op there.
        pass
