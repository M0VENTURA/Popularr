"""Guard: every ``for=`` attribute in a template names a real, usable control.

WHY THIS EXISTS
---------------
``<label for="X">`` is how a screen reader learns a field's name. Two ways it
silently fails:

1. **It points at nothing.** ``for="typo"`` with no ``id="typo"`` anywhere
   leaves the control unnamed -- indistinguishable, to assistive tech, from
   having no label at all. Visually the page looks perfect.
2. **Two labels claim one control.** The accessible name becomes the
   *concatenation* of both labels, so the field is read as
   "Column Order Wikipedia URL". This is worse than no ``for=`` at all,
   because the field gets a wrong name rather than a missing one.

Both were live in this repo. ``test_site/templates/Pages/config.html`` had
``for="newSrcUrl"`` on both the "Column Order" and the "Wikipedia URL" label
(the Column Order one belongs to a ``<div>``). A third rule guards the related
mistake of pointing a label at something that cannot carry an accessible name
(``<div>``, ``<a>``): the association is then silently ignored.

WHAT IS DELIBERATELY **NOT** ENFORCED
-------------------------------------
A label does not need ``for=``:

  * when it **wraps** its control (implicit association), or
  * when it heads a **group** of controls (e.g. a row of checkboxes) -- there
    is no single target, and the right fix there is ``<fieldset><legend>``,
    which is a markup change, not an attribute.

So this guard only holds the invariant that is always true: **if a
``for=`` is present, it must work.**

KNOWN LIMITATION -- read before trusting a pass
-----------------------------------------------
These rules cannot detect a ``for=`` that points at the WRONG control when
that control happens to be labelable. The live tree had exactly this:
``test_site/templates/Pages/config.html`` aimed the "Column Order" label at
``newSrcUrl`` (the Wikipedia URL input) while its own control was a ``<div>``.
Rule 1 and 2 both pass, because ``newSrcUrl`` exists and is an ``<input>``.

It was caught only *indirectly*: adding the correct ``for="newSrcUrl"`` to the
Wikipedia URL label created a second claim on one control, which is what rule
3 reports. A wrong-but-labelable target in isolation remains undetected.

Markup inside ``<script>`` is skipped. Those are JS template literals that
build rows at runtime; they cannot be validated statically, and
``templates/playlists/browse.html`` legitimately contains two *mutually
exclusive* builders that both emit ``nspGroupAll_<id>``.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_ROOTS = (
    REPO_ROOT / "templates",
    REPO_ROOT / "test_site" / "templates",
)

LABEL = re.compile(r"<label\b([^>]*)>", re.IGNORECASE)
FOR_ATTR = re.compile(r'\bfor\s*=\s*"([^"]*)"', re.IGNORECASE)
ID_ATTR = re.compile(r'\bid\s*=\s*"([^"]*)"', re.IGNORECASE)
CONTROL = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.IGNORECASE)
ANY_TAG = re.compile(r"<([a-zA-Z][\w-]*)\b([^>]*)>", re.IGNORECASE)
SCRIPT_BLOCK = re.compile(r"<script\b.*?</script\s*>", re.IGNORECASE | re.DOTALL)
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

#: Elements that can be the target of a label's ``for=``.
LABELABLE = frozenset({"input", "select", "textarea", "button", "meter",
                       "output", "progress"})


def _blank(match: re.Match[str]) -> str:
    """Replace with whitespace of the same length, newlines preserved."""
    return re.sub(r"[^\n]", " ", match.group(0))


def _strip_non_markup(source: str) -> str:
    """Blank out content that is not rendered markup, preserving offsets.

    ``<script>`` bodies are JS, not markup, and are excluded deliberately:
    their labels are built at runtime and two exclusive builders may reuse an
    id. Comments are excluded so that a label *documented* in a comment (this
    repo documents its traps that way) is not treated as live markup.
    """
    source = SCRIPT_BLOCK.sub(_blank, source)
    source = JINJA_COMMENT.sub(_blank, source)
    source = HTML_COMMENT.sub(_blank, source)
    return source


def _template_files() -> list[Path]:
    files: list[Path] = []
    for root in TEMPLATE_ROOTS:
        files.extend(sorted(root.rglob("*.html")))
    return files


def _audit(path: Path) -> tuple[list[str], list[str], list[str]]:
    """Return (missing_target, not_labelable, duplicate_claim) problems."""
    raw = path.read_text(encoding="utf-8")
    markup = _strip_non_markup(raw)
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        # A file outside the repo (the self-test uses tmp_path).
        rel = path.name

    # Which element owns each id.
    owner: dict[str, str] = {}
    for tag in ANY_TAG.finditer(markup):
        id_match = ID_ATTR.search(tag.group(2))
        if id_match:
            owner.setdefault(id_match.group(1), tag.group(1).lower())

    missing: list[str] = []
    not_labelable: list[str] = []
    claims: dict[str, list[int]] = defaultdict(list)

    for label in LABEL.finditer(markup):
        for_match = FOR_ATTR.search(label.group(1))
        if not for_match:
            continue
        target = for_match.group(1)
        line = raw.count("\n", 0, label.start()) + 1
        claims[target].append(line)

        if target not in owner:
            missing.append(
                f'{rel}:{line} for="{target}" names no id in this file'
            )
        elif owner[target] not in LABELABLE:
            not_labelable.append(
                f'{rel}:{line} for="{target}" targets '
                f"<{owner[target]}>, which cannot be labelled"
            )

    duplicates = [
        f'{rel} for="{target}" claimed by labels on lines {lines}'
        for target, lines in claims.items()
        if len(lines) > 1
    ]
    return missing, not_labelable, duplicates


def _collect() -> tuple[list[str], list[str], list[str]]:
    missing: list[str] = []
    not_labelable: list[str] = []
    duplicates: list[str] = []
    for path in _template_files():
        m, n, d = _audit(path)
        missing.extend(m)
        not_labelable.extend(n)
        duplicates.extend(d)
    return missing, not_labelable, duplicates


class TestEveryLabelForAttributeResolves:
    def test_no_label_points_at_a_missing_id(self) -> None:
        missing, _, _ = _collect()
        assert not missing, (
            "A label's for= must name an id in the same file, or the control "
            "is announced with no name at all:\n  " + "\n  ".join(missing)
        )

    def test_no_label_points_at_an_unlabelable_element(self) -> None:
        _, not_labelable, _ = _collect()
        assert not not_labelable, (
            "A for= aimed at a non-labelable element is silently ignored:\n  "
            + "\n  ".join(not_labelable)
        )

    def test_no_control_is_claimed_by_two_labels(self) -> None:
        _, _, duplicates = _collect()
        assert not duplicates, (
            "Two labels on one control CONCATENATE into its accessible name, "
            "so the field is read with a wrong name:\n  "
            + "\n  ".join(duplicates)
        )


class TestNoActionableLabelIsLeftUnassociated:
    """Ratchet: a label that COULD be associated must not be left unassociated.

    "Could be associated" means: it directly precedes a control that already
    carries an ``id``. In that case the association is free -- one attribute --
    and leaving it out is a silent accessibility defect.

    This is deliberately NOT "every label must have ``for=``": a label that
    heads a group of checkboxes has no single target, and forcing one on it
    would be wrong. Wrapping labels are already associated implicitly.
    """

    @staticmethod
    def _actionable() -> list[str]:
        found: list[str] = []
        for path in _template_files():
            raw = path.read_text(encoding="utf-8")
            markup = _strip_non_markup(raw)
            try:
                rel = path.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                rel = path.name
            for label in LABEL.finditer(markup):
                if FOR_ATTR.search(label.group(1)):
                    continue
                close = markup.find("</label>", label.end())
                if close == -1:
                    continue
                inner = markup[label.end():close]
                if CONTROL.search(inner):
                    continue  # wraps its control: already associated
                tail = markup[close + len("</label>"):][:600]
                control = CONTROL.match(tail.lstrip())
                if not control:
                    continue  # heads a group, or nothing follows
                if ID_ATTR.search(control.group(2)):
                    line = raw.count("\n", 0, label.start()) + 1
                    found.append(f"{rel}:{line}")
        return found

    def test_no_label_that_could_be_associated_is_missing_for(self) -> None:
        actionable = self._actionable()
        assert not actionable, (
            "These labels sit directly above a control that already has an id, "
            "so adding for= is free — yet they are unassociated:\n  "
            + "\n  ".join(actionable)
        )

    def test_the_ratchet_can_actually_fail(self, tmp_path: Path) -> None:
        """A ratchet that cannot fail is decoration."""
        bad = tmp_path / "actionable.html"
        bad.write_text('<label>Name</label><input id="n">', encoding="utf-8")
        markup = _strip_non_markup(
            bad.read_text(encoding="utf-8"))
        label = LABEL.search(markup)
        close = markup.find("</label>", label.end())
        tail = markup[close + len("</label>"):][:600]
        control = CONTROL.match(tail.lstrip())
        assert control and ID_ATTR.search(control.group(2)), (
            "the detector must classify this as actionable"
        )

    def test_script_bodies_are_excluded_from_the_scan(self) -> None:
        src = (
            "<label for=\"a\">A</label><input id=\"a\">\n"
            "<script>const html = `<label for=\"b\">B</label>`;</script>\n"
        )
        stripped = _strip_non_markup(src)
        assert 'for="a"' in stripped
        assert 'for="b"' not in stripped

    def test_it_would_catch_a_missing_target(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.html"
        bad.write_text('<label for="nope">X</label><input id="y">',
                       encoding="utf-8")
        missing, _, _ = _audit(bad)
        assert missing, "a for= with no matching id must be reported"

    def test_it_would_catch_a_duplicate_claim(self, tmp_path: Path) -> None:
        bad = tmp_path / "dup.html"
        bad.write_text(
            '<label for="t">One</label><input id="t">'
            '<label for="t">Two</label>',
            encoding="utf-8",
        )
        _, _, duplicates = _audit(bad)
        assert duplicates, "two labels on one control must be reported"

    def test_it_would_catch_an_unlabelable_target(self, tmp_path: Path) -> None:
        bad = tmp_path / "div.html"
        bad.write_text('<label for="c">X</label><div id="c"></div>',
                       encoding="utf-8")
        _, not_labelable, _ = _audit(bad)
        assert not_labelable, "a for= aimed at a <div> must be reported"

    def test_a_wrapping_label_is_not_reported(self, tmp_path: Path) -> None:
        """Implicit association is valid and must not be flagged."""
        ok = tmp_path / "wrap.html"
        ok.write_text('<label>Name <input id="n"></label>', encoding="utf-8")
        missing, not_labelable, duplicates = _audit(ok)
        assert not (missing or not_labelable or duplicates)

    def test_a_group_heading_label_is_not_reported(self, tmp_path: Path) -> None:
        """A label heading a group has no target and is out of scope."""
        ok = tmp_path / "group.html"
        ok.write_text(
            '<label class="fw-bold">Pick some</label>'
            '<div><input type="checkbox" id="a"></div>',
            encoding="utf-8",
        )
        missing, not_labelable, duplicates = _audit(ok)
        assert not (missing or not_labelable or duplicates)
