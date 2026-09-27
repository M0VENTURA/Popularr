"""The release modal must rank the release the user asked for first.

REPORTED
    "On the musicbrainz release modal, when selecting all release types, it
    doesn't always smart find the closest match at the top even when the artist
    and album are filled in. Often needed to change to the release type."

DEFECT
    ``api_musicbrainz_search`` ended with a BLIND re-sort whose key was
    ``(artist name DESC, release date DESC)``.  The album title was not in the
    key at all, and MusicBrainz's own relevance ``score`` was discarded — so a
    NEWER near-miss outranked the release the user typed.  That also explains
    the "change the release type and it works" workaround: filtering drops the
    newer near-misses, letting the wanted release float up.

    It additionally threw away the artist-only fallback's own fuzzy
    ``_rg_similarity`` ordering, computed ~40 lines earlier: the code sorted by
    relevance and then immediately sorted by date over the top of it.

These tests drive the REAL route over HTTP with the fake-client seam the other
MB search tests use.
"""
from __future__ import annotations

import pytest

import routes.musicbrainz_routes as mb_routes

ARTIST = "36 Crazyfists"
ALBUM = "Reviver"

# The wanted release is the OLDEST. Sorted by date DESC (the old behaviour) the
# live album wins; ranked by relevance the exact match wins.
EXACT_ID = "rg-exact"
GROUPS = [
    {
        "id": "rg-live", "title": "Reviver Live",
        "artist-credit": [{"name": ARTIST}],
        "first-release-date": "2015-06-01", "primary-type": "Album",
        "secondary-types": ["Live"], "score": 60,
    },
    {
        "id": "rg-remix", "title": "Reviver (Remixes)",
        "artist-credit": [{"name": ARTIST}],
        "first-release-date": "2011-03-01", "primary-type": "Album",
        "secondary-types": ["Remix"], "score": 70,
    },
    {
        "id": EXACT_ID, "title": "Reviver",
        "artist-credit": [{"name": ARTIST}],
        "first-release-date": "2010-05-01", "primary-type": "Single",
        "secondary-types": [], "score": 100,
    },
]


class _FakeClient:
    """Strict artist+album returns ``strict_groups``; the artist-only retry
    (which the route falls back to) returns every group."""

    def __init__(self, strict_groups):
        self.strict_groups = strict_groups
        self.queries: list[str] = []

    def get(self, endpoint: str, *, params=None, timeout: float = 10.0) -> dict:
        query = str((params or {}).get("query") or "")
        self.queries.append(query)
        if "releasegroup" in query:
            return {"release-groups": list(self.strict_groups)}
        return {"release-groups": list(GROUPS)}


def _patched(monkeypatch, strict_groups):
    fake = _FakeClient(strict_groups)
    monkeypatch.setattr(mb_routes, "_get_mb_client", lambda: fake)
    return fake


@pytest.fixture()
def strict_empty(monkeypatch):
    """The strict query finds nothing, so the upward fallback runs."""
    return _patched(monkeypatch, [])


@pytest.fixture()
def strict_full(monkeypatch):
    """The strict query returns every group (MB fuzzy-matched the title)."""
    return _patched(monkeypatch, GROUPS)


def _titles(data):
    return [str(r.get("title") or "") for r in data["releases"]]


# ---------------------------------------------------------------------------
# The reported defect
# ---------------------------------------------------------------------------

class TestClosestMatchRanksFirst:
    async def test_exact_album_match_ranks_first_on_the_fallback_path(
        self, client, strict_empty
    ):
        resp = await client.post(
            "/api/musicbrainz/search", json={"artist": ARTIST, "album": ALBUM},
        )
        data = await resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        titles = _titles(data)
        assert ALBUM in titles, f"the exact match must still be returned: {titles}"
        assert titles[0] == ALBUM, (
            "the album the user typed must rank first, not the newest near-miss; "
            f"got {titles}"
        )

    async def test_exact_album_match_ranks_first_when_the_strict_query_matches(
        self, client, strict_full
    ):
        resp = await client.post(
            "/api/musicbrainz/search", json={"artist": ARTIST, "album": ALBUM},
        )
        data = await resp.get_json()
        titles = _titles(data)
        assert titles and titles[0] == ALBUM, (
            f"expected {ALBUM!r} first, got {titles}"
        )

    async def test_a_newer_release_never_outranks_the_exact_match(
        self, client, strict_full
    ):
        """The core complaint, stated as an invariant."""
        resp = await client.post(
            "/api/musicbrainz/search", json={"artist": ARTIST, "album": ALBUM},
        )
        data = await resp.get_json()
        releases = data["releases"]
        exact = next(r for r in releases if r["id"] == EXACT_ID)
        exact_pos = releases.index(exact)
        newer = [
            r for r in releases
            if str(r.get("first_release_date") or "") > str(
                exact.get("first_release_date") or ""
            )
        ]
        for r in newer:
            assert releases.index(r) > exact_pos, (
                f"{r['title']!r} ({r['first_release_date']}) is newer but must "
                f"not outrank the exact match {ALBUM!r}"
            )

    async def test_musicbrainz_score_breaks_a_title_tie(self, client, monkeypatch):
        """Same title twice: MB's own relevance decides."""
        tied = [
            {
                "id": "rg-low", "title": "Reviver",
                "artist-credit": [{"name": ARTIST}],
                "first-release-date": "2020-01-01", "primary-type": "Album",
                "score": 40,
            },
            {
                "id": "rg-high", "title": "Reviver",
                "artist-credit": [{"name": ARTIST}],
                "first-release-date": "2001-01-01", "primary-type": "Album",
                "score": 95,
            },
        ]
        _patched(monkeypatch, tied)
        resp = await client.post(
            "/api/musicbrainz/search", json={"artist": ARTIST, "album": ALBUM},
        )
        data = await resp.get_json()
        ids = [r["id"] for r in data["releases"]]
        assert ids[0] == "rg-high", (
            "with an identical title the higher MusicBrainz score must win, "
            f"got {ids}"
        )


# ---------------------------------------------------------------------------
# The definition of "closest match", called directly
#
# These are what catch a TERNARY inversion: the end-to-end tests only pin the
# exact match at the top, so swapping the marker and containment tiers would
# reorder the near-misses without any of them failing.
# ---------------------------------------------------------------------------

class TestReleaseTitleRelevance:
    def _relevance(self, album, title):
        from routes.musicbrainz_routes import release_title_relevance

        return release_title_relevance(album, title)

    def test_exact_match_is_the_highest_tier(self):
        assert self._relevance("Reviver", "Reviver") == 1.0

    def test_case_and_spacing_do_not_matter(self):
        assert self._relevance("Reviver", "  reviver  ") == 1.0

    def test_a_version_marker_variant_scores_below_exact(self):
        score = self._relevance("Reviver", "Reviver Live")
        assert 0.0 < score < 1.0
        assert score == 0.9

    def test_a_containment_match_scores_below_a_version_variant(self):
        """The same name minus a marker beats a title that merely contains it.

        Pins the tier ORDER, so inverting the middle two tiers is caught.
        """
        variant = self._relevance("Reviver", "Reviver Live")
        contains = self._relevance("Reviver", "Reviver (Remixes)")
        assert contains < variant, (
            f"a containment match ({contains}) must rank below a "
            f"marker-insensitive match ({variant})"
        )

    def test_an_unrelated_title_scores_low(self):
        assert self._relevance("Reviver", "Renegades") < 0.85

    def test_a_missing_side_scores_zero(self):
        assert self._relevance("", "Reviver") == 0.0
        assert self._relevance("Reviver", "") == 0.0

    def test_fuzzy_scores_can_never_reach_the_containment_tier(self):
        # A single shared character must not be able to look like a match.
        assert self._relevance("Reviver", "R") < 0.85

    def test_containment_requires_whole_words(self):
        """"r" must not be a "containment match" for "reviver".

        A raw substring test made a one-character title as relevant as
        "Reviver (Remixes)", which is how a near-miss can float to the top.
        """
        # A word-aligned prefix IS containment: the whole word appears.
        assert self._relevance("Reviver", "Reviver Remixes Live") == 0.85
        assert self._relevance("Reviver", "Reviver XX") == 0.85
        # But a fragment inside a word is NOT — this is what the guard is for.
        assert self._relevance("Reviver", "Revivering") < 0.85


class TestSortKeyContract:
    """The ranking key must actually contain the relevance terms.

    Extracting the order into a callable is what makes this checkable at all —
    a source-text assertion cannot catch a neutered branch.
    """

    def _key(self, release, album=ALBUM, year=""):
        from routes.musicbrainz_routes import release_result_sort_key

        return release_result_sort_key(release, album=album, year=year)

    def test_title_relevance_is_the_primary_term(self):
        exact = self._key({"title": ALBUM, "first_release_date": "2001-01-01"})
        later = self._key({"title": "Reviver Live", "first_release_date": "2020-01-01"})
        assert exact > later, (
            "the album title must outrank the release date; otherwise a newer "
            "near-miss wins"
        )

    def test_the_musicbrainz_score_breaks_a_title_tie(self):
        low = self._key({"title": ALBUM, "score": 10, "first_release_date": "2020-01-01"})
        high = self._key({"title": ALBUM, "score": 90, "first_release_date": "2001-01-01"})
        assert high > low

    def test_a_non_numeric_score_is_tolerated(self):
        for broken in (None, "", "abc", {}, []):
            key = self._key({"title": ALBUM, "score": broken})
            assert key[1] == 0.0

    def test_a_matching_year_breaks_a_title_and_score_tie(self):
        matching = self._key({"title": ALBUM, "score": 50, "first_release_date": "2017-09-29"}, year="2017")
        other = self._key({"title": ALBUM, "score": 50, "first_release_date": "1999-01-01"}, year="2017")
        assert matching > other

    def test_the_newest_release_is_the_final_tiebreak(self):
        newer = self._key({"title": ALBUM, "score": 50, "first_release_date": "2020-01-01"})
        older = self._key({"title": ALBUM, "score": 50, "first_release_date": "2001-01-01"})
        assert newer > older


# ---------------------------------------------------------------------------
# Controls — the change must stay scoped
# ---------------------------------------------------------------------------

class TestOrderingControls:
    async def test_artist_only_search_is_still_newest_first(
        self, client, strict_empty
    ):
        """CONTROL: browsing one artist with no album keeps its date order.

        Without this, a blanket "sort by score" change would silently reorder
        artist browsing, which nobody asked for.
        """
        resp = await client.post(
            "/api/musicbrainz/search",
            json={"query": ARTIST, "artist_only": True},
        )
        data = await resp.get_json()
        dates = [str(r.get("first_release_date") or "") for r in data["releases"]]
        assert dates == sorted(dates, reverse=True), (
            f"artist-only browsing must stay newest-first, got {dates}"
        )

    async def test_release_type_filter_still_filters(self, client, strict_full):
        """CONTROL: the type filter keeps working (and is what the user was
        using as a workaround)."""
        resp = await client.post(
            "/api/musicbrainz/search",
            json={"artist": ARTIST, "album": ALBUM, "type": "single"},
        )
        data = await resp.get_json()
        titles = _titles(data)
        assert titles == [ALBUM], (
            f"type=single should leave only the exact single, got {titles}"
        )

    async def test_all_release_types_is_not_filtered(self, client, strict_full):
        """CONTROL: "All Release Types" must keep every category."""
        resp = await client.post(
            "/api/musicbrainz/search",
            json={"artist": ARTIST, "album": ALBUM, "type": "all"},
        )
        data = await resp.get_json()
        assert len(_titles(data)) == len(GROUPS), (
            "type=all must not filter anything, got "
            f"{_titles(data)}"
        )

    async def test_results_are_still_returned_without_an_album(
        self, client, strict_empty
    ):
        """CONTROL: a free-text search still works and is not reordered into
        nonsense."""
        resp = await client.post(
            "/api/musicbrainz/search", json={"query": ARTIST},
        )
        data = await resp.get_json()
        assert resp.status_code == 200
        assert len(data["releases"]) == len(GROUPS)
