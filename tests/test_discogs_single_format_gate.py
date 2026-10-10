"""Regression tests: a Discogs single whose artist-releases row carries no
single/EP TYPE token.

Reported (2026-10-10): AFI's "Leaving Song Part II" is a single on Discogs (the
release "The Leaving Song Pt. II") but was never detected. The report hypothesised
the "Pt." vs "Part" wording was the cause — that is WRONG: the shipped title
matcher scores the pair 0.947 (gate 0.75).

The real cause is the ``format`` gate in ``_scan_releases``:

* It required a single/EP TYPE token BEFORE it ever computed title similarity.
* The Discogs ``/artists/{id}/releases`` endpoint does NOT carry that token —
  it reports the PHYSICAL medium ("CD", "Vinyl") or nothing. The type lives in
  the release DETAIL's ``formats[].descriptions``, which
  ``resolve_master_formats`` recovers only for the first
  ``_MAX_MASTER_FORMAT_RESOLUTIONS`` (15) masters.

So a catalogue-heavy artist's single arrived format-less and was skipped before
its near-exact title match was ever computed.

A SECOND, independent defect made the safety net dead too: the global-search
fallback filtered results by ``_release_artist_matches(result["artist"], artist)``,
but Discogs search results often OMIT ``artist`` (the credit lives in the title
"AFI - The Leaving Song Pt. II"), so every result was dropped.

Fix:
* ``_scan_releases`` gates on the LOCAL title similarity FIRST, then resolves the
  format on demand for a title-matching release that has no single/EP token
  (``_resolve_release_format``, cached on the release dict).
* ``_search_result_matches_artist`` matches the title's leading credit segment
  when the ``artist`` field is absent.
"""

from __future__ import annotations

from services.enrichment.discogs_service import (
    MIN_DISCOGS_SIMILARITY,
    DiscogsService,
)

REPORTED_TRACK = "Leaving Song Part II"
DISCOGS_SINGLE = "The Leaving Song Pt. II"


def _fallback_helper():
    """Lazy accessor for the NEW symbol.

    Imported inside the function so that reverting ``discogs_service`` makes
    each test fail for its own reason instead of turning the whole module into
    a single collection ERROR (which would hide every individual verdict).
    """
    from services.enrichment.discogs_service import _search_result_matches_artist
    return _search_result_matches_artist


class _StubHttp:
    """Release-detail stub: the SINGLE type lives in formats[].descriptions."""

    def __init__(self, formats=None, tracklist=None):
        self._formats = formats if formats is not None else [
            {"name": "CD", "descriptions": ["Single"]}
        ]
        self._tracklist = tracklist if tracklist is not None else [
            {"position": "1"},
            {"position": "2"},
        ]
        self.get_release_calls = 0

    def get_release(self, release_id):
        self.get_release_calls += 1
        return {"formats": self._formats, "tracklist": self._tracklist}

    def search_database(self, params, timeout=10.0):
        return []


def _svc(http=None):
    svc = DiscogsService.__new__(DiscogsService)
    svc.http = http or _StubHttp()
    svc.token = "t"
    svc.enabled = True
    return svc


def _release(**over):
    rel = {
        "id": "1",
        "title": DISCOGS_SINGLE,
        "role": "Main",
        "type": "release",
        "year": 2006,
    }
    rel.update(over)
    return rel


class TestTitleMatcherAlreadyHandlesAbbreviations:
    """The reported "Pt." hypothesis is disproven — pin it so it is not re-litigated."""

    def test_pt_vs_part_is_well_above_the_gate(self):
        from services.enrichment.discogs_service import _discogs_title_similarity

        sim = _discogs_title_similarity(REPORTED_TRACK, DISCOGS_SINGLE)
        assert sim >= MIN_DISCOGS_SIMILARITY
        assert sim >= 0.90, "the abbreviation alone scores near-exact"


class TestScanReleasesResolvesMissingFormat:
    """A format-less title-matching release now has its format fetched on demand."""

    def test_format_absent_single_is_detected(self):
        rel = _release()  # no `format` at all
        status = _svc()._scan_releases(REPORTED_TRACK, [rel], artist_verified=True)
        assert status is not None
        assert status["is_single"] is True
        assert status["similarity"] >= MIN_DISCOGS_SIMILARITY

    def test_physical_only_format_single_is_detected(self):
        # "CD" is a medium, not a type — the type must be fetched.
        status = _svc()._scan_releases(
            REPORTED_TRACK, [_release(format=["CD"])], artist_verified=True
        )
        assert status is not None and status["is_single"] is True

    def test_master_row_without_format_is_detected(self):
        rel = _release(type="master", main_release="99")
        status = _svc()._scan_releases(REPORTED_TRACK, [rel], artist_verified=True)
        assert status is not None and status["is_single"] is True

    def test_already_resolved_single_needs_no_extra_fetch(self):
        http = _StubHttp()
        rel = _release(format=["CD", "Single"], track_count=2)
        status = _svc(http)._scan_releases(REPORTED_TRACK, [rel], artist_verified=True)
        assert status is not None and status["is_single"] is True
        # The row already carried the type token — no release-detail call.
        assert http.get_release_calls == 0

    def test_resolved_values_are_cached_on_the_release_dict(self):
        rel = _release()
        _svc()._scan_releases(REPORTED_TRACK, [rel], artist_verified=True)
        # Cached so the next track of the same artist does not re-fetch.
        assert rel.get("format")
        assert rel.get("track_count") == 2


class TestScanReleasesStillRejectsNonSingles:
    """The fix must not widen detection to albums or wrong titles (controls)."""

    def test_album_format_is_still_rejected(self):
        http = _StubHttp(formats=[{"name": "CD", "descriptions": ["Album"]}])
        rel = _release(format=["CD", "Album"])
        assert _svc(http)._scan_releases(REPORTED_TRACK, [rel]) is None

    def test_fetched_album_format_is_still_rejected(self):
        # Fetched detail says Album → must not be promoted to a single.
        http = _StubHttp(formats=[{"name": "CD", "descriptions": ["Album"]}])
        assert _svc(http)._scan_releases(REPORTED_TRACK, [_release()]) is None

    def test_wrong_title_is_still_rejected_without_fetching(self):
        http = _StubHttp()
        assert _svc(http)._scan_releases("Totally Unrelated Song", [_release()]) is None
        # The title gate is local and runs first — no network for a non-match.
        assert http.get_release_calls == 0

    def test_non_main_role_is_still_rejected(self):
        assert _svc()._scan_releases(
            REPORTED_TRACK, [_release(role="Appearance")]
        ) is None


class TestSearchResultMatchesArtist:
    """The fallback filter accepts a title-embedded credit (the second defect)."""

    def test_artist_field_present_matches(self):
        assert _fallback_helper()(
            {"title": DISCOGS_SINGLE, "artist": "AFI"}, "AFI"
        ) is True

    def test_artist_field_absent_credit_in_title_matches(self):
        assert _fallback_helper()(
            {"title": f"AFI – {DISCOGS_SINGLE}", "format": ["CD", "Single"]}, "AFI"
        ) is True

    def test_ascii_hyphen_credit_matches(self):
        assert _fallback_helper()(
            {"title": f"AFI - {DISCOGS_SINGLE}"}, "AFI"
        ) is True

    def test_different_artist_in_title_is_rejected(self):
        assert _fallback_helper()(
            {"title": f"Someone Else – {DISCOGS_SINGLE}"}, "AFI"
        ) is False

    def test_no_credit_anywhere_is_rejected(self):
        assert _fallback_helper()({"title": DISCOGS_SINGLE}, "AFI") is False


class TestFallbackSurvivesArtistFilter:
    """End-to-end: the global-search fallback is no longer dead."""

    def test_title_embedded_credit_reaches_the_scan(self):
        from services.enrichment.discogs_service import DiscogsService

        class _SearchHttp(_StubHttp):
            def __init__(self):
                super().__init__()
                self.search_calls = 0

            def search_database(self, params, timeout=10.0):
                self.search_calls += 1
                # Discogs search result: `artist` OMITTED, credit in the title,
                # format present.
                return [{
                    "id": "1001",
                    "title": f"AFI – {DISCOGS_SINGLE}",
                    "format": ["CD", "Single"],
                    "year": 2006,
                }]

            def get_artist_releases_all(self, artist_id, max_pages=10):
                return []  # artist list is empty → forces the fallback

            def get_artist_id(self, artist, timeout=10.0):
                return "253322"

        svc = DiscogsService(token="t", http_client=_SearchHttp())
        status = svc.get_single_status(REPORTED_TRACK, "AFI")
        assert status["is_single"] is True, "the fallback must accept a title-embedded credit"
        assert status["similarity"] >= MIN_DISCOGS_SIMILARITY
