"""Similar artists: artist-level refresh, DB-only album path.

Reported shape: *"Should similar artist detection be removed for albums and
only run for artists, then have the albums pages be populated with the
artist results?"* — the architecture already existed (artist-level fetch,
90-day ``artists``-table cache, artist page DB-only, and NO consumer even
reads the album-context copy), but two defects made it behave like
per-album detection:

1. **Checked-empty was not remembered.** The cache write stored NULL for an
   empty result (``if result["lastfm"] else None``), so freshness
   (``bool(list)``) was never satisfied — an artist with NO similar artists
   re-fired Last.fm + the MBID lookup + ListenBrainz **for every album of
   every scan, forever**.
2. **The fetch trigger sat inside per-album ``_run_full_enrichment``** — so
   even the happy path ran its DB read per album, and the failure/empty
   cases ran the network per album.

The fix matches the proposed design exactly: the network refresh runs ONCE
PER ARTIST in the scan runner's prefetch thread (``refresh_similar_artists``);
the per-album path is a pure DB read (``_read_similar_artists``); a checked
result — including an empty one — is persisted as a JSON list and honoured
for 90 days; failures leave the column NULL and retry on the next ARTIST
pass (your single-album-scan clause: the artist lookup runs first, lazily,
and albums read the artist results).
"""

from __future__ import annotations

import json
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _strip_python(src: str) -> str:
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"'''.*?'''", "", src, flags=re.S)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


class _Row:
    def __init__(self, mapping):
        self._mapping = mapping or {}

    def keys(self):
        return self._mapping.keys()

    def __getitem__(self, key):
        return self._mapping[key]


class _FakeDb:
    """Serves the artists-table read, the tracks-mbid read, and records writes."""

    def __init__(self, artist_row=None, mbid_row=None):
        self.artist_row = artist_row
        self.mbid_row = mbid_row
        self.writes: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = dict(params or {})
        self.writes.append((sql, params))
        if "FROM artists" in sql:
            return _Result(_Row(self.artist_row) if self.artist_row is not None else None)
        if "musicbrainz_artistid" in sql:
            return _Result(_Row(self.mbid_row) if self.mbid_row is not None else None)
        return _Result(None)

    @contextmanager
    def __call__(self, *args, **kwargs):
        yield self


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row

    def fetchall(self):
        return []


@pytest.fixture
def album_stage(monkeypatch):
    import services.popularity.stages.album_stage as mod

    return mod, monkeypatch


def _lf_client_factory(calls, result=None, raises=False):
    def _factory(api_key=None, *a, **k):
        class _Client:
            def get_similar_artists(self, artist, limit=10):
                calls.append(artist)
                if raises:
                    raise RuntimeError("lastfm down")
                return result or []

        return _Client()

    return _factory


def _config(monkeypatch, lastfm=True):
    from helpers import config_helpers

    cfg = {
        "api_integrations": {
            "lastfm": {"enabled": lastfm, "api_key": "k"} if lastfm else {"enabled": False},
            "discogs": {"enabled": False, "token": ""},
        }
    }
    monkeypatch.setattr(config_helpers, "get_config", lambda: cfg)


# ---------------------------------------------------------------------------
# 1. The album path is a pure DB read
# ---------------------------------------------------------------------------


class TestAlbumPathIsReadOnly:
    def test_the_read_never_touches_the_network(self, album_stage, monkeypatch):
        mod, mp = album_stage
        db = _FakeDb(artist_row={
            "similar_artists_lastfm": json.dumps(["A", "B"]),
            "similar_artists_listenbrainz": None,
            "similar_artists_last_updated": datetime.now(timezone.utc).isoformat(),
        })
        mp.setattr(mod, "db_session", db)
        # Any network client construction inside the read is a bug.
        import api_clients.lastfm as lf_mod
        import api_clients.listenbrainz as lb_mod

        mp.setattr(lf_mod, "LastFmClient", lambda *a, **k: pytest.fail("network in the album read"))
        mp.setattr(lb_mod, "ListenBrainzClient", lambda *a, **k: pytest.fail("network in the album read"))

        out = mod._read_similar_artists("Band")
        assert out["lastfm"] == ["A", "B"]
        assert out["listenbrainz"] is None, "NULL column means NEVER CHECKED, not empty"

    def test_a_null_column_reads_as_none_and_an_empty_list_as_answer(self, album_stage, monkeypatch):
        mod, mp = album_stage
        db = _FakeDb(artist_row={
            "similar_artists_lastfm": "[]",            # checked, genuinely nothing
            "similar_artists_listenbrainz": None,      # never checked
            "similar_artists_last_updated": datetime.now(timezone.utc).isoformat(),
        })
        mp.setattr(mod, "db_session", db)
        out = mod._read_similar_artists("Band")
        assert out["lastfm"] == [], "an empty list is a remembered ANSWER"
        assert out["listenbrainz"] is None, "NULL is an unchecked source"


# ---------------------------------------------------------------------------
# 2. The refresh: checked-empty remembered, failures retried per ARTIST
# ---------------------------------------------------------------------------


class TestRefreshSemantics:
    def _run(self, mp, mod, db, lf_calls, *, lf_result=None, lf_raises=False, mbid=True):
        _config(mp, lastfm=True)
        mp.setattr(mod, "db_session", db)
        import api_clients.lastfm as lf_mod

        mp.setattr(lf_mod, "LastFmClient", _lf_client_factory(lf_calls, lf_result, lf_raises))
        if mbid:
            db.mbid_row = {"mbid": "artist-mbid-1"}
        return mod.refresh_similar_artists("Band", {})

    def test_a_genuinely_empty_artist_is_remembered(self, album_stage, monkeypatch):
        mod, mp = album_stage
        db = _FakeDb()  # no artists row yet
        lf_calls: list = []

        self._run(mp, mod, db, lf_calls, lf_result=[])
        assert lf_calls == ["Band"], "the first artist pass fetches"
        # The write stored "[]" — a checked empty — not NULL.
        lf_writes = [p for s, p in db.writes if "similar_artists_lastfm = :lf" in s]
        assert lf_writes, "the cache must be written"
        assert lf_writes[-1]["lf"] == "[]", (
            "an empty result persisted as NULL re-fires the network for "
            "every album of every scan — the reported defect"
        )

    def test_the_second_artist_pass_serves_the_empty_answer(self, album_stage, monkeypatch):
        mod, mp = album_stage
        lf_calls: list = []
        fresh = datetime.now(timezone.utc).isoformat()
        db = _FakeDb(artist_row={
            "similar_artists_lastfm": "[]",
            "similar_artists_listenbrainz": "[]",
            "similar_artists_last_updated": fresh,
        })
        out = self._run(mp, mod, db, lf_calls)
        assert lf_calls == [], "checked empties within the window must cost NOTHING"
        assert out == {"lastfm": [], "listenbrainz": []}

    def test_a_failure_is_not_remembered_as_an_answer(self, album_stage, monkeypatch):
        mod, mp = album_stage
        db = _FakeDb(artist_row={
            "similar_artists_lastfm": None,
            "similar_artists_listenbrainz": "[]",
            "similar_artists_last_updated": datetime.now(timezone.utc).isoformat(),
        })
        lf_calls: list = []
        self._run(mp, mod, db, lf_calls, lf_raises=True)
        # The failed source stays NULL (unchecked) — NOT "[]".
        lf_writes = [p for s, p in db.writes if "similar_artists_lastfm = :lf" in s]
        assert lf_writes and lf_writes[-1]["lf"] is None, (
            "a transient outage must not be remembered as 'no similar artists'"
        )

    def test_a_failure_retries_on_the_next_artist_pass(self, album_stage, monkeypatch):
        mod, mp = album_stage
        db = _FakeDb(artist_row={
            "similar_artists_lastfm": None,
            "similar_artists_listenbrainz": "[]",
            "similar_artists_last_updated": datetime.now(timezone.utc).isoformat(),
        })
        lf_calls: list = []
        self._run(mp, mod, db, lf_calls, lf_raises=True)
        self._run(mp, mod, db, lf_calls, lf_raises=True)
        assert len(lf_calls) == 2, (
            "NULL means retry — once per ARTIST pass, never per album"
        )

    def test_fresh_non_empty_answers_skip_the_network(self, album_stage, monkeypatch):
        mod, mp = album_stage
        fresh = datetime.now(timezone.utc).isoformat()
        db = _FakeDb(artist_row={
            "similar_artists_lastfm": json.dumps(["A"]),
            "similar_artists_listenbrainz": json.dumps(["X"]),
            "similar_artists_last_updated": fresh,
        })
        lf_calls: list = []
        out = self._run(mp, mod, db, lf_calls)
        assert lf_calls == []
        assert out == {"lastfm": ["A"], "listenbrainz": ["X"]}

    def test_a_singles_pass_touches_nothing(self, album_stage, monkeypatch):
        mod, mp = album_stage

        def _boom(*a, **k):
            raise AssertionError("the singles pass must not even read the cache")

        mp.setattr(mod, "db_session", _boom)
        out = mod.refresh_similar_artists("Band", {"singles_only": True})
        assert out == {"lastfm": [], "listenbrainz": []}

    def test_an_unavailable_artist_mbid_leaves_listenbrainz_unchecked(self, album_stage, monkeypatch):
        mod, mp = album_stage
        db = _FakeDb(artist_row={
            "similar_artists_lastfm": None,
            "similar_artists_listenbrainz": None,
            "similar_artists_last_updated": None,
        })
        _config(mp, lastfm=True)
        mp.setattr(mod, "db_session", db)
        import api_clients.lastfm as lf_mod
        import api_clients.listenbrainz as lb_mod

        mp.setattr(lf_mod, "LastFmClient", _lf_client_factory([], result=[{"name": "A"}]))
        mp.setattr(lb_mod, "ListenBrainzClient", lambda *a, **k: pytest.fail("no mbid -> no LB call"))
        # The mbid lookups return nothing: the artist is simply not in MB.
        import services.enrichment.musicbrainz_persistence_service as mbp

        mp.setattr(mbp, "lookup_and_save_artist_mbid", lambda *a, **k: None)

        mod.refresh_similar_artists("Band", {})
        lb_writes = [p for s, p in db.writes if "similar_artists_listenbrainz = :lb" in s]
        assert lb_writes and lb_writes[-1]["lb"] is None, (
            'without an MBID ListenBrainz stays NULL ("never checked") — an '
            "artist absent from MB must not be remembered as having no "
            "ListenBrainz similar artists"
        )


# ---------------------------------------------------------------------------
# 3. The wiring: runner refreshes per artist, albums only read
# ---------------------------------------------------------------------------


class TestTheWiring:
    @classmethod
    def _album_stage_src(cls) -> str:
        path = REPO_ROOT / "services" / "popularity" / "stages" / "album_stage.py"
        return _strip_python(path.read_text(encoding="utf-8"))

    @classmethod
    def _runner_src(cls) -> str:
        path = REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py"
        return _strip_python(path.read_text(encoding="utf-8"))

    def test_the_runner_refreshes_once_per_artist(self):
        src = self._runner_src()
        assert "refresh_similar_artists(artist, options)" in src, (
            "the artist-change prefetch thread is the ONE network trigger"
        )

    def test_the_album_path_reads_only(self):
        src = self._album_stage_src()
        idx = src.index("full.similar_artists")
        window = src[idx: idx + 500]
        assert "_read_similar_artists(artist)" in window
        assert "refresh_similar_artists(" not in window, (
            "the per-album path must never fetch"
        )

    def test_the_old_per_album_fetch_name_is_gone(self):
        assert "_fetch_similar_artists" not in self._album_stage_src()


# ---------------------------------------------------------------------------
# 4. Controls
# ---------------------------------------------------------------------------


class TestControls:
    def test_the_refresh_still_fills_the_artists_table(self, album_stage, monkeypatch):
        mod, mp = album_stage
        db = _FakeDb()
        lf_calls: list = []
        _config(mp, lastfm=True)
        mp.setattr(mod, "db_session", db)
        import api_clients.lastfm as lf_mod

        mp.setattr(lf_mod, "LastFmClient", _lf_client_factory(lf_calls, result=[{"name": "A"}, {"name": "B"}]))
        db.mbid_row = {"mbid": "m1"}
        import api_clients.listenbrainz as lb_mod

        mp.setattr(lb_mod, "ListenBrainzClient", lambda *a, **k: type("C", (), {
            "get_similar_artists": lambda self, m, limit=10: [{"name": "X"}],
        })())

        out = mod.refresh_similar_artists("Band", {})
        assert out == {"lastfm": ["A", "B"], "listenbrainz": ["X"]}
        write = [p for s, p in db.writes if "INSERT INTO artists" in s][-1]
        assert json.loads(write["lf"]) == ["A", "B"]
        assert json.loads(write["lb"]) == ["X"]
        assert write["updated"], "the window timestamp is stamped on a real check"

    def test_a_disabled_lastfm_integration_costs_nothing(self, album_stage, monkeypatch):
        mod, mp = album_stage
        db = _FakeDb(artist_row={
            "similar_artists_lastfm": None,
            "similar_artists_listenbrainz": "[]",
            "similar_artists_last_updated": datetime.now(timezone.utc).isoformat(),
        })
        _config(mp, lastfm=False)
        mp.setattr(mod, "db_session", db)
        import api_clients.lastfm as lf_mod

        mp.setattr(lf_mod, "LastFmClient", lambda *a, **k: pytest.fail("disabled integration must not construct a client"))

        mod.refresh_similar_artists("Band", {})
        lf_writes = [p for s, p in db.writes if "similar_artists_lastfm = :lf" in s]
        assert lf_writes and lf_writes[-1]["lf"] is None
