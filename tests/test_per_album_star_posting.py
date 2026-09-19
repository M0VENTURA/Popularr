"""Regression tests for per-album star rating posting.

The staged runner used to defer ALL star-rating assignment/persistence/logging
to ``finalise_scan`` at the end of the whole run, so a full artist scan only
posted the "Star Ratings - Album ..." summary once everything had finished.
The legacy scanner posted each album's ratings right after the album
completed.  These tests cover:

- ``post_album_star_ratings`` assigns, persists, logs and syncs one album's
  ratings and returns counts.
- ``finalise_scan`` honors ``options["_per_album_posted"]`` — when the runner
  already posted per-album ratings during the scan loop, finalise skips the
  (now redundant) per-album work but still writes artist_stats and the
  essential playlist.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RecordingSession:
    """Stub session that records executes and returns fixed (empty) rows.

    Mirrors the real ``db_session()`` contract: each ``execute`` returns a
    result with ``fetchall()``/``fetchone()``, and statements are recorded so
    tests can assert exactly which SQL/params ran.
    """

    def __init__(self, rows_by_execute=None):
        self.executed: list[tuple] = []
        self._results = list(rows_by_execute or [])
        self._index = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        rows = self._results[self._index] if self._index < len(self._results) else []
        self._index += 1
        return _FakeResult(rows)

    def commit(self):
        pass

    def rollback(self):
        pass


@contextmanager
def _fake_db_session(session):
    yield session


def _session_factory(session):
    """Return a ``db_session`` callable backed by ONE shared fake session.

    Sequential ``execute`` calls across separate ``with`` blocks advance one
    shared result queue — mirrors the real session-per-call behaviour.
    """
    @contextmanager
    def _cm():
        yield session

    return _cm


def _album_results():
    return [
        {
            "track_id": "t1",
            "artist": "Muse",
            "album": "Absolution",
            "title": "Apocalypse Please",
            "popularity_score": 80.0,
            "final_score": 80.0,
            "lastfm_listeners": 500,
            "listenbrainz_listens": 4000,
            "lastfm_score": 6.0,
            "listenbrainz_score": 7.0,
            "is_single": False,
            "single_confidence": "low",
        },
        {
            "track_id": "t2",
            "artist": "Muse",
            "album": "Absolution",
            "title": "Time Is Running Out",
            "popularity_score": 90.0,
            "final_score": 90.0,
            "lastfm_listeners": 1000,
            "listenbrainz_listens": 8000,
            "lastfm_score": 8.0,
            "listenbrainz_score": 9.0,
            "is_single": True,
            "single_confidence": "high",
        },
    ]


class TestAlbumZBandStars:
    """Spec rule 4: 1-4★ come purely from the album's popularity z-score bands.

    After 5★ singles/standouts are assigned, the rest of the album is ranked by
    its intra-album z-score: Z >= +0.5 → 4★, -0.5 <= Z < +0.5 → 3★,
    -1.2 <= Z < -0.5 → 2★, Z < -1.2 → 1★.  Artist context never affects the
    base rating, and popularity alone never reaches 5★.
    """

    def _track(self, score, **overrides):
        track = {
            "track_id": "t1", "artist": "A", "album": "B", "title": "Song",
            "popularity_score": score, "final_score": score,
            "lastfm_listeners": 100, "listenbrainz_listens": 100,
            "lb_percentile": 0.5, "lastfm_score": 4.0, "listenbrainz_score": 4.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False, "popularity_marked": False,
        }
        track.update(overrides)
        return track

    def _album(self):
        # 12 tracks spread 1-100 → mean 50.5, sample stdev ~32.45, so:
        # 4★ for 100/91/82/73 (z >= +0.5), 3★ for 64/46, 2★ for 28/19,
        # 1★ for 10/1 (z < -1.2).
        return list(range(1, 101, 9))[:12]  # [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91, 100]

    def _stars(self, score, album, **overrides):
        from services.popularity.stages import finalise_stage as fs
        return fs._assign_stars(self._track(score, **overrides), album, album)

    def test_top_band_gets_four(self):
        # Z >= +0.5 → 4★.
        album = self._album()
        assert self._stars(100.0, album) == 4
        assert self._stars(73.0, album) == 4

    def test_middle_band_gets_three(self):
        # -0.5 <= Z < +0.5 → 3★.
        assert self._stars(64.0, self._album()) == 3
        assert self._stars(46.0, self._album()) == 3

    def test_lower_band_gets_two(self):
        # -1.2 <= Z < -0.5 → 2★.
        assert self._stars(28.0, self._album()) == 2
        assert self._stars(19.0, self._album()) == 2

    def test_bottom_outliers_gets_one(self):
        # Z < -1.2 → 1★.  Robust (median+MAD) z is used, so the truly lowest
        # outlier (score 1 → z ≈ -1.24) is 1★ while score 10 (z ≈ -1.01) still
        # sits in the 2★ band.
        assert self._stars(1.0, self._album()) == 1
        assert self._stars(10.0, self._album()) == 2

    def test_zero_score_is_one_star(self):
        assert self._stars(0.0, self._album()) == 1

    def test_high_confidence_single_gets_five_regardless_of_band(self):
        # Spec rule 5: a high-confidence single is 5★ even at the bottom of a
        # strong album — singles are exempt from the album-z-band base (as
        # long as the ORGANIC popularity floor is met: score >= 45 or >= 1000
        # Last.fm listeners).
        assert self._stars(
            10.0, self._album(), is_single=True, single_confidence="high",
            lastfm_listeners=2000,
        ) == 5

    def test_high_confidence_single_below_organic_floor_stays_on_band(self):
        # A metadata-tagged single with almost no organic audience (e.g.
        # Discogs-confirmed with 299 listeners and a sub-45 score) must not
        # leapfrog genuinely popular album tracks — it keeps the album-z band
        # rating instead of the forced single promotion.  Robust z puts score
        # 10 in the 2★ band (z ≈ -1.01).
        from services.popularity.stages import finalise_stage as fs

        assert self._stars(
            10.0, self._album(), is_single=True, single_confidence="high",
            lastfm_listeners=299,
        ) == 2
        # The same gate caps the 4★ Single Floor: a non-organic high single
        # that misses the era bar never exceeds 3★ (era-model path).  Score
        # must be below the organic floor (45.0) — a 46.0 score meets it and
        # legitimately earns the 4★ Single Floor instead.
        track = self._track(
            37.0, is_single=True, single_confidence="high", lastfm_listeners=299
        )
        album_model = {"has_benchmark": True, "era": "peak", "catalog_cutoff": 80.0,
                       "max_5star_slots": 4}
        assert fs._assign_stars(track, self._album(), self._album(), album_model=album_model) <= 3

    def test_medium_single_not_marked_follows_album_band(self):
        # A medium-confidence single that is NOT popularity-marked stays on the
        # album-z-band base (no 5★) unless it is a genuine standout.
        album = self._album()
        assert self._stars(46.0, album, is_single=True, single_confidence="medium") == 3

    def test_popularity_marked_standout_gets_five(self):
        # A non-single triple-standout (album z + artist z + top-10% marking)
        # reaches 5★ per spec rule 5.
        album = [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91, 100]
        assert self._stars(100.0, album, popularity_marked=True) == 5

    def test_popularity_marked_alone_gets_five_without_single_source(self):
        # Spec rule 2: a track in the artist's top 10% is "popular" — the
        # marking alone grants 5★ even though it is NOT a high-confidence
        # single and does NOT clear the album/artist standout thresholds.
        album = [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91, 100]
        assert self._stars(46.0, album, popularity_marked=True, is_single=False,
                           single_confidence="low") == 5

    def test_z_standout_source_counts_as_popularity_proof(self):
        # The popularity_z_standout detection signal is an alternative proof for
        # the 5★ standout condition.
        from services.popularity.stages import finalise_stage as fs
        album = [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91, 100]
        track = self._track(
            100.0,
            single_sources='[{"source": "popularity_z_standout", "matched": true, "confidence": 0.5}]',
        )
        assert fs._assign_stars(track, album, album) == 5


class TestZStandoutSourceReVerification:
    """A ``popularity_z_standout`` source is re-verified against the album's raw
    listener distribution before it can grant 5★.

    On a standout album (one album far more popular than the rest of an
    artist's catalogue) every track's artist_z is inflated, so the old code
    marked whole albums as ``popularity_z_standout`` and promoted them to 5★.
    The scan re-verifies the flag via the track's composite LF/LB listener z
    (``listener_5star_z_threshold``) so only genuine intra-album standouts
    reach 5★ from popularity.
    """

    def _track(self, score, lastfm, lb, **overrides):
        track = {
            "track_id": "t1", "artist": "A", "album": "B", "title": "Song",
            "popularity_score": score, "final_score": score,
            "lastfm_listeners": lastfm, "listenbrainz_listens": lb,
            "lb_percentile": 0.5, "lastfm_score": 4.0, "listenbrainz_score": 4.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False, "popularity_marked": False,
        }
        track.update(overrides)
        return track

    def _album(self):
        return [40.0, 50.0, 60.0, 70.0, 80.0, 90.0]

    def _artist(self):
        # The album distribution [40..90] gives the top track a ROBUST
        # (median+MAD) artist z of ~1.12 — below the 5★ standout gate (1.2),
        # so ``popularity_z_standout`` could never reach 5★ regardless of the
        # listener re-verification being tested.  A wider catalogue where the
        # top track genuinely clears the robust gate (z=1.21) isolates the
        # listener-verification behaviour these tests exercise.
        return [1.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0]

    def _listeners(self):
        return [10000, 9000, 8000, 7000, 6000, 5000], [20000, 18000, 16000, 14000, 12000, 10000]

    def test_mid_pack_listeners_do_not_honour_standout_flag(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listeners()
        # Album z AND artist z clear the 5★ standout thresholds, and the track
        # carries a popularity_z_standout source — but its RAW listener counts
        # sit mid-pack in the album, so the re-verification must NOT honour the
        # flag. Popularity alone never reaches 5★.
        track = self._track(
            90.0, 6000, 12000,
            single_sources='[{"source": "popularity_z_standout", "matched": true, "confidence": 0.5}]',
        )
        assert fs._assign_stars(track, self._album(), self._artist(), lf, lb) == 4

    def test_album_top_listeners_honour_standout_flag(self):
        from services.popularity.stages import finalise_stage as fs

        # A GENUINE album-internal standout shape: 50,000 against a 500
        # runner-up (100x), a 350 median (~143x) and a 100 floor (500x), so
        # all three ratio multipliers clear comfortably.
        #
        # NOTE the fixture change. This test previously reused ``_listeners()``
        # = [10000, 9000, 8000, 7000, 6000, 5000], a uniformly-declining album
        # whose top track is only 1.11x the runner-up and 1.54x the median.
        # Under the album RATIO standout rule that distribution contains NO
        # standout, so 5* is now correctly withheld from it — the fixture had
        # to become an actually-standout album to keep testing what the test
        # is named for. See TestAlbumRatioStandout for the policy itself.
        lf = [50000, 500, 400, 300, 200, 100]
        lb = [20000, 18000, 16000, 14000, 12000, 10000]
        track = self._track(
            90.0, 50000, 20000,
            single_sources='[{"source": "popularity_z_standout", "matched": true, "confidence": 0.5}]',
        )
        assert fs._assign_stars(track, self._album(), self._artist(), lf, lb) == 5

    def test_no_album_listener_distribution_keeps_flag(self):
        from services.popularity.stages import finalise_stage as fs

        # No listener distribution → verification is skipped, flag honoured.
        track = self._track(
            90.0, 6000, 12000,
            single_sources='[{"source": "popularity_z_standout", "matched": true, "confidence": 0.5}]',
        )
        assert fs._assign_stars(track, self._album(), self._artist()) == 5

    def test_popularity_marked_is_ratio_gated_but_not_listener_z_gated(self):
        from services.popularity.stages import finalise_stage as fs

        # TWO gates sit on the 5* path and they have DIFFERENT scope:
        #
        #   * the ``popularity_z_standout`` re-verification (composite LF/LB
        #     listener z >= listener_5star_z_threshold) applies ONLY to that
        #     source; and
        #   * the album RATIO standout test applies to EVERY popularity-based
        #     5*, including the artist top-10% ``popularity_marked`` flag.
        #
        # This test previously asserted the marking was exempt from listener
        # gating entirely. It is still exempt from the LISTENER-Z check — but
        # the top-10% marking alone is no longer sufficient on a flat album:
        # the track must also be a genuine standout within its own release.
        #
        # The artist top-10% marking is a CATALOGUE-level signal; it says
        # nothing about whether the album itself contains a standout. A hit
        # sitting on an album of uniformly-played tracks is not a standout.
        lf_flat = [10000, 9000, 8000, 7000, 6000, 5000]
        lb_flat = [20000, 18000, 16000, 14000, 12000, 10000]

        flat = self._track(90.0, 10000, 20000, popularity_marked=True)
        assert fs._assign_stars(flat, self._album(), self._artist(), lf_flat, lb_flat) == 4

        # ...whereas a real standout shape does grant 5*.
        lf_real = [50000, 500, 400, 300, 200, 100]
        real = self._track(90.0, 50000, 20000, popularity_marked=True)
        assert fs._assign_stars(real, self._album(), self._artist(), lf_real, lb_flat) == 5


class TestAlbumRatioStandout:
    """5★ must ALSO require the album to contain a genuine standout.

    Why: the z-score paths answer "how far above this album's mean is the
    track?" — but on a small, FLAT album the spread shrinks too, so a
    6-listener gap over the runner-up can clear every z-bound while the album
    plainly has no standout. The ratio test is scale-free and closes that hole.

    Reference case is a real one (Anti-Flag, acoustic album; counts
    14, 13, 13, ~11 median, 8), where the top track is only 1.07x the
    runner-up and 1.27x the median — a tie, not a standout.
    """

    #: The reference album. Deliberately FLAT at a tiny magnitude.
    ANTIFLAG = [14, 13, 13, 11, 11, 10, 9, 8]

    #: A genuine studio standout: hit + unpopular deep cuts.
    REAL_STANDOUT = [500000, 40000, 30000, 25000, 20000, 15000, 12000, 10000]

    def _track(self, score, lf, lb, **overrides):
        track = {
            "track_id": "t1", "artist": "Anti-Flag", "album": "Acoustic",
            "title": "Song",
            "popularity_score": score, "final_score": score,
            "lastfm_listeners": lf, "listenbrainz_listens": lb,
            "lastfm_score": 8.0, "listenbrainz_score": 9.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False, "popularity_marked": True,
        }
        track.update(overrides)
        return track

    #: Wide album/artist score spread so the z-bounds are satisfied — the
    #: ratio test must be what decides, not a z-score failure.
    _SCORES = [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91, 100]

    def test_anticheat_reference_album_is_not_a_five_star(self):
        from services.popularity.stages import finalise_stage as fs

        track = self._track(100.0, 14, 0)
        stars = fs._assign_stars(
            track, self._SCORES, self._SCORES,
            [float(v) for v in self.ANTIFLAG], [0.0] * len(self.ANTIFLAG),
        )
        assert stars < 5, (
            "the Anti-Flag reference album (14/13/13/11/11/10/9/8) has no "
            "standout — top is only 1.08x the runner-up and 1.27x the median"
        )
        assert "ratio_flat" in str(track.get("_ratio_5star_reason") or "")

    def test_real_standout_album_still_reaches_five_stars(self):
        from services.popularity.stages import finalise_stage as fs

        track = self._track(100.0, 500000, 0)
        stars = fs._assign_stars(
            track, self._SCORES, self._SCORES,
            [float(v) for v in self.REAL_STANDOUT], [0.0] * len(self.REAL_STANDOUT),
        )
        assert stars == 5, (
            "a hit at 500k against a 10k floor (50x) on a wide-spread album "
            "is exactly the shape the rule should accept"
        )

    def test_inconclusive_data_never_blocks_five_stars(self):
        from services.popularity.stages import finalise_stage as fs

        # Only two listener counts -> no distribution to judge. The gate must
        # be permissive here, or short albums could never score 5★.
        track = self._track(100.0, 14, 0)
        stars = fs._assign_stars(track, self._SCORES, self._SCORES, [14.0, 13.0], [0.0, 0.0])
        assert stars == 5
        assert "inconclusive" in str(track.get("_ratio_5star_reason") or "")

    def test_non_top_track_is_ineligible(self):
        from services.popularity.stages import finalise_stage as fs

        # The multipliers describe the distance between #1 and the rest, so a
        # track that is not the album's top can never satisfy them — this is
        # what stops a standout album from minting several 5★ tracks.
        track = self._track(100.0, 40000, 0)
        stars = fs._assign_stars(
            track, self._SCORES, self._SCORES,
            [float(v) for v in self.REAL_STANDOUT], [0.0] * len(self.REAL_STANDOUT),
        )
        assert stars < 5
        assert "not_album_top" in str(track.get("_ratio_5star_reason") or "")

    def test_gate_can_be_disabled_by_config(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import helpers.config_helpers as ch

        real = ch.get_standout_config
        monkeypatch.setattr(
            fs, "get_standout_config",
            lambda: {**real(), "album_ratio_standout": {"enabled": 0}},
        )
        track = self._track(100.0, 14, 0)
        stars = fs._assign_stars(
            track, self._SCORES, self._SCORES,
            [float(v) for v in self.ANTIFLAG], [0.0] * len(self.ANTIFLAG),
        )
        assert stars == 5, "disabled gate must restore the pre-ratio behaviour"

    def test_live_album_uses_the_looser_live_thresholds(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import helpers.config_helpers as ch
        from services.popularity.popularity_math import calculate_robust_zscore

        # A live album routinely has a flat BOTTOM (applause/tuning on every
        # track), so the floor ratio rarely clears. The live rules require only
        # 2 of 3 multipliers for exactly this reason.
        artist = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0]
        live_shape = [500000, 40000, 12000, 11000, 10500, 10000, 9800, 9500]
        track = self._track(
            100.0, 500000, 0, is_live=True, single_confidence="high",
        )
        stars = fs._assign_stars(
            track, [40.0, 50.0, 60.0, 70.0, 80.0, 90.0], artist,
            [float(v) for v in live_shape], [0.0] * len(live_shape),
            popularity_only=False, is_live_album=True,
        )
        assert stars == 5, "2/3 live multipliers met -> still a live standout"

    def test_live_flat_album_is_gated(self):
        from services.popularity.stages import finalise_stage as fs

        artist = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0]
        track = self._track(
            100.0, 14, 0, is_live=True, single_confidence="high",
        )
        stars = fs._assign_stars(
            track, [40.0, 50.0, 60.0, 70.0, 80.0, 90.0], artist,
            [float(v) for v in self.ANTIFLAG], [0.0] * len(self.ANTIFLAG),
            popularity_only=False, is_live_album=True,
        )
        assert stars <= 4

    def test_user_override_is_never_demoted(self):
        from services.popularity.stages import finalise_stage as fs

        artist = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0]
        track = self._track(
            100.0, 14, 0, is_live=True, single_confidence="user",
        )
        stars = fs._assign_stars(
            track, [40.0, 50.0, 60.0, 70.0, 80.0, 90.0], artist,
            [float(v) for v in self.ANTIFLAG], [0.0] * len(self.ANTIFLAG),
            popularity_only=False, is_live_album=True,
        )
        assert stars == 5, "a user override is an explicit instruction, not a guess"


class TestPostAlbumStarRatings:
    def test_assigns_persists_logs_and_syncs(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine

        session = _RecordingSession()
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))
        # Deterministic star value per track so assertions are stable.
        monkeypatch.setattr(
            fs, "_assign_stars",
            lambda track, *args, **kwargs: 4 if track["title"] == "Apocalypse Please" else 5
        )
        monkeypatch.setattr(fs, "_sync_rating_to_navidrome", lambda track_id, stars, clients=None: True)
        logged = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(msg))

        results = _album_results()
        outcome = fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={"sync_navidrome": True},
        )

        assert outcome["star_ratings"] == 2
        assert outcome["navidrome_synced"] == 2
        assert [t["stars"] for t in results] == [4, 5]

        # Each rated track persisted its star rating.
        star_updates = [e for e in session.executed if "UPDATE tracks SET stars" in str(e[0])]
        assert len(star_updates) == 2
        assert any(e[1].get("tid") == "t2" and e[1].get("stars") == 5 for e in star_updates)

        # Per-album tabular summary was emitted (new format).
        assert any("📊 SCAN RESULTS: Muse — Absolution" in m for m in logged)
        assert any("Singles Detection - Detected 1 single(s) in 'Absolution'" in m for m in logged)
        assert any("⭐ Distribution" in m for m in logged)

    def test_returns_zero_for_empty_album(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine

        session = _RecordingSession()
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))
        outcome = fs.post_album_star_ratings(
            album_results=[],
            artist="Muse",
            artist_scores=[],
            options={},
        )
        assert outcome == {"star_ratings": 0, "navidrome_synced": 0}
        assert session.executed == []


class TestAssignStarsPopularityOnly:
    """A popularity-only pass rates on popularity alone.

    Single detection did not run, so single status must not drive the rating.
    The album-relative spec reserves 5★ for high-confidence singles and
    genuine triple-standouts (album z + artist z + a popularity standout) —
    popularity alone never grants 5★.
    """

    def _artist_scores(self):
        return [50, 52, 54, 56, 58, 60, 62, 64, 66, 90]

    def _album_scores(self):
        return [50, 55, 60, 65, 90]

    def _base_track(self, **overrides):
        track = {
            "track_id": "t1", "artist": "A", "album": "B", "title": "Song",
            "popularity_score": 90.0, "final_score": 90.0,
            "lastfm_listeners": 5000, "listenbrainz_listens": 4000,
            "lb_percentile": 0.95, "lastfm_score": 8.0, "listenbrainz_score": 9.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False, "popularity_marked": False,
        }
        track.update(overrides)
        return track

    def _listener_counts(self):
        return [100, 200, 300, 500, 5000], [200, 300, 400, 500, 4000]

    def test_album_top_without_standout_proof_is_four_star(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listener_counts()
        # Top of the album (and catalogue) but no popularity-standout proof
        # (no top-10% marking, no popularity_z_standout source) → 4★, the top
        # of the album-percentile band. Popularity alone never reaches 5★.
        track = self._base_track()
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=True) == 4
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=False) == 4

    def test_popularity_marked_standout_gets_five_star(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listener_counts()
        # A genuine triple-standout (album z + artist z + artist top-10%
        # marking) reaches 5★ in both popularity-only and full scans.
        track = self._base_track(popularity_marked=True)
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=True) == 5
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=False) == 5

    def test_single_status_does_not_inflate_popularity_only_rating(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listener_counts()
        # A high-confidence single with only average popularity rates on its
        # popularity alone (2★) in a popularity-only pass — single status is
        # not a rating input because detection didn't run this pass. The full
        # scan honours the confirmed single and grants 5★.
        track = self._base_track(
            title="Mid Single",
            popularity_score=54.0,
            final_score=54.0,
            is_single=True,
            single_confidence="high",
            lastfm_listeners=300,
            listenbrainz_listens=200,
            lb_percentile=0.4,
        )
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=True) == 2
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=False) == 5

    def test_live_track_caps_at_four_in_popularity_only(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listener_counts()
        # Live recordings never reach 5★ from popularity alone (legacy parity).
        track = self._base_track(is_live=True)
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=True) <= 4

    def test_user_override_survives_popularity_only(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listener_counts()
        # A manually-set single is an explicit user preference — preserved by
        # every scan type, including popularity-only passes.
        track = self._base_track(single_confidence="user", popularity_score=10.0, final_score=10.0)
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=True) == 5


class TestComputeArtistScoresExcludesScanned:
    """Artist-wide score merge must not double-count this scan's tracks.

    Tracks scored during the current scan were persisted to the DB, so a naive
    merge (scan scores + ALL stored final_scores) double-counts them and
    drifts the artist z-scores. The album-level merge already excludes scanned
    titles; ``compute_artist_scores`` must do the same.
    """

    def test_scanned_titles_excluded_from_db_merge(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine
        from sqlalchemy import create_engine, text as sa_text

        # Real SQLAlchemy Rows for the artist-score merge SELECT.
        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            conn.execute(sa_text("CREATE TABLE t (title TEXT, album TEXT, final_score REAL)"))
            for title, score in [
                ("Scanned A", 80.0), ("Scanned B", 90.0), ("Older Track", 70.0),
            ]:
                conn.execute(
                    sa_text("INSERT INTO t (title, album, final_score) VALUES (:t, 'Album', :s)"),
                    {"t": title, "s": score},
                )
            rows = list(conn.execute(sa_text("SELECT title, album, final_score FROM t")).fetchall())

        session = _RecordingSession([rows])
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))

        # Both scanned titles must be excluded → only the older track anchors.
        scores = fs.compute_artist_scores(
            "Artist",
            [80.0, 90.0],
            scanned_titles={"scanned a", "scanned b"},
        )
        assert scores == [80.0, 90.0, 70.0]


class TestFinaliseScanPerAlbumFlag:
    def _fegefeuer_results(self):
        # One album, three tracks that carry different TRACK artists (a
        # featured-artist split).  They must all group under the album artist
        # "Feuerschwanz", not fragment into three 1-track "albums".
        return [
            {
                "track_id": f"t{i}",
                "artist": artist,
                "album_artist": "Feuerschwanz",
                "album": "Fegefeuer",
                "title": f"Song {i}",
                "popularity_score": 60.0,
                "final_score": 60.0,
                "lastfm_listeners": 100,
                "listenbrainz_listens": 200,
                "lastfm_score": 4.0,
                "listenbrainz_score": 4.0,
                "is_single": False,
                "single_confidence": "low",
                "single_sources": "",
            }
            for i, artist in enumerate(
                ["Feuerschwanz", "Feuerschwanz feat. Fabienne Erni", "Feuerschwanz feat. Melissa Bonny"]
            )
        ]

    def test_featured_tracks_group_by_album_artist(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        posted = []
        monkeypatch.setattr(
            fs,
            "post_album_star_ratings",
            lambda **kw: posted.append(kw) or {"star_ratings": 3, "navidrome_synced": 0},
        )
        monkeypatch.setattr(fs, "_create_essential_m3u", lambda artist: None)
        monkeypatch.setattr(fs, "log_unified", lambda msg: None)

        fs.finalise_scan(
            results=self._fegefeuer_results(),
            options={"sync_navidrome": True},
        )

        # Exactly ONE album posting under the album artist — the feat. tracks
        # must not split into separate 1-track albums (tracks=1, MAD=0.0 →
        # broken z-scores / duplicate Navidrome syncs).
        assert len(posted) == 1
        assert posted[0]["artist"] == "Feuerschwanz"
        assert len(posted[0]["album_results"]) == 3
        assert posted[0]["album_results"][0]["album"] == "Fegefeuer"

    def test_per_album_posted_skips_duplicate_star_work(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine

        session = _RecordingSession()
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))
        posted = []
        monkeypatch.setattr(
            fs,
            "post_album_star_ratings",
            lambda **kw: posted.append(kw),
        )
        monkeypatch.setattr(fs, "_create_essential_m3u", lambda artist: None)
        logged = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(msg))

        results = _album_results()
        fs.finalise_scan(
            results=results,
            options={
                "_per_album_posted": True,
                "_per_album_posted_keys": {("Muse", "Absolution")},
            },
        )

        # Runner already posted per-album ratings → finalise must not re-assign.
        assert posted == []
        # artist_stats write still happened (album/artist context data).
        stats_writes = [e for e in session.executed if "INSERT INTO artist_stats" in str(e[0])]
        assert len(stats_writes) == 1

    def test_without_flag_delegates_per_album(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine

        session = _RecordingSession()
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))
        posted = []
        monkeypatch.setattr(
            fs,
            "post_album_star_ratings",
            lambda **kw: posted.append(kw) or {"star_ratings": 2, "navidrome_synced": 0},
        )
        monkeypatch.setattr(fs, "_create_essential_m3u", lambda artist: None)
        monkeypatch.setattr(fs, "log_unified", lambda msg: None)

        fs.finalise_scan(
            results=_album_results(),
            options={"sync_navidrome": True},
        )

        assert len(posted) == 1
        assert posted[0]["album_results"][0]["track_id"] == "t1"


class TestListenerZRobustness:
    """log_listener_z must never divide by zero.

    An album whose positive listener/listen counts are all the same value
    (e.g. a tracklist fallback that resolved every track to the same count)
    has zero variance — the old code computed ``stdev == 0`` and crashed the
    whole album's star-rating pass with ``ZeroDivisionError``, leaving every
    track unrated.  The helper now lives in ``popularity_zscore`` (shared by
    single detection and the star-rating stage).
    """

    def test_zero_variance_distribution_returns_zero(self):
        from services.popularity.popularity_zscore import log_listener_z

        # 3+ identical positive counts → sigma == 0 → must not raise.
        assert log_listener_z(300, [300, 300, 300]) == 0.0
        assert log_listener_z(500, [500, 500, 500, 500]) == 0.0

    def test_mixed_counts_still_score(self):
        from services.popularity.popularity_zscore import log_listener_z

        z = log_listener_z(1000, [100, 200, 300, 1000])
        assert z > 1.0

    def test_assign_stars_survives_identical_counts(self):
        from services.popularity.stages import finalise_stage as fs

        track = {
            "track_id": "t1", "artist": "A", "album": "B", "title": "Song",
            "popularity_score": 40.0, "final_score": 40.0,
            "lastfm_listeners": 300, "listenbrainz_listens": 300,
            "lb_percentile": 0.5, "lastfm_score": 4.0, "listenbrainz_score": 4.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False,
        }
        stars = fs._assign_stars(track, [40.0, 40.0, 40.0], [40.0, 40.0, 40.0],
                                 [300, 300, 300], [300, 300, 300])
        assert 1 <= stars <= 5


class _Row:
    """Row stub exposing a ``_mapping`` dict (the batch-load query reads it)."""

    def __init__(self, mapping):
        self._mapping = mapping


class TestSkipUnchangedRatingWrites:
    """Ratings that match the stored value must not rewrite the audio file or
    re-sync to Navidrome — mutagen rewrites the whole file on every rating
    save, which is the dominant scan disk-write source."""

    def _rows(self, tmp_path):
        f1 = tmp_path / "t1.mp3"
        f2 = tmp_path / "t2.mp3"
        f1.write_bytes(b"")
        f2.write_bytes(b"")
        return [
            _Row({"id": "t1", "stars": 5, "file_path": str(f1)}),
            _Row({"id": "t2", "stars": 5, "file_path": str(f2)}),
        ]

    def test_unchanged_rating_skips_file_write_and_navidrome_sync(self, monkeypatch, tmp_path):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine
        import services.metadata.tag_file_service as tag_file_service

        session = _RecordingSession([
            [],                                  # album DB score merge
            [],                                  # album scaling model
            self._rows(tmp_path),                # batch rating/path load
        ])
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))
        # t1 → 4★ (CHANGED from stored 5★), t2 → 5★ (UNCHANGED).
        monkeypatch.setattr(
            fs, "_assign_stars",
            lambda track, *args, **kwargs: 4 if track["title"] == "Apocalypse Please" else 5
        )
        syncs = []
        monkeypatch.setattr(fs, "_sync_rating_to_navidrome", lambda tid, stars, clients=None: syncs.append((tid, stars)) or True)
        writes = []
        monkeypatch.setattr(
            tag_file_service, "write_rating_to_file",
            lambda path, stars: writes.append((path, stars)) or True,
        )
        logged = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(msg))

        results = _album_results()
        outcome = fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={"sync_navidrome": True},
        )

        # t1 (4★, changed) synced + file rewritten; t2 (5★, unchanged) skipped.
        assert outcome["navidrome_synced"] == 1
        assert [t["stars"] for t in results] == [4, 5]
        assert syncs == [("t1", 4)]
        assert len(writes) == 1
        assert writes[0][1] == 4
        assert "skipped 1 unchanged rating(s)" in " ".join(logged)

        # The batch rating/path load replaced the per-track file-path lookup.
        batch_loads = [e for e in session.executed if "SELECT id, stars, file_path" in str(e[0])]
        assert len(batch_loads) == 1

    def test_changed_ratings_still_rewrite_and_sync(self, monkeypatch, tmp_path):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine
        import services.metadata.tag_file_service as tag_file_service

        session = _RecordingSession([
            [],                                  # album DB score merge
            [],                                  # album scaling model
            self._rows(tmp_path),                # batch rating/path load
        ])
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))
        # Both tracks get a DIFFERENT rating than stored (stored 5★).
        monkeypatch.setattr(fs, "_assign_stars", lambda track, *args, **kwargs: 3)
        syncs = []
        monkeypatch.setattr(fs, "_sync_rating_to_navidrome", lambda tid, stars, clients=None: syncs.append((tid, stars)) or True)
        writes = []
        monkeypatch.setattr(
            tag_file_service, "write_rating_to_file",
            lambda path, stars: writes.append((path, stars)) or True,
        )
        logged = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(msg))

        results = _album_results()
        outcome = fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={"sync_navidrome": True},
        )

        assert outcome["navidrome_synced"] == 2
        assert len(writes) == 2
        assert len(syncs) == 2


class TestPostAlbumStarRatingsResilience:
    def test_one_track_failure_does_not_abort_album(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine

        session = _RecordingSession()
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))

        def flaky_assign(track, *args, **kwargs):
            if track["title"] == "Time Is Running Out":
                raise RuntimeError("boom")
            return 4

        monkeypatch.setattr(fs, "_assign_stars", flaky_assign)
        monkeypatch.setattr(fs, "_sync_rating_to_navidrome", lambda *a, **k: True)
        logged = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(msg))

        results = _album_results()
        outcome = fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={"sync_navidrome": True},
        )

        # The failing track is skipped; the healthy one still gets rated.
        assert outcome["star_ratings"] == 1
        assert results[0]["stars"] == 4
        assert results[1].get("stars") is None
        star_updates = [e for e in session.executed if "UPDATE tracks SET stars" in str(e[0])]
        assert len(star_updates) == 1
        assert any("📊 SCAN RESULTS: Muse — Absolution" in m for m in logged)


class TestUpsertTracksBulk:
    """``upsert_tracks_bulk`` persists a whole batch in ONE session + commit.

    The scan workers used to open a fresh ``db_session`` (one transaction) per
    track — tens of thousands of transactions for a full library scan.  The
    bulk path runs the whole album through a single session.
    """

    class _CountingSession:
        def __init__(self):
            self.executed = []
            self.commit_count = 0

        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            return _FakeResult([])

        def commit(self):
            self.commit_count += 1

        def rollback(self):
            pass

    def test_bulk_uses_one_session_for_many_payloads(self, monkeypatch):
        from db.repositories import popularity_repository as pr

        sessions = []

        @contextmanager
        def _fake_db_session():
            s = self._CountingSession()
            sessions.append(s)
            yield s

        monkeypatch.setattr(pr, "db_session", _fake_db_session)
        saved = []
        monkeypatch.setattr(
            pr, "_execute_save",
            lambda session, payload: saved.append(payload) or True,
        )

        payloads = [
            {"id": "t1", "title": "One"},
            {"id": "t2", "title": "Two"},
            {"id": "t3", "title": "Three"},
        ]
        ok = pr.upsert_tracks_bulk(payloads)

        assert ok is True
        # ONE session + commit for the whole album, not one per track.
        assert len(sessions) == 1
        assert len(saved) == 3
        assert [p["id"] for p in saved] == ["t1", "t2", "t3"]

    def test_bulk_empty_returns_false_without_session(self, monkeypatch):
        from db.repositories import popularity_repository as pr

        sessions = []

        @contextmanager
        def _fake_db_session():
            s = self._CountingSession()
            sessions.append(s)
            yield s

        monkeypatch.setattr(pr, "db_session", _fake_db_session)
        assert pr.upsert_tracks_bulk([]) is False
        assert len(sessions) == 0

    def test_bulk_skips_bad_rows_without_aborting_batch(self, monkeypatch):
        from db.repositories import popularity_repository as pr

        @contextmanager
        def _fake_db_session():
            yield self._CountingSession()

        monkeypatch.setattr(pr, "db_session", _fake_db_session)
        saved = []

        def flaky_save(session, payload):
            if payload.get("id") == "bad":
                raise ValueError("missing id")
            saved.append(payload)
            return True

        monkeypatch.setattr(pr, "_execute_save", flaky_save)

        ok = pr.upsert_tracks_bulk([
            {"id": "good1"}, {"id": "bad"}, {"id": "good2"},
        ])
        assert ok is False
        assert [p["id"] for p in saved] == ["good1", "good2"]


class TestDeferredPersistSink:
    def test_drain_returns_and_clears(self):
        from db.repositories.tracks import DeferredPersistSink

        sink = DeferredPersistSink()
        sink.add({"id": "a"})
        sink.add({"id": "b"})
        assert sink.drain() == [{"id": "a"}, {"id": "b"}]
        # A second drain is empty — the runner never double-persists an album.
        assert sink.drain() == []


class TestDeferredPersistBatching:
    """``process_track`` defers its upsert into the runner's batch sink when
    ``options["_deferred_persist"]`` is present, and persists inline otherwise.

    The scan runner uses the deferred path so every album's track writes flush
    in one ``upsert_tracks_bulk`` call instead of one transaction per track.
    """

    def _run(self, monkeypatch, options):
        import services.popularity.stages.track_stage as ts

        persist_calls = []
        monkeypatch.setattr(ts, "LastFmClient", lambda *a, **k: object())
        monkeypatch.setattr(ts, "ListenBrainzClient", lambda *a, **k: object())
        monkeypatch.setattr(ts, "get_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_aggregated_listenbrainz_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_search_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(
            ts, "insert_or_update_track",
            lambda tid, data: persist_calls.append((tid, data)),
        )
        monkeypatch.setattr(
            ts, "detect_single_for_track",
            lambda **kw: {"is_single": False, "confidence": "low",
                          "confidence_score": 0.0, "single_status": "none",
                          "sources": [], "reasons": []},
        )

        track = {
            "id": "t1",
            "artist": "Muse",
            "album": "Absolution",
            "title": "Hysteria",
            "final_score": 50.0,
            "popularity": 50.0,
            "lastfm_listeners": 1000,
            "listenbrainz_listens": 2000,
            "lb_percentile": 0.9,
            "single_detection_last_updated": None,
        }
        result = ts.process_track(
            track=track,
            track_context={"artist": "Muse", "album": "Absolution"},
            album_context={"album": "Absolution", "artist": "Muse", "tracks": [track]},
            album_result={"detected_album_type": "album", "is_heterogeneous": False},
            options=options,
        )
        return result, persist_calls

    def test_with_sink_defers_and_skips_inline_persist(self, monkeypatch):
        from db.repositories.tracks import DeferredPersistSink

        sink = DeferredPersistSink()
        result, persist_calls = self._run(
            monkeypatch,
            {"singles_detection_only": True, "_deferred_persist": sink},
        )

        # No inline DB write — the payload went to the batch sink instead.
        assert persist_calls == []
        drained = sink.drain()
        assert len(drained) == 1
        assert drained[0]["id"] == "t1"
        assert result is not None

    def test_without_sink_persists_inline(self, monkeypatch):
        result, persist_calls = self._run(
            monkeypatch,
            {"singles_detection_only": True},
        )

        assert result is not None
        assert len(persist_calls) == 1
        assert persist_calls[0][0] == "t1"


class TestNavidromeClientReusePerAlbum:
    """The album-level rating sync builds its Navidrome clients ONCE and reuses
    them across every track, instead of reloading config + reconstructing a
    client per track per user."""

    def test_clients_built_once_and_passed_to_every_sync(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine

        session = _RecordingSession()
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))
        monkeypatch.setattr(
            fs, "_assign_stars",
            lambda track, *args, **kwargs: 4 if track["title"] == "Apocalypse Please" else 5
        )

        builds = []
        monkeypatch.setattr(
            fs, "_build_rating_sync_clients",
            lambda: builds.append(True) or ["navidrome-user-1", "navidrome-user-2"],
        )
        syncs = []
        monkeypatch.setattr(
            fs, "_sync_rating_to_navidrome",
            lambda tid, stars, clients=None: syncs.append((tid, stars, clients)) or True,
        )
        logged = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(msg))

        results = _album_results()
        outcome = fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={"sync_navidrome": True},
        )

        assert outcome["navidrome_synced"] == 2
        # Clients built ONCE for the whole album, not once per track.
        assert len(builds) == 1
        assert len(syncs) == 2
        assert syncs[0][1] == 4
        assert syncs[1][1] == 5
        # Both tracks reuse the SAME pre-built client list.
        assert syncs[0][2] == ["navidrome-user-1", "navidrome-user-2"]
        assert syncs[1][2] == ["navidrome-user-1", "navidrome-user-2"]

    def test_no_clients_built_when_everything_is_skipped(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import db.engine as db_engine

        session = _RecordingSession([
            [],                                  # album DB score merge
            [],                                  # album scaling model
            [_Row({"id": "t1", "stars": 4, "file_path": "/x/t1.mp3"}),
             _Row({"id": "t2", "stars": 5, "file_path": "/x/t2.mp3"})],  # stored ratings
        ])
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))
        monkeypatch.setattr(
            fs, "_assign_stars",
            lambda track, *args, **kwargs: 4 if track["title"] == "Apocalypse Please" else 5
        )
        builds = []
        monkeypatch.setattr(fs, "_build_rating_sync_clients", lambda: builds.append(True) or [])
        monkeypatch.setattr(fs, "_sync_rating_to_navidrome", lambda *a, **k: True)
        logged = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(msg))

        results = _album_results()
        outcome = fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={"sync_navidrome": True},
        )

        # Both ratings are unchanged (stored) → skipped → no client build.
        assert outcome["navidrome_synced"] == 0
        assert builds == []
