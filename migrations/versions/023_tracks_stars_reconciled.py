"""Two-phase star rating: tracks.stars_reconciled flag.

Why
---
Star ratings were previously assigned only at the ARTIST boundary
(``scan_stage_runner._flush_artist_star_ratings``), because the global 5★
catalogue lock and the popularity-marking cutoffs need the artist's COMPLETE
album set. The consequence: an artist whose scan is interrupted (crash, budget
stop, hard error) leaves every album SCORED but UNRATED — the whole artist has
to be re-run to get its stars.

This column backs the two-phase design:

* the PER-ALBUM pass posts each album's stars as soon as it finishes (durable),
  writing ``stars_reconciled = FALSE`` — provisional, computed with only the
  artist context available at that moment;
* the ARTIST-boundary RECONCILE pass re-rates with the complete catalogue and
  writes ``stars_reconciled = TRUE``.

The skip gate treats ``FALSE`` as "still needs the artist pass", so an
interrupted artist is reconciled on the next scan instead of keeping
provisional ratings forever. Defaults TRUE: rows that predate this column were
already fully reconciled by the old deferred flush, so they must not be forced
through a full re-scan on upgrade.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "023_tracks_stars_reconciled"
down_revision: Union[str, None] = "022_tracks_recording_mbid_checked_and_file_tags_synced"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The tracks table is created by the schema BOOTSTRAP (db/schema.py), not
    # by this chain — a missing table means there is nothing to add.
    if not sa.inspect(op.get_bind()).has_table("tracks"):
        return
    bind = op.get_bind()
    existing = {col["name"] for col in sa.inspect(bind).get_columns("tracks")}
    if "stars_reconciled" not in existing:
        # Inspector-guarded + dialect-portable: ``ADD COLUMN IF NOT EXISTS`` is
        # PostgreSQL-only and the chain also runs on the SQLite test engine
        # (the house pattern — 014/016/018/020/021/022).
        op.add_column(
            "tracks",
            sa.Column("stars_reconciled", sa.Boolean(), nullable=True, server_default=sa.true()),
        )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("tracks"):
        return
    try:
        op.drop_column("tracks", "stars_reconciled")
    except Exception:
        # SQLite cannot ALTER/DROP a column; the chain only needs to be a
        # no-op there.
        pass
