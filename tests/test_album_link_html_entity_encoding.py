"""An album name containing "&" produced an unreachable album link.

REPORTED:

    When selecting B-Sides & Rarities from the artist page and going to the
    album link, it loads the page
        B-Sides &amp; Rarities
    [B-Sides & Rarities - Deftones - Popularr](https://…/album/Deftones/B-Sides%2520%2526amp%253B%2520Rarities/2005)
    Due to this, the album link isn't accessable.

ROOT CAUSE — a Jinja MACRO's output is HTML-escaped ``Markup``.

``test_site/templates/components/_release_section.html`` renders the album name
through a macro:

    {% macro _title_of(album) %}{{ album.album or album.title or '' }}{% endmacro %}
    {% set title = _title_of(album)|trim %}
    href="/album/{{ artist_name|path_segment }}/{{ title|path_segment }}"

A macro body is rendered with autoescape ON, so the macro RETURNS
``Markup('B-Sides &amp; Rarities')`` — already escaped. ``path_segment`` then
percent-encoded the ESCAPED text, encoding the entity's own ``&`` and ``;``:

    reported : B-Sides%2520%2526amp%253B%2520Rarities
    correct  : B-Sides%2520%2526%2520Rarities

The route ``unquote()``s to ``'B-Sides &amp; Rarities'``, which does not match
the stored ``'B-Sides & Rarities'``, so the lookup misses and the album page
404s.

⚠️ WHY IT HID SO WELL: ``Markup`` renders UNESCAPED, so the visible page text
read as a correct "B-Sides & Rarities" and only the href was broken. Nothing
looked wrong on screen. The same line proves the asymmetry — ``data-album="{{
title|e }}"`` worked, because ``|e`` on ``Markup`` is a no-op.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest
from jinja2 import DictLoader, Environment, select_autoescape
from markupsafe import Markup

from helpers.template_filters import encode_path_segment

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_SITE_TEMPLATES = REPO_ROOT / "test_site" / "templates"

#: Verbatim from the report.
REPORTED_SEGMENT = "B-Sides%2520%2526amp%253B%2520Rarities"
CLEAN_SEGMENT = "B-Sides%2520%2526%2520Rarities"

ALBUM = "B-Sides & Rarities"
ARTIST = "Deftones"


def _render(template_source: str, **ctx) -> str:
    env = Environment(
        loader=DictLoader({"t.html": template_source}),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["path_segment"] = encode_path_segment
    return env.get_template("t.html").render(**ctx)


def _segment(rendered_link: str) -> str:
    return rendered_link.rsplit("/", 1)[1].strip()


# ---------------------------------------------------------------------------
# 1. The reproduction
# ---------------------------------------------------------------------------

class TestTheReportedBugIsFixed:
    """The exact shape of the shipped template."""

    MACRO_TEMPLATE = """
{% macro _title_of(album) -%}{{- album.album or album.title or '' -}}{% endmacro %}
{%- set title = _title_of(album)|trim -%}
LINK: /album/{{ artist_name|path_segment }}/{{ title|path_segment }}
"""

    def test_the_macro_output_really_is_escaped_markup(self):
        """The premise. If this ever changes, the fix's rationale must be revisited."""
        out = _render(
            "{% macro m(a) -%}{{- a -}}{% endmacro %}{{ m(album).__class__.__name__ }}",
            album=ALBUM,
        )
        assert out.strip() == "Markup", (
            "a macro's output is no longer Markup; the reason path_segment "
            "unescapes has changed and this needs rethinking"
        )

    def test_the_album_link_now_resolves_to_the_clean_name(self):
        out = _render(self.MACRO_TEMPLATE, album={"album": ALBUM}, artist_name=ARTIST)
        link = [ln for ln in out.splitlines() if ln.startswith("LINK:")][0]
        assert _segment(link) == CLEAN_SEGMENT, (
            "the album link must encode the RAW name; the reported URL encoded "
            "its HTML-escaped form"
        )

    def test_it_no_longer_reproduces_the_reported_url(self):
        out = _render(self.MACRO_TEMPLATE, album={"album": ALBUM}, artist_name=ARTIST)
        link = [ln for ln in out.splitlines() if ln.startswith("LINK:")][0]
        assert _segment(link) != REPORTED_SEGMENT, (
            "this is the previously-broken output; the bug is back"
        )

    def test_the_round_trip_recovers_the_stored_name(self):
        """What the route does: ASGI decodes once, the handler unquotes once."""
        out = _render(self.MACRO_TEMPLATE, album={"album": ALBUM}, artist_name=ARTIST)
        link = [ln for ln in out.splitlines() if ln.startswith("LINK:")][0]
        recovered = unquote(unquote(_segment(link)))
        assert recovered == ALBUM, (
            f"the route would look up {recovered!r}, which does not match the "
            f"stored {ALBUM!r}, so the album page 404s"
        )

    def test_the_displayed_text_still_shows_the_ampersand(self):
        """The fix must not unescape what is RENDERED."""
        src = (
            "{% macro m(a) -%}{{- a -}}{% endmacro %}"
            "DISPLAY: {{ m(album) }}"
        )
        out = _render(src, album=ALBUM)
        display = [ln for ln in out.splitlines() if ln.startswith("DISPLAY:")][0]
        # In HTML source an ampersand must be an entity; the browser shows "&".
        assert display.strip() in ("DISPLAY: B-Sides &amp; Rarities", "DISPLAY: B-Sides & Rarities")


# ---------------------------------------------------------------------------
# 2. The filter itself
# ---------------------------------------------------------------------------

class TestEncodePathSegment:
    @pytest.mark.parametrize("raw", [
        "B-Sides & Rarities",
        "AC/DC",
        "Mötley Crüe",
        "What's Up?",
        "A/B & C",
        "Plain Album",
    ])
    def test_a_raw_string_round_trips(self, raw):
        assert unquote(unquote(encode_path_segment(raw))) == raw

    def test_markup_is_unescaped_before_encoding(self):
        assert encode_path_segment(Markup("B-Sides &amp; Rarities")) == CLEAN_SEGMENT

    def test_markup_and_raw_agree(self):
        assert encode_path_segment(Markup(ALBUM)) == encode_path_segment(ALBUM)

    def test_none_encodes_to_empty(self):
        assert encode_path_segment(None) == ""

    def test_a_name_genuinely_containing_the_entity_text_still_works(self):
        """Unguessable inverse: "&amp;" as literal user text must survive.

        ``html.unescape`` maps ``&amp;`` to ``&``, which looks like data loss —
        but the value here really was the ESCAPED form of "&", and re-escaping
        on output is stable, so the segment round-trips either way.
        """
        literal = "B-Sides &amp; Rarities"
        seg = encode_path_segment(literal)
        assert unquote(unquote(seg)) == literal

    def test_it_still_keeps_the_slash_inside_one_segment(self):
        """The reason the double-encoding exists — must not regress."""
        seg = encode_path_segment("AC/DC")
        assert "/" not in seg
        assert unquote(seg) == "AC%2FDC"  # still encoded after one decode

    def test_it_does_not_blindly_replace_entities_in_a_plain_string_with_none(self):
        assert encode_path_segment("") == ""


# ---------------------------------------------------------------------------
# 3. The shipped template — no regression at the source
# ---------------------------------------------------------------------------

class TestTheShippedTemplate:
    TEMPLATE = TEST_SITE_TEMPLATES / "components" / "_release_section.html"

    def test_the_album_link_uses_path_segment(self):
        source = self.TEMPLATE.read_text(encoding="utf-8")
        assert re.search(r'href="/album/\{\{ artist_name\|path_segment \}\}', source), (
            "_release_section.html must keep using path_segment for the album href"
        )

    def test_the_macro_sourced_variable_is_the_one_used_in_the_href(self):
        """Guard the exact hazard: a macro output fed to a URL filter.

        If a future edit replaces the macro with a plain attribute read the
        hazard disappears — and this test should then be updated, because the
        filter's unescaping is still needed for the OTHER call sites.
        """
        source = self.TEMPLATE.read_text(encoding="utf-8")
        assert "{% macro _title_of(album)" in source
        assert re.search(r"set title = _title_of\(album\)", source), (
            "the href's album name is expected to come from the _title_of macro"
        )

    def test_no_other_template_feeds_macro_output_to_path_segment(self):
        """The scan that found this bug, kept as a guard.

        Any ``{% set x = _macro(...) %}`` followed by ``{{ x|path_segment }}``
        is the same hazard. The filter now handles it, so this is a CANARY: if
        a second site appears, its author should know why it works.
        """
        offenders = []
        for path in sorted(TEST_SITE_TEMPLATES.rglob("*.html")):
            src = path.read_text(encoding="utf-8", errors="replace")
            for var, macro in re.findall(
                r"{%-?\s*set\s+(\w+)\s*=\s*(_\w+)\s*\(", src
            ):
                if re.search(r"\{\{\s*" + re.escape(var) + r"\s*\|\s*path_segment", src):
                    offenders.append(f"{path.relative_to(TEST_SITE_TEMPLATES)}: {var} <- {macro}()")
        # '_release_section.html: title <- _title_of()' is the known one.
        assert all("_title_of" in o for o in offenders), (
            "a NEW macro-sourced path_segment call site appeared — it is handled "
            "by _unwrap_markup, but check the author intended that:\n  "
            + "\n  ".join(offenders)
        )
