"""Record WHY a downloaded file was discarded, so the queue page can show it.

Why a table, and why now
-----------------------
Reported: *"a section at the top for errors that list queue items discarded and
the reasons for being discarded. Such as wrong artist (noting found artist
name), wrong length (noting the length)"*.

The reason a rejected download is invisible today is structural, not cosmetic:

* ``mark_failed(queue_id, reason)`` writes ``download_queue.failure_reason`` and
  puts the row BACK TO ``queued`` — so the next attempt overwrites the reason,
  and the one that matters is gone within minutes;
* ``get_failed_queue()`` reads ``WHERE status = 'failed'``, a status
  ``mark_failed`` never sets, so it never sees these rows at all;
* the search-side rejections in ``download_pipeline_service`` are only COUNTED
  (``_note_reject``) into a per-query dict and logged at WARNING.

So the discard is announced once, on a log line, and then exists nowhere. A page
that reads the live queue therefore CANNOT show it, however it is queried — the
record has to be kept somewhere that outlives the queue row's status changes,
which is what this table is.

Deliberately denormalized
------------------------
``artist`` / ``title`` / ``album`` are copied rather than joined from
``download_queue``. The rejection has to remain readable after the queue row is
deleted, re-queued or re-pointed at a different file — and it is the DISCARDED
item's identity the operator is reading, so a join that could return a
different title later would be worse than the duplication.

``queue_id`` is kept for correlation (and so a future "rejections for this item"
view can group), but it is NOT a foreign key: the queue row may legitimately be
gone by the time anyone looks.

Revision ID: 027_queue_rejections
Revises: 026_tracks_isrc_indexes
Create Date: 2026-10-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "027_queue_rejections"
down_revision: Union[str, None] = "026_tracks_isrc_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "queue_rejections"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("queue_id", sa.Integer()),
            sa.Column("artist", sa.Text()),
            sa.Column("title", sa.Text()),
            sa.Column("album", sa.Text()),
            # A CODE ("wrong_artist", "wrong_length", …), not prose: the UI needs
            # to group and filter on it, and prose cannot be reworded later.
            sa.Column("reason", sa.Text(), nullable=False),
            # The human sentence, which is what gets displayed.
            sa.Column("detail", sa.Text()),
            # The two facts the report names explicitly.
            sa.Column("found_artist", sa.Text()),
            sa.Column("found_length_seconds", sa.Float()),
            sa.Column("expected_length_seconds", sa.Float()),
            sa.Column("found_title", sa.Text()),
            sa.Column("filename", sa.Text()),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp()),
        )

    # The page reads the most recent rows, so that is the index that matters.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS idx_queue_rejections_created_at "
        f"ON {_TABLE} (created_at DESC)"
    )
    # Grouping/counting by reason ("what is failing most?") is the other read.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS idx_queue_rejections_reason ON {_TABLE} (reason)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)
