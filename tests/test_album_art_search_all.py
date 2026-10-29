"""Album-art search: Discogs fields, all-source results, and import keying.

REPORTS:
* "when selecting Change Cover Art ... selecting Discogs → Error: HTTP 404: No
  album art found";
* "change it so the search cover art searches all source and shows all results
  returned to select the one that's wanted";
* a library-copied file imported without its album art.

WHY BEHAVIOURAL: the Discogs bug was a WRONG FIELD NAME (``cover_url`` exists
nowhere in the Discogs payload), and the import keying bug is about which
(artist, album) key is read. Neither is visible in a source-text assertion.
"""

from __future__ import annotations

import pytest

from services.enrichment import album_art_service as aas


class _Resp:
    def __init__(self, status_code: int = 200, content: bytes = b"cover-bytes"):
        self.status_code = status_code
        self.content = content


class TestDiscogsImages:
    def test_a_search_result_without_cover_url_still_yields_art(self, monkeypatch):
        """Discogs returns ``thumb``/``cover_image`` — never ``cover_url``."""
        requested: list[str] = []

        class FakeDiscogs:
            def __init__(self, token=None):
                self.token = token

            def search_database(self, params, timeout=10):
                return [{
                    "id": 123,
                    "title": "Album",
                    "thumb": "https://img/thumb.jpg",
                    "cover_image": "",
                }]

            def get_release(self, release_id, timeout=10):
                return {}

        def fake_get(url, **kwargs):
            requested.append(url)
            return _Resp(200, b"discogs-cover")

        monkeypatch.setattr(aas, "DiscogsHttpClient", FakeDiscogs)
        monkeypatch.setattr(aas._art_client, "get", fake_get)

        data = aas.fetch_album_art_from_discogs("Artist", "Album", token="token")

        assert data == b"discogs-cover"
        assert requested == ["https://img/thumb.jpg"]

    def test_release_detail_images_are_preferred(self, monkeypatch):
        requested: list[str] = []

        class FakeDiscogs:
            def __init__(self, token=None):
                pass

            def search_database(self, params, timeout=10):
                return [{
                    "id": 9,
                    "title": "Album",
                    "thumb": "https://img/small.jpg",
                    "cover_image": "",
                }]

            def get_release(self, release_id, timeout=10):
                return {"images": [
                    {"type": "secondary", "uri": "https://img/back.jpg"},
                    {"type": "primary", "uri": "https://img/front.jpg"},
                ]}

        def fake_get(url, **kwargs):
            requested.append(url)
            return _Resp(200, b"full-cover")

        monkeypatch.setattr(aas, "DiscogsHttpClient", FakeDiscogs)
        monkeypatch.setattr(aas._art_client, "get", fake_get)

        data = aas.fetch_album_art_from_discogs("Artist", "Album", token="token")

        assert data == b"full-cover"
        assert requested == ["https://img/front.jpg"], (
            "the PRIMARY image is the front cover and must be taken first"
        )

    def test_the_fake_cover_url_field_is_not_required(self):
        assert aas._discogs_search_image_urls(
            {"thumb": "https://img/t.jpg", "cover_image": ""}
        ) == ["https://img/t.jpg"]
        assert aas._discogs_search_image_urls(
            {"thumb": "https://img/t.jpg", "cover_image": "https://img/c.jpg"}
        ) == ["https://img/c.jpg", "https://img/t.jpg"]


class TestAllSourceSearch:
    def _stub_providers(self, monkeypatch):
        monkeypatch.setattr(
            aas, "_musicbrainz_art_candidates",
            lambda artist, album: [
                aas._art_candidate("MusicBrainz", "https://img/mb1", title=album),
                aas._art_candidate("MusicBrainz", "https://img/mb2", title=album),
            ],
        )
        monkeypatch.setattr(
            aas, "_discogs_art_candidates",
            lambda artist, album: [
                aas._art_candidate("Discogs", "https://img/d1", title=album),
            ],
        )
        monkeypatch.setattr(
            aas, "_itunes_art_candidates",
            lambda artist, album: [
                aas._art_candidate("Apple Music", "https://img/i1", title=album),
            ],
        )
        monkeypatch.setattr(
            aas, "_audiodb_art_candidates",
            lambda artist, album: [
                aas._art_candidate("AudioDB", "https://img/a1", title=album),
            ],
        )
        monkeypatch.setattr(aas, "_probe_art_urls", lambda candidates: candidates)

    def test_all_is_the_default_and_returns_every_source(self, monkeypatch):
        self._stub_providers(monkeypatch)

        result, status = aas.search_album_art_all("Artist", "Album")

        assert status == 200
        urls = [image["url"] for image in result["images"]]
        assert urls == [
            "https://img/mb1", "https://img/mb2", "https://img/d1",
            "https://img/i1", "https://img/a1",
        ]
        assert result["sources_searched"] == [
            "Apple Music", "AudioDB", "Discogs", "MusicBrainz",
        ]

    def test_a_specific_source_narrows_the_search(self, monkeypatch):
        self._stub_providers(monkeypatch)

        result, status = aas.search_album_art_all("Artist", "Album", source="discogs")

        assert status == 200
        assert [image["url"] for image in result["images"]] == ["https://img/d1"]
        assert result["sources_searched"] == ["Discogs"]

    def test_duplicate_urls_are_returned_once(self, monkeypatch):
        monkeypatch.setattr(
            aas, "_musicbrainz_art_candidates",
            lambda artist, album: [aas._art_candidate("MusicBrainz", "https://same")],
        )
        monkeypatch.setattr(
            aas, "_discogs_art_candidates",
            lambda artist, album: [aas._art_candidate("Discogs", "https://same")],
        )
        monkeypatch.setattr(aas, "_itunes_art_candidates", lambda a, b: [])
        monkeypatch.setattr(aas, "_audiodb_art_candidates", lambda a, b: [])
        monkeypatch.setattr(aas, "_probe_art_urls", lambda candidates: candidates)

        result, _status = aas.search_album_art_all("Artist", "Album")

        assert [image["url"] for image in result["images"]] == ["https://same"]

    def test_no_candidates_is_a_404(self, monkeypatch):
        for name in (
            "_musicbrainz_art_candidates", "_discogs_art_candidates",
            "_itunes_art_candidates", "_audiodb_art_candidates",
        ):
            monkeypatch.setattr(aas, name, lambda artist, album: [])

        result, status = aas.search_album_art_all("Artist", "Album")

        assert status == 404
        assert result["images"] == []
        assert result["error"] == "No album art found"

    def test_the_external_entry_point_defaults_to_all(self, monkeypatch):
        monkeypatch.setattr(
            aas, "search_album_art_all",
            lambda artist, album, source="all": ({"success": True, "images": [], "source": source}, 200),
        )

        result, _status = aas.search_album_art_external("Artist", "Album")

        assert result["source"] == "all"


class TestImportArtLooksUnderBothCredits:
    def test_a_copy_queued_under_the_track_artist_finds_the_albums_art(self, monkeypatch):
        from services.downloads import download_completion_service as dcs

        calls: list[str] = []

        def fake_get(artist, album):
            calls.append(artist)
            if artist == "Coldplay":
                return b"coldplay-album-art", "image/jpeg"
            return None, None

        monkeypatch.setattr(
            "services.enrichment.album_art_service.get_or_fetch_album_art",
            fake_get,
        )
        monkeypatch.setattr(
            "services.enrichment.album_art_service.save_album_art_to_db",
            lambda *a, **k: True,
        )

        art = dcs._fetch_import_art({
            "id": 1,
            "album": "2002 - MTV2 Handpicked",
            "album_artist": "Various Artists",  # page key: no art here
            "artist": "Coldplay",               # stored art key
        })

        assert art is not None
        assert art[0] == b"coldplay-album-art"
        assert calls[:2] == ["Various Artists", "Coldplay"]

    def test_the_album_artist_key_is_tried_first(self, monkeypatch):
        from services.downloads import download_completion_service as dcs

        calls: list[str] = []

        def fake_get(artist, album):
            calls.append(artist)
            return b"art", "image/jpeg"

        monkeypatch.setattr(
            "services.enrichment.album_art_service.get_or_fetch_album_art",
            fake_get,
        )

        art = dcs._fetch_import_art({
            "id": 1, "album": "Album",
            "album_artist": "Various Artists", "artist": "Coldplay",
        })

        assert art is not None
        assert calls == ["Various Artists"]
