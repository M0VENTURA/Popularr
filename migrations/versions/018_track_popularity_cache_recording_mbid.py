"""Persist the album-tracklist recording identity on ``track_popularity_cache``.

Why
---
``scan_stage_runner`` resolves each album's recording MBIDs ONCE per album
per pass via ``get_listenbrainz_album_tracklist_with_release`` (a
ListenBrainz metadata lookup), and the result feeds two consumers:

* the per-album MusicBrainz recording batch
  (``_build_album_recording_batch`` → one bulk MB request per album), and
* track_stage's LB listen lookup (batched BY MBID — a track without one
  falls to the expensive per-track chain: ISRC resolution → MB recording
  SEARCH → single LB lookup).

The identity map was already persisted per ``(artist, title)`` as
``source = 'album_tracklist'`` rows — but WITHOUT the MBID, because the
column did not exist. Every pass therefore re-ran the ListenBrainz tracklist
lookup for every album whose rows were seeded from the database (the
runner's guard only skips the lookup when the seeded entry carries
``source == 'album_tracklist'`` AND a ``recording_mbid``).

The column closes the loop: the identity resolved on pass N is read back on
pass N+1 from the cache, zero ListenBrainz calls, and the mbid also flows
into the batch/LB paths that depend on it.

Rows written before this migration have ``recording_mbid IS NULL``; the
runner re-resolves those albums once and fills the column.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "018_track_popularity_cache_recording_mbid"
down_revision: Union[str, None] = "017_add_download_queue_release_group"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns() -> set[str]:
    """Inspector-guarded and dialect-portable (``ADD COLUMN IF NOT EXISTS``
    is PostgreSQL-only and the chain also runs on the SQLite test engine —
    the same pattern ``014``/``016`` use)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns("track_popularity_cache")}


def upgrade() -> None:
    # The table itself is created by the schema BOOTSTRAP (db/schema.py), not
    # by this chain — on a fresh database the bootstrap adds it WITH the
    # column, so a missing table means there is nothing to add.
    if not sa.inspect(op.get_bind()).has_table("track_popularity_cache"):
        return
    if "recording_mbid" not in _existing_columns():
        op.add_column(
            "track_popularity_cache",
            sa.Column("recording_mbid", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("track_popularity_cache"):
        return
    if "recording_mbid" in _existing_columns():
        op.drop_column("track_popularity_cache", "recording_mbid")
