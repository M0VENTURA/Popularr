"""The album page can download a popularity report as CSV.

Requested: the artist page's "Download Popularity Report" belongs in the
**album** page's Actions menu too.

Design notes
------------
* **One builder, two scopes** — ``services.popularity.popularity_report_service``
  builds both files. The album report is the artist report restricted to one
  album: same columns, same ordering chain, same BOM, so an album's rows sit in
  the same relative order in both files. Two copies of the SQL would eventually
  disagree about a column or a sort key, which is worse than no report at all.
* **Artist AND album** — album titles are NOT unique ("Greatest Hits" exists a
  thousand times over), so an album-only filter would merge every artist's hits
  album into one file.
* **Query parameters, not a path segment** — the album name is arbitrary text
  and already travels as ``?album=`` everywhere else in
  ``routes/album_routes.py``.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

REPORT_URL = "/api/album/popularity-report"
ARTIST_REPORT_URL = "/api/artist/popularity-report"
REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_SITE_ALBUM_PAGE = (
    REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"
)
TEST_SITE_ARTIST_PAGE = (
    REPO_ROOT / "test_site" / "templates" / "Pages" / "artist_detail_v2.html"
)

#: What a reader of the report needs: both platforms' raw counts, both source
#: scores, the combined score and the rating.
EXPECTED_COLUMNS = [
    "rank", "title", "artist", "album",
    "final_score", "popularity", "stars",
    "lastfm_listeners", "lastfm_playcount", "lastfm_score",
    "listenbrainz_listens", "listenbrainz_users", "listenbrainz_score",
    "is_single", "single_confidence",
]


@pytest.fixture(autouse=True)
def _empty_tracks_table(db_session):
    """Start each test with no rows.

    ``_recreate_test_schema`` does not wipe ``tracks`` between tests here, so a
    fixed ``id`` collides with an earlier test's row and ``ON CONFLICT DO
    NOTHING`` silently skips the insert — the report then contains the PREVIOUS
    test's tracks and not this one's.
    """
    from sqlalchemy import text

    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()
    yield


def _insert(
    db_session,
    *,
    track_id: str,
    title: str,
    artist: str = "Test Artist",
    album_artist: str | None = None,
    album: str = "Test Album",
    final_score: float | None = None,
    raw_score: float | None = None,
    stars: int | None = None,
    lastfm_listeners: int | None = None,
    listenbrainz_listens: int | None = None,
) -> None:
    from sqlalchemy import text

    db_session.execute(
        text(
            """
            INSERT INTO tracks (id, artist, album_artist, album, title,
                                disc_number, track_number, file_path,
                                final_score, raw_score, stars,
                                lastfm_listeners, listenbrainz_listens)
            VALUES (:id, :artist, :album_artist, :album, :title,
                    1, :track_number, :file_path,
                    :final_score, :raw_score, :stars,
                    :lastfm_listeners, :listenbrainz_listens)
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "id": track_id,
            "artist": artist,
            "album_artist": album_artist,
            "album": album,
            "title": title,
            "track_number": int(track_id[-1]) if track_id[-1].isdigit() else 1,
            "file_path": f"/music/{track_id}.mp3",
            "final_score": final_score,
            "raw_score": raw_score,
            "stars": stars,
            "lastfm_listeners": lastfm_listeners,
            "listenbrainz_listens": listenbrainz_listens,
        },
    )
    db_session.commit()


def _parse(body: bytes) -> list[list[str]]:
    text = body.decode("utf-8-sig")  # strips the BOM
    return list(csv.reader(io.StringIO(text)))


async def _report(client, artist: str = "Test Artist", album: str = "Test Album"):
    from urllib.parse import quote

    response = await client.get(
        f"{REPORT_URL}?artist={quote(artist)}&album={quote(album)}"
    )
    return response, _parse(await response.get_data())


async def _report_rows(client, artist="Test Artist", album="Test Album"):
    _response, parsed = await _report(client, artist, album)
    return parsed


class TestTheReportEndpoint:
    async def test_it_is_returned_as_a_downloadable_csv(self, client, db_session):
        _insert(db_session, track_id="t1", title="Popular", final_score=91.0)

        response, _parsed = await _report(client)

        assert response.status_code == 200
        assert response.mimetype == "text/csv", response.headers.get("Content-Type")
        disposition = response.headers.get("Content-Disposition", "")
        assert disposition.startswith("attachment;")
        assert "Test Artist - Test Album - popularity report.csv" in disposition, (
            "the album must be in the filename — a folder full of downloads "
            "needs to say which album each file is"
        )

    async def test_the_header_lists_both_platforms_and_the_rating(
        self, client, db_session
    ):
        _insert(db_session, track_id="t1", title="Popular", final_score=91.0)

        parsed = await _report_rows(client)

        for column in EXPECTED_COLUMNS:
            assert column in parsed[0], f"{column} is missing from the report"

    async def test_rows_come_out_most_popular_first(self, client, db_session):
        _insert(db_session, track_id="t1", title="Middle", final_score=44.0,
                raw_score=44.0)
        _insert(db_session, track_id="t2", title="Bottom", final_score=12.5,
                raw_score=12.5)
        _insert(db_session, track_id="t3", title="Top", final_score=91.0,
                raw_score=91.0)

        rows = await _report_rows(client)

        titles = [row[1] for row in rows[1:]]
        assert titles == ["Top", "Middle", "Bottom"], (
            "the report must be ordered most popular first — that is the "
            "whole point of it"
        )
        assert [row[0] for row in rows[1:]] == ["1", "2", "3"], (
            "rank must follow the popularity order, not the insertion order"
        )

    async def test_only_this_albums_tracks_appear(self, client, db_session):
        _insert(db_session, track_id="t1", title="On This Album", final_score=50.0)
        _insert(db_session, track_id="t2", title="Another Album", album="Other",
                final_score=99.0)

        rows = await _report_rows(client)

        assert [row[1] for row in rows[1:]] == ["On This Album"], (
            "the report is per album — a 99.0 from another album of the same "
            "artist must not appear"
        )

    async def test_another_artists_same_titled_album_is_excluded(
        self, client, db_session
    ):
        """Album titles are not unique — "Greatest Hits" exists a thousand
        times over, so the artist must be part of the scope."""
        _insert(db_session, track_id="t1", title="Mine", album="Greatest Hits",
                final_score=50.0)
        _insert(db_session, track_id="t2", title="Theirs", artist="Other Artist",
                album="Greatest Hits", final_score=99.0)

        rows = await _report_rows(client, album="Greatest Hits")

        assert [row[1] for row in rows[1:]] == ["Mine"]

    async def test_a_compilation_track_is_filed_under_its_album_artist(
        self, client, db_session
    ):
        """CONTROL — the artist key everywhere else is
        ``COALESCE(album_artist, artist)``, so the album report uses it too."""
        _insert(db_session, track_id="t1", title="Compilation Cut",
                artist="Somebody Else", album_artist="Test Artist",
                final_score=40.0)

        rows = await _report_rows(client)

        assert [row[1] for row in rows[1:]] == ["Compilation Cut"]

    async def test_the_match_ignores_case(self, client, db_session):
        _insert(db_session, track_id="t1", title="Popular", final_score=50.0)

        rows = await _report_rows(client, artist="test artist", album="test album")

        assert [row[1] for row in rows[1:]] == ["Popular"]

    async def test_the_listens_and_scores_reach_the_file(self, client, db_session):
        _insert(db_session, track_id="t1", title="Rich", final_score=70.0,
                stars=5, lastfm_listeners=477_200, listenbrainz_listens=290)

        _response, parsed = await _report(client)
        record = dict(zip(parsed[0], parsed[1]))

        assert record["lastfm_listeners"] == "477200"
        assert record["listenbrainz_listens"] == "290"
        assert record["stars"] == "5"
        assert record["final_score"] == "70.0"

    async def test_a_missing_artist_is_rejected(self, client):
        response = await client.get(f"{REPORT_URL}?album=Test%20Album")

        assert response.status_code == 400, (
            "an empty artist would silently download every artist's album "
            "with this title"
        )

    async def test_a_missing_album_is_rejected(self, client):
        response = await client.get(f"{REPORT_URL}?artist=Test%20Artist")

        assert response.status_code == 400, (
            "an empty album would silently download the artist's whole "
            "catalogue — that is the artist report, not this one"
        )

    async def test_the_endpoint_requires_a_session(self, unauthed_client, db_session):
        """CONTROL — the report is a download URL, so it must not be public."""
        _insert(db_session, track_id="t1", title="Mine", final_score=50.0)

        response = await unauthed_client.get(
            f"{REPORT_URL}?artist=Test%20Artist&album=Test%20Album"
        )

        assert response.status_code != 200, (
            "a download endpoint left off the auth gate would expose the whole "
            "catalogue to anyone with the URL"
        )


class TestTheTwoScopesShareOneBuilder:
    async def test_the_album_file_is_the_artist_file_filtered(
        self, client, db_session
    ):
        """Same ordering rule in both files: the album's rows keep their
        relative order, so the two reports can be compared row for row."""
        _insert(db_session, track_id="t1", title="Alpha", final_score=90.0,
                raw_score=90.0)
        _insert(db_session, track_id="t2", title="Beta", album="Other Album",
                final_score=80.0, raw_score=80.0)
        _insert(db_session, track_id="t3", title="Gamma", final_score=70.0,
                raw_score=70.0)

        album_rows = await _report_rows(client)
        artist_response = await client.get(
            f"{ARTIST_REPORT_URL}?artist=Test%20Artist"
        )
        artist_rows = _parse(await artist_response.get_data())

        assert [row[1] for row in album_rows[1:]] == ["Alpha", "Gamma"]
        artist_order = [row[1] for row in artist_rows[1:]]
        assert artist_order.index("Alpha") < artist_order.index("Gamma")

    def test_the_artist_route_no_longer_carries_its_own_copy(self):
        source = (REPO_ROOT / "routes" / "artist_routes.py").read_text(encoding="utf-8")

        assert "_POPULARITY_REPORT_SQL" not in source, (
            "a second copy of the SQL drifts from the album file's columns"
        )
        assert "_build_popularity_report" not in source
        assert "popularity_report_service" in source, (
            "the artist route must delegate to the shared builder"
        )


class TestTheTestSiteAlbumPageOffersIt:
    def test_the_actions_menu_has_the_download(self):
        source = TEST_SITE_ALBUM_PAGE.read_text(encoding="utf-8")

        assert "Download Popularity Report" in source, (
            "the requested action is missing from the test-site album page"
        )
        assert "album_routes.api_album_popularity_report" in source, (
            "the action must point at the report endpoint, not a placeholder"
        )

    def test_the_action_sits_in_the_actions_menu(self):
        """Where a user would look for it — with the other album actions."""
        source = TEST_SITE_ALBUM_PAGE.read_text(encoding="utf-8")

        position = source.index("Download Popularity Report")
        menu_start = source.rindex('<ul class="dropdown-menu', 0, position)
        menu_end = source.index("</ul>", position)
        menu = source[menu_start:menu_end]

        assert "Change Album Art" in menu, (
            "the report must live with the other actions, not somewhere new"
        )

    def test_the_artist_page_keeps_its_own(self):
        """CONTROL — the new action must be a SECOND entry point, not a move."""
        source = TEST_SITE_ARTIST_PAGE.read_text(encoding="utf-8")

        assert "Download Popularity Report" in source
        assert "artist.api_artist_popularity_report" in source

    def test_the_named_endpoint_actually_exists(self, app):
        """A typo'd ``url_for`` target raises BuildError and 500s the page.

        Asserted against the app's own URL map rather than the source: the
        template's string is only correct if it matches a registered rule.
        """
        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}

        assert "album_routes.api_album_popularity_report" in endpoints, (
            "the album page would render a BuildError instead of a download"
        )
        assert any(
            rule.rule == "/api/album/popularity-report" and "GET" in rule.methods
            for rule in app.url_map.iter_rules()
        )
