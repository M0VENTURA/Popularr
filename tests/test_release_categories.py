"""Tests for the canonical release-type category registry.

This module is the single source of truth for which discography section a
release lands in, and it replaced six duplicated classifiers that disagreed
with each other.  The rules it must never break:

1. **A release with a secondary type is never a plain studio album.**  Both old
   classifiers only knew live/compilation/remix, so ``Album + Field recording``
   and ``Album + DJ-mix + Mixtape/Street`` (the reported Prodigy cases)
   appeared under Studio Albums.  This holds for secondary types that do not
   exist yet, too.
2. **The standard categories keep their keys and labels**, so an existing
   library looks identical and stored ``missing_releases.category`` values keep
   resolving.
3. **An unrecognised value resolves to the catch-all, never to Studio.**
"""
from __future__ import annotations

import pytest

from services.catalog.release_categories import (
    ORDERED_KEYS,
    SPECS,
    STUDIO_KEY,
    category_for_album_row,
    category_for_album_type,
    category_for_musicbrainz,
    icon_for,
    label_for,
    normalise_category,
    normalise_secondary_types,
    ordered_specs,
    parse_composite_type,
)


class TestTheReportedBug:
    """The two Prodigy releases that were misfiled under Studio Albums."""

    def test_album_plus_field_recording(self):
        assert category_for_musicbrainz("album", ["Field recording"]) == "field_recording"

    def test_album_plus_dj_mix_and_mixtape(self):
        assert (
            category_for_musicbrainz("album", ["DJ-mix", "Mixtape/Street"]) == "dj_mix"
        )

    def test_neither_lands_in_studio(self):
        for secondary in (["Field recording"], ["DJ-mix", "Mixtape/Street"]):
            assert category_for_musicbrainz("album", secondary) != STUDIO_KEY


class TestTheInvariant:
    """A release carrying ANY secondary type is never a studio album."""

    ALL_SECONDARY = [
        "live", "compilation", "remix", "soundtrack", "demo", "field recording",
        "dj-mix", "mixtape/street", "spokenword", "interview", "audiobook",
        "audio drama", "score",
    ]

    @pytest.mark.parametrize("secondary", ALL_SECONDARY)
    def test_known_secondary_escapes_studio(self, secondary):
        assert category_for_musicbrainz("album", [secondary]) != STUDIO_KEY

    @pytest.mark.parametrize(
        "secondary",
        ["some future type", "Whatever MusicBrainz Adds Next", "wibble"],
    )
    def test_unknown_secondary_escapes_studio(self, secondary):
        """The whole point: a type we have never seen must not claim to be a
        studio album. It lands in the catch-all instead."""
        assert category_for_musicbrainz("album", [secondary]) == "other"

    def test_no_secondary_is_still_studio(self):
        assert category_for_musicbrainz("album", []) == STUDIO_KEY
        assert category_for_musicbrainz("album") == STUDIO_KEY


class TestStandardCategoriesUnchanged:
    """Keys and behaviour must not shift, or stored data stops resolving."""

    @pytest.mark.parametrize(
        "primary,secondary,expected",
        [
            ("album", None, "album"),
            ("album", ["Live"], "live_album"),
            ("album", ["Compilation"], "compilation"),
            ("album", ["Remix"], "remix_album"),
            ("ep", None, "ep"),
            ("single", None, "single"),
        ],
    )
    def test_standard_mapping(self, primary, secondary, expected):
        assert category_for_musicbrainz(primary, secondary) == expected

    def test_standard_labels_are_unchanged(self):
        assert label_for("album") == "Studio Albums"
        assert label_for("live_album") == "Live Albums"
        assert label_for("remix_album") == "Remix Albums"
        assert label_for("compilation") == "Compilations"
        assert label_for("ep") == "EPs"
        assert label_for("single") == "Singles"

    def test_compilation_still_outranks_live(self):
        """Pre-existing precedence — a 'live compilation' is a compilation."""
        assert category_for_musicbrainz("album", ["Live", "Compilation"]) == "compilation"

    def test_single_wins_over_secondary(self):
        assert category_for_musicbrainz("single", ["Live"]) == "single"


class TestStringSecondaryTypes:
    def test_comma_joined_string_is_split(self):
        """MusicBrainz search returns 'Live,Compilation' as a STRING. Iterating
        it character-by-character is what used to flatten these to Studio."""
        assert category_for_musicbrainz("album", "Live,Compilation") == "compilation"

    def test_single_value_string(self):
        assert category_for_musicbrainz("album", "Field recording") == "field_recording"

    @pytest.mark.parametrize("value", [None, "", "  ", [], (), 0])
    def test_empty_values(self, value):
        assert normalise_secondary_types(value) == []


class TestSpellingTolerance:
    @pytest.mark.parametrize(
        "value",
        [
            "album+fieldrecording",
            "album+field recording",
            "album+Field Recording",
            "album+Field-Recording",
            "album+FIELD_RECORDING",
        ],
    )
    def test_field_recording_spellings_all_resolve(self, value):
        assert category_for_album_type(value) == "field_recording"

    @pytest.mark.parametrize("value", ["album+djmix", "album+dj-mix", "album+DJ Mix"])
    def test_dj_mix_spellings_all_resolve(self, value):
        assert category_for_album_type(value) == "dj_mix"


class TestCompositeTypes:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("album", "album"),
            ("album+live", "live_album"),
            ("album+compilation", "compilation"),
            ("album+remix", "remix_album"),
            ("album+soundtrack", "soundtrack"),
            ("album+dj-mix+mixtape/street", "dj_mix"),
            ("ep", "ep"),
            ("single", "single"),
            ("", "album"),
        ],
    )
    def test_composite(self, value, expected):
        assert category_for_album_type(value) == expected

    def test_album_plus_remix_was_previously_studio(self):
        """Regression: the in-library classifier's early
        ``if "album" in raw_type: return "album"`` made its own remix branch
        unreachable, so a remix album you OWNED filed under Studio while the
        identical missing release filed under Remix."""
        assert category_for_album_type("album+remix") == "remix_album"

    def test_album_plus_soundtrack_was_previously_studio(self):
        assert category_for_album_type("album+soundtrack") == "soundtrack"

    def test_parse_composite(self):
        assert parse_composite_type("album+dj-mix+mixtape/street") == (
            "album",
            ["dj-mix", "mixtape/street"],
        )
        assert parse_composite_type(None) == ("", [])


class TestLegacyValues:
    """Stored data holds display LABELS ('Live Album') and keys alike."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Live Album", "live_album"),
            ("Remix", "remix_album"),
            ("EP", "ep"),
            ("Single", "single"),
            ("Compilation", "compilation"),
            ("Album", "album"),
            ("Live Albums", "live_album"),
            ("Studio Albums", "album"),
            ("Audio Dramas", "audio_drama"),
            ("DJ-mixes", "dj_mix"),
            ("live_album", "live_album"),
            ("field_recording", "field_recording"),
        ],
    )
    def test_legacy_resolves(self, value, expected):
        assert normalise_category(value) == expected

    @pytest.mark.parametrize("value", ["live", "remix", "compilation", "soundtrack"])
    def test_bare_secondary_spelling(self, value):
        """Legacy single-value storage puts a secondary spelling where a
        primary belongs — it must still reach its own section."""
        assert normalise_category(value) != "album"

    @pytest.mark.parametrize("value", ["Wibble", "Broadcast", "???", "Nonsense Type"])
    def test_unknown_never_becomes_studio(self, value):
        assert normalise_category(value) != STUDIO_KEY

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_is_studio(self, value):
        assert normalise_category(value) == STUDIO_KEY


class TestOrdering:
    def test_standard_sections_come_first(self):
        assert ORDERED_KEYS[:6] == (
            "album", "ep", "single", "compilation", "live_album", "remix_album",
        )

    def test_catch_all_is_last(self):
        assert ORDERED_KEYS[-1] == "other"

    def test_ordered_specs_sorts_by_registry_order(self):
        keys = ["other", "dj_mix", "album", "single", "live_album"]
        assert [s.key for s in ordered_specs(keys)] == [
            "album", "single", "live_album", "dj_mix", "other",
        ]

    def test_ordered_specs_deduplicates(self):
        assert [s.key for s in ordered_specs(["album", "album", "album"])] == ["album"]

    def test_ordered_specs_keeps_unknown_keys(self):
        """An unrecognised key must still render a section rather than
        vanishing the album from the page."""
        assert [s.key for s in ordered_specs(["album", "Wibble"])] == ["album", "other"]

    def test_ordered_specs_empty(self):
        assert ordered_specs([]) == []
        assert ordered_specs(None) == []

    def test_every_ordered_key_has_a_spec(self):
        for key in ORDERED_KEYS:
            assert key in SPECS


class TestLabels:
    @pytest.mark.parametrize(
        "key,label",
        [
            ("field_recording", "Field Recordings"),
            ("dj_mix", "DJ-mixes"),
            ("mixtape_street", "Mixtapes & Street"),
            ("soundtrack", "Soundtracks"),
            ("audio_drama", "Audio Dramas"),
            ("other", "Other Releases"),
        ],
    )
    def test_labels_follow_musicbrainz_vocabulary(self, key, label):
        assert label_for(key) == label

    def test_every_category_has_an_icon(self):
        for key in ORDERED_KEYS:
            assert icon_for(key).startswith("bi-")

    def test_unknown_key_falls_back_to_the_catch_all_spec(self):
        assert label_for("Nonsense") == "Other Releases"


class TestAlbumRow:
    def test_stored_type_wins(self):
        row = {"musicbrainz_albumtype": "album+fieldrecording", "album": "A Live Album"}
        assert category_for_album_row(row) == "field_recording"

    def test_spotify_type_used_when_musicbrainz_missing(self):
        row = {"spotify_album_type": "album+live"}
        assert category_for_album_row(row) == "live_album"

    def test_library_remix_album_is_not_studio(self):
        """Regression for the owned-vs-missing disagreement."""
        assert category_for_album_row({"musicbrainz_albumtype": "album+remix"}) == (
            "remix_album"
        )

    def test_no_type_falls_back_to_title(self):
        assert category_for_album_row({"album": "Greatest Hits"}) == "compilation"

    def test_no_type_no_title_is_studio(self):
        assert category_for_album_row({}) == STUDIO_KEY
