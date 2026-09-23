"""A release category with nothing owned starts collapsed.

Reported request
----------------
"On an artist page, if a release type has 0 showing in Library, I want that
section minimized so on the page you can easily scroll past. It should still
show the total albums in missing above the section and allow it to be expanded
manually."

The count is what makes the collapse safe: the section is out of the way but its
"M missing" badge is still in the header, and the toggle reopens it. A section
that collapsed AND hid its count would just be lost information.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The shared macro library, which must stay identical in intent in both trees.
COMPONENTS = [
    "test_site/templates/components/_release_section.html",
    "templates/components/_release_section.html",
]

#: The per-release expand/collapse script, in both trees.
RELEASE_SCRIPTS = [
    "test_site/static/js/pages/artist-releases.js",
    "static/js/artist-releases.js",
]

#: The stylesheets that style the collapsed header.
STYLESHEETS = [
    "test_site/static/css/artist.css",
    "static/css/artist.css",
]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# The macro must base the decision on the LIBRARY count, not the total
# ---------------------------------------------------------------------------

class TestMacroStructure:
    @pytest.mark.parametrize("rel", COMPONENTS)
    def test_collapse_is_driven_by_the_library_count(self, rel):
        body = _read(rel)
        assert "set auto_collapse = library|length == 0" in body, (
            "the auto-collapse decision must use the LIBRARY count; using the "
            "total would keep a section of purely-missing releases open"
        )

    @pytest.mark.parametrize("rel", COMPONENTS)
    def test_body_class_is_conditional(self, rel):
        body = _read(rel)
        assert "collapse{{ '' if auto_collapse else ' show' }} release-section-body" in body, (
            "the body must be 'collapse' (shut) only when auto_collapse"
        )

    @pytest.mark.parametrize("rel", COMPONENTS)
    def test_aria_expanded_tracks_the_initial_state(self, rel):
        """`aria-expanded` drives the chevron AND the hint's visibility."""
        body = _read(rel)
        assert "aria-expanded=\"{{ 'false' if auto_collapse else 'true' }}\"" in body

    @pytest.mark.parametrize("rel", COMPONENTS)
    def test_initial_state_is_recorded_for_the_page_script(self, rel):
        """Collapse-all returns to THIS state, not to 'everything shut'."""
        body = _read(rel)
        assert "data-auto-collapsed=\"1\"" in body
        assert "data-library-count=" in body

    @pytest.mark.parametrize("rel", COMPONENTS)
    def test_missing_badge_stays_in_the_header(self, rel):
        """The count must remain visible above the collapsed body."""
        body = _read(rel)
        header = body.split("release-section-body", 1)[0]
        assert "{% if missing %}<span class=\"badge bg-warning text-dark\">" in header, (
            "the 'N missing' badge must stay in the header, above the body"
        )

    @pytest.mark.parametrize("rel", COMPONENTS)
    def test_toggle_still_uses_bootstrap_collapse(self, rel):
        """Manual expansion is the Bootstrap collapse wired by the toggle."""
        body = _read(rel)
        assert "data-bs-toggle=\"collapse\"" in body
        assert 'data-bs-target="#{{ section_id }}-body"' in body

    @pytest.mark.parametrize("rel", COMPONENTS)
    def test_collapsed_header_says_it_can_be_opened(self, rel):
        """A rotated chevron alone reads as a dead header."""
        body = _read(rel)
        assert "release-collapsed-hint" in body


# ---------------------------------------------------------------------------
# Both trees must agree — the rebuilt tree is served first under the cutover
# ---------------------------------------------------------------------------

class TestTreesAgree:
    def test_both_components_exist(self):
        for rel in COMPONENTS:
            assert (REPO_ROOT / rel).is_file(), f"{rel} is missing"

    def test_components_are_intent_identical(self):
        """Both copies must carry the same collapse logic.

        The trees legitimately differ in comments and parked dead markup, so
        compare the LOGIC rather than the bytes.
        """
        markers = [
            "set auto_collapse = library|length == 0",
            "data-auto-collapsed",
            "release-collapsed-hint",
            "collapse{{ '' if auto_collapse else ' show' }} release-section-body",
        ]
        for marker in markers:
            present = [rel for rel in COMPONENTS if marker in _read(rel)]
            assert len(present) == len(COMPONENTS), (
                f"{marker!r} is missing from {set(COMPONENTS) - set(present)}"
            )


# ---------------------------------------------------------------------------
# Rendering — the real macro, through the real app's Jinja environment
# ---------------------------------------------------------------------------

def _render(items: list[dict], artist: str = "Test Artist") -> str:
    """Render the shipped macro via the app so custom filters resolve."""
    app_mod = importlib.import_module("app")
    env = app_mod.app.jinja_env

    async def _go() -> str:
        async with app_mod.app.app_context():
            await app_mod.app.update_template_context({})
            tmpl = env.from_string(
                '{% from "components/_release_section.html" import render_release_section %}'
                "{{ render_release_section('album', 'albums', 'bi-vinyl-fill', 'Albums', items, artist_name) }}"
            )
            return await tmpl.render_async(items=items, artist_name=artist)

    return asyncio.get_event_loop().run_until_complete(_go()) if False else asyncio.run(_go())


def _album(title: str, missing: bool) -> dict:
    return {"title": title, "is_missing": missing, "album": title}


class TestRenderedBehaviour:
    def test_zero_library_items_renders_collapsed(self):
        html = _render([_album("Alpha", True), _album("Beta", True)])
        assert "collapse release-section-body" in html, "body should start shut"
        assert "collapse show release-section-body" not in html
        assert 'aria-expanded="false"' in html

    def test_zero_library_items_still_shows_the_missing_total(self):
        """The information must survive the collapse."""
        html = _render([_album("Alpha", True), _album("Beta", True), _album("Gamma", True)])
        assert re.search(r'bg-warning text-dark">3 missing', html), (
            "the missing total must remain in the header"
        )

    def test_zero_library_items_shows_the_expand_hint(self):
        html = _render([_album("Alpha", True)])
        assert "release-collapsed-hint" in html

    def test_some_library_items_stays_expanded(self):
        html = _render([_album("Alpha", False), _album("Beta", True)])
        assert "collapse show release-section-body" in html, "owned releases must stay visible"
        assert 'aria-expanded="true"' in html
        assert "release-collapsed-hint" not in html

    def test_expand_target_is_still_wired_when_collapsed(self):
        """Manual expansion depends on the toggle -> body id pairing."""
        html = _render([_album("Alpha", True)])
        assert 'data-bs-toggle="collapse"' in html
        assert 'data-bs-target="#albums-body"' in html
        assert 'id="albums-body"' in html

    def test_all_owned_shows_no_missing_badge(self):
        """With nothing missing there is no 'N missing' badge at all.

        Asserted on the BADGE, not the word: the filter group legitimately
        contains a radio named "...-missing" and a "Missing (0)" label.
        """
        html = _render([_album("Alpha", False)])
        header = html.split("release-section-body")[0]
        assert "badge bg-warning" not in header

    def test_section_with_no_items_renders_nothing(self):
        """An empty category must not produce an empty collapsed card."""
        html = _render([])
        assert "release-section" not in html


# ---------------------------------------------------------------------------
# Expand All / Collapse All must be coherent with the new initial state
# ---------------------------------------------------------------------------

def _code_only(source: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", without_block)


class TestExpandAllHandlesSections:
    @pytest.mark.parametrize("rel", RELEASE_SCRIPTS)
    def test_expand_all_reveals_section_bodies(self, rel):
        """Otherwise a collapsed section could never be revealed by the button
        that claims to expand everything."""
        code = _code_only(_read(rel))
        assert "setSectionExpanded" in code
        assert "getOrCreateInstance(body" in code or "release-section-body" in code

    @pytest.mark.parametrize("rel", RELEASE_SCRIPTS)
    def test_collapse_all_returns_to_the_rendered_state(self, rel):
        """Collapse-all must not shut sections the server chose to open."""
        code = _code_only(_read(rel))
        assert "restoreInitialSectionState" in code
        assert "data-auto-collapsed" in code

    @pytest.mark.parametrize("rel", RELEASE_SCRIPTS)
    def test_aria_is_updated_directly_not_via_an_event(self, rel):
        """Collapsing an already-shut section fires no event, which would leave
        the header (chevron + hint) stale."""
        code = _code_only(_read(rel))
        assert 'setAttribute(\'aria-expanded\'' in code

    @pytest.mark.parametrize("rel", RELEASE_SCRIPTS)
    def test_falls_back_without_bootstrap(self, rel):
        """`bootstrap` absent (or JS not loaded) must still toggle the body."""
        code = _code_only(_read(rel))
        assert "classList.toggle('show'" in code


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

class TestHintStyling:
    @pytest.mark.parametrize("rel", STYLESHEETS)
    def test_hint_is_styled_in_both_trees(self, rel):
        css = _read(rel)
        assert ".release-collapsed-hint" in css, (
            f"{rel} has no rule for the hint; it would render as body text"
        )

    @pytest.mark.parametrize("rel", STYLESHEETS)
    def test_hint_hides_once_expanded(self, rel):
        """The hint explains the COLLAPSED state and must not linger."""
        css = _read(rel)
        assert '.release-section-toggle[aria-expanded="true"] .release-collapsed-hint' in css
