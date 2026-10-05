"""Genre-playlist behaviour that is still observable.

RETIRED — and therefore removed from this file.  The create/delete thresholds,
name template, toggles and popularity ordering used to be asserted by writing a
``{Genre} - Top Tracks.m3u`` into a temp Playlists dir.  That is impossible now:
``finalise_stage``'s own module docstring says "No .m3u or .nsp files are written
to disk anywhere in this module" — every playlist is created through the
Subsonic/Navidrome API.  The fixture those tests shared also patched
``_essential_playlists_dir`` and ``_genre_playlists_state_file``, neither of
which exists any more, so all 18 errored at setup and could never pass again.
They asserted retired behaviour; they were dead, not merely failing.

What remains is what can still be observed: the Navidrome orphan sweep (it
stems from the same reported defect — playlists that survive on the server
after they stop being wanted) and the "a dead database does not abort the
stage" path.
"""

from __future__ import annotations

from contextlib import contextmanager

from services.popularity.stages import finalise_stage as fs


def _session_factory(session):
    @contextmanager
    def _cm():
        yield session

    return _cm


class TestEdgeCases:
    def test_db_fetch_failure_is_graceful(self, monkeypatch):
        import db.engine as db_engine

        class BoomSession:
            def execute(self, sql, params=None):
                raise RuntimeError("db down")

            def commit(self):
                pass

            def rollback(self):
                pass

        monkeypatch.setattr(db_engine, "db_session", _session_factory(BoomSession()))
        written = fs._create_genre_top_track_playlists()
        assert written == 0


class _FakeNavidromeClient:
    """Minimal Navidrome client stand-in for the orphan-sweep tests."""

    def __init__(self, playlists):
        self._playlists = playlists
        self.deleted = []

    def fetch_all_playlists(self):
        return list(self._playlists)

    def find_playlist_by_name(self, name):
        wanted = str(name or "").strip().lower()
        for p in self._playlists:
            if str(p.get("name") or "").strip().lower() == wanted:
                return p
        return None

    def delete_playlist(self, playlist_id):
        self.deleted.append(playlist_id)
        return True


class TestNavidromeOrphanSweep:
    """The server keeps a playlist after we stop wanting it.

    Nothing is written to disk any more, so "wanted" is whatever the rebuild
    hands the sweep as ``keep_playlist_names`` — the sweep must delete exactly
    the genre playlists outside that set (the reported "removed from our side
    but still on Navidrome").
    """

    def test_sweeps_orphaned_keeps_named_and_foreign(self, monkeypatch):
        monkeypatch.setattr(fs, "_genre_playlists_delete_enabled", lambda: True)

        client = _FakeNavidromeClient([
            {"id": "p1", "name": "Alt-rock - Top Tracks"},                    # not kept → sweep
            {"id": "p2", "name": "Alternative - Top Tracks"},                 # in the keep set → keep
            {"id": "p3", "name": "Amon Amarth - Essential Collection"},       # not genre suffix → keep
        ])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        fs._sweep_orphaned_genre_playlists_from_navidrome(
            {"Alternative - Top Tracks"}
        )

        assert client.deleted == ["p1"]

    def test_respects_delete_toggle(self, monkeypatch):
        monkeypatch.setattr(fs, "_genre_playlists_delete_enabled", lambda: False)
        client = _FakeNavidromeClient([{"id": "p1", "name": "Alt-rock - Top Tracks"}])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        fs._sweep_orphaned_genre_playlists_from_navidrome(set())

        assert client.deleted == []

    def test_delete_by_name_uses_normalized_clients(self, monkeypatch):
        """``_delete_playlist_from_navidrome`` must resolve clients via
        ``_navidrome_clients`` (normalised config) and delete by case-insensitive
        name."""
        client = _FakeNavidromeClient([{"id": "p9", "name": "Alt-rock - Top Tracks"}])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        fs._delete_playlist_from_navidrome("Alt-Rock - Top Tracks")

        assert client.deleted == ["p9"]
