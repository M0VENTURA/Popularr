"""The album blend must not read Navidrome — it writes what Navidrome reflects.

⚠️⚠️ THE BUG THIS PINS: A TAG PIPELINE FEEDING ON ITS OWN OUTPUT

``navidrome_genres`` is not an independent genre source. Navidrome serves the
tags of the files ON DISK, and this pipeline is what writes them:

    scan writes ``tracks.genres``
      → ``sync_album_file_tags`` pushes ``genres`` into each FILE's tags
        → Navidrome re-reads the files and serves the genre back
          → the next import stores it as ``navidrome_genres``
            → ``get_track_recommendations`` counted it as genre EVIDENCE

So whatever the blend got WRONG in one pass returned as PROOF it was right in
the next. Measured on a clean 14-track album with one Christmas song and no
holiday tag anywhere: the blend was ``['Alternative Rock']`` before the round
trip and ``['Alternative Rock', 'Holiday']`` after it, ``_album_holiday_votes``
scored 14/14 holiday, and ``album_is_holiday_album`` returned True.

⭐ No threshold fixes that. The majority rule was ALREADY in place — the
contaminated evidence simply made the majority look unanimous. That is why the
user still saw "it's still assigning holiday to all tracks" AFTER the majority
fix: the denominator was right, the evidence was poisoned.

The convention was already established elsewhere in this file —
``genre_selection`` weights ``navidrome`` 0.30 as "a tie-breaker, never a sole
source", and the VA path passes ``nav_genres=None`` on purpose.
"""

from __future__ import annotations

import inspect
import json
import re
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from services.enrichment import genre_aggregation_service as gas


def _regexp_replace(value, pattern, replacement, flags=""):
    import re

    if value is None:
        return None
    fl = re.IGNORECASE if flags and "i" in flags else 0
    return re.sub(pattern, replacement, value or "", flags=fl)


_ARTIST = "Loop Probe Artist"
_ALBUM = "Loop Probe Album"


@pytest.fixture()
def seeded(monkeypatch, tmp_path):
    """14 tracks, ONE a Christmas song, with a controllable Navidrome column.

    ``navidrome_genres`` is what would be reflected back after a write; the
    MusicBrainz genres stand in for the real, independent evidence.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'loop.db'}")

    @event.listens_for(engine, "connect")
    def _register(conn, _rec):
        conn.create_function("REGEXP_REPLACE", 4, _regexp_replace)

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                artist TEXT, album_artist TEXT, album TEXT, title TEXT,
                lastfm_tags TEXT, musicbrainz_genres TEXT, discogs_genres TEXT,
                listenbrainz_genres TEXT, spotify_genres TEXT, essentia_genres TEXT,
                manual_genres TEXT, navidrome_genres TEXT, audiodb_genres TEXT,
                wikidata_genres TEXT
            )
        """))

    def _seed(navidrome_value: str | None) -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tracks"))
            for i in range(1, 15):
                conn.execute(
                    text("""
                        INSERT INTO tracks
                            (id, artist, album_artist, album, title,
                             musicbrainz_genres, navidrome_genres)
                        VALUES (:id, :a, :a, :al, :t, :mb, :nav)
                    """),
                    {
                        "id": f"t{i}",
                        "a": _ARTIST,
                        "al": _ALBUM,
                        # The ONE seasonal track. Its title is the only real
                        # seasonal evidence on this album.
                        "t": "Hating You for Christmas" if i == 1 else f"Ordinary Song {i}",
                        "mb": json.dumps(["Alternative Rock"]),
                        "nav": navidrome_value,
                    },
                )

    @contextmanager
    def _db_session(*_a, **_k):
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(gas, "db_session", _db_session)
    yield _seed
    engine.dispose()


class TestTheBlendIgnoresNavidrome:
    """``navidrome_genres`` must not vote in the album blend."""

    def test_a_clean_album_has_no_holiday(self, seeded):
        seeded(None)
        result = gas.get_track_recommendations(_ARTIST, _ALBUM)

        assert "Holiday" not in (result.get("genres") or [])

    def test_holiday_arriving_only_via_navidrome_is_ignored(self, seeded):
        """THE REGRESSION.

        This is the round-trip state: Navidrome reflects back the "Holiday"
        this pipeline previously wrote, while the real genres are unchanged.
        The blend must NOT adopt it.
        """
        seeded("Holiday")
        result = gas.get_track_recommendations(_ARTIST, _ALBUM)

        assert "Holiday" not in (result.get("genres") or []), (
            "the blend read Navidrome, which serves the tags this pipeline "
            "writes — the value came back as evidence for itself"
        )

    def test_real_genres_survive_the_round_trip(self, seeded):
        """Removing the source must not empty the blend."""
        seeded("Holiday")
        result = gas.get_track_recommendations(_ARTIST, _ALBUM)

        assert "Alternative Rock" in (result.get("genres") or [])

    def test_christmas_still_propagates_from_a_title(self, seeded):
        """The genuine signal must keep working — this is not a blanket ban.

        An album whose TITLE is seasonal is seasonal, regardless of Navidrome.
        """
        seeded(None)

        from services.catalog.album_classification_service import detect_christmas_song

        assert detect_christmas_song("", "A Very Special Christmas") is True
        assert gas.album_is_holiday_album(
            album="A Very Special Christmas",
            holiday_track_count=0,
            track_count=14,
        ) is True

    def test_navidrome_not_in_the_source_map(self, seeded):
        """Pin the mechanism, not just the symptom."""
        source = inspect.getsource(gas.get_track_recommendations)
        mapping_block = source.split("column_mapping = [", 1)[1].split("]\n", 1)[0]

        assert '"navidrome"' not in mapping_block, (
            "navidrome must not appear as a source_map key in the album blend"
        )

    def test_navidrome_column_not_in_the_select(self, seeded):
        """``navidrome_genres`` must not be read at all.

        Checked against the SQL only — the surrounding docstring explains WHY
        the column was dropped, so a whole-function text search would trip on
        the explanation rather than on the code.
        """
        source = inspect.getsource(gas.get_track_recommendations)
        select_block = source.split("SELECT", 1)[1].split("FROM", 1)[0]

        assert "navidrome_genres" not in select_block

    def test_column_indices_still_align_with_the_select(self, seeded):
        """The mappings are positional, so a dropped column MUST renumber.

        A stale index would silently read ``audiodb_genres`` where
        ``navidrome_genres`` used to be — a wrong answer with no error.
        """
        seeded(None)
        source = inspect.getsource(gas.get_track_recommendations)
        select_block = source.split("SELECT", 1)[1].split("FROM", 1)[0]
        selected = [c.strip() for c in select_block.replace("\n", " ").split(",")]

        mapping_block = source.split("column_mapping = [", 1)[1].split("]\n", 1)[0]
        pairs = re.findall(r'\("([^"]+)",\s*(\d+)\)', mapping_block)

        assert pairs, "no (source, index) pairs parsed — the test is not measuring"

        for name, raw_index in pairs:
            index = int(raw_index)
            assert index < len(selected), (
                f"mapping {name!r} points at index {index} but only "
                f"{len(selected)} columns are selected"
            )
            assert selected[index] == f"{name}_genres" or selected[index].startswith(name), (
                f"mapping {name!r} points at index {index}, which selects "
                f"{selected[index]!r} — the column order changed without "
                "renumbering the mapping"
            )

    def test_holiday_vote_does_not_count_navidrome(self, seeded):
        """The vote and the blend must agree about what evidence is."""
        seeded("Holiday")
        result = gas.get_track_recommendations(_ARTIST, _ALBUM)

        assert "Holiday" not in (result.get("genres") or [])

    def test_repeated_scans_do_not_ratchet(self, seeded):
        """The loop's defining property: a fixed point must be stable.

        Running twice must give the same answer. Before the fix the second run
        saw the first run's output.
        """
        seeded("Holiday")
        first = gas.get_track_recommendations(_ARTIST, _ALBUM).get("genres") or []
        second = gas.get_track_recommendations(_ARTIST, _ALBUM).get("genres") or []

        assert first == second
        assert "Holiday" not in first
