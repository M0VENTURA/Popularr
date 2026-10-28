"""Release-type classification for explicitly marked singles."""

from __future__ import annotations

from services.popularity.stages import album_stage
from services.popularity import scan_stage_runner as runner


def test_a_bracketed_single_title_is_classified_as_single():
    assert album_stage._detect_album_type(
        "Poppy", "Guardian [Single]", "Poppy", None
    ) == "single"
    assert album_stage._detect_album_type(
        "Poppy", "Guardian (Single)", "Poppy", None
    ) == "single"
    assert album_stage._detect_album_type(
        "Poppy", "Guardian - Single", "Poppy", None
    ) == "single"


def test_a_plain_album_title_stays_album():
    assert album_stage._detect_album_type(
        "Poppy", "Empty Hands", "Poppy", None
    ) == "album"


def test_primary_release_type_beats_the_generic_album_default():
    assert runner._prefer_specific_release_type("album", "single") == "single"
    assert runner._prefer_specific_release_type("album", "ep") == "ep"
    assert runner._prefer_specific_release_type("album", "album+live") == "album+live"
    assert runner._prefer_specific_release_type("album", "album") == "album"


def test_a_specific_detected_type_is_never_overridden():
    assert runner._prefer_specific_release_type("album+live", "single") == "album+live"
    assert runner._prefer_specific_release_type("single", "album") == "single"
