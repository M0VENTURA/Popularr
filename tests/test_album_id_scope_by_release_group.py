"""An album-wide id write must not cross into another same-named album.

Reported: the right release was matched to "I am YOU", then it reverted to the
old one (a track on SKZ2020). Both albums have the same artist and NAME, so the
fan-out that fixes an off-page row was also rewriting the OTHER album's release
id and release group; the next scan then wrote the other album's ids back into
the files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OLD_RELEASE = "89f4e9b4-4786-4471-ac23-4a6900259a35"
OLD_GROUP = "f41e221b-aa20-4b20-b209-8bad6ae46ea8"
NEW_RELEASE = "9cbf3109-0853-4397-b0e5-05c8968fbade"
NEW_GROUP = "a815fc7d-aa83-4267-a135-dd68210f296c"
OTHER_RELEASE = "00000000-1111-2222-3333-444444444444"
OTHER_GROUP = "55555555-6666-7777-8888-999999999999"

ARTIST = "Stray Kids"
ALBUM = "I am YOU"


@pytest.fixture(autouse=True)
def _empty_tracks(db_session):
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()


def _seed(db_session, track_id: str, release: str, group: str, *, album: str = ALBUM):
    db_session.execute(
        text("""
            INSERT INTO tracks (
                id, artist, album, album_artist, file_path,
                musicbrainz_album_mbid, musicbrainz_albumid,
                musicbrainz_releasegroupid
            ) VALUES (
                :id, :artist, :album, :artist, :file_path,
                :release, :release, :group
            )
            ON CONFLICT DO NOTHING
        """),
        {
            "id": track_id,
            "artist": ARTIST,
            "album": album,
            "file_path": f"/music/{ARTIST}/{album}/{track_id}.flac",
            "release": release,
            "group": group,
        },
    )
    db_session.commit()


def _ids(db_session, track_id: str) -> tuple[str, str]:
    row = db_session.execute(
        text(
            "SELECT musicbrainz_album_mbid, musicbrainz_releasegroupid "
            "FROM tracks WHERE id = :id"
        ),
        {"id": track_id},
    ).fetchone()
    return (str(row[0] or ""), str(row[1] or ""))


class TestTheFanOutStaysInsideOneReleaseFamily:
    def test_the_other_same_named_album_is_untouched(self, db_session):
        from services.metadata.album_service import apply_album_wide_ids

        _seed(db_session, "ep-1", OLD_RELEASE, OLD_GROUP)
        _seed(db_session, "ep-2", OLD_RELEASE, OLD_GROUP)
        _seed(db_session, "other-1", OTHER_RELEASE, OTHER_GROUP)

        out = apply_album_wide_ids(
            artist=ARTIST,
            album=ALBUM,
            album_mbid=NEW_RELEASE,
            release_group_mbid=NEW_GROUP,
            previous_release_group_mbid=OLD_GROUP,
        )

        assert out["rows_updated"] == 2, out
        assert _ids(db_session, "ep-1") == (NEW_RELEASE, NEW_GROUP)
        assert _ids(db_session, "ep-2") == (NEW_RELEASE, NEW_GROUP)
        assert _ids(db_session, "other-1") == (OTHER_RELEASE, OTHER_GROUP), (
            "the other same-named album was rewritten — this is the reported "
            "release-group revert"
        )

    def test_a_row_with_no_group_is_still_filled(self, db_session):
        from services.metadata.album_service import apply_album_wide_ids

        _seed(db_session, "ep-1", OLD_RELEASE, OLD_GROUP)
        _seed(db_session, "ungrouped", "", "")
        _seed(db_session, "other-1", OTHER_RELEASE, OTHER_GROUP)

        apply_album_wide_ids(
            artist=ARTIST,
            album=ALBUM,
            album_mbid=NEW_RELEASE,
            release_group_mbid=NEW_GROUP,
            previous_release_group_mbid=OLD_GROUP,
        )

        assert _ids(db_session, "ungrouped")[0] == NEW_RELEASE
        assert _ids(db_session, "other-1") == (OTHER_RELEASE, OTHER_GROUP)

    def test_clearing_is_scoped_too(self, db_session, monkeypatch):
        from services.metadata.album_service import apply_album_wide_ids

        # The release-missing cleanup opens its own tables; not part of this
        # test and it must not disturb the fixture's engine.
        import services.metadata.album_lookup_skip_service as skip

        monkeypatch.setattr(
            skip, "release_missing_tracks_for_cleared_mbid", lambda *a, **k: 0
        )

        _seed(db_session, "ep-1", OLD_RELEASE, OLD_GROUP)
        _seed(db_session, "other-1", OTHER_RELEASE, OTHER_GROUP)

        apply_album_wide_ids(
            artist=ARTIST,
            album=ALBUM,
            clear_album_mbid=True,
            previous_release_group_mbid=OLD_GROUP,
        )

        assert _ids(db_session, "ep-1")[0] == ""
        assert _ids(db_session, "other-1")[0] == OTHER_RELEASE


class TestTheSavePassesItsOwnReleaseFamily:
    def test_the_page_save_scopes_the_fan_out(self):
        import inspect

        import routes.ui_routes as ui_routes

        source = inspect.getsource(ui_routes)
        start = source.index("apply_album_wide_ids(")
        window = source[start:start + 900]
        assert "previous_release_group_mbid=" in window, (
            "the save must tell the fan-out which release family it is editing"
        )
