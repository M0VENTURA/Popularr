"""The artist page can download a popularity report as CSV.

Requested: *"add a button to the artist page on test_site that downloads a
report of all tracks for the artist. It will list the tracks along with the
total listens on last.fm and musicbrainz and all other relevant popularity
data ordered from most popular to least popular."*

Design notes
------------
* **CSV**, not JSON: the point of the report is to sort and filter it in a
  spreadsheet, which is exactly what a screen cannot do.
* **Ordering matches the page**: the artist page's own "top tracks" ranks by
  ``final_score`` with ``popularity``/``stars`` as fallbacks, so the report
  uses the same chain (``COALESCE(final_score, popularity, stars, 0) DESC``)
  — a report that disagreed with the page it hangs off would be worse than
  no report at all.
* **BOM**: track titles carry accents; without ``\\ufeff`` Excel reads the
  file in the local codepage and mangles them.
* **Query parameter, not a path segment**: every other artist API in
  ``routes/artist_routes.py`` takes ``?artist=``, and a ``<path:>`` converter
  would swallow the trailing static segment.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

REPORT_URL = "/api/artist/popularity-report"
REPO_ROOT = Path(__file__).resolve().parent.parent
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
    test's tracks and not this one's. Deleting first makes each test's rows the
    only rows.
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


async def _report_rows(client, artist: str = "Test Artist") -> list[list[str]]:
    """Fetch the report and parse it. Quart's ``response.data`` is awaitable."""
    from urllib.parse import quote

    response = await client.get(f"{REPORT_URL}?artist={quote(artist)}")
    return _parse(await response.get_data())


async def _report(client, artist: str = "Test Artist"):
    from urllib.parse import quote

    response = await client.get(f"{REPORT_URL}?artist={quote(artist)}")
    return response, _parse(await response.get_data())


class TestTheReportEndpoint:
    async def test_it_is_returned_as_a_downloadable_csv(self, client, db_session):
        _insert(db_session, track_id="t1", title="Popular", final_score=91.0)

        response = await client.get(f"{REPORT_URL}?artist=Test%20Artist")

        assert response.status_code == 200
        assert response.mimetype == "text/csv", response.headers.get("Content-Type")
        disposition = response.headers.get("Content-Disposition", "")
        assert disposition.startswith("attachment;")
        assert "Test Artist - popularity report.csv" in disposition

    async def test_the_header_lists_both_platforms_and_the_rating(
        self, client, db_session
    ):
        _insert(db_session, track_id="t1", title="Popular", final_score=91.0)

        _response, parsed = await _report(client)
        header = parsed[0]

        for column in EXPECTED_COLUMNS:
            assert column in header, f"{column} is missing from the report"

    async def test_rows_come_out_most_popular_first(self, client, db_session):
        _insert(db_session, track_id="t1", title="Middle", final_score=44.0)
        _insert(db_session, track_id="t2", title="Bottom", final_score=12.5)
        _insert(db_session, track_id="t3", title="Top", final_score=91.0)

        rows = await _report_rows(client)

        titles = [row[1] for row in rows[1:]]
        assert titles == ["Top", "Middle", "Bottom"], (
            "the report must be ordered most popular first — that is the "
            "whole point of it"
        )
        assert [row[0] for row in rows[1:]] == ["1", "2", "3"], (
            "rank must follow the popularity order, not the insertion order"
        )

    async def test_a_missing_final_score_sorts_last(self, client, db_session):
        """An unscanned track is still in the report — at the bottom."""
        _insert(db_session, track_id="t1", title="Scored", final_score=10.0)
        _insert(db_session, track_id="t2", title="Unscored", final_score=None)

        rows = await _report_rows(client)

        titles = [row[1] for row in rows[1:]]
        assert titles == ["Scored", "Unscored"]

    async def test_the_pre_remap_blend_outranks_the_album_remapped_score(
        self, client, db_session
    ):
        """THE REPORTED CASE.

        ``final_score`` is ``apply_album_relative_popularity(raw)`` — it
        re-normalises every album onto the same band, so the top cut of a live
        album (4,700 Last.fm listeners) read 97.68 and ranked ABOVE the
        artist's biggest hit (1.1M listeners, 88.7). ``raw_score`` is the
        cross-album blend that number was remapped FROM, and the report
        promises "most popular first" — so the raw blend must lead, with the
        remapped value still shown in its column.
        """
        _insert(
            db_session, track_id="t1", title="Live Cut",
            final_score=97.68, raw_score=68.70,
            lastfm_listeners=4_660, listenbrainz_listens=168_786,
        )
        _insert(
            db_session, track_id="t2", title="Studio Hit",
            final_score=88.72, raw_score=91.40,
            lastfm_listeners=1_099_696, listenbrainz_listens=957_674,
        )

        rows = await _report_rows(client)

        titles = [row[1] for row in rows[1:]]
        assert titles == ["Studio Hit", "Live Cut"], (
            "ranking by the album-relative final_score puts a 4.7k-listener "
            "live rendition above a 1.1M-listener hit — the report must order "
            "by the cross-album pre-remap blend"
        )
        assert [row[0] for row in rows[1:]] == ["1", "2"]

    async def test_a_legacy_row_without_raw_score_falls_back(self, client, db_session):
        """CONTROL — rows written before migration 016 keep the old chain."""
        _insert(db_session, track_id="t1", title="Modern", final_score=50.0, raw_score=60.0)
        _insert(db_session, track_id="t2", title="Legacy", final_score=70.0, raw_score=None)

        rows = await _report_rows(client)

        titles = [row[1] for row in rows[1:]]
        # The modern row's pre-remap 60 is compared against the legacy row's
        # FALLBACK (its final 70) numerically, so 70 sorts first here — what
        # matters is that the legacy row is still present and still ordered by
        # its own final_score, exactly as it was before raw_score existed.
        assert titles == ["Legacy", "Modern"]

    async def test_only_this_artists_tracks_appear(self, client, db_session):
        _insert(db_session, track_id="t1", title="Mine", final_score=50.0)
        _insert(
            db_session, track_id="t2", title="Someone Elses",
            artist="Other Artist", final_score=99.0,
        )

        rows = await _report_rows(client)

        titles = [row[1] for row in rows[1:]]
        assert titles == ["Mine"], (
            "the report is per artist — another artist's 99.0 must not appear"
        )

    async def test_an_album_artist_row_is_included_too(self, client, db_session):
        """A compilation track is filed under its ALBUM artist."""
        _insert(
            db_session, track_id="t1", title="Compilation Cut",
            artist="Somebody Else", album_artist="Test Artist", final_score=40.0,
        )

        rows = await _report_rows(client)

        assert [row[1] for row in rows[1:]] == ["Compilation Cut"], (
            "the artist key everywhere else is COALESCE(album_artist, artist)"
        )

    async def test_the_listens_and_scores_reach_the_file(self, client, db_session):
        _insert(
            db_session, track_id="t1", title="Rich", final_score=70.0, stars=5,
            lastfm_listeners=477_200, listenbrainz_listens=290,
        )

        _response, parsed = await _report(client)
        header, row = parsed[:2]
        record = dict(zip(header, row))

        assert record["lastfm_listeners"] == "477200"
        assert record["listenbrainz_listens"] == "290"
        assert record["stars"] == "5"
        assert record["final_score"] == "70.0"

    async def test_a_missing_artist_is_rejected(self, client):
        response = await client.get(REPORT_URL)

        assert response.status_code == 400, (
            "an empty artist would silently download the whole catalogue"
        )

    async def test_the_endpoint_requires_a_session(self, unauthed_client, db_session):
        """CONTROL — the report is a download URL, so it must not be public."""
        _insert(db_session, track_id="t1", title="Mine", final_score=50.0)

        response = await unauthed_client.get(f"{REPORT_URL}?artist=Test%20Artist")

        assert response.status_code != 200, (
            "a download endpoint left off the auth gate would expose the whole "
            "catalogure to anyone with the URL"
        )


class TestTheTestSiteArtistPageOffersIt:
    def test_the_actions_menu_has_the_download(self):
        source = TEST_SITE_ARTIST_PAGE.read_text(encoding="utf-8")

        assert "Download Popularity Report" in source, (
            "the requested button is missing from the test-site artist page"
        )
        assert "artist.api_artist_popularity_report" in source, (
            "the action must point at the report endpoint, not a placeholder"
        )

    def test_the_action_sits_in_the_actions_menu(self):
        """Where a user would look for it — with the other artist actions."""
        source = TEST_SITE_ARTIST_PAGE.read_text(encoding="utf-8")
        menu_start = source.index('<ul class="dropdown-menu dropdown-menu-end">')
        menu_end = source.index("</ul>", menu_start)
        menu = source[menu_start:menu_end]

        assert "Download Popularity Report" in menu
        assert "Corrections" in menu, (
            "the report must live with the other actions, not somewhere new"
        )

    def test_the_named_endpoint_actually_exists(self, app):
        """A typo'd ``url_for`` target raises BuildError and 500s the page.

        Asserted against the app's own URL map rather than the source: the
        template's string is only correct if it matches a registered rule.
        """
        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}

        assert "artist.api_artist_popularity_report" in endpoints, (
            "the artist page would render a BuildError instead of a download"
        )
        assert any(
            rule.rule == "/api/artist/popularity-report" and "GET" in rule.methods
            for rule in app.url_map.iter_rules()
        )
