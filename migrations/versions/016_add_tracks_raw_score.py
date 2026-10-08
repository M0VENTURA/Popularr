"""Add ``tracks.raw_score`` — the pre-album-relative-remap popularity blend.

Why a column is needed
----------------------
``_apply_album_relative_normalization`` rewrites ``popularity_score`` /
``final_score`` from ``_raw_combined`` using

    z = (score - median) / mad ;  score = sigmoid(z)

which is monotonic but **saturating**: applying it to an already-remapped value
pulls the album's extremes back toward the middle. ``_raw_combined`` was never
persisted — it is an underscore-prefixed key that ``_execute_save`` drops — so
the next run rebuilt it from ``final_score``, i.e. from the *remapped* value, and
remapped it again, then wrote the result back.

Measured with the shipped functions on an 8-track album, the top track's
``album_z`` falls monotonically and crosses below ``star5_album_z`` (1.0) after
12 further passes, after which the 5-star gate can never clear again while
4/3/2/1 keep working. A repeated Finalise pass is exactly that loop, which is
why "running a Finalise scan assigns no 5★".

``raw_score`` keeps the only copy of the blend the remap is defined *against*,
so the operation becomes idempotent: re-running a Finalise pass no longer moves
the numbers.

Rows written before this migration have ``raw_score IS NULL``; readers fall back
to ``final_score`` for them (the old, eroding behaviour) until the next fresh
score populates it.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "016_add_tracks_raw_score"
down_revision: Union[str, None] = "015_drop_download_queue_max_retries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns() -> set[str]:
    """Return the current column set of the ``tracks`` table.

    Inspector-guarded and dialect-portable — ``ADD COLUMN IF NOT EXISTS`` is
    PostgreSQL-only and the migration chain also runs against the SQLite test
    engine (the same pattern ``014_add_tracks_release_detail`` uses).
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns("tracks")}


def upgrade() -> None:
    if "raw_score" not in _existing_columns():
        op.add_column("tracks", sa.Column("raw_score", sa.Double(), nullable=True))


def downgrade() -> None:
    if "raw_score" in _existing_columns():
        op.drop_column("tracks", "raw_score")
