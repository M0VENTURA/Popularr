"""The artist About SHEET — the page's third tab, moved behind "Read more".

The artist page used to carry Albums / Top Tracks / About as three tabs. The
About tab was removed and its contents became `components/modals/
_artist_about_modal.html`, opened from the hero's "Read more".

WHAT THESE TESTS PROTECT

1. **The id contract.** ``static/js/artist-page.js`` drives the sheet BY ID
   (``artistBioClamp``, ``artistCountryDisplay``, ``musicbrainzArtistId``, …).
   Moving that markup without its id silently disables the button that reads it
   — no error, no console warning, just a control that does nothing. Every id is
   asserted present in BOTH trees.

2. **The modals bundle.** ``components/modals/_artist_modals.html`` provides
   ``editArtistIdsModal`` (reached from the sheet's own "Edit IDs" button) and
   the per-row Edit Release / Search MusicBrainz sheets. It was included only by
   the UNUSED ``artist_detail.html``, so on the live page every one of those
   buttons opened nothing.

3. **Links are built from ids we hold, never guessed.** A Wikipedia or YouTube
   URL invented from the artist's name would be a dead link, so the sheet offers
   only the providers whose identifiers are stored.
"""

from __future__ import annotations

import pathlib

import pytest

from tests._test_site_render import REPO_ROOT, build_env

TREES = (
    ("templates", "pages/artist_detail_v2.html"),
    ("test_site/templates", "Pages/artist_detail_v2.html"),
)

SHEETS = (
    REPO_ROOT / "templates" / "components" / "modals" / "_artist_about_modal.html",
    REPO_ROOT / "test_site" / "templates" / "components" / "modals" / "_artist_about_modal.html",
)

LIVE_JS = REPO_ROOT / "static" / "js" / "artist-page.js"
REBUILT_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist-detail-extras.js"


def _payload() -> dict:
    """Every key the artist page reads, shaped like the real route's payload."""
    tracks = [
        {"id": "t1", "title": "Come As You Are", "album": "Nevermind", "stars": 5,
         "final_score": 88.0, "popularity_score": 88.0, "artist_z_score": 2.1},
    ]
    albums = [
        {"album": "Nevermind", "album_year": 1991, "track_count": 12,
         "avg_stars": 4.5, "is_missing": False, "release_id": ""},
        {"album": "Bleach", "album_year": 1989, "track_count": 0,
         "is_missing": True, "release_id": "rid-1", "cover_art_url": ""},
    ]
    return {
        "artist_name": "Test Artist",
        "artist_bio": "A biography long enough to be clamped.",
        "artist_country": "US",
        "artist_members": [
            {"name": "Mark Hoppus", "relation_type": "member of band",
             "attributes": ["bass", "vocals"], "ended": True, "end": "2005", "begin": "1992"},
            {"name": "Travis Barker", "relation_type": "member of band",
             "attributes": ["drums"], "ended": False, "begin": "1998", "end": ""},
        ],
        # Deliberately NO attributes on one member: the fallback path must not
        # print the relation_type as if it were the instrument.
        "artist_aliases": ["Plus 44"],
        "artist_alias_canonical": "Plus Forty Four",
        "similar_artists": {"display": [{"name": "Blink-182"}]},
        "stats": {
            "album_count": 2, "track_count": 14, "five_star_count": 1, "avg_stars": 2.43,
            "musicbrainz_artist_id": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
            "discogs_artist_id": "123456", "lastfm_artist_mbid": "",
        },
        "top_tracks": tracks,
        "genres": ["Alternative Rock"],
        "genre_sources": {},
        "album_categories": [{"key": "studio_albums", "section_id": "studio-albums",
                              "icon": "bi-disc", "label": "Studio Albums"}],
        "albums_by_category": {"studio_albums": albums},
        "appears_on_albums": [],
        "artist_playlists": [],
        "scan_active": False,
        "slskd_config": {"enabled": False},
    }


@pytest.fixture(params=TREES, ids=lambda t: t[0])
def rendered(request):
    """The artist page, rendered from ITS OWN tree."""
    root, name = request.param
    env = build_env("/artist/Test Artist")
    env.loader = __import__("jinja2").FileSystemLoader(str(REPO_ROOT / root))
    return env.get_template(name).render(**_payload())


# ---------------------------------------------------------------------------
# The reorganisation itself
# ---------------------------------------------------------------------------

class TestTheTabBecameASheet:
    def test_the_about_sheet_is_rendered(self, rendered):
        assert 'id="artistAboutModal"' in rendered

    def test_the_about_tab_is_gone(self, rendered):
        """Both the PANE and its tab BUTTON — leaving the button would show a
        tab that switches to nothing."""
        assert 'id="tab-about"' not in rendered
        assert 'id="tab-about-btn"' not in rendered

    def test_the_other_tabs_survive(self, rendered):
        """CONTROL: removing the third tab must not remove the first two."""
        assert 'id="tab-albums"' in rendered
        assert 'id="tab-top"' in rendered

    def test_read_more_opens_the_sheet(self, rendered):
        assert "goToArtistAbout()" in rendered


# ---------------------------------------------------------------------------
# The id contract the JS depends on
# ---------------------------------------------------------------------------

class TestTheIdsTheJsDrives:
    @pytest.mark.parametrize("element_id", [
        "artistAboutModal",     # the sheet itself
        "artistBioClamp",       # toggleArtistBio()
        "artistBioToggle",      # toggleArtistBio()
        "artistCountryDisplay",  # fetchArtistCountry(), editArtistCountry()
        "fetchArtistCountryBtn",  # fetchArtistCountry()
        "artistMembersDisplay",
        "musicbrainzArtistId",  # openEditArtistIdsModal() seeds from this
        "discogsArtistId",      # openEditArtistIdsModal() seeds from this
        "artistAliasesList",    # renderArtistAliases()
        "artistAliasInput",     # addArtistAlias()
        "artistSimilarArtistsContainer",  # loadSimilarArtists()
        "artistPendingUpdates",  # loadArtistPendingUpdates()
    ])
    def test_the_id_is_present(self, rendered, element_id):
        assert f'id="{element_id}"' in rendered, (
            f"{element_id} missing — the JS handler that reads it is now dead"
        )

    def test_the_country_badge_has_an_id(self):
        """⚠️ A regression guard for a specific historical bug.

        The country badge used to be an anonymous span, so fetchArtistCountry()
        and editArtistCountry() both wrote into a missing element: the badge only
        ever changed after a full page reload, which looks like the lookup simply
        not working.
        """
        for sheet in SHEETS:
            source = sheet.read_text(encoding="utf-8")
            idx = source.index('id="artistCountryDisplay"')
            # the badge it wraps must be inside the same element
            assert "badge bg-info" in source[idx: idx + 200], sheet.name


# ---------------------------------------------------------------------------
# The modals bundle
# ---------------------------------------------------------------------------

class TestTheModalsBundleIsIncluded:
    def test_the_bundle_is_present(self, rendered):
        """Without it, the sheet's Edit IDs button opens nothing."""
        assert 'id="editArtistIdsModal"' in rendered

    @pytest.mark.parametrize("modal_id", [
        "editArtistIdsModal",       # the sheet's "Edit IDs"
        "editReleaseModal",         # a row's Edit release
        "artistMbSearchModal",      # a row's Search MusicBrainz
        "editTrackFromArtistModal",
        "editTrackModal",
    ])
    def test_each_bundled_modal_renders(self, rendered, modal_id):
        assert f'id="{modal_id}"' in rendered

    def test_the_download_modal_is_guarded_not_fatal(self):
        """⚠️ `qbit_config` is supplied by NOTHING.

        Reading `qbit_config.enabled` unguarded raised UndefinedError and took
        the whole PAGE down rather than the modal — which is why the live
        template never included this partial. Both trees now guard it.
        """
        for rel in ("templates/components/modals/_artist_modals.html",
                    "test_site/templates/components/modals/_artist_modals.html"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "qbit_config is defined" in source, (
                f"{rel}: an unguarded qbit_config reference 500s every page that "
                "includes this partial"
            )


# ---------------------------------------------------------------------------
# Content correctness
# ---------------------------------------------------------------------------

class TestTheSheetContent:
    def test_member_roles_come_from_attributes(self, rendered):
        """MusicBrainz puts the instrument in `attributes` and the CATEGORY in
        `relation_type`, so rendering relation_type listed every musician as
        "member of band" — the same string, N times."""
        assert "bass, vocals" in rendered
        assert "drums" in rendered

    def test_relation_type_is_not_shown_as_the_role(self, rendered):
        assert "member of band" not in rendered

    def test_a_former_member_is_marked(self, rendered):
        assert "Former" in rendered

    def test_external_links_use_the_stored_ids(self, rendered):
        assert "musicbrainz.org/artist/a74b1b7f-71a5-4011-9441-d0b5e4122711" in rendered
        assert "discogs.com/artist/123456" in rendered

    def test_no_link_is_invented_for_a_provider_we_have_no_id_for(self, rendered):
        """⚠️ A Wikipedia/YouTube URL guessed from the name would be a dead link
        on every artist. Nothing in the database holds those URLs, so they are
        absent until a scan stores them."""
        assert "wikipedia.org" not in rendered
        assert "youtube.com" not in rendered

    def test_the_pending_panel_starts_hidden(self, rendered):
        """It is filled by a fetch, so it must not render as an empty panel that
        claims to summarise nothing."""
        idx = rendered.index('id="artistPendingUpdates"')
        tag = rendered[idx: rendered.index(">", idx)]
        assert "hidden" in tag, f"the panel renders visible while empty: {tag!r}"

    def test_manage_metadata_is_a_real_destination(self, rendered):
        assert "Manage Metadata" in rendered
        assert "Genre Management" in rendered


# ---------------------------------------------------------------------------
# Both trees' JS
# ---------------------------------------------------------------------------

class TestBothTreesImplementTheSheet:
    @pytest.mark.parametrize("path", [LIVE_JS, REBUILT_JS], ids=lambda p: p.parent.parent.name)
    def test_the_module_publishes_the_openers(self, path):
        source = path.read_text(encoding="utf-8")
        assert "openArtistAboutModal" in source
        assert "loadArtistPendingUpdates" in source

    @pytest.mark.parametrize("path", [LIVE_JS, REBUILT_JS], ids=lambda p: p.parent.parent.name)
    def test_read_more_no_longer_scrolls_to_a_dead_tab(self, path):
        """`goToArtistAbout` used to scroll to `#artist-bio-section` and expand
        the clamp. That element is gone with the tab, so a scroll-only version
        would do nothing at all."""
        source = path.read_text(encoding="utf-8")
        idx = source.index("function goToArtistAbout")
        body = source[idx: idx + 400]
        assert "openArtistAboutModal" in body

    @pytest.mark.parametrize("path", [LIVE_JS, REBUILT_JS], ids=lambda p: p.parent.parent.name)
    def test_the_aliases_endpoint_is_called(self, path):
        source = path.read_text(encoding="utf-8")
        assert "/api/artist/aliases" in source

    def test_the_live_module_binds_the_remove_selector(self):
        """The sheet is shared, so the live tree renders these badges too. When
        only the rebuilt module bound the delegated selector, the × buttons were
        inert in the live tree."""
        source = LIVE_JS.read_text(encoding="utf-8")
        assert 'data-action="remove-artist-alias"' in source
