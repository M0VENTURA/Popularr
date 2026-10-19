"""The global 5★ lock is keyed by track IDENTITY, never by title.

⚠️ THE BUG.

``_compute_global_5star_locked_titles`` returned a set of **lowercased titles**,
and the lock was applied by comparing titles:

    if str(_tr.get("title") or "").strip().lower() in _locked_set:
        _tr["_global_5star_locked"] = True

So a title in the set matched **every** track that shared it — including a
different recording on another disc. The lock is an artist-wide verdict about a
track's OWN popularity, so lending it to a second, weaker recording inverts the
rating the pre-pass exists to protect.

Reported on Candlemass — ``Dactylis Glomerata``. That edition's Disc 2 is
``Abstrakt Algebra II``, whose tracks share titles with Disc 1. The live log
printed the tell-tale pair:

    'Dustflow' → GLOBAL 5★ LOCKED (raw 69.7, catalog top)
    'Dustflow' → GLOBAL 5★ LOCKED (raw 40.2, catalog top)

Two different raw scores for one title, from inside the same matching loop. The
second is **below** ``global_5star_min_raw_score`` (60.0), so it could only have
been locked by matching the first track's title — not on its own merit.

⚠️ THIS EXACT CLASS OF BUG WAS ALREADY SOLVED TWICE IN THIS CODEBASE, one layer
down:

* ``popularity_sources.track_identity_key`` — "A normalised TITLE cannot
  identify a row: dArtagnan's *Helden X Hymnen* files both the album's title
  track AND its *(Unplugged Version)* rendition under the same title."
* ``album_recording_batch_key`` — "``<artist>::<title>`` is the legacy
  title-only form; appending ``::<disc>::<track>`` makes the key ROW-specific,
  which is what keeps two same-titled rows of one album apart."

Those two give the per-album batch and the single-detection pass row identity.
The 5★ lock never got the same treatment, and this module pins it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "services" / "popularity" / "scan_stage_runner.py"


def _track(title, raw_score, *, track_id=None, disc=None, **overrides):
    """An album-RESULT-shaped track (the shape the lock consumes).

    ``track_id`` is the canonical identity here: the star pipeline and the
    Navidrome rating sync both read ``track.get("track_id")``.
    """
    track = {
        "track_id": track_id if track_id is not None else f"id-{title.lower()}",
        "artist": "Candlemass",
        "album": "Dactylis Glomerata",
        "title": title,
        "popularity_score": float(raw_score),
        "final_score": float(raw_score),
        "_raw_combined": float(raw_score),
        "lastfm_listeners": int(raw_score * 500) if raw_score > 50 else 500,
        "listenbrainz_listens": int(raw_score * 90) if raw_score > 50 else 500,
        "is_single": True,
        "single_confidence": "high",
        "single_sources": '[{"source": "musicbrainz", "matched": true}]',
        "is_live": False,
        "popularity_marked": False,
        "exclude_from_stats": False,
    }
    if disc is not None:
        track["disc_number"] = str(disc)
    track.update(overrides)
    return track


# ---------------------------------------------------------------------------
# The collision itself
# ---------------------------------------------------------------------------


class TestSameTitleOnAnotherDiscIsNotLocked:
    def test_a_weaker_same_titled_track_does_not_inherit_the_lock(self):
        """THE REGRESSION — the Dactylis Glomerata case."""
        from services.popularity.scan_stage_runner import _compute_global_5star_locked_ids

        results = [
            # Disc 1: the real hit, above the 60.0 raw floor.
            _track("Dustflow", 69.7, track_id="disc1-dustflow", disc=1),
            # Disc 2 (Abstrakt Algebra II): SAME title, weakest in the band's
            # catalogue, well BELOW the floor.
            _track("Dustflow", 40.2, track_id="disc2-dustflow", disc=2),
            # A couple of genuinely mid tracks so the top-% slice is not just
            # the one row (n is derived from len(all_results)).
            _track("Molotov", 60.3, track_id="disc1-molotov", disc=1),
            _track("Apathy", 61.9, track_id="disc1-apathy", disc=1),
            _track("Abstrakt Sun", 39.5, track_id="disc2-abstrakt-sun", disc=2),
            _track("Thirst", 38.1, track_id="disc2-thirst", disc=2),
        ]

        locked = _compute_global_5star_locked_ids("Candlemass", results, {})

        assert "disc1-dustflow" in locked, "the above-floor hit must be locked"
        assert "disc2-dustflow" not in locked, (
            "the same-titled Disc 2 track was locked on the Disc 1 track's "
            "merit — this is the title-keyed bug, and it awards 5★ to a "
            "recording whose own raw score is BELOW the minimum"
        )

    def test_every_locked_member_clears_the_raw_floor_on_its_own(self):
        """The invariant the title key broke: a lock must be earned individually."""
        from services.popularity import scan_stage_runner as runner

        _sd_keys = ("global_5star_catalog_top_pct", "global_5star_min_raw_score")
        results = [
            _track("Dustflow", 69.7, track_id="d1-a", disc=1),
            _track("Dustflow", 40.2, track_id="d2-a", disc=2),
            _track("Molotov", 60.3, track_id="d1-b", disc=1),
            _track("Abstrakt Sun", 39.5, track_id="d2-b", disc=2),
            _track("Thirst", 38.1, track_id="d2-c", disc=2),
            _track("Cylinder", 22.0, track_id="d2-d", disc=2),
        ]

        locked = runner._compute_global_5star_locked_ids("Candlemass", results, {})

        _by_id = {t["track_id"]: t for t in results}
        for ident in locked:
            assert float(_by_id[ident]["_raw_combined"]) >= 60.0, (
                f"locked {ident} with raw {_by_id[ident]['_raw_combined']} — "
                f"below the configured floor ({_sd_keys[1]}), so it cannot have "
                f"qualified on its own score"
            )

    def test_the_set_holds_identities_not_titles(self):
        """A title must never appear, or the collision silently returns."""
        from services.popularity.scan_stage_runner import _compute_global_5star_locked_ids

        # ⚠️ ``n`` is ``ceil(len(all_results) * top_pct)`` (20%), so the
        # fixtures must not outrank the row under test or the slice excludes it
        # and the assertion measures the fixture instead of the fix.
        results = [_track("Dustflow", 99.0, track_id="d1", disc=1)] + [
            _track("Filler", 55.0 + i, track_id=f"f{i}") for i in range(5)
        ]

        locked = _compute_global_5star_locked_ids("Candlemass", results, {})

        assert "dustflow" not in locked, (
            "a bare lowercased title is in the lock set — that is exactly the "
            "key that made the lock contagious"
        )
        assert "d1" in locked


# ---------------------------------------------------------------------------
# _lock_identity
# ---------------------------------------------------------------------------


class TestLockIdentity:
    def test_prefers_track_id(self):
        from services.popularity.scan_stage_runner import _lock_identity

        assert _lock_identity({"track_id": "abc", "id": "xyz", "title": "T"}) == "abc"

    def test_falls_back_to_id(self):
        """Track-stage inputs carry the DB column name instead of ``track_id``."""
        from services.popularity.scan_stage_runner import _lock_identity

        assert _lock_identity({"id": "xyz", "title": "T"}) == "xyz"

    def test_idless_track_falls_back_to_a_namespaced_title(self):
        """Degrade to a title key, but never one that can collide with an id."""
        from services.popularity.scan_stage_runner import _lock_identity

        assert _lock_identity({"title": "Dustflow"}) == "title:dustflow"

    def test_the_namespaced_fallback_cannot_equal_a_real_id(self):
        """``title:`` must keep the fallback out of the id keyspace."""
        from services.popularity.scan_stage_runner import _lock_identity

        assert _lock_identity({"title": "abc"}) != _lock_identity({"track_id": "abc"})

    def test_two_same_titled_idless_tracks_still_collide(self):
        """Documented, accepted degradation.

        Without an id there is nothing to tell two same-titled rows apart, so
        they share a key. This is why the fallback is a last resort and why the
        preferred path is an id — it preserves today's behaviour for a library
        that has none, rather than silently locking nothing.
        """
        from services.popularity.scan_stage_runner import _lock_identity

        a = _lock_identity({"title": "Dustflow"})
        b = _lock_identity({"title": "dustflow "})
        assert a == b

    def test_different_ids_never_collide(self):
        from services.popularity.scan_stage_runner import _lock_identity

        assert _lock_identity({"track_id": "d1", "title": "Dustflow"}) != _lock_identity(
            {"track_id": "d2", "title": "Dustflow"}
        )


# ---------------------------------------------------------------------------
# Structural: the application site must compare identities
# ---------------------------------------------------------------------------


def test_the_application_site_compares_identity():
    """Pin the one link that cannot be exercised without a whole artist run.

    ``run_scan`` is a large closure, so the lock's application is pinned at the
    source. The chain around it is covered directly: ``_compute_..._ids`` and
    ``_lock_identity`` by the tests above, and ``_assign_stars`` honouring
    ``_global_5star_locked`` by ``tests/test_global_first_5star_allocation.py``.
    """
    src = RUNNER.read_text(encoding="utf-8")

    assert "_lock_identity(_tr) in _locked_set" in src, (
        "the lock must be applied by track identity"
    )
    assert 'str(_tr.get("title") or "").strip().lower() in _locked_set' not in src, (
        "the title comparison is back — that is the bug"
    )


def test_the_old_title_keyed_api_is_gone():
    """A lingering alias would invite the next caller to use the wrong key."""
    src = RUNNER.read_text(encoding="utf-8")

    assert "_compute_global_5star_locked_titles" not in src
    assert "_artist_5star_locked_titles" not in src
    assert "_artist_5star_locked_ids" in src


def test_the_logged_raw_score_is_the_locked_tracks_own():
    """Belt and braces: the log must keep printing the track's own raw score.

    The reported log gave it away because it printed ONE title with TWO raw
    scores. Keeping the value tied to ``_tr`` is what makes a future regression
    equally visible instead of silent.
    """
    src = RUNNER.read_text(encoding="utf-8")

    assert "GLOBAL 5★ LOCKED (raw {float(_tr.get('_raw_combined') or 0):.1f}" in src
