"""One file must never re-identify its album.

Reported: Korn — *Untouchables* was merged in Navidrome and stayed merged for
about eight hours, then split again. The two files ended up carrying DIFFERENT
release ids — Canada on track 1, Japan on the bonus remix — because a
per-track write stamped that track's own release onto its file.

The album-level writers (the release unifier, the album save) fan ONE id across
every row and file. ``update_file_tags`` therefore drops a lone, disagreeing
release id instead of splitting the album, and reports it.
"""

from __future__ import annotations

import importlib

import pytest
from sqlalchemy import text

CANADA = "3c8d8b38-ac3b-480c-87f8-5048a6198ff5"
JAPAN = "2606ffe5-3d2d-4549-8fa3-14e3ed6b1cfd"
GROUP = "4041334a-fdca-3a73-878e-8513b464a88d"

ARTIST = "Korn"
ALBUM = "Untouchables"


@pytest.fixture()
def tfs():
    return importlib.import_module("services.metadata.tag_file_service")


@pytest.fixture()
def tracks(db_session):
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()
    return db_session


def _seed(session, track_id: str, path: str, release: str, *, group: str = GROUP):
    session.execute(
        text(
            "INSERT INTO tracks (id, title, artist, album, album_artist, file_path, "
            "musicbrainz_album_mbid, musicbrainz_albumid, musicbrainz_releasegroupid) "
            "VALUES (:id, :title, :artist, :album, :artist, :path, :release, "
            ":release, :group) ON CONFLICT DO NOTHING"
        ),
        {
            "id": track_id,
            "title": track_id,
            "artist": ARTIST,
            "album": ALBUM,
            "path": path,
            "release": release,
            "group": group,
        },
    )
    session.commit()


class TestTheGuard:
    def test_a_lone_track_cannot_stamp_a_different_release(self, tracks, tfs):
        _seed(tracks, "t1", f"/music/{ARTIST}/{ALBUM}/01.mp3", CANADA)
        _seed(tracks, "t2", f"/music/{ARTIST}/{ALBUM}/02.mp3", CANADA)
        _seed(tracks, "t15", f"/music/{ARTIST}/{ALBUM}/15.mp3", CANADA)

        out = tfs._guard_album_release_id(
            f"/music/{ARTIST}/{ALBUM}/15.mp3",
            {
                "title": "Here to Stay (T Ray's mix)",
                "musicbrainz_albumid": JAPAN,
                "musicbrainz_releasetrackid": "ebd4ec10-3cea-48f1-b0f3-a2c91666f7e8",
            },
        )

        assert "musicbrainz_albumid" not in out, (
            "a single file must not overwrite the album's release identity"
        )
        # The rest of the payload still lands.
        assert out["title"] == "Here to Stay (T Ray's mix)"
        assert out["musicbrainz_releasetrackid"] == "ebd4ec10-3cea-48f1-b0f3-a2c91666f7e8"

    def test_an_album_wide_write_is_allowed(self, tracks, tfs):
        """The unifier/save updates every row FIRST, so consensus agrees."""
        _seed(tracks, "t1", f"/music/{ARTIST}/{ALBUM}/01.mp3", JAPAN)
        _seed(tracks, "t2", f"/music/{ARTIST}/{ALBUM}/02.mp3", JAPAN)

        out = tfs._guard_album_release_id(
            f"/music/{ARTIST}/{ALBUM}/02.mp3",
            {"musicbrainz_albumid": JAPAN},
        )

        assert out["musicbrainz_albumid"] == JAPAN

    def test_a_mixed_album_is_not_guarded(self, tracks, tfs):
        """No consensus yet: a first/wrong-state write is not blocked."""
        _seed(tracks, "t1", f"/music/{ARTIST}/{ALBUM}/01.mp3", CANADA)
        _seed(tracks, "t15", f"/music/{ARTIST}/{ALBUM}/15.mp3", JAPAN)

        out = tfs._guard_album_release_id(
            f"/music/{ARTIST}/{ALBUM}/15.mp3", {"musicbrainz_albumid": JAPAN},
        )

        assert out["musicbrainz_albumid"] == JAPAN

    def test_a_file_outside_the_library_is_untouched(self, tfs):
        payload = {"musicbrainz_albumid": JAPAN}
        assert tfs._guard_album_release_id("/nowhere/x.mp3", payload) == payload

    def test_a_malformed_album_id_is_not_a_consensus(self, tracks, tfs):
        """`albumid == releasegroupid` rows must not define the album id."""
        _seed(tracks, "t1", f"/music/{ARTIST}/{ALBUM}/01.mp3", CANADA)
        _seed(tracks, "t2", f"/music/{ARTIST}/{ALBUM}/02.mp3", GROUP)

        out = tfs._guard_album_release_id(
            f"/music/{ARTIST}/{ALBUM}/02.mp3", {"musicbrainz_albumid": JAPAN},
        )

        # Only ONE valid id (Canada) exists, so Japan is refused.
        assert "musicbrainz_albumid" not in out
