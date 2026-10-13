"""Supported download formats: reject unsupported containers, do not import them.

Reported
--------
> I've had a few tracks download as aiff files such as
> ``/music/Various Artists/2002 - MTV2 Handpicked/07. Various Artists - Sometimes.aiff``
> These tracks have copied over but not applied the metadata.  Can we have an
> option in config for supported filetypes?  Have only flac and mp3 enabled by
> default, if any accidentally download that arent that file type it should
> reject them from the download queue rather than matching.

Why AIFF landed tagless: ``services/metadata/tag_file_service.py`` can only
WRITE tags for ``.mp3/.flac/.m4a/.mp4``. Any other container — AIFF, WAV,
ALAC, DSF, APE, WV — hits the "Unsupported file format" branch, so the file is
copied to the library with NO metadata written. The fix is to not accept those
containers in the first place:

* ``downloads.quality.allowed_formats`` (default ``["flac", "mp3"]``) — the
  set of containers a download may be accepted in;
* the Soulseek SEARCH rejects a disallowed container (never offered as a
  match), so it is not chosen;
* the COMPLETION match rejects a disallowed container that slipped through,
  deleting it and rescheduling the item rather than importing it tagless.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Config getter
# ---------------------------------------------------------------------------

class TestTheConfigGetter:
    @staticmethod
    def _get(monkeypatch, quality: dict):
        from helpers import config_helpers

        monkeypatch.setattr(
            config_helpers, "get_config", lambda: {"downloads": {"quality": quality}}
        )
        return config_helpers.get_search_quality_config()

    def test_default_is_flac_and_mp3(self, monkeypatch) -> None:
        cfg = self._get(monkeypatch, {})
        assert cfg["allowed_formats"] == {"flac", "mp3"}

    def test_it_normalises_dots_and_case(self, monkeypatch) -> None:
        """A hand-edited config.yaml may spell it ``.FLAC`` / ``MP3``."""
        cfg = self._get(monkeypatch, {"allowed_formats": [".FLAC", "Mp3", " .ogg "]})
        assert cfg["allowed_formats"] == {"flac", "mp3", "ogg"}

    def test_an_empty_list_means_no_restriction(self, monkeypatch) -> None:
        cfg = self._get(monkeypatch, {"allowed_formats": []})
        assert cfg["allowed_formats"] == set(), (
            "an empty allow-list must disable the check, not reject everything"
        )

    def test_it_keeps_the_existing_quality_keys(self, monkeypatch) -> None:
        cfg = self._get(monkeypatch, {"min_bitrate": 256})
        assert cfg["min_bitrate"] == 256
        assert "min_sample_rate" in cfg and "fallback_min_bitrate" in cfg


# ---------------------------------------------------------------------------
# 2. Search gate — SearchFile.matches_quality
# ---------------------------------------------------------------------------

class TestTheSearchGate:
    @staticmethod
    def _file(**kw):
        from services.downloads.slskd_service import SearchFile

        base = dict(filename="Music/a.flac", size=0, bitrate=0, sample_rate=44100, length=200)
        base.update(kw)
        return SearchFile(**base)

    def test_an_aiff_is_rejected_when_only_flac_and_mp3_are_allowed(self) -> None:
        reasons: dict[str, int] = {}
        f = self._file(filename="Music/07 - Sometimes.aiff", extension="aiff")
        # Without the gate an AIFF is "lossless" and sails through.
        assert f.is_lossless is True
        assert f.matches_quality(allowed_formats={"flac", "mp3"}, reject_reasons=reasons) is False
        assert reasons.get("format_not_allowed") == 1

    def test_flac_and_mp3_pass(self) -> None:
        assert self._file(extension="flac").matches_quality(allowed_formats={"flac", "mp3"})
        assert self._file(
            filename="Music/a.mp3", extension="mp3", bitrate=320
        ).matches_quality(allowed_formats={"flac", "mp3"})

    def test_the_extension_is_read_from_the_filename_when_absent(self) -> None:
        """The gate must work off the derived extension too (payload may omit it)."""
        f = self._file(filename="Music/song.aiff")
        assert f.extension == "aiff"
        assert f.matches_quality(allowed_formats={"flac", "mp3"}) is False

    def test_no_allow_list_leaves_behaviour_unchanged(self) -> None:
        """CONTROL — None / empty means no restriction (back-compat)."""
        f = self._file(filename="Music/song.aiff", extension="aiff")
        assert f.matches_quality(allowed_formats=None) is True
        assert f.matches_quality(allowed_formats=set()) is True

    def test_filter_results_by_quality_drops_the_disallowed_container(self) -> None:
        from services.downloads.slskd_service import SearchResponse, SlskdService

        resp = SearchResponse(username="peer", files=[
            {"filename": "Music/a.flac", "size": 0, "bitRate": 0, "sampleRate": 44100, "length": 200},
            {"filename": "Music/a.aiff", "size": 0, "bitRate": 0, "sampleRate": 44100, "length": 200},
        ])
        svc = SlskdService(http_client=None)
        reasons: dict[str, int] = {}
        out = svc.filter_results_by_quality(
            [resp], allowed_formats={"flac", "mp3"}, reject_reasons=reasons
        )
        names = [r["filename"] for r in out]
        assert names == ["Music/a.flac"], names
        assert reasons.get("format_not_allowed") == 1


# ---------------------------------------------------------------------------
# 3. Completion gate — an already-downloaded wrong type is rejected
# ---------------------------------------------------------------------------

class TestTheCompletionGate:
    def test_an_aiff_never_matches_the_queue_item(self, monkeypatch) -> None:
        from helpers import config_helpers
        from services.downloads import download_completion_service as dcs

        monkeypatch.setattr(
            config_helpers, "get_config", lambda: {"downloads": {"quality": {}}}
        )
        is_match, source = dcs._file_matches_queue_item(
            "/downloads/Music/07 - Sometimes.aiff", {"artist": "X", "title": "Sometimes"}
        )
        assert is_match is False
        assert source == "unsupported_format", source

    def test_a_non_allowed_extension_is_not_reported_as_a_metadata_mismatch(
        self, monkeypatch
    ) -> None:
        """The failure message must name the FORMAT, not a matching bug."""
        from services.downloads.download_completion_service import (
            _queue_reject_failure_reason,
        )

        msg = _queue_reject_failure_reason("unsupported_format")
        assert "format" in msg.lower()
        assert "did not match queue item" not in msg
        # A genuine mismatch keeps the original wording.
        assert "did not match queue item" in _queue_reject_failure_reason("metadata")


# ---------------------------------------------------------------------------
# 4. Config page contract (the option must be on the page — sources of truth)
# ---------------------------------------------------------------------------

class TestTheConfigPageExposesIt:
    @pytest.mark.parametrize(
        "page",
        ["templates/pages/config.html", "test_site/templates/Pages/config.html"],
    )
    def test_the_checkboxes_are_on_both_config_pages(self, page: str) -> None:
        src = (REPO_ROOT / page).read_text(encoding="utf-8")
        assert "download-format-check" in src
        assert "Allowed Download Formats" in src
        # The ids are generated by the Jinja loop, so the SOURCE carries the
        # template expression — and flac/mp3 must be in the offered set.
        assert "downloads_format_{{ _fmt }}" in src
        assert "('flac','FLAC')" in src and "('mp3','MP3')" in src

    @pytest.mark.parametrize(
        "js",
        [REPO_ROOT / "static/js/config.js", REPO_ROOT / "test_site/static/js/pages/config.js"],
    )
    def test_both_collectors_read_the_checkbox_group(self, js: Path) -> None:
        src = js.read_text(encoding="utf-8")
        assert "allowed_formats" in src, f"{js} does not persist allowed_formats"
        assert ".download-format-check:checked" in src, (
            f"{js} must collect the checked formats or the setting silently reverts"
        )

    async def test_the_rendered_config_page_has_the_checkboxes(self, client) -> None:
        """A source grep can pass on a block Jinja never emits — render it.

        The rendered page must show the checkbox group AND default FLAC+MP3
        checked (so the shipped default is visibly flac+mp3).
        """
        resp = await client.get("/config")
        assert resp.status_code == 200
        html = await resp.get_data(as_text=True)
        assert "download-format-check" in html, "the checkbox group did not render"
        assert "Allowed Download Formats" in html
        # FLAC and MP3 ship checked; the other allowed types (e.g. WAV) do not.
        import re as _re

        def _checked(fmt: str) -> bool:
            m = _re.search(r'<input[^>]*id="downloads_format_' + fmt + r'"[^>]*>', html)
            return bool(m and "checked" in m.group(0))

        assert _checked("flac") and _checked("mp3")
        assert not _checked("wav"), "WAV must not be enabled by default"
