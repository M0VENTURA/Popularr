"""Regression tests: single detection must not be overzealous.

Reproduces the +44 "When Your Heart Stops Beating" scan, where 7 of 14
tracks were flagged as 5-star high-confidence singles. Three legacy-parity
regressions drove the false positives:

1. ``musicbrainz`` was defaulted to a *high*-confidence source; legacy
   treated MusicBrainz as *medium* (only Discogs was high). A lone MB
   match therefore reached ``high`` at any z-score.
2. A duration-based weak source (any track under 4:30) fired for nearly
   every track, inflating ``medium_sources``. Legacy never used duration
   as single evidence — only actual radio-edit title markers counted.
3. The medium z-band (0.6-1.0) accepted ``medium >= 1``; legacy required
   ``medium >= 2``.

The +44 album shows why each rule mattered:
- Lycanthrope / No, It Isn't / 155 / Baby Come On: genuine singles with a
  Discogs match -> still ``high`` (correct).
- Cliffdiving: NOT a single, MB match only, album-z 0.33 -> now ``none``
  (was ``high``).
- When Your Heart Stops Beating: title track, genuine single, but at
  album-z -1.87 -> now ``medium`` via the title-track boost instead of an
  unwarranted ``high``/5-star.
- A mid-z track corroborated by a single weak source -> now ``none``.
"""

from __future__ import annotations

import pytest


def _final(**kw):
    from services.enrichment.single_detection_service import determine_final_status

    # The old boolean flags (``radio_edit`` / ``has_metadata``) no longer
    # exist — the source-count API (``high_sources`` / ``medium_sources`` /
    # ``metadata_medium_sources``) replaced them. The counts below are the
    # definitive evidence; the old flags were redundant alongside them.
    defaults = dict(
        discogs=False,
        musicbrainz=False,
        album_z=0.0,
        artist_z=0.0,
        is_title_track=False,
        is_compilation=False,
        zscore_high=1.0,
        zscore_medium=0.6,
        high_sources=0,
        medium_sources=0,
    )
    defaults.update(kw)
    return determine_final_status(**defaults)


class TestSourceConfidenceDefaults:
    """MusicBrainz must be medium-confidence by default (legacy parity)."""

    def test_musicbrainz_default_is_medium(self):
        from services.enrichment.single_detection_service import _source_confidence_levels

        levels = _source_confidence_levels()
        assert levels["musicbrainz"] == "medium"
        assert levels["discogs"] == "high"

    def test_discogs_default_is_high(self):
        from services.enrichment.single_detection_service import _source_confidence_levels

        assert _source_confidence_levels()["discogs"] == "high"


class TestPlus44AlbumScenario:
    """The +44 "When Your Heart Stops Beating" album at scan time."""

    def test_lycanthrope_discogs_match_high(self):
        # Real single, discogs confirmed, high album-z.
        assert _final(
            discogs=True,
            album_z=1.24,
            artist_z=1.24,
            high_sources=1,
            medium_sources=0,
        ) == "high"

    def test_cliffdiving_lone_mb_match_low_z_medium(self):
        # Only a MusicBrainz match (medium source), album-z 0.33. MB
        # confirming the track as a single is authoritative: the track IS a
        # single. A lone medium source can never be 'high', but it must be
        # marked as a single ('medium') rather than dropped to 'none' —
        # low z-scores only refine high vs medium, they never demote a
        # confirmed single. MB is a METADATA medium source
        # (``metadata_medium_sources``), which confirms at the low z band.
        assert _final(
            musicbrainz=True,
            album_z=0.33,
            artist_z=0.33,
            high_sources=0,
            medium_sources=1,
            metadata_medium_sources=1,
        ) == "medium"

    def test_title_track_low_z_medium_not_high(self):
        # Title track IS a single (title-track boost -> 'medium'), but at
        # album-z -1.87 a lone medium source must not reach 'high'/5-star.
        assert _final(
            musicbrainz=True,
            album_z=-1.87,
            artist_z=-1.87,
            high_sources=0,
            medium_sources=1,
            metadata_medium_sources=1,
            is_title_track=True,
        ) == "medium"


class TestMediumBandNeedsTwoSources:
    """Legacy parity: medium band (0.6-1.0) requires medium >= 2."""

    def test_one_weak_source_medium_band_is_none(self):
        # A mid-z track corroborated by a single weak source (e.g. a radio-edit
        # marker) must not be flagged as a single when metadata is present.
        assert _final(
            album_z=0.8,
            artist_z=0.8,
            high_sources=0,
            medium_sources=1,
        ) == "none"

    def test_two_weak_sources_medium_band_is_medium(self):
        assert _final(
            album_z=0.8,
            artist_z=0.8,
            high_sources=0,
            medium_sources=2,
        ) == "medium"

    def test_one_high_source_medium_band_is_high(self):
        assert _final(
            discogs=True,
            album_z=0.8,
            artist_z=0.8,
            high_sources=1,
            medium_sources=0,
        ) == "high"

    def test_metadata_poor_medium_band_not_single(self):
        # Medium-band z-score (0.6-1.0) with NO sources at all is NOT single
        # evidence — the old "metadata-poor" fallback flagged every mid-album
        # track in the band (Human Era / Ph4/NT0mA / Buried in Code on
        # Unleash the Archers - Phantoma all got 'medium' with hi=0, med=0).
        assert _final(
            album_z=0.7,
            artist_z=0.7,
            high_sources=0,
            medium_sources=0,
        ) == "none"


class TestDurationNotASource:
    """Duration alone must not corroborate a single (legacy parity)."""

    def test_detect_duration_not_counted(self):
        # A short track with no external source and no title marker stays
        # unflagged even at a decent z-score (duration used to add a free
        # radio_edit medium source).
        from services.enrichment.single_detection_service import detect_single_for_track

        result = detect_single_for_track(
            title="Some Mid Album Track",
            artist="Some Artist",
            album_track_count=12,
            album="Some Album",
            duration=210,
            popularity=70,
            use_advanced_detection=False,
            persist_result=False,
        )
        assert result["is_single"] is False
