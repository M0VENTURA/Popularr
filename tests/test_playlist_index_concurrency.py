"""Playlist index sync: concurrency, one listing, and unreadable-playlist safety.

Three defects in ``sync_playlist_index``, all measured against the reported run:

    playlists=615 skipped=549 changed=66 entries=79434

That run spent **5 m 36 s** inside the sync. Reading one changed playlist costs
one ``getPlaylist`` round trip, and Navidrome has to build/serialise the whole
entry list (~1,200 tracks each), so:

* **it fetched 66 playlists strictly one after another** — no concurrency, so the
  cost was the sum rather than a fraction of it;
* **it re-listed every playlist to answer a name lookup** — ``_track_ids_for``
  called ``client.find_playlist_by_name(name)`` for rule-based ``.nsp`` files,
  and that calls ``fetch_all_playlists()``, re-downloading all 615 playlists'
  metadata for ONE name;
* **a failed fetch was recorded as an empty playlist.** ``fetch_playlist``
  swallows every error and returns ``{}``, and the old loop treated that as "0
  tracks", then wrote it — which DELETES every indexed row for that playlist.

The third is a correctness bug, not a speed one, and it is the most dangerous of
the three.
"""
from __future__ import annotations

import threading
import time

import pytest

from services.playlists import playlist_index_service as svc


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeClient:
    """Duck-typed NavidromeClient for the index sync.

    Records every call so the tests can assert that things did NOT happen —
    which is the whole point of the listing-reuse and unreadable-playlist rules.
    """

    def __init__(self, playlists=None, details=None, *, fail_ids=(), delay=0.0):
        self._playlists = list(playlists or [])
        self._details = dict(details or {})
        self._fail_ids = set(fail_ids)
        self._delay = delay
        self.calls: list[str] = []
        self.find_by_name_calls: list[str] = []
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def fetch_all_playlists(self):
        with self._lock:
            self.calls.append("fetch_all_playlists")
        return list(self._playlists)

    def find_playlist_by_name(self, name):
        with self._lock:
            self.calls.append(f"find_playlist_by_name:{name}")
            self.find_by_name_calls.append(name)
        wanted = str(name or "").strip().lower()
        for candidate in self._playlists:
            if str(candidate.get("name") or "").strip().lower() == wanted:
                return candidate
        return None

    def fetch_playlist(self, playlist_id, timeout=60):
        with self._lock:
            self.calls.append(f"fetch_playlist:{playlist_id}")
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self._delay:
                time.sleep(self._delay)
            if playlist_id in self._fail_ids:
                # Mirrors the client: errors are swallowed into an empty dict.
                return {}
            return dict(self._details.get(playlist_id) or {})
        finally:
            with self._lock:
                self.in_flight -= 1


def _nav(playlist_id: str, name: str, *, song_count: int = 2, changed: str = "1"):
    return {
        "id": playlist_id,
        "name": name,
        "songCount": song_count,
        "changed": changed,
        "type": "regular",
    }


def _detail(playlist_id: str, ids: list[str]):
    return {
        "id": playlist_id,
        "name": playlist_id,
        "tracks": [{"id": i, "title": f"t-{i}"} for i in ids],
    }


class _Repo:
    """Records every write, so a test can prove one did NOT happen."""

    def __init__(self, stored=None):
        self.stored = dict(stored or {})
        self.written: list[dict] = []
        self.pruned = 0

    def stored_changed_map(self):
        return dict(self.stored)

    def replace_playlist(self, **kwargs):
        self.written.append(kwargs)
        return {"entries": len(kwargs.get("track_ids") or [])}

    def delete_playlists_not_in(self, playlist_ids):
        self.pruned += 1
        return 0


@pytest.fixture
def repo(monkeypatch):
    fake = _Repo()
    monkeypatch.setattr(svc, "_repo", fake, raising=False)
    return fake


@pytest.fixture
def no_nsp(monkeypatch):
    """No file-backed playlists unless a test asks for them."""
    monkeypatch.setattr(
        "services.playlists.playlist_service.list_nsp_playlists", lambda: []
    )


# ===========================================================================
# 1. Bounded concurrency
# ===========================================================================
class TestChangedPlaylistsAreFetchedConcurrently:
    def test_every_changed_playlist_is_prefetched(self, no_nsp):
        client = _FakeClient(
            playlists=[_nav(f"p{i}", f"P{i}") for i in range(6)],
            details={f"p{i}": _detail(f"p{i}", ["a", "b"]) for i in range(6)},
        )
        entries = [
            {"playlist_id": f"p{i}", "source": "navidrome", "name": f"P{i}", "changed": "1", "song_count": 2}
            for i in range(6)
        ]

        prefetched = svc._prefetch_navidrome_details(entries, [client])

        assert sorted(prefetched) == [f"p{i}" for i in range(6)]

    def test_the_pool_is_bounded(self, no_nsp, monkeypatch):
        """⚠️ Unbounded would mean one thread per playlist (hundreds)."""
        monkeypatch.setattr(svc, "_fetch_worker_count", lambda: 3)
        client = _FakeClient(delay=0.02)
        entries = [
            {"playlist_id": f"p{i}", "source": "navidrome", "name": f"P{i}", "changed": "1"}
            for i in range(12)
        ]
        client._details = {f"p{i}": _detail(f"p{i}", ["a"]) for i in range(12)}

        svc._prefetch_navidrome_details(entries, [client])

        assert client.max_in_flight <= 3, (
            f"{client.max_in_flight} concurrent fetches exceeded the configured 3"
        )
        assert client.max_in_flight > 1, (
            "the fetches never overlapped, so there was no concurrency at all"
        )

    def test_the_worker_count_is_clamped_from_env(self, monkeypatch):
        monkeypatch.delenv("PLAYLIST_INDEX_FETCH_WORKERS", raising=False)
        assert svc._fetch_worker_count() == svc._DEFAULT_FETCH_WORKERS

        monkeypatch.setenv("PLAYLIST_INDEX_FETCH_WORKERS", "0")
        assert svc._fetch_worker_count() == 1

        monkeypatch.setenv("PLAYLIST_INDEX_FETCH_WORKERS", "9999")
        assert svc._fetch_worker_count() == svc._MAX_FETCH_WORKERS

        monkeypatch.setenv("PLAYLIST_INDEX_FETCH_WORKERS", "not-a-number")
        assert svc._fetch_worker_count() == svc._DEFAULT_FETCH_WORKERS

    def test_only_navidrome_entries_are_prefetched(self, no_nsp):
        """File-backed playlists read a local file and a DB query.

        Those must stay on the calling thread — the in-memory test engine hands
        the whole suite ONE connection, and concurrent DB access from workers
        would break it (and contend on SQLite).
        """
        client = _FakeClient(details={"p1": _detail("p1", ["a"])})
        entries = [
            {"playlist_id": "p1", "source": "navidrome", "name": "P1", "changed": "1"},
            {"playlist_id": "file::x.nsp", "source": "file", "name": "X", "changed": "1"},
        ]

        prefetched = svc._prefetch_navidrome_details(entries, [client])

        assert list(prefetched) == ["p1"]
        assert not any("file::" in c for c in client.calls)

    def test_a_failed_fetch_is_absent_not_empty(self, no_nsp):
        """Absent lets the caller fall back to a live fetch; '' would not."""
        client = _FakeClient(fail_ids={"p2"})
        client._details = {"p1": _detail("p1", ["a"])}
        entries = [
            {"playlist_id": pid, "source": "navidrome", "name": pid, "changed": "1"}
            for pid in ("p1", "p2")
        ]

        prefetched = svc._prefetch_navidrome_details(entries, [client])

        assert "p1" in prefetched
        assert "p2" not in prefetched

    def test_a_payload_for_the_wrong_playlist_is_ignored(self, no_nsp):
        """Storing it would attach another playlist's tracks to this id."""
        client = _FakeClient()
        client._details = {"p1": {"id": "SOMETHING-ELSE", "tracks": [{"id": "a"}]}}
        entries = [{"playlist_id": "p1", "source": "navidrome", "name": "P1", "changed": "1"}]

        assert svc._prefetch_navidrome_details(entries, [client]) == {}


# ===========================================================================
# 2. One listing per sync
# ===========================================================================
class TestThePlaylistListingIsFetchedOnce:
    def test_enumerate_returns_the_raw_listings(self, no_nsp):
        client = _FakeClient(playlists=[_nav("p1", "P1")])

        entries, errors, listings = svc._enumerate([client])

        assert errors == 0
        assert len(entries) == 1
        assert listings == [[_nav("p1", "P1")]], (
            "the listing was not kept, so rule-based .nsp lookups would re-fetch it"
        )

    def test_a_failed_server_yields_an_empty_listing_slot(self, no_nsp):
        class _Boom(_FakeClient):
            def fetch_all_playlists(self):
                raise RuntimeError("server down")

        entries, errors, listings = svc._enumerate([_Boom()])

        assert errors == 1
        assert entries == []
        assert listings == [[]], "listings must stay aligned with the client list"

    def test_a_rule_based_nsp_resolves_without_relisting(self, no_nsp, monkeypatch, tmp_path):
        """The reported cost: one ``getPlaylists`` per rule-based file."""
        # A rule-based .nsp: no embedded tracks, only rules.
        nsp = tmp_path / "Smart.nsp"
        nsp.write_text('{"name": "Smart", "rules": {"genre": "Metal"}}', encoding="utf-8")

        client = _FakeClient(
            playlists=[_nav("p9", "Smart", song_count=2)],
            details={"p9": _detail("p9", ["t1", "t2"])},
        )
        entry = {
            "playlist_id": "file::Smart.nsp",
            "source": "file",
            "name": "Smart",
            "kind": "nsp",
            "file_path": str(nsp),
            "changed": "x",
            "song_count": 2,
        }

        ids, _unresolved = svc._track_ids_for(
            entry, [client], listings=[client._playlists]
        )

        assert ids == ["t1", "t2"]
        assert client.find_by_name_calls == [], (
            "find_playlist_by_name re-downloads the whole listing; the sync already "
            "has it"
        )

    def test_find_in_listings_is_case_insensitive(self):
        listings = [[{"id": "p1", "name": "  Metal  "}]]

        assert svc._find_in_listings("metal", listings) == {"id": "p1", "name": "  Metal  "}
        assert svc._find_in_listings("MISSING", listings) is None
        assert svc._find_in_listings("", listings) is None


# ===========================================================================
# 3. An unreadable playlist must never be recorded as empty
# ===========================================================================
class TestAnUnreadablePlaylistIsNotEmptied:
    def test_the_empty_versus_unreadable_rule(self):
        """The distinction the whole fix rests on.

        ``fetch_playlist`` returns ``{}`` for EVERY failure, so a payload with no
        ``tracks`` key and no ``songCount`` cannot be read as "0 tracks".
        """
        assert svc._navidrome_ids_from_detail({}) is None
        assert svc._navidrome_ids_from_detail({"id": "p1"}) is None
        # A genuinely empty playlist is readable, and reads as empty.
        assert svc._navidrome_ids_from_detail({"id": "p1", "tracks": []}) == []
        assert svc._navidrome_ids_from_detail({"id": "p1", "songCount": 0}) == []
        # A populated one yields its ids.
        assert svc._navidrome_ids_from_detail(
            {"id": "p1", "tracks": [{"id": "a"}, {"id": "b"}]}
        ) == ["a", "b"]

    def test_track_ids_for_reports_unknown_rather_than_empty(self, no_nsp):
        client = _FakeClient(fail_ids={"p1"})
        entry = {"playlist_id": "p1", "source": "navidrome", "name": "P1", "song_count": 5}

        ids, _unresolved = svc._track_ids_for(entry, [client])

        assert ids is None, (
            "an unreadable playlist was reported as an empty one; writing [] would "
            "delete every indexed row for it"
        )

    def test_the_sync_does_not_write_when_contents_are_unreadable(
        self, no_nsp, repo, monkeypatch
    ):
        """The data-loss case: a transient failure must not wipe the index."""
        client = _FakeClient(playlists=[_nav("p1", "P1")], fail_ids={"p1"})
        monkeypatch.setattr(svc, "_navidrome_clients", lambda: [client], raising=False)

        stats = svc.sync_playlist_index([client])

        assert repo.written == [], (
            "the sync wrote an empty playlist over good index data — a transient "
            "Navidrome error would blank the artist page until the next success"
        )
        assert stats["errors"] >= 1
        assert stats["changed"] == 0

    def test_a_genuinely_empty_playlist_IS_written(self, no_nsp, repo):
        """CONTROL: the guard must not block a real empty playlist."""
        client = _FakeClient(
            playlists=[_nav("p1", "P1", song_count=0)],
            details={"p1": {"id": "p1", "tracks": []}},
        )

        stats = svc.sync_playlist_index([client])

        assert stats["changed"] == 1
        assert repo.written and repo.written[0]["track_ids"] == []

    def test_a_normal_playlist_still_indexes(self, no_nsp, repo):
        """CONTROL: the happy path is unchanged."""
        client = _FakeClient(
            playlists=[_nav("p1", "P1", song_count=2)],
            details={"p1": _detail("p1", ["a", "b"])},
        )

        stats = svc.sync_playlist_index([client])

        assert stats["changed"] == 1
        assert stats["entries"] == 2
        assert repo.written[0]["track_ids"] == ["a", "b"]
        assert repo.written[0]["changed"] == "1"

    def test_an_unchanged_playlist_costs_no_fetch(self, no_nsp, repo):
        """The stamp fast path — the reason 549 of 615 cost nothing."""
        repo.stored = {"p1": "1"}
        client = _FakeClient(playlists=[_nav("p1", "P1", changed="1")])

        stats = svc.sync_playlist_index([client])

        assert stats["skipped"] == 1
        assert stats["changed"] == 0
        assert not any(c.startswith("fetch_playlist") for c in client.calls), (
            "an unchanged playlist was fetched anyway"
        )

    def test_the_sync_still_prunes_on_a_clean_run(self, no_nsp, repo):
        """CONTROL: the unreadable guard must not disable pruning."""
        client = _FakeClient(
            playlists=[_nav("p1", "P1")],
            details={"p1": _detail("p1", ["a"])},
        )

        svc.sync_playlist_index([client])

        assert repo.pruned == 1

    def test_the_sync_skips_pruning_when_a_server_failed(self, no_nsp, repo):
        """A partial view must never delete playlists that still exist."""
        class _Boom(_FakeClient):
            def fetch_all_playlists(self):
                raise RuntimeError("server down")

        repo.stored = {"previously": "1"}

        svc.sync_playlist_index([_Boom()])

        assert repo.pruned == 0
