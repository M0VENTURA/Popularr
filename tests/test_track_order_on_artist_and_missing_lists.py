"""Track lists must be in LISTENING order on every surface, not TEXT order.

Reported
--------
> When expanding albums on the artist page, the order isn't correct as it goes
> 1, 10, 11, 12, 2

> Similar ordering issue seems to happen sometimes when importing missing
> tracks on the album page

Why
---
``tracks.disc_number`` and ``tracks.track_number`` are **TEXT**, so
``ORDER BY track_number`` — and any ``sort()`` over the raw strings — compares
"10" < "2". ``2026-10-05-album-track-ordering`` fixed the ALBUM page with
``helpers/track_ordering``; these were the surfaces that never joined it:

* ``db/repositories/metadata.fetch_album_tracklist`` → the **artist page's
  expandable tracklist** (`/api/album/tracklist`);
* ``album_missing_service.get_library_tracks`` → the library side of the
  missing-track comparison;
* ``album_missing_service.get_missing_tracks_from_db`` → the **missing list**
  the album page shows (`/api/album/missing-tracks`);
* the duplicate-groups sort in ``get_missing_tracks``, which tuple-sorted TEXT
  disc/track numbers.

The SQL ``ORDER BY`` stays as a pre-sort — same contract the album page
documents — but the order the user SEES is decided in Python through
``album_track_sort_key``, so every surface shares one rule and cannot drift.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
METADATA_REPO = REPO_ROOT / "db" / "repositories" / "metadata.py"
MISSING_SERVICE = REPO_ROOT / "services" / "metadata" / "album_missing_service.py"

ARTIST = "Sirenia"
ALBUM = "Ordered Album"


def _seed_tracks(db_session, album: str, numbers: list[str]) -> None:
    from sqlalchemy import text

    for number in numbers:
        db_session.execute(
            text("""
                INSERT INTO tracks (id, artist, album, title, file_path,
                                    track_number, disc_number)
                VALUES (:id, :artist, :album, :title, :file_path, :number, '1')
                ON CONFLICT DO NOTHING
            """),
            {
                "id": f"ord-{album[:10]}-{number}",
                "artist": ARTIST,
                "album": album,
                "title": f"Track {number}",
                "file_path": f"/music/{ARTIST}/{album}/{number}.mp3",
                "number": number,
            },
        )
    db_session.commit()


class TestTheArtistPageExpand:
    def test_the_repository_orders_numerically(self, db_session):
        """``1, 10, 11, 12, 2`` is the TEXT order — the reported one."""
        _seed_tracks(db_session, ALBUM, ["10", "11", "12", "2", "1"])

        from db.repositories.metadata import fetch_album_tracklist

        rows = fetch_album_tracklist(artist=ARTIST, album=ALBUM)
        assert [str(r[2]) for r in rows] == ["1", "2", "10", "11", "12"], (
            "the artist page's tracklist is in TEXT order"
        )

    def test_the_expanded_tracklist_positions_are_numeric(self, db_session):
        """The shape the artist page actually renders (`position`)."""
        _seed_tracks(db_session, ALBUM, ["10", "2", "1", "12", "11"])

        from services.metadata.album_service import get_album_tracklist_from_db

        rows = get_album_tracklist_from_db(ARTIST, ALBUM)
        assert [r["position"] for r in rows] == ["1", "2", "10", "11", "12"]
        assert rows[0]["title"] == "Track 1"

    def test_an_unnumbered_track_sorts_after_the_numbered_ones(self, db_session):
        album = "Unnumbered Album"
        _seed_tracks(db_session, album, ["2", "1"])
        from sqlalchemy import text

        db_session.execute(
            text("""
                INSERT INTO tracks (id, artist, album, title, file_path, track_number)
                VALUES ('ord-unnumbered-x', :artist, :album, 'ZZ No Number',
                        '/music/x.mp3', '')
                ON CONFLICT DO NOTHING
            """),
            {"artist": ARTIST, "album": album},
        )
        db_session.commit()

        from db.repositories.metadata import fetch_album_tracklist

        rows = fetch_album_tracklist(artist=ARTIST, album=album)
        titles = [str(r[1]) for r in rows]
        assert titles[-1] == "ZZ No Number", titles


class TestTheAlbumPageMissingTracks:
    def test_library_tracks_are_ordered_numerically(self, db_session):
        # Its OWN album: the shared in-memory DB keeps rows seeded by earlier
        # tests, so reusing ALBUM would make this expectation depend on them.
        album = "Library Ordered Album"
        _seed_tracks(db_session, album, ["10", "2", "1", "11"])

        from services.metadata.album_missing_service import get_library_tracks

        rows = get_library_tracks(ARTIST, album)
        assert [str(r["track_number"]) for r in rows] == ["1", "2", "10", "11"]

    def test_missing_tracks_are_ordered_numerically(self, db_session):
        """The list the album page renders after importing missing tracks."""
        from sqlalchemy import text

        db_session.execute(text("""
            CREATE TABLE IF NOT EXISTS missing_album_tracks (
                id INTEGER PRIMARY KEY,
                artist_name TEXT,
                album_name TEXT,
                title TEXT,
                track_number TEXT,
                disc_number TEXT,
                track_artist TEXT,
                year TEXT,
                release_id TEXT,
                recording_mbid TEXT,
                duration REAL,
                ignored BOOLEAN
            )
        """))
        album = "Missing Ordered Album"
        for number in ("10", "2", "1"):
            db_session.execute(
                text("""
                    INSERT INTO missing_album_tracks
                        (artist_name, album_name, title, track_number,
                         disc_number, ignored)
                    VALUES (:artist, :album, :title, :number, '1', FALSE)
                """),
                {
                    "artist": ARTIST,
                    "album": album,
                    "title": f"Missing {number}",
                    "number": number,
                },
            )
        db_session.commit()

        from services.metadata.album_missing_service import get_missing_tracks_from_db

        result = get_missing_tracks_from_db(ARTIST, album)
        assert [str(t["track_number"]) for t in result["missing_tracks"]] == [
            "1", "2", "10",
        ], "the missing list is in TEXT order"

    def test_the_duplicate_groups_sort_numerically(self):
        """The tuple sort compared TEXT disc/track numbers directly."""
        src = MISSING_SERVICE.read_text(encoding="utf-8")
        assert "duplicates.sort(key=album_track_sort_key)" in src, (
            "duplicate groups are still tuple-sorted on raw strings"
        )


class TestEverySurfaceSharesTheOneRule:
    def test_the_tracklist_repository_imports_the_shared_key(self):
        src = METADATA_REPO.read_text(encoding="utf-8")
        assert "from helpers.track_ordering import album_track_sort_key" in src

    def test_the_missing_service_imports_the_shared_key(self):
        src = MISSING_SERVICE.read_text(encoding="utf-8")
        assert "from helpers.track_ordering import album_track_sort_key" in src

    def test_disc_number_is_selected_but_appended_last(self):
        """``get_album_tracklist`` reads rows positionally — appending a column
        is safe, inserting one in the middle would silently shift every field."""
        src = METADATA_REPO.read_text(encoding="utf-8")
        block = src[src.index("def fetch_album_tracklist"):][:2000]
        assert "SELECT id, title, track_number, duration, artist, disc_number" in block, (
            "disc_number must be appended last so the positional reads in "
            "album_service.get_album_tracklist keep their meaning"
        )
