"""The album-level proposal must list "Genres" ONCE.

REPORT (2026-10-21): "When checking metadata after either choosing a new
release group or using lookup mbid on the album page, it lists the Genres
twice on the bottom of the page under test_site."

The shape of the defect
-----------------------
``_album_level_proposals`` built its field list as::

    list(resolved) + (["album_genres"] if "album_genres" in resolved else [])

``album_genres`` has NO entry in ``_ALBUM_FIELD_SPECS`` — its value is built
by ``_album_genres`` and assigned into ``resolved`` a few lines above. So
``list(resolved)`` ALREADY contained ``album_genres``, and the ``+`` appended
it a SECOND time. The loop therefore ran the album-genres branch twice and
produced TWO IDENTICAL "Genres" proposals.

The review banner renders one row per proposal, so the same
"Genres: <current> -> <proposed>" line was printed twice.

Why it looked intermittent: the duplication only becomes VISIBLE when a
proposal is actually generated — i.e. when the MusicBrainz genres differ
from the stored ones. Choosing a new release group and using Lookup MBID are
precisely the two actions that change the MusicBrainz release, so they are
precisely the two actions that showed it.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


def _local_tracks(genres: str = "House") -> List[Dict[str, Any]]:
    return [
        {"album": "House", "album_artist": "Some Artist", "genres": genres},
    ]


def _metadata(release_group_genres: str = "country rock, dance-pop, electro") -> Dict[str, Any]:
    return {
        "release_group_title": "House",
        "release_group_genres": release_group_genres,
        "release_genres": "",
        "tracks": [
            {"musicbrainz_genres": ["country rock", "dance-pop", "electro"]},
        ],
    }


class TestAlbumGenresAreProposedOnce:
    def _proposals(self, tracks, metadata) -> List[Dict[str, Any]]:
        from services.metadata.metadata_proposal_service import _album_level_proposals

        return _album_level_proposals(tracks, metadata)

    def test_the_genres_change_is_proposed_once(self):
        """THE REPORTED CASE."""
        proposals = self._proposals(_local_tracks(), _metadata())
        genre_props = [p for p in proposals if p.get("field") == "album_genres"]
        assert len(genre_props) == 1, (
            "the album genre change must be proposed ONCE; the review banner "
            f"rendered it twice. Got {len(genre_props)}."
        )

    def test_the_single_proposal_carries_the_right_values(self):
        proposals = self._proposals(_local_tracks(), _metadata())
        genre_props = [p for p in proposals if p.get("field") == "album_genres"]
        assert len(genre_props) == 1
        proposal = genre_props[0]
        assert proposal["label"] == "Genres"
        assert proposal["current"] == "House"
        assert "country rock" in proposal["proposed"]

    def test_no_proposal_when_the_genres_already_match(self):
        """Unchanged genres must not be proposed at all."""
        tracks = _local_tracks(genres="country rock, dance-pop, electro")
        proposals = self._proposals(tracks, _metadata())
        genre_props = [p for p in proposals if p.get("field") == "album_genres"]
        assert genre_props == []

    def test_no_proposal_when_the_genres_match_in_a_different_order(self):
        """Genres are an unordered set — a re-ordering is not a change."""
        tracks = _local_tracks(genres="electro, dance-pop, country rock")
        proposals = self._proposals(tracks, _metadata())
        genre_props = [p for p in proposals if p.get("field") == "album_genres"]
        assert genre_props == []

    def test_the_other_album_fields_are_still_proposed(self):
        """The de-duplication must not silence the OTHER fields."""
        metadata = _metadata()
        metadata["recordlabel"] = "Some Label"
        tracks = _local_tracks()
        tracks[0]["recordlabel"] = ""
        proposals = self._proposals(tracks, metadata)
        fields = {p.get("field") for p in proposals}
        assert "album_recordlabel" in fields
        assert "album_genres" in fields

    def test_no_duplicate_fields_at_all(self):
        """No field may be proposed twice, whatever it is."""
        metadata = _metadata()
        metadata["recordlabel"] = "Some Label"
        proposals = self._proposals(_local_tracks(), metadata)
        fields = [p.get("field") for p in proposals]
        assert len(fields) == len(set(fields)), f"duplicated proposal fields: {fields}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
