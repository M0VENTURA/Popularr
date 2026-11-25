"""The release-group id must never be written as the release id.

The audit suspected a fallback shape ``album_id = release_id or release_group_id``.
The writer that could actually re-introduce it was the scan's dominant-id
fan-out: it counted a malformed row (``musicbrainz_albumid ==
musicbrainz_releasegroupid``) as a candidate and then wrote that GROUP id onto
every file — undoing a correct merge after the next scan.

Also: a recording id can live in BOTH an ID3 ``TXXX:MUSICBRAINZ_TRACKID`` frame
and a MusicBrainz ``UFID`` frame; ffprobe shows both as ``musicbrainz_trackid``,
which looked like two recording MBIDs on one file.
"""

from __future__ import annotations

import importlib

import pytest

from services.metadata.tag_names import clear_id3_variants, clear_keys_for

GROUP = "4041334a-fdca-3a73-878e-8513b464a88d"
RELEASE = "3c8d8b38-ac3b-480c-87f8-5048a6198ff5"
OTHER = "2606ffe5-3d2d-4549-8fa3-14e3ed6b1cfd"


@pytest.fixture()
def sync():
    return importlib.import_module("services.metadata.album_tag_sync_service")


class TestTheDominantIdIgnoresMalformedRows:
    def test_a_group_id_in_the_release_frame_is_not_a_candidate(self, sync):
        tracks = [
            {"musicbrainz_albumid": RELEASE, "musicbrainz_releasegroupid": GROUP},
            {"musicbrainz_albumid": GROUP, "musicbrainz_releasegroupid": GROUP},
        ]

        assert sync._dominant_album_id(tracks) == RELEASE

    def test_an_album_of_only_malformed_rows_has_no_dominant_id(self, sync):
        tracks = [
            {"musicbrainz_albumid": GROUP, "musicbrainz_releasegroupid": GROUP},
            {"musicbrainz_album_mbid": GROUP, "musicbrainz_releasegroupid": GROUP},
        ]

        assert sync._dominant_album_id(tracks) == "", (
            "with no valid release id the scan must not fan one out at all"
        )

    def test_a_locally_corrected_row_outranks_the_majority(self, sync):
        tracks = [
            {"musicbrainz_albumid": OTHER, "musicbrainz_releasegroupid": GROUP},
            {"musicbrainz_albumid": OTHER, "musicbrainz_releasegroupid": GROUP},
            {
                "musicbrainz_albumid": RELEASE,
                "musicbrainz_releasegroupid": GROUP,
                "metadata_dirty_for_navidrome": True,
            },
        ]

        assert sync._dominant_album_id(tracks) == RELEASE

    def test_the_file_tags_never_carry_group_as_release(self, sync):
        track = {
            "id": "t1", "title": "Song", "album": "Untouchables",
            "musicbrainz_albumid": GROUP,
            "musicbrainz_releasegroupid": GROUP,
            "musicbrainz_artistid": "artist-1",
        }

        tags = sync._db_tag_candidates(track, perfect=True)

        assert "musicbrainz_albumid" not in tags
        assert tags.get("musicbrainz_releasegroupid") == GROUP


class TestTheImportCannotSkipARegroupedAlbum:
    def test_a_changed_album_identity_forces_a_reimport(self):
        import inspect

        import services.scanning.navidrome_import as imp

        source = inspect.getsource(imp)
        assert "album_identity_changed=_album_identity_changed" in source, (
            "the import must not skip an album whose Navidrome release id "
            "changed — the songs' fingerprint cannot see it, and the stale DB "
            "id is what the next tag sync writes back over the files"
        )


class TestTheApplyEndpointsRefuseTheGroupId:
    def test_artist_apply_refuses_when_it_cannot_resolve_a_release(
        self, monkeypatch
    ):
        artist_service = importlib.import_module("services.metadata.artist_service")
        mbs = importlib.import_module("services.enrichment.musicbrainz_service")

        monkeypatch.setattr(mbs, "resolve_release_id", lambda rid: rid)

        payload, code = artist_service.apply_album_mbid({
            "artist": "Korn", "album": "Untouchables",
            "mbid": GROUP, "release_group_mbid": GROUP,
        })

        assert code == 409
        assert "GROUP" in payload["error"]

    def test_update_album_ids_refuses_when_it_cannot_resolve_a_release(
        self, monkeypatch
    ):
        album_service = importlib.import_module("services.metadata.album_service")
        mbs = importlib.import_module("services.enrichment.musicbrainz_service")

        monkeypatch.setattr(mbs, "resolve_release_id", lambda rid: "")

        payload, code = album_service.update_album_ids({
            "artist": "Korn", "album": "Untouchables",
            "musicbrainz_release_id": GROUP,
            "musicbrainz_release_group_id": GROUP,
        })

        assert code == 409

    def test_artist_apply_uses_the_resolved_release(self, monkeypatch):
        artist_service = importlib.import_module("services.metadata.artist_service")
        mbs = importlib.import_module("services.enrichment.musicbrainz_service")
        applied: dict = {}

        monkeypatch.setattr(mbs, "resolve_release_id", lambda rid: RELEASE)
        monkeypatch.setattr(
            artist_service, "is_album_mbid_guard_enabled", lambda: False,
        )

        def _fake_update(*args, **kwargs):
            applied["mbid"] = args[3]
            return 0

        monkeypatch.setattr(
            artist_service, "update_album_mbid_fields", _fake_update,
        )

        payload, code = artist_service.apply_album_mbid({
            "artist": "Korn", "album": "Untouchables",
            "mbid": GROUP, "release_group_mbid": GROUP,
        })

        assert code == 200, payload
        assert applied["mbid"] == RELEASE


class TestOneRecordingIdPerFile:
    def test_recording_id_synonyms_are_cleared(self):
        keys = clear_keys_for("musicbrainz_trackid")
        assert "musicbrainzrecordingid" in keys

    def test_a_musicbrainz_ufid_is_removed_with_the_txxx_write(self):
        class _FakeId3(dict):
            def delall(self, key):
                self.pop(key, None)

        tags = _FakeId3({
            "TXXX:MUSICBRAINZ TRACK ID": object(),   # stale Picard spelling
            "TXXX:MUSICBRAINZ_TRACKID": object(),    # canonical, being rewritten
            "UFID:http://musicbrainz.org": object(),  # also a recording id
            "UFID:http://example.com": object(),      # unrelated, keep
        })

        canonical = clear_id3_variants(tags, "musicbrainz_trackid")

        assert canonical == "MUSICBRAINZ_TRACKID"
        assert list(tags) == ["UFID:http://example.com"]


class TestTheDownloadWriterRefusesTheGroupId:
    def test_update_file_metadata_does_not_write_group_as_release(
        self, monkeypatch
    ):
        tfs = importlib.import_module("services.metadata.tag_file_service")
        written: dict = {}

        monkeypatch.setattr(
            tfs, "write_tags_to_file",
            lambda path, tags: written.update(tags) or True,
        )

        tfs.update_file_metadata(
            "/music/Korn/x.mp3",
            {
                "title": "Song",
                "release_mbid": GROUP,
                "musicbrainz_releasegroupid": GROUP,
                "recording_mbid": "rec-1",
            },
        )

        assert "musicbrainz_albumid" not in written
        assert written.get("musicbrainz_trackid") == "rec-1"
