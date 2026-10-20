"""A remastered track must resolve to the canonical studio recording.

REPORT (2026-10-21): a remastered track's ListenBrainz playcount, single
detection, and popularity scoring must resolve to the studio original,
not the remastered edition's fresh (and near-empty) Recording MBID.

Why
----
A remaster is a mastering-engineering pass on the exact same studio
performance. MusicBrainz mints a brand-new Recording MBID whenever an
engineer remasters an album (the audio fingerprint — AcoustID — and the
mastering credits changed), but the PERFORMANCE and the AUDIENCE are the
same one. Treating the two as different recordings breaks three decisions:

* **ListenBrainz** counts plays per Recording MBID. The 1999 original of a
  hit carries hundreds of thousands of lifetime plays; the 2024 remaster's
  fresh Recording MBID carries a few hundred. Locking onto the release's
  Recording MBID therefore paired a huge Last.fm count with a tiny
  ListenBrainz count for the same song.
* **Single detection** files singles under the plain title on
  MusicBrainz/Discogs. A remastered library copy titled
  "Higher (remastered 2024)" therefore missed its single status
  (`is_single = False`).
* **Variant compatibility** previously treated `remaster` as a hard
  version marker alongside `live`/`acoustic`/`remix`, so a remastered
  track was judged incompatible with its identically titled studio
  original and kept its own empty playcount.

Live / acoustic / remix / demo markers describe a DIFFERENT performance
and must stay as hard variants. Only `remaster` is a mastering pass on the
same performance.
"""

from __future__ import annotations

import pytest


# ===========================================================================
# 1. The shared remaster helpers
# ===========================================================================

class TestTheRemasterHelpers:
    def test_has_recognises_every_form(self):
        from helpers.normalization_service import has_remaster_marker

        assert has_remaster_marker("Higher (remastered)")
        assert has_remaster_marker("Higher (remastered 2024)")
        assert has_remaster_marker("Higher (2024 remaster)")
        assert has_remaster_marker("Higher [remastered]")
        assert has_remaster_marker("Higher - 2024 Remaster")

    def test_has_ignores_a_plain_title(self):
        from helpers.normalization_service import has_remaster_marker

        assert not has_remaster_marker("Higher")
        assert not has_remaster_marker("Higher (Live)")
        assert not has_remaster_marker("Higher (radio edit)")

    def test_strip_standalones(self):
        from helpers.normalization_service import strip_remaster_qualifier

        assert strip_remaster_qualifier("Higher (remastered)") == "Higher"
        assert strip_remaster_qualifier("Higher (remastered 2024)") == "Higher"
        assert strip_remaster_qualifier("Higher (2024 remaster)") == "Higher"
        assert strip_remaster_qualifier("Higher [remastered]") == "Higher"
        assert strip_remaster_qualifier("Higher - 2024 Remaster") == "Higher"
        assert strip_remaster_qualifier("Higher (Remastered Version)") == "Higher"

    def test_strip_compound_keeps_the_other_half(self):
        """A compound qualifier names TWO things at once — the variant AND the
        mastering pass. Only the remaster half is removed."""
        from helpers.normalization_service import strip_remaster_qualifier

        assert strip_remaster_qualifier("Higher (radio edit / remastered 2024)") == "Higher (radio edit)"
        assert (
            strip_remaster_qualifier("With Arms Wide Open (Strings version / remastered 2024)")
            == "With Arms Wide Open (Strings version)"
        )
        assert (
            strip_remaster_qualifier("With Arms Wide Open (acoustic version / remastered 2024)")
            == "With Arms Wide Open (acoustic version)"
        )
        assert (
            strip_remaster_qualifier("Is This the End? (Scream edit / remastered 2024)")
            == "Is This the End? (Scream edit)"
        )
        assert strip_remaster_qualifier("Higher (remastered 2024 / radio edit)") == "Higher (radio edit)"

    def test_strip_ignores_non_remaster_titles(self):
        from helpers.normalization_service import strip_remaster_qualifier

        assert strip_remaster_qualifier("Higher") == "Higher"
        assert strip_remaster_qualifier("Higher (radio edit)") == "Higher (radio edit)"
        assert strip_remaster_qualifier("Higher (Live)") == "Higher (Live)"
        assert strip_remaster_qualifier("Higher (feat. Someone)") == "Higher (feat. Someone)"

    def test_strip_handles_deluxe_remastered(self):
        """A qualifier whose only remaster word is "remastered" inside a
        compound like "Deluxe Remastered 2011" keeps "Deluxe"."""
        from helpers.normalization_service import strip_remaster_qualifier

        assert strip_remaster_qualifier("Higher (Deluxe Remastered 2011)") == "Higher (Deluxe)"


# ===========================================================================
# 2. Variant compatibility: a remaster is COMPATIBLE with its original
# ===========================================================================

class TestRemasterIsCompatibleWithOriginal:
    def test_remaster_matches_plain(self):
        """THE REPORTED CASE: a remastered title must be treated as the same
        recording as its studio original."""
        from services.popularity.popularity_matching import title_variants_compatible

        assert title_variants_compatible("Higher", "Higher (remastered 2024)")
        assert title_variants_compatible("Higher (remastered 2024)", "Higher")
        assert title_variants_compatible("Higher (remastered 2024)", "Higher (Remastered Version)")

    def test_true_variants_remain_incompatible(self):
        """A live / acoustic / remix / demo marker is a DIFFERENT performance
        and must still be incompatible with the studio original."""
        from services.popularity.popularity_matching import title_variants_compatible

        assert not title_variants_compatible("See You in Hell", "See You in Hell (Live)")
        assert not title_variants_compatible("See You in Hell", "See You in Hell (acoustic)")
        assert not title_variants_compatible("Hypa Hypa", "Hypa Hypa (Remix)")
        assert not title_variants_compatible("Hypa Hypa", "Hypa Hypa (demo)")

    def test_soft_markers_remain_compatible(self):
        from services.popularity.popularity_matching import title_variants_compatible

        assert title_variants_compatible("Herzblut", "Herzblut (Radio Edit)")
        assert title_variants_compatible("Higher", "Higher (radio edit / remastered 2024)")


# ===========================================================================
# 3. Single detection uses the remaster-stripped match key
# ===========================================================================

class TestSingleDetectionMatchKey:
    def _key(self, title):
        from services.enrichment.single_detection_service import single_match_key

        return single_match_key(title)

    def test_remaster_matches_plain_title(self):
        """MusicBrainz and Discogs file singles under the plain title. A
        remastered library copy must match that entry."""
        assert self._key("Higher (remastered 2024)") == self._key("Higher")
        assert self._key("Higher (Remastered Version)") == self._key("Higher")
        assert self._key("Higher [remastered]") == self._key("Higher")

    def test_compound_keeps_the_other_half(self):
        """A radio edit is a DIFFERENT release from the plain studio track."""
        assert self._key("Higher (radio edit / remastered 2024)") == self._key("Higher (radio edit)")

    def test_a_true_variant_is_not_collapsed(self):
        assert self._key("Higher (Live)") != self._key("Higher")


# ===========================================================================
# 4. The remaster-edition detector (used by the LB tracklist path)
# ===========================================================================

class TestRemasterEditionDetector:
    def test_title_trigger(self):
        from services.popularity.popularity_sources import _release_is_remaster_edition

        assert _release_is_remaster_edition({"title": "Human Clay (Remastered 2024)"})
        assert _release_is_remaster_edition({"title": "Human Clay [2024 remaster]"})
        assert _release_is_remaster_edition({"title": "Human Clay (remastered)"})

    def test_year_gap_trigger(self):
        """A release more than two years newer than its release group's
        original release is a remaster/reissue."""
        from services.popularity.popularity_sources import _release_is_remaster_edition

        release = {
            "title": "Human Clay",
            "date": "2024-09-20",
            "release-group": {"first-release-date": "1999-09-20", "id": "rg-mbid"},
        }
        assert _release_is_remaster_edition(release)

    def test_same_year_release_is_not_a_remaster(self):
        """A same-year reissue reuses the original recordings and is not a
        mastering pass."""
        from services.popularity.popularity_sources import _release_is_remaster_edition

        assert not _release_is_remaster_edition({"title": "Human Clay", "date": "1999-11-01"})

    def test_two_year_gap_is_not_a_remaster(self):
        """Within two years is a same-cycle reissue, not a remaster."""
        from services.popularity.popularity_sources import _release_is_remaster_edition

        release = {
            "title": "Human Clay",
            "date": "2001-09-20",
            "release-group": {"first-release-date": "1999-09-20", "id": "rg-mbid"},
        }
        assert not _release_is_remaster_edition(release)

    def test_plain_release_is_not_a_remaster(self):
        from services.popularity.popularity_sources import _release_is_remaster_edition

        assert not _release_is_remaster_edition({"title": "Human Clay"})


# ===========================================================================
# 5. The original-release resolver
# ===========================================================================

class TestOriginalReleaseResolver:
    def _resolve(self, group_mbid, releases):
        """Drive ``_original_release_of_group`` with a stubbed MB client."""
        from services.popularity.popularity_sources import _original_release_of_group

        class _StubClient:
            def browse_releases_for_group(self, mbid, inc="", limit=50):
                return releases

        release = {
            "title": "Human Clay (Remastered 2024)",
            "release-group": {"id": group_mbid},
        }
        return _original_release_of_group(release, _StubClient())

    def test_exact_original_year_wins(self):
        releases = [
            {"id": "new-mbid", "date": "2024-09-20"},
            {"id": "orig-mbid", "date": "1999-09-20"},
            {"id": "other-mbid", "date": "2005-09-20"},
        ]
        result = self._resolve("rg-mbid", releases)
        assert result and result["id"] == "orig-mbid"

    def test_earliest_release_wins_when_no_exact_match(self):
        releases = [
            {"id": "new-mbid", "date": "2024-09-20"},
            {"id": "orig-mbid", "date": "1999-09-20"},
        ]
        result = self._resolve("rg-mbid", releases)
        assert result and result["id"] == "orig-mbid"

    def test_returns_none_for_no_releases(self):
        assert self._resolve("rg-mbid", []) is None

    def test_returns_none_for_missing_group(self):
        from services.popularity.popularity_sources import _original_release_of_group

        assert _original_release_of_group({}, None) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
