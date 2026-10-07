"""Every imported download gets the album's cover art embedded.

Reported:

> I've also noticed that the album art isn't always downloading with the
> metadata for the downloaded tracks.

Root cause: nothing in the import path embedded art. The completion wrote the
stored metadata to the file and moved it — cover art only arrived if a LATER
happened to run its art pass (config-dependent, album-level), which is exactly
"isn't always". The import now embeds the album's art itself, after the tag
write (the writer may rebuild frames, which would drop art embedded first),
via the existing ``embed_album_art`` helper (MP3 APIC + FLAC pictures).

Sources, in order: the queue row's own cover URL (a manual MusicBrainz match
stores one) → the album-art cache/providers (stored → Navidrome →
MusicBrainz/CAA → Discogs …) → Cover Art Archive by the release MBID. Art is
enrichment: a failure is logged, never allowed to fail the import.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.downloads import download_completion_service as completion  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8


class TestTheMimeSniff:
    def test_jpeg(self):
        assert completion._sniff_image_mime(JPEG) == "image/jpeg"

    def test_png(self):
        assert completion._sniff_image_mime(PNG) == "image/png"

    def test_webp(self):
        assert completion._sniff_image_mime(WEBP) == "image/webp"

    def test_unknown_bytes_default_to_jpeg(self):
        assert completion._sniff_image_mime(b"not-an-image") == "image/jpeg"


class TestTheArtSources:
    def test_the_queue_rows_cover_url_wins(self, monkeypatch):
        """A manual MusicBrainz match stores a cover URL on the row."""
        import httpx

        class _Resp:
            status_code = 200
            content = JPEG

        monkeypatch.setattr(httpx, "get", lambda url, timeout=10: _Resp())

        art = completion._fetch_import_art({
            "id": 1,
            "cover_art_url": "https://coverartarchive.org/release/x/front.jpg",
            "artist": "Madball",
            "album": "Greatest Hits",
        })

        assert art == (JPEG, "image/jpeg")

    def test_the_album_art_cache_is_next(self, monkeypatch):
        """No row URL → the album-art pipeline (stored → Navidrome → MB/CAA…)."""
        monkeypatch.setattr(
            "services.enrichment.album_art_service.get_or_fetch_album_art",
            lambda artist, album, discogs_token="": (PNG, None),
        )

        art = completion._fetch_import_art({"id": 1, "artist": "Madball", "album": "Greatest Hits"})

        assert art == (PNG, "image/png"), "mime falls back to the sniff when the helper returns none"

    def test_cover_art_archive_by_release_mbid_is_the_last_resort(self, monkeypatch):
        monkeypatch.setattr(
            "services.enrichment.album_art_service.get_or_fetch_album_art",
            lambda artist, album, discogs_token="": (None, None),
        )
        monkeypatch.setattr(
            "api_clients.coverartarchive.get_release_front_image_bytes",
            lambda mbid, size="500": JPEG,
        )

        art = completion._fetch_import_art({
            "id": 1,
            "artist": "Madball",
            "album": "Greatest Hits",
            "release_mbid": "11111111-2222-3333-4444-555555555555",
        })

        assert art == (JPEG, "image/jpeg")

    def test_no_source_means_no_art(self, monkeypatch):
        monkeypatch.setattr(
            "services.enrichment.album_art_service.get_or_fetch_album_art",
            lambda artist, album, discogs_token="": (None, None),
        )

        assert completion._fetch_import_art({"id": 1, "artist": "A", "album": "B"}) is None


class TestTheImportEmbedsTheArt:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch, tmp_path):
        state = {"calls": [], "file": str(tmp_path / "01 - Song.flac")}  # ordered: ("tags", …) / ("art", …)
        Path(state["file"]).write_bytes(b"audio")  # real file: the embed is gated on one existing

        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_metadata",
            lambda path, meta: state["calls"].append(("tags", path, dict(meta))) or True,
        )
        monkeypatch.setattr(
            "services.metadata.tag_file_service.embed_album_art",
            lambda path, data, mime="image/jpeg": state["calls"].append(("art", path, data, mime)) or True,
        )
        monkeypatch.setattr(
            "services.downloads.download_completion_service._fetch_import_art",
            lambda item: (JPEG, "image/jpeg"),
        )
        # Keep the test on the art path: no stored album fields, no MB refresh.
        monkeypatch.setattr(
            "services.downloads.download_completion_service._resolve_album_level_metadata",
            lambda item: {},
        )
        return state

    _ITEM = {
        "id": 7,
        "title": "Song",
        "artist": "Madball",
        "album": "Greatest Hits",
    }

    def test_the_art_is_embedded_with_the_metadata(self, _env):
        ok = completion._apply_stored_metadata(dict(self._ITEM), _env["file"])

        assert ok is True
        art_calls = [c for c in _env["calls"] if c[0] == "art"]
        assert art_calls == [("art", _env["file"], JPEG, "image/jpeg")]

    def test_the_art_is_embedded_AFTER_the_tag_write(self, _env):
        """The writer may rebuild frames — art embedded first would drop."""
        completion._apply_stored_metadata(dict(self._ITEM), _env["file"])

        kinds = [c[0] for c in _env["calls"]]
        assert kinds.index("tags") < kinds.index("art"), kinds

    def test_a_synthetic_path_never_triggers_the_art_lookup(self, _env, monkeypatch):
        """The fetch is gated on a file that can receive it: a fake path must
        not reach the providers (in the shared test DB a missing-table error
        disposes the ENGINE, wiping every table)."""
        fetched: list = []
        monkeypatch.setattr(
            "services.downloads.download_completion_service._fetch_import_art",
            lambda item: fetched.append(item) or (JPEG, "image/jpeg"),
        )

        completion._apply_stored_metadata(dict(self._ITEM), "/music/does-not-exist.flac")

        assert not fetched
        assert not [c for c in _env["calls"] if c[0] == "art"]

    def test_missing_art_never_fails_the_import(self, _env, monkeypatch):
        """CONTROL — art is enrichment, not a reason to refuse an import."""
        monkeypatch.setattr(
            "services.downloads.download_completion_service._fetch_import_art",
            lambda item: (_ for _ in ()).throw(RuntimeError("CAA down")),
        )

        ok = completion._apply_stored_metadata(dict(self._ITEM), _env["file"])

        assert ok is True
        assert not [c for c in _env["calls"] if c[0] == "art"]

    def test_an_embed_failure_is_not_fatal(self, _env, monkeypatch):
        monkeypatch.setattr(
            "services.metadata.tag_file_service.embed_album_art",
            lambda path, data, mime="image/jpeg": False,
        )

        ok = completion._apply_stored_metadata(dict(self._ITEM), _env["file"])

        assert ok is True
