"""Say WHICH tracks Navidrome refused to store, and stop the import leaving a stale cache.

Reported
--------
> A full forced Navidrome import has been run. Is something missing from the
> Navidrome import?

Two log lines said the opposite of each other: the sync warned *"stale song
IDs … re-run the Navidrome import scan to refresh them"* after the operator had
already run exactly that.

Two things were missing:

1. **The diagnosis was a count.** ``stored=298, requested=300`` cannot tell a
   *stale* id (a Navidrome import fixes it) from a song Navidrome has not
   *indexed* yet (only a Navidrome scan fixes it — the import is powerless, so
   the advice could be followed to the letter and the warning come back). The
   ids existed all along; nobody printed them.

2. **The import left the playlist row cache warm.** Genre / Top-Tracks
   playlists are built from ``_GENRE_ROWS_CACHE`` — a 120s snapshot of
   ``tracks`` rows *including ``tracks.id``, which is the Navidrome song id*.
   The import rewrites exactly that column, and nothing in the import path
   cleared the cache, so a finalise inside that window pushed the ids the
   import had just replaced.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import services.playlists.playlist_navidrome_service as pns  # noqa: E402
import services.popularity.stages.finalise_stage as fs  # noqa: E402
from services.scanning import navidrome_import  # noqa: E402

IMPORT_SOURCE = Path(navidrome_import.__file__).read_text(encoding="utf-8")


class _RecLogger:
    """Structlog-shaped recorder: collects ``(level, event, kwargs)``.

    Identical to the one in ``test_playlist_sync_auth_and_cap.py`` — the two
    suites assert against the same records, so they share the contract:
    ``warnings(needle)[0][1]`` is the message, ``[0][2]`` the kwargs.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):  # pragma: no cover - trivial shim
        def _log(event, *args, **kw):
            self.calls.append((str(level), str(event), kw))
        return _log

    def warnings(self, contains: str = "") -> list[tuple[str, str, dict]]:
        return [
            c for c in self.calls
            if c[0] == "warning" and contains in c[1]
        ]

    def kw(self, needle: str, key: str):
        found = [c[2] for c in self.warnings(needle)]
        assert found, f"no warning containing {needle!r}"
        return found[0].get(key)


class _FakeNavClient:
    def __init__(self, *, playlists=None, stored=None, update_ok=True,
                 stored_after_update=None):
        self.playlists = playlists if playlists is not None else []
        self.stored = list(stored or [])
        self.update_ok = update_ok
        self.stored_after_update = (
            list(stored_after_update) if stored_after_update is not None else None
        )

    def fetch_all_playlists(self):
        return [dict(p) for p in self.playlists]

    def fetch_playlist(self, playlist_id):
        return {"tracks": [{"id": sid} for sid in self.stored]}

    def update_playlist_songs(self, playlist_id, song_ids, current_count=None,
                              owner=None, playlist_name=None):
        if self.update_ok and self.stored_after_update is not None:
            self.stored = list(self.stored_after_update)
        return self.update_ok

    def delete_playlist(self, playlist_id):
        return True

    def create_playlist(self, name, song_ids, public=True):
        return {"status": "ok", "playlist": {"id": "new-1",
                                             "songCount": len(song_ids)}}


class TestTheSyncReportsWhichIdsWereDropped:
    REQUESTED = ["t1", "t2", "t3", "t4", "t5"]

    def test_the_dropped_ids_come_back_on_the_result(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(pns, "logger", rec)
        client = _FakeNavClient(
            playlists=[{"id": "pl1", "name": "Metal - Top Tracks"}],
            stored=["old-1"],
            stored_after_update=["t1", "t2", "t4"],   # t3 and t5 not stored
        )

        result = pns.sync_playlist_by_name(client, "Metal - Top Tracks", self.REQUESTED)

        dropped = result.get("dropped_ids")
        assert dropped == ["t3", "t5"], (
            "the caller needs the IDs, not a count — a count cannot tell a "
            "stale id from one Navidrome has never indexed"
        )

    def test_the_warning_carries_the_ids_and_the_extra_ones(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(pns, "logger", rec)
        client = _FakeNavClient(
            playlists=[{"id": "pl1", "name": "Metal - Top Tracks"}],
            stored=["old-1"],
            stored_after_update=["t1", "t2", "t4", "leftover"],
        )

        pns.sync_playlist_by_name(client, "Metal - Top Tracks", self.REQUESTED)

        assert rec.kw("stored a different number", "dropped_ids") == ["t3", "t5"]
        assert rec.kw("stored a different number", "extra_ids") == ["leftover"], (
            "an id Navidrome kept that we did NOT ask for means the replace "
            "did not fully replace — equally worth seeing"
        )
        assert rec.kw("stored a different number", "dropped") == 2

    def test_the_guidance_admits_the_import_cannot_fix_everything(self, monkeypatch):
        """The old message sent people to an import that could not help."""
        rec = _RecLogger()
        monkeypatch.setattr(pns, "logger", rec)
        client = _FakeNavClient(
            playlists=[{"id": "pl1", "name": "Metal - Top Tracks"}],
            stored_after_update=["only-one"],
        )

        pns.sync_playlist_by_name(client, "Metal - Top Tracks", self.REQUESTED)

        msg = rec.warnings("stored a different number")[0][1]
        assert "rescan Navidrome" in msg, (
            "a file Navidrome has not indexed has no id to refresh — the "
            "message must say what to do instead of repeating the import advice"
        )

    def test_matching_counts_stay_silent_and_carry_no_ids(self, monkeypatch):
        """CONTROL — the extra fetch only reports on a real mismatch."""
        rec = _RecLogger()
        monkeypatch.setattr(pns, "logger", rec)
        client = _FakeNavClient(
            playlists=[{"id": "pl1", "name": "Metal - Top Tracks"}],
            stored_after_update=list(self.REQUESTED),
        )

        result = pns.sync_playlist_by_name(client, "Metal - Top Tracks", self.REQUESTED)

        assert result["updated"] is True
        assert not rec.warnings("stored a different number")
        assert "dropped_ids" not in result


class TestTheCallerNamesTheTracks:
    ROWS = [
        {"id": "t3", "artist": "Stabbing Westward", "title": "Shame"},
        {"id": "t5", "artist": "Stabbing Westward", "title": "Want"},
    ]

    def test_dropped_ids_are_logged_as_artist_and_title(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(fs, "logger", rec)
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [object()])
        monkeypatch.setattr(
            fs, "_playlist_already_pushed", lambda name, sig: False,
        )
        monkeypatch.setattr(
            fs, "_playlist_signature", lambda ids: "sig",
        )
        monkeypatch.setattr(
            fs, "_record_playlist_push", lambda name, sig: None,
        )

        import services.playlists.playlist_navidrome_service as svc
        monkeypatch.setattr(
            svc, "sync_playlist_by_name",
            lambda client, name, ids: {
                "updated": True, "success": True, "dropped_ids": ["t3", "t5"],
            },
        )

        fs._sync_playlist_to_navidrome(
            "Stabbing Westward - Essential Collection",
            ["t1", "t3", "t5"],
            rows=self.ROWS,
        )

        named = rec.kw("did not store these track", "tracks")
        assert named == ["Stabbing Westward - Shame", "Stabbing Westward - Want"], (
            "a bare id in the log cannot be acted on — the operator must be "
            "able to see which songs to look at"
        )
        assert "Navidrome scan" in rec.kw("did not store these track", "hint")

    def test_an_unknown_id_is_still_reported(self, monkeypatch):
        """A row that is not in our own list must not vanish silently."""
        rec = _RecLogger()
        monkeypatch.setattr(fs, "logger", rec)
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [object()])
        monkeypatch.setattr(fs, "_playlist_already_pushed", lambda name, sig: False)
        monkeypatch.setattr(fs, "_playlist_signature", lambda ids: "sig")
        monkeypatch.setattr(fs, "_record_playlist_push", lambda name, sig: None)

        import services.playlists.playlist_navidrome_service as svc
        monkeypatch.setattr(
            svc, "sync_playlist_by_name",
            lambda client, name, ids: {
                "updated": True, "success": True, "dropped_ids": ["nope"],
            },
        )

        fs._sync_playlist_to_navidrome("X - Top Tracks", ["nope"], rows=self.ROWS)

        assert rec.kw("did not store these track", "tracks") == ["<unknown id nope>"]

    def test_no_dropped_ids_means_no_extra_warning(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(fs, "logger", rec)
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [object()])
        monkeypatch.setattr(fs, "_playlist_already_pushed", lambda name, sig: False)
        monkeypatch.setattr(fs, "_playlist_signature", lambda ids: "sig")
        monkeypatch.setattr(fs, "_record_playlist_push", lambda name, sig: None)

        import services.playlists.playlist_navidrome_service as svc
        monkeypatch.setattr(
            svc, "sync_playlist_by_name",
            lambda client, name, ids: {"updated": True, "success": True},
        )

        fs._sync_playlist_to_navidrome("X - Top Tracks", ["t1", "t2"], rows=self.ROWS)

        assert not rec.warnings("did not store these track")


class TestTheImportClearsThePlaylistRowCache:
    """The half that answers "is something missing from the import?"."""

    def test_the_cache_snapshot_is_a_row_set_including_the_song_id(self):
        """Guard the premise: the cache holds ``tracks.id`` — the Navidrome id.

        If this ever stops being true, invalidating it after an import is
        pointless and the tests below should be deleted rather than trusted.
        """
        assert fs._GENRE_ROWS_TTL_SECONDS, "the genre row cache must exist"
        sql = " ".join(fs._GENRE_ROWS_SQL.split())
        assert "SELECT id" in sql, (
            "the cached query no longer selects the song id, so refreshing it "
            "after an import would not refresh what playlists push"
        )
        assert "FROM tracks" in sql

    def test_the_import_invalidates_it_after_the_album_loop(self):
        assert "invalidate_genre_playlist_rows()" in IMPORT_SOURCE, (
            "the import rewrites tracks.id (the Navidrome song id) and leaves "
            "the 120s playlist row cache holding the old ones — the next "
            "finalise then pushes ids the import has just replaced"
        )
        # AFTER the albums were processed, not before them.
        assert IMPORT_SOURCE.index("invalidate_genre_playlist_rows()") > (
            IMPORT_SOURCE.index("for album_index, album in enumerate(albums, 1)")
        )

    def test_invalidate_actually_clears_the_snapshot(self):
        previous = fs._GENRE_ROWS_CACHE
        try:
            fs._GENRE_ROWS_CACHE = [{"id": "stale"}]
            fs.invalidate_genre_playlist_rows()
            assert fs._GENRE_ROWS_CACHE is None
        finally:
            fs._GENRE_ROWS_CACHE = previous
