"""Regression tests for diacritic folding on the Last.fm lookup path.

Last.fm indexes an accented artist under its ASCII spelling — ``Lïve`` lives
in the catalogue as ``Live``, ``Motörhead`` as ``Motorhead``.  The lookup chain
compared spellings WITHOUT folding, so the real global row scored 0 on the
artist-match gate and was discarded as a mismatch.  The scan then fell back to
a near-empty object carrying a few hundred listeners instead of the millions
the track really has, and the rating was computed from that deflated number.

Reported symptom: every track on *Throwing Copper* showed ``LF:`` counts in the
hundreds (``Lightning Crashes ... LF: 589``) for a band with millions of global
plays.

These tests pin the fix at every point on that chain:

1. ``strip_diacritics`` folds, and leaves already-ASCII text alone.
2. ``artist_match_score`` accepts the folded pair (the gate that discarded it).
3. ``build_artist_lookup_candidates`` offers the folded spelling.
4. ``get_artist_top_tracks`` retries the folded spelling when the verbatim one
   is empty — and does NOT make a second request when the first succeeds.
5. ``normalize_for_aggregation`` no longer splits an accented word into
   ``caf `` / ``hopp polla``.
6. ``make_artist_match_key`` puts both spellings in ONE cache bucket.
7. End-to-end: an accented artist now resolves the real catalogue counts.
"""

from __future__ import annotations

import importlib

import pytest


def _fold(value):
    """``strip_diacritics``, imported LAZILY.

    A module-scope import of the new symbol turns this whole file into a
    single COLLECTION ERROR on the unpatched tree, which hides every
    individual verdict and makes the oracle useless.
    """
    module = importlib.import_module("helpers.normalization_service")
    return module.strip_diacritics(value)


def _normalize_string(value):
    module = importlib.import_module("helpers.normalization_service")
    return module.normalize_string(value)


# ---------------------------------------------------------------------------
# 1. The folding helper
# ---------------------------------------------------------------------------

class TestStripDiacritics:
    """The shared helper every other fix builds on."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Lïve", "Live"),
            ("Motörhead", "Motorhead"),
            ("Björk", "Bjork"),
            ("Sigur Rós", "Sigur Ros"),
            ("Beyoncé", "Beyonce"),
            ("Café", "Cafe"),
            ("Hoppípolla", "Hoppipolla"),
        ],
    )
    def test_folds_accented_letters_to_ascii(self, raw, expected):
        assert _fold(raw) == expected

    def test_ascii_text_unchanged(self):
        # A plain name must survive untouched, or every existing cache key moves.
        assert _fold("Lightning Crashes") == "Lightning Crashes"
        assert _fold("Throwing Copper") == "Throwing Copper"

    def test_casing_and_punctuation_preserved(self):
        # Only combining marks are removed — safe to run before other compares.
        assert _fold("Lïve!") == "Live!"
        assert _fold("SIGUR RÓS") == "SIGUR ROS"

    def test_empty_and_none_safe(self):
        assert _fold("") == ""
        assert _fold(None) == ""

    def test_matches_normalize_string_folding(self):
        # Both must agree, or the comparator and the cache keys would disagree.
        from services.enrichment.lastfm_service import LastFmService

        for value in ("Lïve", "Motörhead", "Sigur Rós"):
            assert LastFmService.normalize_artist_for_compare(value) == _normalize_string(value)


# ---------------------------------------------------------------------------
# 2. The artist-match gate
# ---------------------------------------------------------------------------

class TestArtistMatchScoreFoldsDiacritics:
    """``artist_match_score`` is the gate: < 60 discards the candidate."""

    def _score(self, query, returned):
        from services.enrichment.lastfm_service import LastFmService

        return LastFmService.artist_match_score(query, returned)

    def test_accented_query_matches_ascii_return(self):
        """The reported failure: this returned 0, so the real row was dropped."""
        assert self._score("Lïve", "Live") >= 90

    def test_ascii_query_matches_accented_return(self):
        # Symmetric — the provider may answer either way.
        assert self._score("Live", "Lïve") >= 90

    @pytest.mark.parametrize(
        "query,returned",
        [
            ("Motörhead", "Motorhead"),
            ("Sigur Rós", "Sigur Ros"),
            ("Beyoncé", "Beyonce"),
        ],
    )
    def test_other_accented_artists_also_match(self, query, returned):
        assert self._score(query, returned) >= 90

    def test_identical_accented_spelling_still_exact(self):
        assert self._score("Lïve", "Lïve") == 100

    def test_unrelated_artists_still_rejected(self):
        # The gate must keep doing its job — folding must not make it permissive.
        assert self._score("Lïve", "Nirvana") < 60
        assert self._score("Live", "Live Aid") < 60


# ---------------------------------------------------------------------------
# 3. Lookup candidates
# ---------------------------------------------------------------------------

class TestArtistLookupCandidatesIncludeFoldedSpelling:
    def _candidates(self, artist):
        from services.enrichment.lastfm_service import LastFmService

        return LastFmService.build_artist_lookup_candidates(artist)

    def test_folded_spelling_is_offered(self):
        candidates = self._candidates("Lïve")
        assert "Live" in candidates, (
            "the ASCII spelling must be queried — the provider indexes only it"
        )

    def test_original_spelling_kept_first(self):
        candidates = self._candidates("Lïve")
        assert candidates[0] == "Lïve"

    def test_ascii_artist_not_duplicated(self):
        # No accent to fold -> no duplicate candidate.
        assert self._candidates("Live") == ["Live"]
        assert self._candidates("Nirvana") == ["Nirvana"]

    def test_folded_primary_strip_of_featured_credit(self):
        candidates = self._candidates("Lïve feat. Guest")
        assert "Live" in candidates


# ---------------------------------------------------------------------------
# 4. The catalogue fetch — the numbers actually come from here
# ---------------------------------------------------------------------------

class _RecordingHttp:
    """Stand-in for the Last.fm HTTP client, recording every query."""

    def __init__(self, data_by_artist):
        self.data_by_artist = data_by_artist
        self.queries: list[str] = []

    def get_json(self, method, **params):
        assert method == "artist.getTopTracks"
        artist = params.get("artist")
        self.queries.append(artist)
        tracks = self.data_by_artist.get(artist)
        if tracks is None:
            return {}
        return {"toptracks": {"track": tracks}}


class _RecordingService:
    """Bind the REAL method onto a stub so the shipped logic is exercised."""

    def __init__(self, http):
        from services.enrichment.lastfm_service import LastFmService

        self.api_key = "test-key"
        self.http = http
        self._get = LastFmService.get_artist_top_tracks.__get__(self, type(self))
        self._clean = LastFmService.clean_spaces

    def clean_spaces(self, text):
        return self._clean(text)

    def get_artist_top_tracks(self, artist, limit=100):
        return self._get(artist, limit)


CATALOGUE = [
    {"name": "Lightning Crashes", "listeners": 1_850_000, "playcount": 24_000_000},
    {"name": "I Alone", "listeners": 980_000, "playcount": 9_400_000},
]


class TestArtistTopTracksFoldedRetry:
    def test_retries_folded_spelling_when_verbatim_is_empty(self):
        # The reported condition: Last.fm has nothing indexed under "Lïve".
        http = _RecordingHttp({"Live": CATALOGUE})
        service = _RecordingService(http)

        tracks = service.get_artist_top_tracks("Lïve")

        assert http.queries == ["Lïve", "Live"]
        assert tracks == CATALOGUE

    def test_no_second_request_when_first_succeeds(self):
        # An ASCII artist must not pay for a second round trip.
        http = _RecordingHttp({"Live": CATALOGUE})
        service = _RecordingService(http)

        tracks = service.get_artist_top_tracks("Live")

        assert http.queries == ["Live"]
        assert tracks == CATALOGUE

    def test_accented_artist_indexed_under_accent_still_works(self):
        # Some artists ARE indexed under the accented form — do not discard it.
        http = _RecordingHttp({"Lïve": CATALOGUE})
        service = _RecordingService(http)

        tracks = service.get_artist_top_tracks("Lïve")

        assert http.queries == ["Lïve"]
        assert tracks == CATALOGUE

    def test_returns_empty_when_neither_spelling_has_data(self):
        http = _RecordingHttp({})
        service = _RecordingService(http)

        tracks = service.get_artist_top_tracks("Lïve")

        assert tracks == []
        assert http.queries == ["Lïve", "Live"]

    def test_no_api_key_returns_empty_without_querying(self):
        http = _RecordingHttp({"Live": CATALOGUE})
        service = _RecordingService(http)
        service.api_key = ""

        assert service.get_artist_top_tracks("Lïve") == []
        assert http.queries == []


# ---------------------------------------------------------------------------
# 5. Title aggregation key
# ---------------------------------------------------------------------------

class TestNormalizeForAggregationFoldsDiacritics:
    """The combining mark used to become a SPACE and split the word."""

    def _nfa(self, title):
        from services.popularity.popularity_matching import normalize_for_aggregation

        return normalize_for_aggregation(title)

    @pytest.mark.parametrize(
        "accented,ascii_form",
        [
            ("Café", "Cafe"),
            ("Hoppípolla", "Hoppipolla"),
            ("Jóga", "Joga"),
            ("Motörhead - Ace of Spades", "Motorhead - Ace of Spades"),
            ("Björk - Jóga", "Bjork - Joga"),
        ],
    )
    def test_accented_and_ascii_collapse_to_one_key(self, accented, ascii_form):
        assert self._nfa(accented) == self._nfa(ascii_form)

    def test_no_stray_space_inserted(self):
        # The exact defect: "Café" produced "caf " (word split by the mark).
        assert self._nfa("Café") == "cafe"
        assert self._nfa("Hoppípolla") == "hoppipolla"
        assert " " not in self._nfa("Café").strip()

    def test_plain_titles_unaffected(self):
        # The reported album's own titles are ASCII — their keys must not move.
        assert self._nfa("Lightning Crashes") == "lightning crashes"
        assert self._nfa("Selling the Drama") == "selling the drama"
        assert self._nfa("I Alone") == "i alone"
        assert self._nfa("All Over You") == "all over you"

    def test_existing_normalisation_behaviour_preserved(self):
        # Regression guard on the surrounding pipeline (feat./remaster/cover).
        assert self._nfa("Herzblut (feat. Melissa Bonny)") == "herzblut"
        assert self._nfa("Song (Remastered)") == "song"
        assert self._nfa("Gangnam Style (PSY Cover)") == "gangnam style"


# ---------------------------------------------------------------------------
# 6. Cache grouping keys
# ---------------------------------------------------------------------------

class TestMatchKeysFoldDiacritics:
    def test_artist_key_identical_for_both_spellings(self):
        from services.popularity.popularity_matching import make_artist_match_key

        # NFKC alone kept the umlaut, splitting the cache into two buckets.
        assert make_artist_match_key("Lïve") == make_artist_match_key("Live") == "live"
        assert make_artist_match_key("Motörhead") == make_artist_match_key("Motorhead")

    def test_track_key_identical_for_both_spellings(self):
        from services.popularity.popularity_matching import make_track_match_key

        assert (
            make_track_match_key("Björk", "Jóga")
            == make_track_match_key("Bjork", "Joga")
        )

    def test_distinct_tracks_stay_distinct(self):
        from services.popularity.popularity_matching import make_track_match_key

        assert (
            make_track_match_key("Lïve", "I Alone")
            != make_track_match_key("Live", "Lightning Crashes")
        )


# ---------------------------------------------------------------------------
# 7. End-to-end: the reported scenario
# ---------------------------------------------------------------------------

class _AggregationClient:
    """Fake client with the real catalogue keyed under the ASCII spelling."""

    def __init__(self):
        self.top_calls: list[str] = []
        self.search_calls: list[tuple] = []

    def get_artist_top_tracks(self, artist, limit=200):
        self.top_calls.append(artist)
        if _fold(artist) != "Live":
            return []
        return [
            {"name": "Lightning Crashes", "listeners": 1_850_000, "playcount": 24_000_000},
            {"name": "All Over You", "listeners": 640_000, "playcount": 5_200_000},
            {"name": "I Alone", "listeners": 980_000, "playcount": 9_400_000},
            {"name": "Selling the Drama", "listeners": 720_000, "playcount": 6_100_000},
        ]

    def search_track(self, artist, title, limit=20):
        self.search_calls.append((artist, title, limit))
        return []

    def get_track_info(self, artist, title, track_mbid=None):
        # The near-empty object the provider returned before: a few hundred.
        return {"listeners": 589, "track_play": 1200}


@pytest.fixture(autouse=True)
def _clear_catalog_caches():
    """Both artist-catalogue caches are module-level — reset them per test."""
    from services.popularity import popularity_sources as _ps
    from services.popularity import popularity_cache_service as _pcs

    for cache in (
        getattr(_ps, "_lastfm_artist_catalog_cache", None),
        getattr(_pcs, "_lf_top_tracks_cache", None),
        getattr(_pcs, "_lf_top_tracks_titles", None),
        getattr(_pcs, "_lf_top_tracks_tags", None),
    ):
        if isinstance(cache, dict):
            cache.clear()
    yield
    for cache in (
        getattr(_ps, "_lastfm_artist_catalog_cache", None),
        getattr(_pcs, "_lf_top_tracks_cache", None),
        getattr(_pcs, "_lf_top_tracks_titles", None),
        getattr(_pcs, "_lf_top_tracks_tags", None),
    ):
        if isinstance(cache, dict):
            cache.clear()


class TestReportedScenarioEndToEnd:
    """An accented artist must reach the REAL global listener counts."""

    def _aggregate(self, artist, title):
        from services.popularity.popularity_sources import (
            get_aggregated_lastfm_popularity,
        )

        client = _AggregationClient()
        result = get_aggregated_lastfm_popularity(
            artist=artist,
            track_title=title,
            lastfm_client=client,
        )
        return client, result

    def test_accented_artist_scores_global_listeners_not_local(self):
        """The bug: 589 listeners. The fix: the real global figure."""
        _client, result = self._aggregate("Lïve", "Lightning Crashes")

        assert result["listeners"] == 1_850_000
        assert result["listeners"] > 500_000, (
            "a headline single must not be scored from a few hundred listeners"
        )

    def test_every_throwing_copper_track_resolves(self):
        expected = {
            "Lightning Crashes": 1_850_000,
            "I Alone": 980_000,
            "Selling the Drama": 720_000,
            "All Over You": 640_000,
        }
        for title, listeners in expected.items():
            _client, result = self._aggregate("Lïve", title)
            assert result["listeners"] == listeners, f"{title} should resolve"

    def test_ascii_spelling_still_resolves(self):
        """No regression for the artists that were always fine."""
        _client, result = self._aggregate("Live", "Lightning Crashes")
        assert result["listeners"] == 1_850_000

    def test_unaccented_plain_artist_unaffected(self):
        from services.popularity.popularity_sources import (
            get_aggregated_lastfm_popularity,
        )

        class _Plain:
            def get_artist_top_tracks(self, artist, limit=200):
                return [{"name": "Ace of Spades", "listeners": 2_100_000, "playcount": 1}]

            def search_track(self, *a, **k):
                return []

            def get_track_info(self, *a, **k):
                return {"listeners": 0, "track_play": 0}

        result = get_aggregated_lastfm_popularity(
            artist="Motörhead",
            track_title="Ace of Spades",
            lastfm_client=_Plain(),
        )
        assert result["listeners"] == 2_100_000
