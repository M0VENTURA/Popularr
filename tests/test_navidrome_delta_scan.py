"""Tests for Navidrome delta-scan detection of new songs in existing albums.

Regression: "new songs were added to an existing album, but the change scan
doesn't detect it".

Root cause: the delta candidate selection relied on album-list deltas
(``getAlbumList2`` newest/recentlyAdded, which are ordered by ``created`` —
an existing album that gains tracks keeps its old position) and a song-level
delta (``getSongs?modified=``, which Navidrome does NOT implement).  Artists
whose existing albums gained new songs were therefore never candidates and
their new tracks were never imported.

Fix verified here:
  - ``get_indexes`` sends ``ifModifiedSince`` as epoch MILLISECONDS
    (Navidrome's ``req.TimeOr`` parses with ``time.UnixMilli``; the old
    seconds value made the delta gate always pass).
  - ``build_delta_artist_index`` surfaces artists from ``getIndexes`` — the
    only delta source that catches "new songs added to an existing album".
  - ``compute_artist_album_diff`` marks an existing album changed when
    Navidrome's ``songCount`` exceeds the local track count, so the
    per-artist diff re-imports the new tracks.
"""

from __future__ import annotations

from datetime import datetime

from api_clients.navidrome import NavidromeClient
from db.engine import db_session
from services.scanning.navidrome_import import compute_artist_album_diff
from services.scanning.navidrome_service import build_delta_artist_index
from sqlalchemy import text


def _epoch_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


class _FakeDeltaClient:
    """Fake Navidrome client driving the delta-scan sources."""

    def __init__(self, index_groups=None, album_pages=None):
        self.index_groups = index_groups if index_groups is not None else []
        self.album_pages = album_pages if album_pages is not None else []
        self.index_calls = 0
        self.album_pages_calls = 0

    def get_indexes(self, if_modified_since=None):
        self.index_calls += 1
        return {"index": self.index_groups}

    def get_album_list2_page(self, list_type="newest", size=200, offset=0):
        self.album_pages_calls += 1
        if offset >= len(self.album_pages):
            return []
        return self.album_pages[offset]

    def get_songs(self, offset=0, size=500, modified=None):
        return []


# ---------------------------------------------------------------------------
# get_indexes timestamp unit
# ---------------------------------------------------------------------------


def test_get_indexes_sends_epoch_milliseconds(monkeypatch):
    captured = {}

    def fake_response(self, endpoint, timeout=30, **params):
        captured["params"] = dict(params)
        return {"indexes": {"index": []}}

    monkeypatch.setattr(NavidromeClient, "_get_subsonic_response", fake_response)
    client = NavidromeClient(base_url="http://navidrome:4533", username="u", password="p")
    client.get_indexes(if_modified_since="2026-08-01T00:00:00Z")

    assert captured["params"]["ifModifiedSince"] == _epoch_ms("2026-08-01T00:00:00Z")
    # A raw epoch-seconds value would never equal a millisecond timestamp.
    assert captured["params"]["ifModifiedSince"] % 1000 == 0


def test_get_indexes_omits_param_when_no_timestamp(monkeypatch):
    captured = {}

    def fake_response(self, endpoint, timeout=30, **params):
        captured["params"] = dict(params)
        return {"indexes": {"index": []}}

    monkeypatch.setattr(NavidromeClient, "_get_subsonic_response", fake_response)
    client = NavidromeClient(base_url="http://navidrome:4533", username="u", password="p")
    client.get_indexes(if_modified_since=None)

    assert "ifModifiedSince" not in captured["params"]


# ---------------------------------------------------------------------------
# build_delta_artist_index candidate selection
# ---------------------------------------------------------------------------


def test_delta_index_surfaces_artist_with_new_songs_in_existing_album():
    """getIndexes returns the full album-artist index after a Navidrome rescan.

    This is the exact scenario the old album/song deltas missed: an artist
    whose EXISTING album gained new songs has an old ``created`` timestamp,
    so it never appears in ``recentlyAdded``/``newest`` album lists.
    """
    client = _FakeDeltaClient(
        index_groups=[
            {
                "name": "A",
                "artist": [
                    {"id": "ar-1", "name": "The Existing Album Artist"},
                    {"id": "ar-2", "name": "Brand New Artist"},
                ],
            }
        ],
    )
    delta = build_delta_artist_index(client, since_ts="2026-08-01T00:00:00Z")

    assert delta == {
        "The Existing Album Artist": {
            "id": "ar-1",
            "album_count": 0,
            "track_count": 0,
            "last_updated": None,
        },
        "Brand New Artist": {
            "id": "ar-2",
            "album_count": 0,
            "track_count": 0,
            "last_updated": None,
        },
    }
    assert client.index_calls == 1


def test_delta_index_merges_album_delta_when_get_indexes_empty():
    """Empty getIndexes (nothing rescanned) still picks up brand-new albums."""
    client = _FakeDeltaClient(
        index_groups=[],
        album_pages=[
            [
                {
                    "id": "al-1",
                    "artist": "New Album Artist",
                    "artistId": "ar-9",
                    "songCount": 4,
                }
            ]
        ],
    )
    delta = build_delta_artist_index(client, since_ts="2026-08-01T00:00:00Z")

    assert delta == {
        "New Album Artist": {
            "id": "ar-9",
            "album_count": 1,
            "track_count": 4,
            "last_updated": None,
        }
    }


def test_delta_index_empty_when_no_source_reports_changes():
    client = _FakeDeltaClient(index_groups=[], album_pages=[])
    assert build_delta_artist_index(client, since_ts="2026-08-01T00:00:00Z") == {}


def test_delta_index_without_since_ts_falls_back_to_full_index():
    """First run (no checkpoint): getIndexes without a cutoff returns everyone."""
    client = _FakeDeltaClient(
        index_groups=[{"name": "Z", "artist": [{"id": "ar-3", "name": "First Run Artist"}]}]
    )
    delta = build_delta_artist_index(client, since_ts=None)

    assert "First Run Artist" in delta
    assert delta["First Run Artist"]["id"] == "ar-3"


# ---------------------------------------------------------------------------
# Per-artist album diff (inside scan_artist_to_db diff mode)
# ---------------------------------------------------------------------------


class _FakeArtistClient:
    """Fake client for ``compute_artist_album_diff`` (getArtist album list)."""

    def __init__(self, albums):
        self.albums = albums

    def fetch_artist_albums(self, artist_id):
        return self.albums


def test_artist_diff_marks_existing_album_changed_when_song_count_grows():
    """New song added to an existing album → Navidrome songCount > local count.

    The album must be reported as changed so ``should_skip_album`` lets it
    through and the new track is upserted.
    """
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, album_artist) "
                "VALUES (:id, :artist, :album, :album_artist)"
            ),
            [
                {"id": "t1", "artist": "Existing Artist", "album": "Album", "album_artist": "Existing Artist"},
                {"id": "t2", "artist": "Existing Artist", "album": "Album", "album_artist": "Existing Artist"},
            ],
        )

    client = _FakeArtistClient(
        [
            {"id": "al-1", "name": "Album", "songCount": 3},  # +1 new song in Navidrome
        ]
    )

    skip_artist, changed_albums, removed_albums = compute_artist_album_diff(
        "Existing Artist",
        client.albums,
    )

    assert skip_artist is False
    assert changed_albums == {"Album"}
    assert removed_albums == set()


# ---------------------------------------------------------------------------
# getAlbumList2 list types
# ---------------------------------------------------------------------------


def test_delta_list_types_exclude_unimplemented_recently_added():
    """Navidrome does not implement ``recentlyAdded`` for ``getAlbumList2``.

    It answers with ``status="failed"`` and
    ``code=0 message="type 'recentlyAdded' not implemented"``, which the
    client logs as a WARNING on every delta scan. Requesting it therefore
    bought nothing but noise, so only ``newest`` may be requested.
    """
    from services.scanning.navidrome_service import _DELTA_LIST_TYPES

    assert "recentlyAdded" not in _DELTA_LIST_TYPES
    assert "newest" in _DELTA_LIST_TYPES


def test_album_sort_ts_ignores_recently_added_field():
    """Navidrome album objects carry ``created``/``updated``, not ``recentlyAdded``."""
    from services.scanning.navidrome_service import _album_sort_ts

    assert _album_sort_ts({"created": "2026-08-01T00:00:00Z"}) > 0
    assert _album_sort_ts({"updated": "2026-08-01T00:00:00Z"}) > 0
    # An album exposing ONLY the never-present field sorts to 0 ("unknown").
    assert _album_sort_ts({"recentlyAdded": "2026-08-01T00:00:00Z"}) == 0.0


def test_fetch_changed_albums_requests_only_implemented_list_types():
    """The caller must never send a list type Navidrome rejects."""
    from services.scanning.navidrome_service import fetch_changed_albums

    requested: list[str] = []

    class _Recorder:
        def get_album_list2_page(self, list_type="newest", size=200, offset=0):
            requested.append(list_type)
            return []

    fetch_changed_albums(_Recorder(), since_ts=None, page_size=10, max_pages=1)

    assert requested == ["newest"]


def test_artist_diff_skips_artist_when_counts_match():
    """Unchanged artist: Navidrome songCount == local count → no re-import."""
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, album_artist) "
                "VALUES (:id, :artist, :album, :album_artist)"
            ),
            [
                {"id": "t3", "artist": "Steady Artist", "album": "Old Album", "album_artist": "Steady Artist"},
                {"id": "t4", "artist": "Steady Artist", "album": "Old Album", "album_artist": "Steady Artist"},
            ],
        )

    client = _FakeArtistClient(
        [
            {"id": "al-2", "name": "Old Album", "songCount": 2},
        ]
    )

    skip_artist, changed_albums, removed_albums = compute_artist_album_diff(
        "Steady Artist",
        client.albums,
    )

    assert skip_artist is True
    assert changed_albums == set()
    assert removed_albums == set()
