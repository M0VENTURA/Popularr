"""Playlist index sync — enumeration, stamps, pruning, and the artist query.

The load-bearing behaviours here are not "does it write rows":

* an UNCHANGED playlist must cost NO request (that is the whole reason the sync
  is affordable), so the fake client counts calls;
* the prune must NOT run against a partial view — a Navidrome that is down
  returns no playlists, and pruning on that would wipe the index and leave the
  artist page permanently empty;
* the artist match must use the album-artist expression the artist page itself
  uses, or an artist's own page misses the playlists its tracks are in.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.engine import db_session
from db.repositories import playlists as repo
from services.playlists import playlist_index_service as svc


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class FakeClient:
    """Stands in for ``NavidromeClient`` for the two methods the sync uses."""

    def __init__(self, *, playlists=None, details=None, list_error=None, fetch_error=None):
        self._playlists = playlists if playlists is not None else []
        self._details = details or {}
        self._list_error = list_error
        self._fetch_error = fetch_error
        self.list_calls = 0
        self.fetch_calls: list[str] = []

    def fetch_all_playlists(self):
        self.list_calls += 1
        if self._list_error:
            raise self._list_error
        return list(self._playlists)

    def fetch_playlist(self, playlist_id):
        self.fetch_calls.append(str(playlist_id))
        if self._fetch_error:
            raise self._fetch_error
        return dict(self._details.get(str(playlist_id), {}))

    def find_playlist_by_name(self, name):
        for item in self._playlists:
            if str(item.get("name") or "") == str(name):
                return {"id": item.get("id")}
        return None


def _remote(playlist_id: str, name: str, *, song_count: int = 1, changed: str = "c1",
            tracks: list[str] | None = None, **extra) -> tuple[dict, dict]:
    """One Navidrome playlist: the list entry plus its detail payload."""
    entry = {
        "id": playlist_id, "name": name, "songCount": song_count, "changed": changed,
        "owner": extra.pop("owner", "admin"),
        "public": extra.pop("public", True),
        "type": extra.pop("type", "regular"),
        "comment": extra.pop("comment", ""),
        **extra,
    }
    detail = {"tracks": [{"id": t} for t in (tracks or [])]}
    return entry, detail


@pytest.fixture(autouse=True)
def _clean_index():
    """Clear the index tables AND ``tracks`` before each test.

    The in-memory engine is shared for the whole session and the schema is only
    ever re-created with ``checkfirst``, so rows written by one test are still
    visible in the next. ``tracks`` is cleared too, and deliberately: the artist
    match asserts on an EXACT id list, so a leftover row from another test with
    the same artist name turns `assert ids == ["v1"]` into a list of three and
    fails for a reason that has nothing to do with the code under test. Clearing
    makes each test's fixture complete, instead of relying on every test picking
    a unique artist name forever.
    """
    with db_session() as session:
        session.execute(text("DELETE FROM playlist_entries"))
        session.execute(text("DELETE FROM playlists"))
        session.execute(text("DELETE FROM tracks"))
    yield


@pytest.fixture(autouse=True)
def _no_file_playlists(monkeypatch):
    """Default: no .nsp/.m3u on disk.

    `_enumerate` imports `list_nsp_playlists` INSIDE the function, so patching
    it on its own module bites. Without this, the tests would read whatever the
    real Playlists directory happens to contain.
    """
    monkeypatch.setattr(
        "services.playlists.playlist_service.list_nsp_playlists", lambda: []
    )


def _seed_tracks(rows: list[dict]) -> None:
    with db_session() as session:
        for row in rows:
            session.execute(
                text(
                    "INSERT INTO tracks (id, artist, album_artist, album, title, stars) "
                    "VALUES (:id, :artist, :album_artist, :album, :title, :stars) "
                    "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title"
                ),
                row,
            )


# ---------------------------------------------------------------------------
# Enumeration + stamping
# ---------------------------------------------------------------------------

class TestSyncWritesTheIndex:
    def test_a_new_playlist_is_fetched_and_indexed(self):
        entry, detail = _remote("p1", "Road Trip", song_count=2, tracks=["t1", "t2"])
        client = FakeClient(playlists=[entry], details={"p1": detail})

        stats = svc.sync_playlist_index([client])

        assert stats["changed"] == 1
        assert stats["entries"] == 2
        assert client.fetch_calls == ["p1"]
        assert repo.index_sizes() == {"playlists": 1, "entries": 2}

    def test_metadata_is_stored_from_the_listing(self):
        entry, detail = _remote(
            "p2", "Focus", song_count=1, tracks=["t1"], owner="amy", public=False,
        )
        svc.sync_playlist_index([FakeClient(playlists=[entry], details={"p2": detail})])

        with db_session() as session:
            row = session.execute(
                text("SELECT name, owner, is_public, song_count, comment, source "
                     "FROM playlists WHERE playlist_id = 'p2'")
            ).mappings().one()
        assert row["name"] == "Focus"
        assert row["owner"] == "amy"
        assert row["is_public"] in (False, 0)
        assert row["song_count"] == 1
        assert row["source"] == "navidrome"

    def test_an_unchanged_playlist_costs_no_request(self):
        """The fast path — the reason the index is affordable at all.

        Asserted on the CALL COUNT, not on the row count: a second sync that
        re-fetched everything would still leave the same rows behind, so a
        row-based assertion cannot tell the two apart.
        """
        entry, detail = _remote("p3", "Steady", song_count=1, tracks=["t1"])
        client = FakeClient(playlists=[entry], details={"p3": detail})

        svc.sync_playlist_index([client])
        first_calls = len(client.fetch_calls)

        stats = svc.sync_playlist_index([client])

        assert stats["skipped"] == 1
        assert stats["changed"] == 0
        assert len(client.fetch_calls) == first_calls, (
            "an unchanged playlist was re-fetched"
        )

    def test_a_moved_stamp_re_indexes_the_playlist(self):
        entry, detail = _remote("p4", "Drifting", song_count=1, tracks=["t1"])
        client = FakeClient(playlists=[entry], details={"p4": detail})
        svc.sync_playlist_index([client])

        # Same id, new stamp, one more track.
        entry2, detail2 = _remote("p4", "Drifting", song_count=2,
                                  changed="c2", tracks=["t1", "t2"])
        client2 = FakeClient(playlists=[entry2], details={"p4": detail2})
        stats = svc.sync_playlist_index([client2])

        assert stats["changed"] == 1
        assert client2.fetch_calls == ["p4"]
        assert repo.index_sizes()["entries"] == 2

    def test_a_playlist_with_no_changed_stamp_is_not_stuck_unchanged(self):
        """Navidrome omitting `changed` must not read as "never moved".

        The stamp falls back to the song count. Stamping such a playlist as ""
        would make it compare equal to a stored "" forever and be skipped for
        good, so a growing playlist would never update.
        """
        entry, detail = _remote("p5", "NoStamp", song_count=1, tracks=["t1"])
        entry.pop("changed")
        client = FakeClient(playlists=[entry], details={"p5": detail})
        svc.sync_playlist_index([client])

        entry2, detail2 = _remote("p5", "NoStamp", song_count=3, tracks=["t1", "t2", "t3"])
        entry2.pop("changed")
        client2 = FakeClient(playlists=[entry2], details={"p5": detail2})
        stats = svc.sync_playlist_index([client2])

        assert stats["changed"] == 1
        assert repo.index_sizes()["entries"] == 3


# ---------------------------------------------------------------------------
# File-backed playlists
# ---------------------------------------------------------------------------

class TestFileBackedPlaylists:
    def test_a_file_playlist_wins_over_a_same_named_remote_one(self, monkeypatch):
        """Mirrors `api_playlists_all`'s merge, so the two pages agree."""
        monkeypatch.setattr(
            "services.playlists.playlist_service.list_nsp_playlists",
            lambda: [{
                "name": "Loved Tracks", "file_name": "Loved Tracks.nsp",
                "file_path": "/tmp/Loved Tracks.nsp", "comment": "", "rules": {},
                "track_count": 1, "rule_based": False, "kind": "nsp",
            }],
        )
        monkeypatch.setattr(svc, "_file_stamp", lambda _p: "stamp1")
        monkeypatch.setattr(
            svc, "_track_ids_for",
            lambda entry, clients: (["fake-from-file"], 0),
        )
        entry, detail = _remote("nav-1", "Loved Tracks", song_count=1, tracks=["nav-track"])

        svc.sync_playlist_index([FakeClient(playlists=[entry], details={"nav-1": detail})])

        with db_session() as session:
            ids = [r[0] for r in session.execute(
                text("SELECT playlist_id FROM playlists")
            ).fetchall()]
        assert ids == ["file::Loved Tracks.nsp"], (
            "the remote duplicate should have been skipped, not indexed twice"
        )

    def test_the_indexed_id_scheme_matches_the_playlists_page(self, monkeypatch):
        """`file::<file_name>` — the scheme routes/playlists.py already uses."""
        monkeypatch.setattr(
            "services.playlists.playlist_service.list_nsp_playlists",
            lambda: [{
                "name": "Mix", "file_name": "Mix.m3u", "file_path": "/tmp/Mix.m3u",
                "comment": "Generated playlist", "rules": {}, "track_count": 0,
                "rule_based": False, "kind": "m3u",
            }],
        )
        monkeypatch.setattr(svc, "_file_stamp", lambda _p: "s")
        monkeypatch.setattr(svc, "_track_ids_for", lambda entry, clients: ([], 0))

        svc.sync_playlist_index([])

        assert "file::Mix.m3u" in repo.stored_changed_map()


class TestFileStamp:
    def test_a_missing_file_stamps_empty(self, tmp_path):
        assert svc._file_stamp(str(tmp_path / "nope.nsp")) == ""

    def test_a_present_file_stamps_and_reacts_to_a_size_change(self, tmp_path):
        path = tmp_path / "mix.nsp"
        path.write_text('{"name": "Mix"}', encoding="utf-8")
        first = svc._file_stamp(str(path))
        assert first

        path.write_text('{"name": "Mix", "tracks": []}', encoding="utf-8")
        assert svc._file_stamp(str(path)) != first


# ---------------------------------------------------------------------------
# Pruning — and when it must NOT happen
# ---------------------------------------------------------------------------

class TestPrune:
    def test_a_playlist_gone_from_the_source_is_pruned(self):
        entry, detail = _remote("gone", "Gone", song_count=1, tracks=["t1"])
        svc.sync_playlist_index([FakeClient(playlists=[entry], details={"gone": detail})])
        assert repo.index_sizes()["playlists"] == 1

        stats = svc.sync_playlist_index([FakeClient(playlists=[], details={})])

        assert stats["pruned"] == 1
        assert repo.index_sizes() == {"playlists": 0, "entries": 0}

    def test_a_failed_listing_does_not_prune_the_index(self):
        """⚠️ THE IMPORTANT ONE.

        A Navidrome that is down (or misconfigured) returns no playlists. If the
        prune ran on that, a transient outage would empty the index and the
        artist page's section would go blank until someone re-scanned — the
        outage would become data loss. An unenumerable source is a SKIP.
        """
        entry, detail = _remote("keep", "Keep", song_count=1, tracks=["t1"])
        svc.sync_playlist_index([FakeClient(playlists=[entry], details={"keep": detail})])
        assert repo.index_sizes()["playlists"] == 1

        broken = FakeClient(list_error=RuntimeError("connection refused"))
        stats = svc.sync_playlist_index([broken])

        assert stats["errors"] == 1
        assert stats["pruned"] == 0
        assert repo.index_sizes()["playlists"] == 1, (
            "a transient Navidrome failure must not delete the index"
        )

    def test_one_broken_server_does_not_stop_a_working_one(self):
        good_entry, good_detail = _remote("g1", "Good", song_count=1, tracks=["t1"])
        good = FakeClient(playlists=[good_entry], details={"g1": good_detail})
        broken = FakeClient(list_error=RuntimeError("down"))

        stats = svc.sync_playlist_index([broken, good])

        assert stats["errors"] == 1
        assert repo.index_sizes()["playlists"] == 1

    def test_a_partial_prune_is_skipped_even_when_some_playlists_were_seen(self):
        """The guard is "every server listed", not "we saw at least one playlist"."""
        entry, detail = _remote("a1", "A", song_count=1, tracks=["t1"])
        both = FakeClient(playlists=[entry], details={"a1": detail})
        svc.sync_playlist_index([both])

        mixed_entry, mixed_detail = _remote("b1", "B", song_count=1, tracks=["t2"])
        good = FakeClient(playlists=[mixed_entry], details={"b1": mixed_detail})
        broken = FakeClient(list_error=RuntimeError("down"))
        stats = svc.sync_playlist_index([good, broken])

        # B was indexed, but A must survive because the broken server might
        # have been the one that owns it.
        assert stats["pruned"] == 0
        assert "a1" in repo.stored_changed_map()
        assert "b1" in repo.stored_changed_map()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class TestUnresolvedReporting:
    def test_entries_that_do_not_resolve_are_counted(self):
        """A playlist whose ids are all stale must be visibly partial."""
        entry, detail = _remote("p9", "Stale", song_count=5, tracks=["x1", "x2"])
        client = FakeClient(playlists=[entry], details={"p9": detail})

        stats = svc.sync_playlist_index([client])

        assert stats["unresolved"] == 3
        assert stats["entries"] == 2

    def test_a_fetch_failure_is_counted_not_raised(self):
        entry, _detail = _remote("p10", "Broken", song_count=2)
        client = FakeClient(playlists=[entry], details={}, fetch_error=RuntimeError("boom"))

        stats = svc.sync_playlist_index([client])

        assert stats["unresolved"] == 2
        assert repo.index_sizes()["entries"] == 0


# ---------------------------------------------------------------------------
# The artist page's query
# ---------------------------------------------------------------------------

class TestPlaylistsForArtist:
    def _index(self, monkeypatch, *, name: str, track_ids: list[str], playlist_id: str = "p1"):
        monkeypatch.setattr(svc, "_artist_track_ids", lambda artist, max_tracks: track_ids)
        entry, detail = _remote(playlist_id, name, song_count=len(track_ids),
                                tracks=track_ids)
        svc.sync_playlist_index([FakeClient(playlists=[entry], details={playlist_id: detail})])

    def test_it_groups_tracks_under_their_playlist(self, monkeypatch):
        _seed_tracks([
            {"id": "n1", "artist": "Nirvana", "album_artist": "Nirvana",
             "album": "Nevermind", "title": "Come As You Are", "stars": 5},
            {"id": "n2", "artist": "Nirvana", "album_artist": "Nirvana",
             "album": "Nevermind", "title": "Lithium", "stars": 5},
        ])
        self._index(monkeypatch, name="Grunge - Top Tracks", track_ids=["n1", "n2"],
                    playlist_id="g1")

        result = svc.playlists_for_artist("Nirvana")

        assert len(result) == 1
        assert result[0]["name"] == "Grunge - Top Tracks"
        assert [t["title"] for t in result[0]["tracks"]] == ["Come As You Are", "Lithium"]

    def test_positions_are_kept_in_playlist_order(self, monkeypatch):
        _seed_tracks([
            {"id": "m1", "artist": "A", "album_artist": "A", "album": "Al",
             "title": "First", "stars": 5},
            {"id": "m2", "artist": "A", "album_artist": "A", "album": "Al",
             "title": "Second", "stars": 4},
        ])
        self._index(monkeypatch, name="Ordered", track_ids=["m1", "m2"], playlist_id="o1")

        result = svc.playlists_for_artist("A")
        assert [t["position"] for t in result[0]["tracks"]] == [1, 2]

    def test_an_artist_with_no_indexed_playlists_is_empty(self, monkeypatch):
        self._index(monkeypatch, name="Other", track_ids=[], playlist_id="z1")
        assert svc.playlists_for_artist("Nobody") == []

    def test_a_blank_artist_is_empty_without_querying(self):
        assert svc.playlists_for_artist("") == []
        assert svc.playlists_for_artist("   ") == []


class TestArtistMatchUsesTheAlbumArtistExpression:
    def test_a_track_whose_album_artist_differs_is_still_found(self):
        """The same expression the artist page uses to find its albums.

        A guest-credited track ("Nirvana feat. X") has its own `artist` but an
        `album_artist` of the host. Matching the bare `artist` column would put
        that track's playlists on the GUEST's page and lose them from the host's.
        """
        _seed_tracks([
            {"id": "v1", "artist": "Nirvana feat. Someone", "album_artist": "Nirvana",
             "album": "Nevermind", "title": "Territorial Pissings", "stars": 5},
        ])
        ids = svc._artist_track_ids("Nirvana", max_tracks=50)
        assert ids == ["v1"]

    def test_the_match_is_case_insensitive(self):
        _seed_tracks([
            {"id": "v2", "artist": "acdc", "album_artist": "Acdc",
             "album": "Back in Black", "title": "Hells Bells", "stars": 5},
        ])
        assert svc._artist_track_ids("ACDC", max_tracks=50) == ["v2"]

    def test_it_falls_back_to_the_artist_column_when_album_artist_is_blank(self):
        _seed_tracks([
            {"id": "v3", "artist": "Solo", "album_artist": "",
             "album": "Al", "title": "One", "stars": 5},
        ])
        assert svc._artist_track_ids("Solo", max_tracks=50) == ["v3"]


# ---------------------------------------------------------------------------
# The scan hook
# ---------------------------------------------------------------------------

class TestTheScanRunsTheSync:
    """Nothing indexes anything unless the scan calls the sync.

    Asserted on SOURCE because driving `finalise_scan` end-to-end needs a whole
    album's scan results. The invariant is a wiring one — "is the sync reachable
    from a scan at all" — and without it the artist page would render its empty
    state forever with every unit test above still green.
    """

    def _source(self) -> str:
        from pathlib import Path

        return Path("services/popularity/stages/finalise_stage.py").read_text(
            encoding="utf-8"
        )

    def test_the_global_rebuild_calls_it(self):
        source = self._source()
        assert "from services.playlists.playlist_index_service import sync_playlist_index" in source
        assert "sync_playlist_index(_navidrome_clients())" in source

    def test_it_runs_after_the_builders(self):
        """The index must record what this pass CREATED, not last pass's contents."""
        source = self._source()
        marker = "sync_playlist_index(_navidrome_clients())"
        assert source.index("_create_new_music_playlist()") < source.index(marker)
        assert source.index("_create_genre_top_track_playlists()") < source.index(marker)

    def test_it_uses_the_server_scoped_client_factory(self):
        """⚠️ Not a per-account source.

        Playlists belong to the Navidrome SERVER and are owned by the account
        that created them. Resolving per-account clients here would reintroduce
        the duplicate-playlist ping-pong (`_navidrome_clients`' own docstring).
        """
        source = self._source()
        start = source.index("sync_playlist_index(_navidrome_clients())")
        window = source[start - 200:start + 40]
        assert "_navidrome_clients()" in window
        assert "get_navidrome_users" not in window

    def test_a_failure_cannot_break_the_scan(self):
        """The index feeds a display section; it must not cost a scan its work."""
        source = self._source()
        start = source.index("from services.playlists.playlist_index_service import sync_playlist_index")
        window = source[start:start + 900]
        assert "except Exception" in window
