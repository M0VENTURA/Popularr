"""Add the album release-detail columns to ``tracks``.

These columns back fields the album page has ALWAYS had inputs for, and that the
scan already extracts from file tags — but which had no column to live in.  The
persistence layer silently drops keys with no matching column, so every one of
them was written to the audio files and then discarded from the database (the
album page's inputs appeared to save and then showed blank on reload).

Also adds ``release_title``, the SPECIFIC release/edition name, which is
distinct from the album name:

    album (release GROUP)  "Experience"
    release_title          "Experience: Expanded (Remixes and B-Sides)"

A release group has many releases, so the two names legitimately differ; the
release title is what the album page shows as a tagline under the main name.

Year semantics (unchanged by this migration, both columns already exist):
    year          the album's year, treated as the ORIGINAL/release-group year
    release_year  the SPECIFIC release's year (the edition's year)

Every DDL statement is existence-guarded and dialect-portable, so the chain
also runs cleanly against the SQLite test engine.

Revision ID: 014_add_tracks_release_detail
Revises: 013_add_tracks_discogs_album_id
Create Date: 2026-09-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "014_add_tracks_release_detail"
down_revision: Union[str, None] = "013_add_tracks_discogs_album_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Release-detail fields the album form posts and the tag extractor reads.
# All TEXT: they carry tag values verbatim (dates as "YYYY"/"YYYY-MM-DD",
# totals as "12", and so on), matching the existing ``year``/``releasedate``
# columns rather than coercing here.
_RELEASE_DETAIL_COLUMNS: tuple[str, ...] = (
    "recordlabel",
    "catalognumber",
    "barcode",
    "asin",
    "releasedate",
    "media",
    "releasestatus",
    "copyright",
    "language",
    "explicitstatus",
    "originalyear",
    "originaldate",
    "tracktotal",
    "disctotal",
    "script",
    "discsubtitle",
    "albumversion",
    # The specific release/edition name (the release-group name lives in
    # ``album``).  New in this revision — drives the album-page tagline.
    "release_title",
)


def _existing_columns() -> set[str]:
    """Return the current column set of the ``tracks`` table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns("tracks")}


def upgrade() -> None:
    existing = _existing_columns()

    for column in _RELEASE_DETAIL_COLUMNS:
        if column not in existing:
            op.add_column("tracks", sa.Column(column, sa.Text(), nullable=True))


def downgrade() -> None:
    existing = _existing_columns()

    # Reverse order so the chain unwinds the way it was applied.
    for column in reversed(_RELEASE_DETAIL_COLUMNS):
        if column in existing:
            op.drop_column("tracks", column)
