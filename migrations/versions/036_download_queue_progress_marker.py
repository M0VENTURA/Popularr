"""A real "no bytes moved" clock for a download, so a frozen transfer can be reaped.

Why the existing timeouts could not see this
--------------------------------------------
Reported: a track was handed the same file from one peer four times over about
seven hours, and nine queue items sat in ``downloading`` for three days across
many worker restarts.

Every existing guard measures the WRONG clock:

* ``slskd_reaper_service.reap_stalled_transfers`` compares ``progress`` against
  the transfer's ``startedAt``. A transfer that trickles for 59 minutes and then
  dies is not "60 minutes old" until the 60-minute mark, and the
  ``progress <= 0`` arm never fires because progress is non-zero.
* ``download_completion_service._reconcile_transfer_state``,
  ``cleanup_stuck_items`` and ``_STALE_DOWNLOADING_MINUTES`` all key off
  ``download_queue.updated_at`` — and ``_sync_download_progress`` writes that
  column on every pass, so a live-but-frozen transfer looks alive forever.

``updated_at`` is a "the row was touched" clock, not a "bytes moved" clock. This
migration adds the second one:

* ``slskd_bytes_seen`` — the last observed ``bytesTransferred``;
* ``slskd_progress_at`` — when that value last CHANGED.

A writer that polls with an unchanged byte count leaves both alone, so the marker
ages and a 20-minute no-progress timeout becomes expressible. In-memory state was
rejected: the stuck items outlived many restarts, and a restart would have reset
the clock and given every frozen transfer a fresh window.

Idempotent and dialect-portable (inspector-guarded ``add_column``, the house
pattern from 014/016/017/018/021/022) because the chain also runs against the
SQLite test engine. The runtime bootstrap adds the same two columns from
``db/schema.py`` on a fresh install.

Revision ID: 036_download_queue_progress_marker
Revises: 035_navidrome_album_state
Create Date: 2026-11-23
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "036_download_queue_progress_marker"
down_revision: Union[str, None] = "035_navidrome_album_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "download_queue"

#: column name -> SQLAlchemy type. ``BIGINT`` because ``bytesTransferred`` is a
#: byte count and a large FLAC release can exceed 32 bits.
_COLUMNS: dict[str, Any] = {
    "slskd_bytes_seen": sa.BigInteger(),
    "slskd_progress_at": sa.TIMESTAMP(),
}


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    # The table is created by the schema BOOTSTRAP (db/schema.py), not by this
    # chain — on a fresh database it is created WITH these columns, so a missing
    # table means there is nothing to add.
    if not sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    existing = _existing_columns()
    for name, type_ in _COLUMNS.items():
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    for name in _COLUMNS:
        try:
            op.drop_column(_TABLE, name)
        except Exception:
            # SQLite cannot ALTER/DROP a column; the chain only needs to be a
            # no-op there.
            pass
