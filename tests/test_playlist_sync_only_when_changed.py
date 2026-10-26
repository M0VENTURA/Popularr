"""Only a playlist whose CONTENT changed may be pushed again.

Reported: an album scan logged ``Genre playlists: 69 playlist(s) synced`` with
nothing changed. The skip existed — ``_playlist_already_pushed`` — but its only
store was a **per-process dict with a one-hour TTL**, so:

* every container restart / redeploy emptied it and the next scan re-pushed
  EVERY generated playlist (which is exactly what the log showed: a batch of
  ``Created Navidrome playlist`` lines, i.e. Navidrome had none of them);
* a scan longer than the TTL re-pushed everything it touched after the hour;
* the four hypercorn workers and the queue worker each kept their OWN dict, so
  which process ran the rebuild decided whether anything was skipped.

A playlist does not become stale because time passed or a process restarted, so
the record now lives on disk beside the other runtime state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services.popularity.stages import finalise_stage as fs


@pytest.fixture(autouse=True)
def _clean_store(tmp_path, monkeypatch):
    """Isolate each test's store file and clear the in-memory layer."""
    monkeypatch.setenv("SCAN_STATE_DIR", str(tmp_path))
    fs.reset_playlist_push_cache()
    yield
    fs.reset_playlist_push_cache()


def ids(n: int = 5) -> list[str]:
    return [f"track-{i}" for i in range(n)]


# ---------------------------------------------------------------------------
# The durable store
# ---------------------------------------------------------------------------


class TestTheRecordSurvivesARestart:
    def test_a_push_survives_losing_the_in_memory_cache(self) -> None:
        """⭐⭐ THE REPORTED BUG. A redeploy wiped the dict and re-pushed all 69."""
        sig = fs._playlist_signature(ids())
        fs.reset_playlist_push_cache()
        fs._record_playlist_push("Rock - Top Tracks", sig)
        assert fs._playlist_already_pushed("Rock - Top Tracks", sig) is True

        # Simulate a restart: the module dict is emptied, nothing else is.
        fs._PLAYLIST_SIGNATURES.clear()
        assert fs._playlist_already_pushed("Rock - Top Tracks", sig) is True

    def test_a_push_survives_a_new_interpreter(self, tmp_path, monkeypatch) -> None:
        """The file is the source of truth, not the module globals."""
        sig = fs._playlist_signature(ids())
        fs._record_playlist_push("Metal - Top Tracks", sig)

        store = json.loads((tmp_path / "playlist_pushes.json").read_text(encoding="utf-8"))
        assert store["metal - top tracks"]["sig"] == sig
        assert float(store["metal - top tracks"]["at"]) > 0

    def test_the_store_is_written_atomically(self, tmp_path) -> None:
        """No ``.tmp`` left behind, and the file is valid JSON."""
        fs._record_playlist_push("A - Top Tracks", fs._playlist_signature(ids()))
        leftovers = list(tmp_path.glob("*.tmp"))
        assert not leftovers, f"temp files left behind: {leftovers}"
        json.loads((tmp_path / "playlist_pushes.json").read_text(encoding="utf-8"))

    def test_recording_one_playlist_keeps_the_others(self) -> None:
        """⭐ A blind overwrite would drop another process's entries — and those
        playlists would then be re-pushed."""
        sig_a = fs._playlist_signature(ids(3))
        sig_b = fs._playlist_signature(ids(4))
        fs._record_playlist_push("A - Top Tracks", sig_a)
        fs._record_playlist_push("B - Top Tracks", sig_b)
        fs._PLAYLIST_SIGNATURES.clear()

        assert fs._playlist_already_pushed("A - Top Tracks", sig_a) is True
        fs._PLAYLIST_SIGNATURES.clear()
        assert fs._playlist_already_pushed("B - Top Tracks", sig_b) is True


class TestOnlyAChangedPlaylistIsPushed:
    def test_different_tracks_are_pushed(self) -> None:
        fs._record_playlist_push("Rock - Top Tracks", fs._playlist_signature(ids(5)))
        assert fs._playlist_already_pushed(
            "Rock - Top Tracks", fs._playlist_signature(ids(6))
        ) is False

    def test_a_reordered_list_is_pushed(self) -> None:
        """Order IS the playlist's content — the weekly rotation relies on it."""
        base = ids(5)
        fs._record_playlist_push("Rock - Top Tracks", fs._playlist_signature(base))
        assert fs._playlist_already_pushed(
            "Rock - Top Tracks", fs._playlist_signature(list(reversed(base)))
        ) is False

    def test_the_name_is_matched_case_insensitively(self) -> None:
        sig = fs._playlist_signature(ids())
        fs._record_playlist_push("Rock - Top Tracks", sig)
        assert fs._playlist_already_pushed("rock - top tracks", sig) is True

    def test_a_different_playlist_with_the_same_tracks_is_pushed(self) -> None:
        sig = fs._playlist_signature(ids())
        fs._record_playlist_push("Rock - Top Tracks", sig)
        assert fs._playlist_already_pushed("Metal - Top Tracks", sig) is False


class TestTheRecordIsBoundedAndRecoverable:
    def test_an_aged_record_is_pushed_again(self, monkeypatch) -> None:
        """⚠️ Bounds how long a playlist hand-edited in Navidrome stays edited."""
        sig = fs._playlist_signature(ids())
        fs._record_playlist_push("Rock - Top Tracks", sig)

        real_time = fs.time.time
        monkeypatch.setattr(
            fs.time,
            "time",
            lambda: real_time() + fs._PLAYLIST_PUSH_MAX_AGE_SECONDS + 1,
        )
        fs._PLAYLIST_SIGNATURES.clear()
        assert fs._playlist_already_pushed("Rock - Top Tracks", sig) is False

    def test_a_corrupt_store_reads_as_empty(self, tmp_path) -> None:
        """Fail toward a redundant push, never toward a skipped one."""
        (tmp_path / "playlist_pushes.json").write_text("{not json", encoding="utf-8")
        sig = fs._playlist_signature(ids())
        assert fs._playlist_already_pushed("Rock - Top Tracks", sig) is False

    def test_a_legacy_flat_string_entry_still_loads(self, tmp_path) -> None:
        """An older build wrote ``{name: sig}``; treat it as stale, not corrupt."""
        sig = fs._playlist_signature(ids())
        (tmp_path / "playlist_pushes.json").write_text(
            json.dumps({"rock - top tracks": sig}), encoding="utf-8"
        )
        # Loads (so the name is known) but is aged out by ``at=0``.
        store = fs._read_signature_store()
        assert store["rock - top tracks"] == (sig, 0.0)
        assert fs._playlist_already_pushed("Rock - Top Tracks", sig) is False

    def test_forgetting_removes_it_from_disk_too(self, tmp_path) -> None:
        """⭐ A forgotten playlist must be re-created, so the file must agree."""
        sig = fs._playlist_signature(ids())
        fs._record_playlist_push("Rock - Top Tracks", sig)
        fs._forget_playlist_push("Rock - Top Tracks")

        assert fs._playlist_already_pushed("Rock - Top Tracks", sig) is False
        store = json.loads((tmp_path / "playlist_pushes.json").read_text(encoding="utf-8"))
        assert "rock - top tracks" not in store

    def test_a_config_save_resets_every_remembered_push(self, tmp_path) -> None:
        """Naming, limits and ordering can all change, so every signature dies."""
        sig = fs._playlist_signature(ids())
        fs._record_playlist_push("Rock - Top Tracks", sig)

        fs.reset_playlist_push_cache()

        assert fs._playlist_already_pushed("Rock - Top Tracks", sig) is False
        assert not (tmp_path / "playlist_pushes.json").exists()

    def test_the_store_is_bounded(self, monkeypatch) -> None:
        monkeypatch.setattr(fs, "_PLAYLIST_SIG_STORE_MAX", 10)
        for i in range(25):
            fs._record_playlist_push(f"P{i} - Top Tracks", fs._playlist_signature([f"t{i}"]))
        assert len(fs._read_signature_store()) <= 10


# ---------------------------------------------------------------------------
# A playlist deleted on the server must come back immediately
# ---------------------------------------------------------------------------


class TestServerConfirmedAbsenceForcesAPush:
    def test_a_deleted_playlist_is_recreated(self, monkeypatch) -> None:
        """⭐⭐ Without this the age bound would keep a deleted playlist gone for
        a week, because the remembered push would still match."""
        sig = fs._playlist_signature(ids())
        fs._record_playlist_push("Rock - Top Tracks", sig)
        assert fs._playlist_already_pushed("Rock - Top Tracks", sig) is True

        synced: list[str] = []
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [object()])
        monkeypatch.setattr(
            fs,
            "_sync_playlist_to_navidrome",
            fs._sync_playlist_to_navidrome,
        )
        import services.playlists.playlist_navidrome_service as pns

        monkeypatch.setattr(
            pns,
            "sync_playlist_by_name",
            lambda *a, **k: synced.append("pushed") or {"success": True, "created": True},
        )

        res = fs._sync_playlist_to_navidrome(
            "Rock - Top Tracks", ids(), server_confirmed_exists=False
        )

        assert synced, "a playlist absent from the server was not re-pushed"
        assert res["skipped"] == 0

    def test_a_present_playlist_still_skips(self, monkeypatch) -> None:
        sig = fs._playlist_signature(ids())
        fs._record_playlist_push("Rock - Top Tracks", sig)

        res = fs._sync_playlist_to_navidrome(
            "Rock - Top Tracks", ids(), server_confirmed_exists=True
        )
        assert res["skipped"] == 1

    def test_unknown_existence_falls_back_to_the_signature(self) -> None:
        sig = fs._playlist_signature(ids())
        fs._record_playlist_push("Rock - Top Tracks", sig)
        res = fs._sync_playlist_to_navidrome("Rock - Top Tracks", ids())
        assert res["skipped"] == 1

    def test_the_genre_builder_passes_the_existence_hint(self) -> None:
        """The builder already fetched the server's playlist list."""
        import inspect

        source = inspect.getsource(fs._create_genre_top_track_playlists)
        assert "server_confirmed_exists=playlist_exists" in source


# ---------------------------------------------------------------------------
# The album-scan path only refreshes the genres that album touches
# ---------------------------------------------------------------------------


class TestThePerAlbumRefreshIsBounded:
    def test_it_pools_only_the_affected_genres(self) -> None:
        """`only_genres` bounds a per-album refresh to that album's playlists."""
        import inspect

        source = inspect.getsource(fs.refresh_genre_playlists_for_album)
        assert "only_genres=affected" in source

    def test_only_genres_skips_unrelated_pools(self) -> None:
        """A pool not in `only_genres` must never be synced."""
        import inspect

        source = inspect.getsource(fs._create_genre_top_track_playlists)
        assert "if norm_only_genres is not None and norm_key not in norm_only_genres:" in source
