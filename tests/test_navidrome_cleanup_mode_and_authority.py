"""Mode-aware stale cleanup and local metadata authority stamps."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session
from services.scanning.cleanup import (
    cleanup_stale_album_tracks_if_needed,
    cleanup_stale_artist_tracks_if_needed,
)
from services.scanning.navidrome_import import prefetch_artist_state
from services.scanning.reconciliation import (
    local_authority_conflicts,
    mark_local_tags_written_for_track_ids,
    mark_navidrome_import_confirmed,
)


def _seed(ids):
    with db_session() as session:
        for track_id in ids:
            session.execute(
                text(
                    "INSERT INTO tracks (id, artist, album, album_artist, title) "
                    "VALUES (:id, 'A', 'Al', 'A', :id)"
                ),
                {"id": track_id},
            )


@pytest.fixture(autouse=True)
def _isolated_tracks():
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))
    yield
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))


def _ids():
    with db_session() as session:
        return {
            str(row[0])
            for row in session.execute(text("SELECT id FROM tracks")).fetchall()
        }


def test_delta_cleanup_without_explicit_evidence_does_nothing():
    _seed(["keep"])
    cleanup_stale_album_tracks_if_needed(
        artist_name="A",
        album_name="Al",
        cached_ids_for_album={"keep"},
        navidrome_tracks=[],
        allow_cleanup=False,
    )
    assert _ids() == {"keep"}


def test_verified_full_cleanup_removes_absent_track():
    _seed(["gone"])
    cleanup_stale_artist_tracks_if_needed(
        artist_name="A",
        existing_track_ids={"gone"},
        navidrome_track_ids={"other"},
        allow_cleanup=True,
    )
    assert _ids() == set()


def test_local_correction_is_dirty_until_verified_import_confirms_it():
    _seed(["t1"])
    assert mark_local_tags_written_for_track_ids(["t1"]) == 1
    with db_session() as session:
        row = session.execute(
            text(
                "SELECT metadata_dirty_for_navidrome, local_tags_written_at "
                "FROM tracks WHERE id = 't1'"
            )
        ).fetchone()
    assert bool(row[0]) is True
    assert row[1] is not None

    assert mark_navidrome_import_confirmed(["t1"]) == 1
    with db_session() as session:
        row = session.execute(
            text(
                "SELECT metadata_dirty_for_navidrome, navidrome_imported_at "
                "FROM tracks WHERE id = 't1'"
            )
        ).fetchone()
    assert bool(row[0]) is False
    assert row[1] is not None


def test_prefetch_loads_every_authority_field_for_conflict_check():
    """A field omitted from the prefetch query reads as "" and looks like a
    local correction, so an agreeing payload was flagged as a conflict and the
    row could never be confirmed (reported for composer/barcode/originalyear)."""
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks ("
                "id, artist, album, album_artist, title, composer, barcode,"
                " originalyear, originaldate, musicbrainz_album_mbid,"
                " metadata_dirty_for_navidrome"
                ") VALUES ('t1', 'A', 'Al', 'A', 'T', 'Composer X', '123',"
                " '1999', '1999-01-01', 'mb-1', TRUE)"
            )
        )

    row = prefetch_artist_state(canonical_artist_name="A")["known_rows_by_id"]["t1"]
    payload = {
        "id": "t1", "title": "T", "artist": "A", "album": "Al",
        "album_artist": "A", "composer": "Composer X", "barcode": "123",
        "originalyear": "1999", "originaldate": "1999-01-01",
        "musicbrainz_album_mbid": "mb-1",
    }

    assert local_authority_conflicts(row, payload) == []
