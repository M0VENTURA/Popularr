"""Edit External IDs: it saves, it clears, and Discogs matches the right artist.

Reported
--------
> The edit external IDs on the artist page doesn't save when updating the
> discogs ID. It should also have a button to search artists with the same
> name to adjust the match if it's incorrectly matched on discogs. Discogs
> seems to match the first artist, not the one with the same albums.

Three defects, one report:

1. **The Save button was dead on the rebuilt page.** The modal's button
   carries no inline ``onclick`` — it is bound in JS — and the test_site
   bundle never bound it (only the LIVE ``artist-page.js`` did), so clicking
   Save sent nothing at all.
2. **A wrong id could not be removed.** ``update_ids`` wrote only TRUTHY
   values, so clearing the Discogs box saved nothing and the bad id survived
   the reload — "it doesn't save".
3. **Discogs matched ``results[0]``.** The search returns every artist with
   that name in Discogs' own relevance order, so the resolution picked
   whichever Discogs listed first instead of the artist whose releases match
   the library's albums.

The fix also adds the requested picker: a "Find" button in the modal that
lists same-named Discogs artists scored by how many of THIS artist's albums
appear in each candidate's release list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_SITE_PARTIAL = REPO_ROOT / "test_site" / "templates" / "components" / "modals" / "_artist_modals.html"
LIVE_PARTIAL = REPO_ROOT / "templates" / "components" / "modals" / "_artist_modals.html"
TEST_SITE_ARTIST_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist.js"
LIVE_ARTIST_JS = REPO_ROOT / "static" / "js" / "artist-page.js"


# ===========================================================================
# 1. Discogs resolution: name first, then "same albums"
# ===========================================================================


class _FakeDiscogsHttp:
    """Stands in for the client: the two network calls, nothing else.

    ⚠️ Must NOT construct a real ``DiscogsHttpClient`` — the tests patch that
    name, so calling it here would recurse through the patched factory.
    """

    def __init__(self, candidates, releases_by_id=None, record=None):
        self._candidates = candidates
        self._releases = releases_by_id or {}
        self._record = record if record is not None else []

    def search_artists(self, name, limit=10, timeout=10.0):
        return list(self._candidates)

    def matched_artist_albums(self, artist_id, albums, timeout=10.0):
        self._record.append(("score", str(artist_id)))
        from api_clients.discogs_http import _normalize_release_title

        wanted = {_normalize_release_title(a) for a in (albums or []) if a}
        matched = [
            title for title in self._releases.get(str(artist_id), [])
            if _normalize_release_title(title) in wanted
        ]
        return matched


def _client(candidates, releases_by_id=None, record=None):
    from api_clients.discogs_http import DiscogsHttpClient

    fake = _FakeDiscogsHttp(candidates, releases_by_id, record)
    client = DiscogsHttpClient(token="test-token")
    client.search_artists = fake.search_artists
    client.matched_artist_albums = fake.matched_artist_albums
    return client


CANDIDATES = [
    {"id": "1", "name": "Electric Callboy Tribute", "resource_url": "", "thumb": ""},
    {"id": "2", "name": "Electric Callboy", "resource_url": "", "thumb": ""},
    {"id": "3", "name": "Eskimo Callboy", "resource_url": "", "thumb": ""},
]


class TestTheResolutionPrefersTheRightArtist:
    def test_a_single_candidate_is_used_without_extra_requests(self):
        client = _client([{"id": "42", "name": "Ghostkid", "resource_url": "", "thumb": ""}])

        assert client.get_artist_id("Ghostkid") == "42"

    def test_the_exact_name_beats_the_first_hit(self):
        """CONTROL for the reported cause: Discogs ranked a tribute act first."""
        client = _client(CANDIDATES)

        assert client.get_artist_id("Electric Callboy") == "2", (
            "the first search hit was a DIFFERENT artist — the exact name wins"
        )

    def test_no_exact_name_keeps_the_first_hit(self):
        """CONTROL — unchanged behaviour when nothing matches better."""
        client = _client([
            {"id": "1", "name": "Some Band", "resource_url": "", "thumb": ""},
            {"id": "2", "name": "Other Band", "resource_url": "", "thumb": ""},
        ])

        assert client.get_artist_id("Unknown Band") == "1"

    def test_the_album_overlap_decides_between_same_named_artists(self):
        """THE REPORTED CASE: two artists share the name, and only one has the
        library's albums. Discogs listed the wrong one first."""
        duplicates = [
            {"id": "9", "name": "Electric Callboy", "resource_url": "", "thumb": ""},
            {"id": "7", "name": "Electric Callboy", "resource_url": "", "thumb": ""},
        ]
        client = _client(
            duplicates,
            releases_by_id={
                "9": ["Some Other Album", "Another One"],
                "7": ["MMXX", "Tekkno"],
            },
        )

        resolved = client.get_artist_id("Electric Callboy", albums=["MMXX", "Tekkno"])

        assert resolved == "7", (
            "the artist whose release list holds the library's albums must win "
            "over the first hit"
        )

    def test_a_candidate_with_no_matching_album_does_not_win(self):
        """Scoring must not INVENT a winner: no overlap anywhere falls back to
        the first candidate rather than to whichever was scored last."""
        duplicates = [
            {"id": "9", "name": "Electric Callboy", "resource_url": "", "thumb": ""},
            {"id": "7", "name": "Electric Callboy", "resource_url": "", "thumb": ""},
        ]
        client = _client(
            duplicates,
            releases_by_id={"9": ["Nothing Alike"], "7": ["Also Nothing"]},
        )

        assert client.get_artist_id("Electric Callboy", albums=["MMXX"]) == "9"

    def test_the_number_of_scored_candidates_is_capped(self):
        """Each scored candidate costs a Discogs request at 1 req/sec, so a
        shared name must not turn a lookup into a crawl."""
        from api_clients.discogs_http import _MAX_ALBUM_SCORED_CANDIDATES

        many = [
            {"id": str(i), "name": "Shared Name", "resource_url": "", "thumb": ""}
            for i in range(10)
        ]
        record: list[tuple] = []
        client = _client(many, releases_by_id={}, record=record)

        client.get_artist_id("Shared Name", albums=["Album"])

        assert len(record) == _MAX_ALBUM_SCORED_CANDIDATES, (
            f"expected at most {_MAX_ALBUM_SCORED_CANDIDATES} release fetches, "
            f"got {len(record)}"
        )

    def test_an_empty_name_resolves_to_nothing(self):
        assert _client(CANDIDATES).get_artist_id("") is None


class TestAlbumTitlesCompareThroughDiscogsSuffixes:
    def test_discogs_pressing_suffixes_are_ignored(self):
        """Discogs release titles carry format qualifiers the library does
        not ("Album, LP, Reissue"), so an exact string compare would find no
        album and every candidate would score zero."""
        from api_clients.discogs_http import _normalize_release_title

        assert _normalize_release_title("MMXX - Hypa Hypa Edition") == _normalize_release_title(
            "MMXX - Hypa Hypa Edition"
        )
        assert _normalize_release_title("Tekkno (CD, Album)") == "tekkno"
        assert _normalize_release_title("Tekkno [Reissue]") == "tekkno"

    def test_no_albums_means_no_scoring(self):
        record: list[tuple] = []
        duplicates = [
            {"id": "9", "name": "Same", "resource_url": "", "thumb": ""},
            {"id": "7", "name": "Same", "resource_url": "", "thumb": ""},
        ]
        client = _client(duplicates, releases_by_id={"9": ["A"], "7": ["A"]}, record=record)

        client.get_artist_id("Same", albums=None)

        assert record == [], "no albums supplied means no release fetches"

    def test_matched_albums_returns_the_library_titles(self):
        from api_clients.discogs_http import DiscogsHttpClient

        client = DiscogsHttpClient(token="t")
        client.get_artist_releases = lambda artist_id, per_page=100, timeout=10.0: [
            {"title": "Tekkno (CD, Album)"},
            {"title": "Something Else"},
            {"title": "MMXX"},
        ]

        matched = client.matched_artist_albums("7", ["Tekkno", "MMXX"])

        assert sorted(matched) == ["MMXX", "Tekkno (CD, Album)"]


# ===========================================================================
# 2. Saving: present keys write, empty clears, nothing matched is reported
# ===========================================================================


class TestUpdateIds:
    def _patch_session(self, monkeypatch, rowcount=3):
        import services.metadata.artist_metadata_service as ams

        executed: list[tuple] = []

        class _Result:
            def __init__(self):
                self.rowcount = rowcount

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, statement, params=None):
                executed.append((str(statement), dict(params or {})))
                return _Result()

        monkeypatch.setattr(ams, "db_session", lambda *a, **k: _Session())
        return executed

    def test_a_present_empty_discogs_id_clears_the_column(self, monkeypatch):
        """THE REPORTED CASE: a wrong id must be removable."""
        import services.metadata.artist_metadata_service as ams

        executed = self._patch_session(monkeypatch)

        data, code = ams.update_ids({
            "artist": "Electric Callboy",
            "musicbrainz_artist_id": "mb-1",
            "discogs_artist_id": "",
        })

        assert code == 200 and data["success"]
        statement, params = executed[0]
        assert "discogs_artist_id = :v1" in statement, "the cleared field must be written"
        assert params["v1"] is None, "an empty value becomes NULL, not an empty string"

    def test_an_absent_key_is_left_alone(self, monkeypatch):
        """CONTROL — a caller updating one field must not wipe the other."""
        import services.metadata.artist_metadata_service as ams

        executed = self._patch_session(monkeypatch)

        ams.update_ids({"artist": "A", "discogs_artist_id": "55"})

        statement, params = executed[0]
        assert "musicbrainz_artistid" not in statement
        assert "discogs_artist_id = :v1" in statement and params["v1"] == "55"

    def test_both_keys_still_write_together(self, monkeypatch):
        import services.metadata.artist_metadata_service as ams

        executed = self._patch_session(monkeypatch)

        ams.update_ids({
            "artist": "A", "musicbrainz_artist_id": "mb-1", "discogs_artist_id": "77",
        })

        statement, params = executed[0]
        assert "musicbrainz_artistid = :v0" in statement and "discogs_artist_id = :v1" in statement
        assert params["v0"] == "mb-1" and params["v1"] == "77"

    def test_no_keys_at_all_is_still_rejected(self, monkeypatch):
        import services.metadata.artist_metadata_service as ams

        self._patch_session(monkeypatch)

        _data, code = ams.update_ids({"artist": "A"})

        assert code == 400

    def test_a_missing_artist_is_rejected(self, monkeypatch):
        import services.metadata.artist_metadata_service as ams

        self._patch_session(monkeypatch)

        _data, code = ams.update_ids({"discogs_artist_id": "5"})

        assert code == 400

    def test_no_matching_tracks_is_reported_as_a_failure(self, monkeypatch):
        """The old reply said success while nothing was written — the second
        half of "it doesn't save": the page reloaded unchanged."""
        import services.metadata.artist_metadata_service as ams

        self._patch_session(monkeypatch, rowcount=0)

        data, code = ams.update_ids({"artist": "Ghost", "discogs_artist_id": "5"})

        assert code == 404
        assert data["success"] is False
        assert "No tracks" in data["error"]


# ===========================================================================
# 3. The Discogs picker service + endpoint
# ===========================================================================


class TestSearchDiscogsArtists:
    def _patch(self, monkeypatch, candidates, releases_by_id=None, token="test-token", enabled=True):
        import services.metadata.artist_metadata_service as ams

        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"api_integrations": {"discogs": {"token": token, "enabled": enabled}}},
        )
        monkeypatch.setattr(
            "api_clients.discogs_http.DiscogsHttpClient",
            lambda **kw: _FakeDiscogsHttp(candidates, releases_by_id),
        )

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, statement, params=None):
                class _Rows:
                    def fetchall(self):
                        return [("MMXX",), ("Tekkno",)]
                return _Rows()

        monkeypatch.setattr(ams, "db_session", lambda *a, **k: _Session())
        return ams

    def test_candidates_are_sorted_by_album_overlap(self, monkeypatch):
        ams = self._patch(
            monkeypatch,
            [
                {"id": "9", "name": "Electric Callboy", "resource_url": "", "thumb": ""},
                {"id": "7", "name": "Electric Callboy", "resource_url": "", "thumb": ""},
            ],
            releases_by_id={"9": ["Nothing"], "7": ["MMXX", "Tekkno"]},
        )

        data, code = ams.search_discogs_artists("Electric Callboy")

        assert code == 200 and data["success"]
        assert [c["id"] for c in data["candidates"]] == ["7", "9"], (
            "the artist with the library's albums sorts first"
        )
        assert data["candidates"][0]["match_count"] == 2
        assert data["albums_checked"] == ["MMXX", "Tekkno"]

    def test_each_candidate_carries_what_the_picker_shows(self, monkeypatch):
        ams = self._patch(
            monkeypatch,
            [{"id": "7", "name": "Electric Callboy", "resource_url": "", "thumb": ""}],
            releases_by_id={"7": ["MMXX"]},
        )

        data, _code = ams.search_discogs_artists("Electric Callboy")
        candidate = data["candidates"][0]

        assert candidate["id"] == "7"
        assert candidate["name"] == "Electric Callboy"
        assert candidate["discogs_url"] == "https://www.discogs.com/artist/7"
        assert candidate["matched_albums"] == ["MMXX"]

    def test_an_unconfigured_integration_is_explained(self, monkeypatch):
        ams = self._patch(monkeypatch, [], token="")

        data, code = ams.search_discogs_artists("Electric Callboy")

        assert code == 400
        assert "not configured" in data["error"].lower()

    def test_a_missing_artist_is_rejected(self, monkeypatch):
        ams = self._patch(monkeypatch, [])

        _data, code = ams.search_discogs_artists("")

        assert code == 400


class TestTheSearchEndpoint:
    async def test_it_returns_the_candidates(self, client, monkeypatch):
        import services.metadata.artist_metadata_service as ams

        monkeypatch.setattr(
            ams, "search_discogs_artists",
            lambda artist: ({"success": True, "artist": artist, "candidates": [], "albums_checked": []}, 200),
        )

        resp = await client.get("/api/artist/discogs-search?artist=Electric%20Callboy")

        assert resp.status_code == 200
        payload = await resp.get_json()
        assert payload["success"] is True

    async def test_a_missing_artist_is_400(self, client):
        resp = await client.get("/api/artist/discogs-search")
        assert resp.status_code == 400

    async def test_it_requires_a_session(self, unauthed_client):
        """CONTROL — the endpoint reaches a token-authenticated API."""
        resp = await unauthed_client.get("/api/artist/discogs-search?artist=A")
        assert resp.status_code != 200

    def test_the_endpoint_is_registered(self, app):
        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
        assert "artist.api_artist_discogs_search" in endpoints


# ===========================================================================
# 4. The modal: the button is bound, and the picker is wired
# ===========================================================================


class TestTheSaveButtonIsWired:
    def test_the_test_site_binds_the_button(self):
        """⚠️ The reported bug: the button has no inline onclick, and nothing
        in the test_site bundle bound it — clicking Save sent nothing."""
        source = TEST_SITE_ARTIST_JS.read_text(encoding="utf-8")

        assert "editArtistIdsSaveBtn" in source, (
            "the rebuilt artist page must bind the modal's Save button"
        )
        assert "addEventListener('click', saveArtistIds)" in source

    def test_the_live_page_still_binds_it(self):
        """CONTROL — the live tree was the one place this worked."""
        source = LIVE_ARTIST_JS.read_text(encoding="utf-8")

        assert "editArtistIdsSaveBtn" in source

    def test_the_modal_inputs_are_seeded_from_the_page(self):
        """An unseeded form shows blank fields over a saved artist, so saving
        it would write NULL over ids that were already set."""
        source = TEST_SITE_ARTIST_JS.read_text(encoding="utf-8")

        assert "musicbrainzArtistId" in source and "discogsArtistId" in source
        assert "seed(mbShown)" in source and "seed(dcShown)" in source

    def test_a_failed_save_is_reported(self):
        """The old handler reloaded only on success and swallowed errors, so a
        rejected save looked like a dead button."""
        source = TEST_SITE_ARTIST_JS.read_text(encoding="utf-8")

        assert "setArtistIdsStatus(" in source
        assert "Save failed" in source


class TestThePickerIsWired:
    def test_the_test_site_modal_has_the_search_control(self):
        source = TEST_SITE_PARTIAL.read_text(encoding="utf-8")

        assert 'id="discogsArtistSearchBtn"' in source
        assert "searchDiscogsArtists()" in source
        assert 'id="discogsArtistSearchResults"' in source

    def test_the_live_modal_exists_and_has_the_search_control(self):
        """The live partial had NO ids modal at all, so its Edit button opened
        nothing (`showModal` found no element)."""
        source = LIVE_PARTIAL.read_text(encoding="utf-8")

        assert 'id="editArtistIdsModal"' in source
        assert 'id="editDiscogsArtistId"' in source
        assert 'id="editArtistIdsSaveBtn"' in source
        assert "searchDiscogsArtists()" in source

    def test_the_search_handler_is_defined_and_exported(self):
        for path in (TEST_SITE_ARTIST_JS, LIVE_ARTIST_JS):
            source = path.read_text(encoding="utf-8")
            assert "function searchDiscogsArtists()" in source, path.name
            assert "global.searchDiscogsArtists = searchDiscogsArtists" in source or (
                "function searchDiscogsArtists()" in source
            ), "inline onclick resolves by GLOBAL name"

    def test_the_live_handler_is_exported_globally(self):
        source = LIVE_ARTIST_JS.read_text(encoding="utf-8")
        assert "global.searchDiscogsArtists = searchDiscogsArtists" in source

    def test_choosing_a_candidate_fills_the_id_field(self):
        for path in (TEST_SITE_ARTIST_JS, LIVE_ARTIST_JS):
            source = path.read_text(encoding="utf-8")
            assert "useDiscogsArtist" in source, path.name
            assert 'data-action="use-discogs-artist"' in source, (
                f"{path.name}: the candidate rows need a delegated action — an "
                "artist name with a quote would break an inline onclick"
            )

    def test_the_album_match_is_shown(self):
        """The whole point of the picker: the user needs to SEE which candidate
        has their albums."""
        for path in (TEST_SITE_ARTIST_JS, LIVE_ARTIST_JS):
            source = path.read_text(encoding="utf-8")
            assert "matched_albums" in source, path.name
            assert "album" in source and "match" in source
