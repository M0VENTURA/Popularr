"""The genre cover check must verify the cover, not merely the prompt hash.

Reported: a scan whose log showed all 75 genre playlists present and synced had
NO cover lines at all, prompting the question *"can the cover be checked if it
exists when updating a playlist with new tracks?"*

Root cause: ``generate_genre_cover`` skipped whenever the state file's prompt
hash matched — WITHOUT knowing whether the playlist actually carries the cover.
The hash records "we once made this image for this genre", not "this playlist
still has it". So a playlist that was DELETED and RECREATED (new id, no image),
or one whose cover was removed by hand, was skipped forever.

The state entry is now ``{"hash": ..., "playlist_id": ...}`` and the skip
requires BOTH to match the current playlist. A legacy ``{genre: hash}`` entry
carries no id, so it is treated as UNCONFIRMED and applied once (upgrading the
entry) — which is what heals an install whose covers are missing.

These tests drive the real service; only Gemini and the two config getters are
stubbed, and the state file is a real temp file so the format round-trips.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

GENRE = "Nu Metal"
PLAYLIST = "Nu Metal - Top Tracks"
GENRE_KEY = GENRE.casefold()


class _FakeClient:
    def __init__(self, playlist):
        self._playlist = playlist
        self.lookups = 0
        self.uploads: list[tuple] = []

    def find_playlist_by_name(self, name):
        self.lookups += 1
        return self._playlist if self._playlist and self._playlist.get("id") else None

    def upload_playlist_cover(self, playlist_id, image, mime="image/png"):
        self.uploads.append((playlist_id, image, mime))
        return True, ""


@pytest.fixture
def cover_env(tmp_path, monkeypatch):
    """The real service with a real temp state file and a stubbed Gemini.

    The default prompt embeds a RANDOM creative stimulus, so the SAME args
    produce DIFFERENT prompts on consecutive calls. These tests assert
    deterministic skip/upgrade behaviour, so a deterministic custom template
    is used — both ``_digest`` and the service itself render the same prompt,
    and the skip/upgrade logic is exercised on a reproducible hash."""
    import api_clients.gemini as gemini_mod
    import helpers.config_helpers as cfg_mod
    from services.playlists import genre_cover_service as svc

    state_file = tmp_path / "genre_playlist_covers.json"
    monkeypatch.setattr(svc, "_state_path", lambda: str(state_file))

    monkeypatch.setattr(cfg_mod, "get_genre_cover_config", lambda: {
        "enabled": True, "regenerate": False,
        "prompt_template": "Square album cover for a {genre} playlist.",
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


def _digest(svc) -> str:
    # Match the configured template the service itself uses — the empty
    # string would fall back to the random default and never match.
    return svc._prompt_hash(svc.build_cover_prompt(GENRE, _DIGEST_TEMPLATE))


#: The deterministic template used by the digest helper — must match the
#: ``prompt_template`` the ``cover_env`` fixture configures.
_DIGEST_TEMPLATE = "Square album cover for a {genre} playlist."


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# 1. The state entry reader
# ===========================================================================
class TestStateEntry:
    def test_a_new_style_entry_yields_hash_and_playlist(self):
        from services.playlists.genre_cover_service import _state_entry

        assert _state_entry({GENRE_KEY: {"hash": "abc", "playlist_id": "pl-1"}}, GENRE_KEY) == (
            "abc", "pl-1",
        )

    def test_a_legacy_bare_hash_yields_no_playlist(self):
        """Legacy means "we cannot say which playlist" — NOT "it matches"."""
        from services.playlists.genre_cover_service import _state_entry

        assert _state_entry({GENRE_KEY: "abc"}, GENRE_KEY) == ("abc", "")

    def test_a_missing_entry_is_empty(self):
        from services.playlists.genre_cover_service import _state_entry

        assert _state_entry({}, GENRE_KEY) == ("", "")

    def test_load_state_preserves_dict_entries(self, cover_env):
        """The old ``str(v)`` coercion flattened the dict and lost the id."""
        svc, state_file, _calls = cover_env
        _write(state_file, {GENRE_KEY: {"hash": "abc", "playlist_id": "pl-1"}})

        assert svc._load_state()[GENRE_KEY] == {"hash": "abc", "playlist_id": "pl-1"}


# ===========================================================================
# 2. A cover that is not confirmed on THIS playlist is re-applied
# ===========================================================================
class TestLegacyStateIsNotProofTheCoverExists:
    def test_a_legacy_hash_entry_re_applies_the_cover(self, cover_env):
        svc, state_file, calls = cover_env
        # A pre-existing install: {genre: hash}, no playlist recorded.
        _write(state_file, {GENRE_KEY: _digest(svc)})

        client = _FakeClient({"id": "pl-1", "name": PLAYLIST})
        result = svc.generate_genre_cover(GENRE, PLAYLIST, client, playlist_id="pl-1")

        assert result["status"] == "generated", result
        assert calls["images"] == 1, (
            "a legacy entry cannot prove the cover is on this playlist, so the "
            "cover must be (re)applied once"
        )
        assert client.uploads and client.uploads[0][0] == "pl-1"

    def test_the_legacy_entry_is_upgraded_to_carry_the_id(self, cover_env):
        svc, state_file, _calls = cover_env
        _write(state_file, {GENRE_KEY: _digest(svc)})

        client = _FakeClient({"id": "pl-1", "name": PLAYLIST})
        svc.generate_genre_cover(GENRE, PLAYLIST, client, playlist_id="pl-1")

        assert _read(state_file)[GENRE_KEY] == {
            "hash": _digest(svc), "playlist_id": "pl-1",
        }

    def test_the_upgrade_only_costs_one_extra_image(self, cover_env):
        """The second call — now confirmed — must skip and not re-bill."""
        svc, state_file, calls = cover_env
        _write(state_file, {GENRE_KEY: _digest(svc)})

        client = _FakeClient({"id": "pl-1", "name": PLAYLIST})
        svc.generate_genre_cover(GENRE, PLAYLIST, client, playlist_id="pl-1")
        second = svc.generate_genre_cover(GENRE, PLAYLIST, client, playlist_id="pl-1")

        assert second["status"] == "skipped"
        assert calls["images"] == 1


class TestARecreatedPlaylistIsReCovered:
    def test_a_new_playlist_id_under_the_same_name_regenerates(self, cover_env):
        svc, state_file, calls = cover_env
        # The cover was applied to a playlist since deleted; the NAME still
        # resolves, but the new playlist has a different id and no image. This
        # is exactly what the old hash-only check skipped forever.
        _write(state_file, {GENRE_KEY: {"hash": _digest(svc), "playlist_id": "old-id"}})

        client = _FakeClient({"id": "new-id", "name": PLAYLIST})
        result = svc.generate_genre_cover(GENRE, PLAYLIST, client, playlist_id="new-id")

        assert result["status"] == "generated", result
        assert calls["images"] == 1, (
            "a recreated playlist carries no cover — it must be re-made"
        )
        assert _read(state_file)[GENRE_KEY]["playlist_id"] == "new-id"


class TestAConfirmedCoverIsSkipped:
    def test_matching_hash_and_playlist_skips_without_an_image(self, cover_env):
        svc, state_file, calls = cover_env
        _write(state_file, {
            GENRE_KEY: {"hash": _digest(svc), "playlist_id": "pl-1"},
        })

        client = _FakeClient({"id": "pl-1", "name": PLAYLIST})
        result = svc.generate_genre_cover(GENRE, PLAYLIST, client, playlist_id="pl-1")

        assert result["status"] == "skipped", result
        assert calls["images"] == 0, "a confirmed cover must not re-bill the image API"
        assert client.uploads == []

    def test_a_different_prompt_regenerates(self, cover_env):
        svc, state_file, calls = cover_env
        _write(state_file, {
            GENRE_KEY: {"hash": "stale-hash", "playlist_id": "pl-1"},
        })

        client = _FakeClient({"id": "pl-1", "name": PLAYLIST})
        result = svc.generate_genre_cover(GENRE, PLAYLIST, client, playlist_id="pl-1")

        assert result["status"] == "generated"
        assert calls["images"] == 1


# ===========================================================================
# 3. Playlist id plumbing
# ===========================================================================
class TestPlaylistIdPlumbing:
    def test_a_supplied_id_avoids_a_lookup(self, cover_env):
        svc, _state_file, _calls = cover_env
        client = _FakeClient(None)  # a lookup would find nothing

        result = svc.generate_genre_cover(GENRE, PLAYLIST, client, playlist_id="pl-9")

        assert client.lookups == 0, (
            "the caller already resolved the playlist from its own "
            "getPlaylists fetch; the service must not re-query Navidrome"
        )
        assert result["playlist_id"] == "pl-9"
        assert result["status"] == "generated"

    def test_without_an_id_the_service_resolves_the_playlist(self, cover_env):
        svc, _state_file, _calls = cover_env
        client = _FakeClient({"id": "pl-2", "name": PLAYLIST})

        result = svc.generate_genre_cover(GENRE, PLAYLIST, client)

        assert client.lookups == 1
        assert result["playlist_id"] == "pl-2"

    def test_no_match_is_a_failure_not_a_generation(self, cover_env):
        svc, _state_file, calls = cover_env
        client = _FakeClient(None)

        result = svc.generate_genre_cover(GENRE, PLAYLIST, client)

        assert result["status"] == "failed"
        assert result["reason"] == "no Navidrome match"
        assert calls["images"] == 0, "never spend an image we cannot attach"


# ===========================================================================
# 4. The genre pass passes the id and reports the outcome
# ===========================================================================
class TestTheGenrePassWiresTheCheck:
    def _genre_body(self) -> str:
        src = (
            REPO_ROOT / "services" / "popularity" / "stages" / "finalise_stage.py"
        ).read_text(encoding="utf-8")
        return src.split("def _create_genre_top_track_playlists(", 1)[1].split("\ndef ", 1)[0]

    def test_the_playlist_id_map_is_built_from_the_existing_fetch(self):
        body = self._genre_body()
        assert "existing_playlist_ids" in body, (
            "the pass must capture playlist ids from the fetch it already does, "
            "so the cover check costs no extra round trip"
        )
        assert "playlist_id=existing_playlist_ids.get(" in body

    def test_the_cover_outcome_is_summarised(self):
        body = self._genre_body()
        assert "Genre playlist covers —" in body, (
            "covers enabled but silent is indistinguishable from the feature "
            "not running — the pass must say how many it checked"
        )
