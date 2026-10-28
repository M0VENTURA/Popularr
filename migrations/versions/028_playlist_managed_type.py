"""Add an explicit ownership type to the playlist index.

Why
---
Genre playlists and decade playlists share the ``* - Top Tracks`` name shape.
The genre orphan cleaner must only delete playlists it can POSITIVELY identify
as genre playlists; name matching alone let it delete ``1980s - Top Tracks``
and the next global rebuild re-created it.

``playlists.managed_type`` records the builder that owns each playlist
(``genre``, ``decade``, ``new_music``, ``essential``, ``favourites``,
``curated``, ``mood`` or NULL when unknown). Nullable on purpose: an unknown
owner is a reason to leave a playlist alone, never a reason to claim it.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "028_playlist_managed_type"
down_revision: Union[str, None] = "027_queue_rejections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("playlists"):
        return
    columns = {str(column.get("name")) for column in inspector.get_columns("playlists")}
    if "managed_type" not in columns:
        op.add_column("playlists", sa.Column("managed_type", sa.Text()))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("playlists"):
        return
    columns = {str(column.get("name")) for column in inspector.get_columns("playlists")}
    if "managed_type" in columns:
        op.drop_column("playlists", "managed_type")
