"""A GLOBAL 5★ lock is NOT revoked by the album's internal shape.

Reported
--------
> Every single track that used to be 5★ (including tracks explicitly marked
> GLOBAL 5★ LOCKED) was demoted to 4★ in finalise_stage.

The measured cases (Abney Park, Advents, Aether, A Dream of Poe) all shared one
shape: the track held the pre-pass GLOBAL 5★ lock, cleared both 5★ z-bounds,
and was then force-floored to 4★ with ``_force_floor = 4``.

Root cause
----------
``_assign_stars`` runs the album RATIO standout gate
(``_album_ratio_standout_ok``) in TWO places. The one in the *lock* branch can
only ever veto a locked track, because the branch itself requires
``_global_5star_locked``. The veto is a double-count:

* The lock's own pre-pass (``_compute_global_5star_locked_titles``) ranks the
  artist's WHOLE scanned catalogue by ``_raw_combined`` and locks the top
  ``global_5star_catalog_top_pct`` (20%) that also clear
  ``global_5star_min_raw_score`` (default 60). That raw-score floor IS the
  "these counts are real, not noise" test the ratio gate exists to supply.
* The ratio gate then re-judges the lock using only ONE album's linear Last.fm
  spread — narrower evidence against a wider verdict.

It is also unsatisfiable on some albums by construction: measured on Abney
Park, "The Wake" (6.8k LF, raw 74.4, the artist's catalogue top) failed at
``runner_up=1.11x/1.50x`` because the album's #2 ("The Only One", 6.1k LF) is
ALSO one of the artist's best tracks. Two hits on one album can never be 1.5x
apart, so no track on that album could reach 5★.

Finally the veto ERASED the flag (``track["_global_5star_locked"] = False``),
which disarmed every downstream consumer of it — the era slot cap's protected
list and ``_short_track_cap_is_protected``.

The fix
-------
A track that holds the lock satisfies the shape test. The z-bounds still gate
it (so this is not an unconditional award), and the ratio gate remains in force
on the ordinary, UNLOCKED 5★ path where it is the only shape guard.

This file also pins WHICH CATALOGUE rates a compilation: a VA compilation
always uses the credited artist's online chart, while a single-artist
compilation uses the local artist catalogue (CAT-Z) so a best-of can produce a
normal spread. See ``test_greatest_hits_artist_catalogue`` for the routing's
own report.
"""

from __future__ import annotations

import pytest

from services.popularity.stages import finalise_stage as fs

# ---------------------------------------------------------------------------
# Fixtures — the measured Abney Park shape
# ---------------------------------------------------------------------------

#: A 12-track album score spread wide enough to clear every z-bound.
ALBUM_SCORES = [84.8, 75.8, 72.0, 70.0, 68.0, 66.0, 64.0, 62.0, 60.0, 58.0, 55.0, 50.0]
ARTIST_SCORES = ALBUM_SCORES + [
    80.0, 78.0, 76.0, 74.0, 72.0, 70.0, 68.0, 66.0,
    64.0, 62.0, 60.0, 58.0, 56.0, 54.0, 52.0, 50.0,
]
#: TWO hits 1.11x apart at the top, then a long tail — "The Wake" / "The Only
#: One". The runner-up multiplier can never be satisfied on this album.
ALBUM_LF = [6800, 6100, 4200, 3900, 3500, 3200, 3000, 2800, 2600, 2400, 2200, 2000]
ALBUM_LB = [13100, 12200, 9000, 8500, 8000, 7600, 7200, 6800, 6400, 6000, 5600, 5200]
#: era="minor" + an old release is the ONLY context in which the ratio gate
#: still runs, so every test here has to declare it.
MINOR_OLD = {"era": "minor", "has_benchmark": True}
PEAK = {"era": "peak", "has_benchmark": True}


def _track(**over):
    track = {
        "title": "The Wake",
        "artist": "Abney Park",
        "album": "Cemetary Number 1",
        "popularity_score": 84.8,
        "final_score": 84.8,
        "lastfm_listeners": 6800,
        "listenbrainz_listens": 13100,
        "single_confidence": "low",
        "year": 2005,
        "popularity_marked": True,
        "_global_5star_locked": True,
    }
    track.update(over)
    return track


def _assign(track, *, album_model=MINOR_OLD, **kwargs):
    kwargs.setdefault("is_compilation", False)
    return fs._assign_stars(
        track,
        ALBUM_SCORES,
        ARTIST_SCORES,
        ALBUM_LF,
        ALBUM_LB,
        album_model=album_model,
        **kwargs,
    )


# ===========================================================================
# 1. The lock survives the album's shape
# ===========================================================================


class TestALockedTrackIsNotVetoedByTheAlbumShape:
    def test_a_locked_track_reaches_five_stars(self):
        """THE REPORTED CASE. ``album_z=3.22``, ``artist_z=2.12``, 6.8k LF and
        the artist's catalogue top — previously floored to 4★."""
        track = _track()

        stars = _assign(track)

        assert stars == 5
        assert track["_global_5star_locked"] is True, (
            "the lock must survive, or downstream protections that key off it "
            "(era slot caps, the short-track cap) stop seeing the track"
        )
        assert not track.get("_force_floor"), "a 5★ must not carry a 4★ floor"

    def test_the_bypass_is_recorded(self):
        """The diagnostic must name the decision, so a future log reader can
        tell 'the shape test was skipped' from 'the shape test passed'."""
        track = _track()

        _assign(track)

        assert track.get("_ratio_5star_reason") == "lock_bypasses_album_shape_gate"

    def test_a_locked_track_buried_on_its_album_still_reaches_five(self):
        """The ``ratio_not_album_top`` shape: the lock is ranked over the
        ARTIST catalogue, so a locked track need not be its own album's #1 by
        Last.fm listeners. 280 LF is below the album's 400 LF floor."""
        track = _track(
            lastfm_listeners=280, listenbrainz_listens=26,
            popularity_score=82.7, final_score=82.7,
        )
        # An album whose floor is above this track: the old gate returned
        # ``ratio_not_album_top(280<400)``.
        album_lf = [400, 380, 360, 340, 320, 300, 280, 260, 240, 220, 200, 180]

        stars = fs._assign_stars(
            track, ALBUM_SCORES, ARTIST_SCORES, album_lf,
            [40, 38, 36, 34, 32, 30, 26, 24, 22, 20, 18, 16],
            album_model=MINOR_OLD,
        )

        assert stars == 5

    def test_the_lock_survives_on_a_peak_era_album_too(self):
        """CONTROL — the bypass does not depend on the era that used to gate
        the ratio test. A peak-era album bypassed the gate by context before;
        it must still reach 5★."""
        track = _track()

        assert _assign(track, album_model=PEAK) == 5
        assert track["_global_5star_locked"] is True


# ===========================================================================
# 2. Controls — the guard is NOT weakened anywhere else
# ===========================================================================


class TestTheRatioGateIsStillInForce:
    def test_an_unlocked_track_is_still_vetoed(self):
        """CONTROL — the whole point. An identical track WITHOUT the lock is
        still blocked by the album's flat top: this is the Anti-Flag guard
        (14/13/13 vs 11 median), which must keep working."""
        track = _track(_global_5star_locked=False)

        stars = _assign(track)

        assert stars == 4, "the ratio gate still decides for unlocked tracks"
        assert "ratio_flat" in str(track.get("_ratio_5star_reason") or "")

    def test_a_locked_track_that_fails_the_z_bounds_is_still_four(self):
        """CONTROL — the lock is not an unconditional award. The z-bounds are
        computed from the same distributions, and a track that fails them is
        still floored at 4★."""
        track = _track(popularity_score=50.0, final_score=50.0)

        stars = _assign(track)

        assert stars == 4
        assert track["_global_5star_locked"] is False

    def test_the_flat_top_is_still_unreachable_for_the_album_number_two(self):
        """CONTROL — the album's #2 is ALSO one of the artist's best tracks but
        carries no lock in this fixture, so it stays at 4★. The fix exempts the
        lock, not the album."""
        track = _track(
            title="The Only One", popularity_score=75.8, final_score=75.8,
            lastfm_listeners=6100, listenbrainz_listens=12200,
            _global_5star_locked=False,
        )

        assert _assign(track) == 4

    def test_a_compilation_does_not_use_the_lock_branch(self):
        """CONTROL — compilations are routed to the catalogue rater, so the
        lock shortcut is never reached for them (unchanged behaviour)."""
        track = _track(
            title="Whiskey in the Jar", artist="Metallica",
            album="Garage Inc.", popularity_score=82.0, final_score=82.0,
            lastfm_listeners=928_200, listenbrainz_listens=500_000,
            single_confidence="high", year=1998,
        )

        _assign(track, is_compilation=True, is_va_compilation=True)

        assert track.get("_ratio_5star_reason") != "lock_bypasses_album_shape_gate"

    def test_popularity_only_still_skips_the_lock_branch(self):
        """CONTROL — ``popularity_only`` has always excluded the lock shortcut
        and must keep doing so."""
        track = _track()

        stars = _assign(track, popularity_only=True)

        assert stars != 5

    def test_a_live_album_still_uses_its_own_rating(self):
        """CONTROL — the live-album path returns before the lock branch and is
        untouched by this change."""
        track = _track(_global_5star_locked=False)

        stars = _assign(track, is_live_album=True)

        assert track.get("_live_reason") is not None
        assert track.get("_ratio_5star_reason") != "lock_bypasses_album_shape_gate"
        assert 1 <= stars <= 5


# ===========================================================================
# 3. Which catalogue rates a compilation
# ===========================================================================


#: The online rater's rank bands, as the config ships them.
RULES = {
    "enabled": 1,
    "min_local_catalogue": 5,
    "rank_percentile_5star": 0.02,
    "rank_percentile_4star": 0.10,
    "rank_percentile_3star": 0.35,
    "rank_percentile_2star": 0.65,
    "min_catalogue_size": 5,
}

#: A catalogue whose top is a genuine outlier (CAT-Z ≈ +3).
HEALTHY = [40.0, 44.0, 48.0, 50.0, 52.0, 54.0, 56.0, 58.0, 60.0, 62.0, 64.0, 66.0]
THIN = [60.0, 64.0]


def _comp_track(**over):
    track = {
        "title": "Whiskey in the Jar",
        "artist": "Metallica",
        "album": "Garage Inc.",
        "popularity_score": 82.0,
        "final_score": 82.0,
        "lastfm_listeners": 928_200,
        "listenbrainz_listens": 500_000,
        "single_confidence": "high",
        "year": 1998,
    }
    track.update(over)
    return track


@pytest.fixture
def online(monkeypatch):
    """Record every online-catalogue lookup and answer with a top-7.7% rank."""
    import services.popularity.popularity_sources as ps

    calls: list[str] = []

    def _rank(artist, title, lastfm_client=None):
        calls.append(title)
        return {
            "rank": 12, "total": 150, "percentile": 0.0767,
            "listeners": 928_200, "top_listeners": 12_000_000,
            "source": "lastfm_artist_top_tracks",
        }

    monkeypatch.setattr(
        "helpers.config_helpers.get_compilation_online_catalogue_config",
        lambda: RULES,
    )
    monkeypatch.setattr(ps, "get_online_artist_track_rank", _rank)
    return calls


class TestWhichCatalogueRatesACompilation:
    def test_a_single_artist_compilation_rates_on_cat_z(self, online):
        """⭐ THE ROUTING FIX. "Garage Inc." is one artist's own record, so the
        LOCAL catalogue answers and its CAT-Z bands decide. The reported
        symptom: forcing it online capped the album at 4★ because the online
        5★ band is the artist's GLOBAL top 2% (rank 12/150 = top 7.7%)."""
        track = _comp_track()

        stars = fs._assign_stars(
            track, HEALTHY, HEALTHY,
            is_compilation=True, is_va_compilation=False,
        )

        assert track["_compilation_rating_mode"] == "track_artist_catalogue"
        assert stars == 5, "CAT-Z is the artist's top band → 5★"
        assert online == [], "the local catalogue must answer without going online"

    def test_a_va_compilation_always_goes_online(self, online):
        """CONTROL — the 2026-09-21 decision is untouched. A VA compilation's
        credited artists each own one or two songs in the library, so the local
        catalogue can never answer "is this that artist's hit?"."""
        track = _comp_track()

        fs._assign_stars(
            track, HEALTHY, HEALTHY,
            is_compilation=True, is_va_compilation=True,
        )

        assert track["_compilation_rating_mode"] == "track_artist_online_catalogue"
        assert online == ["Whiskey in the Jar"], (
            "a VA compilation must consult the credited artist's online chart "
            "even when the local catalogue looks healthy"
        )

    def test_a_thin_local_catalogue_falls_back_online(self, online):
        """CONTROL — ``min_local_catalogue`` is the thinness guard. Below it
        there is no distribution to compute a robust z against, so the online
        chart answers — which keeps a barely-owned artist working."""
        track = _comp_track(title="Deep Cut")

        fs._assign_stars(
            track, THIN, THIN,
            is_compilation=True, is_va_compilation=False,
        )

        assert track["_compilation_rating_mode"] == "track_artist_online_catalogue"
        assert online == ["Deep Cut"]

    def test_the_local_catalogue_boundary_is_the_config_value(self, online, monkeypatch):
        """CONTROL — exactly ``min_local_catalogue`` scores are enough to rate
        locally; one fewer goes online. The boundary is config-driven, so a
        library with a small artist catalogue can move it."""
        assert len(HEALTHY) >= RULES["min_local_catalogue"]

        for size, expected in (
            (RULES["min_local_catalogue"], "track_artist_catalogue"),
            (RULES["min_local_catalogue"] - 1, "track_artist_online_catalogue"),
        ):
            track = _comp_track(title=f"Size {size}")
            fs._assign_stars(
                track, HEALTHY[:size], HEALTHY[:size],
                is_compilation=True, is_va_compilation=False,
            )
            assert track["_compilation_rating_mode"] == expected, (
                f"a catalogue of {size} scored the wrong route"
            )

    def test_a_studio_album_never_records_a_compilation_mode(self, online):
        """CONTROL — the routing is confined to the compilation branch."""
        track = _comp_track()

        fs._assign_stars(track, HEALTHY, HEALTHY, is_compilation=False)

        assert "_compilation_rating_mode" not in track
        assert online == []


# ===========================================================================
# 4. The scan runner threads the VA verdict through
# ===========================================================================


class TestTheVaVerdictReachesTheRater:
    def test_post_album_star_ratings_passes_is_va_compilation(self, monkeypatch):
        """The routing is only worth anything if the caller passes the flag.
        ``post_album_star_ratings`` receives it from the scan runner — the only
        layer that still holds the album context — and must forward it."""
        captured: list[dict] = []

        def _recorder(*args, **kwargs):
            captured.append(kwargs)
            return 3

        monkeypatch.setattr(fs, "_assign_stars", _recorder)
        monkeypatch.setattr(fs, "_build_album_model", lambda *a, **k: {})

        fs.post_album_star_ratings(
            album_results=[
                {"title": "T1", "album": "Garage Inc.", "artist": "Metalloid",
                 "popularity_score": 80.0, "final_score": 80.0, "track_id": "1"},
                {"title": "T2", "album": "Garage Inc.", "artist": "Metalloid",
                 "popularity_score": 70.0, "final_score": 70.0, "track_id": "2"},
            ],
            artist="Metalloid",
            artist_scores=[80.0, 70.0, 60.0, 50.0, 40.0],
            # ``sync_navidrome=False`` keeps this hermetic: the real call would
            # attempt a setRating against the configured Navidrome host.
            options={"sync_navidrome": False},
            is_compilation=True,
            is_va_compilation=True,
        )

        assert captured, "no star assignment ran"
        assert captured[0].get("is_va_compilation") is True
        assert "generic_compilation_artist" not in captured[0], (
            "the dead parameter must be gone, not merely ignored"
        )

    def test_the_va_flag_defaults_to_false_for_a_plain_album(self, monkeypatch):
        """CONTROL — a studio album is not a compilation at all, so the flag is
        irrelevant there and must be False."""
        captured: list[dict] = []
        monkeypatch.setattr(
            fs, "_assign_stars", lambda *a, **k: (captured.append(k), 3)[1],
        )
        monkeypatch.setattr(fs, "_build_album_model", lambda *a, **k: {})

        fs.post_album_star_ratings(
            album_results=[
                {"title": "T1", "album": "Memento Mori", "artist": "Feuerschwanz",
                 "popularity_score": 80.0, "final_score": 80.0, "track_id": "1"},
            ],
            artist="Feuerschwanz",
            artist_scores=[80.0, 70.0, 60.0, 50.0, 40.0],
            options={"sync_navidrome": False},
            is_compilation=False,
            is_va_compilation=False,
        )

        assert captured and captured[0].get("is_va_compilation") is False
