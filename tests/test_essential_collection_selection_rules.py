"""Essential Collection selection: the six-step spec, one test per step.

The owner's spec for an artist X:

1. Candidate pool — X as PRIMARY credit (the credit STARTS with X, so
   "X feat. Y" counts) or X as a FEATURED credit on another act's track.
2. Filter — drop live/remix/acoustic/demo unless a high-confidence single;
   merge duplicates by normalised title + primary artist.
3. Include if 4★+ AND (high-confidence single OR reach >= X's 80th percentile).
   The percentile is computed from PRIMARY tracks only.
4. Reach — Last.fm listeners, falling back to ListenBrainz listens.
5. Order — reach descending, ONE list (no separate features section).
6. File choice — prefer the original album version over a compilation copy.
"""

from __future__ import annotations

import pytest

from services.popularity.stages import finalise_stage as fs


def _row(title, *, artist="Artist", album_artist=None, lastfm=0, lb=0,
         stars=4, single="low", is_live=0, is_compilation=0, **kw):
    row = {
        "id": f"id-{title}-{lastfm}-{lb}",
        "title": title,
        "artist": artist,
        "album_artist": album_artist if album_artist is not None else artist,
        "stars": stars,
        "lastfm_listeners": lastfm,
        "listenbrainz_listens": lb,
        "single_confidence": single,
        "is_live": is_live,
        "is_compilation": is_compilation,
        "popularity_score": 50.0,
        "final_score": 50.0,
        "year": "2000",
        "release_year": "2000",
    }
    row.update(kw)
    return row


class TestStep4Reach:
    def test_lastfm_is_the_primary_number(self):
        assert fs._essential_reach(_row("A", lastfm=5000, lb=999)) == 5000.0

    def test_listenbrainz_is_the_fallback(self):
        assert fs._essential_reach(_row("A", lastfm=0, lb=999)) == 999.0

    def test_neither_gives_zero(self):
        assert fs._essential_reach(_row("A", lastfm=0, lb=0)) == 0.0

    def test_it_is_not_the_album_relative_score(self):
        """``popularity_score`` is album-relative; reach must ignore it."""
        row = _row("A", lastfm=0, lb=0, popularity_score=99.0, final_score=99.0)
        assert fs._essential_reach(row) == 0.0


class TestStep1PrimaryCredit:
    def test_the_bare_artist(self):
        assert fs._essential_is_primary_credit(_row("A", artist="Papa Roach"),
                                               "papa roach") is True

    def test_the_artist_leading_a_feature(self):
        assert fs._essential_is_primary_credit(
            _row("A", artist="Papa Roach feat. Maria Brink"), "papa roach"
        ) is True

    def test_a_guest_slot_is_NOT_primary(self):
        assert fs._essential_is_primary_credit(
            _row("A", artist="Korn feat. Papa Roach"), "papa roach"
        ) is False

    def test_a_different_artist_is_not_primary(self):
        assert fs._essential_is_primary_credit(
            _row("A", artist="Papa Roach Tribute Band"), "papa roach"
        ) is False


class TestStep2VersionFilter:
    def test_a_studio_track_survives(self):
        assert fs._essential_version_ok(_row("Song")) is True

    def test_a_live_take_is_dropped(self):
        assert fs._essential_version_ok(_row("Song (Live)")) is False

    def test_a_remix_is_dropped(self):
        assert fs._essential_version_ok(_row("Song (Remix)")) is False

    def test_a_high_confidence_single_survives_whatever_its_title(self):
        """The spec's exemption: a live SINGLE is still the artist's single."""
        assert fs._essential_version_ok(
            _row("Song (Live)", single="high")
        ) is True

    def test_a_user_lock_survives(self):
        assert fs._essential_version_ok(_row("Song (Remix)", single="user")) is True


class TestStep3Percentile:
    def test_the_threshold_sits_at_the_80th_percentile(self):
        values = [float(v) for v in range(1, 11)]  # 1..10
        assert fs._essential_percentile(values, 0.80) == 8.0

    def test_no_data_is_None_not_zero(self):
        """⚠️ None means "do not gate". 0.0 would keep everything; a caller that
        conflated them would empty a sparse artist's collection."""
        assert fs._essential_percentile([], 0.80) is None
        assert fs._essential_percentile([0, 0, 0], 0.80) is None

    def test_a_single_value_is_its_own_threshold(self):
        assert fs._essential_percentile([42.0], 0.80) == 42.0

    def test_it_is_order_independent(self):
        assert fs._essential_percentile([5, 1, 9, 3], 0.80) == \
            fs._essential_percentile([9, 5, 3, 1], 0.80)


class TestStep3aSingleGate:
    def test_high_confidence(self):
        assert fs._essential_is_high_confidence_single(_row("A", single="high"))

    def test_user_is_treated_as_high(self):
        assert fs._essential_is_high_confidence_single(_row("A", single="user"))

    @pytest.mark.parametrize("value", ["low", "medium", "", None])
    def test_everything_else_is_not(self, value):
        assert not fs._essential_is_high_confidence_single(_row("A", single=value))


class TestTheSpecIsRecordedInTheSource:
    """The six steps are easy to lose in a refactor; pin their call sites."""

    @pytest.fixture(scope="class")
    def body(self):
        import inspect

        return inspect.getsource(fs._sync_essential_playlist)

    def test_step2_version_filter_is_applied(self, body):
        assert "_essential_version_ok(row)" in body

    def test_step3_percentile_gate_is_applied(self, body):
        assert "_essential_percentile(_primary_reach, 0.80)" in body
        assert "_essential_is_high_confidence_single(row)" in body

    def test_the_percentile_basis_is_PRIMARY_tracks_only(self, body):
        """Featured tracks are compared against the threshold, never shift it."""
        idx = body.index("_primary_reach = [")
        window = body[idx: idx + 220]
        assert "_essential_is_primary_credit" in window, (
            "features must not enter the percentile basis"
        )

    def test_step5_orders_by_reach(self, body):
        idx = body.index("winners = sorted(")
        window = body[idx: idx + 260]
        assert "_essential_reach(r)" in window

    def test_step6_prefers_a_non_compilation_copy(self, body):
        idx = body.index("group.sort(")
        window = body[idx: idx + 400]
        assert 'is_compilation' in window, (
            "a compilation re-release must lose to the album copy"
        )

    def test_the_old_star_tier_orderer_is_gone(self, body):
        assert "_ordered_playlist_rows(winners" not in body
