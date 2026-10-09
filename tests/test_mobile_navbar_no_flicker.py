"""The mobile menu must not fight the navbar-height token (the flicker).

Reported
--------
> When pressing the hamburger menu in mobile view, the bottom section under
> discover keeps flickering

It is a FEEDBACK LOOP, not a layout mistake:

* ``syncNavbarHeight()`` (``static/js/main.js``) sets ``--navbar-height`` from
  ``nav.offsetHeight``, and a ``ResizeObserver`` on the navbar re-runs it;
* ``.navbar-collapse.show`` is *sized from that same token* —
  ``max-height: calc(100dvh - var(--navbar-height) - 0.5rem)``.

So with the menu open the navbar measured INCLUDES the menu:

    menu grows → navbar taller → token grows → max-height shrinks
    → menu clamps → navbar height changes → ResizeObserver fires → …

The token settles on two values and sweeps between them, and everything that
consumes it (``main``'s padding-top, the dashboard's ``min-height``, the sticky
footer) jumps on every sweep. On a 720px-tall phone that is 540px ↔ 282px, i.e.
the menu itself only ever got 172px of its 430px — which is why the visible
symptom is at the BOTTOM of the menu.

The token means "how tall the COLLAPSED bar is": the open menu overlays the
page rather than moving it, and the clamp then measures from the collapsed bar,
so the menu gets the whole remaining viewport.

``tests/js/mobile-navbar-flicker-probe.js`` drives the real function inside a
stub DOM whose layout obeys the same CSS rule, and reports whether the token
stabilises — a structural assertion cannot see a loop.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "mobile-navbar-flicker-probe.js"
LIVE_MAIN = "static/js/main.js"
REBUILT_MAIN = "test_site/static/js/main.js"
LIVE_CSS = REPO_ROOT / "static" / "css" / "popularr.css"
REBUILT_CSS = REPO_ROOT / "test_site" / "static" / "css" / "popularr.css"


def _node_available() -> bool:
    try:
        return subprocess.run(
            ["node", "--version"], capture_output=True, shell=True
        ).returncode == 0
    except Exception:
        return False


needs_node = pytest.mark.skipif(not _node_available(), reason="node is required")


def _run_probe(main_rel: str, viewport: int = 720) -> dict:
    out = subprocess.run(
        ["node", str(PROBE), str(REPO_ROOT / main_rel), str(viewport)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), shell=True,
        encoding="utf-8",
    )
    assert out.returncode == 0, f"probe failed:\n{out.stdout}\n{out.stderr}"
    lines = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")]
    assert lines, f"probe produced no JSON:\n{out.stdout}\n{out.stderr}"
    return json.loads(lines[-1])


# ===========================================================================
# 1. Behaviour: the loop
# ===========================================================================
class TestTheTokenStabilises:
    @needs_node
    @pytest.mark.parametrize("main_rel", [LIVE_MAIN, REBUILT_MAIN])
    def test_pressing_the_hamburger_does_not_make_the_token_oscillate(self, main_rel):
        """THE BUG: the pre-fix function swept between two heights."""
        result = _run_probe(main_rel)
        assert result["stable"], (
            f"{main_rel}: --navbar-height never settles "
            f"(saw {result['unique_tokens']} values: {result['sequence']}) — the "
            "open menu is being measured into the token it is itself clamped by"
        )
        assert result["unique_tokens"] == 1, result

    @needs_node
    @pytest.mark.parametrize("main_rel", [LIVE_MAIN, REBUILT_MAIN])
    def test_the_token_is_the_collapsed_bar(self, main_rel):
        """Not "the navbar with a menu open" — `main` only has to clear the bar."""
        result = _run_probe(main_rel)
        assert result["applied_token"] == result["chrome"], (
            f"{main_rel}: token {result['applied_token']} != collapsed bar "
            f"{result['chrome']}"
        )

    @needs_node
    @pytest.mark.parametrize("main_rel", [LIVE_MAIN, REBUILT_MAIN])
    def test_the_menu_gets_the_rest_of_the_viewport(self, main_rel):
        """CONTROL for the same change: excluding the menu from the token is
        what lets the clamp measure from the bar — the pre-fix code squeezed a
        430px menu into 172px."""
        result = _run_probe(main_rel, viewport=720)
        assert result["menu_height"] >= 400, result

    @needs_node
    @pytest.mark.parametrize("viewport", [560, 720, 900])
    def test_it_holds_at_every_phone_height(self, viewport):
        result = _run_probe(LIVE_MAIN, viewport=viewport)
        assert result["stable"] and result["unique_tokens"] == 1, result


# ===========================================================================
# 2. Structure: what makes it stay fixed
# ===========================================================================
class TestBothTreesCarryTheGuard:
    @pytest.mark.parametrize("main_rel", [LIVE_MAIN, REBUILT_MAIN])
    def test_the_open_collapse_is_excluded(self, main_rel):
        src = (REPO_ROOT / main_rel).read_text(encoding="utf-8")
        assert "navbarCollapseIsOpen" in src, (
            f"{main_rel}: nothing checks whether the menu is open"
        )
        assert "height -= collapse.offsetHeight" in src, (
            f"{main_rel}: the navbar is still measured WITH the open menu"
        )

    @pytest.mark.parametrize("main_rel", [LIVE_MAIN, REBUILT_MAIN])
    def test_both_animation_phases_are_covered(self, main_rel):
        """Bootstrap adds `collapsing` DURING the animation — measuring there is
        what made the sweep visible while the menu opened."""
        src = (REPO_ROOT / main_rel).read_text(encoding="utf-8")
        assert "collapsing" in src
        assert "show" in src

    @pytest.mark.parametrize("main_rel", [LIVE_MAIN, REBUILT_MAIN])
    def test_the_desktop_breakpoint_is_respected(self, main_rel):
        """At lg+ the collapse is INLINE in the row and IS the bar's height —
        subtracting it there would under-measure the navbar."""
        src = (REPO_ROOT / main_rel).read_text(encoding="utf-8")
        assert "991.98px" in src, (
            f"{main_rel}: no breakpoint guard — an inline (desktop) collapse "
            "would be subtracted from the bar's own height"
        )

    @pytest.mark.parametrize("main_rel", [LIVE_MAIN, REBUILT_MAIN])
    def test_the_observer_is_still_attached(self, main_rel):
        """CONTROL: the fix must not 'solve' the loop by never measuring."""
        src = (REPO_ROOT / main_rel).read_text(encoding="utf-8")
        assert "ResizeObserver" in src
        assert "nav.offsetHeight" in src

    def test_both_css_trees_clamp_from_the_token(self):
        for path in (LIVE_CSS, REBUILT_CSS):
            css = path.read_text(encoding="utf-8")
            assert ".navbar-collapse.show { max-height: calc(100dvh - var(--navbar-height" in css, (
                f"{path.name}: the menu is no longer clamped by the collapsed "
                "bar's height — see the note in main.js before changing it"
            )
