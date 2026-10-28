"""Remaster-safe popularity matching.

The rule under test:

    SAME PERFORMANCE + DIFFERENT MASTERING
        -> the canonical title may be used as a popularity fallback

    DIFFERENT PERFORMANCE / MIX / ARRANGEMENT
        -> never collapse to the bare song title

Exact lookup is tried first. The canonical fallback is used only when the exact
tier is empty or below the "credible data" floor, and the two tiers are never
summed.
"""

from __future__ import annotations

import pytest

from services.popularity.popularity_matching import normalize_for_aggregation
from services.popularity.title_versioning import (
    classify_popularity_title,
    remaster_performance_key,
)


@pytest.fixture(autouse=True)
def _clear_provider_caches():
    from services.popularity import popularity_cache_service as pcs
    from services.popularity import popularity_sources as ps

    ps._lastfm_artist_catalog_cache.clear()
    ps._lastfm_artist_max_cache.clear()
    ps._ONLINE_CATALOGUE_CACHE.clear()
    pcs.clear_prefetch_memory_caches()
    yield
    ps._lastfm_artist_catalog_cache.clear()
    ps._lastfm_artist_max_cache.clear()
    ps._ONLINE_CATALOGUE_CACHE.clear()
    pcs.clear_prefetch_memory_caches()


class FakeLastFm:
    """Provider stub with separate catalogue / search / direct responses."""

    def __init__(self, *, catalogue=None, search=None, info=None):
        self.catalogue = list(catalogue or [])
        self.search = dict(search or {})
        self.info = dict(info or {})
        self.search_calls: list[tuple] = []
        self.info_calls: list[tuple] = []
        self.top_calls = 0

    def get_artist_top_tracks(self, artist, limit=500):
        self.top_calls += 1
        return list(self.catalogue)

    def search_track(self, artist, title, limit=20):
        self.search_calls.append((artist, title))
        return list(self.search.get(title, []))

    def get_track_info(self, artist, title, track_mbid=None):
        self.info_calls.append((artist, title, track_mbid))
        return dict(self.info.get(title, {}))


# ---------------------------------------------------------------------------
# A/B — a plain remaster safely falls back to the canonical song
# ---------------------------------------------------------------------------

def test_remaster_falls_back_to_canonical_lastfm_title():
    from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

    lf = FakeLastFm(
        search={"Orpheus": [{"name": "Orpheus", "artist": "Ash", "listeners": 400_000, "playcount": 1_200_000}]},
        info={"Orpheus": {"name": "Orpheus", "artist": "Ash", "listeners": 400_000, "playcount": 1_200_000}},
    )
    result = get_aggregated_lastfm_popularity(
        "Ash", "Orpheus (2011 Remaster)", lastfm_client=lf
    )

    assert result["listeners"] == 400_000
    assert result["lookup_title"] == "Orpheus"
    assert result["match_type"] == "canonical_remaster_fallback"
    assert result["matched_title"] == "Orpheus"
    assert result["requested_title"] == "Orpheus (2011 Remaster)"


def test_remastered_suffix_is_eligible_for_canonical_fallback():
    from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

    lf = FakeLastFm(
        info={"Song": {"name": "Song", "artist": "A", "listeners": 5000, "playcount": 9000}},
    )
    result = get_aggregated_lastfm_popularity("A", "Song (Remastered)", lastfm_client=lf)

    assert result["listeners"] == 5000
    assert result["lookup_title"] == "Song"
    assert result["match_type"] == "canonical_remaster_fallback"


def test_exact_remaster_page_wins_when_it_has_credible_evidence():
    from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

    lf = FakeLastFm(
        search={
            "Orpheus (2011 Remaster)": [
                {"name": "Orpheus (2011 Remaster)", "artist": "Ash", "listeners": 5000}
            ],
            "Orpheus": [
                {"name": "Orpheus", "artist": "Ash", "listeners": 400_000}
            ],
        },
    )
    result = get_aggregated_lastfm_popularity(
        "Ash", "Orpheus (2011 Remaster)", lastfm_client=lf
    )

    assert result["listeners"] == 5000
    assert result["match_type"] == "exact"


def test_weak_exact_remaster_page_defers_to_strong_canonical_evidence():
    from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

    lf = FakeLastFm(
        search={
            "Orpheus (2011 Remaster)": [
                {"name": "Orpheus (2011 Remaster)", "artist": "Ash", "listeners": 5}
            ],
            "Orpheus": [
                {"name": "Orpheus", "artist": "Ash", "listeners": 400_000}
            ],
        },
    )
    result = get_aggregated_lastfm_popularity(
        "Ash", "Orpheus (2011 Remaster)", lastfm_client=lf
    )

    assert result["listeners"] == 400_000
    assert result["match_type"] == "canonical_remaster_fallback"


# ---------------------------------------------------------------------------
# C/E/G/H/I/J — protected versions never fall back to the bare title
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    [
        "Song (Live)",
        "Song (Acoustic Version)",
        "Song (Demo)",
        "Song (Radio Edit)",
        "Song (Extended Mix)",
        "Song (Artist Remix)",
        "Song (Instrumental)",
        "Song (Re-recorded Version)",
        "Song (Alternate Take)",
        "Song (Piano Version)",
    ],
)
def test_protected_versions_have_no_bare_canonical_fallback(title):
    candidates = classify_popularity_title(title).lookup_titles()

    assert candidates == ((title, "exact"),)


def test_live_track_does_not_inherit_studio_counts():
    from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

    lf = FakeLastFm(
        catalogue=[
            {"name": "Song", "listeners": 100_000, "playcount": 400_000},
            {"name": "Song (Live)", "listeners": 1200, "playcount": 5000},
        ],
        search={
            "Song (Live)": [
                {"name": "Song", "artist": "A", "listeners": 100_000, "url": "/studio"},
                {"name": "Song (Live)", "artist": "A", "listeners": 1200, "url": "/live"},
            ]
        },
        info={"Song (Live)": {"name": "Song (Live)", "artist": "A", "listeners": 1200, "playcount": 5000}},
    )
    result = get_aggregated_lastfm_popularity("A", "Song (Live)", lastfm_client=lf)

    assert result["listeners"] == 1200
    assert result["lookup_title"] == "Song (Live)"


# ---------------------------------------------------------------------------
# D/F — combined descriptors strip ONLY the mastering half
# ---------------------------------------------------------------------------

def test_live_remaster_canonicalises_to_live_not_studio():
    info = classify_popularity_title("Song (Live - 2011 Remaster)")

    assert info.mastering_stripped_title == "Song (Live)"
    assert info.canonical_song_title == "Song"
    assert [(title, kind) for title, kind in info.lookup_titles()] == [
        ("Song (Live - 2011 Remaster)", "exact"),
        ("Song (Live)", "canonical_remaster_fallback"),
    ]
    assert "Song" not in [title for title, _ in info.lookup_titles()]


def test_acoustic_remaster_canonicalises_to_acoustic_not_studio():
    info = classify_popularity_title("Song (Acoustic Version - Remastered)")

    assert info.mastering_stripped_title == "Song (Acoustic Version)"
    assert "Song" not in [title for title, _ in info.lookup_titles()]


def test_combined_live_remaster_uses_live_lastfm_counts():
    from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

    lf = FakeLastFm(
        search={
            "Song (Live)": [
                {"name": "Song", "artist": "A", "listeners": 900_000, "url": "/studio"},
                {"name": "Song (Live)", "artist": "A", "listeners": 1500, "url": "/live"},
            ]
        },
        info={"Song (Live)": {"name": "Song (Live)", "artist": "A", "listeners": 1500}},
    )
    result = get_aggregated_lastfm_popularity(
        "A", "Song (Live - 2011 Remaster)", lastfm_client=lf
    )

    assert result["listeners"] == 1500
    assert result["lookup_title"] in {"Song (Live - 2011 Remaster)", "Song (Live)"}


def test_combined_acoustic_remaster_uses_acoustic_counts():
    from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

    lf = FakeLastFm(
        search={
            "Song (Acoustic Version)": [
                {"name": "Song", "artist": "A", "listeners": 900_000, "url": "/studio"},
                {"name": "Song (Acoustic Version)", "artist": "A", "listeners": 250, "url": "/acoustic"},
            ]
        },
        info={"Song (Acoustic Version)": {"name": "Song (Acoustic Version)", "artist": "A", "listeners": 250}},
    )
    result = get_aggregated_lastfm_popularity(
        "A", "Song (Acoustic Version - Remastered)", lastfm_client=lf
    )

    assert result["listeners"] == 250
    assert result["lookup_title"] in {
        "Song (Acoustic Version - Remastered)",
        "Song (Acoustic Version)",
    }


# ---------------------------------------------------------------------------
# Normalisation keys
# ---------------------------------------------------------------------------

def test_normalisation_preserves_protected_versions_in_combined_remasters():
    assert normalize_for_aggregation("Song (Live - 2011 Remaster)") == normalize_for_aggregation("Song (Live)")
    assert normalize_for_aggregation("Song (Live - 2011 Remaster)") != normalize_for_aggregation("Song")

    assert normalize_for_aggregation("Song (Acoustic Version - Remastered)") == normalize_for_aggregation(
        "Song (Acoustic Version)"
    )
    assert normalize_for_aggregation("Song (Acoustic Version - Remastered)") != normalize_for_aggregation("Song")


def test_normalisation_still_collapses_mastering_only_variants():
    assert normalize_for_aggregation("Orpheus (2011 Remaster)") == "orpheus"
    assert normalize_for_aggregation("Song (Deluxe Remaster)") == "song"
    assert normalize_for_aggregation("Song (Digital Remaster)") == "song"


def test_radio_edit_and_extended_mix_stay_distinct():
    assert normalize_for_aggregation("Song (Radio Edit)") != normalize_for_aggregation("Song")
    assert normalize_for_aggregation("Song (Extended Mix)") != normalize_for_aggregation("Song")


# ---------------------------------------------------------------------------
# K — artist identity is still required
# ---------------------------------------------------------------------------

def test_same_title_by_a_different_artist_is_not_matched():
    from services.popularity.popularity_sources import get_aggregated_lastfm_popularity

    lf = FakeLastFm(
        search={"Song": [{"name": "Song", "artist": "Other Artist", "listeners": 900_000, "url": "/other"}]},
        info={"Song": {"name": "Song", "artist": "Other Artist", "listeners": 900_000}},
    )
    result = get_aggregated_lastfm_popularity("A", "Song", lastfm_client=lf)

    assert result["listeners"] == 0


# ---------------------------------------------------------------------------
# ListenBrainz — the same exact → safe-remaster tiering
# ---------------------------------------------------------------------------

def test_listenbrainz_remaster_search_falls_back_to_canonical(monkeypatch):
    from services.popularity import popularity_sources as ps

    class _MB:
        def search_recordings(self, query, limit=20, inc=""):
            if "remaster" in query.casefold():
                return []
            return [{
                "id": "rec-orig",
                "title": "Orpheus",
                "artist-credit": [{"artist": {"name": "Ash"}, "name": "Ash"}],
            }]

    monkeypatch.setattr(
        ps,
        "lb_get_recording_popularity_batch",
        lambda mbids: {
            "rec-orig": {"total_listen_count": 400_000, "total_user_count": 50_000}
        },
    )
    result = ps.get_aggregated_listenbrainz_popularity(
        "Orpheus (2011 Remaster)", "Ash", mb_client=_MB()
    )

    assert result["total_listen_count"] == 400_000
    assert result["lookup_title"] == "Orpheus"
    assert result["match_type"] == "canonical_remaster_fallback"


# ---------------------------------------------------------------------------
# L — Work identity alone is not recording identity
# ---------------------------------------------------------------------------
def test_work_id_does_not_make_a_live_recording_the_same_popularity_entity(monkeypatch):
    from services.popularity import popularity_sources as ps

    def _rec(rec_id, title):
        return {
            "id": rec_id,
            "title": title,
            "artist-credit": [{"artist": {"id": "art-1", "name": "A"}, "name": "A"}],
        }

    class _MB:
        def get_recording(self, recording_mbid, inc="", timeout=10.0):
            return {
                "id": "rec-orig",
                "title": "Song",
                "relations": [{"type": "performance", "work": {"id": "work-1", "title": "Song"}}],
                "artist-credit": [{"artist": {"id": "art-1", "name": "A"}, "name": "A"}],
            }

        def browse_work_recordings(self, work_mbid, inc="artist-credits", limit=100):
            return [_rec("rec-orig", "Song"), _rec("rec-live", "Song (Live)")]

    monkeypatch.setattr(
        ps,
        "lb_get_recording_popularity_batch",
        lambda mbids: {
            "rec-orig": {"total_listen_count": 1000, "total_user_count": 100},
            "rec-live": {"total_listen_count": 50_000, "total_user_count": 5000},
        },
    )
    result = ps.get_work_level_listenbrainz_popularity(
        title="Song",
        artist="A",
        artist_mbid="art-1",
        primary_mbid="rec-orig",
        mb_client=_MB(),
    )

    assert "rec-live" not in result["mbids"]
    assert result["total_listen_count"] == 1000


# ---------------------------------------------------------------------------
# M — original + remaster share evidence, never double it
# ---------------------------------------------------------------------------

def test_remaster_and_original_share_one_provider_entity_not_a_sum():
    from services.popularity.popularity_sources import _lastfm_result

    result = _lastfm_result([
        {"name": "Orpheus", "listeners": 400_000, "playcount": 1_000_000, "url": "/orig"},
        {"name": "Orpheus (2011 Remaster)", "listeners": 400_000, "playcount": 1_000_000, "url": "/rm"},
    ])

    assert result["listeners"] == 400_000
    assert remaster_performance_key("Orpheus") == remaster_performance_key("Orpheus (2011 Remaster)")


def test_prefetch_map_does_not_sum_original_and_remaster(monkeypatch):
    from services.popularity import popularity_cache_service as pcs

    monkeypatch.setattr(pcs.cache_repo, "get_cached_popularity_for_titles", lambda *a, **k: {})
    monkeypatch.setattr(pcs.cache_repo, "upsert_track_popularity_bulk", lambda *a, **k: 0)

    class _LF:
        def get_artist_top_tracks(self, artist, limit=500):
            return [
                {"name": "Orpheus", "listeners": 400_000, "playcount": 1_000_000},
                {"name": "Orpheus (2011 Remaster)", "listeners": 400_000, "playcount": 1_000_000},
            ]

    entries = pcs.prefetch_artist_popularity(
        "Ash",
        [{"title": "Orpheus (2011 Remaster)", "recording_mbid": None}],
        lastfm_client=_LF(),
        cache_full_catalogue=False,
    )

    assert entries["orpheus"]["lastfm_listeners"] == 400_000


# ---------------------------------------------------------------------------
# N — a forced rescan replaces a stale provider map
# ---------------------------------------------------------------------------

def test_forced_prefetch_bypasses_a_stale_memory_map(monkeypatch):
    from services.popularity import popularity_cache_service as pcs

    monkeypatch.setattr(pcs.cache_repo, "get_cached_popularity_for_titles", lambda *a, **k: {})
    monkeypatch.setattr(pcs.cache_repo, "upsert_track_popularity_bulk", lambda *a, **k: 0)

    class _LF:
        def __init__(self, listeners):
            self.listeners = listeners
            self.calls = 0

        def get_artist_top_tracks(self, artist, limit=500):
            self.calls += 1
            return [{"name": "Orpheus", "listeners": self.listeners, "playcount": 1}]

    old = _LF(100)
    fresh = _LF(500_000)
    tracks = [{"title": "Orpheus (2011 Remaster)", "recording_mbid": None}]

    first = pcs.prefetch_artist_popularity(
        "Ash", tracks, lastfm_client=old, cache_full_catalogue=False,
    )
    assert first["orpheus"]["lastfm_listeners"] == 100

    second = pcs.prefetch_artist_popularity(
        "Ash", tracks, lastfm_client=fresh, force=True, cache_full_catalogue=False,
    )
    assert second["orpheus"]["lastfm_listeners"] == 500_000
    assert fresh.calls == 1


# ---------------------------------------------------------------------------
# O — missing playcount is UNKNOWN, not a measured zero
# ---------------------------------------------------------------------------

def test_missing_playcount_is_distinguished_from_a_measured_zero():
    from services.popularity.popularity_sources import _lastfm_result

    missing = _lastfm_result([{"name": "Song", "listeners": 400_000}])
    measured = _lastfm_result([
        {"name": "Song", "listeners": 400_000, "playcount": 0}
    ])

    assert missing["listeners"] == 400_000
    assert missing["playcount_available"] is False
    assert measured["playcount_available"] is True


# ---------------------------------------------------------------------------
# Remaster-edition detector (title + genuine reissue year gap)
# ---------------------------------------------------------------------------

def test_remaster_edition_detector_accepts_title_and_year_gap():
    from services.popularity.popularity_sources import _release_is_remaster_edition

    assert _release_is_remaster_edition({"title": "Human Clay (Remastered 2024)"})
    assert _release_is_remaster_edition({
        "title": "Human Clay",
        "date": "2024-09-20",
        "release-group": {"first-release-date": "1999-09-20"},
    })
    assert not _release_is_remaster_edition({
        "title": "Human Clay",
        "date": "2001-09-20",
        "release-group": {"first-release-date": "1999-09-20"},
    })
