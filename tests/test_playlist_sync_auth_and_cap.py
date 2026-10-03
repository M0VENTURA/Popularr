"""Genre playlist size + Navidrome playlist-write failures must be visible.

Report: *"The top tracks playlists for genres are only showing maximum of 50
tracks"* (clarified: *"less than 50 on each playlist"*), reported right after
Navidrome was upgraded to the newest release (0.64.x).

Diagnosis pinned here:

* Navidrome rejects ``updatePlaylist``/``deletePlaylist`` with Subsonic
  **code 50 ("not authorized")** when the configured user is not an admin and
  does not own the playlist (``core/playlists.checkWritable``). The playlist
  then keeps its OLD contents forever. Our client swallowed this: the update
  logged only the raw response with no guidance, and ``delete_playlist``
  logged **nothing** — so the genre sweep silently kept stale playlists.
* Navidrome 0.64 re-encoded all internal IDs; song IDs cached in
  ``tracks.id`` may no longer resolve, so an ACCEPTED write can store fewer
  tracks than requested. ``sync_playlist_by_name`` now re-checks the stored
  count and warns with re-import guidance.
* ``_sync_playlist_to_navidrome`` counted only EXCEPTIONS as failures — a
  returned failure counted as nothing, and the genre loop's
  ``if not _playlist_sync_succeeded: continue`` was silent.
* ``genre_playlists_max_tracks`` had no Config-page input (contract §4.1),
  the Config description promised "(no top-N cap)" while the code capped at
  300, and a configured ``0`` was silently coerced back to 300.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import api_clients.navidrome as nav_mod
import helpers.config_helpers as config_helpers
import services.playlists.playlist_navidrome_service as pns
import services.popularity.stages.finalise_stage as fs


REPO_ROOT = Path(__file__).resolve().parent.parent


class _RecLogger:
    """Structlog-shaped recorder: collects ``(level, event, kwargs)``."""

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


# ---------------------------------------------------------------------------
# 1. The Navidrome client: authorization failures must be actionable
# ---------------------------------------------------------------------------

class TestUpdatePlaylistAuthGuidance:
    def _client(self, monkeypatch, response: dict) -> nav_mod.NavidromeClient:
        client = nav_mod.NavidromeClient("http://navidrome:4533", "user", "pass")
        monkeypatch.setattr(client, "_post_subsonic_response", lambda *a, **k: response)
        return client

    def test_code_50_logs_actionable_guidance(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(nav_mod, "logger", rec)
        client = self._client(monkeypatch, {
            "status": "failed",
            "error": {"code": 50, "message": "User is not authorized for the given operation"},
        })

        ok = client.update_playlist_songs("pl-1", ["a", "b", "c"], current_count=100)

        assert ok is False
        failures = rec.warnings("updatePlaylist songs rejected")
        assert failures, "a rejected update must be logged at WARNING"
        hint = failures[0][2].get("hint") or ""
        assert "admin" in hint and "does not own" in hint, (
            "code 50 must tell the operator HOW to fix it (make the user admin "
            "or delete the playlist so it is recreated under this user)"
        )

    def test_non_auth_failure_has_no_admin_hint(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(nav_mod, "logger", rec)
        client = self._client(monkeypatch, {"status": "failed", "error": {"code": 0}})

        ok = client.update_playlist_songs("pl-1", ["a"], current_count=1)

        assert ok is False
        failures = rec.warnings("updatePlaylist songs rejected")
        assert failures
        assert not failures[0][2].get("hint")


class TestDeletePlaylistIsNotSilent:
    def test_rejected_delete_is_logged(self, monkeypatch):
        """The genre sweep treats False as 'keep' — a silent False left stale
        playlists behind with nothing in the logs."""
        rec = _RecLogger()
        monkeypatch.setattr(nav_mod, "logger", rec)
        client = nav_mod.NavidromeClient("http://navidrome:4533", "user", "pass")
        monkeypatch.setattr(
            client, "_get_subsonic_response",
            lambda *a, **k: {"status": "failed", "error": {"code": 50, "message": "User is not authorized for the given operation"}},
        )

        deleted = client.delete_playlist("pl-9")

        assert deleted is False
        failures = rec.warnings("rejected playlist deletion")
        assert failures, "delete_playlist used to return False with NO log line"
        assert "admin" in (failures[0][2].get("hint") or "")

    def test_successful_delete_stays_quiet(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(nav_mod, "logger", rec)
        client = nav_mod.NavidromeClient("http://navidrome:4533", "user", "pass")
        monkeypatch.setattr(client, "_get_subsonic_response", lambda *a, **k: {"status": "ok"})

        assert client.delete_playlist("pl-9") is True
        assert not rec.warnings("rejected playlist deletion")


# ---------------------------------------------------------------------------
# 2. Post-write verification: an accepted write must store what we asked
# ---------------------------------------------------------------------------

class _FakeNavClient:
    """Duck-typed NavidromeClient for sync_playlist_by_name."""

    def __init__(self, *, playlists=None, stored=None, update_ok=True,
                 stored_after_update=None, create_song_count=None):
        self.playlists = playlists if playlists is not None else []
        self.stored = list(stored or [])
        self.update_ok = update_ok
        self.stored_after_update = (
            list(stored_after_update) if stored_after_update is not None else None
        )
        self.create_song_count = create_song_count
        self.update_calls: list[list[str]] = []

    def fetch_all_playlists(self):
        return [dict(p) for p in self.playlists]

    def fetch_playlist(self, playlist_id):
        return {"tracks": [{"id": sid} for sid in self.stored]}

    def update_playlist_songs(self, playlist_id, song_ids, current_count=None):
        self.update_calls.append(list(song_ids))
        if self.update_ok and self.stored_after_update is not None:
            self.stored = list(self.stored_after_update)
        return self.update_ok

    def delete_playlist(self, playlist_id):
        return True

    def create_playlist(self, name, song_ids, public=True):
        count = self.create_song_count if self.create_song_count is not None else len(song_ids)
        return {"status": "ok", "playlist": {"id": "new-1", "songCount": count}}


def _sync(client, name, song_ids):
    return pns.sync_playlist_by_name(client, name, song_ids)


class TestPostWriteVerification:
    REQUESTED = ["t1", "t2", "t3", "t4", "t5"]

    def test_stored_count_mismatch_warns_with_reimport_guidance(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(pns, "logger", rec)
        client = _FakeNavClient(
            playlists=[{"id": "pl1", "name": "Metal - Top Tracks"}],
            stored=["old-1"],                      # pre-write contents
            stored_after_update=["only-one"],      # Navidrome kept 1 of 5
        )

        result = _sync(client, "Metal - Top Tracks", self.REQUESTED)

        assert result["updated"] is True and result["success"] is True
        warns = rec.warnings("stored a different number of tracks")
        assert warns, (
            "an accepted write that stores fewer tracks (stale IDs after "
            "Navidrome's 0.64 re-encoding) must warn"
        )
        assert "Navidrome import" in warns[0][1]
        assert warns[0][2]["requested"] == 5 and warns[0][2]["stored"] == 1

    def test_matching_counts_verify_silently(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(pns, "logger", rec)
        client = _FakeNavClient(
            playlists=[{"id": "pl1", "name": "Metal - Top Tracks"}],
            stored=["old-1"],
            stored_after_update=list(self.REQUESTED),
        )

        result = _sync(client, "Metal - Top Tracks", self.REQUESTED)

        assert result["updated"] is True
        assert not rec.warnings("stored a different number of tracks")

    def test_rejected_update_reports_failure_and_skips_verification(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(pns, "logger", rec)
        client = _FakeNavClient(
            playlists=[{"id": "pl1", "name": "Metal - Top Tracks"}],
            stored=["old-1"],
            update_ok=False,
        )

        result = _sync(client, "Metal - Top Tracks", self.REQUESTED)

        assert result["success"] is False and result["updated"] is False
        assert client.update_calls, "the update must still be ATTEMPTED"
        assert not rec.warnings("stored a different number of tracks")

    def test_created_playlist_song_count_is_verified(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(pns, "logger", rec)
        client = _FakeNavClient(playlists=[], create_song_count=2)

        result = _sync(client, "Brand New - Top Tracks", self.REQUESTED)

        assert result["created"] is True and result["success"] is True
        warns = rec.warnings("stored a different number of tracks")
        assert warns and warns[0][2]["stored"] == 2


# ---------------------------------------------------------------------------
# 3. The sync layer must COUNT returned failures
# ---------------------------------------------------------------------------

class TestSyncCountsReturnedFailures:
    def test_returned_failure_counts_as_failed(self, monkeypatch):
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [object()])
        monkeypatch.setattr(fs, "_playlist_already_pushed", lambda *a, **k: False)
        monkeypatch.setattr(fs, "_record_playlist_push", lambda *a, **k: None)
        monkeypatch.setattr(
            pns, "sync_playlist_by_name",
            lambda *a, **k: {"success": False, "updated": False, "created": False,
                             "unchanged": False, "deduped": 0, "skipped": 0},
        )

        synced = fs._sync_playlist_to_navidrome("Metal - Top Tracks", ["a", "b"])

        assert synced["failed"] == 1, (
            "a returned failure (e.g. code 50) used to increment NOTHING — "
            "only exceptions were counted"
        )
        assert fs._playlist_sync_succeeded(synced) is False

    def test_successful_update_still_counts_updated(self, monkeypatch):
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [object()])
        monkeypatch.setattr(fs, "_playlist_already_pushed", lambda *a, **k: False)
        monkeypatch.setattr(fs, "_record_playlist_push", lambda *a, **k: None)
        monkeypatch.setattr(
            pns, "sync_playlist_by_name",
            lambda *a, **k: {"success": True, "updated": True, "created": False,
                             "unchanged": False, "deduped": 0, "skipped": 0},
        )

        synced = fs._sync_playlist_to_navidrome("Metal - Top Tracks", ["a"])

        assert synced["updated"] == 1 and synced["failed"] == 0
        assert fs._playlist_sync_succeeded(synced) is True


# ---------------------------------------------------------------------------
# 4. The genre builder: failures are loud, the cap is configurable
# ---------------------------------------------------------------------------

def _genre_row(i: int) -> dict:
    return {
        "id": f"nav-{i}",
        "title": f"Track {i}",
        "album": "Some Album",
        "file_path": f"/music/Artist {i}/track{i}.mp3",
        "duration": 180,
        "artist": f"Artist {i}",
        "album_artist": f"Artist {i}",
        "stars": 4,
        "popularity_score": 50.0,
        "final_score": 50.0,
        "lastfm_listeners": 1000,
        "listenbrainz_listens": 100,
        "is_live": 0,
        "is_compilation": 0,
        "genres": "metal",
    }


@pytest.fixture()
def genre_env(monkeypatch):
    """3-track metal pool; sync captured; sweep disabled."""
    state = {"rows": [_genre_row(1), _genre_row(2), _genre_row(3)],
             "sync_result": {"success": True, "updated": True, "created": False,
                             "unchanged": False, "deduped": 0, "skipped": 0},
             "captures": [], "max_tracks": 300}

    def _cfg():
        return {"playlists": {
            "genre_playlists_enabled": True,
            "genre_playlists_delete_enabled": False,
            "genre_playlists_create_threshold": 1,
            "genre_playlists_delete_threshold": 1,
            "genre_playlists_min_stars": 1,
            "genre_playlists_max_genres": 3,
            "genre_playlists_max_tracks": state["max_tracks"],
            "genre_playlists_max_per_artist": 0,
            "playlist_order_mode": "prominence",
            "playlist_interleave_artists": False,
            "exclude_christmas_from_playlists": True,
        }}

    monkeypatch.setattr(config_helpers, "get_config", _cfg)
    monkeypatch.setattr(fs, "_fetch_genre_playlist_rows", lambda _m: list(state["rows"]))
    monkeypatch.setattr(fs, "_navidrome_clients", lambda: [type("C", (), {"fetch_all_playlists": lambda self: []})()])
    monkeypatch.setattr(fs, "_playlist_already_pushed", lambda *a, **k: False)
    monkeypatch.setattr(fs, "_record_playlist_push", lambda *a, **k: None)

    def _sync(client, name, song_ids, **kw):
        state["captures"].append((name, list(song_ids)))
        return dict(state["sync_result"])

    monkeypatch.setattr(pns, "sync_playlist_by_name", _sync)
    rec = _RecLogger()
    monkeypatch.setattr(fs, "logger", rec)
    state["rec"] = rec
    return state


class TestGenrePlaylistBuilder:
    def test_failed_sync_is_logged_not_silent(self, genre_env):
        genre_env["sync_result"] = {"success": False, "updated": False, "created": False,
                                    "unchanged": False, "deduped": 0, "skipped": 0}

        written = fs._create_genre_top_track_playlists()

        assert written == 0
        warns = genre_env["rec"].warnings("Genre playlist sync failed")
        assert warns, (
            "the genre loop used to `continue` silently on failure — the "
            "playlist kept its previous contents with nothing in the logs"
        )
        assert warns[0][2]["playlist"].endswith("Top Tracks")

    def test_cap_limits_pushed_tracks(self, genre_env):
        genre_env["max_tracks"] = 2

        written = fs._create_genre_top_track_playlists()

        assert written == 1
        assert len(genre_env["captures"][0][1]) == 2, "max_tracks=2 must cap the push"

    def test_zero_cap_pushes_every_qualifying_track(self, genre_env):
        """0 = no cap (the documented meaning elsewhere in this config).

        ⚠️ The pool must EXCEED the default cap (300) or the old
        ``int(0 or 300)`` coercion is invisible at these sizes and the test
        passes on broken code.
        """
        genre_env["max_tracks"] = 0
        genre_env["rows"] = [_genre_row(i) for i in range(1, 306)]  # 305 rows

        written = fs._create_genre_top_track_playlists()

        assert written == 1
        pushed = genre_env["captures"][0][1]
        assert len(pushed) == 305, (
            "a configured 0 used to be coerced back to 300 AND the template "
            "promised 'no top-N cap' — 0 must mean unlimited"
        )


class TestResolveGenreMaxTracks:
    @pytest.mark.parametrize("raw,expected", [
        (0, None),           # 0 = unlimited
        (-5, None),          # negative = unlimited (mirrors max_per_artist)
        (300, 300),
        ("250", 250),
        ("banana", 300),     # invalid falls back to the default
        (None, 300),
        ({}, 300),           # wrong type → default
    ])
    def test_resolution(self, raw, expected):
        assert fs._resolve_genre_max_tracks({"genre_playlists_max_tracks": raw}) == expected

    def test_missing_key_uses_default(self):
        assert fs._resolve_genre_max_tracks({}) == 300


# ---------------------------------------------------------------------------
# 5. Config-page contract (§4.1): the key must be visible and collectable
# ---------------------------------------------------------------------------

class TestGenreMaxTracksConfigContract:
    TEMPLATES = [
        "templates/pages/config.html",
        "test_site/templates/Pages/config.html",
    ]
    COLLECTORS = [
        "static/js/config.js",
        "test_site/static/js/pages/config.js",
    ]

    @pytest.mark.parametrize("rel", TEMPLATES)
    def test_template_exposes_the_input(self, rel):
        html = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert 'id="playlists_genre_max_tracks"' in html, (
            f"{rel} has no input for genre_playlists_max_tracks — a hidden "
            "config key cannot be seen or changed by the user (§4.1)"
        )

    @pytest.mark.parametrize("rel", TEMPLATES)
    def test_description_no_longer_pretends_there_is_no_cap(self, rel):
        html = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "(no top-N cap)" not in html
        assert "Max Tracks Per Playlist" in html

    @pytest.mark.parametrize("rel", COLLECTORS)
    def test_config_js_collects_the_key(self, rel):
        js = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "genre_playlists_max_tracks:" in js
        # 0 must survive collection (no `|| 300` fallback).
        assert "|| 300" not in js.split("genre_playlists_max_tracks:", 1)[1][:220]

    def test_getter_preserves_zero(self, monkeypatch):
        monkeypatch.setattr(
            config_helpers, "get_config",
            lambda: {"playlists": {"genre_playlists_max_tracks": 0}},
        )
        assert config_helpers.get_playlists_config()["genre_playlists_max_tracks"] == 0
