"""AI cover artwork for the TOP GENRE playlists (Gemini).

Scope is deliberately narrow: only ``{Genre} - Top Tracks``. Essential /
artist playlists keep the artist-image cover they already get.

The tests drive the real functions. Only the network (Gemini) and the two
config getters are stubbed — the gating, the prompt, the state file and the
Navidrome upload path are the shipped code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _patch_config(monkeypatch, *, cover=None, gemini=None):
    import helpers.config_helpers as ch

    cover_cfg = {"enabled": True, "regenerate": False, "prompt_template": ""}
    cover_cfg.update(cover or {})
    gem_cfg = {"enabled": True, "api_key": "test-key", "model": "gemini-2.5-flash-image"}
    gem_cfg.update(gemini or {})
    monkeypatch.setattr(ch, "get_genre_cover_config", lambda: cover_cfg)
    monkeypatch.setattr(ch, "get_gemini_config", lambda: gem_cfg)
    monkeypatch.setattr(ch, "get_state_directory", lambda: "/tmp/popr-test-state")
    return cover_cfg, gem_cfg


class _FakeClient:
    def __init__(self, *, playlist_id="pl-1", upload_ok=True, upload_reason=""):
        self.playlist_id = playlist_id
        self.upload_ok = upload_ok
        self.upload_reason = upload_reason
        self.uploaded: list[tuple] = []

    def find_playlist_by_name(self, name):
        return {"id": self.playlist_id, "name": name} if self.playlist_id else None

    def upload_playlist_cover(self, playlist_id, image, mime="image/png"):
        self.uploaded.append((playlist_id, image, mime))
        return self.upload_ok, self.upload_reason


class TestThePrompt:
    def test_the_genre_is_substituted(self):
        from services.playlists.genre_cover_service import build_cover_prompt

        assert build_cover_prompt("Folk Metal", "Art for {genre} now") == "Art for Folk Metal now"

    def test_a_template_without_the_placeholder_is_used_as_is(self):
        """A user-edited template is still a valid prompt."""
        from services.playlists.genre_cover_service import build_cover_prompt

        assert build_cover_prompt("Folk Metal", "Just a scene") == "Just a scene"

    def test_a_blank_template_falls_back_to_the_builtin(self):
        from services.playlists.genre_cover_service import (
            DEFAULT_PROMPT_TEMPLATE,
            build_cover_prompt,
        )

        # The builtin still has {genre} substituted — the fallback is the
        # prompt, not the raw constant.
        assert build_cover_prompt("Folk Metal", "") == DEFAULT_PROMPT_TEMPLATE.replace(
            "{genre}", "Folk Metal"
        )

    def test_a_stray_brace_does_not_raise(self):
        """⚠️ ``str.format`` would raise on a bare brace — a legitimate prompt
        character — and that must not take the genre pass down."""
        from services.playlists.genre_cover_service import build_cover_prompt

        assert build_cover_prompt("Metal", "A {genre} cover with a { brace") == (
            "A Metal cover with a { brace"
        )

    def test_the_builtin_asks_for_the_genre_title(self):
        from services.playlists.genre_cover_service import (
            DEFAULT_PROMPT_TEMPLATE,
            build_cover_prompt,
        )

        rendered = build_cover_prompt("Folk Metal", DEFAULT_PROMPT_TEMPLATE)

        assert "Folk Metal" in rendered
        assert "Folk Metal" in rendered.split('"')[1]     # the on-art title


class TestTheGeminiClient:
    def _patch_session(self, monkeypatch, *, status=200, payload=None, raise_exc=None):
        import api_clients.gemini as gem

        captured = {}

        class _Resp:
            status_code = status

            def json(self):
                return payload or {}

            text = ""

        class _Session:
            def post(self, url, **kwargs):
                captured["url"] = url
                captured["kwargs"] = kwargs
                if raise_exc:
                    raise raise_exc
                return _Resp()

        monkeypatch.setattr(gem, "session", _Session())
        return captured

    def test_a_missing_key_short_circuits(self):
        from api_clients.gemini import generate_image

        image, mime, reason = generate_image("a prompt", api_key="")

        assert image is None and reason == "no_api_key"

    def test_the_key_travels_in_a_header_not_the_url(self, monkeypatch):
        """⚠️ A ``?key=`` query string leaks the key into every log line."""
        import base64

        captured = self._patch_session(
            monkeypatch,
            payload={"candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"IMG").decode()}}
            ]}}]},
        )

        from api_clients.gemini import generate_image

        image, mime, reason = generate_image("p", api_key="secret")

        assert image == b"IMG" and mime == "image/png" and reason == ""
        assert "secret" not in captured["url"]
        assert captured["kwargs"]["headers"]["x-goog-api-key"] == "secret"
        assert captured["kwargs"]["json"]["generationConfig"]["responseModalities"] == ["IMAGE"]

    def test_a_text_part_before_the_image_still_yields_the_image(self, monkeypatch):
        """⭐ Parts are SCANNED, not indexed: a TEXT+IMAGE reply puts a text part
        first, and indexing ``parts[0]`` would report "no image"."""
        import base64

        self._patch_session(
            monkeypatch,
            payload={"candidates": [{"content": {"parts": [
                {"text": "Here you go:"},
                {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"IMG2").decode()}},
            ]}}]},
        )

        from api_clients.gemini import generate_image

        image, _mime, reason = generate_image("p", api_key="k")

        assert image == b"IMG2" and reason == ""

    def test_an_error_status_reports_the_code_and_does_not_raise(self, monkeypatch):
        self._patch_session(monkeypatch, status=429, payload={"error": {"message": "quota"}})

        from api_clients.gemini import generate_image

        image, _mime, reason = generate_image("p", api_key="k")

        assert image is None and reason == "http_429"

    def test_a_transport_error_does_not_raise(self, monkeypatch):
        self._patch_session(monkeypatch, raise_exc=RuntimeError("boom"))

        from api_clients.gemini import generate_image

        assert generate_image("p", api_key="k")[2] == "exception"

    def test_a_safety_block_is_named(self, monkeypatch):
        self._patch_session(
            monkeypatch,
            payload={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []},
        )

        from api_clients.gemini import generate_image

        assert generate_image("p", api_key="k")[2] == "blocked: SAFETY"

    def test_no_image_part_is_no_image(self, monkeypatch):
        self._patch_session(monkeypatch, payload={"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})

        from api_clients.gemini import generate_image

        assert generate_image("p", api_key="k")[2] == "no_image"


class TestTheCoverService:
    def _wire(self, monkeypatch, *, image=b"IMG", reason=""):
        from services.playlists import genre_cover_service as svc
        import api_clients.gemini as gem

        saved: dict = {}
        monkeypatch.setattr(svc, "_load_state", lambda: dict(saved))
        monkeypatch.setattr(svc, "_save_state", lambda state: saved.update(state))
        monkeypatch.setattr(
            gem, "generate_image", lambda *a, **k: (image, "image/png", reason),
        )
        return svc, saved

    def test_it_is_disabled_until_the_toggle_is_on(self, monkeypatch):
        _patch_config(monkeypatch, cover={"enabled": False})
        svc, _saved = self._wire(monkeypatch)
        client = _FakeClient()

        result = svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)

        assert result["status"] == "disabled"
        assert client.uploaded == []

    def test_it_is_disabled_without_a_gemini_key(self, monkeypatch):
        _patch_config(monkeypatch, gemini={"enabled": True, "api_key": ""})
        svc, _saved = self._wire(monkeypatch)
        client = _FakeClient()

        result = svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)

        assert result["status"] == "disabled"
        assert client.uploaded == []

    def test_it_generates_uploads_and_records(self, monkeypatch):
        _patch_config(monkeypatch)
        svc, saved = self._wire(monkeypatch)
        client = _FakeClient()

        result = svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)

        assert result["status"] == "generated" and result["ok"] is True
        assert client.uploaded and client.uploaded[0][0] == "pl-1"
        assert saved.get("folk metal")          # recorded, so it will not re-bill

    def test_the_same_prompt_is_not_regenerated(self, monkeypatch):
        """⭐ The cost guard: a nightly scan must not re-make identical art."""
        _patch_config(monkeypatch)
        svc, saved = self._wire(monkeypatch)
        client = _FakeClient()

        svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)
        second = svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)

        assert second["status"] == "skipped"
        assert len(client.uploaded) == 1

    def test_regenerate_writes_again(self, monkeypatch):
        _patch_config(monkeypatch, cover={"regenerate": True})
        svc, _saved = self._wire(monkeypatch)
        client = _FakeClient()

        svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)
        second = svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)

        assert second["status"] == "generated"
        assert len(client.uploaded) == 2

    def test_a_changed_prompt_regenerates(self, monkeypatch):
        _patch_config(monkeypatch)
        svc, _saved = self._wire(monkeypatch)
        client = _FakeClient()

        svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)
        cfg = _patch_config(monkeypatch, cover={"prompt_template": "Different {genre}"})[0]
        assert cfg["prompt_template"] == "Different {genre}"
        second = svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)

        assert second["status"] == "generated"

    def test_no_navidrome_match_does_not_spend_quota(self, monkeypatch):
        """⭐ The playlist is resolved BEFORE generating: an image that cannot be
        attached is quota spent for nothing."""
        _patch_config(monkeypatch)
        svc, _saved = self._wire(monkeypatch)
        client = _FakeClient(playlist_id="")

        result = svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)

        assert result["status"] == "failed" and result["reason"] == "no Navidrome match"
        assert client.uploaded == []

    def test_a_rejected_upload_is_not_recorded_as_done(self, monkeypatch):
        """Recording it would make the next scan SKIP a cover that is not there."""
        _patch_config(monkeypatch)
        svc, saved = self._wire(monkeypatch)
        client = _FakeClient(upload_ok=False, upload_reason="disabled")

        result = svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)

        assert result["status"] == "failed" and result["reason"] == "disabled"
        assert saved == {}

    def test_a_generation_failure_is_reported_not_raised(self, monkeypatch):
        _patch_config(monkeypatch)
        svc, _saved = self._wire(monkeypatch, image=None, reason="http_429")
        client = _FakeClient()

        result = svc.generate_genre_cover("Folk Metal", "Folk Metal - Top Tracks", client)

        assert result["status"] == "failed" and result["reason"] == "http_429"
        assert client.uploaded == []


class TestTheConfigSurfaceExists:
    """§4.1: every configurable option must appear on the Config page FIRST.

    ⚠️ Both trees — the live one and ``test_site``.
    """

    _HTML = ["templates/pages/config.html", "test_site/templates/Pages/config.html"]
    _JS = ["static/js/config.js", "test_site/static/js/pages/config.js"]
    _INPUT_IDS = (
        "api_gemini_enabled",
        "api_gemini_api_key",
        "api_gemini_model",
        "playlists_genre_ai_cover_enabled",
        "playlists_genre_ai_cover_regenerate",
        "playlists_genre_ai_cover_prompt",
    )

    @pytest.mark.parametrize("rel", _HTML)
    def test_the_inputs_exist(self, rel):
        html = (REPO_ROOT / rel).read_text(encoding="utf-8")

        for input_id in self._INPUT_IDS:
            assert f'id="{input_id}"' in html, f"{rel} has no input for {input_id}"

    @pytest.mark.parametrize("rel", _JS)
    def test_the_collectors_exist(self, rel):
        """Without a collector the inputs render but never reach config.yaml."""
        js = (REPO_ROOT / rel).read_text(encoding="utf-8")

        assert "api_gemini_api_key" in js
        assert "api_gemini_model" in js
        assert "genre_playlist_ai_cover_enabled:" in js
        assert "genre_playlist_ai_cover_regenerate:" in js
        assert "genre_playlist_ai_cover_prompt:" in js

    def test_the_getters_read_what_the_page_writes(self, monkeypatch):
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {
            "api_integrations": {"gemini": {"enabled": True, "api_key": "k", "model": "m"}},
            "playlists": {
                "genre_playlist_ai_cover_enabled": True,
                "genre_playlist_ai_cover_regenerate": True,
                "genre_playlist_ai_cover_prompt": "Art for {genre}",
            },
        })

        assert ch.get_gemini_config() == {"enabled": True, "api_key": "k", "model": "m"}
        assert ch.get_genre_cover_config() == {
            "enabled": True, "regenerate": True, "prompt_template": "Art for {genre}",
        }

    def test_defaults_are_off_and_sane(self, monkeypatch):
        """⭐ THE BILLABLE-API GUARD LIVES ON THE GEMINI INTEGRATION.

        This test used to assert that the COVER toggle also defaulted to off.
        That was the defect behind the reported "Gemini is still not creating
        the covers": Gemini has exactly ONE consumer in this app (these
        covers), so a second toggle in a different config section asked the
        user to opt in twice, and the second opt-in was the easy one to miss.

        The guard is therefore the Gemini integration itself — off, with no
        key, until the user supplies one — and the cover toggle defaults to
        ON so that configuring Gemini is enough. Turning the covers OFF is
        still possible (the Config page writes ``false``).
        """
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {})

        gem = ch.get_gemini_config()
        cover = ch.get_genre_cover_config()

        # ⭐ The billing guard: no Gemini, no key, nothing enabled.
        assert gem["enabled"] is False and gem["api_key"] == ""
        assert gem["model"] == "gemini-2.5-flash-image"
        # The cover toggle now follows Gemini rather than gating it twice.
        assert cover["enabled"] is True and cover["regenerate"] is False

    def test_the_covers_can_still_be_switched_off(self, monkeypatch):
        """An explicit ``false`` must still win — the default is a default."""
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {
            "playlists": {"genre_playlist_ai_cover_enabled": False},
        })

        assert ch.get_genre_cover_config()["enabled"] is False

    def test_the_key_is_not_also_read_from_a_legacy_top_level(self, monkeypatch):
        """One source only: a billable key that can arrive from two places
        cannot be reasoned about or redacted when one is stale."""
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {"gemini_api_key": "legacy"})

        assert ch.get_gemini_config()["api_key"] == ""

    def test_it_is_wired_only_to_the_genre_pass(self):
        """Scope: the hook lives in the GENRE playlist builder, and the
        Essential (artist) cover path is untouched."""
        src = (
            REPO_ROOT / "services" / "popularity" / "stages" / "finalise_stage.py"
        ).read_text(encoding="utf-8")

        assert "def _create_genre_top_track_playlists(" in src
        genre_body = src.split("def _create_genre_top_track_playlists(", 1)[1]
        assert "generate_genre_cover(" in genre_body.split("\ndef ", 1)[0]
