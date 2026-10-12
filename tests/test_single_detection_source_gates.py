"""Regression tests: single-detection source gates after a primary confirm.

The user observed that once MusicBrainz confirms a track as a single, the
pipeline stopped checking some corroborating sources — the ``Matched:``
line only ever showed the sources checked before the gate fired.

Two gates were relaxed:

1. **ISRC lookup now runs for EVERY track with an ISRC**, even when
   MusicBrainz already confirmed via its release-group.  A track can be a
   single on one release while its recording's release-groups on other
   releases carry different primary types; the ISRC path (bounded to
   tracks that HAVE an ISRC, MB-cached) adds genuine cross-release
   corroboration.

2. **Discogs-video corroboration is configurable**: by default it still
   runs only when neither Discogs nor MusicBrainz confirmed (rate-limited
   Discogs API call); set
   ``single_detection.always_check_discogs_video: true`` to also run it
   after a primary confirm so a track that is BOTH an MB single AND has a
   Discogs video shows both.
"""

from __future__ import annotations


class _FakeMBClient:
    """MB client that confirms a single via release-group AND via ISRC."""

    def __init__(self, isrc_singles=True):
        self._isrc_singles = isrc_singles
        self.isrc_calls = 0

    def search_release_groups(self, query, limit=10):
        # Return a single release-group for any query → MB confirms.
        return [{"id": "rg-1", "title": "Song", "primary-type": "Single"}]

    def lookup_by_isrc(self, isrc, inc=""):
        self.isrc_calls += 1
        if not self._isrc_singles:
            return []
        return [{
            "id": "rec-1",
            "releases": [{
                "release-group": {"id": "rg-2", "primary-type": "Single"},
            }],
        }]

    def is_single(self, *args, **kwargs):
        return False


class _FakeMBService:
    """Stands in for ``musicbrainz_service.get_shared_mb_service``.

    ``_detect_musicbrainz`` falls back to the SERVICE-level ``is_single``
    when the raw client found nothing (advanced mode). The raw fake client
    cannot cover that; without the stub the service would construct a real
    shared client and hit the network.
    """

    def is_single(self, *args, **kwargs):
        return False


def _detect(title="Song", artist="Artist", isrc="US1234567890", mb_client=None, **kw):
    from services.enrichment.single_detection_service import detect_single_for_track

    # ``use_advanced_detection`` arrives via ``**kw`` from the individual
    # tests — it must NOT be hard-coded here or a test that passes it
    # explicitly (e.g. ``_detect(..., use_advanced_detection=True)``) raises
    # ``TypeError: got multiple values for keyword argument``.
    return detect_single_for_track(
        title=title,
        artist=artist,
        album="Album",
        album_track_count=12,
        isrc=isrc,
        mb_client=mb_client,
        persist_result=False,
        is_va_compilation=False,
        **kw,
    )


class TestISRCStillCheckedAfterMBConfirm:
    def test_isrc_lookup_runs_even_when_mb_confirms(self):
        # MB confirms via release-group.  With an ISRC present, the ISRC
        # lookup must STILL run (it used to be gated on
        # ``not musicbrainz_confirmed``) — a track can be a single on one
        # release while its ISRC's canonical recording carries different
        # release-groups.
        client = _FakeMBClient(isrc_singles=True)
        result = _detect(mb_client=client)

        assert client.isrc_calls == 1
        # The ISRC confirmation is a medium source → appears in sources.
        sources = result.get("sources") or []
        assert any(
            isinstance(s, dict) and s.get("source") == "isrc" and s.get("matched")
            for s in sources
        )

    def test_no_isrc_no_lookup(self):
        client = _FakeMBClient(isrc_singles=True)
        result = _detect(isrc=None, mb_client=client)

        assert client.isrc_calls == 0
        assert result is not None


class TestDiscogsVideoConfigurable:
    def test_default_skips_discogs_video_after_mb_confirm(self, monkeypatch):
        from services.enrichment import single_detection_service as sds

        calls = {}

        def _fake_video(title, artist, discogs_token=None):
            calls["called"] = True
            return {"source": "discogs_video", "matched": True, "confidence": 0.5, "metadata": {}}

        monkeypatch.setattr(sds, "_detect_discogs_video", _fake_video)
        monkeypatch.setattr(sds, "_always_check_discogs_video", lambda: False)
        # Advanced mode's ``_detect_musicbrainz`` falls back to the SERVICE
        # ``is_single`` (get_shared_mb_service) — stub it so no real MusicBrainz
        # network call happens in this test.
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service",
            lambda *a, **k: _FakeMBService(),
        )

        client = _FakeMBClient(isrc_singles=False)
        _detect(mb_client=client, use_advanced_detection=True, artist_mbid="artist-1")

        # Default: Discogs-video NOT checked after MB confirmed.
        assert not calls.get("called")

    def test_enabled_checks_discogs_video_after_mb_confirm(self, monkeypatch):
        from services.enrichment import single_detection_service as sds

        calls = {}

        def _fake_video(title, artist, discogs_token=None):
            calls["called"] = True
            return {"source": "discogs_video", "matched": True, "confidence": 0.5, "metadata": {}}

        monkeypatch.setattr(sds, "_detect_discogs_video", _fake_video)
        monkeypatch.setattr(sds, "_always_check_discogs_video", lambda: True)
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service",
            lambda *a, **k: _FakeMBService(),
        )

        client = _FakeMBClient(isrc_singles=False)
        result = _detect(mb_client=client, use_advanced_detection=True, artist_mbid="artist-1")

        # With the config flag on, Discogs-video runs and corroborates.
        assert calls.get("called")
        sources = result.get("sources") or []
        assert any(
            isinstance(s, dict) and s.get("source") == "discogs_video" and s.get("matched")
            for s in sources
        )

    def test_config_default_is_false(self):
        from services.enrichment.single_detection_service import _always_check_discogs_video

        assert _always_check_discogs_video() is False
