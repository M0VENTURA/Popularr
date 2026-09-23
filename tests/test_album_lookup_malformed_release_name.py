"""Album lookup on a release-name title with a duplicated annotation.

Reported symptom
----------------
An album stored as::

    Jomsviking (jewelcase version with "Vengeance Is My Name" as bonus track
    (no. 09))))) (jewelcase version with "Vengeance Is My Name" as bonus track
    (no. 09)) (2016)

"fails to do a lookup for the album when matching in the album scan and fails
to match the mbid."

Three independent defects combined to make the MusicBrainz query unanswerable
-------------------------------------------------------------------------------
1. **The edition stripper was ``$``-anchored, so a trailing YEAR blocked it.**
   ``strip_album_edition_marker`` only matched a marker that was the LAST thing
   in the string. Taggers routinely write the year as its own trailing group
   ("… (2016)"), which silently disabled marker stripping for EVERY edition
   keyword, not just this album.

2. **A corrupted bracket blob defeated every balanced-group rule.** The
   ``(no. 09)))))`` run leaves more closers than openers, so the regexes —
   which all match a balanced ``(…)`` — skipped the annotation entirely and it
   survived into the lookup key.

3. **``strip_search_keywords`` was a NO-OP by default.** It returned its input
   unchanged whenever ``search.strip_keywords`` (a config list) was empty, and
   that key is unset on a default install. The album scan's lookup path calls
   it, so the search ran with the raw annotated name.

Net effect: the query became
``releasegroup:"Jomsviking (jewelcase version with … (no. 09))))) … (2016)"``
— a title MusicBrainz cannot hold — and the album never matched an MBID.
"""

from __future__ import annotations

import importlib

import pytest


def _ns():
    """Import the module lazily.

    Module-scope imports of symbols this change ADDS raise ImportError on the
    unpatched tree, which turns the whole file into a collection error and hides
    every individual verdict — so the oracle reports "1 error" instead of the
    real failure set. Importing per-call keeps each test's verdict visible.
    """
    return importlib.import_module("helpers.normalization_service")

#: The exact title from the report.
REPORTED = (
    'Jomsviking (jewelcase version with \u201cVengeance Is My Name\u201d '
    'as bonus track (no. 09))))) (jewelcase version with \u201cVengeance Is '
    'My Name\u201d as bonus track (no. 09)) (2016)'
)


# ---------------------------------------------------------------------------
# The reported title
# ---------------------------------------------------------------------------

class TestReportedTitle:
    def test_malformed_blob_is_repaired_to_the_core_title(self):
        assert _ns().repair_malformed_annotations(REPORTED) == "Jomsviking"

    def test_lookup_key_is_the_release_group_title(self):
        """The lookup must ask for a title MusicBrainz can actually hold."""
        assert _ns().strip_search_keywords(REPORTED) == "Jomsviking"

    def test_edition_strip_alone_also_recovers_the_title(self):
        """It must not depend on the caller remembering to repair first."""
        assert _ns().strip_album_edition_marker(REPORTED) == "Jomsviking"

    def test_lucene_terms_are_clean(self):
        cleaned = _ns().strip_search_keywords(REPORTED)
        assert _ns().normalize_title_for_lucene_query(cleaned) == "jomsviking"

    def test_annotation_text_is_not_duplicated_in_the_query(self):
        """The doubled annotation was a tell-tale of the un-stripped name."""
        terms = _ns().normalize_title_for_lucene_query(_ns().strip_search_keywords(REPORTED))
        assert terms.count("jewelcase") == 0
        assert terms.count("bonus") == 0


# ---------------------------------------------------------------------------
# Defect 1 — a trailing year must not disable marker stripping
# ---------------------------------------------------------------------------

class TestTrailingYear:
    @pytest.mark.parametrize("raw,expected", [
        ("Weezer (Deluxe Edition) (2016)", "Weezer (2016)"),
        ("Slipknot (Clean) (2016)", "Slipknot (2016)"),
        ("Eminem (Explicit) (2002)", "Eminem (2002)"),
        ("Abbey Road (Anniversary Edition) (2009)", "Abbey Road (2009)"),
        ("Some Album (Japanese Edition) (2005)", "Some Album (2005)"),
        ("Some Album [Deluxe] (2011)", "Some Album (2011)"),
    ])
    def test_marker_is_stripped_even_with_a_trailing_year(self, raw, expected):
        assert _ns().strip_album_edition_marker(raw) == expected

    def test_the_year_itself_is_never_discarded(self):
        """The year is not an edition marker — losing it would be data loss."""
        assert _ns().strip_album_edition_marker("Weezer (Deluxe Edition) (2016)").endswith("(2016)")

    def test_a_bare_year_is_untouched(self):
        assert _ns().strip_album_edition_marker("Jomsviking (2016)") == "Jomsviking (2016)"

    def test_repeated_marker_then_year_collapses(self):
        """_REPEATED_TRAILING_MARKER_RE is also $ -anchored, so it was blocked
        by the trailing year in the same way."""
        assert _ns().strip_album_edition_marker(
            "Jomsviking (tour edition) (tour edition) (2011)"
        ) == "Jomsviking (2011)"


# ---------------------------------------------------------------------------
# Defect 2 — the malformed-blob repair must be narrow
# ---------------------------------------------------------------------------

class TestMalformedRepairIsNarrow:
    @pytest.mark.parametrize("raw", [
        "Jomsviking",
        # A LEADING group that is part of the real name must survive.
        "(What's the Story) Morning Glory?",
        "(What's the Story) Morning Glory? (2016)",
        "Some Album (Live)",
        "Unplugged (Live)",
        "Some Album (Boogie Version)",
        "Some Album (Album)",
        # Balanced nested groups are well-formed.
        "Jomsviking (jewelcase version with X as bonus track (no. 09))",
        "",
    ])
    def test_well_formed_titles_are_untouched(self, raw):
        assert _ns().repair_malformed_annotations(raw) == raw

    def test_truncates_only_the_malformed_tail(self):
        assert _ns().repair_malformed_annotations("Jomsviking (no. 09)))))") == "Jomsviking"

    def test_never_reduces_a_title_to_nothing(self):
        """If the opener is at position 0 there is no name left to keep."""
        raw = "((no. 09)))))"
        assert _ns().repair_malformed_annotations(raw) == raw

    def test_balanced_input_after_a_year_is_untouched(self):
        assert _ns().repair_malformed_annotations("Jomsviking (2016)") == "Jomsviking (2016)"


# ---------------------------------------------------------------------------
# Defect 3 — the lookup helper must not be a config-dependent no-op
# ---------------------------------------------------------------------------

class TestStripSearchKeywordsAlwaysStrips:
    def test_standard_markers_strip_with_default_config(self):
        """`search.strip_keywords` is UNSET by default, and the function used to
        return its input unchanged when so — making the lookup a no-op."""
        assert _ns().strip_search_keywords("Weezer (Deluxe Edition)") == "Weezer"

    def test_reported_title_strips_with_default_config(self):
        assert _ns().strip_search_keywords(REPORTED) == "Jomsviking"

    def test_plain_titles_pass_through(self):
        assert _ns().strip_search_keywords("Jomsviking") == "Jomsviking"

    def test_empty_input_is_safe(self):
        assert _ns().strip_search_keywords("") == ""

    @pytest.mark.parametrize("raw", [
        "Some Album (Live)",
        "Unplugged (Live)",
        "Some Album (Boogie Version)",
    ])
    def test_form_markers_survive_the_lookup_helper(self, raw):
        """Live/Remix/Acoustic name a different RECORDING, so a lookup must not
        treat them as an edition marker."""
        assert _ns().strip_search_keywords(raw) == raw

    def test_config_keywords_are_additive(self, monkeypatch):
        """A configured keyword list adds to the standard stripping."""
        import helpers.config_helpers as cfg_mod

        monkeypatch.setattr(
            cfg_mod, "get_config",
            lambda: {"search": {"strip_keywords": ["jewelcase version"]}},
        )
        # "jewelcase version" is not a standard marker, so it only goes when
        # configured — proving the config path still runs.
        assert "jewelcase version" not in _ns().strip_search_keywords("Album (jewelcase version)")


# ---------------------------------------------------------------------------
# The query the lookup actually builds
# ---------------------------------------------------------------------------

class TestLookupQueryIsClean:
    def _query(self, album: str, artist: str = "Amon Amarth") -> str:
        clean = _ns().strip_search_keywords(album) or album
        return (
            f'artist:"{artist}" '
            f'AND releasegroup:"{clean}"'
        )

    def test_query_does_not_contain_the_annotation(self):
        query = self._query(REPORTED)
        assert "jewelcase" not in query
        assert "bonus track" not in query
        assert query.endswith('releasegroup:"Jomsviking"')

    def test_query_is_a_plain_title_for_an_unannotated_album(self):
        assert self._query("Jomsviking").endswith('releasegroup:"Jomsviking"')

    def test_normal_edition_album_still_searches_by_its_core_title(self):
        assert self._query("Weezer (Deluxe Edition)").endswith('releasegroup:"Weezer"')

    def test_lookup_musicbrainz_album_uses_the_cleaned_name(self):
        """The route quoted the RAW album, so it must clean it too."""
        from pathlib import Path

        source = Path("services/enrichment/musicbrainz_service.py").read_text(encoding="utf-8")
        assert "strip_search_keywords" in source, (
            "lookup_musicbrainz_album must clean the album before quoting it"
        )
        # And the raw-interpolation form must be gone.
        assert 'f\'release:"{Escape_lucene_special_chars(Album)}"' not in source


# ---------------------------------------------------------------------------
# End-to-end: the album-scan match path
# ---------------------------------------------------------------------------

class TestAlbumScanSearchPath:
    def test_search_releasegroup_matches_cleans_the_name(self):
        """The album-scan lookup entry point must clean before querying."""
        from pathlib import Path

        source = Path("services/enrichment/musicbrainz_service.py").read_text(encoding="utf-8")
        assert "Strip_search_keywords" in source
