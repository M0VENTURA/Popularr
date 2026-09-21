"""The artist page reads the DATABASE — the scan does the metadata lookups.

Reported: "Is the artist page doing metadata lookups? It should only be checking
on the database itself; all parts of the lookups that are happening on the page
should be done during the metadata scans in the popularity scan runners."

It was doing two kinds, and both are pinned here:

* ``get_artist_members_cached`` performed a MusicBrainz ``search_artists`` +
  ``get_artist_members`` on EVERY artist-page load whenever its 7-day cache was
  cold — a network call inside ``_build_artist_detail_payload``. Because the
  shared client throttles by RESERVING a future slot and sleeping, a page load
  during a scan queued behind it for tens of seconds and stalled the hypercorn
  worker. The scan now owns the lookup (``album_stage._fetch_artist_metadata``,
  the step that already caches country/bio/image for the same artist).
* ``GET /api/album/missing-tracks`` recomputed the missing set from MusicBrainz
  on every request — and the artist page calls it ONCE PER OWNED ALBUM on load,
  plus a release SEARCH per album that has no stored MBID. The endpoint is now a
  pure ``missing_album_tracks`` read and the scan refreshes that table.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeResult:
    def __init__(self, rows):
        # A single row may be handed over as a bare mapping; normalise it, so a
        # test cannot fail with a KeyError(0) from indexing the mapping itself.
        if isinstance(rows, dict):
            rows = [rows]
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else 0


class _FakeSession:
    """Replays canned results in order and records every statement."""

    def __init__(self, results):
        self._results = list(results)
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return _FakeResult(self._results.pop(0) if self._results else [])


class _FakeCM:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False


def _patch_session(monkeypatch, module, session):
    monkeypatch.setattr(module, "db_session", lambda: _FakeCM(session))
    return session


def _strip_string_literals(source: str) -> str:
    """``source`` with every string constant blanked out.

    Used by the "this call is gone" guards: a docstring or comment that names a
    retired call is documentation, not usage, and must neither satisfy nor fail
    the check. SQL text lives in string constants too, so any assertion ABOUT
    the SQL reads the raw source instead.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.unparse(tree)


# ---------------------------------------------------------------------------
# Artist members
# ---------------------------------------------------------------------------

class TestMembersReaderIsDatabaseOnly:
    def test_it_cannot_reach_musicbrainz(self):
        from services.metadata import artist_metadata_service as svc

        source = inspect.getsource(svc.get_artist_members_cached)
        # Scan the CODE only. The docstring deliberately NAMES the retired calls
        # (that prose is the explanation of why they went), and a raw substring
        # scan would trip over it — the same trap as grepping a source file for a
        # string the comments quote.
        code_only = _strip_string_literals(source)
        # ``get_artist_members`` is asserted WITH its opening parenthesis: the
        # function's own name is ``get_artist_members_cached``, so the bare name
        # is a substring of the definition itself and could never be absent.
        for forbidden in ("search_artists", "get_shared_mb_client", "get_artist_members("):
            assert forbidden not in code_only, f"the page path must not call {forbidden}"
        # The SQL text is a string constant, so that check reads the RAW source.
        assert "SELECT" in source

    def test_it_parses_the_cached_roster(self, monkeypatch):
        from services.metadata import artist_metadata_service as svc

        members = [{"name": "Someone", "role": "guitar"}]
        session = _patch_session(monkeypatch, svc, _FakeSession([{"members": json.dumps(members)}]))
        assert svc.get_artist_members_cached("Artist") == members
        assert "\"members\"" in session.statements[0] or "members" in session.statements[0]

    def test_no_row_means_no_members_not_a_lookup(self, monkeypatch):
        from services.metadata import artist_metadata_service as svc

        _patch_session(monkeypatch, svc, _FakeSession([None]))
        assert svc.get_artist_members_cached("Artist") == []

    def test_unparsable_cache_is_not_fatal(self, monkeypatch):
        from services.metadata import artist_metadata_service as svc

        _patch_session(monkeypatch, svc, _FakeSession([{"members": "{not json"}]))
        assert svc.get_artist_members_cached("Artist") == []

    def test_a_failing_query_returns_empty(self, monkeypatch):
        from services.metadata import artist_metadata_service as svc

        def _boom():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(svc, "db_session", _boom)
        assert svc.get_artist_members_cached("Artist") == []


class TestScanOwnsTheMembersLookup:
    def test_the_artist_metadata_step_writes_the_roster(self):
        from services.popularity.stages import album_stage

        source = inspect.getsource(album_stage._fetch_artist_metadata)
        assert "_lookup_artist_members" in source
        assert "members_last_updated" in source
        # Freshness window preserved from the page's old rule.
        assert "_ARTIST_MEMBERS_TTL_DAYS" in source or "timedelta" in source

    def test_a_stale_roster_is_refreshed(self):
        from services.popularity.stages import album_stage

        source = inspect.getsource(album_stage._fetch_artist_metadata)
        assert "fresh cache" in source, "a fresh roster must be skipped, a stale one refetched"

    def test_the_persist_never_clobbers_with_nothing(self):
        """COALESCE on conflict: a failed lookup must not erase the roster."""
        from services.popularity.stages import album_stage

        source = inspect.getsource(album_stage._fetch_artist_metadata)
        assert "COALESCE(excluded.members, artists.members)" in source

    def test_the_lookup_prefers_a_group(self):
        from services.popularity.stages import album_stage

        class _Client:
            def search_artists(self, artist, limit=5):
                return [
                    {"id": "person-1", "type": "Person"},
                    {"id": "group-1", "type": "Group"},
                ]

            def get_artist_members(self, mbid):
                return [{"name": "Member", "mbid": mbid}]

        assert album_stage._lookup_artist_members("Artist", _Client()) == [
            {"name": "Member", "mbid": "group-1"}
        ]

    def test_the_lookup_returns_nothing_without_a_match(self):
        from services.popularity.stages import album_stage

        class _Client:
            def search_artists(self, artist, limit=5):
                return []

        assert album_stage._lookup_artist_members("Artist", _Client()) == []


# ---------------------------------------------------------------------------
# Missing tracks
# ---------------------------------------------------------------------------

class TestMissingTracksEndpointIsDatabaseOnly:
    def test_the_route_reads_the_persisted_snapshot(self):
        from routes import album_routes

        source = inspect.getsource(album_routes)
        assert "get_missing_tracks_from_db" in source
        # The recompute must not be reachable from the route any more.
        assert "result = get_missing_tracks(" not in source

    def test_the_recompute_is_still_available_for_the_scan(self):
        from services.metadata import album_missing_service as svc

        # The MB-driven version must survive — the scan calls it.
        assert "fetch_musicbrainz_release_metadata" in inspect.getsource(svc.get_missing_tracks)

    def test_the_scan_refreshes_the_snapshot_per_album(self):
        from services.popularity import scan_stage_runner

        source = inspect.getsource(scan_stage_runner)
        assert "get_missing_tracks(artist=artist, album=album)" in source
        assert "[MISSING_TRACKS]" in source


class TestMissingTracksReader:
    def _rows(self):
        return [
            {"title": "Gone", "track_number": "4", "disc_number": 1,
             "track_artist": "Artist", "year": "2026", "release_id": "rel-1",
             "recording_mbid": "rec-1", "duration": 200},
        ]

    def test_it_returns_the_persisted_rows(self, monkeypatch):
        from services.metadata import album_missing_service as svc

        session = _patch_session(monkeypatch, svc, _FakeSession([self._rows(), [7]]))
        result = svc.get_missing_tracks_from_db("Artist", "Album")
        assert result["missing_count"] == 1
        assert result["missing_tracks"][0]["title"] == "Gone"
        assert session.statements, "a query must actually run"

    def test_totals_add_up(self, monkeypatch):
        from services.metadata import album_missing_service as svc

        _patch_session(monkeypatch, svc, _FakeSession([self._rows(), [7]]))
        result = svc.get_missing_tracks_from_db("Artist", "Album")
        assert result["library_count"] == 7
        assert result["mb_total"] == 7 + result["missing_count"]

    def test_rejected_rows_are_excluded_in_sql(self, monkeypatch):
        from services.metadata import album_missing_service as svc

        session = _patch_session(monkeypatch, svc, _FakeSession([[], [0]]))
        svc.get_missing_tracks_from_db("Artist", "Album")
        sql = session.statements[0]
        assert "missing_album_tracks" in sql
        assert "ignored" in sql

    def test_an_empty_snapshot_is_zero_not_an_error(self, monkeypatch):
        from services.metadata import album_missing_service as svc

        _patch_session(monkeypatch, svc, _FakeSession([[], [3]]))
        result = svc.get_missing_tracks_from_db("Artist", "Album")
        assert result == {
            "missing_tracks": [], "missing_count": 0, "mb_total": 3, "library_count": 3,
        }

    def test_a_failing_query_degrades_predictably(self, monkeypatch):
        from services.metadata import album_missing_service as svc

        def _boom():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(svc, "db_session", _boom)
        assert svc.get_missing_tracks_from_db("Artist", "Album") == {
            "missing_tracks": [], "missing_count": 0, "mb_total": 0, "library_count": 0,
        }
