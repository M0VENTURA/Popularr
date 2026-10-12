"""Regression tests: popularity standouts are detected as singles.

Reproduces the Stray Kids scans where genuinely dominant tracks were missed:

- District 9 (180k listeners vs ~92k for the next track, album-z ~1.8) with a
  Last.fm confirmation was marked ``single=low`` (status=none, hi=0, med=1):
  the high z-band required ``medium >= 2`` and a lone weak source was dropped.
- "U (Stray Kids feat. Tablo)" had the highest album z in HOP (1.80) but zero
  source matches — and its feature-artist name meant the popularity-stats
  lookups (grouped by album artist "Stray Kids") found nothing, so even the
  z-score / standout signal could not compute.

Fix: a catalog-size-aware popularity standout (``z_standout``) is now a
popularity *confirmation* — it is NOT single evidence on its own (a popular
album track is not a single), but it bolsters a track that already carries
medium-confidence evidence to ``high`` when the z-score hits the standout
range. The dynamic z threshold was lowered so strong outliers (z≈1.8) qualify,
and the stats artist is resolved to the album artist for "feat." tracks.
"""

from __future__ import annotations

import pytest


def _final(**kw):
    from services.enrichment.single_detection_service import determine_final_status

    # The old boolean flags (``radio_edit`` / ``has_metadata``) no longer
    # exist — the source-count API (``high_sources`` / ``medium_sources`` /
    # ``metadata_medium_sources``) replaced them.
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


class TestPopularityStandoutConfirmsSingle:
    """The popularity confirmation bolsters, never confirms, a single."""

    def test_standout_alone_is_none(self):
        # "U" case: album-z 1.80, zero metadata matches. The popularity
        # standout is NOT a medium single confirmation on its own — a
        # popular album track is not a single.
        assert _final(
            album_z=1.8,
            artist_z=0.8,
            high_sources=0,
            medium_sources=0,
            z_standout=True,
        ) == "none"

    def test_standout_with_weak_source_is_high(self):
        # District 9 case: a track with one medium source (Last.fm
        # confirmation) is BOLSTERED to high when its z-score hits the
        # standout range.
        assert _final(
            album_z=1.8,
            artist_z=0.88,
            high_sources=0,
            medium_sources=1,
            z_standout=True,
        ) == "high"

    def test_standout_with_metadata_confirmation_is_high(self):
        assert _final(
            discogs=True,
            album_z=1.8,
            artist_z=0.8,
            high_sources=1,
            medium_sources=0,
            z_standout=True,
        ) == "high"

    def test_high_z_no_sources_no_standout_still_none(self):
        # Tehran/Crossroads guardrail: z ~1.2 with no corroboration must stay
        # unflagged — z_standout (>= ~1.6-1.8) is required to bolster.
        assert _final(
            album_z=1.2,
            artist_z=1.2,
            high_sources=0,
            medium_sources=0,
        ) == "none"

    def test_high_z_one_weak_source_no_standout_still_none(self):
        # One weak signal without a popularity standout is not enough.
        assert _final(
            album_z=1.4,
            artist_z=1.4,
            high_sources=0,
            medium_sources=1,
        ) == "none"

    def test_medium_band_one_weak_source_still_none(self):
        # The medium band (0.6-1.0) still requires medium >= 2.
        assert _final(
            album_z=0.8,
            artist_z=0.8,
            high_sources=0,
            medium_sources=1,
        ) == "none"


class TestDynamicZThreshold:
    """Strong outliers qualify as standouts across catalog sizes."""

    def _thresh(self, n):
        from services.enrichment.single_detection_service import get_dynamic_z_threshold
        return get_dynamic_z_threshold(n)

    def test_tiny_catalog(self):
        assert self._thresh(3) == 1.5

    def test_small_catalog(self):
        assert self._thresh(8) == 1.7

    def test_medium_catalog(self):
        assert self._thresh(20) == 1.8

    def test_large_catalog(self):
        assert self._thresh(100) == 1.7

    def test_very_large_catalog(self):
        assert self._thresh(300) == 1.6

    def test_district9_style_outlier_qualifies(self):
        # album-z ~1.8 must beat the threshold for a mid-to-large catalog so
        # the standout fires instead of falling into the boundary rounding.
        assert 1.8 >= self._thresh(100)
        assert 1.8 >= self._thresh(50)


class TestFeatureTrackStatsResolution:
    """A "Artist feat. Guest" track resolves stats to the album artist."""

    def test_feature_track_uses_album_artist_stats(self, monkeypatch):
        import services.popularity.popularity_stats_service as pss
        from services.enrichment.single_detection_service import detect_single_for_track

        def fake_artist_stats(conn, artist):
            if artist == "Stray Kids":
                return (50.0, 10.0, [50, 45, 40, 55, 48, 42, 60, 52, 44, 47])
            return (0.0, 0.0, [])

        def fake_album_stats(conn, artist, album):
            if artist == "Stray Kids" and album == "HOP":
                return (45.0, 8.0, [45, 42, 40, 44, 41])
            return (0.0, 0.0, [])

        monkeypatch.setattr(pss, "calculate_artist_stats", fake_artist_stats)
        monkeypatch.setattr(pss, "calculate_album_stats", fake_album_stats)
        monkeypatch.setattr(
            "services.enrichment.single_detection_service._detect_musicbrainz",
            lambda *a, **k: {"source": "musicbrainz", "matched": False, "confidence": 0.0, "metadata": {}},
        )

        # "U" by "Stray Kids feat. Tablo" — the raw-name stats lookup finds
        # nothing, so detection falls back to the album artist "Stray Kids".
        result = detect_single_for_track(
            title="U",
            artist="Stray Kids feat. Tablo",
            album="HOP",
            album_track_count=14,
            popularity=60.0,
            album_type="album",
            use_advanced_detection=False,
            persist_result=False,
        )

        decision = result["decision"]
        assert decision["album_z"] > 1.5, decision
        assert decision["z_standout"] is True, result["reasons"]
        # The standout is NOT a medium single confirmation on its own: with
        # zero source evidence the track is not flagged as a single even
        # though its stats resolved correctly.
        assert result["is_single"] is False, result["reasons"]
        assert result["confidence"] == "low"


class TestAlbumLocalCompositeZ:
    """The single verdict uses the album-local composite z (raw LF/LB counts),
    never the artist-wide z.

    Reproduces the 36 Crazyfists "Bitterness the Star" bleed: the album is a
    standout vs the rest of the catalogue, so EVERY track's artist_z was huge
    (z≈2.6-3.4) and ``max(album_z, artist_z)`` promoted mid-pack album tracks
    to ``single=high`` / 5★ on popularity alone (e.g. "Circle the Drain" at
    album-z 0.90, "Bury Me Where I Fall" at album-z -0.11).
    """

    ALBUM_LF = [119490.0, 67064.0, 48039.0, 39262.0, 50697.0, 46555.0, 42740.0, 31215.0]
    ALBUM_LB = [178820.0, 95311.0, 71434.0, 55095.0, 77329.0, 65510.0, 64751.0, 60976.0]

    def _patch(self, monkeypatch):
        import services.popularity.popularity_stats_service as pss
        from services.enrichment import single_detection_service as sds

        # Tight artist distribution → an inflated artist_z for ANY track
        # (artist_z ≈ 3.1) regardless of the track's own popularity.
        def fake_artist_stats(conn, artist):
            return (50.0, 0.0, [50] * 50)

        def fake_album_stats(conn, artist, album):
            return (72.5, 10.0, [55, 60, 65, 70, 75, 80, 85, 82])

        monkeypatch.setattr(pss, "calculate_artist_stats", fake_artist_stats)
        monkeypatch.setattr(pss, "calculate_album_stats", fake_album_stats)
        monkeypatch.setattr(
            sds, "_detect_discogs",
            lambda *a, **k: {"source": "discogs", "matched": False, "confidence": 0.0, "metadata": {}},
        )
        monkeypatch.setattr(
            sds, "_detect_musicbrainz",
            lambda *a, **k: {"source": "musicbrainz", "matched": False, "confidence": 0.0, "metadata": {}},
        )
        monkeypatch.setattr(
            sds, "_detect_discogs_video",
            lambda *a, **k: {"source": "discogs_video", "matched": False, "confidence": 0.0, "metadata": {}},
        )

        class FakeLastFm:
            def check_track_as_single(self, artist, title):
                return True

            def get_album_track_count(self, artist, album):
                return 0

            def search_album(self, title, artist=None, limit=30):
                return []

        return FakeLastFm()

    def _detect(self, monkeypatch, lastfm_listeners, listenbrainz_listens):
        from services.enrichment.single_detection_service import detect_single_for_track

        lastfm_client = self._patch(monkeypatch)
        return detect_single_for_track(
            title="Circle the Drain",
            artist="36 Crazyfists",
            album="Bitterness the Star",
            album_track_count=12,
            popularity=81.6,
            album_type="album",
            use_advanced_detection=True,
            persist_result=False,
            lastfm_client=lastfm_client,
            lastfm_listeners=lastfm_listeners,
            listenbrainz_listens=listenbrainz_listens,
            album_lf_listeners=self.ALBUM_LF,
            album_lb_listens=self.ALBUM_LB,
        )

    def test_mid_pack_album_track_not_promoted_by_inflated_artist_z(self, monkeypatch):
        # "Circle the Drain": mid-pack listeners on the album, but its artist_z
        # is inflated to ~3.1 by the standout album.  The composite album-local
        # z is in the medium band -> no z_standout.  The NEW engine semantics:
        # a Last.fm confirmation is a METADATA medium source -> the track is a
        # MEDIUM-confidence single (never 'high'). Before the singles split the
        # engine treated the lone Last.fm source as insufficient and flagged it
        # low; the re-asserted expectation is the new deliberate behaviour
        # (Last.fm single confirmation IS authoritative, and the inflated
        # artist_z must NOT promote it to high/5★).
        result = self._detect(monkeypatch, lastfm_listeners=39262, listenbrainz_listens=55095)
        decision = result["decision"]
        assert decision["artist_z"] > 2.5, decision
        assert decision["z_composite"] < 1.0, decision
        assert decision["z_standout"] is False, result["reasons"]
        assert result["is_single"] is True, result["reasons"]
        assert result["confidence"] == "medium", result["reasons"]

    def test_album_standout_still_promoted(self, monkeypatch):
        # "Slit Wrist Theory": the genuine intra-album standout (119k vs ~40k
        # listeners) keeps its composite z ~2.1 → z_standout + Last.fm → high.
        result = self._detect(monkeypatch, lastfm_listeners=119490, listenbrainz_listens=178820)
        decision = result["decision"]
        assert decision["z_composite"] > 1.5, decision
        assert decision["z_standout"] is True, result["reasons"]
        assert result["is_single"] is True, result["reasons"]
        assert result["confidence"] == "high"
