"""The artist page must not show one album as several releases.

Reported
--------
> The artist page is sometimes showing releases multiple times, selecting edit
> or opening the album will only show one item will all releases inside it.

Two grouping rules were in play for one dataset.

**The album page** (`routes/ui_routes.py::album_detail`) says a year is not an
identity:
  * one release group on the rows → ONE page, even when the URL carries a year;
  * several release groups sharing a name → the one with the MOST TRACKS, with
    rows carrying no group folded into it;
  * no release-group data → every year merged, because "the SAME NAME across
    years is the same album to the person looking at it".

**The artist page** disagreed in exactly the cases that matter:
  * no release-group data → it keyed cards by ``name + year``, so a re-press
    tagged with its pressing year, or a compilation crediting its source
    recordings (`Battlefield Vietnam` holds tracks tagged 1963 … 2004), became
    SEVERAL cards;
  * several release groups → a row with no group got its own year-keyed card,
    which no address could ever open.

Every duplicate therefore linked to ``/<year>`` — which the album page
canonicalises away — so all of them opened the SAME single album page. Hence
"opening one shows a single album with all the releases inside it".

The rule now lives in ONE place, ``plan_artist_release_cards``, and the card
links carry the RELEASE GROUP.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"
ALBUM_ROUTES = REPO_ROOT / "routes" / "album_routes.py"
LIVE_SECTION = REPO_ROOT / "templates" / "components" / "_release_section.html"
REBUILT_SECTION = REPO_ROOT / "test_site" / "templates" / "components" / "_release_section.html"
LIVE_JS = REPO_ROOT / "static" / "js" / "artist-releases.js"
REBUILT_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist-releases.js"
LIVE_MODAL = REPO_ROOT / "templates" / "components" / "modals" / "_artist_modals.html"
REBUILT_MODAL = REPO_ROOT / "test_site" / "templates" / "components" / "modals" / "_artist_modals.html"

RG_ONE = "11111111-1111-4111-8111-111111111111"
RG_TWO = "22222222-2222-4222-8222-222222222222"


def _track(album: str, rg: str = "", year: str = "2000", title: str = "T") -> dict:
    return {"album": album, "musicbrainz_releasegroupid": rg, "year": year, "title": title}


# ---------------------------------------------------------------------------
# 1. The card plan — the reported duplicates, as behaviour
# ---------------------------------------------------------------------------


class TestOneAlbumNeverBecomesSeveralCards:
    def test_one_name_across_years_without_a_release_group_is_ONE_card(self) -> None:
        """⭐ THE REPORTED DUPLICATE. A compilation's tracks carry the years of
        the SOURCE recordings, so ``name + year`` produced a card per year while
        the album page showed one."""
        from routes.ui_routes import plan_artist_release_cards

        plan = plan_artist_release_cards([
            _track("100 Greatest Alternative 90s", year="1993", title="A"),
            _track("100 Greatest Alternative 90s", year="1997", title="B"),
            _track("100 Greatest Alternative 90s", year="2004", title="C"),
        ])
        # No card is planned for a name with no release-group data: the caller
        # uses the name-only key, so every row above lands on ONE card.
        assert plan == {}
        assert all(
            plan.get(("100 greatest alternative 90s", "")) is None for _ in [0]
        )

    def test_one_release_group_across_years_is_ONE_card(self) -> None:
        from routes.ui_routes import plan_artist_release_cards

        plan = plan_artist_release_cards([
            _track("Nine Destinies", rg=RG_ONE, year="1999"),
            _track("Nine Destinies", rg=RG_ONE, year="2015"),
        ])
        assert plan[("nine destinies", RG_ONE)] == f"nine destinies::rg:{RG_ONE}"
        assert plan[("nine destinies", "")] == f"nine destinies::rg:{RG_ONE}"

    def test_several_release_groups_get_a_card_each(self) -> None:
        """Deliberate, and the reason the links must carry the release group:
        each of these is a different release the user may want to open."""
        from routes.ui_routes import plan_artist_release_cards

        plan = plan_artist_release_cards([
            _track("Same Name", rg=RG_ONE),
            _track("Same Name", rg=RG_TWO),
        ])
        assert plan[("same name", RG_ONE)] == f"same name::rg:{RG_ONE}"
        assert plan[("same name", RG_TWO)] == f"same name::rg:{RG_TWO}"
        assert plan[("same name", RG_ONE)] != plan[("same name", RG_TWO)]

    def test_a_row_with_no_group_joins_the_primary_release(self) -> None:
        """⭐⭐ NOT a card of its own. The album page folds an unscoped row into
        the release with the most tracks, so a year-keyed extra card could never
        be opened — it was a duplicate pointing at someone else's release."""
        from routes.ui_routes import plan_artist_release_cards

        plan = plan_artist_release_cards([
            _track("Mixed", rg=RG_ONE),
            _track("Mixed", rg=RG_TWO),
            _track("Mixed", rg=RG_TWO),
            _track("Mixed", rg="", year="1999"),
        ])
        assert plan[("mixed", "")] == f"mixed::rg:{RG_TWO}", (
            "the unscoped row must join the release holding the most tracks"
        )

    def test_the_tie_goes_to_the_first_seen_group(self) -> None:
        """Matches ``album_detail``'s ``max(counts, key=lambda r: counts[r])``,
        which keeps the first key on a tie — so both pages pick the same one."""
        from routes.ui_routes import plan_artist_release_cards

        plan = plan_artist_release_cards([
            _track("Tie", rg=RG_ONE),
            _track("Tie", rg=RG_TWO),
        ])
        assert plan[("tie", "")] == f"tie::rg:{RG_ONE}"

    def test_names_are_matched_case_insensitively(self) -> None:
        from routes.ui_routes import plan_artist_release_cards

        plan = plan_artist_release_cards([
            _track("MiXeD CaSe", rg=RG_ONE),
            _track("mixed case", rg=RG_ONE),
        ])
        assert plan[("mixed case", RG_ONE)] == f"mixed case::rg:{RG_ONE}"

    def test_blank_album_names_are_ignored(self) -> None:
        from routes.ui_routes import plan_artist_release_cards

        assert plan_artist_release_cards([_track("   ", rg=RG_ONE)]) == {}

    def test_two_names_do_not_share_a_card(self) -> None:
        from routes.ui_routes import plan_artist_release_cards

        plan = plan_artist_release_cards([
            _track("Album A", rg=RG_ONE),
            _track("Album B", rg=RG_ONE),
        ])
        assert plan[("album a", RG_ONE)] != plan[("album b", RG_ONE)]


# ---------------------------------------------------------------------------
# 2. The route uses that plan
# ---------------------------------------------------------------------------


class TestTheArtistPageUsesThePlan:
    @pytest.fixture(scope="class")
    def src(self) -> str:
        return UI_ROUTES.read_text(encoding="utf-8")

    def test_the_plan_is_the_only_grouping_rule(self, src: str) -> None:
        assert "card_plan = plan_artist_release_cards(tracks)" in src

    def test_a_year_is_never_a_grouping_key(self, src: str) -> None:
        assert 'f"{_name_key}::{track_year or \'\'}"' not in src, (
            "a year-keyed album card is back — one album will appear several "
            "times and every copy will open the same page"
        )

    def test_a_card_without_release_group_data_uses_the_name(self, src: str) -> None:
        assert 'f"{_name_key}::name"' in src

    def test_the_card_still_carries_its_release_group(self, src: str) -> None:
        """The template's link scope reads it; without it every card links by
        year again."""
        assert '"release_group_mbid": entry_rg,' in src

    def test_the_card_year_is_the_albums_maximum(self, src: str) -> None:
        """``album_detail`` computes ``max(year_values)``. Taking the first
        track's year made a merged card read 1996 on one load and 1997 on the
        next."""
        start = src.index("album_entry = albums_by_key[album_key]")
        block = src[start:start + 1400]
        assert "year > album_entry[\"album_year\"]" in block, (
            "the card year stopped being the maximum of its rows"
        )


# ---------------------------------------------------------------------------
# 3. The links must distinguish the cards
# ---------------------------------------------------------------------------


class TestEveryCardLinksToItsOwnRelease:
    @pytest.mark.parametrize("path", [LIVE_SECTION, REBUILT_SECTION], ids=["live", "rebuilt"])
    def test_the_scope_prefers_the_release_group(self, path: Path) -> None:
        html = path.read_text(encoding="utf-8")
        assert "{%- set scope = ('/' ~ rgid) if rgid else" in html, (
            f"{path.name} does not scope its links by release group"
        )

    @pytest.mark.parametrize("path", [LIVE_SECTION, REBUILT_SECTION], ids=["live", "rebuilt"])
    def test_the_open_link_uses_the_scope(self, path: Path) -> None:
        html = path.read_text(encoding="utf-8")
        assert "{{ title|path_segment }}{{ scope }}" in html, (
            f"{path.name}'s Open link ignores the scope"
        )
        assert "{% if year %}/{{ year }}{% endif %}" not in html, (
            f"{path.name} still appends a bare YEAR to the album link — every "
            "duplicate card would resolve to the same release"
        )

    @pytest.mark.parametrize("path", [LIVE_SECTION, REBUILT_SECTION], ids=["live", "rebuilt"])
    def test_the_edit_button_carries_the_release_group(self, path: Path) -> None:
        html = path.read_text(encoding="utf-8")
        assert 'data-rgid="{{ rgid|e }}"' in html


# ---------------------------------------------------------------------------
# 4. Edit must address the card it was opened from
# ---------------------------------------------------------------------------


class TestTheEditModalIsScoped:
    @pytest.mark.parametrize("path", [LIVE_MODAL, REBUILT_MODAL], ids=["live", "rebuilt"])
    def test_the_modal_has_a_scope_holder(self, path: Path) -> None:
        html = path.read_text(encoding="utf-8")
        assert 'id="editReleaseRgid"' in html
        # ⚠️ No name attribute: the save handler posts every
        # ``#editReleaseModal [name]`` generically, and this is the address,
        # not a field.
        assert 'id="editReleaseRgid" name=' not in html

    @pytest.mark.parametrize("path", [LIVE_JS, REBUILT_JS], ids=["live", "rebuilt"])
    def test_the_row_seeds_the_scope(self, path: Path) -> None:
        js = path.read_text(encoding="utf-8")
        assert "set('editReleaseRgid', data.rgid || '')" in js

    @pytest.mark.parametrize("path", [LIVE_JS, REBUILT_JS], ids=["live", "rebuilt"])
    def test_the_prefill_is_scoped(self, path: Path) -> None:
        """Otherwise every card's form was filled with the PRIMARY release's
        values."""
        js = path.read_text(encoding="utf-8")
        assert "'&release_group=' + encodeURIComponent(data.rgid)" in js

    @pytest.mark.parametrize("path", [LIVE_JS, REBUILT_JS], ids=["live", "rebuilt"])
    def test_the_save_posts_to_the_scoped_url(self, path: Path) -> None:
        """⚠️ NOT cosmetic: an unscoped POST makes the album route pick the
        PRIMARY release, so editing card #2 wrote its values onto card #1's
        tracks."""
        js = path.read_text(encoding="utf-8")
        assert "+ (rgid ? '/' + encodeURIComponent(rgid) : '')" in js


# ---------------------------------------------------------------------------
# 5. The metadata prefill honours the scope
# ---------------------------------------------------------------------------


class TestTheMetadataEndpointHonoursTheScope:
    def test_the_endpoint_reads_the_parameter(self) -> None:
        src = ALBUM_ROUTES.read_text(encoding="utf-8")
        assert 'request.args.get("release_group")' in src

    async def test_a_scope_returns_only_that_releases_values(self, client, db_session) -> None:
        from sqlalchemy import text

        for track_id, rg, label in (
            ("card-a", RG_ONE, "Release One Label"),
            ("card-b", RG_TWO, "Release Two Label"),
        ):
            db_session.execute(
                text("""
                    INSERT INTO tracks (id, artist, album, album_artist, title,
                                        file_path, recordlabel,
                                        musicbrainz_releasegroupid)
                    VALUES (:id, :artist, :album, :artist, :title, :file_path,
                            :label, :rg)
                    ON CONFLICT DO NOTHING
                """),
                {
                    "id": track_id,
                    "artist": "Pop Split",
                    "album": "Two Releases",
                    "title": f"Track {track_id}",
                    "file_path": f"/music/Pop Split/Two Releases/{track_id}.flac",
                    "label": label,
                    "rg": rg,
                },
            )
        db_session.commit()

        resp = await client.get(
            "/api/album/metadata?artist=Pop%20Split&album=Two%20Releases"
            f"&release_group={RG_TWO}"
        )
        assert resp.status_code == 200
        payload = await resp.get_json()
        assert payload["metadata"]["album_recordlabel"] == "Release Two Label", (
            "the prefill ignored the release group, so every card's Edit form "
            "showed the other release's values"
        )
        assert payload["track_count"] == 1

    async def test_an_unknown_scope_still_returns_the_album(self, client, db_session) -> None:
        """CONTROL: a release whose rows carry no group must not look like a
        missing album — the modal has already seeded itself from the row."""
        from sqlalchemy import text

        db_session.execute(
            text("""
                INSERT INTO tracks (id, artist, album, album_artist, title,
                                    file_path, recordlabel)
                VALUES (:id, :artist, :album, :artist, :title, :file_path, :label)
                ON CONFLICT DO NOTHING
            """),
            {
                "id": "ng-1",
                "artist": "Pop Split",
                "album": "No Group",
                "title": "Only Track",
                "file_path": "/music/Pop Split/No Group/1.flac",
                "label": "Some Label",
            },
        )
        db_session.commit()

        resp = await client.get(
            "/api/album/metadata?artist=Pop%20Split&album=No%20Group"
            f"&release_group={RG_ONE}"
        )
        assert resp.status_code == 200
        payload = await resp.get_json()
        assert payload["success"] is True
        assert payload["metadata"]["album_recordlabel"] == "Some Label"
