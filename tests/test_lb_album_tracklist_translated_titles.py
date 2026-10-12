"""Regression tests for ListenBrainz album-tracklist matching.

Covers the fix where a local track whose title does NOT appear on the
ListenBrainz album tracklist (e.g. the release lists the Korean name
"삐처리" while the library stores the English "BLEEP") is matched by
disc + track number + length instead of being left at ~0 listens.

The motivating case: Stray Kids - BLEEP scored low (lb=5) despite high
Last.fm popularity because ListenBrainz lists the track under the Korean
name, so the normalized-title key never matched.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_shared_mb_client(monkeypatch):
    """Each test fakes ``MusicBrainzHttpClient`` via monkeypatch, but
    ``get_shared_mb_client()`` caches the constructed client in a process-wide
    singleton (``_SHARED_MB_CLIENT``). Without a reset, the FIRST test's fake
    leaks into every later test in the process and its release tracklist
    replaces the later fixture's — the position gate made that order-dependent
    failure visible (the leaked CEREMONY/삐처리 tracklist answered a HERO
    fixture with the wrong release)."""
    import services.enrichment.musicbrainz_service as _mbs

    monkeypatch.setattr(_mbs, "_SHARED_MB_CLIENT", None)
    monkeypatch.setattr(_mbs, "_shared_mb_service", None)
    monkeypatch.setattr(_mbs, "_service", None)
    yield


class _FakeMBClient:
    """Stands in for MusicBrainzHttpClient.get_release tracklist."""

    def __init__(self, media):
        self._media = media

    def get_release(self, release_mbid, inc="", timeout=10.0):
        return {"id": release_mbid, "media": self._media}


_COUNTS = {
    "rec-bleep-en": {"total_listen_count": 5, "total_user_count": 3},
    "rec-bleep-kr": {"total_listen_count": 98765, "total_user_count": 42000},
    "rec-other": {"total_listen_count": 1000, "total_user_count": 500},
}


def _fake_lb_batch(mbids):
    return {
        m: _COUNTS.get(m, {"total_listen_count": 0, "total_user_count": 0})
        for m in mbids
    }


def _run_tracklist(monkeypatch, media, local_tracks, release_mbid="rel-karma"):
    from services.popularity import popularity_sources as ps

    monkeypatch.setattr(
        ps,
        "_resolve_release_mbid",
        lambda artist, album, tracks: release_mbid,
    )
    # The release-first lookup asks ListenBrainz for its cached release
    # metadata first and falls back to MusicBrainz — force the fallback so
    # the fake MusicBrainz tracklist below is the source of truth.
    monkeypatch.setattr(ps, "lb_get_release_metadata_batch", lambda *a, **k: {})
    monkeypatch.setattr(
        "services.enrichment.musicbrainz_service.get_shared_mb_client",
        lambda *a, **k: _FakeMBClient(media),
    )
    monkeypatch.setattr(
        ps,
        "lb_get_recording_popularity_batch",
        _fake_lb_batch,
    )

    return ps.get_listenbrainz_album_tracklist("Stray Kids", "KARMA", local_tracks)


def test_korean_title_matches_by_position_and_length(monkeypatch):
    """'BLEEP' must adopt the Korean-titled recording's counts via position."""
    media = [
        {
            "position": 1,
            "tracks": [
                {
                    "title": "CEREMONY",
                    "position": 1,
                    "number": 1,
                    "length": 180000,
                    "recording": {"id": "rec-other"},
                },
                {
                    # MusicBrainz / ListenBrainz list the track under the
                    # Korean name; the local library stores the English title.
                    "title": "삐처리",
                    "position": 2,
                    "number": 2,
                    "length": 200000,
                    "recording": {"id": "rec-bleep-kr"},
                },
            ],
        }
    ]
    local_tracks = [
        {"title": "CEREMONY", "track_number": "1", "disc_number": "1", "duration": 180.0},
        {"title": "BLEEP", "track_number": "2", "disc_number": "1", "duration": 200.0},
    ]
    result = _run_tracklist(monkeypatch, media, local_tracks)

    # CEREMONY matches by title.
    assert result["ceremony"]["listenbrainz_listens"] == 1000
    # BLEEP must get the Korean recording's real count (not the 5-listen
    # English split), matched by disc+position+length.
    assert result["bleep"]["listenbrainz_listens"] == 98765
    assert result["bleep"]["recording_mbid"] == "rec-bleep-kr"


def test_mismatched_length_does_not_false_positive(monkeypatch):
    """Position match is gated by length — a different-length track must not
    adopt another track's counts."""
    media = [
        {
            "position": 1,
            "tracks": [
                {
                    "title": "Wrong Song",
                    "position": 3,
                    "number": 3,
                    "length": 210000,
                    "recording": {"id": "rec-other"},
                },
            ],
        }
    ]
    local_tracks = [
        {"title": "Some Other Track", "track_number": "3", "disc_number": "1", "duration": 120.0},
    ]
    result = _run_tracklist(monkeypatch, media, local_tracks)

    # Length differs by 90s — must NOT match.
    assert "some other track" not in result


def test_position_match_only_fires_when_title_missing(monkeypatch):
    """A title that already matched keeps its (correct) recording."""
    media = [
        {
            "position": 1,
            "tracks": [
                {
                    "title": "HERO",
                    "position": 1,
                    "number": 1,
                    "length": 180000,
                    "recording": {"id": "rec-other"},
                },
            ],
        }
    ]
    local_tracks = [
        {"title": "HERO", "track_number": "1", "disc_number": "1", "duration": 180.0},
    ]
    result = _run_tracklist(monkeypatch, media, local_tracks)

    assert result["hero"]["listenbrainz_listens"] == 1000


def test_disc_number_matters_for_position_match(monkeypatch):
    """Track 2 on disc 2 must not adopt track 2 on disc 1's counts."""
    media = [
        {
            "position": 1,
            "tracks": [
                {
                    "title": "First Disc",
                    "position": 2,
                    "number": 2,
                    "length": 200000,
                    "recording": {"id": "rec-other"},
                },
            ],
        }
    ]
    local_tracks = [
        {"title": "BLEEP", "track_number": "2", "disc_number": "2", "duration": 200.0},
    ]
    result = _run_tracklist(monkeypatch, media, local_tracks)

    assert "bleep" not in result
