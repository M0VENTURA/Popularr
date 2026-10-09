"""Albums are scoped by RELEASE GROUP, not by year.

Reported
--------
> [Battlefield Vietnam … /2004] and [Battlefield Vietnam … /1963] — this is
> what I mean, the same release, but split in years. The same Release MBID is
> assigned but has two different pages to browse to under test_site.

And:

> Ones that do get split should be viewable under the artist page to be able to
> browse to each release properly to edit the information.

Why year was never a sound identity
-----------------------------------
A compilation credits its source *recordings*, so the tracks of ONE release
carry many different years. `2003 - Battlefield Vietnam` holds tracks tagged
1963 … 2004 — and the old rule (`album_key = name + year`) sliced that single
release into one page per year. The screenshots show it exactly: `…/2004`
listed 7 tracks (2, 7, 11, 13–17) and `…/1963` listed 16 (1–9, 11–17), both
from the same folder, both headed "(2004)".

The rule now
------------
* an explicit **UUID** segment → that release group;
* **one** release group on the rows → *no split at all*, even when the URL
  carries a year (old `/2004` links stop slicing, and the dashboard keeps
  building them);
* several release groups sharing a name → the one with the most tracks, with
  the artist page listing **each** of them as its own browsable card;
* **no** release-group data → the old year split, unchanged.

Digits still mean a year, so every existing link keeps working.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"
LIVE_SECTION = REPO_ROOT / "templates" / "components" / "_album_category_section.html"
REBUILT_SECTION = REPO_ROOT / "test_site" / "templates" / "components" / "_album_category_section.html"


class TestTheAlbumPageKnowsItsScope:
    def test_a_uuid_segment_is_read_as_a_release_group(self):
        src = UI_ROUTES.read_text(encoding="utf-8")

        assert "_RG_SEGMENT_RE" in src, "the path segment is never parsed as a UUID"
        assert "if _RG_SEGMENT_RE.fullmatch(album_year_seg) else \"\"" in src, (
            "the segment must be classified BEFORE it is treated as a year — "
            "a UUID is not a number and would otherwise select nothing"
        )

    def test_a_single_release_group_never_splits(self):
        """The reported bug, stated as code."""
        src = UI_ROUTES.read_text(encoding="utf-8")

        assert "elif len(release_groups) == 1:" in src, (
            "one release group must produce one page"
        )
        # No year branch may exist to beat it. The year stopped identifying
        # anything when same-name albums without a release ID were merged too
        # (2026-10-09), so there is nothing left for a year URL to slice with.
        assert "elif explicit_year and album_year_filter is not None:" not in src, (
            "a year-scoping branch is back — an album with a release group must "
            "never be sliced by the year in its address"
        )

    def test_several_release_groups_default_to_the_largest(self):
        src = UI_ROUTES.read_text(encoding="utf-8")
        assert "elif len(release_groups) > 1:" in src
        assert "_primary = max(counts, key=lambda r: counts[r])" in src, (
            "with several releases sharing a name the page must pick one "
            "deterministically rather than merging them"
        )

    def test_no_release_group_data_also_merges_every_year(self):
        """Reported: "I also want it to show on similar albums with no release
        ID but the same name with a different year."""
        src = UI_ROUTES.read_text(encoding="utf-8")
        branch = src[src.index("else:", src.index("elif len(release_groups) > 1:")):]
        head = branch.split("\n\n")[0]
        assert "year_tracks" not in head, (
            "an album with no release-group data still splits by year — the "
            "same name across years must land on ONE page"
        )
        assert "pass" in head, (
            "the no-release-group branch must merge (the fall-through case)"
        )


class TestTheArtistPageGroupsByRelease:
    def test_the_group_key_is_built_from_the_release_group(self):
        src = UI_ROUTES.read_text(encoding="utf-8")
        assert "release_groups_by_name.setdefault" in src, (
            "the artist page still keys albums by name + year — one release "
            "would keep appearing as several cards"
        )
        assert 'album_key = f"{_name_key}::rg:' in src

    def test_each_card_carries_the_release_group_for_its_link(self):
        src = UI_ROUTES.read_text(encoding="utf-8")
        assert '"release_group_mbid": entry_rg,' in src, (
            "a card without a release group cannot link to a release-scoped "
            "album page, so split releases would not be browsable"
        )

    def test_the_tracks_are_collected_with_the_same_key(self):
        """Not re-derived from name + year — that would mis-fill an RG card."""
        src = UI_ROUTES.read_text(encoding="utf-8")
        assert "tracks_by_key.setdefault(album_key, []).append(track)" in src
        assert "tracks_by_key.get(album_key, [])" in src


class TestTheLinksPreferTheReleaseGroup:
    def test_both_trees_scope_their_links(self):
        for path in (LIVE_SECTION, REBUILT_SECTION):
            html = path.read_text(encoding="utf-8")
            assert "{{ album_scope }}" in html, (
                f"{path.name} still links by year only"
            )
            assert "album_rg" in html, (
                f"{path.name} never computes the release-group scope"
            )

    def test_the_old_year_segment_is_gone_from_the_links(self):
        for path in (LIVE_SECTION, REBUILT_SECTION):
            html = path.read_text(encoding="utf-8")
            assert "/{{ album.get('album_year') }}{% endif %}" not in html, (
                f"{path.name} still appends a year segment directly — the "
                "release group must win when it exists"
            )


@pytest.fixture(autouse=True)
def _sqlite_regexp_replace():
    """The album page's ``ORDER BY`` uses Postgres-only ``regexp_replace``.

    Stamped on the LIVE connection as well as registered for future ones:
    ``StaticPool`` may already have opened the shared connection before this
    module imported, and an event listener alone would then never fire.
    """
    import re as _re

    from conftest import register_sqlite_regexp_replace
    from db.engine import get_engine

    def _regexp_replace(value, pattern, repl, flags=""):
        if value is None:
            return None
        try:
            return _re.sub(
                pattern, repl, str(value),
                flags=_re.IGNORECASE if "i" in flags else 0,
            )
        except Exception:
            return str(value)

    engine = get_engine()
    register_sqlite_regexp_replace(engine)
    try:
        with engine.connect() as conn:
            pooled = conn.connection
            dbapi = getattr(pooled, "dbapi_connection", None) or pooled.driver_connection
            dbapi.create_function("regexp_replace", -1, _regexp_replace)
    except Exception:
        pass
    yield


class TestTheYearURLCannotSliceAReleaseGroup:
    """The reported bug, end to end.

    > Albums with the same release group ID but have the wrong years are being
    > split by year. I want them all to appear on the one release.

    Two URLs differing only in the year used to reach two different pages.
    The release group now wins outright: a year address is LEGACY, so it
    canonicalises onto the release group instead of scoping anything.
    """

    RG = "cb232173-17fa-309e-80a1-b0490c646a38"

    @staticmethod
    def _seed(db_session, track_id, artist, album, title, year, rg=""):
        from sqlalchemy import text

        db_session.execute(
            text("""
                INSERT INTO tracks (id, artist, album, title, file_path, year,
                                    musicbrainz_releasegroupid)
                VALUES (:id, :artist, :album, :title, :file_path, :year, :rg)
                ON CONFLICT DO NOTHING
            """),
            {
                "id": track_id,
                "artist": artist,
                "album": album,
                "title": title,
                "file_path": f"/music/{artist}/{album}/{title}.flac",
                "year": year,
                "rg": rg,
            },
        )
        db_session.commit()

    async def test_a_year_url_collapses_onto_the_release_group(
        self, app, client, db_session
    ):
        album = "Nine Destinies and a Downfall"
        self._seed(db_session, "rg-a", "Sirenia", album, "First Edition Track", "1999", self.RG)
        self._seed(db_session, "rg-b", "Sirenia", album, "Second Edition Track", "2015", self.RG)

        resp = await client.get(f"/album/Sirenia/{album}/1999".replace(" ", "%20"))
        assert resp.status_code in (301, 302), (
            "a year address on an album that HAS a release group must not be a "
            f"second page — got {resp.status_code}"
        )
        location = resp.headers["Location"]
        assert location.endswith(f"/{self.RG}"), location

        page = await client.get(location)
        assert page.status_code == 200
        body = await page.get_data(as_text=True)
        assert "First Edition Track" in body
        assert "Second Edition Track" in body, (
            "the release must appear as ONE page carrying every year"
        )

    async def test_both_year_addresses_reach_the_same_page(
        self, app, client, db_session
    ):
        album = "Same Release Two Addresses"
        self._seed(db_session, "y-a", "Artist", album, "Nineteen Ninety Nine", "1999", self.RG)
        self._seed(db_session, "y-b", "Artist", album, "Twenty Fifteen", "2015", self.RG)

        first = await client.get(f"/album/Artist/{album}/1999".replace(" ", "%20"))
        second = await client.get(f"/album/Artist/{album}/2015".replace(" ", "%20"))
        assert first.headers["Location"] == second.headers["Location"], (
            "two year addresses must collapse to ONE canonical release URL"
        )

    async def test_a_year_url_without_a_release_group_collapses_to_the_bare_url(
        self, app, client, db_session
    ):
        """CONTROL: the merge applies to unbound albums too — the year is
        canonicalised away rather than used to scope."""
        album = "Unbound Editions"
        self._seed(db_session, "u-1", "Artist", album, "Old Cut", "1999")
        self._seed(db_session, "u-2", "Artist", album, "New Cut", "2015")

        resp = await client.get(f"/album/Artist/{album}/1999".replace(" ", "%20"))
        assert resp.status_code in (301, 302), (
            "a legacy year address must canonicalise, not serve a sliced page"
        )
        location = resp.headers["Location"]
        assert "1999" not in location, "the year must not survive as a scope"

        page = await client.get(location)
        assert page.status_code == 200
        body = await page.get_data(as_text=True)
        assert "Old Cut" in body
        assert "New Cut" in body, "same name, different year, no release ID → one page"

    def test_the_year_never_scopes_anything(self):
        src = UI_ROUTES.read_text(encoding="utf-8")
        assert "elif explicit_year" not in src, (
            "a year-scoping branch is back"
        )
        assert 'if request.method != "POST" and explicit_year:' in src, (
            "a year segment must still be canonicalised away — dropping the "
            "scope without redirecting would leave two addresses for one album"
        )
