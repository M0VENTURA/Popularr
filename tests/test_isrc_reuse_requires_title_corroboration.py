"""ISRC reuse must require TITLE corroboration, not just the ISRC.

Reported (scoring review, Arion — "Life Is Not Beautiful")
----------------------------------------------------------
Three bonus tracks ("The End Of The Fall" plus two 2018 bonus tracks) all carry
ISRC ``FIRAN1800001`` — the lead single's code, upstream. Every one of them
inherited the single's 91.7 score / 42k listeners before the final pass could
reconcile it.

Two shipped paths reuse data by ISRC, and BOTH matched on the identity alone:

* ``popularity_cache_service.get_recording_identity_popularity`` — copies
  listeners from the strongest row sharing the code.
* ``finalise_stage._sync_isrc_popularity`` — ``MAX(score)`` grouped by ``isrc``,
  which ALSO ORs ``is_single`` (so a mis-tagged row was marked a single too).

The fix requires the titles to agree. These tests are the discriminators.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# ===========================================================================
# 1. The cache copy (Python) — normalised-title corroboration
# ===========================================================================

SINGLE_ISRC = "FIRAN1800001"


def _rows(*rows):
    """Rows shaped like the SELECT in get_recording_identity_popularity."""
    return list(rows)


class TestTheCacheCopyRequiresTheSameTitle:
    def _run(self, monkeypatch, *, requested_title, sibling_title, sibling_isrc=SINGLE_ISRC):
        from services.popularity import popularity_cache_service as svc

        db_rows = _rows({
            "id": "sibling",
            "title": sibling_title,
            "artist": "Arion",
            "album": "At The Break Of Dawn",
            "isrc": sibling_isrc,
            "recording_mbid": None,
            "lastfm_listeners": 42000,
            "lastfm_playcount": 90000,
            "listenbrainz_listens": 1000,
            "listenbrainz_users": 100,
            "lastfm_tags": "",
            "is_live": 0,
        })

        class _Result:
            def fetchall(self):
                return [type("R", (), {"_mapping": r})() for r in db_rows]

        class _Session:
            def execute(self, *a, **k):
                return _Result()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("db.engine.db_session", lambda: _Session())

        tracks = [{
            "id": "requested",
            "title": requested_title,
            "isrc": SINGLE_ISRC,
            "recording_mbid": None,
        }]
        return svc.get_recording_identity_popularity(tracks)

    def test_a_different_song_sharing_a_bad_isrc_does_not_inherit(self, monkeypatch):
        """THE REPORTED CASE: a bonus track must not answer from the hit."""
        out = self._run(
            monkeypatch,
            requested_title="The End Of The Fall",
            sibling_title="At The Break Of Dawn",
        )

        assert out == {}

    def test_the_same_song_still_inherits(self, monkeypatch):
        """CONTROL — the feature the user asked for still works."""
        out = self._run(
            monkeypatch,
            requested_title="At The Break Of Dawn",
            sibling_title="At The Break Of Dawn",
        )

        assert out
        assert out["at the break of dawn"]["lastfm_listeners"] == 42000

    def test_a_variant_spelling_still_corroborates(self, monkeypatch):
        """⭐ The normaliser folds \"(feat. X)\" etc., so a genuine variant
        spelling of the SAME song must still corroborate."""
        out = self._run(
            monkeypatch,
            requested_title="The End Of The Fall",
            sibling_title="The End Of The Fall (feat. Someone)",
        )

        assert out
        assert out["the end of the fall"]["lastfm_listeners"] == 42000

    def test_a_remaster_marker_also_corroborates(self, monkeypatch):
        out = self._run(
            monkeypatch,
            requested_title="At The Break Of Dawn",
            sibling_title="At The Break Of Dawn (Remastered)",
        )

        assert out


# ===========================================================================
# 2. The SQL sync — MAX(score) must not cross titles
# ===========================================================================

class TestTheSqlSyncDoesNotCrossTitles:
    """Drives the REAL SQL against SQLite.

    ⚠️ ``BOOL_OR`` is PostgreSQL-only, so the statement is adapted for SQLite
    the same way the suite adapts any PG-only aggregate: the title-safety
    property under test does not depend on it.
    """

    def _db(self, monkeypatch):
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{Path(tmp) / 't.db'}")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE tracks (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    isrc TEXT,
                    popularity REAL,
                    final_score REAL,
                    is_single INTEGER DEFAULT 0
                )
            """))
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        class _Wrapper:
            def __init__(self, s):
                self._s = s

            def execute(self, *a, **k):
                return self._s.execute(*a, **k)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, *a):
                if exc_type is None:
                    self._s.commit()
                self._s.close()
                return False

        session = factory()
        import services.popularity.stages.finalise_stage as fs

        monkeypatch.setattr(fs, "db_session", lambda *a, **k: _Wrapper(session))

        def _sql(stmt):
            # SQLite adaptation: BOOL_OR → MAX over the CAST integer flag.
            return str(stmt).replace(
                "BOOL_OR(COALESCE(is_single, FALSE))",
                "MAX(COALESCE(is_single, 0))",
            )

        # ⚠️ Must return a REAL TextClause — the shipped code passes the result
        # straight to ``session.execute`` without calling ``bindparams``, so a
        # wrapper object would fail inside SQLAlchemy instead of testing the SQL.
        from sqlalchemy import text as _real_text

        monkeypatch.setattr(fs, "text", lambda sql, *a, **k: _real_text(_sql(sql)))
        return session

    def test_a_bonus_track_does_not_inherit_the_singles_score(self, monkeypatch):
        """THE REPORTED CASE."""
        session = self._db(monkeypatch)
        from sqlalchemy import text as real_text
        import services.popularity.stages.finalise_stage as fs

        session.execute(real_text("""
            INSERT INTO tracks (id, title, isrc, popularity, final_score, is_single) VALUES
              ('s1', 'At The Break Of Dawn', :isrc, 91.7, 91.7, 1),
              ('b1', 'The End Of The Fall', :isrc, 12.0, 12.0, 0),
              ('b2', 'Bonus Track Two',     :isrc, 8.0,  8.0,  0)
        """), {"isrc": SINGLE_ISRC})
        session.commit()

        fs._sync_isrc_popularity()

        rows = {
            r[0]: (r[1], r[2])
            for r in session.execute(
                real_text("SELECT id, popularity, is_single FROM tracks")
            ).fetchall()
        }
        assert rows["s1"] == (91.7, 1)          # control: the single keeps its own
        assert rows["b1"][0] == 12.0            # NOT 91.7
        assert rows["b2"][0] == 8.0
        assert rows["b1"][1] == 0               # NOT marked a single
        assert rows["b2"][1] == 0

    def test_the_same_title_still_pools(self, monkeypatch):
        """CONTROL — the duplicate-recording reuse this sync exists for."""
        session = self._db(monkeypatch)
        from sqlalchemy import text as real_text
        import services.popularity.stages.finalise_stage as fs

        session.execute(real_text("""
            INSERT INTO tracks (id, title, isrc, popularity, final_score, is_single) VALUES
              ('a1', 'At The Break Of Dawn', :isrc, 91.7, 91.7, 0),
              ('a2', 'At The Break Of Dawn', :isrc, 40.0, 40.0, 0)
        """), {"isrc": SINGLE_ISRC})
        session.commit()

        fs._sync_isrc_popularity()

        pops = [
            r[0] for r in session.execute(
                real_text("SELECT popularity FROM tracks ORDER BY id")
            ).fetchall()
        ]
        assert pops == [91.7, 91.7]

    def test_a_case_or_space_difference_still_pools(self, monkeypatch):
        """``LOWER(TRIM(title))`` is the key, so casing/whitespace still pools."""
        session = self._db(monkeypatch)
        from sqlalchemy import text as real_text
        import services.popularity.stages.finalise_stage as fs

        session.execute(real_text("""
            INSERT INTO tracks (id, title, isrc, popularity, final_score, is_single) VALUES
              ('a1', 'At The Break Of Dawn',  :isrc, 91.7, 91.7, 0),
              ('a2', '  at the break of dawn ', :isrc, 10.0, 10.0, 0)
        """), {"isrc": SINGLE_ISRC})
        session.commit()

        fs._sync_isrc_popularity()

        pops = [
            r[0] for r in session.execute(
                real_text("SELECT popularity FROM tracks ORDER BY id")
            ).fetchall()
        ]
        assert pops == [91.7, 91.7]
