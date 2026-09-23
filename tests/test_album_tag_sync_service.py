"""Tests for the end-of-album file-tag fill + correction recording service.

Covers ``album_tag_sync_service``: at the end of an album metadata scan,
MISSING file tags are filled from the freshly scanned DB values (MBIDs only
when the album tracklist perfectly matches the MusicBrainz release), and
per-track corrections are recorded for file values that differ from what the
scan resolved.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def _tagging_on(monkeypatch):
    monkeypatch.setattr(
        "helpers.config_helpers.get_tagging_config",
        lambda: {"sync_album_tags_on_scan": True, "embed_lyrics": False},
    )


def _track(**overrides):
    track = {
        "id": "t1", "title": "Song One", "artist": "Artist", "album": "Album",
        "album_artist": "Artist", "year": "2020", "track_number": "1",
        "disc_number": "1", "genres": "Rock, Metal", "isrc": "US123",
        "writer": '["Writer One", "Writer Two"]', "lyrics": None,
        "file_path": "/tmp/1.mp3",
        "recording_mbid": "rec-1", "musicbrainz_albumid": "rel-1",
        "musicbrainz_releasegroupid": "rg-1", "musicbrainz_artistid": "art-1",
        "musicbrainz_releasetrackid": "rt-1", "musicbrainz_workid": "work-1",
    }
    track.update(overrides)
    return track


def test_db_tag_candidates_perfect_includes_mbids(_tagging_on):
    from services.metadata import album_tag_sync_service as svc

    cands = svc._db_tag_candidates(_track(), perfect=True, include_lyrics=True)
    assert cands["title"] == "Song One"
    assert cands["artist"] == "Artist"
    assert cands["composer"] == "Writer One, Writer Two"
    assert cands["genres"] == "Rock, Metal"
    assert cands["musicbrainz_trackid"] == "rec-1"
    assert cands["musicbrainz_albumid"] == "rel-1"
    assert cands["musicbrainz_releasegroupid"] == "rg-1"
    assert cands["musicbrainz_artistid"] == "art-1"
    assert cands["musicbrainz_releasetrackid"] == "rt-1"
    assert cands["musicbrainz_workid"] == "work-1"
    # No lyrics stored → key absent (not an empty string).
    assert "lyrics" not in cands


def test_db_tag_candidates_imperfect_excludes_mbids(_tagging_on):
    """Without a perfect tracklist match, MBIDs must never be candidates —
    a bad MB match can't stamp wrong IDs into the files."""
    from services.metadata import album_tag_sync_service as svc

    cands = svc._db_tag_candidates(_track(), perfect=False, include_lyrics=False)
    assert cands["title"] == "Song One"
    assert cands["isrc"] == "US123"
    for key in (
        "musicbrainz_trackid", "musicbrainz_albumid", "musicbrainz_releasegroupid",
        "musicbrainz_artistid", "musicbrainz_releasetrackid", "musicbrainz_workid",
    ):
        assert key not in cands


def test_is_perfect_match():
    from services.metadata import album_tag_sync_service as svc

    tracks = [
        {"track_number": "1", "disc_number": "1"},
        {"track_number": "2", "disc_number": "1"},
    ]
    index = {(1, 1): {"recording_mbid": "r1"}, (1, 2): {"recording_mbid": "r2"}}
    # 1:1 mapping with equal track count → perfect.
    assert svc._is_perfect_match(tracks, index, 2) is True
    # Local track with no MB slot → not perfect.
    tracks.append({"track_number": "3", "disc_number": "1"})
    assert svc._is_perfect_match(tracks, index, 2) is False
    # Missing MB slot for a local position → not perfect.
    assert svc._is_perfect_match(tracks[:2], {(1, 1): {}}, 2) is False


def test_sync_fills_missing_and_records_corrections(monkeypatch, _tagging_on):
    """End-to-end: a file missing fields gets them filled (MBIDs included on
    a perfect match); a file holding a wrong value records a correction
    instead of being overwritten."""
    from services.metadata import album_tag_sync_service as svc

    tracks = [
        _track(id="t1", title="Song One"),
        _track(
            id="t2", title="Song Two", track_number="2", recording_mbid="rec-2",
            genres="", isrc="", writer=None, file_path="/tmp/2.mp3",
        ),
    ]
    monkeypatch.setattr(svc, "_load_fresh_tracks", lambda artist, album: tracks)
    monkeypatch.setattr(
        svc, "_resolve_mb_release",
        lambda tracks: ("rel-1", {(1, 1): {}, (1, 2): {}}, 2),
    )
    monkeypatch.setattr(
        svc, "_read_file_values",
        lambda path: {
            "title": "Song One", "artist": "Artist", "album": "Album",
            "year": "2020", "track_number": "1", "disc_number": "1",
            "genres": "Rock, Metal",
        }
        if path.endswith("1.mp3")
        else {"title": "Old Wrong Title"},
    )
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "services.metadata.tag_file_service.write_tags_to_file",
        lambda path, tags: written.append((path, tags)) or True,
    )
    # The DB stores paths that may be RELATIVE to the music root, so the sync
    # resolves each one before writing. Without patching the resolver, a fake
    # "/tmp/1.mp3" does not exist on disk and the write is (correctly) skipped —
    # which is the behaviour under test elsewhere, not this test's subject.
    monkeypatch.setattr(
        "services.metadata.tag_file_service.resolve_music_file_path",
        lambda p: str(p) if p else None,
    )
    recorded: list[dict] = []
    monkeypatch.setattr(
        "services.metadata.conflict_service.detect_and_record_conflicts",
        lambda **kw: recorded.append(kw)
        or {"conflicts_recorded": 1, "safe_updates": [], "blocked_fields": []},
    )
    monkeypatch.setattr("os.path.exists", lambda p: True)

    result = svc.sync_album_file_tags("Artist", "Album")

    assert result["perfect_match"] is True
    fills = dict(written)

    # t1's file already has title/artist/album/year/track/disc/genres — only
    # the missing album_artist, ISRC and MBIDs are filled (perfect match).
    t1_fill = fills["/tmp/1.mp3"]
    assert "musicbrainz_trackid" in t1_fill
    assert "musicbrainz_albumid" in t1_fill
    assert "musicbrainz_releasegroupid" in t1_fill
    assert t1_fill["isrc"] == "US123"
    assert "title" not in t1_fill  # never overwrite an existing value

    # t2's file only has a (wrong) title → everything else is filled.
    t2_fill = fills["/tmp/2.mp3"]
    assert t2_fill["artist"] == "Artist"
    assert t2_fill["album"] == "Album"
    assert "title" not in t2_fill  # file value present → not overwritten

    # t2's wrong title differs from the scan-resolved value → correction.
    assert recorded
    assert recorded[0]["track_id"] == "t2"
    assert recorded[0]["local_data"].get("title") == "Old Wrong Title"
    assert recorded[0]["remote_data"].get("title") == "Song Two"
    assert result["corrections_recorded"] == 1
    assert result["files_updated"] == 2


def test_sync_skipped_when_feature_disabled(monkeypatch):
    from services.metadata import album_tag_sync_service as svc

    monkeypatch.setattr(
        "helpers.config_helpers.get_tagging_config",
        lambda: {"sync_album_tags_on_scan": False, "embed_lyrics": False},
    )
    result = svc.sync_album_file_tags("Artist", "Album")
    assert result["skipped"] == "feature_disabled"
    assert result["files_updated"] == 0
    assert result["corrections_recorded"] == 0


def test_sync_noop_when_no_tracks(monkeypatch, _tagging_on):
    from services.metadata import album_tag_sync_service as svc

    monkeypatch.setattr(svc, "_load_fresh_tracks", lambda artist, album: [])
    result = svc.sync_album_file_tags("Artist", "Album")
    assert result["skipped"] == "no_tracks"


def test_mp3_writer_frame_map_has_new_frames():
    """The MP3 writer must map ISRC / lyrics / MBID fields to real frames so
    the tag-sync service can actually write them."""
    from services.metadata import tag_file_service as tfs

    assert tfs._MP3_FRAME_FOR_FIELD["isrc"] == "TSRC"
    assert tfs._MP3_FRAME_FOR_FIELD["lyrics"] == "USLT"
    assert tfs._MP3_FRAME_FOR_FIELD["musicbrainz_trackid"] == "TXXX"
    assert tfs._MP3_FRAME_FOR_FIELD["musicbrainz_releasegroupid"] == "TXXX"
    assert tfs._MP3_FRAME_FOR_FIELD["musicbrainz_artistid"] == "TXXX"
    assert tfs._MP3_FRAME_FOR_FIELD["musicbrainz_releasetrackid"] == "TXXX"
    assert tfs._MP3_FRAME_FOR_FIELD["musicbrainz_workid"] == "TXXX"
    # TXXX frames must be description-aware in the fill-missing pre-check, and
    # the description must be the CANONICAL name (services.metadata.tag_names)
    # — a hand-written desc here is how one field acquired two spellings.
    from services.metadata.tag_names import canonical_tag_name as _canonical_tag_name

    assert tfs._MB_TXXX_DESC["musicbrainz_releasegroupid"] == _canonical_tag_name("musicbrainz_releasegroupid")
    assert tfs._MB_TXXX_DESC["musicbrainz_releasegroupid"] == "MUSICBRAINZ_RELEASEGROUPID"
