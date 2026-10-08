"""Add ``download_queue.musicbrainz_releasegroupid``.

Why a column is needed
----------------------
The album-scoped MusicBrainz identity was persisted only inside the queue
row's ``metadata`` JSONB (``metadata.album_metadata``), and read back by
``download_completion_service._resolve_album_level_metadata`` as its
**second** source — after the row's own columns.

That made the release group hostage to a blob:

* every queue path that does not build ``album_metadata`` (the single-row
  ``queue_add`` fallback, discovered/local rows, a re-queue that only knows
  artist/title/album) leaves the key absent entirely;
* the field is not a column, so it could not be read by a plain
  ``SELECT ... musicbrainz_releasegroupid`` nor written by
  ``update_queue_item`` without first parsing JSON;
* and the artist page has keyed albums on ``musicbrainz_releasegroupid``
  since 2026-10-06-albums-split-by-release-group, so a row without it imports
  as its OWN album next to the release it was downloaded for.

As a real column it is populated at queue time alongside ``release_mbid`` /
``recording_mbid``, survives any fallback that skips ``metadata``, and is the
FIRST source the import consults.  Rows queued before this migration keep
working: their value still sits in the JSON blob (source 1b) or is inherited
from a library sibling / refreshed from MusicBrainz, and the next queueing of
the same release writes the column.

The statement is existence-checked and dialect-portable, so it is safe on an
install whose runtime bootstrap (``db/bootstrap.py`` ``_ensure_columns``) has
already added the column from ``db/schema.py``, and the chain still runs
cleanly against the SQLite test engine.

Revision ID: 017_add_download_queue_release_group
Revises: 016_add_tracks_raw_score
Create Date: 2026-10-09
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "017_add_download_queue_release_group"
down_revision: Union[str, None] = "016_add_tracks_raw_score"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMN = "musicbrainz_releasegroupid"
_TABLE = "download_queue"


def _existing_columns() -> set[str]:
    """Return the current column set of ``download_queue``.

    Inspector-guarded and dialect-portable — ``ADD COLUMN IF NOT EXISTS`` is
    PostgreSQL-only and the migration chain also runs against the SQLite test
    engine (the same pattern ``014_add_tracks_release_detail`` and
    ``016_add_tracks_raw_score`` use).
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    if _COLUMN not in _existing_columns():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    if _COLUMN in _existing_columns():
        op.drop_column(_TABLE, _COLUMN)
