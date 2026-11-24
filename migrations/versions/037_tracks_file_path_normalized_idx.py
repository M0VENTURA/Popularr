"""A functional index migration: normalise file_path once, not per file.

Revision ID: 037_tracks_file_path_normalized_idx
Revises: 036_download_queue_progress_marker
Create Date: 2026-11-24

Why
---
``mark_local_tags_written_for_path`` runs once per FILE after a tag write:

    UPDATE tracks SET ... WHERE LOWER(REPLACE(COALESCE(file_path, ''), '\\', '/')) IN (...)

A function on the indexed column makes the predicate non-sargable, so
``idx_tracks_file_path`` cannot be used and EVERY tag write does a sequential
scan of the whole ``tracks`` table. On a 70k-row library that is a real per-file
cost, and the genre resync / album save fan-out do it once per file.

An expression index on the SAME expression lets PostgreSQL use an index for
exactly that predicate. It is dialect-guarded because the SQLite test engine
cannot create one and does not need it.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "037_tracks_file_path_normalized_idx"
down_revision: Union[str, None] = "036_download_queue_progress_marker"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "idx_tracks_file_path_normalized"
_EXPR = "LOWER(REPLACE(COALESCE(file_path, ''), '\\', '/'))"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("tracks"):
        return
    if bind.dialect.name != "postgresql":
        # SQLite (the test engine) cannot build an expression index reliably and
        # does not have the row counts that make one matter.
        return
    op.execute(f"CREATE INDEX IF NOT EXISTS {_INDEX} ON tracks ({_EXPR})")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
