"""The artist normaliser must not oscillate with the import payload.

REPORTED:

    INFO services.scanning.cleanup  Normalized existing artist rows
         canonical_artist='SOiL' alias='SOiL' updated_rows=40

`alias == canonical` yet 40 rows were "updated", every scan.

CAUSE

``normalize_existing_artist_rows`` added ``.lower()``/``.upper()``/``.title()``
of the canonical unconditionally. For a mixed-case act those are DIFFERENT
spellings — ``"SOiL".title()`` is ``"Soil"`` — so the scan rewrote ``"Soil"`` to
``"SOiL"`` while ``payload_builder`` wrote ``"Soil"`` back on the very same pass.
The two oscillated forever, churning row versions (dead tuples, write
amplification, autovacuum pressure).

The log line itself is also misleading: ``alias=`` reports the Navidrome VIEW
name, not the alias that matched.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from db.repositories.scan_repository import normalize_existing_artist_rows


@pytest.fixture()
def artist_db(tmp_path, monkeypatch):
    import db.repositories.scan_repository as repo

    url = f"sqlite:///{tmp_path / 'artist.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tracks (id TEXT PRIMARY KEY, artist TEXT, album_artist TEXT)"
        ))
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _seed(rows):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tracks"))
            for tid, artist, album_artist in rows:
                conn.execute(
                    text("INSERT INTO tracks VALUES (:i, :a, :aa)"),
                    {"i": tid, "a": artist, "aa": album_artist},
                )

    def _rows():
        with engine.begin() as conn:
            return {
                r[0]: (r[1], r[2])
                for r in conn.execute(text("SELECT id, artist, album_artist FROM tracks"))
            }

    class _Sess:
        def __init__(self, s):
            self._s = s

        def execute(self, *a, **k):
            return self._s.execute(*a, **k)

        def commit(self):
            self._s.commit()

        def __enter__(self):
            return self

        def __exit__(self, *e):
            if e[0] is None:
                self._s.commit()
            self._s.close()
            return False

    monkeypatch.setattr(repo, "db_session", lambda: _Sess(factory()))
    yield _seed, _rows
    engine.dispose()


class TestNoCaseVariantOfTheCanonicalIsRewritten:
    def test_a_mixed_case_act_is_left_alone(self, artist_db):
        """``"SOiL".title() == "Soil"`` must NOT rewrite anything."""
        seed, rows = artist_db
        seed([("t1", "SOiL", "SOiL"), ("t2", "SOiL", "SOiL")])
        updated = normalize_existing_artist_rows(
            canonical_artist_name="SOiL", aliases=["SOiL"]
        )
        assert updated == 0, (
            "the payload writes the same value back, so any write here is a "
            "permanent oscillation"
        )
        assert rows()["t1"] == ("SOiL", "SOiL")

    def test_a_genuine_alias_is_still_rewritten(self, artist_db):
        """CONTROL: a real variant must still be merged."""
        seed, rows = artist_db
        seed([("t1", "Soil", "Soil"), ("t2", "SOIL", "SOIL")])
        updated = normalize_existing_artist_rows(
            canonical_artist_name="SOiL", aliases=["Soil", "SOIL"]
        )
        assert updated > 0
        assert rows()["t1"] == ("SOiL", "SOiL")
        assert rows()["t2"] == ("SOiL", "SOiL")

    def test_a_second_run_is_a_no_op(self, artist_db):
        """The loop symptom: run twice, nothing left to do."""
        seed, _rows = artist_db
        seed([("t1", "Soil", "Soil")])
        first = normalize_existing_artist_rows(
            canonical_artist_name="SOiL", aliases=["Soil"]
        )
        second = normalize_existing_artist_rows(
            canonical_artist_name="SOiL", aliases=["Soil"]
        )
        assert first == 2  # artist + album_artist on one row
        assert second == 0, "a converged album must not keep re-writing"


class TestLockOrderingIsDeterministic:
    """The bulk tag stamp must acquire row locks in ascending id order."""

    def test_the_stamp_writes_in_sorted_order(self, monkeypatch):
        from services.scanning import reconciliation as rec

        order: list[str] = []

        class _Sess:
            def execute(self, statement, params=None):
                order.append(str((params or {}).get("track_id")))

                class _R:
                    rowcount = 1

                return _R()

            def commit(self):
                pass

        class _Ctx:
            def __enter__(self_inner):
                return _Sess()

            def __exit__(self_inner, *e):
                return False

        monkeypatch.setattr(rec, "db_session", lambda: _Ctx())
        rec.mark_local_tags_written_for_track_ids(["c", "a", "b", "a"])
        assert order == ["a", "b", "c"], order
