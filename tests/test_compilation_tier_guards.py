"""VA compilation star rating: the two absolute guards on the top tier.

These guards are VA-ONLY: a single-artist compilation is rated on the artist's
own CAT-Z bands and skips them (see ``TestTheGuardsAreVaOnly``).

Reported on an 81-track playlist compilation ("Spotify - New Nu Metal")
------------------------------------------------------------------------
The per-track-artist catalogue rating is the intended model for a VA
compilation, but the artist-catalogue RANK PERCENTILE is **scale-free**: it
asks "how popular is this song for this artist", never "is this song popular".

Measured consequences, both directions:

* **Tiny catalogues were promoted.** "devil may cry" (Echochamber, 713 Last.fm
  listeners, rank #3/171) and "type shit" (Gritmonger, 1.2k listeners, #1/9)
  took 5★ — "#1 of 9 songs" is 5★ whatever the audience is.
* **Heavyweights were buried.** Deftones "infinite source" (401.2k listeners,
  #58/187) took 3★, Korn "Reward the Scars" (53.6k, #159/190) took 1★,
  Hollywood Undead "1x1" (15.5k, #126/176) took 1★ — a deep discography
  pushed a genuine compilation hit far down its own artist's chart.

Two guards, both config-driven (`single_detection.compilation_online_catalogue`):

1. **Top-tier gate** — a compilation's 5★ must ALSO clear an absolute bar
   (`top_tier_min_score` OR `top_tier_min_listeners`).
2. **Standout floor** — a track in the compilation's own top band by the
   cross-artist raw blend (`standout_floor_percentile`) floors at
   `standout_floor_stars` (4★; the artist catalogue keeps ownership of 5★).
"""

from __future__ import annotations

import pytest

#: The shipped online-catalogue defaults, including the two new guards.
RULES = {
    "enabled": 1,
    "min_local_catalogue": 5,
    "rank_percentile_5star": 0.02,
    "rank_percentile_4star": 0.10,
    "rank_percentile_3star": 0.35,
    "rank_percentile_2star": 0.65,
    "min_catalogue_size": 5,
    "top_tier_min_score": 55.0,
    "top_tier_min_listeners": 250_000,
    # ⭐ The 4★ evidence bars (2026-10-27). Present so these fixtures exercise
    # the SHIPPED config rather than an older subset — without them the guard
    # reads 0 (disabled) and the tests silently stop covering it.
    "high_tier_min_score": 45.0,
    "high_tier_min_listeners": 1_000,
    "standout_floor_percentile": 0.10,
    "standout_floor_stars": 4,
}

ALBUM_MODEL = {
    "has_benchmark": True,
    "era": "solid",
    "catalog_cutoff": 99.0,
    "max_5star_slots": 3,
    "album_top_n": 3,
}


def _track(title, artist, *, score, lf, lb=0, raw=None, **overrides):
    track = {
        "track_id": f"id-{title[:12]}",
        "title": title,
        "artist": artist,
        "album": "Spotify - New Nu Metal",
        "popularity_score": float(score),
        "final_score": float(score),
        "_raw_combined": float(raw if raw is not None else score),
        "lastfm_listeners": int(lf),
        "listenbrainz_listens": int(lb),
        "is_single": False,
        "single_confidence": "low",
        "is_live": False,
        "popularity_marked": False,
        "exclude_from_stats": False,
    }
    track.update(overrides)
    return track


def _assign(monkeypatch, track, *, rank, total, raw_scores=None, rules=None, **kwargs):
    """Rate one VA compilation track against a stubbed online catalogue."""
    import services.popularity.popularity_sources as ps
    from services.popularity.stages import finalise_stage as fs

    # The guards under test are VA-only (a single-artist compilation is rated
    # on the artist's own CAT-Z bands and skips them — see
    # ``TestTheGuardsAreVaOnly``).
    kwargs.setdefault("is_va_compilation", True)
    _rules = dict(RULES if rules is None else rules)
    monkeypatch.setattr(
        "helpers.config_helpers.get_compilation_online_catalogue_config",
        lambda: _rules,
    )
    monkeypatch.setattr(
        ps, "get_online_artist_track_rank",
        lambda artist, title, lastfm_client=None: {
            "rank": rank, "total": total, "percentile": rank / total,
            "listeners": int(track.get("lastfm_listeners") or 0),
            "top_listeners": 500_000,
            "source": "lastfm_artist_top_tracks",
        },
    )
    album = [float(track.get("popularity_score") or 0)]
    return fs._assign_stars(
        track,
        album,
        [float(track.get("popularity_score") or 0)] * 3,
        album_model=dict(ALBUM_MODEL),
        is_compilation=True,
        compilation_raw_scores=list(raw_scores or []),
        **kwargs,
    )


# ===========================================================================
# 1. The top-tier gate: a scale-free #1 must not be 5★
# ===========================================================================


class TestTheTopTierGate:
    def test_a_tiny_catalogue_number_one_is_gated(self, monkeypatch):
        """THE REPORTED CASE. Gritmonger's "type shit": rank #1 of 9, 1.2k
        listeners. "#1 of 9 songs" was 5★ — now the absolute bar stops it."""
        track = _track("type shit", "Gritmonger", score=50.3, lf=1_200)

        stars = _assign(monkeypatch, track, rank=1, total=9)

        assert stars == 4, "a 1.2k-listener track must not hold a 5★ tier"
        assert track.get("_compilation_top_tier_gated") is True

    def test_an_underground_number_three_is_gated(self, monkeypatch):
        """Echochamber's "devil may cry": #3/171, 713 listeners, 0 LB.

        ⚠️ REFINED (2026-10-27): was 4★. The 5★ gate demoted it by exactly one
        tier, which left a 713-listener track at 4★ — the same "a scale-free
        rank is not popularity" objection, one tier down. The 4★ evidence bars
        (``high_tier_min_score``/``high_tier_min_listeners``) now drop it to the
        absolute ladder. This fixture is a NON-single (``_track`` hardcodes
        ``is_single=False``), so that ladder needs score >= 70 for 3★; 44.8 at
        713 listeners is 2★.
        """
        track = _track("devil may cry", "Echochamber", score=44.8, lf=713)

        stars = _assign(monkeypatch, track, rank=3, total=171)

        assert stars == 2
        assert track.get("_compilation_high_tier_gated") is True

    def test_a_hit_that_clears_both_bars_keeps_five(self, monkeypatch):
        """CONTROL — a track that is both a standout for its artist (score
        83.6) AND genuinely big (300k Last.fm listeners) keeps 5★.

        ⚠️ CONTRACT REVERSAL (2026-10-27). This test used to use Amy Lee's
        "Sally's Song" — 83.6 score, 137,600 listeners — and assert it kept 5★
        under the "either bar" rule. It is now GATED, because the two bars are
        a CONJUNCTION: 137,600 is below the 250,000 listener bar. That is the
        intended tightening (a compilation whose tracks sit at 19k-137k
        listeners should not hand out 26 five-star ratings out of 100), and the
        bar is config-driven — lowering ``top_tier_min_listeners`` restores the
        looser behaviour deliberately rather than by accident.
        """
        track = _track("Big Hit", "Some Band", score=83.6, lf=300_000, lb=151_100)

        stars = _assign(monkeypatch, track, rank=1, total=164)

        assert stars == 5
        assert track.get("_compilation_top_tier_gated") is None

    def test_a_hit_just_under_the_listener_bar_is_gated(self, monkeypatch):
        """The reported tightening, stated as behaviour: Amy Lee's "Sally's
        Song" (83.6 score, 137,600 listeners) is a genuine hit but not a
        top-tier one, so it takes 4★."""
        track = _track("Sally’s Song", "Amy Lee", score=83.6, lf=137_600, lb=151_100)

        stars = _assign(monkeypatch, track, rank=1, total=164)

        assert stars == 4
        assert track.get("_compilation_top_tier_gated") is True

    def test_the_listener_bar_alone_does_not_clear_the_gate(self, monkeypatch):
        """⚠️ CONTRACT REVERSAL (2026-10-27). This test used to assert that
        EITHER bar was enough — "a low score with huge reach keeps 5★".

        That made the guard unreachable, because the score it OR'd against is
        the per-track-artist REMAP (scale-free), so it clears 55 almost always.
        Measured: Pearl Jam's "Leaving Here" (19,396 listeners) held 5★ on
        score 70.27. Both bars are now required.
        """
        track = _track("Big Reach", "Some Band", score=40.0, lf=300_000)

        stars = _assign(monkeypatch, track, rank=2, total=100)

        # ⚠️ Refined again by the 4★ evidence bars: with 300k reach it clears
        # the 4★ listener bar but NOT the 4★ score bar (40 < 45), so it drops to
        # the absolute ladder — which gives 3★ here (the non-single ladder's
        # 75,000-listener rung), not a flat 4.
        assert stars == 3, "reach alone is not a top tier — the score bar failed"
        assert track.get("_compilation_top_tier_gated") is True

    def test_the_score_bar_alone_does_not_clear_the_gate(self, monkeypatch):
        """⚠️ CONTRACT REVERSAL (2026-10-27) — the mirror of the above. A high
        remapped score with 5,000 listeners used to keep 5★; that is exactly
        the "top of a tiny catalogue" case the gate exists to stop."""
        track = _track("High Score", "Some Band", score=70.0, lf=5_000)

        stars = _assign(monkeypatch, track, rank=2, total=100)

        assert stars == 4, "a scale-free score alone is not a top tier"
        assert track.get("_compilation_top_tier_gated") is True

    def test_zeroing_one_bar_leaves_the_other_enforceable(self, monkeypatch):
        """Each bar is independently disableable, so the old looser behaviour is
        one config edit away — and it is DOCUMENTED rather than accidental."""
        track = _track("High Score Only", "Some Band", score=70.0, lf=5_000)

        stars = _assign(
            monkeypatch, track, rank=2, total=100,
            rules={**RULES, "top_tier_min_listeners": 0},
        )

        assert stars == 5, "with the listener bar disabled, the score bar decides"

    def test_the_gate_does_not_touch_lower_tiers(self, monkeypatch):
        """CONTROL — the gate is a TOP-TIER guard only; a 3★ stays 3★."""
        track = _track("Mid", "Some Band", score=50.0, lf=800)

        stars = _assign(monkeypatch, track, rank=60, total=200)

        assert stars == 3

    def test_zero_disables_the_gate(self, monkeypatch):
        """CONTROL — 0 on BOTH bars restores the old behaviour exactly."""
        track = _track("type shit", "Gritmonger", score=50.3, lf=1_200)

        stars = _assign(
            monkeypatch, track, rank=1, total=9,
            rules={**RULES, "top_tier_min_score": 0, "top_tier_min_listeners": 0,
                   "standout_floor_percentile": 0},
        )

        assert stars == 5


# ===========================================================================
# 1b. The guards are VA-only
# ===========================================================================


class TestTheGuardsAreVaOnly:
    """⭐ Absolute reach bars belong to the VA (online) rating model.

    A single-artist compilation is rated on the artist's own catalogue bands.
    The listener floor there suppressed genuine catalogue hits: measured on
    Feuerschwanz "Todsünden" (a covers album — ``va_compilation=False``), every
    CAT-Z 5★ candidate sat at 7.6k-25.3k Last.fm listeners, so the album
    produced ZERO 5★ ratings.
    """

    #: The hit sits at the top of a long tail, so CAT-Z is well past the band.
    ARTIST_SCORES = [86.5, 60.0, 55.0, 50.0, 45.0, 40.0]

    def test_a_catalogue_rated_artist_compilation_keeps_five(self, monkeypatch):
        import services.popularity.popularity_sources as ps
        from services.popularity.stages import finalise_stage as fs

        called: list[str] = []
        monkeypatch.setattr(
            ps, "get_online_artist_track_rank",
            lambda artist, title, lastfm_client=None: called.append(title) or {},
        )
        track = _track("Dragostea Din Tei", "Feuerschwanz", score=86.5, lf=25_290)

        stars = fs._assign_stars(
            track, [86.5], list(self.ARTIST_SCORES),
            album_model=dict(ALBUM_MODEL),
            is_compilation=True,
            is_va_compilation=False,
            compilation_raw_scores=[86.5] * 5,
        )

        assert track["_compilation_rating_mode"] == "track_artist_catalogue"
        assert called == [], "a healthy local catalogue must not go online"
        assert stars == 5, "the VA-only listener floor must not demote it"
        assert track.get("_compilation_top_tier_gated") is None

    def test_a_thin_catalogue_artist_compilation_is_not_gated(self, monkeypatch):
        """The online fallback is still the ARTIST's own record, not a VA one."""
        import services.popularity.popularity_sources as ps
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(
            "helpers.config_helpers.get_compilation_online_catalogue_config",
            lambda: dict(RULES),
        )
        monkeypatch.setattr(
            ps, "get_online_artist_track_rank",
            lambda artist, title, lastfm_client=None: {
                "rank": 1, "total": 200, "percentile": 0.005,
                "listeners": 10_000, "top_listeners": 10_000,
                "source": "lastfm_artist_top_tracks",
            },
        )
        track = _track("Global Hit", "Feuerschwanz", score=86.5, lf=10_000)

        stars = fs._assign_stars(
            track, [86.5], [60.0, 55.0],
            album_model=dict(ALBUM_MODEL),
            is_compilation=True,
            is_va_compilation=False,
            compilation_raw_scores=[86.5] * 5,
        )

        assert track["_compilation_rating_mode"] == "track_artist_online_catalogue"
        assert stars == 5
        assert track.get("_compilation_top_tier_gated") is None


# ===========================================================================
# 2. The standout floor: a buried hit is lifted
# ===========================================================================


class TestTheCompilationStandoutFloor:
    #: The compilation's own cross-artist raw distribution, shaped like the
    #: reported 81-track album: a handful of leaders (91.1 Knocked Loose, 84.9
    #: Korn, 83.0 Deftones) above a long tail of lower-scoring tracks. The tail
    #: MATTERS — the guard asks "is this in the compilation's top band", and on
    #: a 16-track album a 12.5% rank would fall outside a 10% band.
    COMPILATION_RAW = [91.1, 84.9, 83.0, 80.1, 78.5, 77.9, 77.7, 75.0] + [
        round(72.0 - i * 0.5, 1) for i in range(73)
    ]

    def test_a_buried_heavyweight_is_floored(self, monkeypatch):
        """THE REPORTED CASE. Korn's "Reward the Scars" is #159/190 in Korn's
        own catalogue (1★) but a LEADER of this compilation by raw score."""
        track = _track(
            "Reward the Scars", "Korn", score=84.9, lf=53_600, lb=1_900,
            raw=84.9,
        )

        stars = _assign(
            monkeypatch, track, rank=159, total=190,
            raw_scores=self.COMPILATION_RAW,
        )

        assert stars == 4, (
            "a compilation leader must not sit at 1★ because its artist has a "
            "deep discography"
        )
        assert track.get("_compilation_standout_floor") == 4

    def test_the_deep_catalogue_verdict_still_applies_below_the_top_band(
        self, monkeypatch
    ):
        """CONTROL — the floor is a TOP-BAND guard. A track mid-way down the
        compilation keeps the artist-catalogue verdict."""
        track = _track("Mid Table", "Korn", score=50.0, lf=10_000, raw=50.0)

        stars = _assign(
            monkeypatch, track, rank=159, total=190,
            raw_scores=self.COMPILATION_RAW,
        )

        assert stars == 1

    def test_the_floor_never_reaches_five(self, monkeypatch):
        """The artist catalogue keeps ownership of the top tier — the floor
        lifts a buried hit, it does not flatten the album."""
        track = _track("Buried", "Korn", score=91.1, lf=20_000, raw=91.1)

        stars = _assign(
            monkeypatch, track, rank=159, total=190,
            raw_scores=self.COMPILATION_RAW,
        )

        assert stars == 4

    def test_a_large_compilation_still_floors_only_its_top_decile(
        self, monkeypatch
    ):
        """CONTROL — with 100 tracks the top band is 10, so #20 is untouched."""
        raw_scores = [float(100 - i) for i in range(100)]
        track = _track("Twentieth", "Korn", score=80.0, lf=10_000, raw=80.0)

        stars = _assign(
            monkeypatch, track, rank=159, total=190, raw_scores=raw_scores,
        )

        assert stars == 1, "#20 of 100 is not in the top decile"

    def test_no_raw_distribution_means_no_floor(self, monkeypatch):
        """CONTROL — without the compilation's own distribution the guard
        cannot judge, and the previous behaviour stands."""
        track = _track("Reward the Scars", "Korn", score=84.9, lf=53_600)

        stars = _assign(monkeypatch, track, rank=159, total=190, raw_scores=[])

        assert stars == 1

    def test_a_thin_distribution_means_no_floor(self, monkeypatch):
        """CONTROL — fewer than five raw scores is not a distribution."""
        track = _track("Reward the Scars", "Korn", score=84.9, lf=53_600)

        stars = _assign(
            monkeypatch, track, rank=159, total=190, raw_scores=[90.0, 85.0],
        )

        assert stars == 1

    def test_zero_percentile_disables_the_floor(self, monkeypatch):
        """CONTROL — 0 disables it, which is why the config UI reads 0 as a
        real value rather than falling back to the default."""
        track = _track("Reward the Scars", "Korn", score=84.9, lf=53_600)

        stars = _assign(
            monkeypatch, track, rank=159, total=190,
            raw_scores=self.COMPILATION_RAW,
            rules={**RULES, "standout_floor_percentile": 0},
        )

        assert stars == 1


# ===========================================================================
# 3. The two guards together, on the reported album
# ===========================================================================


class TestTheReportedAlbum:
    """The full inversion: both directions corrected on one pass."""

    COMPILATION_RAW = TestTheCompilationStandoutFloor.COMPILATION_RAW

    @pytest.mark.parametrize(
        "title,artist,score,lf,rank,total,expected,why",
        [
            # (a) tiny catalogues gated out of the top tier
            ("type shit", "Gritmonger", 50.3, 1_200, 1, 9, 4, "1.2k listeners"),
            # ⚠️ REFINED (2026-10-27): these two were 4★. The 4★ evidence bars
            # drop anything scoring under 45 to the absolute ladder — which for
            # a NON-single (this fixture) needs 70 for 3★, so 2★ is the honest
            # tier rather than "one below the rank it was given".
            ("devil may cry", "Echochamber", 44.8, 713, 3, 171, 2, "713 listeners"),
            ("Do You Feel Alive?", "Alchemize", 35.3, 1_400, 1, 68, 2, "1.4k listeners"),
            ("KICK BACK", "Private School", 37.0, 2_800, 2, 192, 2, "2.8k listeners"),
            # (b) buried heavyweights floored
            ("infinite source", "Deftones", 83.0, 401_200, 58, 187, 4, "compilation leader"),
            ("Reward the Scars", "Korn", 84.9, 53_600, 159, 190, 4, "compilation leader"),
            # (c) genuinely popular tracks keep their tier
            # ⚠️ CONTRACT REVERSAL (2026-10-27): "Sally's Song" (137,600 LF) was
            # listed here as a "genuine hit" expected to keep 5★. Under the
            # conjunction it is GATED — 137.6k is below the 250k listener bar.
            # A track that clears BOTH bars is the new control (see
            # TestTheTopTierGate::test_a_hit_that_clears_both_bars_keeps_five).
            ("Sally’s Song", "Amy Lee", 83.6, 137_600, 1, 164, 4, "below the listener bar"),
        ],
    )
    def test_the_final_tier(
        self, monkeypatch, title, artist, score, lf, rank, total, expected, why
    ):
        track = _track(title, artist, score=score, lf=lf, raw=score)

        stars = _assign(
            monkeypatch, track, rank=rank, total=total,
            raw_scores=self.COMPILATION_RAW,
        )

        assert stars == expected, f"{title} ({why})"


# ===========================================================================
# 4. The config plumbing
# ===========================================================================


class TestTheConfigPlumbing:
    def test_the_new_keys_have_defaults(self):
        from helpers.config_helpers import (
            _DEFAULT_COMPILATION_ONLINE_CATALOGUE,
            get_compilation_online_catalogue_config,
        )

        rules = get_compilation_online_catalogue_config({})

        for key in (
            "top_tier_min_score",
            "top_tier_min_listeners",
            "standout_floor_percentile",
            "standout_floor_stars",
        ):
            assert key in _DEFAULT_COMPILATION_ONLINE_CATALOGUE, key
            assert key in rules, f"{key} must always be present"

    def test_a_user_block_overrides_only_what_it_names(self):
        from helpers.config_helpers import get_compilation_online_catalogue_config

        rules = get_compilation_online_catalogue_config({
            "single_detection": {
                "compilation_online_catalogue": {"top_tier_min_listeners": 0},
            },
        })

        assert rules["top_tier_min_listeners"] == 0
        assert rules["top_tier_min_score"] == 55.0, "the sibling default stands"

    def test_a_zero_survives_the_config_round_trip(self):
        """0 is a MEANINGFUL value (it disables a guard) — it must not be
        replaced by the default."""
        from helpers.config_helpers import get_compilation_online_catalogue_config

        rules = get_compilation_online_catalogue_config({
            "single_detection": {
                "compilation_online_catalogue": {
                    "top_tier_min_score": 0,
                    "standout_floor_percentile": 0,
                },
            },
        })

        assert rules["top_tier_min_score"] == 0.0
        assert rules["standout_floor_percentile"] == 0.0

    def test_the_config_page_renders_every_key(self):
        """The Config page is the source of truth for user-editable settings."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for relative in ("test_site/templates/Pages/config.html",
                         "templates/pages/config.html"):
            source = (root / relative).read_text(encoding="utf-8")
            for key in (
                "compilation_online_top_tier_min_score",
                "compilation_online_top_tier_min_listeners",
                "compilation_online_standout_floor_percentile",
                "compilation_online_standout_floor_stars",
            ):
                assert key in source, f"{relative} must render {key}"

    def test_the_config_js_saves_every_key(self):
        """An input the save path does not read is a dead control."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for relative in ("test_site/static/js/pages/config.js",
                         "static/js/config.js"):
            source = (root / relative).read_text(encoding="utf-8")
            for key in (
                "top_tier_min_score",
                "top_tier_min_listeners",
                "standout_floor_percentile",
                "standout_floor_stars",
            ):
                assert key in source, f"{relative} must save {key}"

    def test_the_save_path_preserves_a_zero(self):
        """``parseFloat(x) || default`` turns a user's 0 into the default — the
        exact bug the finite check exists to avoid."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for relative in ("test_site/static/js/pages/config.js",
                         "static/js/config.js"):
            source = (root / relative).read_text(encoding="utf-8")
            assert "isFinite(v) ? v : 55" in source, (
                f"{relative} must read top_tier_min_score with a finite check"
            )
