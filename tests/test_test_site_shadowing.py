"""Tests for the rebuilt-tree template shadow list.

``test_site/`` is maintained in an environment that cannot delete files, so a
known-bad rebuilt template is *shadowed* — the loader refuses it and the live
tree serves the real one instead.

The bug this guards: ``test_site/templates/Pages/downloads/monitor.html`` is a
stray artist-page snapshot whose links use BARE endpoint names
(``url_for('dashboard')``).  Every blueprint in this app is namespaced, so
rendering it raised::

    BuildError: Could not build url for endpoint 'dashboard'.
    Did you mean 'ui.dashboard' instead?

on ``GET /downloads/monitor``.

The subtle requirement is that shading must be PER-LOADER.  The cutover wraps
BOTH trees in the same loader class, so a global shadow check would make the
LIVE tree refuse the very file it exists to provide — turning a shadowed
template into a 500 rather than a fallback.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, Environment, TemplateNotFound

from helpers.test_site_mode import CaseInsensitiveFileSystemLoader


def _write(root: Path, relative: str, body: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.fixture
def two_trees(tmp_path: Path) -> tuple[Path, Path]:
    """A rebuilt tree (preferred) and a live tree (fallback)."""
    rebuilt = tmp_path / "test_site" / "templates"
    live = tmp_path / "templates"
    rebuilt.mkdir(parents=True)
    live.mkdir(parents=True)

    _write(rebuilt, "base.html", "REBUILT-BASE")
    _write(live, "base.html", "LIVE-BASE")

    # The page under test: a BAD rebuilt copy and a GOOD live one.
    _write(rebuilt, "Pages/downloads/monitor.html", "BAD-ARTIST-COPY")
    _write(live, "pages/downloads/monitor.html", "GOOD-MONITOR")

    # A page with only a rebuilt copy, to prove the shadow list is narrow.
    _write(rebuilt, "Pages/downloads/queue.html", "REBUILT-QUEUE")

    return rebuilt, live


def _render(rebuilt: Path, live: Path, name: str, shadowed: frozenset[str]) -> str:
    """Render *name* through the same loader arrangement the cutover builds."""
    env = Environment(
        loader=ChoiceLoader([
            CaseInsensitiveFileSystemLoader(str(rebuilt), shadowed=shadowed),
            CaseInsensitiveFileSystemLoader(str(live)),
        ])
    )
    return env.get_template(name).render()


class TestShadowFallsThroughToLive:
    def test_shadowed_template_serves_the_live_version(self, two_trees):
        rebuilt, live = two_trees
        got = _render(
            rebuilt, live, "pages/downloads/monitor.html",
            frozenset({"pages/downloads/monitor.html"}),
        )
        # ⚠️ NOT "BAD-ARTIST-COPY" — that is the 500.
        assert got == "GOOD-MONITOR"

    def test_shadow_applies_to_the_rebuilt_casing_too(self, two_trees):
        """The route asks for lowercase ``pages/…``; the bad file is
        ``Pages/…``.  The case-insensitive hit must still honour the shadow."""
        rebuilt, live = two_trees
        got = _render(
            rebuilt, live, "pages/downloads/monitor.html",
            frozenset({"pages/downloads/monitor.html"}),
        )
        assert got != "BAD-ARTIST-COPY"

    def test_without_the_shadow_the_bad_file_wins(self, two_trees):
        """Documents the failure being fixed: the rebuilt tree is PREFERRED, so
        without the shadow list the stray copy is served (and 500s)."""
        rebuilt, live = two_trees
        got = _render(rebuilt, live, "pages/downloads/monitor.html", frozenset())
        assert got == "BAD-ARTIST-COPY"


class TestShadowIsNarrow:
    def test_other_rebuilt_templates_still_preferred(self, two_trees):
        rebuilt, live = two_trees
        got = _render(
            rebuilt, live, "Pages/downloads/queue.html",
            frozenset({"pages/downloads/monitor.html"}),
        )
        assert got == "REBUILT-QUEUE"

    def test_shared_templates_still_come_from_the_rebuilt_tree(self, two_trees):
        rebuilt, live = two_trees
        got = _render(
            rebuilt, live, "base.html",
            frozenset({"pages/downloads/monitor.html"}),
        )
        assert got == "REBUILT-BASE"


class TestShadowIsPerLoaderNotGlobal:
    """The live loader must be able to serve what the rebuilt loader refuses."""

    def test_live_loader_can_serve_the_shadowed_path(self, tmp_path: Path):
        live = tmp_path / "templates"
        live.mkdir(parents=True)
        _write(live, "pages/downloads/monitor.html", "GOOD-MONITOR")

        # The live tree gets NO shadow list — otherwise the fallback for every
        # shadowed template would itself raise, producing a 500.
        env = Environment(
            loader=CaseInsensitiveFileSystemLoader(str(live), shadowed=frozenset())
        )
        assert env.get_template("pages/downloads/monitor.html").render() == "GOOD-MONITOR"

    def test_a_shadowed_file_missing_from_live_raises_cleanly(self, tmp_path: Path):
        """If the live tree cannot supply the file either, the error must be a
        normal TemplateNotFound, not something misleading."""
        rebuilt = tmp_path / "test_site" / "templates"
        live = tmp_path / "templates"
        rebuilt.mkdir(parents=True)
        live.mkdir(parents=True)
        _write(rebuilt, "Pages/downloads/monitor.html", "BAD")
        # Deliberately no live counterpart.

        with pytest.raises(TemplateNotFound):
            _render(
                rebuilt, live, "pages/downloads/monitor.html",
                frozenset({"pages/downloads/monitor.html"}),
            )


class TestShadowListContents:
    def test_monitor_is_shadowed(self):
        from helpers.test_site_mode import _SHADOWED_TEMPLATES

        assert "pages/downloads/monitor.html" in _SHADOWED_TEMPLATES

    def test_entries_are_lowercase_and_root_relative(self):
        """Lookups are compared casefolded, so entries must be stored that way
        or the comparison silently never matches."""
        from helpers.test_site_mode import _SHADOWED_TEMPLATES

        for entry in _SHADOWED_TEMPLATES:
            assert entry == entry.casefold(), entry
            assert not entry.startswith("/"), entry
            assert "\\" not in entry, entry
