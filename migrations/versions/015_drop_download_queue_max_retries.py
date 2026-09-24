"""Drop the dead ``download_queue.max_retries`` column.

``max_retries`` was never enforced. ``mark_failed`` read only the retry DELAY
(``_queue_retry_defaults()[0]``) and ignored the ceiling entirely, so an item
was retried forever regardless of this column's value. Its three other
surfaces were equally dead:

* ``retry_delay_minutes`` is still written/read, but ``max_retries`` was never
  written by anything — only read.
* ``GET /api/musicbrainz/downloads`` (the endpoint behind the download table)
  never returned it, and neither did any other route, so the JS
  ``(Retry N/5)`` badge always fell back to its own client-side constant.
* ``queue.max_retries`` in ``config.yaml`` had no default and no Config-page
  field, so the config lookup could only ever return the hard-coded 5.

Retries are deliberately UNBOUNDED: a track that fails to download must return
to the queue rather than being abandoned, which is also why ``mark_failed``
never writes the terminal ``'failed'`` status. Removing the column makes that
policy explicit instead of advertising a limit that does not exist.

The guard is deliberate and matches the rest of the chain: every DDL statement
is existence-checked and dialect-portable, so the chain still runs cleanly
against the SQLite test engine (which creates ``download_queue`` without this
column).

Revision ID: 015_drop_download_queue_max_retries
Revises: 014_add_tracks_release_detail
Create Date: 2026-09-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "015_drop_download_queue_max_retries"
down_revision: Union[str, None] = "014_add_tracks_release_detail"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "download_queue"
_COLUMN = "max_retries"


def _has_column() -> bool:
    """True when ``download_queue.max_retries`` currently exists."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    except Exception:
        # Table absent (e.g. a bare SQLite test DB) — nothing to drop.
        return False
    return _COLUMN in columns


def upgrade() -> None:
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    if not _has_column():
        # Recreated with the same shape the initial schema used, so the
        # downgrade restores the previous state exactly.
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Integer(), server_default=sa.text("5"), nullable=True),
        )
