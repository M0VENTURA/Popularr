"""Index ``LOWER(TRIM(artist))`` so the Essential Collection query can be served.

Revision ID: 038_tracks_artist_trim_idx
Revises: 037_tracks_file_path_normalized_idx
Create Date: 2026-11-27

Why
---
``_sync_essential_playlist`` (``services/popularity/stages/finalise_stage.py``)
runs once per ARTIST, i.e. ~one thousand times on a large library. Its row
query filters 4★+ tracks with a four-arm OR:

    WHERE COALESCE(stars, star_rating) >= 4
      AND (
          LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist))) = LOWER(TRIM(:artist))
       OR LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist))) LIKE :prefix
       OR LOWER(TRIM(artist)) = LOWER(TRIM(:artist))
       OR LOWER(TRIM(artist)) LIKE :prefix
      )

Arms 1-2 are already served by ``idx_tracks_album_artist_trim`` (migration
007). Arms 3-4 are NOT: ``idx_tracks_artist`` (migration 001) indexes the bare
``artist`` column, which an expression predicate cannot use.

With one arm unindexable the planner cannot form a complete BitmapOr and falls
back to a sequential scan of the whole ``tracks`` table — once per artist. This
index closes that gap so all four arms can be bitmap-OR'd, which is the
difference between N_artists sequential scans and N_artists index probes.

Dialect-guarded like 037: the SQLite test engine cannot build an expression
index reliably and does not have the row counts that make one matter.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "038_tracks_artist_trim_idx"
down_revision: Union[str, None] = "037_tracks_file_path_normalized_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "idx_tracks_artist_trim"
_EXPR = "LOWER(TRIM(artist))"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("tracks"):
        return
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"CREATE INDEX IF NOT EXISTS {_INDEX} ON tracks ({_EXPR})")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
