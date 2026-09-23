"""Regression: confirming a folder match must FETCH the MusicBrainz release
metadata and match it to the folder's tracks BEFORE importing them.

``match_folder_to_release`` resolved the release, then read each file's OWN
tags and moved them — it never fetched the release's tracklist and never wrote
MusicBrainz values to the files.  ``move_track_to_library`` only builds a
destination path and ``shutil.move``s the file; it writes no tags at all.  So a
release picked in Matched & Unmatched Folders was filed under whatever the
existing tags said.

The internal fallback loop that was supposed to cover this could never run:

1. Its keys were ``title``/``number``, but the MusicBrainz tracks it iterated
   come from ``inc=recordings+media`` and carry ``title``/``position`` — and
   the surrounding ``if not title or number is None`` only ever ran for files
   whose ``track_number`` was None, in which case ``number`` stayed None and
   every comparison below it failed.
2. No MusicBrainz track ever carried a ``number`` key at all, so even a file
   with NO title matched nothing.

Fix: ``match_mb_tracks_to_files`` (services/enrichment/musicbrainz_service.py)
pairs the release tracklist with the folder's files by track number first and
normalized-title similarity second; ``_apply_release_metadata_to_files`` writes
the matched MusicBrainz values to the files' tags BEFORE the move, and the move
step then reuses those same values so the tags, the DB row and the filename all
agree.
"""

from __future__ import annotations

import os
import types

import pytest

MBID = "11111111-2222-3333-4444-555555555555"
RGID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# The shape ``fetch_musicbrainz_release_metadata`` returns for a release fetched
# with ``inc=recordings+media+artist-credits``: each track carries BOTH the plain
# keys and the ``mb_*`` keys, and the medium/track positions are nested one level
# down (not flattened onto the track).
RELEASE_METADATA = {
    "release_mbid": MBID,
    "release_group_mbid": RGID,
    "release_group_title": "I Remember You",
    "release_title": "I Remember You",
    "artist": "Rebecca Black",
    "release_year": "2026",
    "original_year": "2026",
    "tracks": [
        {
            "title": "I Remember You",
            "artist": "Rebecca Black",
            "track_number": 1,
            "disc_number": 1,
            "recording_mbid": "rec-0001",
            "duration": 215000,
            "mb_title": "I Remember You",
            "mb_track_number": 1,
            "mb_disc_number": 1,
            "mb_recording_mbid": "rec-0001",
            "mb_duration": 215000,
            "musicbrainz_genres": "pop",
            "writer": "Rebecca Black",
            "work_mbid": "work-0001",
        },
        {
            "title": "BiiiG",
            "artist": "BIGBANG",
            "track_number": 2,
            "disc_number": 1,
            "recording_mbid": "rec-0002",
            "duration": 199000,
            "mb_title": "BiiiG",
            "mb_track_number": 2,
            "mb_disc_number": 1,
            "mb_recording_mbid": "rec-0002",
            "mb_duration": 199000,
            "is_cover": True,
            "original_cover_artist": "Someone Else",
        },
    ],
}


def _mb():
    import services.enrichment.musicbrainz_service as mbs

    return mbs


# ===========================================================================
# match_mb_tracks_to_files
# ===========================================================================


class TestMatchMbTracksToFiles:
    def test_matches_by_track_number(self):
        """A file with a matching track number takes the MB values."""
        entries = _mb().match_mb_tracks_to_files(
            RELEASE_METADATA,
            [
                {"file_path": "/d/01 intro.flac", "track_number": 1, "title": "garbage"},
                {"file_path": "/d/02 two.flac", "track_number": 2, "title": "garbage too"},
            ],
        )
        assert len(entries) == 2
        assert [e["matched"] for e in entries] == [True, True]
        assert entries[0]["title"] == "I Remember You"
        assert entries[0]["track_number"] == 1
        assert entries[1]["title"] == "BiiiG"

    def test_matches_by_title_when_no_track_number(self):
        """A file with NO track number still matches on its title.

        This is the reported case: the folder's files were untagged/unordered,
        so the track number could not identify them and the old code gave up
        (``number`` stayed None and every MusicBrainz track lacked a ``number``
        key, so the loop matched nothing).
        """
        entries = _mb().match_mb_tracks_to_files(
            RELEASE_METADATA,
            [{"file_path": "/d/BiiiG.m4a", "track_number": None, "title": "BiiiG"}],
        )
        assert entries[0]["matched"] is False  # track 1 not present locally
        assert entries[1]["matched"] is True
        assert entries[1]["file_path"] == "/d/BiiiG.m4a"
        assert entries[1]["track_number"] == 2

    def test_reports_unmatched_mb_tracks(self):
        """An MB track with no local counterpart is reported as unmatched."""
        entries = _mb().match_mb_tracks_to_files(
            RELEASE_METADATA,
            [{"file_path": "/d/01.flac", "track_number": 1, "title": "I Remember You"}],
        )
        assert entries[0]["matched"] is True
        assert entries[1]["matched"] is False
        assert entries[1]["mb_title"] == "BiiiG"

    def test_ignores_extra_local_files(self):
        """A local file matching no MB track is NOT given invented metadata."""
        entries = _mb().match_mb_tracks_to_files(
            RELEASE_METADATA,
            [
                {"file_path": "/d/01.flac", "track_number": 1, "title": "I Remember You"},
                {"file_path": "/d/extra.flac", "track_number": 9, "title": "Totally Different Song"},
            ],
        )
        matched_paths = [e.get("file_path") for e in entries if e.get("matched")]
        assert "/d/extra.flac" not in matched_paths
        assert len(entries) == 2  # one entry per MB track, never per local file

    def test_no_metadata_returns_empty(self):
        assert _mb().match_mb_tracks_to_files(None, [{"file_path": "x"}]) == []
        assert _mb().match_mb_tracks_to_files({}, [{"file_path": "x"}]) == []
        assert _mb().match_mb_tracks_to_files(RELEASE_METADATA, []) == []

    def test_disc_number_not_forced_to_one(self):
        """A 2-disc release must not collapse both discs onto disc 1.

        ``helpers.metadata_reader`` never populates ``disc_number`` for FLAC or
        MP3, so a strict disc comparison would reject every disc-2 file.  A
        file with a blank disc is treated as a wildcard; an explicit disc still
        has to agree.
        """
        metadata = dict(RELEASE_METADATA)
        metadata["tracks"] = [
            {"mb_title": "Disc One Song", "mb_track_number": 1, "mb_disc_number": 1},
            {"mb_title": "Disc Two Song", "mb_track_number": 1, "mb_disc_number": 2},
        ]
        entries = _mb().match_mb_tracks_to_files(
            metadata,
            [
                {"file_path": "/d/d1.flac", "track_number": 1, "disc_number": 1, "title": "Disc One Song"},
                {"file_path": "/d/d2.flac", "track_number": 1, "disc_number": 2, "title": "Disc Two Song"},
            ],
        )
        assert entries[0]["file_path"] == "/d/d1.flac"
        assert entries[1]["file_path"] == "/d/d2.flac"

    def test_carries_album_context_and_enrichment(self):
        """The matched entry is shaped for ``update_file_metadata``.

        Album identity comes from the RELEASE (not the file's own tags) and the
        per-recording enrichment (genres / writer / cover attribution) rides
        along so the file keeps the metadata it was matched with.
        """
        entries = _mb().match_mb_tracks_to_files(
            RELEASE_METADATA,
            [{"file_path": "/d/02.flac", "track_number": 2, "title": "BiiiG"}],
        )
        entry = entries[1]
        assert entry["title"] == "BiiiG"
        assert entry["artist"] == "BIGBANG"
        assert entry["album"] == "I Remember You"
        assert entry["album_artist"] == "Rebecca Black"
        assert entry["track_number"] == 2
        assert entry["disc_number"] == 1
        assert entry["recording_mbid"] == "rec-0002"
        assert entry["release_mbid"] == MBID
        assert entry["is_cover"] is True
        assert entry["original_cover_artist"] == "Someone Else"
        assert entry["release_year"] == "2026"
        assert entry["originalyear"] == "2026"

    def test_carries_writer_and_genres(self):
        entries = _mb().match_mb_tracks_to_files(
            RELEASE_METADATA,
            [{"file_path": "/d/01.flac", "track_number": 1, "title": "I Remember You"}],
        )
        entry = entries[0]
        assert entry["writer"] == "Rebecca Black"
        assert entry["work_mbid"] == "work-0001"
        assert entry["musicbrainz_genres"] == "pop"


# ===========================================================================
# _apply_release_metadata_to_files
# ===========================================================================


class TestApplyReleaseMetadataToFiles:
    def test_writes_mb_values_to_each_file(self, monkeypatch):
        from services.downloads import download_folder_service as dfs

        written: dict[str, dict] = {}

        def _fake_update(path, meta):
            written[path] = dict(meta)
            return True

        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_metadata", _fake_update
        )
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
            lambda release_id: RELEASE_METADATA,
        )

        applied = dfs._apply_release_metadata_to_files(
            files=[
                {"file_path": "/d/01.flac", "track_number": 1, "title": "track one"},
                {"file_path": "/d/02.flac", "track_number": 2, "title": "track two"},
            ],
            album_artist="Rebecca Black",
            album="I Remember You",
            year="2026",
            release_mbid=MBID,
        )

        assert set(written) == {"/d/01.flac", "/d/02.flac"}
        assert written["/d/01.flac"]["title"] == "I Remember You"
        assert written["/d/02.flac"]["title"] == "BiiiG"
        assert written["/d/02.flac"]["track_number"] == 2
        # The applied map is returned so the move step can reuse the SAME
        # values instead of re-reading (and possibly re-inventing) them.
        assert applied["/d/02.flac"]["title"] == "BiiiG"

    def test_failed_tag_write_is_not_returned_as_applied(self, monkeypatch):
        """A file whose tag write failed keeps its own values for the move."""
        from services.downloads import download_folder_service as dfs

        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_metadata",
            lambda path, meta: path != "/d/02.flac",
        )
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
            lambda release_id: RELEASE_METADATA,
        )

        applied = dfs._apply_release_metadata_to_files(
            files=[
                {"file_path": "/d/01.flac", "track_number": 1, "title": "a"},
                {"file_path": "/d/02.flac", "track_number": 2, "title": "b"},
            ],
            album_artist="Rebecca Black",
            album="I Remember You",
            year="2026",
            release_mbid=MBID,
        )
        assert "/d/01.flac" in applied
        assert "/d/02.flac" not in applied

    def test_returns_empty_when_mb_fetch_fails(self, monkeypatch):
        """No release metadata -> no tag writes, and the move proceeds as before."""
        from services.downloads import download_folder_service as dfs

        calls: list = []
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
            lambda release_id: None,
        )
        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_metadata",
            lambda path, meta: calls.append(path) or True,
        )

        applied = dfs._apply_release_metadata_to_files(
            files=[{"file_path": "/d/01.flac", "track_number": 1, "title": "a"}],
            album_artist="A", album="B", year="2026", release_mbid=MBID,
        )
        assert applied == {}
        assert calls == []

    def test_no_release_mbid_does_not_fetch(self, monkeypatch):
        from services.downloads import download_folder_service as dfs

        def _boom(release_id):
            raise AssertionError("must not fetch without a release MBID")

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata", _boom
        )
        assert dfs._apply_release_metadata_to_files(
            files=[{"file_path": "/d/01.flac"}],
            album_artist="A", album="B", year="2026", release_mbid="",
        ) == {}


# ===========================================================================
# match_folder_to_release end-to-end
# ===========================================================================


@pytest.fixture
def folder_env(tmp_path, monkeypatch):
    """Wire match_folder_to_release to a temp folder with no real I/O."""
    from services.downloads import download_folder_service as dfs

    downloads = tmp_path / "downloads"
    folder = downloads / "BIGBANG - BiiiG"
    folder.mkdir(parents=True)
    (folder / "01.flac").write_bytes(b"x")
    (folder / "02.flac").write_bytes(b"x")

    monkeypatch.setattr(dfs, "resolve_downloads_dir", lambda: str(downloads))
    monkeypatch.setattr(dfs, "resolve_original_archive_dir", lambda: str(tmp_path / "orig"))
    monkeypatch.setattr(dfs, "_assert_single_album_folder", lambda *a, **k: None)
    monkeypatch.setattr(dfs, "is_path_under_directory", lambda p, d: True)
    monkeypatch.setattr(dfs, "_resolve_release", lambda client, mb_id: ({"id": MBID}, MBID))

    # ``MusicBrainzHttpClient`` is imported INSIDE match_folder_to_release, so
    # the attribute lives on the source module, not on the service.
    monkeypatch.setattr(
        "api_clients.musicbrainz_http.MusicBrainzHttpClient", lambda *a, **k: object()
    )
    # folder cleanup must not remove the temp dir mid-assert
    monkeypatch.setattr(dfs, "shutil", types.SimpleNamespace(rmtree=lambda *a, **k: None))
    monkeypatch.setattr(
        "db.repositories.folder_match_repository.delete_folder_match", lambda *a, **k: True
    )
    monkeypatch.setattr("helpers.config_helpers.get_config", lambda: {"music": {"root": str(tmp_path / "music")}})
    monkeypatch.setattr(
        "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
        lambda release_id: RELEASE_METADATA,
    )

    # The reported shape: the files carry NO track number (helpers.metadata_reader
    # only surfaces one for files that have a TRACKNUMBER tag), so their title is
    # the only thing to match on — and it is not canonical (lowercase here), so the
    # assertion that the MusicBrainz title replaced it is meaningful.
    _own_titles = {"01.flac": "i remember you", "02.flac": "BiiiG"}
    monkeypatch.setattr(
        "helpers.metadata_reader.read_mp3_metadata",
        lambda path: {
            "artist": "BIGBANG",
            "album": "BiiiG",
            "title": _own_titles.get(os.path.basename(path), ""),
        },
    )
    monkeypatch.setattr(
        dfs,
        "_get_files_in_folder",
        lambda p, **k: [
            {"name": "01.flac", "is_audio": True},
            {"name": "02.flac", "is_audio": True},
        ],
    )
    return dfs, folder


class TestMatchFolderToReleaseAppliesMbMetadata:
    def test_tags_are_written_from_the_release_before_moving(self, folder_env, monkeypatch):
        """THE REPORTED BUG: the release's metadata must reach the files.

        Before the fix no ``update_file_metadata`` call happened at all on this
        path, so the imported files carried whatever they already had.
        """
        dfs, folder = folder_env

        events: list[tuple] = []

        def _fake_update(path, meta):
            events.append(("tag", path, meta.get("title"), meta.get("track_number")))
            return True

        def _fake_move(track, release_metadata, music_root):
            events.append(("move", track["title"], track.get("track_number")))
            return {"success": True, "target_path": f"{music_root}/{track['title']}.flac"}

        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_metadata", _fake_update
        )
        monkeypatch.setattr(
            "services.downloads.download_organize_helpers.move_track_to_library", _fake_move
        )
        monkeypatch.setattr(dfs, "_link_moved_file_to_queue_item", lambda **k: True)

        result = dfs.match_folder_to_release(str(folder), MBID)

        assert result["success"] is True, result
        assert result["metadata_updated"] == 2

        tag_titles = [e[2] for e in events if e[0] == "tag"]
        assert tag_titles == ["I Remember You", "BiiiG"]

        # Tags MUST be written before the file moves: the moved file has to
        # already carry the metadata, and the destination path is built from
        # the SAME values (so path and tags cannot disagree).
        kinds = [e[0] for e in events]
        assert kinds == ["tag", "tag", "move", "move"], kinds

        move_titles = [e[1] for e in events if e[0] == "move"]
        assert move_titles == ["I Remember You", "BiiiG"]
        move_numbers = [e[2] for e in events if e[0] == "move"]
        assert move_numbers == [1, 2]

    def test_falls_back_to_own_tags_when_mb_has_no_match(self, folder_env, monkeypatch):
        """A file matching no MB track keeps its own title — never invented."""
        dfs, folder = folder_env

        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_metadata", lambda p, m: True
        )
        monkeypatch.setattr(
            "helpers.metadata_reader.read_mp3_metadata",
            lambda path: {"artist": "BIGBANG", "album": "BiiiG", "title": "My Own Title", "track_number": 7},
        )
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
            lambda release_id: {"tracks": [{"mb_title": "Something Else", "mb_track_number": 99}]},
        )

        moved: list[tuple] = []

        def _fake_move(track, release_metadata, music_root):
            moved.append((track["title"], track.get("track_number")))
            return {"success": True, "target_path": "/music/x.flac"}

        monkeypatch.setattr(
            "services.downloads.download_organize_helpers.move_track_to_library", _fake_move
        )
        monkeypatch.setattr(dfs, "_link_moved_file_to_queue_item", lambda **k: True)

        result = dfs.match_folder_to_release(str(folder), MBID)
        assert result["success"] is True
        assert moved[0] == ("My Own Title", 7)

    def test_survives_musicbrainz_failure(self, folder_env, monkeypatch):
        """A failed MB fetch must not abort the import."""
        dfs, folder = folder_env

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
            lambda release_id: None,
        )
        monkeypatch.setattr(
            "helpers.metadata_reader.read_mp3_metadata",
            lambda path: {"artist": "BIGBANG", "title": "Fallback Title", "track_number": 1},
        )
        monkeypatch.setattr(
            "services.downloads.download_organize_helpers.move_track_to_library",
            lambda track, release_metadata, music_root: {
                "success": True, "target_path": "/music/x.flac"
            },
        )
        monkeypatch.setattr(dfs, "_link_moved_file_to_queue_item", lambda **k: True)

        result = dfs.match_folder_to_release(str(folder), MBID)
        assert result["success"] is True
        assert result["moved"] == 2
        assert result["metadata_updated"] == 0
