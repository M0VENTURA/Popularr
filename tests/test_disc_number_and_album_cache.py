"""Disc position: one rule for every writer, and a fingerprint that can match.

TWO REPORTED PROBLEMS, ONE CAUSE

**"It copies the file to downloads, updates the metadata, then copies it to the
new location — something isn't clearing the tags."** The queue's group-organize
path built its tag payload WITHOUT ``disc_number``, and ``update_file_metadata``
treats ``None`` as "not supplied". So a rip tagged disc 1 of 2 that was being
filed as a single-disc release kept its downloaded ``/2`` position.

**"Re-importing album — song IDs match but metadata changed … fingerprint_equal
=False dirty=False"** on every album, on every scan. ``dirty=False`` was the
tell: nothing had been edited, the comparison itself could not succeed.
``disc_number`` is a FINGERPRINT field, the import CLEARS it on a one-disc
release, and Navidrome keeps reporting ``discNumber: 1`` for those songs — so the
two sides were permanently unequal and ``should_skip_cached_album`` never skipped.

One shared rule in ``services.metadata.disc_number`` now serves the write path,
the file tag write and the fingerprint.
"""

from __future__ import annotations

import pytest

from services.metadata.disc_number import (
    canonical_disc_number,
    disc_number_int,
    disc_total_int,
    stored_disc_number,
)
from services.scanning.filters import should_skip_cached_album
from services.scanning.reconciliation import (
    db_album_fingerprint,
    navidrome_album_fingerprint,
)


class TestTheParsing:
    @pytest.mark.parametrize(("raw", "want"), [
        ("1", 1), (1, 1), ("2/2", 2), ("1/2", 1), ("", 0), (None, 0),
        ("disc 3", 0), ("0", 0),
    ])
    def test_disc_number_int(self, raw, want: int) -> None:
        assert disc_number_int(raw) == want

    @pytest.mark.parametrize(("raw", "want"), [
        ("2", 2), ("2/2", 2), (None, 0), ("", 0), ("x", 0),
    ])
    def test_disc_total_int(self, raw, want: int) -> None:
        assert disc_total_int(raw) == want


class TestWhatGetsStored:
    """The write rule — shared by the import, the queue and the album page."""

    @pytest.mark.parametrize(("value", "total", "multi", "want"), [
        # A genuine position is always kept.
        ("2/2", None, None, "2"),
        (2, None, None, "2"),
        # Disc 1 of a known multi-disc release is a real position.
        ("1", 2, None, "1"),
        ("1", 3, None, "1"),
        (1, 2, True, "1"),
        # Disc 1 of a single-disc release means nothing — CLEAR it.
        ("1", 1, None, ""),
        ("1", None, None, ""),
        ("1/2", 1, None, ""),
        (1, 1, False, ""),
        # Nothing known on a multi-disc release: keep whatever is stored.
        (None, 2, None, None),
        (None, 3, None, None),
        # Nothing known on a single-disc release: still meaningless.
        (None, 1, None, ""),
        (None, None, None, ""),
        # An explicit multi_disc flag wins over the disc count.
        ("1/2", None, False, ""),
        ("2/2", None, True, "2"),
    ])
    def test_stored_disc_number(self, value, total, multi, want) -> None:
        assert stored_disc_number(value, disc_total=total, multi_disc=multi) == want

    def test_the_clear_is_an_empty_string_not_none(self) -> None:
        """``None`` means "not supplied" to ``update_file_metadata`` — a clear
        that reads as "leave it alone" would keep the downloaded frame."""
        assert stored_disc_number("1", disc_total=1) == ""
        assert stored_disc_number(None, disc_total=2) is None


class TestWhatGetsCompared:
    """The fingerprint rule — disc 1 and "no disc" are the same album."""

    @pytest.mark.parametrize(("value", "multi", "want"), [
        (None, None, ""), ("", None, ""), (1, None, ""), ("1", None, ""),
        (1, False, ""), (1, True, "1"),
        (2, None, "2"), ("2/2", None, "2"),
    ])
    def test_canonical_disc_number(self, value, multi, want: str) -> None:
        assert canonical_disc_number(value, multi_disc=multi) == want


NAV = [
    {"id": "n1", "path": "/music/X/01 - a.mp3", "title": "a", "artist": "X",
     "albumArtist": "X", "album": "A", "year": 2002, "trackNumber": 1,
     "discNumber": 1, "duration": 200, "musicBrainzId": "m1"},
    {"id": "n2", "path": "/music/X/02 - b.mp3", "title": "b", "artist": "X",
     "albumArtist": "X", "album": "A", "year": 2002, "trackNumber": 2,
     "discNumber": 1, "duration": 210, "musicBrainzId": "m2"},
]
# What the DB holds after the import: the same rows, disc cleared.
DB = [
    {"id": "n1", "file_path": "/music/X/01 - a.mp3", "title": "a", "artist": "X",
     "album_artist": "X", "album": "A", "year": 2002, "release_year": 2002,
     "track_number": 1, "disc_number": None, "duration": 200,
     "recording_mbid": "m1", "navidrome_genres": ""},
    {"id": "n2", "file_path": "/music/X/02 - b.mp3", "title": "b", "artist": "X",
     "album_artist": "X", "album": "A", "year": 2002, "release_year": 2002,
     "track_number": 2, "disc_number": None, "duration": 210,
     "recording_mbid": "m2", "navidrome_genres": ""},
]  # noqa: E501


class TestTheAlbumCacheCanFinallySkip:
    def test_a_one_disc_album_matches_whether_or_not_the_disc_is_stored(self) -> None:
        """The reported loop: disc 1 in Navidrome, cleared in the DB."""
        nav = navidrome_album_fingerprint(NAV, album_name="A", album_artist="X")
        assert nav == db_album_fingerprint(DB)

    def test_the_album_is_skipped_rather_than_re_imported(self) -> None:
        nav = navidrome_album_fingerprint(NAV, album_name="A", album_artist="X")
        db = db_album_fingerprint(DB)
        assert should_skip_cached_album(
            artist_name="X", album_name="A", tracks=NAV,
            cached_ids_for_album={"n1", "n2"}, force=False,
            album_needs_reimport=False, verbose=False,
            metadata_fingerprint_equal=bool(nav == db),
            album_metadata_dirty=False,
        ) is True

    def test_a_real_disc_move_is_still_a_change(self) -> None:
        """Canonicalising disc 1 must not hide a genuine 1 -> 2 move."""
        nav = navidrome_album_fingerprint(NAV, album_name="A", album_artist="X")
        assert nav != db_album_fingerprint([dict(r, disc_number=2) for r in DB])

    def test_other_fields_still_invalidate(self) -> None:
        nav = navidrome_album_fingerprint(NAV, album_name="A", album_artist="X")
        assert nav != db_album_fingerprint([dict(r, title="changed") for r in DB])


class TestTheCopyClearsTheDownloadedPosition:
    def test_the_group_organize_payload_always_supplies_the_disc(self) -> None:
        """Omitting the key left the rip's own disc position on the copy."""
        import inspect

        from services.queue import queue_processing_service as svc

        src = inspect.getsource(svc)
        start = src.index("file_metadata = {")
        block = src[start:src.index("}", start) + 1]
        assert '"disc_number"' in block, (
            "a missing key means 'not supplied', so the DOWNLOADED disc frame "
            "rides along onto the library copy"
        )
        assert "stored_disc_number(" in block, (
            "it must use the shared rule, not a bare copy of the item's value"
        )

    def test_the_import_uses_the_same_rule(self) -> None:
        import inspect

        from services.scanning import payload_builder

        assert "disc_total_int(" in inspect.getsource(payload_builder)

    def test_the_completion_path_uses_the_same_rule(self) -> None:
        import inspect

        from services.downloads import download_completion_service as dcs

        assert "stored_disc_number(" in inspect.getsource(dcs)
