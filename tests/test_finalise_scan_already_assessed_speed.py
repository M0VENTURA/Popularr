"""Speed of a Finalise pass over an album that was already scanned.

REPORTED
--------
> I started this scan after running one already, so it should have moved past
> it fairly quick, but still took almost 4 minutes to progress one album that
> had all recent scan data using a finalize scan.

Timings from the attached log (Finalise pass, 11 tracks, every track mature
and frozen):

    18:48:53  [POPULARITY] Album 1/7 (singles): … (11 tracks)
    18:49:38  first [TRACK] line          -> ~45s with no output at all
    18:51:08  album_track_phase 89.2s     -> 7 tracks re-ran singles detection
    18:51:51  Album file tags synced      -> 40s of tag work

Three defects behind those windows, each pinned here.

1. ``_singles_popularity_due`` (was an inline block) asked ``was_album_scanned``
   TWICE for a finalise pass and then threw the answer away — the
   ``if _mode_finalise`` branch immediately after forced ``_pop_due = False``.
   Those two lookups are what sat between the album header and the first track.
2. ``was_album_scanned`` could not use ``idx_scan_history_scope``: its
   artist/album predicates are ``LOWER(COALESCE(...))`` expressions, so the
   index only served the leading ``scan_type`` and every row of that scan type
   had to be read. It runs three times per album (skip check + those two).
3. ``singles_detection_is_fresh`` (was inline in ``track_stage``) demanded
   POSITIVE evidence — ``is_single`` or a matched source — so the commonest
   verdict, "not a single", was never cacheable and every pass re-ran Discogs +
   MusicBrainz for those tracks. In the log 7 of 11 tracks did exactly that,
   30-58s each.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# 1. A finalise pass must not ask a question it immediately discards
# ---------------------------------------------------------------------------
class TestFinaliseNeverLooksUpADiscardedAnswer:
    @pytest.fixture(autouse=True)
    def _count_history_lookups(self, monkeypatch):
        from services.popularity import scan_stage_runner as runner

        calls: list[tuple] = []

        def _spy(artist, album, scan_type, days=7):
            calls.append((artist, album, scan_type, days))
            return False

        monkeypatch.setattr(runner, "was_album_scanned", _spy)
        self.calls = calls
        return calls

    def test_finalise_answers_without_touching_scan_history(self):
        from services.popularity import scan_stage_runner as runner

        due = runner._singles_popularity_due(
            artist="Birds of Tokyo",
            album="Birds of Tokyo",
            album_is_old=True,
            finalise=True,
        )

        assert due is False, "finalise must never window-refresh a stored score"
        assert self.calls == [], (
            "the lookup ran anyway — its answer is discarded two lines later, "
            "so the work is pure cost (~45s per album in the reported log)"
        )

    def test_a_singles_pass_still_consults_the_history(self):
        """CONTROL — the guard must not switch the real decision off."""
        from services.popularity import scan_stage_runner as runner

        due = runner._singles_popularity_due(
            artist="Birds of Tokyo",
            album="Birds of Tokyo",
            album_is_old=False,
            finalise=False,
        )

        assert len(self.calls) >= 1, "a non-finalise singles pass must still ask"
        # Not recently scored (the spy answers False) -> popularity IS due.
        assert due is True

    def test_window_zero_short_circuits_before_any_lookup(self, monkeypatch):
        """popularity_skip_days = 0 means "always rescan" — no history needed."""
        from services.popularity import scan_stage_runner as runner

        monkeypatch.setattr(
            runner,
            "get_feature",
            lambda key, default=None: 0 if key == "popularity_skip_days" else default,
        )

        due = runner._singles_popularity_due(
            artist="A",
            album="B",
            album_is_old=False,
            finalise=False,
        )

        assert due is True
        assert self.calls == []


# ---------------------------------------------------------------------------
# 2. was_album_scanned must be index-served
# ---------------------------------------------------------------------------
@pytest.fixture
def scan_history_table(app):
    """Create ``scan_history`` on the shared in-memory engine.

    Depends on ``app`` because importing the app DISPOSES the engine — a table
    created before that import is thrown away. ``CREATE … IF NOT EXISTS`` keeps
    it idempotent, and SQLite accepts the PostgreSQL ``BIGSERIAL`` type name
    (it is parsed as a type name with numeric affinity, so it never runs).
    """
    from db.engine import get_engine
    from db.schema import TABLES_TO_ENSURE
    from sqlalchemy import text

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(TABLES_TO_ENSURE["scan_history"]))
    return True


def _seed_completed(
    *,
    scan_type: str,
    artist: str,
    album: str,
    started_at: datetime,
) -> None:
    """Insert one completed row directly.

    ``record_scan`` is deliberately NOT used: its completion UPDATE carries
    ``EXTRACT(EPOCH FROM …)``, which only parses on PostgreSQL, so on the
    SQLite test engine the write is swallowed by its own handler.
    """
    from db.engine import get_engine
    from sqlalchemy import text

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO scan_history "
                "(scan_type, artist, album, status, started_at, completed_at) "
                "VALUES (:t, :a, :al, 'completed', :s, :s)"
            ),
            {"t": scan_type, "a": artist, "al": album, "s": started_at},
        )


class TestWasAlbumScanned:
    def test_an_exact_recent_match_is_enough(self, scan_history_table):
        from services.scanning.scan_history_service import was_album_scanned

        _seed_completed(
            scan_type="singles",
            artist="Exact Artist",
            album="Exact Album",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )

        assert was_album_scanned("Exact Artist", "Exact Album", "singles", 7) is True

    def test_casing_still_falls_back_to_the_tolerant_query(self, scan_history_table):
        """CONTROL — the fast path must not make the lookup case-sensitive."""
        from services.scanning.scan_history_service import was_album_scanned

        _seed_completed(
            scan_type="singles",
            artist="Radiohead",
            album="OK Computer",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )

        assert was_album_scanned("radiohead", "ok computer", "singles", 7) is True

    def test_an_unscanned_album_is_not_reported(self, scan_history_table):
        from services.scanning.scan_history_service import was_album_scanned

        assert was_album_scanned("Nobody", "Nothing", "singles", 7) is False

    def test_a_row_outside_the_window_is_ignored(self, scan_history_table):
        from services.scanning.scan_history_service import was_album_scanned

        _seed_completed(
            scan_type="singles",
            artist="Old Artist",
            album="Old Album",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=40),
        )

        assert was_album_scanned("Old Artist", "Old Album", "singles", 7) is False
        assert was_album_scanned("Old Artist", "Old Album", "singles", 100) is True

    def test_another_scan_type_does_not_count(self, scan_history_table):
        """The finalise window reads ``singles`` — a popularity row must not satisfy it."""
        from services.scanning.scan_history_service import was_album_scanned

        _seed_completed(
            scan_type="popularity",
            artist="Type Artist",
            album="Type Album",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )

        assert was_album_scanned("Type Artist", "Type Album", "popularity", 7) is True
        assert was_album_scanned("Type Artist", "Type Album", "singles", 7) is False

    def test_zero_days_never_matches(self, scan_history_table):
        """``0`` is the documented "always rescan" value and short-circuits."""
        from services.scanning.scan_history_service import was_album_scanned

        assert was_album_scanned("Anyone", "Anything", "singles", 0) is False

    def test_the_index_served_query_is_the_one_that_runs_first(self):
        """The exact statement must lead, or the planner never gets its chance."""
        from services.scanning import scan_history_service as svc
        import inspect

        source = inspect.getsource(svc.was_album_scanned)

        exact = source.index("AND artist = :artist")
        tolerant = source.index("LOWER(COALESCE(artist, ''))")
        assert exact < tolerant, (
            "the tolerant (expression-predicate) query ran first — it cannot "
            "use idx_scan_history_scope, so every album pays a table scan"
        )

        # Scope to the SQL itself: this function's own docstring QUOTES the
        # retired ``NOW() - INTERVAL`` form to explain why it went away, so a
        # bare "INTERVAL not in source" assertion fails against its own comment.
        import re

        sql = " ".join(re.findall(r'text\("""(.*?)"""\)', source, flags=re.S))
        assert "INTERVAL" not in sql, (
            "the PostgreSQL-only cutoff is back; it made this helper "
            "untestable on the SQLite test engine"
        )
        assert ":cutoff" in sql


# ---------------------------------------------------------------------------
# 3. A negative singles verdict must be cacheable
# ---------------------------------------------------------------------------
class TestNegativeSinglesVerdictsAreCacheable:
    @staticmethod
    def _track(**extra):
        track = {
            "year": "2010",
            # Not a single, no matched source — the case that used to re-run.
            "is_single": False,
            "single_confidence": "low",
            "single_sources": "[]",
        }
        track.update(extra)
        return track

    def test_a_fresh_not_a_single_verdict_is_reused(self):
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        now = datetime.now(timezone.utc)
        track = self._track(single_detection_last_updated=now - timedelta(days=2))

        assert singles_detection_is_fresh(track, now=now) is True, (
            "the verdict \"not a single\" was treated as no verdict at all, so "
            "Discogs + MusicBrainz ran again for it on EVERY pass"
        )

    def test_no_timestamp_is_not_fresh(self):
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        assert singles_detection_is_fresh(self._track()) is False

    def test_an_expired_timestamp_is_not_fresh(self):
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        now = datetime.now(timezone.utc)
        # 2010 release -> get_cache_duration_hours returns 168h (7 days).
        track = self._track(single_detection_last_updated=now - timedelta(days=8))

        assert singles_detection_is_fresh(track, now=now) is False

    def test_a_mature_track_still_gets_the_long_ttl(self):
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        now = datetime.now(timezone.utc)
        six_days_ago = now - timedelta(days=6)

        mature = self._track(single_detection_last_updated=six_days_ago)
        recent = self._track(year="2026", single_detection_last_updated=six_days_ago)

        assert singles_detection_is_fresh(mature, now=now) is True, "168h TTL"
        assert singles_detection_is_fresh(recent, now=now) is False, "24h TTL"

    def test_a_timestamp_is_read_from_a_string(self):
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        now = datetime.now(timezone.utc)
        track = self._track(
            single_detection_last_updated=(now - timedelta(days=1)).isoformat()
        )

        assert singles_detection_is_fresh(track, now=now) is True

    def test_an_unparsable_timestamp_is_not_fresh(self):
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        track = self._track(single_detection_last_updated="not-a-date")

        assert singles_detection_is_fresh(track) is False

    def test_a_positive_verdict_is_still_reused(self):
        """CONTROL — the old fast path must keep working."""
        from services.popularity.popularity_cache_policy import (
            singles_detection_is_fresh,
        )

        now = datetime.now(timezone.utc)
        track = self._track(
            is_single=True,
            single_confidence="high",
            single_detection_last_updated=now - timedelta(hours=1),
        )

        assert singles_detection_is_fresh(track, now=now) is True


# ---------------------------------------------------------------------------
# Wiring: the runner must actually use these helpers
# ---------------------------------------------------------------------------
class TestTheWiring:
    def test_track_stage_delegates_to_the_shared_helper(self):
        from pathlib import Path
        from services.popularity.stages import track_stage

        source = Path(track_stage.__file__).read_text(encoding="utf-8")

        assert "singles_detection_is_fresh(track)" in source, (
            "track_stage still decides freshness inline"
        )
        # The evidence gate is what made "no" uncachable. It must be gone from
        # the CODE — the prose above it may still describe the old behaviour.
        code_only = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        assert "_sd_has_evidence" not in code_only, (
            "the positive-evidence requirement is back"
        )

    def test_the_runner_uses_the_extracted_decision(self):
        import inspect
        from services.popularity import scan_stage_runner as runner

        source = inspect.getsource(runner)

        assert "_singles_popularity_due(" in source
        assert "finalise=_mode_finalise," in source, (
            "the finalise flag is no longer passed to the decision"
        )
        # The old shape: compute, then unconditionally discard for finalise.
        assert "_pop_due = not _pop_scored_recently" not in source
