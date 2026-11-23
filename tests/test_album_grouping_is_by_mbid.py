"""Both album pipelines must group by album IDENTITY, not by album title.

⭐ This is the regression guard for the reported split. The bug was not in the
rating maths — every rule behaved correctly — it was that one MusicBrainz
release entered the pipeline as two albums, so the "album" each track was rated
against was not the album the operator sees.

Two independent paths build those groups:

  * the scan, via ``load_stage.load_candidates``;
  * the star resync, via ``star_resync_service._load_album_groups``.

Fixing one and not the other would leave the preview disagreeing with the scan,
which is exactly the kind of drift these tests exist for.
"""

from __future__ import annotations

import inspect
import re

import pytest


class TestTheScanGroupsByIdentity:
    def test_load_candidates_uses_album_identity(self) -> None:
        from services.popularity.stages import load_stage

        source = inspect.getsource(load_stage.load_candidates)
        assert "album_identity" in source, (
            "load_candidates still groups by (album_artist, album) strings"
        )

    def test_it_does_not_nest_by_album_title(self) -> None:
        """The old shape was ``grouped[artist][album]`` — two human strings."""
        from services.popularity.stages import load_stage

        source = inspect.getsource(load_stage.load_candidates)
        assert "setdefault(artist, {})" not in source
        assert 'grouped.setdefault(identity, []).append(row)' in source

    def test_it_keeps_the_display_name(self) -> None:
        """Logs, playlists and the album page must read as before."""
        from services.popularity.stages import load_stage

        source = inspect.getsource(load_stage.load_candidates)
        assert "display_name" in source


class TestTheResyncGroupsByIdentity:
    def test_the_resync_uses_album_identity(self) -> None:
        from services.popularity import star_resync_service as svc

        source = inspect.getsource(svc._load_album_groups)
        assert "album_identity" in source

    def test_it_selects_the_mbid_columns(self) -> None:
        """Grouping by an id the query never fetched would silently fall back."""
        from services.popularity import star_resync_service as svc

        assert "musicbrainz_albumid" in svc._TRACK_COLUMNS
        assert "musicbrainz_album_mbid" in svc._TRACK_COLUMNS

    def test_it_carries_the_ids_onto_the_track_dict(self) -> None:
        from services.popularity import star_resync_service as svc

        source = inspect.getsource(svc._row_to_track)
        assert "musicbrainz_albumid" in source

    def test_no_group_unpacks_a_two_tuple_key(self) -> None:
        """``for (artist, album), rows in groups.items()`` raises on an MBID key."""
        from services.popularity import star_resync_service as svc

        source = inspect.getsource(svc)
        bad = re.findall(r"for \(\s*\w+\s*,\s*\w+\s*\)\s*,\s*[\w_]+ in groups\.items\(\)", source)
        assert not bad, f"still unpacks a fixed pair from an identity key: {bad}"


class TestTheTwoPathsAgree:
    def test_the_same_key_function_is_used(self) -> None:
        from services.popularity import album_identity as ident
        from services.popularity.stages import load_stage
        from services.popularity import star_resync_service as svc

        for module in (load_stage, svc):
            source = inspect.getsource(module)
            assert "services.popularity.album_identity import" in source, (
                f"{module.__name__} does not use the shared identity function"
            )
        assert callable(ident.album_identity)


class TestTheScanEndToEnd:
    """The reported split, end to end through the real loader."""

    MBID = "3c8d8b38-ac3b-480c-87f8-5048a6198ff5"
    OTHER = "11111111-2222-3333-4444-555555555555"

    @pytest.fixture
    def library(self):
        from sqlalchemy import text

        from db.engine import get_engine
        from db.models import Track

        Track.__table__.create(get_engine(), checkfirst=True)
        rows = [
            dict(id="t15", title="Here to Stay (T Ray's mix)", artist="Korn",
                 album_artist="Korn", album="Here to Stay (T Ray's mix)",
                 musicbrainz_albumid=self.MBID, musicbrainz_albumtype="Album",
                 releasedate="2002-06-19", disc_number=1, track_number=15,
                 popularity=50, final_score=50),
            dict(id="t01", title="Here to Stay", artist="Korn", album_artist="Korn",
                 album="Here to Stay", musicbrainz_albumid=self.MBID,
                 musicbrainz_albumtype="Album", releasedate="",
                 disc_number=1, track_number=1, popularity=40, final_score=40),
            dict(id="o1", title="Other", artist="Korn", album_artist="Korn",
                 album="Other Album", musicbrainz_albumid=self.OTHER,
                 musicbrainz_albumtype="Album", disc_number=1, track_number=1,
                 popularity=30, final_score=30),
        ]
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM tracks"))
            for row in rows:
                conn.execute(
                    text(f"INSERT INTO tracks ({', '.join(row)}) "
                         f"VALUES ({', '.join(':' + c for c in row)})"),
                    row,
                )
        return rows

    def test_the_two_rips_become_one_candidate(self, library) -> None:
        from services.popularity.stages.load_stage import load_candidates

        candidates = load_candidates({})
        korn = [c for c in candidates if c["album"].startswith("Here to Stay")]
        assert len(korn) == 1, (
            "the re-release must not be its own one-track album — a one-track "
            "album has no distribution to curve a rating across"
        )
        assert len(korn[0]["tracks"]) == 2

    def test_the_display_name_is_still_one_of_the_real_titles(self, library) -> None:
        from services.popularity.stages.load_stage import load_candidates

        korn = next(c for c in load_candidates({}) if c["album"].startswith("Here to Stay"))
        assert korn["album"] in {"Here to Stay", "Here to Stay (T Ray's mix)"}

    def test_the_album_filter_matches_any_title_in_the_release(self, library) -> None:
        from services.popularity.stages.load_stage import load_candidates

        for wanted in ("Here to Stay", "Here to Stay (T Ray's mix)"):
            found = load_candidates({"album_filter": wanted})
            assert found, f"the filter cannot find the release by {wanted!r}"
