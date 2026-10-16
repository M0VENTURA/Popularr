"""Index the two columns the recording-identity popularity reuse joins on.

Why
---
``popularity_cache_service.get_recording_identity_popularity`` answers a track's
popularity from ANOTHER row of the same recording, so a duplicate ISRC on a
second album (or a compilation copy credited to "Various Artists") does not pay
for a second Last.fm + ListenBrainz lookup. An ISRC identifies a performance, so
the ListenBrainz count — held against the recording MBID — and the Last.fm count
are the same number for every copy.

That lookup joins on ``tracks.isrc`` and ``tracks.recording_mbid``, and **neither
column had an index**. Without one it is a sequential scan of ``tracks`` on every
album — trading a network call for a full table scan, which on a large library is
the worse deal of the two.

Guarded per column, not just per table
--------------------------------------
⚠️ ``CREATE INDEX ... ON tracks (isrc)`` raises ``no such column`` when the column
is absent, and the migration chain is run against PARTIAL harnesses that carry a
bare ``tracks (id)`` or ``tracks (id, artist, album, title)``. 017's docstring
records the same class of defect for ``op.add_column``: guarding the TABLE alone
left the statement firing on a table that had the shape but not the column.

So each index is created only when its COLUMN is present. On a real install both
are (the bootstrap in ``db/schema.py`` declares them), and both statements are
``IF NOT EXISTS`` — the same belt-and-braces the other index migrations use.

Deliberately NOT an index on the expression
-------------------------------------------
``isrc`` is stored as a bare code (``USRC17607839``) by any row a scan has
touched — the track stage normalises the bracketed list form and writes the code
back — but a row that has never been scanned may still hold ``["USRC17607839"]``.
The lookup therefore carries a ``LIKE '%code%'`` arm as well as equality. A
functional index cannot help that substring arm (a leading wildcard never uses a
btree), so it is not attempted: the equality arm plus this index carry the common
case, and the LIKE arm exists for correctness on unscanned rows.

Revision ID: 026_tracks_isrc_indexes
Revises: 025_playlist_index
Create Date: 2026-10-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "026_tracks_isrc_indexes"
down_revision: Union[str, None] = "025_playlist_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "tracks"

#: (index name, column). Both are indexed because either identity may be the one
#: a given pair of rows shares: a scanned row has both, an unscanned one may
#: hold only the ISRC.
_INDEXES: tuple[tuple[str, str], ...] = (
    ("idx_tracks_isrc", "isrc"),
    ("idx_tracks_recording_mbid", "recording_mbid"),
)


def _existing_columns() -> set[str]:
    """Current column set of ``tracks``, or empty when the table is absent.

    Inspector-guarded and dialect-portable — the same pattern 014/016/017/018/020
    use, because the chain also runs against the SQLite test engine.
    """
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    columns = _existing_columns()
    # An empty set means the table is missing (or has no columns at all): the
    # schema bootstrap has not run, so there is nothing to index and
    # ``CREATE INDEX`` would raise ``no such table``.
    if not columns:
        return
    for index_name, column in _INDEXES:
        if column in columns:
            op.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {_TABLE} ({column})")


def downgrade() -> None:
    columns = _existing_columns()
    if not columns:
        return
    for index_name, column in _INDEXES:
        # Dropping an index whose column is gone would fail on the same harnesses
        # the upgrade guard exists for.
        if column in columns:
            op.execute(f"DROP INDEX IF EXISTS {index_name}")
