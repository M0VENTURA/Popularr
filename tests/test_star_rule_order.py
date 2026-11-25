"""The Config page claims an order for the star rules. Keep it true.

The page previously explained ten overlapping ways to reach or lose 5★ without
ever saying which wins. Documenting an order is only useful if it matches the
code, so this pins the sequence inside ``_assign_stars``: each step's marker must
appear, and they must appear in this relative order.

These are the RETURN POINTS of the function, which is what makes the order
well-defined: the first matching step returns and nothing later can override it.
"""

from __future__ import annotations

import inspect
import re

import pytest

from services.popularity.stages import finalise_stage


def _assign_stars_source() -> str:
    return inspect.getsource(finalise_stage._assign_stars)


#: (human name, regex that must be present, why it is ordered there)
ORDERED_STEPS = [
    ("user rating wins", r'if single_confidence == "user":\s*\n\s*track\["_lock_honoured"\] = True\s*\n\s*return 5', "before anything else"),
    ("live albums branch off", r"if is_live_album and not is_compilation:", "returns on its own path"),
    ("global 5-star lock", r'if track\.get\("_global_5star_locked"\) and not popularity_only', "pre-pass lock"),
    ("forced percentile", r"if _force_stars is not None:", "failsafe short-circuit"),
    ("compilations branch off", r"if is_compilation:", "own path, before the base tier"),
    ("base tier", r"base_stars = _album_z_band_star\(", "band or curve"),
    ("album curve replaces base", r"if _curve_star is not None:", "base only"),
    ("5-star promotion", r"if \(\s*\n\s*is_standout\s*\n\s*and organic", "standout + organic"),
    ("dedicated single floor", r"if _dedicated_single_min and base_stars < _dedicated_single_min:", "floor last"),
]


@pytest.mark.parametrize(("name", "pattern", "why"), ORDERED_STEPS)
def test_each_documented_step_exists(name: str, pattern: str, why: str) -> None:
    assert re.search(pattern, _assign_stars_source()), (
        f"the documented step '{name}' ({why}) no longer matches _assign_stars. "
        "The Config page's order list is now wrong — update both."
    )


def test_the_documented_order_is_the_code_order() -> None:
    source = _assign_stars_source()
    positions = []
    for name, pattern, _why in ORDERED_STEPS:
        match = re.search(pattern, source)
        assert match, f"missing step: {name}"
        positions.append((name, match.start()))

    ordered = [p[0] for p in sorted(positions, key=lambda p: p[1])]
    documented = [name for name, _p, _w in ORDERED_STEPS]
    assert ordered == documented, (
        "the star rules now run in a different order than the Config page documents.\n"
        f"  code:       {ordered}\n  documented: {documented}"
    )


def test_caps_run_after_assignment_per_album() -> None:
    """Caps are a property of the ALBUM, so they run after every track is rated."""
    source = inspect.getsource(finalise_stage.post_album_star_ratings)
    assign = source.find("stars = _assign_stars(")
    caps = [
        source.find("_apply_live_album_slot_caps("),
        source.find("_apply_album_five_star_cap("),
        source.find("_apply_short_track_star_cap("),
    ]
    assert assign != -1
    for position in caps:
        assert position != -1
        assert position > assign, "a cap must not run before the tiers are assigned"

    # ...and in the documented order among themselves.
    assert source.find("_apply_live_album_slot_caps(") < source.find("_apply_album_five_star_cap(") < source.find("_apply_short_track_star_cap(")


def test_the_page_documents_the_same_number_of_steps() -> None:
    """One <li> per step a reader sees.

    The page merges "base tier" and "the curve replaces the base" into a single
    list item (they are one decision for a reader: which base applies), so the
    rendered count is the code steps minus that merge.
    """
    import pathlib

    displayed_steps = len(ORDERED_STEPS) - 1  # base tier + curve are one <li>
    for tree in (
        pathlib.Path("test_site/templates/Pages/config.html"),
        pathlib.Path("test_site/templates/Pages/config.html"),
    ):
        text = tree.read_text(encoding="utf-8")
        assert "The order the star rules run in" in text, (
            f"{tree} lost the rule-order block"
        )
        block = text.split("The order the star rules run in", 1)[1].split("</ol>", 1)[0]
        items = block.count("<li")
        assert items == displayed_steps, (
            f"{tree} lists {items} steps, expected {displayed_steps}"
        )
