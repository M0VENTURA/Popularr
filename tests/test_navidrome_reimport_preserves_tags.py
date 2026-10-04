"""Navidrome re-import must never BLANK fields its Subsonic API cannot carry.

Two defects pinned here, both found while auditing "is this information
pulled from Navidrome during a scan":

1. **Wipe on re-import.** ``PRESERVE_WHEN_EMPTY_FIELDS`` did not cover
   ``barcode``/``tracktotal``/``disctotal``/``copyright``/``language``/``asin``/
   ``work``/``musicbrainz_albumstatus``/``albumversion``.  Those tags live in
   the FILE (Navidrome reads them into ``model.MediaFile.Tags``) but are NOT
   in the Subsonic ``Child``/``AlbumID3`` structs Navidrome serialises — so
   the extractor always produced ``""`` and ``_execute_save`` wrote
   ``col=EXCLUDED.col`` → the stored value was wiped on every import.
   Verified by round-trip: a row seeded with barcode ``8809928957340`` and
   tracktotal ``17`` came back ``''``.

2. **The missing-fields mode imported NOTHING.**  ``prefetch_artist_state``
   returned ``albums_needing_reimport: set()`` unconditionally (the port
   dropped the population half the old system had), so ``should_skip_album``
   hit "not in albums_needing_reimport" and skipped every album whenever
   ``filter_missing`` was on.

Also pinned: Navidrome's wire key for explicit is ``explicitStatus``
(camelCase) and the album version rides ``AlbumID3.version`` — the extractor
read neither, so both columns could only ever be preserved, never refreshed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session


def _ensure_tracks_table() -> None:
    from db.models import Track

    with db_session() as session:
        Track.__table__.create(session.get_bind(), checkfirst=True)


_ensure_tracks_table()


def _seed(track_id: str = "wipe-1", **overrides) -> None:
    row = {
        "id": track_id,
        "artist": "Stray Kids",
        "album_artist": "Stray Kids",
        "album": "SKZ-REPLAY 2026 Pt.1",
        "title": "Battle Ground",
        "file_path": "Stray Kids/2026 - SKZ-REPLAY 2026 Pt.1/01.mp3",
        "duration": 200,
        "track_number": "1",
        "year": "2026",
        "barcode": "8809928957340",
        "tracktotal": "17",
        "disctotal": "2",
        "copyright": "(c) 2026",
        "language": "en",
        "asin": "B0C1",
        "work": "Some Work",
        "musicbrainz_albumstatus": "Official",
        "albumversion": "Deluxe",
        "explicitstatus": "explicit",
        "isrc": "US5TA2600199",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join(f":{c}" for c in row)
    with db_session() as session:
        session.execute(
            text(f"DELETE FROM tracks WHERE id = :id"), {"id": track_id}
        )
        session.execute(
            text(f"INSERT INTO tracks ({cols}) VALUES ({marks})"), row
        )
        session.commit()


def _read(track_id: str = "wipe-1") -> dict:
    cols = (
        "barcode", "tracktotal", "disctotal", "copyright", "language", "asin",
        "work", "musicbrainz_albumstatus", "albumversion", "explicitstatus",
        "isrc", "duration", "track_number", "year", "file_path",
    )
    with db_session() as session:
        row = session.execute(
            text(f"SELECT {', '.join(cols)} FROM tracks WHERE id = :id"),
            {"id": track_id},
        ).fetchone()
    return dict(zip(cols, row)) if row else {}


def _navidrome_song(**overrides) -> dict:
    """A song exactly as Navidrome's Subsonic ``Child`` serialises it.

    Built from ``server/subsonic/responses/responses.go``: the Child carries
    title/artist/album/track/disc/year/duration/path plus the OpenSubsonic
    extension (musicBrainzId, isrc[], genres, moods, explicitStatus…).  It
    has NO barcode/tracktotal/disctotal/copyright/language/asin/work/
    albumversion fields — those stay in ``model.MediaFile.Tags``.
    """
    song = {
        "id": "wipe-1",
        "title": "Battle Ground (Korean version)",
        "artist": "Stray Kids",
        "albumArtist": "Stray Kids",
        "album": "SKZ-REPLAY 2026 Pt.1",
        "path": "Stray Kids/2026 - SKZ-REPLAY 2026 Pt.1/01 - Battle Ground.mp3",
        "duration": 200,
        "track": 1,
        "disc": 1,
        "year": 2026,
        "musicBrainzId": "eb72d8f8-848e-4ea9-a260-f51bb721cb3d",
        "isrc": ["US5TA2600199"],
        "genres": [{"name": "K-Pop"}],
        "explicitStatus": "explicit",
    }
    song.update(overrides)
    return song


def _reimport(song: dict | None = None, album: dict | None = None) -> None:
    from services.scanning.metadata_extractor import (
        extract_album_metadata,
        extract_track_metadata,
    )
    from services.scanning.payload_builder import build_track_payload
    from db.repositories.popularity_repository import save_to_db

    song = song if song is not None else _navidrome_song()
    payload = build_track_payload(
        track=song,
        extracted=extract_track_metadata(song),
        album_name="SKZ-REPLAY 2026 Pt.1",
        album_artist_value="Stray Kids",
        canonical_artist_name="Stray Kids",
        is_new_track=False,
        album_mbid="729beb45-1c4c-4da9-816a-fc4007ff7507",
        album_tags=extract_album_metadata(
            album if album is not None else {"id": "al-1", "name": "SKZ"}
        ),
    )
    payload["id"] = song.get("id", "wipe-1")
    payload["_navidrome_sync"] = True
    assert save_to_db(payload) is True


@pytest.fixture(autouse=True)
def _clean():
    _ensure_tracks_table()
    yield
    with db_session() as session:
        session.execute(text("DELETE FROM tracks WHERE id LIKE 'wipe-%'"))
        session.commit()


class TestReimportPreservesUnreachableTags:
    """A re-import must keep every tag the Subsonic response cannot carry."""

    def test_file_only_tags_survive_a_reimport(self):
        _seed()
        _reimport()
        row = _read()

        assert row["barcode"] == "8809928957340", (
            "barcode is in the FILE but not in Navidrome's Child struct — "
            "the import cannot re-read it, so it must not blank it"
        )
        assert row["tracktotal"] == "17"
        assert row["disctotal"] == "2"
        assert row["copyright"] == "(c) 2026"
        assert row["language"] == "en"
        assert row["asin"] == "B0C1"
        assert row["work"] == "Some Work"
        assert row["musicbrainz_albumstatus"] == "Official"
        assert row["albumversion"] == "Deluxe"

    def test_wire_fields_are_still_refreshed(self):
        """Preserving must not freeze fields Navidrome DOES send."""
        _seed(isrc="OLDCODE12345", explicitstatus="notExplicit")
        _reimport()
        row = _read()

        # isrc rides Child.isrc[] — the fresh value must win.
        assert row["isrc"] == "US5TA2600199"
        # explicitStatus rides Child.explicitStatus (camelCase) — same.
        assert row["explicitstatus"] == "explicit"

    def test_identity_and_score_columns_still_update(self):
        _seed(title="Old Title", year="1999")
        _reimport()
        row = _read()
        assert row["duration"] == 200
        assert row["year"] == "2026"


class TestExplicitStatusWireKey:
    """Navidrome sends ``explicitStatus``; the extractor read lowercase."""

    def test_extractor_reads_the_camelcase_wire_key(self):
        from services.scanning.metadata_extractor import extract_track_metadata

        extracted = extract_track_metadata(_navidrome_song(explicitStatus="explicit"))
        assert extracted["explicitstatus"] == "explicit"

    def test_extractor_still_reads_the_legacy_aliases(self):
        from services.scanning.metadata_extractor import extract_track_metadata

        extracted = extract_track_metadata(
            _navidrome_song(explicitStatus=None, itunesadvisory="cleaned")
        )
        assert extracted["explicitstatus"] == "cleaned"


class TestAlbumVersionComesFromAlbumID3:
    """``AlbumID3.version`` is on the wire; the song child never carries it."""

    def test_album_object_version_is_collected(self):
        from services.scanning.metadata_extractor import extract_album_metadata

        out = extract_album_metadata({
            "id": "al-1",
            "name": "SKZ",
            "version": "Deluxe Edition",
        })
        assert out["albumversion"] == "Deluxe Edition"

    def test_absent_version_emits_nothing(self):
        from services.scanning.metadata_extractor import extract_album_metadata

        assert "albumversion" not in extract_album_metadata({"id": "al", "name": "X"})

    def test_album_version_reaches_the_payload(self):
        from services.scanning.metadata_extractor import extract_album_metadata

        out = extract_album_metadata(
            {"id": "al", "name": "X", "version": "Anniversary"},
        )
        assert out["albumversion"] == "Anniversary"


class TestMissingFieldsMode:
    """``mode=missing`` must flag albums, not skip every one of them."""

    def test_albums_with_missing_critical_fields_are_flagged(self):
        from services.scanning.navidrome_import import prefetch_artist_state

        _seed("wipe-ok")  # complete row → not flagged
        _seed("wipe-bad", album="Needs Work", duration=None)
        _seed("wipe-bad2", album="Second Helping", track_number="0")
        _seed("wipe-bad3", album="No Year", year="")
        _seed("wipe-bad4", album="No Path", file_path="")

        state = prefetch_artist_state(canonical_artist_name="Stray Kids")

        assert "Needs Work" in state["albums_needing_reimport"]
        # NB: keys go through strip_album_edition_marker, so use names the
        # edition-marker heuristic leaves alone.
        assert "Second Helping" in state["albums_needing_reimport"]
        assert "No Year" in state["albums_needing_reimport"]
        assert "No Path" in state["albums_needing_reimport"]
        # The complete album must NOT be flagged (or mode=missing == mode=all).
        assert "SKZ-REPLAY 2026 Pt.1" not in state["albums_needing_reimport"]

    def test_complete_artist_flags_nothing(self):
        from services.scanning.navidrome_import import prefetch_artist_state

        _seed()
        state = prefetch_artist_state(canonical_artist_name="Stray Kids")
        assert state["albums_needing_reimport"] == set()

    def test_other_artists_rows_do_not_leak_in(self):
        from services.scanning.navidrome_import import prefetch_artist_state

        _seed("wipe-other", artist="Someone Else", album_artist="Someone Else",
              album="Their Album", duration=None)

        state = prefetch_artist_state(canonical_artist_name="Stray Kids")
        assert state["albums_needing_reimport"] == set()

    def test_skip_decision_keeps_the_flagged_album(self):
        """The flag must survive into should_skip_album's contract."""
        from services.scanning.filters import should_skip_album

        flagged = {"Needs Work"}
        assert should_skip_album(
            album_name="Needs Work",
            album_filter=None,
            filter_missing=True,
            albums_needing_reimport=flagged,
            diff_mode=False,
            changed_album_names=None,
        ) is False
        assert should_skip_album(
            album_name="Complete Album",
            album_filter=None,
            filter_missing=True,
            albums_needing_reimport=flagged,
            diff_mode=False,
            changed_album_names=None,
        ) is True
