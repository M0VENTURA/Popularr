"""One album folder, one MusicBrainz release.

Reported on Korn — Untouchables: the Canadian release id on most tracks, a
Japanese edition on the bonus remix, and one row whose ``musicbrainz_albumid``
was the release-GROUP uuid. Navidrome keys albums on the release id, so the
folder split. The fix must pick ONE edition for the whole folder — and prefer
the edition whose tracklist matches the local one (the Japanese bonus track).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from services.metadata import album_release_unify_service as svc

ARTIST = "Korn"
ALBUM = "Untouchables"
GROUP = "4041334a-fdca-3a73-878e-8513b464a88d"
CANADA = "3c8d8b38-ac3b-480c-87f8-5048a6198ff5"
JAPAN = "2606ffe5-3d2d-4549-8fa3-14e3ed6b1cfd"


@pytest.fixture(autouse=True)
def _empty_tracks(db_session):
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()


@pytest.fixture(autouse=True)
def _no_musicbrainz(monkeypatch):
    """Never call the network from these tests; the fallbacks are patched."""
    import services.enrichment.musicbrainz_service as mbs

    monkeypatch.setattr(
        mbs, "fetch_musicbrainz_release_metadata", lambda *a, **k: {},
    )
    monkeypatch.setattr(mbs, "get_release_group_releases", lambda *a, **k: {"releases": []})


def _seed(db_session, track_id: str, release: str, **cols):
    db_session.execute(
        text("""
            INSERT INTO tracks (
                id, title, artist, album, album_artist, file_path,
                musicbrainz_album_mbid, musicbrainz_albumid,
                musicbrainz_releasegroupid, barcode, catalognumber,
                releasecountry, media, tracktotal
            ) VALUES (
                :id, :title, :artist, :album, :artist, :path,
                :release, :release, :group, :barcode, :catalog,
                :country, :media, :tracktotal
            )
            ON CONFLICT DO NOTHING
        """),
        {
            "id": track_id,
            "title": track_id,
            "artist": ARTIST,
            "album": ALBUM,
            "path": f"/music/{ARTIST}/{ALBUM}/{track_id}.flac",
            "release": release,
            "group": cols.pop("group", GROUP),
            "barcode": cols.pop("barcode", ""),
            "catalog": cols.pop("catalog", ""),
            "country": cols.pop("country", ""),
            "media": cols.pop("media", ""),
            "tracktotal": cols.pop("tracktotal", None),
            **cols,
        },
    )
    db_session.commit()


def _ids(db_session) -> set[str]:
    rows = db_session.execute(
        text("SELECT musicbrainz_album_mbid FROM tracks")
    ).fetchall()
    return {str(row[0] or "") for row in rows}


@pytest.fixture
def tag_writes(monkeypatch):
    import services.metadata.tag_file_service as tfs

    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(tfs, "resolve_music_file_path", lambda p: p)
    monkeypatch.setattr(
        tfs, "update_file_tags", lambda path, tags, force=False: writes.append((path, dict(tags))) or True
    )
    return writes


class TestTheRepairIsWiredIn:
    def test_the_scan_repairs_each_album_after_the_batch(self):
        import inspect

        import services.scanning.navidrome_import as imp

        source = inspect.getsource(imp)
        assert "unify_album_release_identity" in source
        assert "_bulk_ok" in source

    def test_the_album_save_repairs_a_malformed_release_id(self):
        import inspect

        import routes.ui_routes as ui

        source = inspect.getsource(ui)
        assert "_album_mbid_is_group" in source
        assert "unify_album_release_identity" in source


class TestOneReleaseForTheAlbum:
    def test_the_majority_release_wins_and_the_malformed_row_is_repaired(
        self, db_session, tag_writes
    ):
        _seed(db_session, "t1", CANADA, barcode="074646148822", country="Canada")
        _seed(db_session, "t2", CANADA, barcode="074646148822", country="Canada")
        # The malformed row's group was cleared by the import's duplicate guard.
        _seed(db_session, "t3", GROUP, group="")

        out = svc.unify_album_release_identity(ARTIST, ALBUM)

        assert out["release_id"] == CANADA
        assert out["rows_updated"] == 3
        assert _ids(db_session) == {CANADA}
        groups = {
            str(row[0] or "")
            for row in db_session.execute(
                text("SELECT musicbrainz_releasegroupid FROM tracks")
            ).fetchall()
        }
        assert groups == {GROUP}, "the valid release GROUP must be restored"
        assert tag_writes, "the files must be repaired too, not just the DB"
        assert all(tags["musicbrainz_albumid"] == CANADA for _p, tags in tag_writes)
        assert all(tags.get("barcode") == "074646148822" for _p, tags in tag_writes)

    def test_the_edition_matching_the_local_tracklist_wins(
        self, db_session, tag_writes, monkeypatch
    ):
        """The Japanese bonus track means the 15-track edition is canonical."""
        _seed(db_session, "t1", CANADA)
        _seed(db_session, "t2", CANADA)
        _seed(db_session, "t15", JAPAN, barcode="4547366004366", country="Japan")

        monkeypatch.setattr(
            svc, "_choose_by_track_count", lambda group, count: [JAPAN],
        )

        out = svc.unify_album_release_identity(ARTIST, ALBUM)

        assert out["release_id"] == JAPAN
        assert _ids(db_session) == {JAPAN}
        assert all(tags.get("releasecountry") == "Japan" for _p, tags in tag_writes)

    def test_an_explicit_album_page_choice_wins(self, db_session, tag_writes):
        _seed(db_session, "t1", CANADA)
        _seed(db_session, "t2", CANADA)
        _seed(db_session, "t15", JAPAN)

        out = svc.unify_album_release_identity(
            ARTIST, ALBUM, preferred_release_mbid=JAPAN,
        )

        assert out["release_id"] == JAPAN
        assert _ids(db_session) == {JAPAN}

    def test_two_release_families_are_never_merged(self, db_session, tag_writes):
        _seed(db_session, "t1", CANADA)
        _seed(db_session, "t2", "99999999-1111-4222-8333-944455556666",
              group="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")

        out = svc.unify_album_release_identity(ARTIST, ALBUM)

        assert out["skipped"] == "multiple_groups"
        assert len(_ids(db_session)) == 2

    def test_a_consistent_album_is_a_no_op(self, db_session, tag_writes):
        _seed(db_session, "t1", CANADA)
        _seed(db_session, "t2", CANADA)

        out = svc.unify_album_release_identity(ARTIST, ALBUM)

        assert out["skipped"] == "already_consistent"
        assert tag_writes == []

    def test_every_row_malformed_still_resolves_through_musicbrainz(
        self, db_session, tag_writes, monkeypatch
    ):
        _seed(db_session, "t1", GROUP)
        _seed(db_session, "t2", GROUP)

        monkeypatch.setattr(
            svc, "_choose_by_track_count", lambda group, count: [CANADA],
        )

        out = svc.unify_album_release_identity(ARTIST, ALBUM)

        assert out["release_id"] == CANADA
        assert _ids(db_session) == {CANADA}
