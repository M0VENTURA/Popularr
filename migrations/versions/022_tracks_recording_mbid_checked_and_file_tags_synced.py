"""Durable recording-MBID search outcome and file-tag sync stamps on tracks.

Why
---
A re-scan of a recently-scanned artist (a compilation with several hundred
tracks in the reported log) re-runs the ENTIRE per-album pipeline because two
search outcomes are forgotten between passes:

1. **A recording-MusicBrainz search that found NOTHING.** The in-memory
   ``_mbid_cache`` negative entry lives in one process only — the queue
   worker and the web worker are separate processes, so a ``candidate_count=0``
   search is repeated on every scan. Worse, ``is_album_incomplete`` treats a
   missing ``recording_mbid`` as "a re-run could fill it", so the album is
   FORCED through the whole pipeline (enrich + track phase + tag sync) even
   though the search would return the same nothing.

2. **File tags that are already in sync.** ``sync_album_file_tags`` re-reads
   every audio file with mutagen on every scan and re-records its corrections,
   even when nothing in the DB changed for the album. A durable
   ``file_tags_synced_at`` stamp lets a later pass skip the reads entirely.

Storage
-------
Two TIMESTAMP columns on ``tracks``:

* ``recording_mbid_checked_at`` — stamped in ``track_stage`` whenever a
  recording search COMPLETED and resolved to no MBID. ``is_album_incomplete``
  treats a fresh stamp as terminal: re-running produces the same nothing.
* ``file_tags_synced_at`` — stamped for a track whenever
  ``sync_album_file_tags`` finished reconciling its file tags against the DB.

Both are TIMESTAMP (matching the other freshness columns, e.g.
``lastfm_last_updated``) so a "checked within N days" query is a timestamp
comparison.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "022_tracks_recording_mbid_checked_and_file_tags_synced"
down_revision: Union[str, None] = "021_album_lookup_skip"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The tracks table is created by the schema BOOTSTRAP (db/schema.py), not
    # by this chain — a missing table means there is nothing to add.
    if not sa.inspect(op.get_bind()).has_table("tracks"):
        return
    bind = op.get_bind()
    existing = {col["name"] for col in sa.inspect(bind).get_columns("tracks")}
    for col in (
        "recording_mbid_checked_at",
        "file_tags_synced_at",
    ):
        if col not in existing:
            # Inspector-guarded + dialect-portable: ``ADD COLUMN IF NOT
            # EXISTS`` is PostgreSQL-only and the chain also runs on the
            # SQLite test engine (the house pattern — 014/016/018/020/021).
            op.add_column("tracks", sa.Column(col, sa.TIMESTAMP(), nullable=True))


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("tracks"):
        return
    for col in (
        "recording_mbid_checked_at",
        "file_tags_synced_at",
    ):
        try:
            op.drop_column("tracks", col)
        except Exception:
            # SQLite cannot ALTER/DROP a column; the chain only needs to be a
            # no-op there.
            pass