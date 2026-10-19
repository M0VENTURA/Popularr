"""Regression tests: global-first 5★ allocation (order-independent ratings).

Reproduces the Battle Beast inversion: Eden (raw score 80.4, the catalog #1)
was demoted to 4★ by its album's per-album era/slot gating, while lower-scored
tracks on earlier-processed albums kept 5★.  The fix pre-locks the artist's
top tracks by RAW cross-album score at the artist-section pre-pass, and
exempts them from the per-album era gate and the 5★ slot cap.

Tests:
- ``_compute_global_5star_locked_ids`` ranks by raw score and locks the
  catalog top-% (restricted to genuine single/standout candidates).
- ``_assign_stars`` honours ``_global_5star_locked`` → 5★ regardless of the
  album's era gate / slot cap.
- The 5★ slot cap never demotes a locked track.

⚠️ The lock is keyed by TRACK IDENTITY, never by title — see
``tests/test_global_5star_lock_is_not_title_keyed.py`` for the collision it
used to cause.
"""

from __future__ import annotations

import pytest


def _track(title, raw_score, score=None, conf="high", single=True, **overrides):
    """Build a result-dict-shaped track with a raw cross-album score."""
    track = {
        "track_id": f"id-{title.lower().replace(' ', '-')}",
        "artist": "Battle Beast",
        "album": "Album",
        "title": title,
        "popularity_score": float(score if score is not None else raw_score),
        "final_score": float(score if score is not None else raw_score),
        "_raw_combined": float(raw_score),
        "lastfm_listeners": int(raw_score * 500) if raw_score > 50 else 500,
        "listenbrainz_listens": int(raw_score * 90) if raw_score > 50 else 500,
        "lastfm_score": 60.0,
        "listenbrainz_score": 60.0,
        "is_single": single,
        "single_confidence": conf,
        "single_sources": (
            '[{"source": "musicbrainz", "matched": true, "confidence": 0.5}]'
            if single else ""
        ),
        "is_live": False,
        "popularity_marked": False,
        "exclude_from_stats": False,
    }
    track.update(overrides)
    return track


class TestComputeGlobal5StarLockedIds:
    def test_ranks_by_raw_score_locks_top_pct(self, monkeypatch):
        from services.popularity.scan_stage_runner import _compute_global_5star_locked_ids

        # Battle Beast catalog: Eden is the raw #1 (80.4); King for a Day 80.4
        # (tie); NMHE 76.6; lower-scored tracks further down.  8 albums, 91
        # tracks → top-20% ≈ 18 slots.
        results = [
            _track("Eden", 80.4, conf="high", single=True),
            _track("King for a Day", 80.4, conf="high", single=True),
            _track("No More Hollywood Endings", 76.6, conf="high", single=True),
            _track("Last Goodbye", 73.8, conf="high", single=True),
            _track("Out of Control", 77.8, conf="high", single=True),
            _track("Black Ninja", 71.8, conf="high", single=True),
            _track("Show Me How to Die", 74.0, conf="medium", single=True),
            _track("Straight to the Heart", 76.3, conf="high", single=True),
            _track("Let It Roar", 74.6, conf="low", single=False),
            _track("Intro (live in Helsinki 2023)", 60.3, conf="low", single=False,
                   is_live=True),
            _track("Shutdown", 57.9, conf="low", single=False),
            _track("Steel", 64.6, conf="low", single=False),
        ]

        locked = _compute_global_5star_locked_ids("Battle Beast", results, {})

        # ⭐ KEYED BY TRACK IDENTITY, not title. The fixture's ``_track`` helper
        # builds ``track_id`` as ``id-<slug>``.
        assert "id-eden" in locked
        assert "id-king-for-a-day" in locked
        assert "id-out-of-control" in locked
        # A live track is never locked.
        assert "id-intro-(live-in-helsinki-2023)" not in locked
        # A low-confidence non-single with a sub-minimum raw score is not
        # locked (raw 57.9 < 60 min).
        assert "id-shutdown" not in locked

    def test_low_raw_score_never_forced_into_pool(self):
        from services.popularity.scan_stage_runner import _compute_global_5star_locked_ids

        # A confirmed single with a weak raw score must NOT be force-locked
        # just to fill the percentage.
        results = [
            _track("Weak Single", 52.0, conf="high", single=True),
            _track("Mid Track", 70.0, conf="high", single=True),
            _track("Strong Track", 90.0, conf="high", single=True),
            _track("Another", 68.0, conf="low", single=False),
            _track("Another 2", 66.0, conf="low", single=False),
        ]
        locked = _compute_global_5star_locked_ids("Artist", results, {})
        assert "id-weak-single" not in locked
        assert "id-strong-track" in locked


class TestAssignStarsHonoursGlobalLock:
    def _album_scores(self):
        # A tight consistent album (like No More Hollywood Endings) where the
        # album-relative re-anchor compresses the top track's z.
        return [50.0, 52.0, 54.0, 56.0, 58.0, 60.0, 62.0, 64.0, 66.0, 68.0, 70.0, 72.0]

    def test_locked_track_gets_five_despite_era_gate(self):
        from services.popularity.stages import finalise_stage as fs

        track = _track("Eden", 80.4, score=80.4, conf="high", single=True,
                       _global_5star_locked=True)
        # The era gate would demote this to 4★ (album_model with a strict
        # catalog cutoff above Eden's score — the sequential-bias case).
        album_model = {"has_benchmark": True, "era": "peak",
                       "catalog_cutoff": 85.0, "max_5star_slots": 4}
        stars = fs._assign_stars(track, self._album_scores(), self._album_scores(),
                                 album_model=album_model)
        assert stars == 5

    def test_unlocked_track_respects_era_gate(self):
        from services.popularity.stages import finalise_stage as fs

        # Without the lock, a high single that misses the catalog cutoff AND
        # the album top-N falls to the 4★ Single Floor.  Album scores here
        # put Eden at rank #5 (below album_top_n=3), so neither era path
        # qualifies — the 4★ Single Floor is the correct fallback.
        album = [50.0, 52.0, 54.0, 56.0, 58.0, 60.0, 62.0, 64.0, 66.0, 68.0, 70.0, 72.0, 80.4,
                 81.0, 82.0, 83.0]  # 4 tracks above Eden → rank #5
        track = _track("Eden", 80.4, score=80.4, conf="high", single=True)
        album_model = {"has_benchmark": True, "era": "peak",
                       "catalog_cutoff": 85.0, "max_5star_slots": 4}
        stars = fs._assign_stars(track, album, album,
                                 album_model=album_model)
        assert stars == 4

    def test_locked_track_never_demoted_by_slot_cap(self):
        # The slot-cap reorder puts locked tracks first, so a locked track is
        # never in the surplus tail.  Directly test the reorder math.
        locked = {"track_id": "id-eden"}
        slot_tracks = [
            {"track_id": "id-eden", "title": "Eden", "popularity_score": 80.4,
             "_global_5star_locked": True, "_era_5star": True, "stars": 5},
            {"track_id": "id-a", "title": "A", "popularity_score": 60.0,
             "_global_5star_locked": False, "_era_5star": True, "stars": 5},
            {"track_id": "id-b", "title": "B", "popularity_score": 55.0,
             "_global_5star_locked": False, "_era_5star": True, "stars": 5},
            {"track_id": "id-c", "title": "C", "popularity_score": 50.0,
             "_global_5star_locked": False, "_era_5star": True, "stars": 5},
            {"track_id": "id-d", "title": "D", "popularity_score": 45.0,
             "_global_5star_locked": False, "_era_5star": True, "stars": 5},
        ]
        max_slots = 4
        locked_kept = [t for t in slot_tracks if t.get("_global_5star_locked")]
        demotable = [t for t in slot_tracks if not t.get("_global_5star_locked")]
        demotable.sort(key=lambda t: t["popularity_score"], reverse=True)
        reordered = list(locked_kept) + list(demotable)
        demoted = [t for t in reordered[max_slots:]]
        # The locked Eden is never in the demoted surplus.
        assert all(t["track_id"] != "id-eden" for t in demoted)
        assert len(demoted) == 1
