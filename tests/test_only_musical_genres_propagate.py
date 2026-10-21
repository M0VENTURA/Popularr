"""Only MUSICAL genres may be inherited by a track that has none of its own.

REPORT (2026-10-21): "There are a few genre's that shouldn't spill out to
every track from the album genres list. Genres like Holiday, Christmas, Cover,
shouldn't automatically assign to all tracks if the track doesn't have any
genre's. Only musical genres should propegate to all tracks."

The shape of the defect
-----------------------
The album's genre list is a FILL: a track with no genre evidence of its own
inherits it. That is right for a musical genre ("this album is Country, so
its untagged tracks are Country too") but wrong for two families:

* the per-track affiliation markers (Cover/Live/Acoustic/Remix/…) — they
  describe ONE recording's performance;
* the Christmas/Holiday family — they describe the ALBUM's OCCASION, and a
  plain track on a Christmas compilation is not itself a Christmas track.

So one cover on an album stamped "Cover" on every untagged sibling, and one
holiday record on a compilation stamped "Holiday"/"Christmas" on all of them.

⭐ THE CHRISTMAS EXEMPTION IS NOT A CONTRADICTION. The album's OWN genre
list still carries Christmas (a Christmas album IS a Christmas album, and the
genre-playlist builder reads it from there). This rule is about a TRACK
INHERITING that list — a different question.
"""

from __future__ import annotations

import pytest


class TestThePropagatableGenreRule:
    def test_musical_genres_propagate(self):
        from services.enrichment.genre_aggregation_service import (
            is_propagatable_genre,
        )

        assert is_propagatable_genre("Rock")
        assert is_propagatable_genre("Pop")
        assert is_propagatable_genre("country rock")
        assert is_propagatable_genre("Nu Metal")
        assert is_propagatable_genre("Jazz")

    def test_the_cover_marker_does_not_propagate(self):
        from services.enrichment.genre_aggregation_service import (
            is_propagatable_genre,
        )

        assert not is_propagatable_genre("Cover")
        assert not is_propagatable_genre("cover")
        assert not is_propagatable_genre(" COVER ")

    def test_the_holiday_family_does_not_propagate(self):
        """THE REPORTED CASE."""
        from services.enrichment.genre_aggregation_service import (
            is_propagatable_genre,
        )

        assert not is_propagatable_genre("Holiday")
        assert not is_propagatable_genre("Christmas")
        assert not is_propagatable_genre("holiday")

    def test_the_other_affiliation_markers_do_not_propagate(self):
        from services.enrichment.genre_aggregation_service import (
            is_propagatable_genre,
        )

        for tag in ("Live", "Acoustic", "Unplugged", "Remix", "Instrumental"):
            assert not is_propagatable_genre(tag), tag

    def test_the_albums_own_genre_list_still_keeps_christmas(self):
        """⭐ The album IS a Christmas album, so the album's OWN list keeps
        the tag — the genre-playlist builder depends on it. Only a TRACK
        inheriting that list is filtered."""
        from services.enrichment.genre_aggregation_service import (
            _TRACK_AFFILIATION_FILTER_TAGS,
        )

        assert "christmas" not in _TRACK_AFFILIATION_FILTER_TAGS
        assert "holiday" not in _TRACK_AFFILIATION_FILTER_TAGS


class TestTheAlbumSaveFillOnlyWritesMusicalGenres:
    def _apply(self, genres_str):
        """Drive the real album-save fill with the write stubbed."""
        import db.repositories.metadata as mdrepo
        import routes.ui_routes as ui

        written: list[str] = []
        original = mdrepo.update_track_genres

        def _fake(track_id=None, genres_str="", write_manual=True, **kwargs):
            written.append(genres_str)
            return 1

        mdrepo.update_track_genres = _fake
        try:
            ui._apply_album_track_genres("t1", genres_str)
        finally:
            mdrepo.update_track_genres = original
        return written

    def test_a_musical_album_list_fills_the_track(self):
        written = self._apply("Country, Rock")
        assert written, "a musical album genre must fill a track with no genres"
        assert "Country" in written[0]
        assert "Rock" in written[0]

    def test_cover_does_not_reach_a_sparse_track(self):
        """THE REPORTED CASE: one cover must not stamp the whole album."""
        written = self._apply("Country, Cover")
        assert written
        assert "cover" not in written[0].lower(), written
        assert "Country" in written[0]

    def test_holiday_does_not_reach_a_sparse_track(self):
        written = self._apply("Country, Holiday")
        assert written
        assert "holiday" not in written[0].lower(), written
        assert "Country" in written[0]

    def test_christmas_does_not_reach_a_sparse_track(self):
        written = self._apply("Country, Christmas")
        assert written
        assert "christmas" not in written[0].lower(), written
        assert "Country" in written[0]

    def test_a_list_of_only_non_propagating_genres_writes_nothing(self):
        assert self._apply("Cover, Christmas, Holiday") == []


class TestTheInheritanceListOnlyHoldsMusicalGenres:
    def test_the_track_stage_inheritance_list(self):
        """The track stage's album-inheritance list is what a sparse track
        takes its genres from."""
        from services.popularity.stages import track_stage
        from helpers import config_helpers

        # A nonzero min weight so a lone source can vote.
        original = config_helpers.get_config
        config_helpers.get_config = lambda: {"genres": {"min_weight": 0.01}}
        try:
            out = track_stage._album_top_genres(
                [{"title": "A", "musicbrainz_genres": '["cover", "christmas", "nu metal"]'}],
                max_genres=3,
            )
        finally:
            config_helpers.get_config = original

        lowered = {g.lower() for g in out}
        assert "cover" not in lowered, out
        assert "christmas" not in lowered, out
        assert "nu metal" in lowered, out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
