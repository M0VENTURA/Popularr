"""Regression tests: title-track single detection and high-z source rules.

Covers the reported miss where +44's "When Your Heart Stops Beating" (the
album's title track) was not detected as a single:

- ``determine_final_status`` excluded title tracks from the weak-source
  boost at/under the medium z-boundary, so a genuine title-track single whose
  MusicBrainz/Discogs lookups returned no match (as in the +44 scan log)
  could never reach ``medium`` when its popularity sat at/below the album
  median. The legacy engine had an explicit title-track boost for this case.
- The z >= high boundary required a real external source and ignored the
  legacy ``medium >= 2`` path, so two corroborating weak signals on a
  standout track were discarded (the "more popular = less likely to be a
  single" inversion).
- ``MusicBrainzService.lookup_recording_metadata`` never returned the
  recording artist's MBID, so the reliable artist-scoped release-group
  search was skipped whenever the track had no tagged ``musicbrainz_artistid``.
"""

from __future__ import annotations

import pytest


class TestTitleTrackBoost:
    """Title tracks corroborated by a weak source reach medium confidence."""

    def test_title_track_low_z_with_weak_source_is_medium(self):
        from services.enrichment.single_detection_service import determine_final_status

        # +44 - When Your Heart Stops Beating: title track, 3:49 (< 4:30 →
        # duration weak source), MB/Discogs unmatched, popularity at/below the
        # album median (low z). Previously returned 'none'.
        result = determine_final_status(
            album_z=0.2,
            artist_z=0.1,
            high_sources=0,
            medium_sources=1,
            radio_edit=True,
            has_metadata=False,
            is_title_track=True,
            is_compilation=False,
            zscore_high=1.0,
            zscore_medium=0.6,
        )
        assert result == "medium"

    def test_title_track_medium_z_single_weak_source_not_single(self):
        # Medium-band z (0.8) with ONE weak source is not single evidence —
        # the rule is one high source OR two medium sources. The title-track
        # boost only applies at/below the album median where the single
        # version's stream share is split; at z 0.8 the track is already
        # popular on its own (the old metadata-poor fallback granted
        # 'medium' here with zero confirmation).
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=0.8,
            artist_z=0.8,
            high_sources=0,
            medium_sources=1,
            radio_edit=True,
            has_metadata=False,
            is_title_track=True,
            is_compilation=False,
            zscore_high=1.0,
            zscore_medium=0.6,
        )
        assert result == "none"

    def test_title_track_zero_z_with_weak_source_is_medium(self):
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=0.0,
            artist_z=0.0,
            high_sources=0,
            medium_sources=1,
            radio_edit=True,
            has_metadata=False,
            is_title_track=True,
            is_compilation=False,
        )
        assert result == "medium"

    def test_title_track_no_weak_source_still_none(self):
        # Guardrail: a title track with NO corroborating evidence must not be
        # flagged as a single on popularity alone.
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=0.2,
            artist_z=0.2,
            high_sources=0,
            medium_sources=0,
            radio_edit=False,
            has_metadata=False,
            is_title_track=True,
            is_compilation=False,
        )
        assert result == "none"

    def test_non_title_track_low_z_one_weak_source_still_none(self):
        # Non-title tracks are unchanged: one weak source at low z stays 'none'.
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=0.2,
            artist_z=0.2,
            high_sources=0,
            medium_sources=1,
            radio_edit=True,
            has_metadata=False,
            is_title_track=False,
            is_compilation=False,
        )
        assert result == "none"

    def test_title_track_with_real_confirmation_is_high(self):
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=0.2,
            artist_z=0.2,
            high_sources=1,
            medium_sources=0,
            musicbrainz=True,
            has_metadata=True,
            is_title_track=True,
            is_compilation=False,
        )
        assert result == "high"


class TestHighZScoreSourceRules:
    """Two corroborating weak sources confirm a standout (legacy parity)."""

    def test_high_z_two_weak_sources_is_high(self):
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=1.4,
            artist_z=1.4,
            high_sources=0,
            medium_sources=2,
            radio_edit=True,
            has_metadata=False,
            is_title_track=False,
            is_compilation=False,
            zscore_high=1.0,
            zscore_medium=0.6,
        )
        assert result == "high"

    def test_high_z_one_weak_source_still_none(self):
        # One weak signal alone must not stack into high confidence.
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=1.4,
            artist_z=1.4,
            high_sources=0,
            medium_sources=1,
            radio_edit=True,
            has_metadata=False,
            is_title_track=False,
            is_compilation=False,
            zscore_high=1.0,
            zscore_medium=0.6,
        )
        assert result == "none"

    def test_high_z_zero_sources_still_none(self):
        # Tehran/Crossroads guardrail: z ~1.2 with no sources must stay 'none'.
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=1.2,
            artist_z=1.2,
            high_sources=0,
            medium_sources=0,
            radio_edit=False,
            has_metadata=False,
            is_title_track=False,
            is_compilation=False,
        )
        assert result == "none"

    def test_high_z_real_confirmation_is_high(self):
        from services.enrichment.single_detection_service import determine_final_status

        result = determine_final_status(
            album_z=1.4,
            artist_z=1.4,
            high_sources=1,
            medium_sources=0,
            musicbrainz=True,
            has_metadata=True,
            is_title_track=False,
            is_compilation=False,
        )
        assert result == "high"


class TestLookupRecordingMetadataArtistMbid:
    """Metadata lookup exposes the recording artist's MusicBrainz ID."""

    def test_artist_mbid_parsed_from_artist_credit(self):
        from services.enrichment.musicbrainz_service import MusicBrainzService

        svc = MusicBrainzService(enabled=True)
        # ``**kwargs`` because the production seam passes ``is_live_release``.
        svc.get_suggested_mbid = lambda title, artist, limit=5, **kwargs: (
            "rec-1234",
            0.99,
        )
        svc.http.get_recording = lambda mbid, inc="": {
            "title": "When Your Heart Stops Beating",
            "artist-credit": [
                {
                    "name": "+44",
                    "artist": {"id": "artist-mbid-44", "name": "+44"},
                }
            ],
            "releases": [
                {
                    "title": "When Your Heart Stops Beating",
                    "date": "2006-11-14",
                    "artist-credit": [{"name": "+44"}],
                }
            ],
        }

        meta = svc.lookup_recording_metadata(
            "When Your Heart Stops Beating", "+44"
        )
        assert meta["artist_mbid"] == "artist-mbid-44"
        assert meta["recording_mbid"] == "rec-1234"

    def test_missing_artist_credit_returns_none(self):
        from services.enrichment.musicbrainz_service import MusicBrainzService

        svc = MusicBrainzService(enabled=True)
        # ``**kwargs`` because the production seam passes ``is_live_release``.
        svc.get_suggested_mbid = lambda title, artist, limit=5, **kwargs: ("rec-5678", 0.9)
        svc.http.get_recording = lambda mbid, inc="": {
            "title": "Some Track",
            "releases": [{"title": "Some Album"}],
        }

        meta = svc.lookup_recording_metadata("Some Track", "Some Artist")
        assert meta["artist_mbid"] is None
        assert meta["recording_mbid"] == "rec-5678"
