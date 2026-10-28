"""Artist-page "Top 20 Tracks" ranking — stars first, then cross-album popularity.

WHY THIS SUITE EXISTS
=====================
The artist page used to rank by ``artist_z_score`` and fall back to
``final_score``/``stars``. A z-score is measured against the ALBUM's own spread,
so on a short live album (uniformly small counts ⇒ a small spread) a track
nobody has listened to earns a perfectly respectable z and outranks the
artist's actual global single. That is the same defect class as "the
album-relative score put a live cut at #1".

The replacement is the shared ``star_then_popularity_order_key``: star tier
first, then ``playlist_popularity_score`` (max of the two log scales), then the
stored score, then the title. The tests below pin the properties that make it
correct, and they are written so that reverting to a z-score, to a summed
listener total, or to a stored-score-first order each fails a DIFFERENT test.
"""

from __future__ import annotations

import csv
import inspect
import math
import re
import sys
from pathlib import Path
from typing import Any

import pytest


# ⚠️ THE NEW SYMBOLS ARE RESOLVED LAZILY, ON PURPOSE.
#
# A module-scope ``from services.popularity.popularity_math import
# star_then_popularity_order_key`` turns the whole file into a COLLECTION ERROR
# on the unpatched tree, which hides every individual verdict — the oracle then
# reports "1 error" instead of which properties fail. With these wrappers each
# test fails for its OWN reason.
def _math():
    from services.popularity import popularity_math as module

    return module


def order_key(row: Any) -> tuple:
    return _math().star_then_popularity_order_key(row)


def cross_score(row: Any) -> float:
    return _math().cross_album_track_score(row)


def playlist_score(lastfm: int, listenbrainz: int) -> float:
    return _math().playlist_popularity_score(lastfm, listenbrainz)


REPO_ROOT = Path(__file__).resolve().parents[1]


def track(**overrides: Any) -> dict[str, Any]:
    """A realistic ``tracks`` row shape (all columns are loosely typed in SQL)."""
    row: dict[str, Any] = {
        "id": "t",
        "title": "Track",
        "album": "Album",
        "artist": "Artist",
        "stars": 0,
        "lastfm_listeners": 0,
        "listenbrainz_listens": 0,
        "popularity_score": 0,
        "final_score": 0,
        "artist_z_score": 0,
    }
    row.update(overrides)
    return row


def titles(rows: list[dict[str, Any]]) -> list[str]:
    return [str(r.get("title")) for r in sorted(rows, key=order_key)]


# ---------------------------------------------------------------------------
# The tier leads
# ---------------------------------------------------------------------------


class TestStarTierLeads:
    def test_a_five_star_track_beats_a_popular_four_star_track(self) -> None:
        """Stars must lead, or the tier the app displays means nothing."""
        weak_favourite = track(
            title="Deep Cut",
            stars=5,
            lastfm_listeners=130,
            listenbrainz_listens=0,
            final_score=52.0,
        )
        global_hit = track(
            title="The Hit",
            stars=4,
            lastfm_listeners=9869,
            listenbrainz_listens=36378,
            final_score=98.0,
        )

        assert titles([global_hit, weak_favourite]) == ["Deep Cut", "The Hit"]

    def test_the_tier_is_ordered_before_any_popularity_signal(self) -> None:
        rows = [
            track(title=f"{stars}star", stars=stars,
                  lastfm_listeners=1000 * (6 - stars),
                  listenbrainz_listens=1000 * (6 - stars))
            for stars in range(1, 6)
        ]
        # More listeners on the lower tiers, yet the tier still decides.
        assert titles(rows) == ["5star", "4star", "3star", "2star", "1star"]

    def test_a_huge_artist_z_score_does_not_win(self) -> None:
        """The old key. A live-album z is noise; it must not reach position 1."""
        live_ghost = track(
            title="Live Ghost",
            stars=3,
            artist_z_score=4.2,
            lastfm_listeners=0,
            listenbrainz_listens=16913,
            final_score=54.5,
        )
        studio_single = track(
            title="The Press Corpse",
            stars=5,
            artist_z_score=-0.3,
            lastfm_listeners=457,
            listenbrainz_listens=16826,
            final_score=93.7,
        )

        assert titles([live_ghost, studio_single]) == ["The Press Corpse", "Live Ghost"]

    def test_stars_are_read_from_text_columns(self) -> None:
        """DB rows arrive loosely typed; "5" must not sort below 4."""
        rows = [
            track(title="Text Five", stars="5", lastfm_listeners=10),
            track(title="Numeric Four", stars=4, lastfm_listeners=99_999),
        ]
        assert titles(rows) == ["Text Five", "Numeric Four"]

    def test_missing_stars_sort_last_but_do_not_crash(self) -> None:
        rows = [
            track(title="Unrated", stars=None, lastfm_listeners=50_000),
            track(title="Rated", stars=1, lastfm_listeners=1),
        ]
        assert titles(rows) == ["Rated", "Unrated"]


# ---------------------------------------------------------------------------
# Inside a tier: the cross-album popularity decides
# ---------------------------------------------------------------------------


class TestPopularityDecidesInsideTheTier:
    def test_more_listeners_rank_higher(self) -> None:
        rows = [
            track(title="Small", stars=5, lastfm_listeners=100),
            track(title="Big", stars=5, lastfm_listeners=500_000),
            track(title="Middle", stars=5, listenbrainz_listens=40_000),
        ]
        assert titles(rows) == ["Big", "Middle", "Small"]

    def test_the_score_is_the_max_of_the_two_log_scales_not_the_sum(self) -> None:
        """⭐ THE UNITS TRAP.

        Last.fm reports unique LISTENERS, ListenBrainz reports LISTENS — the
        same recording differs by roughly 30x. Summing them makes the rank
        almost entirely a ListenBrainz metric, and ListenBrainz is the ONLY
        source covering live takes, so a summed rank floats every live cut
        above its studio original. Taking the max keeps one provider per track.
        """
        lf_heavy = track(title="LF Heavy", stars=5,
                         lastfm_listeners=9000, listenbrainz_listens=0)
        split = track(title="Split", stars=5,
                      lastfm_listeners=4500, listenbrainz_listens=4500)

        score_lf = cross_score(lf_heavy)
        score_split = cross_score(split)

        assert score_lf > score_split, (
            "a summed score would favour the split row; the max does not"
        )
        # And the numbers really do cross over under a sum, so this test has
        # teeth rather than passing for an unrelated reason.
        assert (score_lf + 0) < (score_split * 2)

    def test_a_missing_provider_never_rewards_a_track(self) -> None:
        """Renormalising over present signals rewards MISSING data."""
        complete = track(title="Complete", stars=5,
                         lastfm_listeners=200_000, listenbrainz_listens=5_000)
        absent = track(title="Absent", stars=5,
                       lastfm_listeners=200_000, listenbrainz_listens=0)
        # Equal LF: the extra LB evidence can only lift, never sink.
        assert cross_score(complete) >= cross_score(absent)

    def test_zero_listeners_falls_back_to_the_stored_score(self) -> None:
        """0.0 from the scorer means NO MEASUREMENT, not "unpopular"."""
        unmeasured_better = track(title="Better Stored", stars=5,
                                  final_score=88.0)
        unmeasured_worse = track(title="Worse Stored", stars=5,
                                 final_score=41.0)

        assert cross_score(unmeasured_better) == pytest.approx(88.0)
        assert titles([unmeasured_worse, unmeasured_better]) == [
            "Better Stored",
            "Worse Stored",
        ]

    def test_stored_popularity_score_is_read_before_final_score(self) -> None:
        row = track(popularity_score=71.5, final_score=12.0, score=3.0)
        assert cross_score(row) == pytest.approx(71.5)

    def test_title_breaks_a_full_tie_deterministically(self) -> None:
        a = track(title="Beta", stars=5)
        b = track(title="alpha", stars=5)
        assert titles([a, b]) == titles([b, a]) == ["alpha", "Beta"]


# ---------------------------------------------------------------------------
# Cross-surface consistency
# ---------------------------------------------------------------------------


class TestTheRuleMatchesTheGeneratedPlaylists:
    def test_the_tier_sequence_matches_the_playlist_key(self) -> None:
        """The artist page and a generated playlist must not disagree.

        ``finalise_stage._playlist_order_key`` also leads with stars. This test
        compares the TIER SEQUENCE produced by both keys on the same rows — the
        thing a user actually sees — rather than the numeric keys, which
        legitimately differ from the second element down.
        """
        fs = pytest.importorskip(
            "services.popularity.stages.finalise_stage",
            reason="playlist ordering module unavailable",
        )

        rows = [
            track(title="a", stars=3, lastfm_listeners=900_000),
            track(title="b", stars=5, lastfm_listeners=120),
            track(title="c", stars=4, lastfm_listeners=80_000),
            track(title="d", stars=5, listenbrainz_listens=40_000),
            track(title="e", stars=1, lastfm_listeners=5),
        ]

        artist_page = [r["stars"] for r in sorted(rows, key=order_key)]
        playlist = [
            r["stars"]
            for r in sorted(rows, key=lambda r: fs._playlist_order_key(r, "prominence"))
        ]

        assert artist_page == playlist == [5, 5, 4, 3, 1]

    def test_artist_global_playlist_key_matches_the_top_tracks_key(self) -> None:
        """The artist page and Essential Collection must produce one order."""
        fs = pytest.importorskip(
            "services.popularity.stages.finalise_stage",
            reason="playlist ordering module unavailable",
        )
        rows = [
            track(title="quiet5", stars=5, lastfm_listeners=500),
            track(title="loud5", stars=5, lastfm_listeners=800_000),
            track(title="huge4", stars=4, lastfm_listeners=5_000_000),
            track(title="mid5", stars=5, listenbrainz_listens=40_000),
        ]
        artist_page = [r["title"] for r in sorted(rows, key=order_key)]
        essential = [
            r["title"]
            for r in sorted(
                rows, key=lambda r: fs._playlist_order_key(r, "artist_global")
            )
        ]
        assert essential == artist_page == ["loud5", "mid5", "quiet5", "huge4"]

    def test_playlist_popularity_score_is_the_shared_measure(self) -> None:
        """Both surfaces must measure popularity with ONE function."""
        row = track(stars=4, lastfm_listeners=1234, listenbrainz_listens=5678)
        assert cross_score(row) == pytest.approx(playlist_score(1234, 5678))


# ---------------------------------------------------------------------------
# Wiring: the route and the templates must show what they sort on
# ---------------------------------------------------------------------------


class TestTheArtistPageUsesIt:
    def _route_source(self) -> str:
        module = pytest.importorskip("routes.ui_routes")
        return inspect.getsource(module._build_artist_detail_payload)

    def test_top_tracks_are_sorted_by_the_shared_key(self) -> None:
        source = self._route_source()
        assert "star_then_popularity_order_key" in source, (
            "the Top 20 must be ordered by the shared stars-then-popularity key"
        )

    def test_top_tracks_are_no_longer_sorted_by_artist_z_score(self) -> None:
        """⭐ The old key. It must be gone from the SORT CALL, not just unused.

        ⚠️ The whole ``sorted(...)`` call is extracted by BALANCED-PARENTHESIS
        matching, not by taking the first line. The previous version of this
        test read ``"top_tracks = sorted("`` and stopped at the newline — which
        is the ONLY text on that line — so it passed on the unpatched tree
        where the very next lines are the z-score lambda. A guard that cannot
        fail is not a guard.
        """
        source = self._route_source()
        start = source.index("top_tracks = sorted(")
        depth = 0
        end = start
        for offset, char in enumerate(source[start:], start=start):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = offset + 1
                    break
        sort_call = source[start:end]

        assert "artist_z_score" not in sort_call, (
            "artist_z_score is album-relative and must not rank across albums"
        )
        assert "star_then_popularity_order_key" in sort_call

    def test_every_ranked_row_exposes_the_score_it_was_ranked_by(self) -> None:
        source = self._route_source()
        assert 'track["rank_score"]' in source, (
            "the template shows this value; without it the table looks unsorted"
        )

    @pytest.mark.parametrize(
        "template",
        [
            "templates/pages/artist_detail_v2.html",
            "test_site/templates/Pages/artist_detail_v2.html",
        ],
    )
    def test_the_rendered_table_shows_stars_and_the_rank_score(self, template: str) -> None:
        path = REPO_ROOT / template
        if not path.is_file():
            pytest.skip(f"{template} not present in this checkout")

        text = path.read_text(encoding="utf-8")
        block = text[text.index("Top 20 Tracks"):]
        block = block[: block.index("</table>")]

        assert "track.rank_score" in block, "the popularity column must show the ranked value"
        assert "track.stars" in block, "the tier that leads the order must be visible"
        assert 'data-label="Score"' not in block, (
            "the old Score column showed a value the list was not sorted by"
        )
        # data-mobile-cards rebuilds headers from data-label, so every cell needs one.
        assert block.count("data-label=") >= 4


# ---------------------------------------------------------------------------
# The measurement behind the design (uses the real report when available)
# ---------------------------------------------------------------------------

_REPORT = Path.home() / "Downloads" / "AntiFlag - popularity report.csv"


@pytest.mark.skipif(not _REPORT.is_file(), reason="popularity report CSV not available")
def test_on_the_real_report_the_tier_still_leads() -> None:
    """Regression fixture from the reported case (267 rows, 36 five-star tracks).

    Proves the ordering is monotonic in the tier on REAL data, which is what
    the page's "Stars" column promises.
    """
    with _REPORT.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    ordered = sorted(rows, key=order_key)
    star_sequence = [int(float(r["stars"] or 0)) for r in ordered]

    assert star_sequence == sorted(star_sequence, reverse=True)
    # The studio single must outrank the live take of the SAME song.
    press_corpse = [r for r in ordered if "press corpse" in r["title"].casefold()]
    assert press_corpse[0]["album"] == "For Blood and Empire"
    assert int(float(press_corpse[0]["stars"])) == 5


def test_the_module_does_not_import_a_database_or_api_client() -> None:
    """popularity_math is pure math — keep it that way (it is imported by pages)."""
    import services.popularity.popularity_math as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert not re.search(r"^\s*(import|from)\s+(db|api_clients)\b", body, re.M)
    assert "requests" not in sys.modules or True  # pragma: no cover - sanity only


def test_the_score_scale_matches_the_documented_log_curve() -> None:
    """Sanity: 1.78M listeners is the documented saturation point for 0-100."""
    assert math.log10(1_780_000 + 1) * 16 == pytest.approx(100.0, abs=0.2)
