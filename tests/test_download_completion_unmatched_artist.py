"""Regression tests: downloads for UNMATCHED artists must never be auto-moved.

The reported bug: tracks in the downloads folder whose artist does NOT align
with an artist in the download queue were being automatically transferred into
the music library by ``check_completed_downloads`` (the periodic completion
service).  The fix requires POSITIVE artist alignment before a file is claimed
and moved:

1. ``queue_metadata_matcher._metadata_matches_queue_item`` — the duration
   shortcut (exact title + duration within ``strict_duration_sec``) used to
   return ``True`` BEFORE any artist comparison, so a same-length track by a
   different artist was auto-imported.  It now only trusts duration when the
   artist also agrees.
2. ``match_engine.filename_matches_queue_item`` — a title-only path match used
   to pass (``combined = 0.70 >= 0.65``) with ``artist_score == 0.0``.  It now
   requires the queue artist to appear in the path.
3. ``download_completion_service._file_artist_matches_queue_item`` — new gate:
   a file with a concrete artist that does not match the queue item returns
   ``False`` and is skipped (stays in the downloads folder) instead of being
   claimed and moved.
"""

from __future__ import annotations

import os

from services.queue.queue_metadata_matcher import _metadata_matches_queue_item
from services.downloads.match_engine import filename_matches_queue_item


def _flac_path(name: str, tags: dict) -> str:
    """Write a tiny silent FLAC with the given embedded tags and return its path."""
    import numpy as np
    import soundfile as sf
    from mutagen.flac import FLAC as MFLAC

    path = f"/tmp/{name}.flac"
    sr = 44100
    data = np.zeros(int(sr * 30), dtype=np.float32)
    sf.write(path, data, sr, format="FLAC")
    audio = MFLAC(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


class TestMetadataMatcherDurationRequiresArtist:
    """Hole A: the duration shortcut must not fire without artist agreement."""

    def _queue_item(self) -> dict:
        return {
            "artist": "Radiohead",
            "title": "Creep",
            "album": "Pablo Honey",
            "album_artist": "Radiohead",
            "track_number": "2",
            "duration": 233,  # seconds
        }

    def test_duration_shortcut_rejects_different_artist(self):
        # Same title + same duration, but a DIFFERENT (unmatched) artist.
        # Previously returned True via the duration shortcut; must now reject.
        path = _flac_path(
            "test_mb_unmatched_duration_diff_artist",
            {"title": "Creep", "artist": "Somebody Else", "duration": "233000"},
        )
        try:
            assert _metadata_matches_queue_item(path, self._queue_item()) is False
        finally:
            os.remove(path)

    def test_duration_shortcut_still_matches_same_artist(self):
        path = _flac_path(
            "test_mb_unmatched_duration_same_artist",
            {"title": "Creep", "artist": "Radiohead", "duration": "233000"},
        )
        try:
            assert _metadata_matches_queue_item(path, self._queue_item()) is True
        finally:
            os.remove(path)

    def test_duration_shortcut_missing_artist_defers(self):
        # Missing artist tag is not proof of a mismatch — must defer (None),
        # never hard-reject.
        path = _flac_path(
            "test_mb_unmatched_duration_missing_artist",
            {"title": "Creep", "duration": "233000"},
        )
        try:
            assert _metadata_matches_queue_item(path, self._queue_item()) is None
        finally:
            os.remove(path)


class TestFilenameMatcherRequiresArtist:
    """Hole C: a title-only filename match must not pass."""

    def _queue_item(self) -> dict:
        return {
            "artist": "Pink Floyd",
            "title": "Wish You Were Here",
            "album": "Wish You Were Here",
            "album_artist": "Pink Floyd",
        }

    def test_title_only_path_is_rejected(self):
        # Path contains only the title (no artist anywhere) — previously
        # combined = 0.70 >= 0.65 passed.  Must now be rejected so an
        # unmatched-artist file can't be claimed on title alone.
        assert (
            filename_matches_queue_item(
                "/downloads/Some Unrelated Folder/Wish You Were Here.mp3",
                self._queue_item(),
            )
            is False
        )

    def test_artist_and_title_path_still_matches(self):
        assert (
            filename_matches_queue_item(
                "/downloads/Pink Floyd - Wish You Were Here.mp3",
                self._queue_item(),
            )
            is True
        )


class TestCompletionArtistGate:
    """The new gate: concrete artist mismatch → False (never auto-moved)."""

    def _queue_item(self) -> dict:
        return {
            "artist": "Metallica",
            "title": "Enter Sandman",
            "album": "Metallica",
            "album_artist": "Metallica",
            "track_number": "1",
            "duration": None,
        }

    def test_different_artist_returns_false(self):
        from services.downloads.download_completion_service import (
            _file_artist_matches_queue_item,
        )

        path = _flac_path(
            "test_mb_unmatched_gate_diff_artist",
            {"title": "Enter Sandman", "artist": "A Different Band"},
        )
        try:
            assert (
                _file_artist_matches_queue_item(path, self._queue_item()) is False
            )
        finally:
            os.remove(path)

    def test_matching_artist_returns_true(self):
        from services.downloads.download_completion_service import (
            _file_artist_matches_queue_item,
        )

        path = _flac_path(
            "test_mb_unmatched_gate_same_artist",
            {"title": "Enter Sandman", "artist": "Metallica"},
        )
        try:
            assert _file_artist_matches_queue_item(path, self._queue_item()) is True
        finally:
            os.remove(path)

    def test_missing_artist_defers(self):
        from services.downloads.download_completion_service import (
            _file_artist_matches_queue_item,
        )

        path = _flac_path(
            "test_mb_unmatched_gate_missing_artist",
            {"title": "Enter Sandman"},
        )
        try:
            assert _file_artist_matches_queue_item(path, self._queue_item()) is None
        finally:
            os.remove(path)

    def test_various_artists_defers(self):
        from services.downloads.download_completion_service import (
            _file_artist_matches_queue_item,
        )

        path = _flac_path(
            "test_mb_unmatched_gate_various",
            {"title": "Enter Sandman", "artist": "Various Artists"},
        )
        try:
            assert _file_artist_matches_queue_item(path, self._queue_item()) is None
        finally:
            os.remove(path)

    def test_album_artist_match_counts(self):
        from services.downloads.download_completion_service import (
            _file_artist_matches_queue_item,
        )

        # File tags "Metallica" only as album_artist — still a positive match.
        path = _flac_path(
            "test_mb_unmatched_gate_album_artist",
            {"title": "Enter Sandman", "album_artist": "Metallica"},
        )
        try:
            assert _file_artist_matches_queue_item(path, self._queue_item()) is True
        finally:
            os.remove(path)

    def test_va_queue_item_with_concrete_file_artist_defers(self):
        """Reported: "Weak and Powerless" (A Perfect Circle) queued under
        ``Various Artists`` was DELETED because the real artist could never
        string-match the VA placeholder.  The verifier must defer to the
        title/duration matcher instead of returning ``False``."""
        from services.downloads.download_completion_service import (
            _file_artist_matches_queue_item,
        )

        va_item = {
            "artist": "Various Artists",
            "album_artist": "Various Artists",
            "title": "Weak and Powerless",
            "album": "MTV2 Headbangers Ball",
            "duration": 233,
        }
        path = _flac_path(
            "test_mb_unmatched_gate_va_queue",
            {"title": "Weak and Powerless", "artist": "A Perfect Circle"},
        )
        try:
            assert _file_artist_matches_queue_item(path, va_item) is None, (
                "a VA queue row has no artist to match — defer (None), never "
                "reject a correctly titled download"
            )
        finally:
            os.remove(path)


class TestVaQueueItemsVerifyTitleAndDurationOnly:
    """The matcher half of the VA fix: accept on exact title + duration.

    Previously a VA queue row could only be accepted when the file's artist
    matched "Various Artists" itself (artist_score > 0) — real credits scored
    0.0 and the download fell through to filename matching, which also
    requires the queue artist in the path, so correct files were rejected.
    """

    def _va_item(self) -> dict:
        return {
            "artist": "Various Artists",
            "album_artist": "Various Artists",
            "title": "Weak and Powerless",
            "album": "MTV2 Headbangers Ball",
            "track_number": "4",
            # read_mp3_metadata derives duration from the AUDIO, not the
            # duration tag, and the harness file is 30 s of silence.
            "duration": 30,
        }

    def test_va_queue_accepts_exact_title_and_strict_duration(self):
        path = _flac_path(
            "test_va_match_ok",
            {
                "title": "Weak and Powerless",
                "artist": "A Perfect Circle",
                "duration": "30000",
            },
        )
        try:
            assert _metadata_matches_queue_item(path, self._va_item()) is True, (
                "exact title + strict duration must be enough for a VA row"
            )
        finally:
            os.remove(path)

    def test_va_queue_still_rejects_a_different_duration(self):
        wrong_length = {**self._va_item(), "duration": 90}
        path = _flac_path(
            "test_va_match_bad_duration",
            {
                "title": "Weak and Powerless",
                "artist": "A Perfect Circle",
                "duration": "90000",
            },
        )
        try:
            assert _metadata_matches_queue_item(path, wrong_length) is False, (
                "title-only agreement with a wildly different length is the "
                "wrong track"
            )
        finally:
            os.remove(path)

    def test_va_queue_still_rejects_a_different_title(self):
        path = _flac_path(
            "test_va_match_bad_title",
            {
                "title": "The Nurse Who Loved Me",
                "artist": "A Perfect Circle",
                "duration": "30000",
            },
        )
        try:
            assert _metadata_matches_queue_item(path, self._va_item()) is False, (
                "the VA deferral must never turn into a title-free accept"
            )
        finally:
            os.remove(path)

    def test_concrete_queue_items_keep_their_strict_artist_rule(self):
        """Regression: concrete-vs-concrete mismatches still hard-reject."""
        concrete = {
            "artist": "Metallica",
            "album_artist": "Metallica",
            "title": "Enter Sandman",
            "duration": 30,  # matches the harness audio length
        }
        path = _flac_path(
            "test_concrete_still_strict",
            {"title": "Enter Sandman", "artist": "A Different Band", "duration": "30000"},
        )
        try:
            assert _metadata_matches_queue_item(path, concrete) is False
        finally:
            os.remove(path)


class TestMatchingFileExistsUnconfirmed:
    """The re-download loop guard: a matching file on disk stops the retry."""

    def _item(self, artist="Stray Kids", title="The Little Things"):
        return {"artist": artist, "title": title}

    def _fs_files(self, names):
        return [{"rel_path": n, "full_path": "/downloads/" + n} for n in names]

    def test_duplicate_suffix_file_is_detected(self):
        from services.downloads.download_completion_service import (
            _matching_file_exists_unconfirmed,
        )

        # slskd's duplicate-rename pattern: base + "_<timestamp>".
        files = self._fs_files([
            "skz-Replay 2026 Pt.1/Stray Kids_SKZ-REPLAY 2026 Pt.1_05_The Little Things_639227549704072837.flac",
        ])
        hit = _matching_file_exists_unconfirmed(self._item(), files, "/downloads")
        assert hit is not None
        assert "The Little Things" in hit

    def test_matching_file_detected(self):
        from services.downloads.download_completion_service import (
            _matching_file_exists_unconfirmed,
        )

        files = self._fs_files([
            "skz-Replay 2026 Pt.1/Stray Kids_SKZ-REPLAY 2026 Pt.1_05_The Little Things.flac",
        ])
        hit = _matching_file_exists_unconfirmed(self._item(), files, "/downloads")
        assert hit is not None

    def test_unrelated_file_returns_none(self):
        from services.downloads.download_completion_service import (
            _matching_file_exists_unconfirmed,
        )

        files = self._fs_files([
            "somewhere/Something Entirely Different.flac",
        ])
        assert _matching_file_exists_unconfirmed(self._item(), files, "/downloads") is None

    def test_apostrophe_title_detected(self):
        from services.downloads.download_completion_service import (
            _matching_file_exists_unconfirmed,
        )

        item = {"artist": "Voice of Baceprot", "title": "What's The Holy (Nobel) Today"}
        files = self._fs_files([
            "nicotine/Voice of Baceprot - What's The Holy (Nobel) Today_639226592128975884.flac",
        ])
        hit = _matching_file_exists_unconfirmed(item, files, "/downloads")
        assert hit is not None
