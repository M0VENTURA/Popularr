"""A missing-track download must join the album it was queued from.

REPORTED

> "when I select to download missing tracks, they import as a different album on
> navidrome rather than matching the album that it was selected to download from"

WHY IT SPLIT

Navidrome groups an album by album NAME + album artist + the MusicBrainz RELEASE
id carried in the file's tags. The two dumps show the same album name under two
release ids:

    existing track  musicbrainz_albumid: 42a83ac4-e079-49e5-a274-348f88f1ba42
    downloaded new  musicbrainz_albumid: 58fd275b-b0b8-4a9f-b5d3-7326b73f3947

The queued row carries the release id the MISSING LIST was computed against,
which is not always the release the album's own tracks store — a same-named
edition (different country, disc count or barcode) resolves to a different MBID.
Writing that onto the new file creates a second album with the same name.

THE FIX

The album's existing rows are authoritative: they are what Navidrome already
groups together, so the new file adopts their release id. Only a genuinely new
album (no sibling rows) keeps the queued release.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session, get_engine
from db.models import Track
from services.downloads import download_completion_service as dcs

from queue_schema_helpers import ensure_queue_table  # noqa: F401  (schema parity)

ALBUM_RELEASE = "42a83ac4-e079-49e5-a274-348f88f1ba42"
QUEUED_RELEASE = "58fd275b-b0b8-4a9f-b5d3-7326b73f3947"


@pytest.fixture(autouse=True)
def _wipe():
    Track.__table__.create(get_engine(), checkfirst=True)
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))
        session.commit()
    yield
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))
        session.commit()


def _seed_existing_track(mbid: str = ALBUM_RELEASE) -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, title, artist, album, album_artist, "
                "track_number, disc_number, file_path, musicbrainz_album_mbid) "
                "VALUES ('t1', 'Fuels the Comedy', 'Korn', 'The Path of Totality', "
                "'Korn', '12', '1', :path, :mbid)"
            ),
            {"path": "/music/Korn/2011 - The Path of Totality/12. Korn - Fuels the Comedy.mp3",
             "mbid": mbid},
        )
        session.commit()


def _item(**overrides):
    item = {
        "id": 99,
        "artist": "Korn",
        "album_artist": "Korn",
        "album": "The Path of Totality",
        "title": "Chaos Lives in Everything",
        "track_number": "1",
        "disc_number": "1",
        "year": "2011",
        "release_mbid": QUEUED_RELEASE,
        "metadata": "{}",
    }
    item.update(overrides)
    return item


class TestTheAlbumReleaseWins:
    def test_a_downloaded_missing_track_adopts_the_albums_release(self) -> None:
        """THE REPORT — one album name, one release id."""
        _seed_existing_track()
        item = _item()
        dcs._resolve_album_level_metadata(item)
        assert item["release_mbid"] == ALBUM_RELEASE, (
            "the queued release id would give Navidrome a second album with the "
            f"same name: {item['release_mbid']}"
        )

    def test_a_new_album_keeps_the_queued_release(self) -> None:
        """CONTROL — nothing to align with, so the queued release stands."""
        item = _item()
        dcs._resolve_album_level_metadata(item)
        assert item["release_mbid"] == QUEUED_RELEASE

    def test_an_album_without_a_stored_release_keeps_the_queued_one(self) -> None:
        """CONTROL — a sibling row with NO MBID is not evidence."""
        _seed_existing_track(mbid="")
        item = _item()
        dcs._resolve_album_level_metadata(item)
        assert item["release_mbid"] == QUEUED_RELEASE

    def test_the_albums_own_release_is_used_even_when_it_matches(self) -> None:
        """CONTROL — identical ids are a no-op, not a rewrite."""
        _seed_existing_track(mbid=QUEUED_RELEASE)
        item = _item()
        dcs._resolve_album_level_metadata(item)
        assert item["release_mbid"] == QUEUED_RELEASE

    def test_it_does_not_borrow_from_another_album(self) -> None:
        """The sibling lookup is scoped to THIS album."""
        _seed_existing_track()
        item = _item(album="Life Is Peachy")
        dcs._resolve_album_level_metadata(item)
        assert item["release_mbid"] == QUEUED_RELEASE, (
            "a different album's release id must never be adopted"
        )
