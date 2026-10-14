"""Alternate names for artists — one artist under many names, not covers.

Reported
--------
> Can we add a section under about for the Artist page to have alternate names
> for artists. This should have it so that artists with both names will be
> merged under the one artist so aren't accidentally detected as cover songs,
> Such as Electric Callboy and Eskimo Callboy.

Root cause
----------
Every identity comparison in the app compares artist NAMES. A renamed band's
recording credited under its other name ("Eskimo Callboy" vs "Electric
Callboy") looked like a different artist, so the cover detector's self-credit
guards — ``_detect_via_isrc``'s ``_same_artist`` and the relation paths'
``names_match`` — reported the band's own song as a cover of somebody else.

The fix
-------
``artist_aliases`` records "these names are one artist" (alias → canonical).
``artist_alias_service.resolve_artist_alias`` resolves names through the whole
(table-sized, TTL-cached) map, and ``helpers.normalization_service.names_match``
— the primitive the cover detector uses at every comparison site — resolves
BOTH sides through it first. With no aliases registered the resolution is a
lookup on an empty map: the pre-alias behaviour, exactly.

The page gets an "Alternate Names" card under About (add/remove), served by
``GET/POST /api/artist/aliases`` + ``POST /api/artist/aliases/remove``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_SITE_ARTIST_PAGE = (
    REPO_ROOT / "test_site" / "templates" / "Pages" / "artist_detail_v2.html"
)
ARTIST_EXTRAS_JS = (
    REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist-detail-extras.js"
)

ALIASES_URL = "/api/artist/aliases"
ALIASES_REMOVE_URL = "/api/artist/aliases/remove"


@pytest.fixture(autouse=True)
def _clean_alias_state():
    """A fresh alias table AND resolution cache for every test.

    The cache is module-level (the hot path's whole point), so it would
    otherwise leak one test's aliases into the next — and a leaked map makes
    an unrelated test's ``names_match`` assertions flap.
    """
    from sqlalchemy import text

    from db.engine import db_session
    from db.schema import TABLES_TO_ENSURE
    from db.engine import get_engine
    from services.metadata.artist_alias_service import invalidate_alias_cache

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(TABLES_TO_ENSURE["artist_aliases"]))

    invalidate_alias_cache()
    with db_session() as session:
        session.execute(text("DELETE FROM artist_aliases"))
    yield
    invalidate_alias_cache()


# ===========================================================================
# 1. The merge itself: resolution + the names_match primitive
# ===========================================================================


class TestResolutionMergesTheTwoNames:
    def test_an_unknown_name_resolves_to_itself(self):
        """CONTROL — no alias registered means the input comes back unchanged,
        which is what keeps ``names_match``'s pre-alias behaviour intact."""
        from services.metadata.artist_alias_service import resolve_artist_alias

        assert resolve_artist_alias("Nobody Famous") == "Nobody Famous"
        assert resolve_artist_alias("") == ""

    def test_a_registered_alias_resolves_to_the_canonical_artist(self):
        from services.metadata.artist_alias_service import (
            add_alias,
            resolve_artist_alias,
        )

        add_alias("Electric Callboy", "Eskimo Callboy")

        assert resolve_artist_alias("Eskimo Callboy") == "Electric Callboy"
        # The canonical side is untouched — it resolves to itself.
        assert resolve_artist_alias("Electric Callboy") == "Electric Callboy"
        # Case/whitespace spellings of the alias resolve too (keys are
        # casefolded, matching the SQL side).
        assert resolve_artist_alias("ESKIMO CALLBOY") == "Electric Callboy"

    def test_chains_resolve_to_the_final_canonical(self):
        """A→B registered before B→C still resolves A→C (cycle-guarded hops)."""
        from services.metadata.artist_alias_service import (
            add_alias,
            resolve_artist_alias,
        )

        add_alias("Band B", "Band A")
        add_alias("Band C", "Band B")

        assert resolve_artist_alias("Band A") == "Band C"
        assert resolve_artist_alias("Band B") == "Band C"

    def test_names_match_merges_the_registered_names(self):
        """THE POINT: with the alias registered, the two names are ONE artist
        to every ``names_match`` caller — which is the cover detector's
        comparison primitive."""
        from helpers.normalization_service import names_match
        from services.metadata.artist_alias_service import add_alias

        assert names_match("Eskimo Callboy", "Electric Callboy") is False, (
            "CONTROL — before registration the two names must not match, or "
            "the test cannot prove the alias did the merging"
        )

        add_alias("Electric Callboy", "Eskimo Callboy")

        assert names_match("Eskimo Callboy", "Electric Callboy") is True
        assert names_match("Electric Callboy", "Eskimo Callboy") is True, (
            "the merge is symmetric — a credit under either name compares equal"
        )

    def test_the_merge_does_not_leak_into_unrelated_names(self):
        from helpers.normalization_service import names_match
        from services.metadata.artist_alias_service import add_alias

        add_alias("Electric Callboy", "Eskimo Callboy")

        assert names_match("Eskimo Callboy", "Ghostkid") is False
        assert names_match("Electric Callboy", "Ghostkid") is False

    def test_same_artist_uses_the_alias_too(self):
        """``_same_artist`` is the self-credit guard ``_detect_via_isrc``
        runs; it wraps ``names_match``, so the alias reaches it."""
        from services.enrichment.cover_detector_impl import _same_artist
        from services.metadata.artist_alias_service import add_alias

        add_alias("Electric Callboy", "Eskimo Callboy")

        assert _same_artist("Eskimo Callboy", "Electric Callboy") is True


# ===========================================================================
# 2. The cover detector specifically: a band's own song is not its cover
# ===========================================================================


class TestTheCoverDetectorSeesOneArtist:
    def _detector_with_isrc_lookup(self, monkeypatch, rec_artist: str):
        from services.enrichment.cover_detector_impl import CoverDetector

        detector = CoverDetector()

        def _fake_lookup(isrc, inc=""):
            return [{
                "id": "rec-1",
                "title": "We Got The Moves",
                "first-release-date": "2021-05-01",
                "artist-credit": [{"artist": {"name": rec_artist}}],
            }]

        # The MB client is a process-wide singleton; swap it on the INSTANCE
        # (monkeypatch restores it) so no network can fire either way.
        monkeypatch.setattr(
            detector, "mb", type("MB", (), {"lookup_by_isrc": staticmethod(_fake_lookup)})()
        )
        return detector

    def test_a_credit_under_the_other_name_is_not_a_cover(self, monkeypatch):
        from services.metadata.artist_alias_service import add_alias

        add_alias("Electric Callboy", "Eskimo Callboy")
        detector = self._detector_with_isrc_lookup(monkeypatch, "Eskimo Callboy")

        verdict = detector._detect_via_isrc(
            "DEUM12345678", "We Got The Moves",
            album_artist="Electric Callboy", track_artist="Electric Callboy",
        )

        assert verdict is None, (
            "the recording credited to the band's OWN other name must be "
            "skipped — this is the reported false cover"
        )

    def test_the_same_credit_without_the_alias_still_detects(self):
        """CONTROL — proves the alias (and nothing else) suppresses the
        verdict: without it, a differing credit IS still cover evidence."""
        from services.enrichment.cover_detector_impl import CoverDetector

        detector = CoverDetector()
        detector.mb = type("MB", (), {"lookup_by_isrc": staticmethod(
            lambda isrc, inc="": [{
                "id": "rec-1",
                "title": "We Got The Moves",
                "first-release-date": "2021-05-01",
                "artist-credit": [{"artist": {"name": "Eskimo Callboy"}}],
            }]
        )})()

        verdict = detector._detect_via_isrc(
            "DEUM12345678", "We Got The Moves",
            album_artist="Electric Callboy", track_artist="Electric Callboy",
        )

        assert verdict is not None, (
            "with no alias registered a differing credit must still be "
            "treated as cover evidence — the detector cannot be silenced"
        )
        assert verdict["confidence"] == "medium"

    def test_a_genuinely_different_artist_is_still_a_cover(self, monkeypatch):
        from services.metadata.artist_alias_service import add_alias

        add_alias("Electric Callboy", "Eskimo Callboy")
        detector = self._detector_with_isrc_lookup(monkeypatch, "Ghostkid")

        verdict = detector._detect_via_isrc(
            "DEUM12345678", "We Got The Moves",
            album_artist="Electric Callboy", track_artist="Electric Callboy",
        )

        assert verdict is not None, (
            "the alias merges TWO names, not every artist — an unrelated "
            "credit must still be flagged"
        )
        assert verdict["artist"] == "Ghostkid"


# ===========================================================================
# 3. The group the page shows (both pages agree)
# ===========================================================================


class TestTheGroup:
    def test_both_pages_of_a_pair_show_the_same_group(self):
        from services.metadata.artist_alias_service import add_alias, get_alias_group

        add_alias("Electric Callboy", "Eskimo Callboy")

        on_electric, code_e = get_alias_group("Electric Callboy")
        on_eskimo, code_s = get_alias_group("Eskimo Callboy")

        assert (code_e, code_s) == (200, 200)
        assert on_electric["aliases"] == ["Eskimo Callboy"]
        assert on_eskimo["aliases"] == ["Electric Callboy"]
        assert on_electric["canonical"] == on_eskimo["canonical"] == "Electric Callboy"

    def test_a_chain_shows_every_member_on_every_page(self):
        from services.metadata.artist_alias_service import add_alias, get_alias_group

        add_alias("Band B", "Band A")
        add_alias("Band C", "Band B")

        all_names = {
            "band a": "Band A", "band b": "Band B", "band c": "Band C",
        }
        for page in ("Band A", "Band B", "Band C"):
            group, code = get_alias_group(page)
            expected = sorted(
                display for key, display in all_names.items() if key != page.lower()
            )
            assert code == 200
            assert group["aliases"] == expected, (
                f"page {page!r} must see the whole group minus itself, not "
                "just its own pointer"
            )

    def test_an_unaliased_artist_has_an_empty_group(self):
        from services.metadata.artist_alias_service import get_alias_group

        group, code = get_alias_group("Nobody Famous")

        assert code == 200
        assert group["aliases"] == []
        assert group["canonical"] == "Nobody Famous"

    def test_a_missing_artist_is_rejected(self):
        from services.metadata.artist_alias_service import get_alias_group

        _data, code = get_alias_group("")
        assert code == 400


# ===========================================================================
# 4. CRUD semantics
# ===========================================================================


class TestAddAndRemove:
    def test_add_lists_the_alias_and_is_idempotent(self):
        from services.metadata.artist_alias_service import add_alias

        data, code = add_alias("Electric Callboy", "Eskimo Callboy")
        assert code == 200 and data["success"]
        assert data["aliases"] == ["Eskimo Callboy"]

        again, code2 = add_alias("Electric Callboy", "eskimo callboy")
        assert code2 == 200 and again.get("already") is True
        assert again["aliases"] == ["Eskimo Callboy"], "no duplicate row"

    def test_add_rejects_missing_and_self(self):
        from services.metadata.artist_alias_service import add_alias

        _d, code = add_alias("", "Eskimo Callboy")
        assert code == 400
        _d, code = add_alias("Electric Callboy", "")
        assert code == 400
        _d, code = add_alias("Electric Callboy", "electric callboy")
        assert code == 400, "an artist cannot be an alternate name of itself"

    def test_a_name_owned_by_another_artist_is_a_conflict(self):
        from services.metadata.artist_alias_service import add_alias

        add_alias("Electric Callboy", "Eskimo Callboy")
        data, code = add_alias("Ghostkid", "Eskimo Callboy")

        assert code == 409
        assert "Electric Callboy" in data["error"], (
            "the conflict must name the owner, or the user cannot tell whose "
            "group holds the name"
        )

    def test_adding_a_second_name_extends_the_group(self):
        from services.metadata.artist_alias_service import add_alias, get_alias_group

        add_alias("Electric Callboy", "Eskimo Callboy")
        add_alias("Electric Callboy", "Callboy")

        group, _code = get_alias_group("Electric Callboy")
        assert sorted(group["aliases"]) == ["Callboy", "Eskimo Callboy"]

    def test_remove_unlinks_a_sibling(self):
        from services.metadata.artist_alias_service import (
            add_alias,
            get_alias_group,
            remove_alias,
        )

        add_alias("Electric Callboy", "Eskimo Callboy")
        add_alias("Electric Callboy", "Callboy")

        data, code = remove_alias("Electric Callboy", "Eskimo Callboy")
        assert code == 200 and data["success"]
        assert data["aliases"] == ["Callboy"]

    def test_remove_is_idempotent(self):
        from services.metadata.artist_alias_service import (
            add_alias,
            remove_alias,
        )

        add_alias("Electric Callboy", "Eskimo Callboy")
        remove_alias("Electric Callboy", "Eskimo Callboy")

        data, code = remove_alias("Electric Callboy", "Eskimo Callboy")
        assert code == 200 and data["success"]
        assert data["aliases"] == []

    def test_removing_the_canonical_from_the_alias_page_unlinks_that_page(self):
        """The alias page shows the canonical name as an entry — clicking × on
        it must take THIS page out of the group. There is no "canonical row"
        to delete in a plain pair, so the page's own binding is the target."""
        from services.metadata.artist_alias_service import (
            add_alias,
            get_alias_group,
            remove_alias,
            resolve_artist_alias,
        )

        add_alias("Electric Callboy", "Eskimo Callboy")

        data, code = remove_alias("Eskimo Callboy", "Electric Callboy")
        assert code == 200
        assert data["aliases"] == [], "the alias page is back to no group"

        assert resolve_artist_alias("Eskimo Callboy") == "Eskimo Callboy", (
            "the unlink must actually drop the row, or the badge reappears "
            "on reload"
        )
        # The other page is untouched: it never held a row of its own.
        group, _code = get_alias_group("Electric Callboy")
        assert group["aliases"] == []

    def test_writes_invalidate_the_resolution_cache(self):
        """End-to-end freshness: the API write must be visible to
        ``names_match`` IMMEDIATELY, not after the TTL — otherwise a user adds
        an alias and the next scan still flags covers."""
        from helpers.normalization_service import names_match
        from services.metadata.artist_alias_service import add_alias

        # Prime the cache with an empty map first (a read happens before the write).
        assert names_match("Eskimo Callboy", "Electric Callboy") is False

        add_alias("Electric Callboy", "Eskimo Callboy")

        assert names_match("Eskimo Callboy", "Electric Callboy") is True, (
            "add_alias must invalidate the cached map, not wait for the TTL"
        )


# ===========================================================================
# 5. The endpoints
# ===========================================================================


class TestTheEndpoints:
    async def test_get_returns_the_group(self, client):
        from services.metadata.artist_alias_service import add_alias

        add_alias("Electric Callboy", "Eskimo Callboy")

        resp = await client.get(f"{ALIASES_URL}?artist=Electric%20Callboy")
        assert resp.status_code == 200
        payload = await resp.get_json()
        assert payload["aliases"] == ["Eskimo Callboy"]
        assert payload["canonical"] == "Electric Callboy"

    async def test_get_requires_an_artist(self, client):
        resp = await client.get(ALIASES_URL)
        assert resp.status_code == 400

    async def test_post_adds_and_post_remove_removes(self, client):
        resp = await client.post(
            ALIASES_URL,
            json={"artist": "Electric Callboy", "alias": "Eskimo Callboy"},
        )
        assert resp.status_code == 200
        payload = await resp.get_json()
        assert payload["success"] and payload["aliases"] == ["Eskimo Callboy"]

        resp = await client.post(
            ALIASES_REMOVE_URL,
            json={"artist": "Electric Callboy", "alias": "Eskimo Callboy"},
        )
        assert resp.status_code == 200
        payload = await resp.get_json()
        assert payload["success"] and payload["aliases"] == []

    async def test_post_conflict_is_409(self, client):
        from services.metadata.artist_alias_service import add_alias

        add_alias("Electric Callboy", "Eskimo Callboy")

        resp = await client.post(
            ALIASES_URL, json={"artist": "Ghostkid", "alias": "Eskimo Callboy"}
        )
        assert resp.status_code == 409
        payload = await resp.get_json()
        assert "Electric Callboy" in payload["error"]

    async def test_post_missing_fields_are_400(self, client):
        resp = await client.post(ALIASES_URL, json={"artist": "Electric Callboy"})
        assert resp.status_code == 400

        resp = await client.post(ALIASES_REMOVE_URL, json={})
        assert resp.status_code == 400

    async def test_the_endpoints_require_a_session(self, unauthed_client):
        """CONTROL — a write endpoint left off the auth gate would let anyone
        with the URL rewrite artist identity for the whole library."""
        resp = await unauthed_client.get(f"{ALIASES_URL}?artist=Electric%20Callboy")
        assert resp.status_code != 200

        resp = await unauthed_client.post(
            ALIASES_URL, json={"artist": "A", "alias": "B"}
        )
        assert resp.status_code != 200

    def test_the_endpoints_are_registered(self, app):
        """A typo'd ``url_for`` target raises BuildError and 500s the page."""
        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}

        assert "artist.api_artist_aliases" in endpoints
        assert "artist.api_artist_aliases_add" in endpoints
        assert "artist.api_artist_aliases_remove" in endpoints


# ===========================================================================
# 6. The page: the section under About, and its wiring
# ===========================================================================


class TestTheAboutSection:
    def test_the_card_sits_inside_the_about_tab(self):
        source = TEST_SITE_ARTIST_PAGE.read_text(encoding="utf-8")

        about_start = source.index('id="tab-about"')
        section = source.index('id="artist-aliases-section"')
        similar_card = source.index('id="artist-similar-section"')

        assert about_start < section < similar_card, (
            "the Alternate Names card must render inside the About tab, "
            "before the Similar Artists card that closes it"
        )

    def test_the_card_has_an_input_and_an_add_button(self):
        source = TEST_SITE_ARTIST_PAGE.read_text(encoding="utf-8")

        assert 'id="artistAliasInput"' in source
        assert 'id="artistAliasesList"' in source
        assert "addArtistAlias()" in source, "the Add button must call the handler"

    def test_the_initial_list_is_server_rendered(self):
        """Both the badges and the empty state come from the payload, so the
        section is correct before any JS runs."""
        source = TEST_SITE_ARTIST_PAGE.read_text(encoding="utf-8")

        assert "artist_aliases" in source
        assert 'data-action="remove-artist-alias"' in source
        assert "No alternate names." in source

    def test_the_merge_hint_renders_for_an_alias_page(self):
        source = TEST_SITE_ARTIST_PAGE.read_text(encoding="utf-8")

        assert "artist_alias_canonical" in source
        assert "merged as" in source

    def test_the_js_defines_the_handlers(self):
        source = ARTIST_EXTRAS_JS.read_text(encoding="utf-8")

        assert "global.addArtistAlias = addArtistAlias" in source, (
            "the template's onclick resolves by GLOBAL name — an unexported "
            "handler is a dead button (templates have no compiler)"
        )
        assert 'data-action="remove-artist-alias"' in source, (
            "the delegated remove binding must exist for the badges to work"
        )
        assert "removeArtistAlias(" in source

    def test_the_payload_carries_the_group(self):
        source = (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")

        assert '"artist_aliases": artist_aliases' in source
        assert '"artist_alias_canonical": artist_alias_canonical' in source, (
            "the page payload must feed the template's section"
        )
