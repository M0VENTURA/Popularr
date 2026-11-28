"""Genre labels on ``upcoming_releases``.

Adds two columns so an upcoming row can show what kind of music it is:

* ``musicbrainz_genres`` — the provider's genre/tag names, strongest first;
* ``main_genres`` — the classified ROOT ids (``metal``, ``pop``, ``rock`` …)
  that ``static/css/upcoming-genres.css`` colours by.

Both are derived from the release group, not stored from the source scrape, so
a row that was matched before this migration simply gets them on its next match
(or on the next refresh pass). No backfill here: it would need a MusicBrainz
call per row, and this chain runs inside the container's startup.

Idempotent and dialect-portable (inspector-guarded ``add_column``, the house
pattern from 014/016/017/018/021/022/036) because the chain also runs against
the SQLite test engine. The runtime bootstrap adds the same two columns from
``db/schema.py``.

⚠️ The table check comes BEFORE the column check. An absent table has no
columns, so ``name not in existing`` is true for a table that does not exist and
the ``ALTER`` would fail with ``no such table`` — see migration 033's note.

Revision ID: 039_upcoming_release_genres
Revises: 038_tracks_artist_trim_idx
Create Date: 2026-11-28
"""

from __future__ import annotations

from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "039_upcoming_release_genres"
down_revision: Union[str, None] = "038_tracks_artist_trim_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "upcoming_releases"

#: column name -> SQLAlchemy type. JSONB on PostgreSQL, which degrades to TEXT
#: on SQLite; both hold the JSON-encoded list the reader parses.
_COLUMNS: dict[str, Any] = {
    "musicbrainz_genres": sa.JSON(),
    "main_genres": sa.JSON(),
}


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return set()
    return {col["name"] for col in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    existing = _existing_columns()
    for name, type_ in _COLUMNS.items():
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    existing = _existing_columns()
    for name in _COLUMNS:
        if name not in existing:
            continue
        try:
            op.drop_column(_TABLE, name)
        except Exception:
            # SQLite cannot ALTER/DROP a column; the chain only needs to be a
            # no-op there.
            pass
