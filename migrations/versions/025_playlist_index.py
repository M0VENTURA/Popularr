"""Playlist index: ``playlists`` + ``playlist_entries``.

Why
---
The artist page gained an "Included in Playlists" section. Answering "which
playlists contain this artist's tracks?" needs a playlist -> track mapping, and
**no such index existed anywhere in the app**: playlists were never in Postgres
at all, only in Navidrome (Subsonic) and in local ``.nsp``/``.m3u`` files.

Reading them live on every artist page load would cost one Subsonic request per
playlist, which is unacceptable on page load. So the mapping is recorded here
during the library/playlist sync instead, and the artist page becomes a plain
indexed join against tables it already has.

Two tables, deliberately
------------------------
A single denormalized ``playlist_entries(name, owner, changed, …, track_id)``
table looks tidier but puts each playlist's ``changed`` stamp on every one of
its rows. The sync's whole fast path is "skip the playlists whose stamp has not
moved", so that stamp is READ to refresh ONE playlist — meaning 500 row
comparisons for a 500-track playlist, and a stamp that has to be rewritten
every time even when nothing changed. Splitting the playlist row out makes the
fast path one indexed lookup and an unchanged playlist a zero-write no-op.

Keys
----
* ``playlists.playlist_id`` — the Subsonic id, or ``file::<file_name>`` for a
  file-backed playlist. The same scheme ``routes/playlists.py`` already uses, so
  the two surfaces agree about what identifies a playlist.
* ``playlists.changed`` — Navidrome's own modification stamp for Subsonic
  playlists, and a content digest for file-backed ones (which have no stamp of
  their own).
* ``playlist_entries.(playlist_id, position)`` — the PRIMARY KEY, not
  ``(playlist_id, track_id)``: a playlist may legitimately contain the same
  track twice, and collapsing those would silently drop a row on every sync.
* ``playlist_entries.track_id`` — the Navidrome song id, which is what
  ``tracks.id`` already holds (``services/scanning/payload_builder.py`` stores
  the Subsonic id verbatim). The artist-page join therefore needs no mapping
  table and no name matching.

The tables are also declared in ``db/schema.py`` so the bootstrap creates them
on a fresh install; this migration is guarded rather than authoritative.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "025_playlist_index"
down_revision: Union[str, None] = "024_artist_aliases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("playlists"):
        op.create_table(
            "playlists",
            sa.Column("playlist_id", sa.Text(), primary_key=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("type", sa.Text()),
            sa.Column("owner", sa.Text()),
            sa.Column("is_public", sa.Boolean()),
            sa.Column("song_count", sa.Integer(), server_default="0"),
            sa.Column("comment", sa.Text()),
            sa.Column("changed", sa.Text()),
            sa.Column("synced_at", sa.DateTime(), server_default=sa.func.current_timestamp()),
        )

    if not inspector.has_table("playlist_entries"):
        op.create_table(
            "playlist_entries",
            sa.Column("playlist_id", sa.Text(), nullable=False),
            sa.Column("track_id", sa.Text(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.PrimaryKeyConstraint("playlist_id", "position"),
        )

    # Both are created after the tables on purpose: `inspector.has_table` above
    # may have run against a bind where neither existed yet, and `IF NOT EXISTS`
    # makes these safe to run on every upgrade regardless.
    op.execute("CREATE INDEX IF NOT EXISTS idx_playlist_entries_track_id ON playlist_entries (track_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_playlist_entries_playlist_id ON playlist_entries (playlist_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_playlists_name ON playlists (LOWER(name))")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("playlist_entries"):
        op.drop_table("playlist_entries")
    if inspector.has_table("playlists"):
        op.drop_table("playlists")
