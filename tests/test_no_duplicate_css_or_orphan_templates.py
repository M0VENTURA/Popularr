"""No stylesheet may define the same rule twice, and no component may be orphaned.

WHY THIS EXISTS

``test_site/static/css/artist.css`` carried the ENTIRE ``.release-*`` block
TWICE — 63 identical rule lines — plus a duplicated ``.release-filter-radio``
rule and ``@media (max-width: 576px)`` block. It was a paste-instead-of-update
mistake: the same rules were added to the LIVE tree and then pasted into the
rebuilt one on top of the copy already there.

Nothing caught it. ``test_artist_stylesheets_exist_in_both_trees`` checks that
both files exist; ``test_release_markup_and_release_css_agree_in_both_trees``
checks that emitted classes have rules and that no rule is dead. Neither looks
at DUPLICATION, so a stylesheet could double in size and stay green.

Duplicated rules are not merely noise: two copies drift, and which one wins is
decided by source order rather than intent — exactly the "two competing
definitions of the same thing" failure this codebase keeps having to unpick.

The orphan check covers the same class of rot from the other side: a component
removed from its page but left on disk reads as live code, and
``_monitor_upcoming.html`` was still shipping five inline ``onclick`` handlers,
three of which no longer existed anywhere.
"""

from __future__ import annotations

import pathlib
import re
from collections import Counter

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _stylesheets() -> list[pathlib.Path]:
    return sorted(
        p for p in REPO_ROOT.rglob("*.css")
        if "old_system" not in str(p) and "node_modules" not in str(p)
        and ".git" not in str(p)
    )


def _rules(path: pathlib.Path) -> list[tuple[str, str]]:
    """(selector, body) pairs with comments and whitespace normalised.

    Comments are stripped FIRST so a rule documented twice does not read as a
    duplicate, and bodies are whitespace-collapsed so a reformatted copy still
    matches its original.

    ⚠️ ``@keyframes`` STEPS ARE NOT RULES. ``50% { … }`` appears once per
    keyframes block, and several animations legitimately share that step, so a
    naive scan reports ``2x 50%`` as a duplicate rule. At-rules are therefore
    SKIPPED entirely — a duplicated ``@media`` block is caught by the SELECTORS
    inside it, which is what actually matters.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        sel = " ".join(selector.split())
        # Skip keyframe steps ("50%", "from", "to") and any at-rule header.
        if sel.startswith("@"):
            continue
        if re.fullmatch(r"(?:from|to|[\d.]+%)", sel):
            continue
        out.append((sel, " ".join(body.split())))
    return out


class TestNoStylesheetDefinesTheSameRuleTwice:
    def test_the_stylesheets_are_found(self) -> None:
        assert len(_stylesheets()) > 3, "the stylesheet scan found nothing"

    @pytest.mark.parametrize(
        "path", _stylesheets(), ids=lambda p: p.name + "@" + p.parent.name
    )
    def test_no_exact_duplicate_rule(self, path: pathlib.Path) -> None:
        rules = _rules(path)
        if not rules:
            pytest.skip(f"{path.name} has no parsed rules")

        counts = Counter(rules)
        dupes = {rule: n for rule, n in counts.items() if n > 1}
        if not dupes:
            return

        detail = "\n".join(
            f"  {n}x  {sel}\n       {body[:90]}"
            for (sel, body), n in list(dupes.items())[:8]
        )
        pytest.fail(
            f"{path.relative_to(REPO_ROOT)} defines {len(dupes)} rule(s) more than "
            f"once:\n{detail}\n\n"
            "Two identical blocks do not conflict yet, but they DRIFT — and which "
            "one wins is decided by source order. Delete the redundant copy."
        )

    def test_a_media_query_override_is_not_reported(self) -> None:
        """CONTROL — same selector, different body, is legitimate.

        A mobile override redefines a desktop rule on purpose; that is not a
        duplicate. Only an IDENTICAL selector+body pair is flagged.
        """
        path = REPO_ROOT / "test_site/static/css/artist.css"
        rules = _rules(path)
        by_selector: dict[str, set[str]] = {}
        for sel, body in rules:
            by_selector.setdefault(sel, set()).add(body)
        multi = {s: b for s, b in by_selector.items() if len(b) > 1}
        # `.release-summary` is a desktop rule plus a 576px override — different
        # bodies, both wanted.
        assert ".release-summary" in multi, (
            "expected a media-query override for .release-summary; if this "
            "changed, the control no longer proves what it claims"
        )


class TestNoTemplateIsOrphaned:
    """A component on disk that nothing includes is not code — it is residue."""

    def _components(self) -> list[pathlib.Path]:
        out: list[pathlib.Path] = []
        for templates in (REPO_ROOT / "test_site" / "templates", REPO_ROOT / "test_site/templates"):
            if templates.is_dir():
                out.extend(templates.rglob("*.html"))
        return out

    @staticmethod
    def _referencing_sources() -> str:
        """Every file that could name a component.

        ⚠️ PYTHON TOO, NOT JUST HTML. Four of these partials are included from
        Python (``ui_routes.py`` renders ``_hero``, ``release_categories.py``
        renders ``_release_section``, …), so an HTML-only scan reports working
        components as orphans and the check cries wolf.
        """
        parts: list[str] = []
        for pattern in ("*.html", "*.py"):
            for path in REPO_ROOT.rglob(pattern):
                rel = str(path)
                if any(skip in rel for skip in ("old_system", "node_modules", ".git", "__pycache__")):
                    continue
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)

    def test_the_template_scan_finds_files(self) -> None:
        assert len(self._components()) > 20

    def test_every_component_is_included_somewhere(self) -> None:
        """A ``_partial.html`` must be referenced by at least one template.

        A partial can be named three ways and ALL must count:

          1. Jinja ``{% include 'components/x.html' %}`` / ``extends``;
          2. a literal filename in a Python ``render_template`` call;
          3. bare in a Python string that a render helper later resolves — e.g.
             ``queue_admin.py`` composes the track-match modal's path, so an
             ``include``-only scan called a working component an orphan.
        """
        corpus = self._referencing_sources()

        orphans = []
        for path in self._components():
            name = path.name
            # Only the underscore-prefixed partial convention.
            if not name.startswith("_"):
                continue
            stem = name[:-5]  # drop .html
            if stem in corpus:
                continue
            orphans.append(path.relative_to(REPO_ROOT))

        assert not orphans, (
            "these components are not included by any template:\n  "
            + "\n  ".join(str(o) for o in orphans)
            + "\n\nDelete them, or wire them up. A dead partial reads as live "
            "code and its inline handlers rot silently."
        )

    def test_the_component_that_was_removed_stays_removed(self) -> None:
        """Pins the specific cleanup so it is not accidentally restored.

        ``_monitor_upcoming.html`` was dropped from the monitor page in
        4c3c6746 but left on disk, carrying five inline onclick handlers — three
        of which (scrapeUpcomingReleasesMonitor, autoMatchAllUpcomingReleasesMonitor,
        clearUpcomingReleasesMonitor) were defined nowhere.
        """
        assert not (
            REPO_ROOT / "test_site/templates/components/search/_monitor_upcoming.html"
        ).exists()
