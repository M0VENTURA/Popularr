"""An inline handler must be resolvable on ITS OWN page.

REPORT-CLASS BUG

``templates/pages/downloads/queue.html`` carried

    <button onclick="jumpToUpcomingReleases()">

but ``jumpToUpcomingReleases`` was defined ONLY as an inline ``<script>`` inside
``monitor.html``. The button had been copy-pasted along with the shared header
markup, and the function definition was not. Clicking it on ``/downloads/queue``
threw ``ReferenceError`` and did nothing — and it could never have worked there
anyway, because that page has no ``#upcomingReleasesSection`` to scroll to.

The rebuilt tree had already fixed it (and documented why); the LIVE tree still
shipped the dead button. This is the same failure shape as
``_monitor_upcoming.html``'s five handlers, three of which were defined nowhere.

WHAT THIS CHECKS

For each downloads page in BOTH trees, every function named in an inline
``on*="..."`` attribute must be defined somewhere that page actually loads:

  * a ``function`` in its own inline ``<script>``;
  * ``function`` / ``x = function`` / ``global.x =`` / ``window.x =`` in that
    tree's static JS.

Jinja comments are stripped first — a "BUG FIXED: this called
onclick=\\"jumpToUpcomingReleases()\\"" note is documentation, not a handler, and
scanning it makes the checker re-report the bug it just describes.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

TREES = {
    "live": (REPO_ROOT / "templates/pages/downloads", REPO_ROOT / "static/js"),
    "rebuilt": (
        REPO_ROOT / "test_site/templates/Pages/downloads",
        REPO_ROOT / "test_site/static/js",
    ),
}

#: Names that are built-ins or browser APIs, never defined in our JS.
ALLOW = frozenset({
    "event", "this", "window", "document", "console", "alert", "confirm",
    "prompt", "location", "history", "setTimeout", "setInterval",
    "clearTimeout", "clearInterval", "fetch", "encodeURIComponent",
    "decodeURIComponent", "parseInt", "parseFloat", "String", "Number",
    "JSON", "Object", "Array", "Math", "Date", "return", "void",
})


def _js_corpus(directory: pathlib.Path) -> str:
    if not directory.is_dir():
        return ""
    parts = []
    for path in directory.rglob("*.js"):
        parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _pages(tree: str) -> list[pathlib.Path]:
    directory = TREES[tree][0]
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.html"))


@pytest.mark.parametrize("tree", sorted(TREES))
def test_every_inline_handler_resolves_on_its_page(tree: str) -> None:
    corpus = _js_corpus(TREES[tree][1])
    pages = _pages(tree)
    assert pages, f"no downloads pages found for the {tree} tree"

    problems: list[str] = []
    for page in pages:
        raw = page.read_text(encoding="utf-8", errors="replace")
        # ⚠️ Strip Jinja comments: the documented bug is not a live handler.
        scanned = re.sub(r"\{#.*?#\}", "", raw, flags=re.S)
        # Functions this page defines in its own inline <script>.
        inline = set(re.findall(r"function\s+(\w+)\s*\(", scanned))

        for match in re.finditer(r'on(?:click|change|submit)="([^"]+)"', scanned):
            body = match.group(1)
            for name in re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", body):
                if name in ALLOW or name in inline:
                    continue
                if re.search(
                    rf"(function\s+{re.escape(name)}\b)"
                    rf"|(\b{re.escape(name)}\s*=\s*function)"
                    rf"|(global\.{re.escape(name)}\s*=)"
                    rf"|(window\.{re.escape(name)}\s*=)",
                    corpus,
                ):
                    continue
                line = raw[: match.start()].count("\n") + 1
                problems.append(f"{page.name}:{line}  {name}()")

    assert not problems, (
        f"{tree} tree — inline handlers with no definition anywhere:\n  "
        + "\n  ".join(problems)
        + "\n\nA call-pasted button without its function throws ReferenceError "
        "and looks like a dead button. Either define it on this page or replace "
        "the button with a plain link to where the action lives."
    )


class TestTheQueuePageJumpButton:
    """Pins the specific fix so the dead button is not restored."""

    @pytest.mark.parametrize(
        "rel",
        [
            "templates/pages/downloads/queue.html",
            "test_site/templates/Pages/downloads/queue.html",
        ],
    )
    def test_it_is_a_link_not_a_dead_handler(self, rel: str) -> None:
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        live = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
        assert 'onclick="jumpToUpcomingReleases()"' not in live, (
            f"{rel} still calls a function defined only in monitor.html"
        )
        assert 'href="/downloads/discover/upcoming"' in live, (
            "the jump button must be a plain link to the upcoming page"
        )

    def test_the_monitor_still_defines_its_own(self) -> None:
        """CONTROL — the monitor legitimately defines it inline."""
        src = (
            REPO_ROOT / "templates/pages/downloads/monitor.html"
        ).read_text(encoding="utf-8")
        assert "function jumpToUpcomingReleases(" in src
