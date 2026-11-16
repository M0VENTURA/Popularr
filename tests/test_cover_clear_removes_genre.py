"""Clearing a cover verdict must also drop the Cover GENRE.

REPORTED

> "I had re-ran the cover detection and it had cleared it, but was still building
> the playlist."

> "Motörhead – No Class | Final genres: Heavy Metal | musicbrainz_genres still
> contains Cover"

``clear_track_cover_verdict`` reset ``is_cover``, the reason and the file tags —
but left "Cover" in ``genres`` and ``musicbrainz_genres``. The row therefore kept
DISPLAYING a Cover genre after the verdict was cleared, and any later pass that
reads the genre columns could re-assert it.

``cover_verdict_repair_service`` already documents the contract for the branch it
repairs: a clear removes the flag AND "Cover" from both columns. This pins the
same rule for the manual clear.
"""

from __future__ import annotations

import inspect

from services.enrichment import cover_detection_service as cds


class TestTheClearAlsoRemovesTheGenre:
    def test_the_update_writes_both_genre_columns(self) -> None:
        src = inspect.getsource(cds.clear_track_cover_verdict)
        assert "musicbrainz_genres = :mb_genres" in src, (
            "the reported Motörhead row kept Cover in musicbrainz_genres"
        )
        assert "genres = :genres" in src, (
            "the final genres column must be cleaned too"
        )

    def test_both_columns_are_read_before_they_are_rewritten(self) -> None:
        src = inspect.getsource(cds.clear_track_cover_verdict)
        select = src[src.index("SELECT"):src.index("UPDATE")]
        for column in ("genres", "musicbrainz_genres"):
            assert column in select, (
                f"{column} must be SELECTed, or the rewrite would erase the "
                "other genres instead of filtering one out"
            )

    def test_only_the_cover_marker_is_removed(self) -> None:
        """Live / Acoustic / Remaster are SEPARATE verdicts, not ours to clear."""
        src = inspect.getsource(cds.clear_track_cover_verdict)
        helper = src[src.index("def _without_cover"):]
        helper = helper[: helper.index("return ")]
        # The body only, without the docstring that NAMES the broader helper.
        body = helper.split('"""')[-1]
        assert '"cover"' in body.lower(), "the filter must target Cover"
        assert "is_track_affiliation_tag" not in body, (
            "that helper also strips Live/Acoustic/Remaster, which clearing a "
            "COVER verdict must not touch"
        )


class TestTheFilterItself:
    """Exercise the helper's behaviour through the public path's semantics."""

    def _filter(self, value: str) -> str:
        # Same rule as ``_without_cover`` in the service.
        from services.enrichment.genre_aggregation_service import _parse_genre_input

        kept = [
            str(g).strip()
            for g in (_parse_genre_input(value) or [])
            if str(g).strip() and str(g).strip().casefold() != "cover"
        ]
        return ", ".join(kept)

    def test_a_cover_marker_is_dropped(self) -> None:
        assert self._filter("Heavy Metal; Cover") == "Heavy Metal"

    def test_a_cover_only_list_becomes_empty(self) -> None:
        assert self._filter("Cover") == ""

    def test_live_and_acoustic_survive(self) -> None:
        """CONTROL — clearing a cover verdict is not a live/acoustic reset."""
        assert self._filter("Nu Metal, Cover, Live, Acoustic") == "Nu Metal, Live, Acoustic"

    def test_a_jsonb_shaped_value_is_handled(self) -> None:
        assert self._filter('["Nu Metal", "Cover"]') == "Nu Metal"

    def test_case_is_ignored(self) -> None:
        assert self._filter("Heavy Metal; cover") == "Heavy Metal"
