"""A popularity scan re-added the false "(X Cover)" titles a metadata scan removed.

REPORT: "I ran a metadata scan and it fixed the cover issues, but then when the
album popularity scan ran, it reverted it."

The popularity scan re-runs cover detection at the end of each album
(``_run_album_cover_detection``) and writes the verdict with
``apply_cover_metadata_batch`` → ``_build_cover_title``, which is what appends
" (X Cover)".

WHY IT CAME BACK — two defects, both fixed here:

1. THE POPULARITY SCAN GETS STALE ROWS. ``_run_album_cover_detection`` is handed
   ``tracks`` = ``album_row["tracks"]``, the RAW DB rows read at load time. The
   track stage resolves each track's real performer ("Various Artists" →
   P.O.D./Incubus/…) but applies the result to a COPY
   (``_build_effective_track(track, payload)``), never back onto the raw dict.
   So on a compilation the cover heuristics compare the original they find
   against the album-level PLACEHOLDER, conclude "different artist", and record
   the track as a cover of its own performer. The metadata scan does not hit
   this because it passes the resolved artist — which is exactly why it fixed
   the covers and the popularity scan then put them back.

2. THE SELF-CREDIT GUARD COULD BE BYPASSED. It required BOTH sides to be truthy
   and used ``names_match``, which is punctuation- and accent-SENSITIVE:

   * ``track["artist"]`` empty (a compilation track with no artist of its own,
     before the metadata pass fills it) → guard never ran;
   * ``track["artist"]`` = "Various Artists" → compared against a real credit,
     never matched;
   * "Ünloco" (library) vs "Unloco" (MusicBrainz) → same name, reported as
     different.

Verified by oracle against ``origin/develop`` (7f5330b7).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ALBUM = "Little Nicky"
ALBUM_ARTIST = "Various Artists"


class _StubMB:
    """MusicBrainz stub where every ISRC resolves to a recording credited to
    *credit* — i.e. the track's OWN performance, re-credited."""

    def __init__(self, credit: str):
        self._credit = credit

    def lookup_by_isrc(self, isrc, inc=None):
        return [{
            "id": "rec-" + self._credit,
            "title": "T",
            "artist-credit": [{"artist": {"name": self._credit}}],
            "first-release-date": "1999-06-08",
        }]

    def get_recording(self, mbid, inc=None):
        return {}

    def search_recordings(self, query, limit=15):
        return []

    def get_work(self, wid, inc=None):
        return {}

    def get_release(self, mbid, inc=None):
        return {}


def _detector(credit: str):
    from services.enrichment.cover_detector_impl import CoverDetector

    d = CoverDetector(db_connection=None)
    d.mb = _StubMB(credit)
    d._load_cover_checked_map = lambda ids: {}
    return d


class TestNoUsablePerformerMeansNoCoverVerdict:
    """Defect 2(a): an unusable track artist must not produce a cover."""

    def test_a_placeholder_track_artist_yields_no_verdict(self):
        """The compilation case: the row still says "Various Artists"."""
        det = _detector("Muse")
        result = det._detect_via_isrc(
            "ISRC1", "Cave",
            album_artist=ALBUM_ARTIST,
            track_artist=ALBUM_ARTIST,
        )
        assert result is None, (
            "an album-level placeholder is not a performer, so no cover "
            f"verdict can be sound here (got {result})"
        )

    def test_an_empty_track_artist_yields_no_verdict(self):
        """Falling back to the placeholder is the same hole, not a fix."""
        det = _detector("Muse")
        result = det._detect_via_isrc(
            "ISRC1", "Cave",
            album_artist=ALBUM_ARTIST,
            track_artist="",
        )
        assert result is None

    def test_a_real_performer_still_gets_a_verdict(self):
        """The guard must not disable ISRC detection for ordinary tracks."""
        det = _detector("Someone Else")
        result = det._detect_via_isrc(
            "ISRC1", "Cave",
            album_artist="Muse",
            track_artist="Muse",
        )
        assert result is not None and result["artist"] == "Someone Else"

    def test_the_tracks_own_credit_is_never_a_cover(self):
        det = _detector("Muse")
        assert det._detect_via_isrc(
            "ISRC1", "Cave", album_artist="Muse", track_artist="Muse"
        ) is None


class TestSameArtistComparisonIsAccentAndPunctuationInsensitive:
    """Defect 2(c): "Ünloco" and "Unloco" are the same artist."""

    def test_diacritic_variants_match(self):
        from services.enrichment.cover_detector_impl import _same_artist

        assert _same_artist("\u00dcnloco", "Unloco")

    def test_punctuation_variants_match(self):
        from services.enrichment.cover_detector_impl import _same_artist

        assert _same_artist("Guns N\u2019 Roses", "Guns N' Roses")
        assert _same_artist("P.O.D.", "P.O.D")

    def test_genuinely_different_artists_do_not_match(self):
        from services.enrichment.cover_detector_impl import _same_artist

        assert not _same_artist("Muse", "Incubus")
        assert not _same_artist("Muse", "Various Artists")
        assert not _same_artist("", "Muse")

    def test_the_isrc_guard_uses_the_insensitive_comparison(self):
        """An accented self-credit must not be reported as a cover."""
        det = _detector("Unloco")
        result = det._detect_via_isrc(
            "ISRC1", "Nothing",
            album_artist=ALBUM_ARTIST,
            track_artist="\u00dcnloco",
        )
        assert result is None


class TestTheRecorderDiscardsAnUnusablePerformer:
    """The funnel guard, end to end, on a compilation-shaped row."""

    def _run(self, monkeypatch, track_artist: str):
        import services.enrichment.cover_detector_impl as impl

        monkeypatch.setattr(
            impl, "apply_cover_metadata_batch",
            lambda conn, ups: [u["track_id"] for u in ups],
        )
        det = _detector("Muse")
        return det.detect_covers_for_album(
            ALBUM, ALBUM_ARTIST,
            [{"id": "t1", "title": "Cave", "artist": track_artist, "isrc": "ISRC1",
              "album": ALBUM, "album_artist": ALBUM_ARTIST}],
        )

    def test_a_placeholder_performer_is_discarded(self, monkeypatch):
        assert self._run(monkeypatch, ALBUM_ARTIST) == []

    def test_an_empty_performer_is_discarded(self, monkeypatch):
        assert self._run(monkeypatch, "") == []

    def test_a_real_self_credited_performer_is_discarded(self, monkeypatch):
        assert self._run(monkeypatch, "Muse") == []


class TestThePopularityScanPassesTheResolvedArtist:
    """Defect 1: the cover pass must not receive the stale raw rows."""

    def test_the_cover_pass_overlays_the_resolved_artist(self):
        import inspect

        from services.popularity import scan_stage_runner as runner

        source = inspect.getsource(runner._run_album_cover_detection)
        assert "resolved_track_artists" in source, (
            "the cover pass must overlay the artist the track stage resolved; "
            "the raw rows still carry the album-level placeholder"
        )
        assert "_row[\"artist\"]" in source or "_row['artist']" in source

    def test_the_scan_publishes_the_resolved_artists_before_detecting(self):
        import inspect

        from services.popularity import scan_stage_runner as runner

        source = inspect.getsource(runner.run_scan)
        publish_at = source.index("resolved_track_artists")
        call_at = source.index("_run_album_cover_detection(")
        assert publish_at < call_at, (
            "the resolved artists must be published BEFORE the cover pass runs"
        )
        # And sourced from the per-track results.
        assert "_track_results_ordered" in source

    def test_the_overlay_does_not_mutate_the_caller_rows(self):
        """A copy is overlaid, so the scan's own row dicts are left alone."""
        import inspect

        from services.popularity import scan_stage_runner as runner

        source = inspect.getsource(runner._run_album_cover_detection)
        assert "dict(_row)" in source
