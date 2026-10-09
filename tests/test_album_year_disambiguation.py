"""Same-name albums released in different years now share ONE album page.

CONTRACT REVERSED on request (2026-10-09):

    > I also want it to show on similar albums with no release ID but the same
    > name with a different year.

This file originally pinned the OPPOSITE (2026-08-28): same-name albums with
different years were split, defaulting to the most recent, with a year selector
linking to ``/album/<artist>/<album>/<year>``.  The year selector has since
been dropped from the template, and the album page now merges — with or without
a release group:

* a release-group segment scopes to that release group;
* release-group data on the rows (one or several) scopes to the release group;
* NO release-group data → every year's tracks on ONE page;
* a legacy ``/<year>`` address 302s to the canonical one (release group when
  known, else the bare album URL) — the year identifies nothing.

``tests/test_album_release_group_scope.py`` keeps the release-group half of the
contract; this file owns the no-release-ID half.
"""

from __future__ import annotations

from sqlalchemy import text


def _seed_album(db_session, track_id, artist, album, title, year, release_year=None):
    db_session.execute(
        text("""
            INSERT INTO tracks (id, artist, album, title, file_path, year, release_year)
            VALUES (:id, :artist, :album, :title, :file_path, :year, :release_year)
            ON CONFLICT DO NOTHING
        """),
        {
            "id": track_id,
            "artist": artist,
            "album": album,
            "title": title,
            "file_path": f"/music/{artist}/{album}/{title}.flac",
            "year": year,
            "release_year": release_year,
        },
    )
    db_session.commit()


class TestAlbumYearDisambiguation:
    async def test_route_merges_every_year(self, app, client, db_session):
        """Opening /album/<artist>/<album> shows EVERY edition's tracks — same
        name, different year, no release ID."""
        _seed_album(db_session, "t1", "Artist", "Same Name", "Track One", "1999")
        _seed_album(db_session, "t2", "Artist", "Same Name", "Track Two", "1999")
        _seed_album(db_session, "t3", "Artist", "Same Name", "Remaster Track", "2015")

        resp = await client.get("/album/Artist/Same%20Name")
        assert resp.status_code == 200
        body = await resp.get_data(as_text=True)
        assert "Track One" in body
        assert "Track Two" in body
        assert "Remaster Track" in body, (
            "the 2015 edition must appear alongside the 1999 one"
        )

    async def test_route_year_segment_collapses_onto_the_merged_page(
        self, app, client, db_session
    ):
        """A legacy /<year> address redirects instead of slicing."""
        _seed_album(db_session, "t1", "Artist", "Same Name", "Track One", "1999")
        _seed_album(db_session, "t3", "Artist", "Same Name", "Remaster Track", "2015")

        resp = await client.get("/album/Artist/Same%20Name/1999")
        assert resp.status_code in (301, 302), (
            "the year address must canonicalise, not serve a sliced page"
        )
        location = resp.headers["Location"]
        assert "1999" not in location, "the year must not survive as a scope"

        page = await client.get(location)
        assert page.status_code == 200
        body = await page.get_data(as_text=True)
        assert "Track One" in body
        assert "Remaster Track" in body, "both editions on the one page"

    async def test_single_year_album_unaffected(self, app, client, db_session):
        """An album with a single year is shown in full (no year selector)."""
        _seed_album(db_session, "t1", "Artist", "Only Album", "Track One", "2001")
        _seed_album(db_session, "t2", "Artist", "Only Album", "Track Two", "2001")

        resp = await client.get("/album/Artist/Only%20Album")
        assert resp.status_code == 200
        body = await resp.get_data(as_text=True)
        assert "Track One" in body
        assert "Track Two" in body
