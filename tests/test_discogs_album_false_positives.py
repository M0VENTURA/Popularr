"""Regression tests: Discogs must not confirm every album track as a single.

Reproduces the Soilwork "Övergivenheten" scan, where all 14 tracks were flagged
with a Discogs source (only the title track is a genuine Discogs single):

1. The split-single similarity branch returned 0.95 for ANY slash-less title —
   the "primary side" of a title without " / " IS the whole title, so every
   track matched the first single/EP candidate (e.g. "Is It in Your Darkness"
   matched the "Departure Plan / Rejection Role" promo single).
2. The format check accepted any row containing the substring "single"/"ep"
   without rejecting Album/LP rows, and never handled Discogs' comma-joined
   STRING formats (the artist-releases endpoint returns "CD, Single, Enh",
   not a list) — so singles on the artist page were invisible.
3. A fuzzy/unverified Discogs match still marked the track as a single. Discogs
   now only confirms a single on HIGH-confidence (near-exact verified) matches.
"""

from __future__ import annotations

from services.enrichment.discogs_service import (
    DiscogsService,
    calculate_discogs_confidence,
    _discogs_title_similarity,
)


def _svc() -> DiscogsService:
    return DiscogsService(token="test-token", http_client=None, enabled=True)


# ── Realistic catalogue mirroring the Soilwork artist page ──────────────────
# release rows from the artist-releases endpoint carry format as a comma-joined
# STRING; resolved master rows carry it as a list of descriptor strings plus a
# track count (both shapes are handled by ``_scan_releases``).
_CATALOGUE = [
    {"title": "Steel Bath Suicide", "format": ["CD", "Album"], "track_count": 11, "role": "Main"},
    {"title": "Övergivenheten", "format": ["Vinyl", "LP", "Album"], "track_count": 14, "role": "Main"},
    {"title": "Övergivenheten ", "format": ["File", "AAC", "Single", "Stereo"], "track_count": 1, "role": "Main"},
    {"title": "Beyond The Infinite", "format": ["CD", "EP"], "track_count": 5, "role": "Main"},
    {"title": "Departure Plan / Rejection Role", "format": ["CD", "Single", "Promo"], "track_count": 2, "role": "Main"},
    {"title": "Nerve", "format": "CD, Maxi, Promo", "role": "Main"},
    {"title": "Underworld", "format": "4xFile, MP3, EP", "role": "Main"},
    {"title": "Figure Number Five / Natural Born Chaos",
     "format": ["Vinyl", "LP", "Album", "Compilation"], "track_count": 23, "role": "Main"},
]

_TRACKS = [
    "Valleys of Gloam", "Övergivenheten", "Nous sommes la guerre", "Dreams of Nowhere",
    "Is It in Your Darkness", "Electric Again", "Morgongåva/Stormfågel", "Vultures",
    "Harvest Spine", "This Godless Universe", "Golgata", "The Everlasting Flame",
]


class TestAlbumTracksNotFlaggedAsSingles:
    """The core false-positive fix: deep album cuts get no Discogs confirmation."""

    def test_deep_cuts_do_not_match_any_single(self):
        svc = _svc()
        for title in (
            "Is It in Your Darkness", "Electric Again", "Vultures", "Golgata",
            "The Everlasting Flame", "Harvest Spine",
        ):
            status = svc._scan_releases(title, _CATALOGUE, artist_verified=True)
            assert status is None, f"{title!r} should not match a Discogs single"

    def test_genuine_title_track_single_still_confirms(self):
        svc = _svc()
        status = svc._scan_releases("Övergivenheten", _CATALOGUE, artist_verified=True)
        assert status is not None
        assert status["is_single"] is True
        assert "single" in status["format"]

    def test_album_row_never_confirms_even_on_containment(self):
        # An album whose title contains the track title must still be rejected —
        # a 14-track LP is structural proof it is not a single.
        svc = _svc()
        catalogue = [
            {"title": "A Predator's Portrait", "format": ["CD", "Album"], "track_count": 10, "role": "Main"},
        ]
        status = svc._scan_releases("Predator", catalogue, artist_verified=True)
        assert status is None


class TestStringFormatHandling:
    """Artist-releases rows carry format as a comma-joined string."""

    def test_string_format_single_is_detected(self):
        svc = _svc()
        catalogue = [{"title": "Valleys of Gloam", "format": "File, MP3, Single", "role": "Main"}]
        status = svc._scan_releases("Valleys of Gloam", catalogue, artist_verified=True)
        assert status is not None
        assert status["is_single"] is True

    def test_string_format_promo_is_detected_as_promo(self):
        svc = _svc()
        catalogue = [{"title": "Nerve", "format": "CD, Maxi, Promo", "role": "Main"}]
        status = svc._scan_releases("Nerve", catalogue, artist_verified=True)
        assert status is not None
        assert status["is_promo"] is True

    def test_album_rejected_when_format_is_a_string(self):
        svc = _svc()
        catalogue = [{"title": "Övergivenheten", "format": "CD, Album", "role": "Main"}]
        status = svc._scan_releases("Övergivenheten", catalogue, artist_verified=True)
        assert status is None


class TestSplitSingleSimilarity:
    """Slash-less titles must never borrow the split-single 0.95 shortcut."""

    def test_slash_less_title_does_not_match_split_single(self):
        sim = _discogs_title_similarity("Is It in Your Darkness", "Departure Plan / Rejection Role")
        assert sim < 0.75

    def test_split_primary_side_still_matches(self):
        assert _discogs_title_similarity("Departure Plan", "Departure Plan / Rejection Role") >= 0.95
        assert _discogs_title_similarity(
            "They're All Around Us / New Way Boy", "They're All Around Us"
        ) >= 0.95


class TestHighConfidenceOnly:
    """Discogs confirms a single only on HIGH-confidence matches."""

    def test_exact_verified_match_confirms(self):
        result = calculate_discogs_confidence("Övergivenheten", 1.0, True)
        assert result["matched"] is True
        assert result["confidence"] >= 0.85

    def test_fuzzy_match_does_not_confirm(self):
        # "Dreams of Nowhere" vs the unrelated "Nerve" promo scored 0.75 —
        # well above the old 0.40 gate, so it confirmed. 0.85 × 0.75 = 0.64 is
        # below the high-confidence threshold → never confirms a single.
        result = calculate_discogs_confidence("Dreams of Nowhere", 0.75, True)
        assert result["matched"] is False
        assert result["confidence"] < 0.85

    def test_unverified_match_never_confirms(self):
        result = calculate_discogs_confidence("Some Track", 1.0, False)
        assert result["matched"] is False
