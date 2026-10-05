"""FLAC→M4A conversion, and tag writing on M4A files.

Request
-------
> Along with FLAC conversion, can we add m4a conversion as well as the ability
> to update tags on m4a files?

Two separate gaps:

* **Conversion.** `downloads.conversion.mode` was a two-value enum
  (`flac_to_mp3` / `none`) checked with hand-written `== "flac_to_mp3"`
  comparisons across seven files — and `download_organize_helpers` WHITELISTED
  the modes it accepts, so an unknown value was silently dropped back to
  `flac_to_mp3`. Adding a mode meant finding every one of those comparisons;
  they now all go through `helpers.config_helpers.conversion_target()`.
* **Tags.** `write_tags_to_file` gated on `suffix in (".mp3", ".flac")` and
  logged "Unsupported file format" for everything else, so an m4a could be
  imported but never updated — by the album page, the review, the scan or the
  queue.

The MP4 mapping lives in `_mp4_tag_items()` (pure, so it is testable without
a real audio file): native atoms for what MP4 has, iTunes freeform atoms
(``----:com.apple.iTunes:<name>``) for the MusicBrainz identity fields, which
is the scheme Picard uses so the values round-trip through other taggers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import helpers.config_helpers as _config_helpers
from services.infrastructure.filesystem_service import get_import_destination_path
from services.metadata import tag_file_service as _tag_file_service

# Resolved with getattr rather than imported directly: a missing helper is
# exactly the regression these tests exist for, so the BEHAVIOURAL tests must
# still run and fail with a clear message instead of the whole module erroring
# out of collection as "1 error".
_conversion_target_impl = getattr(_config_helpers, "conversion_target", None)
_is_flac_conversion_impl = getattr(_config_helpers, "is_flac_conversion", None)
_mp4_tag_items_impl = getattr(_tag_file_service, "_mp4_tag_items", None)


def conversion_target(mode):
    assert _conversion_target_impl is not None, (
        "helpers.config_helpers.conversion_target is missing — m4a support has "
        "been reverted, so every mode falls back to mp3"
    )
    return _conversion_target_impl(mode)


def is_flac_conversion(mode):
    assert _is_flac_conversion_impl is not None, (
        "helpers.config_helpers.is_flac_conversion is missing — m4a support has "
        "been reverted"
    )
    return _is_flac_conversion_impl(mode)


def _mp4_tag_items(tags):
    assert _mp4_tag_items_impl is not None, (
        "tag_file_service._mp4_tag_items is missing — m4a files cannot be tagged"
    )
    return _mp4_tag_items_impl(tags)

REPO_ROOT = Path(__file__).resolve().parents[1]

TEMPLATES = [
    REPO_ROOT / "templates" / "pages" / "config.html",
    REPO_ROOT / "test_site" / "templates" / "Pages" / "config.html",
    REPO_ROOT / "templates" / "auth" / "setup.html",
    REPO_ROOT / "test_site" / "templates" / "auth" / "setup.html",
]


# ===========================================================================
# 1. The mode vocabulary
# ===========================================================================
class TestConversionModes:
    @pytest.mark.parametrize("mode,target", [
        ("flac_to_mp3", "mp3"),
        ("flac_to_m4a", "m4a"),
        ("FLAC_TO_M4A", "m4a"),   # config values are normalised, not trusted
        ("none", ""),
        ("", ""),
        (None, ""),
        ("wat", ""),
    ])
    def test_a_mode_maps_to_its_container(self, mode, target):
        assert conversion_target(mode) == target

    @pytest.mark.parametrize("mode,expected", [
        ("flac_to_mp3", True),
        ("flac_to_m4a", True),
        ("none", False),
        ("", False),
    ])
    def test_whether_it_converts_at_all(self, mode, expected):
        assert is_flac_conversion(mode) is expected


# ===========================================================================
# 2. The destination extension follows the mode
# ===========================================================================
class TestTheDestinationExtension:
    def test_a_flac_becomes_m4a_when_asked(self):
        assert get_import_destination_path(
            "/dl/track.flac", "/music/Artist/Album/01 - Track.flac",
            {"mode": "flac_to_m4a"},
        ).endswith(".m4a")

    def test_a_flac_still_becomes_mp3(self):
        """CONTROL — the default path must not move."""
        assert get_import_destination_path(
            "/dl/track.flac", "/music/Artist/Album/01 - Track.flac",
            {"mode": "flac_to_mp3"},
        ).endswith(".mp3")

    def test_conversion_off_leaves_the_path_alone(self):
        dest = "/music/Artist/Album/01 - Track.flac"
        assert get_import_destination_path("/dl/track.flac", dest, {"mode": "none"}) == dest
        assert get_import_destination_path("/dl/track.flac", dest, {}) == dest

    def test_a_non_flac_source_is_never_converted(self):
        """M4A input stays M4A — only FLAC is a conversion source."""
        dest = "/music/Artist/Album/01 - Track.m4a"
        assert get_import_destination_path(
            "/dl/track.m4a", dest, {"mode": "flac_to_m4a"}
        ) == dest


# ===========================================================================
# 3. MP4 atoms
# ===========================================================================
class TestMp4Atoms:
    def test_the_fields_every_import_writes(self):
        items = _mp4_tag_items({
            "title": "Heaven Can Wait",
            "artist": "Meat Loaf",
            "album": "Bat Out of Hell II",
            "album_artist": "Meat Loaf",
            "year": "1993",
            "track_number": "12",
            "disc_number": "1",
        })
        assert items["\xa9nam"] == ["Heaven Can Wait"]
        assert items["\xa9ART"] == ["Meat Loaf"]
        assert items["\xa9alb"] == ["Bat Out of Hell II"]
        assert items["aART"] == ["Meat Loaf"]
        assert items["\xa9day"] == ["1993"]
        # Pair atoms carry (position, total); we never know the total, and 0
        # is MP4's "unknown".
        assert items["trkn"] == [(12, 0)]
        assert items["disk"] == [(1, 0)]

    def test_musicbrainz_identity_goes_into_itunes_freeform_atoms(self):
        items = _mp4_tag_items({
            "musicbrainz_trackid": "e662030e-56fb-4a4a-b108-0ab9ae06c0dc",
            "musicbrainz_albumid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "musicbrainz_artistid": "11111111-2222-3333-4444-555555555555",
            "isrc": "USWB10900012",
        })
        assert items["----:com.apple.iTunes:MusicBrainz Track Id"] == (
            b"e662030e-56fb-4a4a-b108-0ab9ae06c0dc"
        )
        assert items["----:com.apple.iTunes:MusicBrainz Album Id"] == (
            b"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        )
        assert items["----:com.apple.iTunes:MusicBrainz Artist Id"] == (
            b"11111111-2222-3333-4444-555555555555"
        )
        assert items["----:com.apple.iTunes:ISRC"] == b"USWB10900012"

    def test_an_empty_value_means_clear_not_set(self):
        """Same contract as the ID3/Vorbis writers: ``\"\"`` clears."""
        items = _mp4_tag_items({"title": "", "musicbrainz_trackid": ""})
        assert items["\xa9nam"] is None
        assert items["----:com.apple.iTunes:MusicBrainz Track Id"] is None

    def test_a_missing_key_is_left_alone(self):
        """``None`` = "not supplied" — it must not appear at all."""
        items = _mp4_tag_items({"title": None, "artist": None})
        assert items == {}

    def test_rating_is_not_written(self):
        """MP4 has no standard rating atom; inventing one would be invisible."""
        assert "rating" not in _mp4_tag_items({"rating": 5})

    def test_a_track_number_that_is_not_a_number_clears_the_atom(self):
        assert _mp4_tag_items({"track_number": "Intro"})["trkn"] is None

    @pytest.mark.parametrize("value,expected", [
        ("12", [(12, 0)]),
        ("1/2", [(1, 0)]),     # "1 of 2"
        ("01", [(1, 0)]),
    ])
    def test_the_shapes_real_tags_carry(self, value, expected):
        assert _mp4_tag_items({"track_number": value})["trkn"] == expected


# ===========================================================================
# 4. Wiring: the option is offered, and both formats are converted
# ===========================================================================
class TestTheWiring:
    @pytest.mark.parametrize("template", TEMPLATES)
    def test_the_dropdown_offers_m4a(self, template: Path):
        html = template.read_text(encoding="utf-8")
        assert 'value="flac_to_m4a"' in html, (
            f"{template.name} never offers FLAC to M4A, so the mode cannot be chosen"
        )
        assert "FLAC to M4A" in html

    def test_the_organize_path_stopped_whitelisting_modes_by_hand(self):
        """An inline whitelist silently downgraded unknown modes to mp3."""
        source = (
            REPO_ROOT / "services" / "downloads" / "download_organize_helpers.py"
        ).read_text(encoding="utf-8")
        assert 'if mode in ("flac_to_mp3", "none")' not in source
        assert "is_flac_conversion(mode)" in source

    def test_every_hand_written_mp3_comparison_is_gone(self):
        """Each one left behind is a place m4a would fall back to mp3."""
        for rel in (
            "services/downloads/download_organize_helpers.py",
            "services/infrastructure/filesystem_service.py",
            "services/metadata/album_service.py",
        ):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert '== "flac_to_mp3"' not in source, (
                f"{rel} still compares the mode inline instead of using "
                "conversion_target()/is_flac_conversion()"
            )

    @pytest.mark.parametrize("rel", [
        "services/downloads/download_organize_helpers.py",
        "services/downloads/download_pipeline_service.py",
        "services/metadata/tag_file_service.py",
    ])
    def test_the_m4a_branch_uses_aac(self, rel: str):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        if "flac" not in source:
            pytest.skip("no conversion in this module")
        assert '"aac"' in source, f"{rel} has no AAC codec branch"
