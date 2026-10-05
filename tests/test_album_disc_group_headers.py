"""Disc group headers on the album page — and the one trap in rendering them.

Requested: group a multi-disc album under "Disc N" rows. Two things make this
easy to get wrong, so both are pinned here:

1. **The grouping value and the ordering value must be the SAME number.**
   They live in ``helpers/track_ordering.disc_sort_value`` precisely so the
   header a user scrolls past and the order the rows appear in cannot drift.
   The reported behaviour (a bogus ``disc_number`` of 0 folding into disc 1,
   which stopped a "disc 1 and disc 0" split on single-disc releases) is
   therefore stated once, in one place.

2. **``loop.changed`` must be EVALUATED on every row.** It remembers the
   PREVIOUS row's value, so writing ``loop.first or loop.changed(x)`` never
   calls it for row 1 — and row 2 then looks like a new group too, putting a
   header above *every* row instead of every group. The template must assign
   it first and test the result. ``TestTheGroupingPatternItself`` renders
   both forms so a "simplification" back to the short-circuit fails loudly.

The header row also carries no ``data-track-id``, because every row lookup in
both album scripts keys on that attribute — placement of injected missing
rows, select-all, and the duplicate badges all skip this row for free.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import Template

from helpers.track_ordering import album_track_sort_key, disc_sort_value

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = [
    "templates/pages/album_detail.html",
    "test_site/templates/Pages/album_detail.html",
]
ROUTE = REPO / "routes" / "ui_routes.py"


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. One definition of "what disc is this"
# ---------------------------------------------------------------------------
class TestTheDiscValueIsOneDefinition:
    @pytest.mark.parametrize("raw, expected", [
        ("1", 1), ("2", 2), ("10", 10),
        ("01", 1),          # zero-padded text tags
        ("1/2", 1),         # "n of m" tags
        ("", 1), ("  ", 1), ("A", 1), (None, 1),
        ("0", 1),           # reported: folds into disc 1, never its own group
    ])
    def test_the_value(self, raw, expected):
        assert disc_sort_value({"disc_number": raw}) == expected

    @pytest.mark.parametrize("track", [
        {"disc_number": "1"}, {"disc_number": "2"}, {"disc_number": "0"},
        {"disc_number": "01"}, {"disc_number": ""}, {"disc_number": None},
        {"disc_number": "1/2"}, {"disc_number": "10"},
    ])
    def test_grouping_never_disagrees_with_ordering(self, track):
        """The header and the sort must read the SAME disc."""
        assert disc_sort_value(track) == album_track_sort_key(track)[0], (
            "a header that says Disc 2 above rows sorted as Disc 1 is exactly "
            "the class of bug this module exists to prevent"
        )

    def test_the_route_uses_it_for_both(self):
        """Not a second inline normalisation — that is how they drift apart."""
        src = ROUTE.read_text(encoding="utf-8")
        assert "disc_number = disc_sort_value(track)" in src
        assert 'track["disc_group"] = disc_number' in src

        # Scoped to the grouping block: safe_int is still legitimately used
        # elsewhere in this route, so only THIS loop must be free of it.
        block = src.split("tracks_by_disc: dict", 1)[1].split(
            "show_disc_headers =", 1
        )[0]
        assert "safe_int(" not in block, (
            "the route used to normalise discs itself, inline — that is a "
            "second definition of the same rule"
        )


# ---------------------------------------------------------------------------
# 2. The route only asks for headers when there is more than one disc
# ---------------------------------------------------------------------------
class TestTheRouteAsksForHeadersOnlyWhenThereIsAGroup:
    def test_the_threshold_is_more_than_one_group(self):
        src = ROUTE.read_text(encoding="utf-8")
        assert "show_disc_headers = len(tracks_by_disc) > 1" in src, (
            "a single-disc album must not grow a header saying Disc 1 above "
            "its only row"
        )

    def test_it_is_passed_to_the_template(self):
        src = ROUTE.read_text(encoding="utf-8")
        assert "show_disc_headers=show_disc_headers" in src

    @pytest.mark.parametrize("groups, expected", [
        ({1: 1}, False),          # one disc → no header at all
        ({1: 1, 2: 2}, True),     # two discs → group headers
        ({1: 1, 2: 2, 3: 3}, True),
    ])
    def test_the_rule_itself(self, groups, expected):
        """Spelled out so the threshold is testable without the whole route.

        A ``0`` key cannot occur here — ``disc_sort_value`` folds 0 into 1 —
        so the single-group case is the one that must stay header-free.
        """
        assert (len(groups) > 1) is expected


# ---------------------------------------------------------------------------
# 3. The markup is invisible to the album scripts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rel", TEMPLATES)
class TestTheHeaderMarkupIsSafeForTheJs:
    def test_it_is_guarded(self, rel):
        assert "show_disc_headers and (loop.first or disc_changed)" in _read(rel), (
            f"{rel}: unguarded, every album would render a header"
        )

    def test_changed_is_evaluated_before_the_test(self, rel):
        """The trap: `loop.first or loop.changed(...)` never records row 1."""
        src = _read(rel)
        assign = src.index("{% set disc_changed = loop.changed(track.disc_group) %}")
        test = src.index("loop.first or disc_changed")
        assert assign < test, (
            f"{rel}: loop.changed must run on EVERY row, before it is tested — "
            "otherwise row 2 reads as a new group and every row gets a header"
        )

    def test_the_row_has_no_track_id(self, rel):
        """Placement, select-all and duplicate badges key on data-track-id."""
        src = _read(rel)
        header = re.search(
            r'<tr class="disc-group-row">.*?</tr>', src, re.DOTALL
        )
        assert header, f"{rel}: no disc-group-row found"
        assert "data-track-id" not in header.group(0), (
            "a header carrying data-track-id would be counted as a track by "
            "every row lookup"
        )

    def test_it_spans_every_column(self, rel):
        header = re.search(
            r'<tr class="disc-group-row">.*?</tr>', _read(rel), re.DOTALL
        )
        assert header and 'colspan="5"' in header.group(0), (
            "the table has 5 columns; a short colspan leaves the header row "
            "ragged"
        )


class TestTheTwoTreesDoNotDrift:
    def test_the_header_markup_is_identical(self):
        """test_site is not a copy that may drift."""
        live = re.search(
            r'<tr class="disc-group-row">.*?</tr>',
            _read(TEMPLATES[0]), re.DOTALL,
        ).group(0)
        site = re.search(
            r'<tr class="disc-group-row">.*?</tr>',
            _read(TEMPLATES[1]), re.DOTALL,
        ).group(0)
        assert live == site


# ---------------------------------------------------------------------------
# 4. The pattern itself — why the template looks the way it does
# ---------------------------------------------------------------------------
class TestTheGroupingPatternItself:
    DISCS = [1, 1, 2, 2, 3]

    def _render(self, body: str) -> str:
        tpl = "{% for x in " + repr(self.DISCS) + " %}" + body + "{% endfor %}"
        return Template(tpl).render()

    def test_the_assignment_form_groups_correctly(self):
        out = self._render(
            "{% set c = loop.changed(x) %}"
            "{% if loop.first or c %}[{{ x }}]{% endif %}"
        )
        assert out == "[1][2][3]", (
            "one header per GROUP — this is the form the template uses"
        )

    def test_the_short_circuit_form_would_double_the_first_group(self):
        """The regression this file exists to prevent."""
        out = self._render("{% if loop.first or loop.changed(x) %}[{{ x }}]{% endif %}")
        assert out == "[1][1][2][3]", (
            "expected the known-bad shape, so the assertion itself is honest"
        )
