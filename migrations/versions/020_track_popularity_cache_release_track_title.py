"""Persist the RELEASE track title beside the album-tracklist identity.

Why
---
``scan_stage_runner`` skips the per-album ListenBrainz tracklist lookup when
the cached ``track_popularity_cache`` row already carries the recording
identity (``source = 'album_tracklist'`` + ``recording_mbid``). That trust is
only safe when the resolved release was the SAME album: a different EDITION
of a compilation numbers its tracks differently, and a position alias from
the wrong edition is a fabrication (reported: an 80-track "Alternative 90s"
folder carrying a stored release MBID for the 46-track single disc aliased
local#10 "Where Is My Mind?" to release#10 "Happy" and scored Ned's Atomic
Dustbin's ListenBrainz count as the Pixies track).

The source gate in ``popularity_sources`` (``_position_title_corroborates``)
now refuses to CREATE such aliases, and this column lets the runner REFUSE an
already-cached identity when the persisted release title no longer
corroborates the local title — so rows poisoned before the fix are re-resolved
once (cheap: the release MBID is read from the tracks table) and then either
re-persisted with a valid title or dropped.

Rows written before this migration have ``release_track_title IS NULL``; the
runner treats those as uncorroborated, re-resolves once, and fills the column.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "020_track_popularity_cache_release_track_title"
down_revision: Union[str, None] = "019_upcoming_releases_type_in_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns() -> set[str]:
    """Inspector-guarded and dialect-portable (``ADD COLUMN IF NOT EXISTS``
    is PostgreSQL-only and the chain also runs on the SQLite test engine —
    the same pattern ``014``/``016``/``018`` use)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns("track_popularity_cache")}


def upgrade() -> None:
    # The table itself is created by the schema BOOTSTRAP (db/schema.py), not
    # by this chain — on a fresh database the bootstrap adds it WITH the
    # column, so a missing table means there is nothing to add.
    if not sa.inspect(op.get_bind()).has_table("track_popularity_cache"):
        return
    if "release_track_title" not in _existing_columns():
        op.add_column(
            "track_popularity_cache",
            sa.Column("release_track_title", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("track_popularity_cache"):
        return
    if "release_track_title" in _existing_columns():
        op.drop_column("track_popularity_cache", "release_track_title")