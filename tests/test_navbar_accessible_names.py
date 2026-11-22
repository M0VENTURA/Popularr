"""Desktop navbar links keep an accessible name at EVERY width.

WHY
---
Below ``xl`` each nav item's visible label is ``<span class="d-lg-none
d-xl-inline">``: at ``lg``–``xl`` that span is ``display:none``. CSS-hidden text
is removed from the accessibility tree, and Bootstrap renders no tooltip of its
own — so an icon-only nav link had NO accessible name to a screen reader and no
visible name to a sighted user on hover. This suite pins the fix: every top-level
nav link carries ``title`` + ``aria-label`` matching its label.

It also pins two layout consistencies from the same pass: ``System`` uses the
same breakpoint classes as every other item (it was ``d-none d-xl-inline``, so it
vanished at lg while the rest became icons), and the decorative ``<i>`` icons
inside those links are ``aria-hidden`` so the icon font's private-use glyph is
not announced in place of the real name.
"""

from __future__ import annotations

import re
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

BASES = [
    REPO_ROOT / "test_site" / "templates" / "base.html",
    REPO_ROOT / "templates" / "base.html",
]


def _navbar_nav(base: str) -> str:
    """The whole ``navbarNav`` collapse block.

    ⚠️ NOT ``class="navbar-nav" .. first </ul>`` — the first ``</ul>`` is the
    Artists *dropdown's* inner menu, so that slice loses every later item and
    the assertions pass vacuously on the two entries before it.
    """
    start = base.index('id="navbarNav"')
    return base[start : base.index("</nav>", start)]


def _top_level_links(nav: str) -> list[str]:
    """Every ``<a class="nav-link ...">…</a>`` in the bar (not dropdown items)."""
    return re.findall(r'<a class="nav-link[^"]*"[^>]*>.*?</a>', nav, re.S)


@pytest.fixture(scope="module", params=BASES, ids=lambda p: p.parts[-3])
def base(request) -> str:
    return request.param.read_text(encoding="utf-8")


class TestEveryNavLinkHasAnAccessibleName:
    def test_every_top_level_link_has_title_and_aria_label(self, base: str) -> None:
        nav = _navbar_nav(base)
        links = _top_level_links(nav)
        assert len(links) >= 6, f"only found {len(links)} nav links — slice is wrong"

        missing = []
        for tag in links:
            if "title=" not in tag or "aria-label=" not in tag:
                name = re.sub(r"\s+", " ", tag)[:80]
                missing.append(name)
        assert not missing, (
            f"{len(missing)} top-level nav link(s) lack an accessible name "
            f"(title + aria-label needed because the span is display:none "
            f"below xl): {missing}"
        )

    def test_the_label_and_the_accessible_name_agree(self, base: str) -> None:
        """A hover tooltip that says something different from the link is a bug."""
        nav = _navbar_nav(base)
        pairs = re.findall(r'title="([^"]+)"\s+aria-label="([^"]+)"', nav)
        assert pairs, "no title/aria-label pair found — the scan is wrong"
        mismatched = [(t, a) for t, a in pairs if t != a]
        assert not mismatched, f"title and aria-label disagree: {mismatched}"


class TestSystemUsesTheSameBreakpoint:
    def test_system_label_hides_at_lg_like_the_rest(self, base: str) -> None:
        """It was `d-none d-xl-inline`: it vanished at lg while the others were icons.

        Two different "hidden below xl" rules in one bar meant the item
        disappeared rather than shrinking, so the row reflowed as the viewport
        crossed lg.

        Only the served tree has the consolidated System dropdown; the live
        fallback still renders Logs/Help/Config as separate icon links (each
        `d-none d-lg-block`, i.e. always icons at lg+), so there is no label to
        reflow and nothing to assert there.
        """
        nav = _navbar_nav(base)
        if 'id="systemDropdown"' not in nav:
            pytest.skip("this tree has no consolidated System dropdown")
        idx = nav.index('id="systemDropdown"')
        block = nav[idx : nav.index("</a>", idx)]
        assert "d-lg-none d-xl-inline" in block, (
            "System must use the same responsive classes as every other item"
        )
        assert "d-none d-xl-inline" not in block


class TestDecorativeIconsAreHidden:
    def test_nav_link_icons_are_aria_hidden(self, base: str) -> None:
        """bootstrap-icons renders a private-use glyph; declared aria-hidden it
        is not announced in place of the real label."""
        nav = _navbar_nav(base)
        offenders = []
        for tag in _top_level_links(nav):
            for icon in re.findall(r'<i class="bi [^"]*"(?!\s+aria-hidden)[^>]*>', tag):
                offenders.append(icon)
        assert not offenders, (
            f"{len(offenders)} nav-link icon(s) are not aria-hidden: {offenders[:3]}"
        )
