"""Freshness, metadata fingerprints and Navidrome song-ID reconciliation."""

from __future__ import annotations

from services.scanning import reconciliation as rec
from services.scanning.filters import should_skip_cached_album


def _navidrome_track(**overrides):
    track = {
        "id": "new-1",
        "path": "Artist/Album/01 - Song.flac",
        "title": "Song",
        "artist": "Artist",
        "albumArtist": "Artist",
        "year": "2025",
        "trackNumber": 1,
        "discNumber": 1,
        "duration": 200,
        "musicBrainzId": "rec-1",
        "genre": "Rock",
    }
    track.update(overrides)
    return track


def _db_row(**overrides):
    row = {
        "id": "new-1",
        "file_path": "Artist/Album/01 - Song.flac",
        "title": "Song",
        "artist": "Artist",
        "album_artist": "Artist",
        "album": "Album",
        "year": "2025",
        "track_number": "1",
        "disc_number": "1",
        "duration": 200.0,
        "recording_mbid": "rec-1",
        "navidrome_genres": '["rock"]',
    }
    row.update(overrides)
    return row


class TestMetadataFingerprint:
    def test_same_ids_and_metadata_fingerprint_match(self):
        incoming = rec.navidrome_album_fingerprint(
            [_navidrome_track()], album_name="Album", album_artist="Artist"
        )
        cached = rec.db_album_fingerprint([_db_row()])
        assert incoming == cached

    def test_same_ids_changed_title_fingerprint_differs(self):
        incoming = rec.navidrome_album_fingerprint(
            [_navidrome_track(title="Corrected Song")],
            album_name="Album", album_artist="Artist",
        )
        cached = rec.db_album_fingerprint([_db_row(title="Old Song")])
        assert incoming != cached

    def test_same_ids_changed_album_or_year_differs(self):
        base = rec.db_album_fingerprint([_db_row()])
        assert rec.db_album_fingerprint([_db_row(album="Different")]) != base
        assert rec.db_album_fingerprint([_db_row(year="1999")]) != base

    def test_studio_and_live_are_never_equivalent_by_title_alone(self):
        incoming = rec.navidrome_album_fingerprint(
            [_navidrome_track(title="Song (Live)", musicBrainzId="")],
            album_name="Album", album_artist="Artist",
        )
        cached = rec.db_album_fingerprint(
            [_db_row(title="Song", recording_mbid="")]
        )
        assert incoming != cached


class TestSkipDecision:
    def _skip(self, **overrides):
        kwargs = dict(
            artist_name="Artist",
            album_name="Album",
            tracks=[{"id": "a"}],
            cached_ids_for_album={"a"},
            force=False,
            album_needs_reimport=False,
            verbose=False,
            metadata_fingerprint_equal=True,
            album_metadata_dirty=False,
        )
        kwargs.update(overrides)
        return should_skip_cached_album(**kwargs)

    def test_ids_and_fingerprint_equal_skip(self):
        assert self._skip() is True

    def test_ids_equal_changed_metadata_process(self):
        assert self._skip(metadata_fingerprint_equal=False) is False

    def test_ids_equal_dirty_local_correction_process(self):
        assert self._skip(album_metadata_dirty=True) is False

    def test_unknown_fingerprint_process(self):
        assert self._skip(metadata_fingerprint_equal=None) is False


class TestIdAdoption:
    def test_same_exact_path_is_adoptable(self):
        by_path, by_mbid = rec.build_existing_indexes([_db_row(id="old-1")])
        match = rec.find_adoptable_track_id(
            _navidrome_track(id="new-1"), album_name="Album",
            album_artist="Artist", by_path=by_path, by_mbid=by_mbid,
        )
        assert match == ("old-1", "file_path")

    def test_different_artist_never_adopted_by_title(self):
        by_path, by_mbid = rec.build_existing_indexes([_db_row(id="old-1")])
        match = rec.find_adoptable_track_id(
            _navidrome_track(id="new-1", artist="Other Artist", path="", musicBrainzId=""),
            album_name="Album", album_artist="Other Artist",
            by_path=by_path, by_mbid=by_mbid,
        )
        assert match is None

    def test_live_version_not_adopted_by_mbid_alone(self):
        by_path, by_mbid = rec.build_existing_indexes([_db_row(id="old-1")])
        match = rec.find_adoptable_track_id(
            _navidrome_track(id="new-1", title="Song (Live)", path="", musicBrainzId="rec-1"),
            album_name="Album", album_artist="Artist",
            by_path=by_path, by_mbid=by_mbid,
        )
        assert match is None

    def test_ambiguous_paths_are_not_adopted(self):
        by_path, by_mbid = rec.build_existing_indexes([
            _db_row(id="old-1"), _db_row(id="old-2"),
        ])
        match = rec.find_adoptable_track_id(
            _navidrome_track(id="new-1"), album_name="Album",
            album_artist="Artist", by_path=by_path, by_mbid=by_mbid,
        )
        assert match is None


class TestLocalAuthority:
    def test_matching_payload_clears_dirty(self):
        assert rec.local_authority_values_match(
            _db_row(title="Corrected"),
            {"id": "new-1", "title": "Corrected", "album": "Album"},
        ) is True

    def test_stale_payload_does_not_clear_dirty(self):
        assert rec.local_authority_values_match(
            _db_row(title="Corrected"),
            {"id": "new-1", "title": "Old", "album": "Album"},
        ) is False
        assert rec.local_authority_conflicts(
            _db_row(title="Corrected"),
            {"id": "new-1", "title": "Old", "album": "Album"},
        ) == ["title"]
