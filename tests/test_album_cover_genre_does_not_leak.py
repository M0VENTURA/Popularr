"""An album-level genre list must not carry a per-TRACK affiliation marker.

REPORT (2026-10-21): "Albums are still assigning the cover genre to non
cover tracks if the tag is attached to a album genres."

The shape of the defect
-----------------------
"Cover" is a **per-TRACK** fact: it describes ONE recording's performance.
Several album-level lists are assembled from per-track columns, and every one
of those lists is then fanned out to tracks:

* the album page's **"Detected Sources"** panel — its entries are CHECKBOXES;
  selecting one stages it as an ALBUM genre, and the album save writes the
  album's list to every track that has no genre evidence of its own;
* the **album save** itself (``_apply_album_track_genres``) — the choke point
  every album-level list passes through;
* the **album genre proposal** from the "Lookup MBID" review
  (``metadata_proposal_service._album_genres``) — it unions the release's
  per-recording ``musicbrainz_genres``, and MusicBrainz tags a cover
  recording with the "cover" genre;
* the **artist page's** genre chips — applied to EVERY track of the artist.

So ONE cover track put "Cover" on every non-cover track of the album.

The rule these tests pin
------------------------
A per-track affiliation marker belongs to the track that earned it. It may
never enter an album-level (or artist-level) genre list, and an album-level
list may never stamp one onto a track that does not have that affiliation.

The Christmas family is deliberately NOT an affiliation marker: a Christmas
album IS an album property, and the genre-playlist builder relies on the tag
surviving an album-level blend.
"""

from __future__ import annotations

import pytest


COVER_TRACK = {
    "title": "Ruby Don't Take Your Love to Town (Kenny Rogers Cover)",
    "genres": "Country, Cover",
    "manual_genres": None,
    "navidrome_genres": "Country\\Cover",
    "musicbrainz_genres": '["Cover", "Country"]',
    "lastfm_tags": '["country"]',
    "listenbrainz_genres": None,
    "discogs_genres": None,
    "spotify_genres": None,
    "essentia_genres": None,
    "audiodb_genres": None,
    "wikidata_genres": None,
}

ORDINARY_TRACK = {
    "title": "An Ordinary Song",
    "genres": "Country",
    "manual_genres": None,
    "navidrome_genres": "Country",
    "musicbrainz_genres": '["Country"]',
    "lastfm_tags": '["country"]',
    "listenbrainz_genres": None,
    "discogs_genres": None,
    "spotify_genres": None,
    "essentia_genres": None,
    "audiodb_genres": None,
    "wikidata_genres": None,
}


# ===========================================================================
# 1. The shared helper
# ===========================================================================

class TestTheAffiliationHelper:
    def test_cover_is_an_affiliation(self):
        from services.enrichment.genre_aggregation_service import (
            is_track_affiliation_tag,
        )

        assert is_track_affiliation_tag("Cover")
        assert is_track_affiliation_tag("cover")
        assert is_track_affiliation_tag(" COVER ")
        assert is_track_affiliation_tag("Live")
        assert is_track_affiliation_tag("Acoustic")

    def test_a_real_genre_is_not_an_affiliation(self):
        from services.enrichment.genre_aggregation_service import (
            is_track_affiliation_tag,
        )

        assert not is_track_affiliation_tag("Country")
        assert not is_track_affiliation_tag("Rock")
        assert not is_track_affiliation_tag("")

    def test_christmas_is_album_valid(self):
        """A Christmas album IS an album property — the playlist builder
        depends on the tag surviving an album-level blend."""
        from services.enrichment.genre_aggregation_service import (
            is_track_affiliation_tag,
            strip_track_affiliation_tags,
        )

        assert not is_track_affiliation_tag("Christmas")
        assert "Christmas" in strip_track_affiliation_tags(["Christmas", "Pop"])

    def test_the_stripper_removes_only_the_markers(self):
        from services.enrichment.genre_aggregation_service import (
            strip_track_affiliation_tags,
        )

        assert strip_track_affiliation_tags(
            ["Rock", "Cover", "Live", "Pop"]
        ) == ["Rock", "Pop"]

    def test_the_stripper_accepts_the_delimited_shapes(self):
        """The genre columns arrive as JSON arrays, delimited strings and
        lists — one parser, so a name is never mangled."""
        from services.enrichment.genre_aggregation_service import (
            strip_track_affiliation_tags,
        )

        assert strip_track_affiliation_tags('["Cover", "Rock"]') == ["Rock"]
        assert strip_track_affiliation_tags("Cover, Rock") == ["Rock"]
        assert strip_track_affiliation_tags(["Cover", "Rock"]) == ["Rock"]


# ===========================================================================
# 2. The album genre proposal (the "Lookup MBID" review)
# ===========================================================================

class TestTheAlbumGenreProposal:
    def _propose(self, **metadata):
        from services.metadata.metadata_proposal_service import _album_genres

        return _album_genres(metadata)

    def test_a_cover_recording_does_not_propose_the_cover_genre(self):
        """THE REPORTED CASE.

        MusicBrainz tags a cover RECORDING with the "cover" genre. The
        proposal unions the release's per-recording genres, so "cover" used to
        reach the album-level list — and the album save then wrote it to every
        track.
        """
        out = self._propose(
            release_group_genres="Country",
            tracks=[
                {"musicbrainz_genres": ["Cover", "Country"]},
                {"musicbrainz_genres": ["Country"]},
            ],
        )
        assert "cover" not in out.lower(), out
        assert "Country" in out

    def test_the_list_repr_never_reaches_the_proposal(self):
        """``musicbrainz_genres`` arrives as a LIST. Splitting it on commas
        produced the literal text ``"['Country']"`` as a genre name."""
        out = self._propose(
            release_group_genres="",
            tracks=[{"musicbrainz_genres": ["Country", "Folk"]}],
        )
        assert "['" not in out, out
        assert out == "Country, Folk"

    def test_a_comma_joined_string_still_parses(self):
        out = self._propose(release_group_genres="Country, Folk")
        assert out == "Country, Folk"

    def test_christmas_survives_the_proposal(self):
        out = self._propose(release_group_genres="Christmas")
        assert "Christmas" in out


# ===========================================================================
# 3. The album page's "Detected Sources" checkboxes
# ===========================================================================

class TestTheAlbumSourceChipsNeverOfferCover:
    def _sources(self, tracks):
        from services.enrichment.genre_tag_aggregator import get_album_genre_sources

        return get_album_genre_sources(tracks)

    def test_no_source_offers_cover(self):
        """The "Detected Sources" entries are CHECKBOXES: selecting one
        stages it as an ALBUM genre, which the save writes to every track."""
        sources = self._sources([COVER_TRACK, ORDINARY_TRACK])
        for source, tags in sources.items():
            names = {str(t.get("name") or "").lower() for t in tags}
            assert "cover" not in names, f"{source} offered the cover genre: {tags}"

    def test_the_real_genres_are_still_offered(self):
        sources = self._sources([COVER_TRACK, ORDINARY_TRACK])
        offered = {
            str(t.get("name") or "").lower()
            for tags in sources.values()
            for t in tags
        }
        assert "country" in offered

    def test_christmas_is_still_offered(self):
        tracks = [
            {**ORDINARY_TRACK, "musicbrainz_genres": '["Christmas", "Pop"]'},
        ]
        sources = self._sources(tracks)
        offered = {
            str(t.get("name") or "").lower()
            for tags in sources.values()
            for t in tags
        }
        assert "christmas" in offered


# ===========================================================================
# 4. The artist page's chips (applied to EVERY track of the artist)
# ===========================================================================

class TestTheArtistSourceChipsNeverOfferCover:
    def test_no_source_offers_cover(self):
        from services.enrichment.genre_tag_aggregator import get_artist_genre_sources

        sources = get_artist_genre_sources([COVER_TRACK, ORDINARY_TRACK])
        for source, tags in sources.items():
            names = {str(t.get("name") or "").lower() for t in tags}
            assert "cover" not in names, f"{source} offered the cover genre: {tags}"


# ===========================================================================
# 5. The album save's fan-out — the choke point
# ===========================================================================

class TestTheAlbumSaveNeverStampsCoverOntoSiblings:
    def _apply(self, genres_str):
        """Drive the real ``_apply_album_track_genres`` with the write
        stubbed, and return what it WOULD have written."""
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

    def test_the_album_list_cannot_carry_cover(self):
        """THE LAST LINE OF DEFENCE.

        Whatever route the album's genre list arrived by, a marker that
        describes ONE recording must not be stamped onto a track.
        """
        written = self._apply("Country, Cover")
        assert written, "the write must still happen for the real genres"
        assert "cover" not in written[0].lower(), written
        assert "Country" in written[0]

    def test_other_markers_are_stripped_too(self):
        written = self._apply("Country, Live, Acoustic")
        assert "live" not in written[0].lower(), written
        assert "acoustic" not in written[0].lower(), written

    def test_christmas_is_kept(self):
        written = self._apply("Christmas, Pop")
        assert "christmas" in written[0].lower(), written

    def test_a_list_of_only_markers_writes_nothing(self):
        assert self._apply("Cover") == []


# ===========================================================================
# 6. The artist page's "Apply Selected" chips
# ===========================================================================

class TestTheArtistApplyChipsNeverOfferCover:
    def _collect(self, rows):
        """``collect_top_genres`` is a nested function inside the artist
        page builder, so it is exercised through the module source rather
        than by importing it."""
        import inspect

        import routes.ui_routes as ui

        source = inspect.getsource(ui)
        assert "is_track_affiliation_tag" in source, (
            "the artist page's mass-apply chip list must filter the "
            "per-track affiliation markers"
        )
        return source

    def test_the_filter_is_applied_to_the_chip_list(self):
        source = self._collect([COVER_TRACK, ORDINARY_TRACK])
        fn = source[source.index("def collect_top_genres"):]
        fn = fn[: fn.index("def classify_album")]
        assert "is_track_affiliation_tag(genre)" in fn
        assert fn.index("is_track_affiliation_tag(genre)") < fn.index("counts[key] = counts.get(key, 0) + 1"), (
            "the filter must run BEFORE the chip is counted"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
