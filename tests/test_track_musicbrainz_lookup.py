"""``POST /api/track/musicbrainz`` — the track page's recording lookup.

Reported: *"Network error: Server returned HTML instead of JSON (HTTP 405)."*

``/api/track/musicbrainz`` is a STATIC path. Without an explicit POST rule it
was matched by the dynamic ``/<track_id>`` route (GET only), so Quart answered
with its HTML "405 Method Not Allowed" page and the JSON api client surfaced
exactly that message. The old monolithic app had the route; the port lost it.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACK_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "track.js"


class _StubMB:
    def __init__(self) -> None:
        self.search_queries: list[str] = []
        self.get_calls: list[tuple[str, str]] = []

    def search_recordings(self, query, limit=10, inc=""):
        self.search_queries.append(query)
        return [
            {
                "id": "rec-1",
                "title": "The One",
                "artist-credit": [
                    {"name": "Bushido", "artist": {"id": "a1", "name": "Bushido"}}
                ],
                "releases": [
                    {"id": "rel-1", "title": "Album", "date": "2020-01-01"}
                ],
                "relations": [],
            }
        ]

    def get_recording(self, mbid, inc=""):
        self.get_calls.append((str(mbid), str(inc)))
        return {
            "id": str(mbid),
            "title": "The One",
            "artist-credit": [
                {"name": "Bushido", "artist": {"id": "a1", "name": "Bushido"}}
            ],
            "first-release-date": "2020-01-01",
            "relations": [],
        }


def _install_stub(monkeypatch) -> _StubMB:
    import routes.track_routes as tr

    stub = _StubMB()
    monkeypatch.setattr(tr, "get_shared_mb_client", lambda: stub)
    return stub


class TestTheStaticRouteExists:
    async def test_it_answers_json_instead_of_the_html_405(self, client, monkeypatch):
        """⭐ THE REPORTED CASE: a title/artist search must not hit /<track_id>."""
        stub = _install_stub(monkeypatch)

        resp = await client.post(
            "/api/track/musicbrainz",
            json={"title": "The One", "artist": "Bushido"},
        )

        assert resp.status_code == 200
        assert resp.content_type.startswith("application/json")
        body = await resp.get_json()
        assert body["results"], body
        row = body["results"][0]
        assert row["mbid"] == "rec-1"
        assert row["title"] == "The One"
        assert row["artist"] == "Bushido"
        assert row["year"] == "2020"
        assert 0.0 <= float(row["confidence"]) <= 1.0
        assert stub.search_queries

    async def test_a_pasted_recording_id_is_resolved_directly(
        self, client, monkeypatch
    ):
        stub = _install_stub(monkeypatch)

        resp = await client.post(
            "/api/track/musicbrainz", json={"mbid": "rec-pasted"}
        )

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["results"][0]["mbid"] == "rec-pasted"
        assert float(body["results"][0]["confidence"]) == 1.0
        assert stub.get_calls and stub.get_calls[0][0] == "rec-pasted"
        # No text search was needed.
        assert stub.search_queries == []

    async def test_an_empty_request_is_a_json_400_not_html(self, client):
        resp = await client.post("/api/track/musicbrainz", json={})

        assert resp.status_code == 400
        assert resp.content_type.startswith("application/json")
        body = await resp.get_json()
        assert "error" in body


class TestTheModalSearchesTheCurrentTrackThenAllowsAdjustment:
    """Reported follow-up: search with the track's current artist/title first,
    and let the user correct them when nothing matches."""

    def _source(self) -> str:
        return TRACK_JS.read_text(encoding="utf-8")

    def test_the_modal_has_editable_artist_and_title_prefilled(self):
        src = self._source()
        assert "id=\"trackMBLookupArtist\"" in src
        assert "id=\"trackMBLookupTitle\"" in src
        # Pre-filled from the track form's own fields.
        assert "document.getElementById('artist')" in src
        assert "document.getElementById('title')" in src

    def test_the_modal_can_search_again(self):
        src = self._source()
        assert 'data-action="track-mb-lookup-search"' in src
        assert "'track-mb-lookup-search': () => runTrackMusicBrainzSearch()" in src
        # Enter in either field re-runs it.
        assert "event.key === 'Enter'" in src

    def test_no_matches_keeps_the_fields_available(self):
        src = self._source()
        assert "Adjust the artist or title above and search again" in src

    def test_the_current_recording_id_is_only_a_fallback(self):
        """Searching always uses the text fields; the stored MBID is sent only
        when there is no text at all (e.g. an empty form)."""
        src = self._source()
        assert "const payload = { title, artist };" in src
        assert "if (fallbackMbid) payload.mbid = fallbackMbid;" in src
