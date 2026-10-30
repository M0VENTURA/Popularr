"""Multi-disc albums must number across discs: 101, 102, 201, 202…

All discs share one album folder, so numbering each disc from 01 produces
duplicate prefixes. The album rename, single-track rename, queue group
organise and folder import must all agree on the same scheme.
"""

from __future__ import annotations

import os

from services.downloads import download_organize_helpers as helpers
from services.downloads import download_organize_service as organize_service
from services.queue import queue_processing_service as queue_service

_DEFAULT_FORMAT = "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}"


def _set_format(monkeypatch):
    monkeypatch.setattr(
        helpers, "_read_track_file_name_format", lambda: _DEFAULT_FORMAT
    )


class TestTrackNumberFormatting:
    def test_a_single_disc_album_stays_two_digits(self):
        assert helpers._format_track_number_for_rename(3, 1, multi_disc=False) == "03"

    def test_a_multi_disc_album_prefixes_disc_one(self):
        assert helpers._format_track_number_for_rename(1, 1, multi_disc=True) == "101"
        assert helpers._format_track_number_for_rename(2, 1, multi_disc=True) == "102"
        assert helpers._format_track_number_for_rename(3, 2, multi_disc=True) == "203"

    def test_legacy_behaviour_is_kept_when_the_album_is_unknown(self):
        assert helpers._format_track_number_for_rename(1, 1) == "01"
        assert helpers._format_track_number_for_rename(1, 2) == "201"

    def test_total_suffix_is_tolerated(self):
        assert helpers._format_track_number_for_rename("3/12", "2/2", multi_disc=True) == "203"


class TestMultiDiscDetection:
    def test_one_disc_is_not_multi(self):
        assert helpers._any_multi_disc([1, 1, None]) is False

    def test_disc_two_makes_it_multi(self):
        assert helpers._any_multi_disc([None, 1, 2]) is True

    def test_no_rows_is_not_multi(self):
        assert helpers._any_multi_disc([]) is False


class TestPathBuilders:
    def test_both_discs_prefix_and_share_one_folder(self, monkeypatch):
        _set_format(monkeypatch)

        disc1 = helpers._build_target_path(
            "/music", "Limp Bizkit", 1999, "Significant Other",
            "Limp Bizkit", "Break Stuff", 1, "/downloads/track.flac",
            disc_number=1, multi_disc=True,
        )
        disc2 = helpers._build_target_path(
            "/music", "Limp Bizkit", 1999, "Significant Other",
            "Limp Bizkit", "Nookie", 2, "/downloads/track.flac",
            disc_number=2, multi_disc=True,
        )

        assert os.path.basename(disc1).startswith("101. ")
        assert os.path.basename(disc2).startswith("202. ")
        assert os.path.dirname(disc1) == os.path.dirname(disc2)
        assert os.path.normpath(disc1) == os.path.normpath(
            "/music/Limp Bizkit/1999 - Significant Other/101. Limp Bizkit - Break Stuff.flac"
        )

    def test_unknown_album_falls_back_to_detection(self, monkeypatch):
        _set_format(monkeypatch)
        monkeypatch.setattr(
            helpers, "_detect_multi_disc", lambda album, artist: True
        )

        path = helpers._build_target_path(
            "/music", "Artist", 2020, "Album", "Artist", "Song", 4,
            "/downloads/track.flac", disc_number=1,
        )

        assert os.path.basename(path).startswith("104. ")

    def test_queue_group_target_path_carries_the_disc(self, monkeypatch):
        _set_format(monkeypatch)

        path = queue_service.build_organize_group_target_path(
            music_root="/music",
            album_artist="Artist",
            year=2020,
            album_name="Album",
            artist="Artist",
            title="Song",
            track_number=1,
            source_file="/downloads/track.flac",
            disc_number=2,
            multi_disc=True,
        )

        assert "201." in path.name

    def test_legacy_move_to_music_uses_the_shared_layout(self, monkeypatch):
        _set_format(monkeypatch)

        path = organize_service.build_target_path(
            {
                "album_artist": "Artist",
                "album": "Album",
                "year": 2020,
                "artist": "Artist",
                "title": "Song",
                "track_number": 2,
                "disc_number": 2,
                "multi_disc": True,
            },
            ".flac",
            "/music",
        )

        assert str(path).endswith("202. Artist - Song.flac")
        assert "202. Artist - Song" not in os.path.dirname(str(path))
