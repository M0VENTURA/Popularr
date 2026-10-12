"""Single detection on compilation / Various-Artists albums.

Compilations are handled differently from regular albums:

1. EVERY track is checked as a single — ``should_skip_single_detection`` must
   not skip them (the scan pipeline already bypasses the top-50% popularity
   gate for compilation albums).
2. Popularity does NOT factor into single detection — every track on a
   compilation has a different artist, so album/artist-relative z-scores are
   meaningless. The verdict is decided purely by metadata sources
   (Discogs / MusicBrainz / ISRC / radio-edit / Last.fm ...).
"""

from __future__ import annotations


class TestShouldSkipSingleDetection:
    """Compilation tracks must not be pre-filtered; live albums still are."""

    def test_compilation_album_type_is_not_skipped(self):
        from services.enrichment.single_detection_service import should_skip_single_detection

        assert should_skip_single_detection("Billie Jean", "album+compilation") is False
        assert should_skip_single_detection("Billie Jean", "compilation") is False

    def test_soundtrack_album_type_is_not_skipped(self):
        from services.enrichment.single_detection_service import should_skip_single_detection

        assert should_skip_single_detection("Ghostbusters", "album+soundtrack") is False

    def test_live_album_is_still_skipped(self):
        from services.enrichment.single_detection_service import should_skip_single_detection

        assert should_skip_single_detection("Some Song", "album+live") is True


class TestIsCompilationAlbum:
    """is_compilation_album recognises compilation/soundtrack/various-artists."""

    def test_compilation_album_type(self):
        from services.enrichment.single_detection_service import is_compilation_album

        assert is_compilation_album("album+compilation", "Now That's What I Call Music") is True

    def test_soundtrack_album_type(self):
        from services.enrichment.single_detection_service import is_compilation_album

        assert is_compilation_album("album+soundtrack", "The Matrix OST") is True

    def test_various_artists_album_title(self):
        from services.enrichment.single_detection_service import is_compilation_album

        assert is_compilation_album("album", "Various Artists - Big Songs") is True

    def test_compilation_keyword_album_title(self):
        from services.enrichment.single_detection_service import is_compilation_album

        assert is_compilation_album(None, "ABBA Gold") is True
        assert is_compilation_album(None, "Greatest Hits Vol 2") is True

    def test_regular_album_is_not_compilation(self):
        from services.enrichment.single_detection_service import is_compilation_album

        assert is_compilation_album("album", "Thriller") is False


class TestDetermineFinalStatusCompilationIgnoresPopularity:
    """is_compilation=True must make the verdict purely source-based.

    The high z-scores passed below (1.5 / 1.5) would gate the verdict on a
    regular album; on a compilation they must be ignored entirely.
    """

    def _final(self, **kw):
        from services.enrichment.single_detection_service import determine_final_status

        defaults = dict(
            album_z=1.5,
            artist_z=1.5,
            is_compilation=True,
            high_sources=0,
            medium_sources=0,
        )
        defaults.update(kw)
        return determine_final_status(**defaults)

    def test_discogs_confirmed_is_high(self):
        assert self._final(discogs=True, high_sources=1) == "high"

    def test_two_medium_sources_is_medium(self):
        # On a compilation, two medium sources (Last.fm + radio-edit, both
        # folded into the ``medium_sources`` count) still confirm a single.
        assert self._final(medium_sources=2) == "medium"

    def test_z_standout_alone_is_none(self):
        # A high z-score + z_standout with NO sources is not a single on a
        # compilation — popularity must not fire single evidence.
        assert self._final(z_standout=True) == "none"

    def test_z_standout_with_single_weak_source_is_none(self):
        # On a regular album z_standout + 1 medium source would upgrade to
        # 'high'; on a compilation popularity is ignored so it stays 'none'.
        assert self._final(medium_sources=1, z_standout=True) == "none"

    def test_high_z_no_sources_is_none(self):
        assert self._final() == "none"


class TestDetectSingleForTrackCompilation:
    """detect_single_for_track evaluates compilation tracks, popularity-free."""

    def test_compilation_track_is_evaluated_not_prefiltered(self):
        from services.enrichment.single_detection_service import detect_single_for_track

        result = detect_single_for_track(
            title="Some Compilation Track",
            artist="Some Artist",
            album_track_count=20,
            album="Various Artists - Now That's What I Call Music",
            popularity=80,
            album_type="album+compilation",
            use_advanced_detection=False,
            persist_result=False,
        )
        # Must reach source detection — not short-circuited by the pre-filter
        # as an alternate/live/compilation skip.
        assert "alternate_or_live_version" not in result.get("reasons", [])
        # Popularity is ignored: z-scores stay zero, so the standalone
        # popularity signal never factors into the verdict.
        assert result["decision"]["album_z"] == 0.0
        assert result["decision"]["artist_z"] == 0.0
