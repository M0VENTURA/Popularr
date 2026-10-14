"""Artist alternate names: the ``artist_aliases`` table.

Why
---
Renamed or rebranded artists exist under more than one name (Electric Callboy
was Eskimo Callboy). Every identity comparison in the app — above all the cover
detector's "does this recording credit name someone else?" check — compares
artist NAMES, so a recording under the band's other name looked like a
different artist and was flagged as a cover of itself.

This table records "these names are the same artist":

* ``alias_key`` — the alternate name, casefolded; PRIMARY KEY, because a name
  can only ever belong to one artist (an alias claimed by two artists would
  make resolution ambiguous).
* ``alias`` — the display form, as entered.
* ``artist_key`` / ``artist`` — the canonical artist (casefolded key + display
  form). ``artist_key`` is indexed so "all aliases of this page" is one
  lookup; the casefolded keys are what the resolution cache maps.

The table is created by the schema bootstrap (``db/schema.py``) as well as by
this chain, so the migration is guarded rather than authoritative — a missing
table means the bootstrap will create it on startup.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "024_artist_aliases"
down_revision: Union[str, None] = "023_tracks_stars_reconciled"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("artist_aliases"):
        return
    op.create_table(
        "artist_aliases",
        sa.Column("alias_key", sa.Text(), primary_key=True),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("artist_key", sa.Text(), nullable=False),
        sa.Column("artist", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp()),
    )
    op.create_index("idx_artist_aliases_artist_key", "artist_aliases", ["artist_key"])


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("artist_aliases"):
        return
    op.drop_index("idx_artist_aliases_artist_key", table_name="artist_aliases")
    op.drop_table("artist_aliases")
