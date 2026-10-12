"""Per-album "do not search the album release" flag.

Why
---
A custom album — a bootleg, a local tape rip, a self-made compilation that is
not on MusicBrainz or Discogs — has no release to look up. The album page's
Lookup MBID / Compare and the scan's album-stage enrichment (album type,
Discogs release id, album art from CAA/Discogs/AudioDB, and the per-album
missing-track refresh against a MusicBrainz release) all search the release
for it, find nothing (or the WRONG release), and the scan re-runs the same
empty lookups on every pass.

The album save now writes ``album_lookup_skip`` = '1' to every track of the
album when the user checks "Do not search album release" on the Edit Album
tab (and the CSV tracklist upload sets it too — the CSV tracklist stands in
for the MusicBrainz release as the source of the missing-track list). The
scan gates those lookups off for flagged albums; per-track recording
(recording MBID / genre / scan) is untouched — this only stops the ALBUM
release lookup, which is the field's name.

Storage: a TEXT column on ``tracks`` ('' = search allowed, '1' = skip),
written ALBUM-WIDE to every track, matching the album→tracks fan-out rule.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "021_album_lookup_skip"
down_revision: Union[str, None] = "020_track_popularity_cache_release_track_title"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The tracks table is created by the schema BOOTSTRAP (db/schema.py), not
    # by this chain — a missing table means there is nothing to add.
    if not sa.inspect(op.get_bind()).has_table("tracks"):
        return
    bind = op.get_bind()
    existing = {col["name"] for col in sa.inspect(bind).get_columns("tracks")}
    if "album_lookup_skip" not in existing:
        # Inspector-guarded + dialect-portable: ``ADD COLUMN IF NOT EXISTS``
        # is PostgreSQL-only and the chain also runs on the SQLite test
        # engine (the house pattern — 014/016/018/020).
        op.add_column(
            "tracks",
            sa.Column("album_lookup_skip", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("tracks"):
        return
    try:
        op.drop_column("tracks", "album_lookup_skip")
    except Exception:
        # SQLite cannot ALTER/DROP a column; the chain only needs to be a
        # no-op there.
        pass