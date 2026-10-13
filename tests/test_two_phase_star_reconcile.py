"""Two-phase star rating: durable per album, finalised at the artist boundary.

Reported
--------
> I've tried updating the details for Various Artists a few times and it fails
> mid way through. Would it be worth running through each album in full
> including the finalize pass before moving to the next? … An artist scan could
> do an extra pass at the end at the artist level.

What changed (design)
---------------------
Star ratings used to be assigned ONLY at the artist boundary
(``_flush_artist_star_ratings``), because two inputs need the artist's COMPLETE
album set: the global 5★ catalogue lock and the popularity-marking cutoffs. The
consequence was an artist scan interrupted mid-way (crash, budget stop) leaving
every completed album SCORED but UNRATED — the whole artist had to be re-run.

Now there are two phases, tracked by ``tracks.stars_reconciled``:

* **Phase 1 (per album)** — ``_post_album_stars(..., reconciled=False,
  provisional=True)`` runs the moment an album finishes: stars, file tags and
  Navidrome sync are written immediately (durability), the column records
  ``FALSE`` (provisional), and only a compact provisional line is logged (the
  SCAN RESULTS table would show a not-yet-reconciled ranking).
* **Phase 2 (artist boundary)** — the existing flush re-rates every pending
  album with the complete catalogue and writes ``TRUE`` (final). Its
  unchanged-rating skip makes it a write-nothing no-op for albums whose stars
  did not move.

The skip gate's new heal (``_album_needs_star_reconcile``) force-runs an album
whose stars are still provisional, so an interrupted artist converges on the
next scan instead of keeping partial-context ratings forever. An album with NO
stars is the older ``_album_scored_but_never_rated`` heal's job.

⚠️ Harness note: ``finalise_stage`` binds ``db_session`` at MODULE IMPORT, so
the fake must be patched onto ``finalise_stage.db_session`` — patching
``db.engine.db_session`` never reaches the binding.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Recording session (implements the whole contract post_album_star_ratings
# touches: execute + begin_nested savepoints).
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Nested:
    """Savepoint stand-in: the star UPDATE runs inside ``begin_nested()``."""

    def __init__(self, session):
        self._session = session

    @property
    def is_active(self):
        return True

    def commit(self):
        pass

    def rollback(self):
        pass


class _RecordingSession:
    def __init__(self):
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((str(sql), params))
        return _FakeResult()

    def begin_nested(self):
        return _Nested(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def _session_factory(session):
    @contextmanager
    def _cm():
        yield session

    return _cm


def _album_results():
    return [
        {"track_id": "t1", "title": "Map of the Problematique", "album": "Absolution",
         "popularity_score": 80.0, "final_score": 80.0},
        {"track_id": "t2", "title": "Apocalypse Please", "album": "Absolution",
         "popularity_score": 60.0, "final_score": 60.0},
    ]


def _star_updates(session):
    return [e for e in session.executed if "UPDATE tracks SET stars" in e[0]]


# ---------------------------------------------------------------------------
# 1. The column exists everywhere it must (registry / ORM / migration chain)
# ---------------------------------------------------------------------------


class TestTheColumnExistsInEveryLayer:
    def test_the_registry_declares_it(self) -> None:
        """The registry is what bootstraps EXISTING databases — a migration
        alone only helps a fresh chain."""
        src = (REPO_ROOT / "db" / "schema.py").read_text(encoding="utf-8")
        assert '"stars_reconciled": "BOOLEAN DEFAULT TRUE"' in src

    def test_the_orm_declares_it(self) -> None:
        src = (REPO_ROOT / "db" / "models.py").read_text(encoding="utf-8")
        assert "stars_reconciled: Mapped[bool | None]" in src

    def test_migration_023_chains_from_022(self) -> None:
        src = (REPO_ROOT / "migrations" / "versions" /
               "023_tracks_stars_reconciled.py").read_text(encoding="utf-8")
        assert '"022_tracks_recording_mbid_checked_and_file_tags_synced"' in src, (
            "023 must chain from the real head or alembic will refuse to upgrade"
        )
        assert '"stars_reconciled"' in src
        # The house pattern: inspector-guarded + dialect-portable.
        assert "sa.inspect" in src and "has_table" in src


# ---------------------------------------------------------------------------
# 2. The heal: a provisional album must be re-run, a final one must not
# ---------------------------------------------------------------------------


class TestTheReconcileHeal:
    @staticmethod
    def _needs(tracks):
        # Imported lazily so a reverted source FAILS rather than ERRORS.
        from services.popularity import scan_stage_runner as runner
        return runner._album_needs_star_reconcile(tracks)

    def test_a_provisional_album_needs_the_artist_pass(self) -> None:
        """THE reported gap: phase 1 now writes stars, so the old
        scored-but-unrated heal would never fire — this one must."""
        assert self._needs([
            {"stars": 4, "stars_reconciled": False},
            {"stars": 5, "stars_reconciled": False},
        ]) is True

    def test_a_reconciled_album_is_left_alone(self) -> None:
        assert self._needs([
            {"stars": 4, "stars_reconciled": True},
            {"stars": 5, "stars_reconciled": True},
        ]) is False

    def test_a_missing_value_counts_as_reconciled(self) -> None:
        """A row that predates the column (or a SELECT that omits it) must
        never be forced through — that would be an endless re-run."""
        assert self._needs([{"stars": 5}]) is False
        assert self._needs([{"stars": 5, "stars_reconciled": None}]) is False

    def test_an_unrated_album_is_the_other_heal_job(self) -> None:
        """No stars at all → ``_album_scored_but_never_rated`` owns it."""
        assert self._needs([
            {"stars": 0, "stars_reconciled": False},
        ]) is False

    def test_the_flag_check_is_not_an_identity_test(self) -> None:
        """`0 is False` is False in Python — the value must be tested by
        truthiness, not identity, or the heal silently never fires."""
        from services.popularity import scan_stage_runner as runner
        assert runner._stars_reconciled_is_false(False) is True
        assert runner._stars_reconciled_is_false(True) is False
        assert runner._stars_reconciled_is_false(None) is False

    def test_the_skip_gate_calls_it(self) -> None:
        src = (REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py").read_text(
            encoding="utf-8"
        )
        idx = src.index(
            'if skip_album and not options.get("metadata_only") and _album_needs_star_reconcile(tracks):'
        )
        # The heal must un-skip BEFORE the ``if skip_album: continue``.
        after = src[idx:]
        assert after.index("if skip_album:") < after.index("skipped_albums += 1")

    def test_the_older_unrated_heal_is_still_wired(self) -> None:
        """CONTROL — the provisional heal complements, not replaces, it."""
        src = (REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py").read_text(
            encoding="utf-8"
        )
        assert "_album_scored_but_never_rated(tracks)" in src


# ---------------------------------------------------------------------------
# 3. The two phases are wired: phase 1 provisional, phase 2 final
# ---------------------------------------------------------------------------


class TestTheTwoPhasesAreWired:
    @staticmethod
    def _runner_src() -> str:
        return (REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py").read_text(
            encoding="utf-8"
        )

    def test_phase_1_runs_right_after_the_album_completes(self) -> None:
        """Provisional posting sits right AFTER the pending-append (so the
        artist boundary still sees the album) inside the album loop."""
        src = self._runner_src()
        append_idx = src.index('_artist_pending_albums.setdefault(artist, []).append({')
        post_idx = src.index("reconciled=False,", append_idx)
        assert post_idx - append_idx < 2500, (
            "the provisional post must follow the pending append in the same "
            f"album-completion block (distance was {post_idx - append_idx})"
        )

    def test_phase_1_is_guarded_and_non_fatal(self) -> None:
        """A provisional post failing must not discard the album."""
        src = self._runner_src()
        idx = src.index("reconciled=False,")
        window = src[max(0, idx - 900): idx + 500]
        assert "except Exception" in window, (
            "phase 1 must be wrapped so a rating failure cannot abort the album"
        )
        assert "provisional=True" in window

    def test_phase_2_marks_the_rating_final(self) -> None:
        src = self._runner_src()
        flush_idx = src.index("def _flush_artist_star_ratings")
        window = src[flush_idx: flush_idx + 2400]
        assert "reconciled=True" in window, (
            "the artist-boundary flush is the COMPLETE-catalogue pass — it must "
            "write stars_reconciled=TRUE"
        )
        assert "provisional=True" not in window, (
            "phase 2 must print the full SCAN RESULTS table"
        )


# ---------------------------------------------------------------------------
# 4. post_album_star_ratings: the flag rides with the stars; provisional logs
#    keep the durability (star writes + Navidrome sync) but drop the table
# ---------------------------------------------------------------------------


class _PhaseHarness:
    """Runs ``post_album_star_ratings`` against a recording session with every
    collaborator that needs a network/file stubbed out."""

    @staticmethod
    def run(monkeypatch, *, stars_reconciled=None, provisional=False,
            sync_navidrome=False):
        from services.popularity.stages import finalise_stage as fs

        session = _RecordingSession()
        monkeypatch.setattr(fs, "db_session", _session_factory(session))
        monkeypatch.setattr(fs, "_assign_stars", lambda track, *a, **k: 4)
        monkeypatch.setattr(fs, "_build_rating_sync_clients", lambda: [object()])
        syncs: list[tuple] = []
        monkeypatch.setattr(
            fs, "_sync_rating_to_navidrome",
            lambda tid, stars, clients=None: syncs.append((tid, stars)) or True,
        )
        logs: list[str] = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logs.append(str(msg)))

        results = _album_results()
        outcome = fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={"sync_navidrome": bool(sync_navidrome)},
            stars_reconciled=stars_reconciled,
            provisional=provisional,
        )
        return outcome, results, session, syncs, logs


class TestTheFlagRidesWithTheStars:
    def test_a_provisional_post_writes_false_atomically(self, monkeypatch) -> None:
        _o, _r, session, _s, _l = _PhaseHarness.run(
            monkeypatch, stars_reconciled=False, provisional=True
        )
        updates = _star_updates(session)
        assert updates, "no star UPDATE was recorded — the stars never persisted"
        sql, params = updates[0]
        assert "stars_reconciled = :rec" in sql, sql
        assert params["rec"] is False
        assert params["stars"] == 4

    def test_a_reconciled_post_writes_true(self, monkeypatch) -> None:
        _o, _r, session, _s, _l = _PhaseHarness.run(
            monkeypatch, stars_reconciled=True
        )
        updates = _star_updates(session)
        assert updates and updates[0][1]["rec"] is True

    def test_no_flag_leaves_the_sql_untouched(self, monkeypatch) -> None:
        """CONTROL — the finalise grouping path passes nothing (None), so its
        UPDATE must stay the plain pre-existing statement."""
        _o, _r, session, _s, _l = _PhaseHarness.run(monkeypatch)
        updates = _star_updates(session)
        assert updates, "stars must still persist"
        sql, params = updates[0]
        assert "stars_reconciled" not in sql, sql
        assert "rec" not in (params or {})

    def test_provisional_persists_every_rated_track(self, monkeypatch) -> None:
        _o, results, session, _s, _l = _PhaseHarness.run(
            monkeypatch, stars_reconciled=False, provisional=True
        )
        assert len(_star_updates(session)) == 2, (
            "phase 1 exists for durability — every rated track must be written"
        )
        assert [t["stars"] for t in results] == [4, 4]

    def test_provisional_skips_the_table_but_still_logs_a_line(self, monkeypatch) -> None:
        _o, _r, _session, _s, logs = _PhaseHarness.run(
            monkeypatch, stars_reconciled=False, provisional=True
        )
        joined = "\n".join(logs)
        assert "SCAN RESULTS" not in joined, (
            "the table is computed with the PARTIAL artist context — printing "
            "it now would present a not-yet-reconciled ranking as final"
        )
        assert "Provisional ratings" in joined, (
            "phase 1 must still leave one line of proof in the log"
        )

    def test_the_final_pass_still_prints_the_table(self, monkeypatch) -> None:
        """CONTROL — the artist-boundary pass must print exactly as before."""
        _o, _r, _session, _s, logs = _PhaseHarness.run(
            monkeypatch, stars_reconciled=True
        )
        assert any("SCAN RESULTS" in m for m in logs)

    def test_provisional_still_syncs_navidrome(self, monkeypatch) -> None:
        """The durability half includes the Navidrome sync — an early return
        in the provisional branch would silently strand phase 1's stars in the
        DB while Navidrome keeps the old ratings."""
        _o, _r, _session, syncs, _logs = _PhaseHarness.run(
            monkeypatch, stars_reconciled=False, provisional=True,
            sync_navidrome=True,
        )
        assert len(syncs) == 2, (
            f"phase 1 must sync its durable stars to Navidrome, got {syncs!r}"
        )
