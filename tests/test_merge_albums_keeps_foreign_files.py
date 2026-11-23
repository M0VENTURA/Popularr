"""A merge must not relabel a file that belongs to a DIFFERENT album folder.

Report: "many albums somehow got merged into the Various Artists albums such as
Spotify - Essential Alternative", with the give-away being the FILE PATHS:

    /music/Various Artists/2026 - Spotify - Essential Alternative/02. Gorillaz - Clint Eastwood.mp3
    /music/Jimmy Eat World/2002 - Good To Go (EP)/01. Jimmy Eat World - The Middle (Acoustic).mp3

Both rendered on ONE album page, because that page filters on
``(album_artist, album)`` — the TAGS — and never looks at the path.

``merge_album_names`` renamed ``tracks.album`` AND the file's ALBUM TAG for every
source album. A file never MOVES, so a foreign rip kept its path
(``/music/Jimmy Eat World/…``) while its tag claimed the compilation — and since
the tag was overwritten too, there was nothing left on disk to recover it from.

Verified on the export: 16 rows on that 197-row album are dated 2002 (the album
is a 2026 release) and are numbered inside their own tracklists — they came from
four unrelated compilations.
"""

from __future__ import annotations

import pytest

from db.repositories.metadata import _file_belongs_to_canonical

VA_PATH = (
    "/music/Various Artists/2026 - Spotify - Essential Alternative/"
    "02. Gorillaz - Clint Eastwood.mp3"
)
EP_PATH = (
    "/music/Jimmy Eat World/2002 - Good To Go (EP)/"
    "01. Jimmy Eat World - The Middle (Acoustic).mp3"
)
COLDPLAY_PATH = (
    "/music/Coldplay/2002 - A Rush of Blood to the Head/"
    "2 - Coldplay - In My Place.mp3"
)


class TestTheReportedRows:
    def test_the_ep_file_is_not_relabelled(self):
        assert _file_belongs_to_canonical(
            EP_PATH, "Good To Go (EP)", "Spotify - Essential Alternative"
        ) is False, "this is the reported row: a Jimmy Eat World EP file"

    def test_the_coldplay_file_is_not_relabelled(self):
        assert _file_belongs_to_canonical(
            COLDPLAY_PATH, "A Rush of Blood to the Head",
            "Spotify - Essential Alternative",
        ) is False

    def test_the_albums_own_file_is_relabelled(self):
        assert _file_belongs_to_canonical(
            VA_PATH, "Spotify - Essential Alternative",
            "Spotify - Essential Alternative",
        ) is True


class TestARealMergeStillWorks:
    """Two spellings of ONE album must still collapse."""

    def test_a_spelling_variant_in_the_same_folder(self):
        assert _file_belongs_to_canonical(
            VA_PATH, "Spotify Essential Alternative", "Spotify - Essential Alternative"
        ) is True

    def test_a_file_already_inside_the_canonical_folder(self):
        assert _file_belongs_to_canonical(
            VA_PATH, "An Older Source Name", "Spotify - Essential Alternative"
        ) is True

    def test_an_edition_marker_does_not_block_the_merge(self):
        path = "/music/Artist/2001 - Album (Deluxe Edition)/01. A - B.mp3"
        assert _file_belongs_to_canonical(
            path, "Album (Deluxe Edition)", "Album"
        ) is True


class TestItFailsSafe:
    @pytest.mark.parametrize("path", ["", "/", "track.mp3"])
    def test_an_unknown_folder_is_not_relabelled(self, path):
        """A DB rename alone is recoverable; an overwritten tag is not."""
        assert _file_belongs_to_canonical(path, "Other Album", "Target") is False

    def test_a_missing_canonical_name_is_not_relabelled(self):
        assert _file_belongs_to_canonical(VA_PATH, "Source", "") is False


class TestTheMergeIsRecordedInTheSource:
    def test_the_guard_is_called_before_the_file_write(self):
        import inspect

        from db.repositories import metadata

        src = inspect.getsource(metadata.merge_album_names)
        assert "_file_belongs_to_canonical" in src
        # It must gate the FILE list, not the DB update.
        assert src.index("_file_belongs_to_canonical") < src.index("UPDATE tracks")
