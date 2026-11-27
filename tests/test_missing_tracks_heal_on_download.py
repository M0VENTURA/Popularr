"""End-to-end: a downloaded missing track must stop rendering as missing."""

from __future__ import annotations

from sqlalchemy import text


def _seed(session, *, include_downloaded: bool):
    # The suite's shared in-memory DB does not create this table for every test
    # (it is not in the bootstrap registry), so ensure it here.
    session.execute(text(
        "CREATE TABLE IF NOT EXISTS missing_album_tracks ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  artist_name TEXT NOT NULL, album_name TEXT NOT NULL,"
        "  title TEXT NOT NULL, track_number TEXT, disc_number INTEGER DEFAULT 1,"
        "  track_artist TEXT, year TEXT, release_id TEXT, recording_mbid TEXT,"
        "  duration INTEGER, ignored BOOLEAN NOT NULL DEFAULT 0,"
        "  is_bonus BOOLEAN NOT NULL DEFAULT 0,"
        "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    ))
    session.execute(text("DELETE FROM tracks WHERE album = 'Heal Album'"))
    session.execute(text("DELETE FROM missing_album_tracks WHERE album_name = 'Heal Album'"))

    # The library: track 1 is present, and (when downloaded) track 2 as well.
    session.execute(
        text(
            "INSERT INTO tracks (id, artist, album_artist, album, title, "
            "file_path, track_number, disc_number) "
            "VALUES ('h1', 'Healer', 'Healer', 'Heal Album', 'First Song', "
            "'/music/Healer/2001 - Heal Album/01.mp3', '1', '1')"
        )
    )
    if include_downloaded:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album_artist, album, title, "
                "file_path, track_number, disc_number) "
                "VALUES ('h2', 'Healer', 'Healer', 'Heal Album', 'Second Song', "
                "'/music/Healer/2001 - Heal Album/02.mp3', '2', '1')"
            )
        )

    # The release says both exist.
    for title, num in (("First Song", "1"), ("Second Song", "2")):
        session.execute(
            text(
                "INSERT INTO missing_album_tracks "
                "(artist_name, album_name, title, track_number, disc_number, "
                " track_artist, ignored) "
                "VALUES ('Healer', 'Heal Album', :t, :n, '1', 'Healer', FALSE)"
            ),
            {"t": title, "n": num},
        )
    session.commit()


def test_a_downloaded_track_leaves_the_missing_list(db_session):
    from services.metadata.album_missing_service import get_missing_tracks_from_db

    _seed(db_session, include_downloaded=False)
    before = get_missing_tracks_from_db("Healer", "Heal Album")
    # "First Song" is already in the library, so only track 2 is missing.
    titles = {r["title"] for r in before["missing_tracks"]}
    assert "Second Song" in titles

    # The download landed: a `tracks` row now exists for it.
    db_session.execute(
        text(
            "INSERT INTO tracks (id, artist, album_artist, album, title, "
            "file_path, track_number, disc_number) "
            "VALUES ('h2', 'Healer', 'Healer', 'Heal Album', 'Second Song', "
            "'/music/Healer/2001 - Heal Album/02.mp3', '2', '1')"
        )
    )
    db_session.commit()

    after = get_missing_tracks_from_db("Healer", "Heal Album")
    titles_after = {r["title"] for r in after["missing_tracks"]}
    assert "Second Song" not in titles_after, (
        "a track that is now in the library must stop showing as missing "
        "without a manual Lookup MBID"
    )
    assert after["missing_count"] == 0


def test_a_queued_placeholder_does_not_count_as_owned(db_session):
    """The `__queued_for_download__` sentinel row is not a file on disk."""
    from services.metadata.album_missing_service import get_missing_tracks_from_db

    _seed(db_session, include_downloaded=False)
    db_session.execute(
        text(
            "INSERT INTO tracks (id, artist, album_artist, album, title, "
            "file_path, track_number, disc_number) "
            "VALUES ('h3', 'Healer', 'Healer', 'Heal Album', 'Second Song', "
            "'__queued_for_download__', '2', '1')"
        )
    )
    db_session.commit()

    result = get_missing_tracks_from_db("Healer", "Heal Album")
    titles = {r["title"] for r in result["missing_tracks"]}
    assert "Second Song" in titles, (
        "a queued placeholder is a pending download, not a satisfied one"
    )
