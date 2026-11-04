"""Navidrome's ``year`` is the EDITION year — it must not overwrite the original.

Reported: an album edited to Original Year 1987 / Release Year 2013 kept
reverting and re-importing on every Navidrome import.

``Child.year`` is the file's DATE tag, which the album page writes from
``release_year`` (this edition). The import wrote that into ``tracks.year``,
which the rest of the app treats as the release GROUP's first year, so:

* the original year was replaced by the edition year; and
* because Navidrome will always report the edition, ``local_authority_conflicts``
  saw a permanent ``year`` conflict (1987 vs 2013), kept the row dirty and
  re-imported the album forever.

The import must map each column to what it means:
``ORIGINALYEAR`` → ``year`` and ``DATE``/``Child.year`` → ``release_year``.
"""

from __future__ import annotations

from services.scanning.payload_builder import build_track_payload


def _build(*, year, originalyear=None) -> dict:
    extracted: dict = {"year": year}
    if originalyear is not None:
        extracted["originalyear"] = originalyear
    return build_track_payload(
        track={"id": "t1", "title": "T", "artist": "A", "albumArtist": "A"},
        album_name="The Uplift Mofo Party Plan",
        album_artist_value="The Red Hot Chili Peppers",
        canonical_artist_name="The Red Hot Chili Peppers",
        extracted=extracted,
        is_new_track=False,
    )


class TestTheTwoYearColumns:
    def test_edition_year_goes_to_release_year(self):
        payload = _build(year=2013)

        assert payload["release_year"] == 2013, (
            "Navidrome's Child.year (the file DATE) is THIS edition's year"
        )
        # With no original year recorded, the edition is the only year known.
        assert payload["year"] == 2013

    def test_original_year_stays_the_original(self):
        payload = _build(year=2013, originalyear="1987")

        assert payload["release_year"] == 2013, "Release Year = this edition"
        assert payload["year"] == "1987", (
            "Original Year must come from ORIGINALYEAR, not from Child.year; "
            "otherwise the authority check conflicts forever and the album "
            "re-imports/reverts on every Navidrome import"
        )

    def test_a_missing_edition_year_does_not_blank_release_year(self):
        payload = _build(year=None, originalyear="1987")

        assert "release_year" not in payload, (
            "an absent Child.year must leave the stored Release Year alone"
        )
        assert payload["year"] == "1987"
