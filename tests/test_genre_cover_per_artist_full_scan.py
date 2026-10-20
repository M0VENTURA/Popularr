"""The AI genre-cover check must run PER ARTIST during a full library scan.

Reported: *"Gemini is still not creating the covers for the genre playlists"*,
*"It should list any playlist where the tracks are updated after an artist scan
and then update the cover art via gemini if it's set in the config"*, and
*"The album scan seems to show the playlists, but they dont show during a full
library scan that does each artist one by one."*

⭐⭐ ROOT CAUSE — THE COVER CHECK RIDES ON A REBUILD THAT A FULL SCAN DEFERS.

``_create_genre_top_track_playlists`` is where the cover check lives (it runs
for every playlist that pass synced). But a full library scan does NOT call it
per artist: ``services.popularity.scan_scope`` defers the GLOBAL genre
rebuild to the END of the scan, because the genre playlists aggregate the
whole library and rebuilding them after every artist is slow and semantically
wrong.

The consequence: during a 940-artist scan the cover check ran ONCE, at the
very end — and if the scan was stopped, not even then. Artist by artist
nothing genre-shaped happened at all, which is exactly the reported symptom.

The per-ALBUM refresh (``refresh_genre_playlists_for_album``) could not fill
the gap either, for two reasons:
  * it only ran when ``total_albums <= 1`` — a single-ALBUM scan — so an
    artist scan with several albums never reached it;
  * and it re-pools the WHOLE library, which is the expensive half.

⭐ THE FIX IS THE CHEAP HALF, PER ARTIST.

``refresh_genre_playlist_covers_for_artist`` asks only "which genre playlists
does this artist's updated tracks feed?" (an ALBUM-SCOPED query) and then
ensures each of those playlists carries its AI cover. It does NOT re-pool the
library and does NOT re-sync playlist content — that stays with the
end-of-scan rebuild, which is where it belongs.

These tests drive the real service functions; only the database, the Navidrome
client and the image API are stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ===========================================================================
# Fakes
# ===========================================================================
class _FakeClient:
    """A Navidrome client with just the surface the cover pass uses."""

    def __init__(self, playlists=None, upload_ok=True, upload_reason=""):
        self._playlists = playlists or []
        self.upload_ok = upload_ok
        self.upload_reason = upload_reason
        self.uploads: list[tuple] = []
        self.lookups: list[str] = []

    def fetch_all_playlists(self):
        return list(self._playlists)

    def find_playlist_by_name(self, name):
        self.lookups.append(name)
        for pl in self._playlists:
            if str(pl.get("name") or "").strip().lower() == str(name or "").strip().lower():
                return pl
        return None

    def upload_playlist_cover(self, playlist_id, image, mime="image/png"):
        self.uploads.append((playlist_id, image, mime))
        if not self.upload_ok:
            return False, self.upload_reason
        return True, ""


@pytest.fixture
def cover_env(tmp_path, monkeypatch):
    """The real service with a real temp state file and a stubbed Gemini."""
    import api_clients.gemini as gemini_mod
    import helpers.config_helpers as cfg_mod
    from services.playlists import genre_cover_service as svc

    state_file = tmp_path / "genre_playlist_covers.json"
    monkeypatch.setattr(svc, "_state_path", lambda: str(state_file))

    monkeypatch.setattr(cfg_mod, "get_genre_cover_config", lambda: {
        "enabled": True, "regenerate": False, "prompt_template": "",
    })
    monkeypatch.setattr(cfg_mod, "get_gemini_config", lambda: {
        "enabled": True, "api_key": "test-key", "model": "test-model",
    })

    calls = {"images": 0}

    def _fake_generate_image(prompt, api_key=None, model=None):
        calls["images"] += 1
        return b"IMG", "image/png", ""

    monkeypatch.setattr(gemini_mod, "generate_image", _fake_generate_image)

    return svc, state_file, calls


# ===========================================================================
# 1. The per-artist pass reaches the cover service
# ===========================================================================
class TestTheArtistBoundaryReachesTheCoverService:
    def test_the_artist_flush_calls_the_cover_pass(self):
        """The artist boundary must invoke the per-artist cover pass.

        Anchored on the IMPORT and on the CALL SITE: an import alone proves
        nothing (the symbol could be imported and never called), and a call
        site alone could sit in a function the artist boundary never runs.
        """
        src = (
            REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py"
        ).read_text(encoding="utf-8")

        assert "refresh_genre_playlist_covers_for_artist" in src, (
            "the per-artist cover pass is not imported by the scan runner"
        )

        idx = src.index("def _flush_artist_star_ratings(")
        end = src.index("\n    def ", idx + 10)
        body = src[idx:end]

        assert "refresh_genre_playlist_covers_for_artist(" in body, (
            "the artist boundary does not run the cover pass — so a full "
            "library scan never checks a genre playlist's art"
        )

    def test_the_cover_pass_runs_for_every_artist_not_only_a_single_album(self):
        """⭐ THE GATE THAT CAUSED THE REPORT.

        The per-ALBUM refresh only ran when ``total_albums <= 1``, so an artist
        with several albums never reached any genre work at all. The cover
        pass must NOT carry that gate: it is per ARTIST, and it is cheap.

        The guard that wraps the cover call is what matters, so the window is
        the lines IMMEDIATELY above the call — the per-ALBUM refresh block
        above it legitimately keeps its own single-album gate (it re-pools the
        whole library, which is the expensive half).
        """
        src = (
            REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py"
        ).read_text(encoding="utf-8")

        idx = src.index("def _flush_artist_star_ratings(")
        end = src.index("\n    def ", idx + 10)
        body = src[idx:end]

        call = body.index("refresh_genre_playlist_covers_for_artist(")
        guard_window = body[max(0, call - 400):call]
        assert "total_albums" not in guard_window, (
            "the per-artist cover pass is still gated on a single-album scan — "
            "it would never run during a full library scan"
        )

    def test_the_cover_pass_is_reachable_outside_the_scan_runner(self):
        """The function must exist in the service module with the right shape."""
        from services.popularity.stages import finalise_stage as fs

        assert hasattr(fs, "refresh_genre_playlist_covers_for_artist")
        assert callable(fs.refresh_genre_playlist_covers_for_artist)


# ===========================================================================
# 2. The cover pass lists the playlists the artist feeds
# ===========================================================================
class TestThePlaylistsAreListed:
    def test_it_lists_the_playlists_the_artists_tracks_feed(self, cover_env, monkeypatch):
        """⭐ "It should list any playlist where the tracks are updated".

        The pass must REPORT which playlists it touched, not just apply art
        silently — a cover that appears with no trace is the report's other
        half.
        """
        _svc, _state, _calls = cover_env
        from services.popularity.stages import finalise_stage as fs

        logged: list[str] = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(str(msg)))

        monkeypatch.setattr(fs, "_genre_affected_genres_for_albums", lambda a, b: {"Nu Metal"})
        monkeypatch.setattr(fs, "_genre_playlist_ids_by_name", lambda: {
            "nu metal - top tracks": "pl-nm",
        })
        client = _FakeClient(playlists=[{"id": "pl-nm", "name": "Nu Metal - Top Tracks"}])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        checked = fs.refresh_genre_playlist_covers_for_artist("Test Artist", ["Some Album"])

        assert checked == ["Nu Metal - Top Tracks"], checked
        assert any("Nu Metal - Top Tracks" in m for m in logged), (
            "the affected playlists were not listed in the log"
        )

    def test_it_reports_a_playlist_whose_cover_could_not_be_attached(self, cover_env, monkeypatch):
        """A playlist that does not exist yet is LISTED but no image is spent.

        ⭐ The listing is the report's first half ("list any playlist where
        the tracks are updated"), so a playlist that has yet to be created on
        the server must still appear in it — it is the artist's tracks that
        touched it. What must NOT happen is an image being generated for a
        playlist with nothing to attach it to.
        """
        _svc, _state, calls = cover_env
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(fs, "_genre_affected_genres_for_albums", lambda a, b: {"Nu Metal"})
        # The playlist does not exist on the server yet.
        monkeypatch.setattr(fs, "_genre_playlist_ids_by_name", lambda: {})
        client = _FakeClient(playlists=[])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        touched = fs.refresh_genre_playlist_covers_for_artist("Test Artist", ["Some Album"])

        assert touched == ["Nu Metal - Top Tracks"], touched
        assert calls["images"] == 0, "no image may be spent on a missing playlist"
        assert client.uploads == [], "no image may be spent on a missing playlist"


# ===========================================================================
# 3. The cover is actually generated and uploaded
# ===========================================================================
class TestTheCoverIsGenerated:
    def test_a_playlist_without_our_art_gets_it(self, cover_env, monkeypatch):
        _svc, _state, calls = cover_env
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(fs, "_genre_affected_genres_for_albums", lambda a, b: {"Nu Metal"})
        monkeypatch.setattr(fs, "_genre_playlist_ids_by_name", lambda: {
            "nu metal - top tracks": "pl-nm",
        })
        client = _FakeClient(playlists=[{"id": "pl-nm", "name": "Nu Metal - Top Tracks"}])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        fs.refresh_genre_playlist_covers_for_artist("Test Artist", ["Some Album"])

        assert calls["images"] == 1, "no image was generated"
        assert client.uploads and client.uploads[0][0] == "pl-nm"

    def test_an_already_covered_playlist_is_not_re_billed(self, cover_env, monkeypatch):
        """The cost guard: a nightly scan must not re-make identical art."""
        svc, state_file, calls = cover_env
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(fs, "_genre_affected_genres_for_albums", lambda a, b: {"Nu Metal"})
        monkeypatch.setattr(fs, "_genre_playlist_ids_by_name", lambda: {
            "nu metal - top tracks": "pl-nm",
        })
        client = _FakeClient(playlists=[{"id": "pl-nm", "name": "Nu Metal - Top Tracks"}])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        fs.refresh_genre_playlist_covers_for_artist("Test Artist", ["Some Album"])
        fs.refresh_genre_playlist_covers_for_artist("Test Artist", ["Some Album"])

        assert calls["images"] == 1, "the image API was billed twice for one cover"

    def test_the_playlist_id_is_passed_so_no_extra_lookup_happens(self, cover_env, monkeypatch):
        """The id comes from the map, so the service must not look it up again."""
        _svc, _state, _calls = cover_env
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(fs, "_genre_affected_genres_for_albums", lambda a, b: {"Nu Metal"})
        monkeypatch.setattr(fs, "_genre_playlist_ids_by_name", lambda: {
            "nu metal - top tracks": "pl-nm",
        })
        client = _FakeClient(playlists=[{"id": "pl-nm", "name": "Nu Metal - Top Tracks"}])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        fs.refresh_genre_playlist_covers_for_artist("Test Artist", ["Some Album"])

        assert client.lookups == [], (
            "the playlist id was resolved twice — one extra Navidrome round "
            "trip per genre playlist per artist"
        )


# ===========================================================================
# 4. The misconfiguration is spelled out, not silent
# ===========================================================================
class TestTheMisconfigurationIsReported:
    def test_the_toggle_off_says_so(self, cover_env, monkeypatch):
        """A Gemini key WITHOUT the AI-covers toggle must say so."""
        _svc, _state, calls = cover_env
        import helpers.config_helpers as cfg_mod
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(cfg_mod, "get_genre_cover_config", lambda: {
            "enabled": False, "regenerate": False, "prompt_template": "",
        })

        logged: list[str] = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(str(msg)))

        checked = fs.refresh_genre_playlist_covers_for_artist("Test Artist", ["Some Album"])

        assert checked == []
        assert calls["images"] == 0
        assert any("AI Genre Covers" in m for m in logged), (
            "the AI-covers toggle being off was not reported — the user is told "
            "the Gemini key alone does not enable covers"
        )

    def test_a_missing_gemini_key_says_so(self, cover_env, monkeypatch):
        _svc, _state, calls = cover_env
        import helpers.config_helpers as cfg_mod
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(cfg_mod, "get_gemini_config", lambda: {
            "enabled": False, "api_key": "", "model": "test-model",
        })

        logged: list[str] = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(str(msg)))

        fs.refresh_genre_playlist_covers_for_artist("Test Artist", ["Some Album"])

        assert calls["images"] == 0
        assert any("Gemini" in m for m in logged), (
            "a missing Gemini key was not reported"
        )

    def test_it_never_raises(self, cover_env, monkeypatch):
        """An image API that is out of quota must not cost a scan its ratings."""
        _svc, _state, _calls = cover_env
        from services.popularity.stages import finalise_stage as fs

        def _boom(*a, **k):
            raise RuntimeError("navidrome is down")

        monkeypatch.setattr(fs, "_genre_affected_genres_for_albums", _boom)
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [])

        assert fs.refresh_genre_playlist_covers_for_artist("Test Artist", ["Some Album"]) == []


# ===========================================================================
# 5. The end-of-scan pass is still legible when it checks nothing
# ===========================================================================
class TestTheEndOfScanSummaryIsLegible:
    def test_an_enabled_cover_pass_that_checked_nothing_says_so(self):
        """⭐ "Enabled but nothing checked" must not be silent.

        The end-of-scan summary only printed when a playlist was checked, so a
        pass that never got that far produced NO line at all — which is
        indistinguishable from the feature not running.
        """
        src = (
            REPO_ROOT / "services" / "popularity" / "stages" / "finalise_stage.py"
        ).read_text(encoding="utf-8")

        assert "_cover_enabled" in src, (
            "the pass does not record that the cover feature was ENABLED — so "
            "'enabled but nothing checked' is indistinguishable from 'off'"
        )

        idx = src.index("if _cover_checked:")
        between = src[idx: idx + 1200]
        assert "elif _cover_enabled:" in between, (
            "an enabled cover pass that checked nothing produces no log line"
        )

    def test_the_disabled_case_is_still_distinguished(self):
        """The 'feature off' case must keep its own explanation."""
        src = (
            REPO_ROOT / "services" / "popularity" / "stages" / "finalise_stage.py"
        ).read_text(encoding="utf-8")

        idx = src.index("if _cover_checked:")
        between = src[idx: idx + 2000]
        assert "elif _cover_flag_off:" in between, (
            "the 'covers are OFF' explanation was lost"
        )
        assert "AI covers for Top Genre" in between, (
            "the OFF message no longer names the control to tick"
        )
