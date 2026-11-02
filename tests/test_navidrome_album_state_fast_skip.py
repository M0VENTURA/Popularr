"""Per-album Navidrome state: known + clearly-unchanged fast skip."""

from __future__ import annotations

from services.scanning.navidrome_album_state import (
    album_summary_hash,
    can_fast_skip_album,
    track_ids_hash,
)


ALBUM = {
    "id": "al-1",
    "name": "Every You Every Me",
    "artist": "Placebo",
    "artistId": "ar-1",
    "songCount": 2,
    "duration": 420,
    "created": "2026-10-01T10:00:00Z",
    "musicBrainzId": "rel-1",
}


def _state(**overrides):
    state = {
        "album_id": "al-1",
        "song_count": 2,
        "summary_hash": album_summary_hash(ALBUM),
        "last_scan_marker": "scan-10",
    }
    state.update(overrides)
    return state


class TestSummary:
    def test_hash_is_stable_and_changes_with_summary(self):
        assert album_summary_hash(ALBUM) == album_summary_hash(dict(ALBUM))
        changed = dict(ALBUM, songCount=3)
        assert album_summary_hash(changed) != album_summary_hash(ALBUM)

    def test_track_id_hash_is_order_independent(self):
        assert track_ids_hash(["a", "b"]) == track_ids_hash(["b", "a"])


class TestFastSkip:
    def test_known_and_unchanged_is_fast_skipped(self):
        assert can_fast_skip_album(
            _state(),
            ALBUM,
            cached_ids={"s1", "s2"},
            scan_marker="scan-10",
            album_dirty=False,
            needs_reimport=False,
        ) is True

    def test_new_scan_marker_forces_tracklist_check(self):
        assert can_fast_skip_album(
            _state(),
            ALBUM,
            cached_ids={"s1", "s2"},
            scan_marker="scan-11",
            album_dirty=False,
            needs_reimport=False,
        ) is False

    def test_changed_summary_forces_tracklist_check(self):
        assert can_fast_skip_album(
            _state(),
            dict(ALBUM, songCount=3),
            cached_ids={"s1", "s2"},
            scan_marker="scan-10",
            album_dirty=False,
            needs_reimport=False,
        ) is False

    def test_dirty_or_missing_rows_force_the_full_path(self):
        for kwargs in (
            {"album_dirty": True, "needs_reimport": False},
            {"album_dirty": False, "needs_reimport": True},
        ):
            assert can_fast_skip_album(
                _state(),
                ALBUM,
                cached_ids={"s1", "s2"},
                scan_marker="scan-10",
                **kwargs,
            ) is False

    def test_unknown_album_has_no_state(self):
        assert can_fast_skip_album(
            None,
            ALBUM,
            cached_ids={"s1", "s2"},
            scan_marker="scan-10",
            album_dirty=False,
            needs_reimport=False,
        ) is False

    def test_no_scan_marker_disables_the_fast_path(self):
        assert can_fast_skip_album(
            _state(),
            ALBUM,
            cached_ids={"s1", "s2"},
            scan_marker="",
            album_dirty=False,
            needs_reimport=False,
        ) is False

    def test_local_count_must_match_the_summary(self):
        assert can_fast_skip_album(
            _state(),
            ALBUM,
            cached_ids={"s1"},
            scan_marker="scan-10",
            album_dirty=False,
            needs_reimport=False,
        ) is False
