"""Persist popularity-report diagnostics on ``tracks``.

The popularity report mixed raw and album-relative scores, and showed source
counts without the flags that explain them. Two values the scan already
computes were memory-only:

* ``exclude_from_stats`` — why a track with visible source counts can have
  ``final_score`` 0 and a 1★ rating (interludes/bonus tracks).
* ``lastfm_playcount_available`` — whether ``lastfm_playcount`` is a measured
  zero or an unavailable value that must not be read as real zero.

Both are nullable/defaulted so existing rows remain valid; the next popularity
scan fills them.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "030_popularity_report_diagnostics"
down_revision: Union[str, None] = "029_navidrome_metadata_authority"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("tracks"):
        return
    columns = {str(column.get("name")) for column in inspector.get_columns("tracks")}
    if "exclude_from_stats" not in columns:
        op.add_column(
            "tracks",
            sa.Column(
                "exclude_from_stats",
                sa.Boolean(),
                server_default=sa.text("FALSE"),
            ),
        )
    if "lastfm_playcount_available" not in columns:
        op.add_column(
            "tracks",
            sa.Column("lastfm_playcount_available", sa.Boolean()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("tracks"):
        return
    columns = {str(column.get("name")) for column in inspector.get_columns("tracks")}
    if "lastfm_playcount_available" in columns:
        op.drop_column("tracks", "lastfm_playcount_available")
    if "exclude_from_stats" in columns:
        op.drop_column("tracks", "exclude_from_stats")
