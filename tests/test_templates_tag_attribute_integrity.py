"""Guard: no template may emit ELEMENT MARKUP inside an HTML opening tag.

The bug this guards - reported as "This is sometimes showing on the top of the
album page":

    Hardwired... to Self-Destruct
    class="d-flex align-items-start gap-3 mb-3">

A `{# comment #}` and a `{% if %}` block had been spliced INTO the hero div's
opening tag in ``test_site/templates/Pages/album_detail.html``:

    <div{# Edition tagline: ... #}
        {% if _show_rel_title or _show_rel_year %}
          <div class="... album-hero-release ...">...</div>
        {% endif %}
         class="d-flex align-items-start gap-3 mb-3">

When the condition was TRUE the tagline markup was emitted inside the tag, so
the tag never parsed and the pending ``class=`` attribute spilled onto the page
as literal text. That is why the report says "sometimes": when the condition
was FALSE nothing was emitted, the ``>`` closed the tag normally, and the page
looked correct.

LEGAL and explicitly allowed:

    <div{% if x %} class="a"{% endif %}>     conditional ATTRIBUTE
    <tr{% if q %} class="x"{% endif %}>      conditional ATTRIBUTE
    <div class="{% if x %}a{% endif %}">     Jinja inside an attribute value

Only a nested ELEMENT inside the tag is a defect. This rule was verified against
the real templates: exactly one violation before the fix, zero after.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Trees that ship to users. old_system/ is a frozen reference snapshot and is
# deliberately excluded.
TEMPLATE_ROOTS = (REPO_ROOT / "templates", REPO_ROOT / "test_site" / "templates")

# A tag whose name is followed only by whitespace and then Jinja: no attribute
# has been emitted yet, so any element here sits inside the opening tag.
_TAG_THEN_JINJA = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)[ \t]*(\{[%#])")

# A nested element opening: `<tag` followed by whitespace, '>' or '/'.
# Requiring one of those excludes an `<html'` fragment inside a JavaScript
# string literal — a false positive a looser rule hits in artist_detail.html.
_NESTED_ELEMENT = re.compile(r"<[a-zA-Z][a-zA-Z0-9-]*[ \t\r\n>/]")

# Cap the region so a stray '<' cannot make the scan walk the whole file.
_MAX_REGION = 4000


def find_violations(src: str) -> list[tuple[int, str, str]]:
    """Return ``(line, tag_name, snippet)`` for each tag containing markup."""
    violations: list[tuple[int, str, str]] = []

    for match in _TAG_THEN_JINJA.finditer(src):
        tag = match.group(1)
        start = match.end(2)

        end = src.find(">", start)
        if end == -1:
            continue

        region = src[start:min(end + 1, start + _MAX_REGION)]
        if _NESTED_ELEMENT.search(region):
            lineno = src.count("\n", 0, match.start()) + 1
            snippet = region.strip().splitlines()[0][:100]
            violations.append((lineno, tag, snippet))

    return violations


def _template_files() -> list[Path]:
    files: list[Path] = []
    for root in TEMPLATE_ROOTS:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.html")))
    return files


def test_templates_are_discovered() -> None:
    """The scan must find templates, or the guard is vacuous."""
    files = _template_files()
    assert files, "no templates discovered - TEMPLATE_ROOTS is wrong"
    assert "album_detail.html" in {f.name for f in files}


@pytest.mark.parametrize(
    "template", _template_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_no_element_markup_inside_a_tag(template: Path) -> None:
    """No tag may contain nested element markup before its closing '>'."""
    src = template.read_text(encoding="utf-8")
    violations = find_violations(src)

    assert not violations, (
        f"{template.relative_to(REPO_ROOT)} emits element markup inside an HTML "
        "opening tag. When the rendering condition is true the tag will not "
        "parse and the following attribute renders as visible page text:\n  "
        + "\n  ".join(
            f"line {lineno}: <{tag}>  {snippet}"
            for lineno, tag, snippet in violations
        )
    )


def test_album_detail_hero_tag_is_well_formed() -> None:
    """Pins the reported regression with context, not just the general rule."""
    checked = 0
    for path in (
        REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html",
        REPO_ROOT / "templates" / "pages" / "album_detail.html",
    ):
        if not path.is_file():
            continue
        checked += 1
        body = path.read_text(encoding="utf-8")

        assert '<div class="d-flex align-items-start gap-3 mb-3">' in body, (
            f"{path.relative_to(REPO_ROOT)}: the album hero div's opening tag "
            "is missing or malformed."
        )
        assert re.search(r'^\s*class="d-flex align-items-start', body, re.M) is None, (
            f"{path.relative_to(REPO_ROOT)}: a bare `class=` line means the hero "
            "tag did not parse - the attribute will render as page text."
        )
        assert "album-hero-release" in body, "the edition tagline markup was lost"

    assert checked > 0, "no album_detail template found to check"


def test_detector_flags_the_reported_shape() -> None:
    """Self-test: the detector must flag the real defect and pass legal markup.

    Without this, a future refactor could hollow the detector out (e.g. drop
    the nested-element requirement) and every parametrised case above would
    still pass while protecting nothing.
    """
    broken = (
        "      <div{# Edition tagline: ... #}\n"
        "          {% if _show_rel_title or _show_rel_year %}\n"
        '            <div class="small text-muted album-hero-release mb-1">x</div>\n'
        "          {% endif %}\n"
        '           class="d-flex align-items-start gap-3 mb-3">\n'
    )
    assert find_violations(broken), "detector failed to flag the reported defect"

    legal = [
        # Conditional ATTRIBUTE - the common, correct idiom.
        '<tr{% if track_is_queued %} class="table-secondary opacity-75"{% endif %}>',
        # Jinja inside an attribute VALUE.
        "<div class=\"metro-tile {{ 'disabled' if not has_artists else '' }}\">",
        # Jinja block emitting an attribute across lines.
        '<input class="form-check-input" type="checkbox"\n'
        "  {% if enabled %}checked{% endif %}\n"
        '  onchange="toggle(this.checked)">',
        # `<html' inside a JavaScript string literal must not be a nested tag.
        "if (raw.trim().startsWith('<html')) { throw new Error('nope'); }",
    ]
    for case in legal:
        assert not find_violations(case), f"false positive on legal markup: {case!r}"
