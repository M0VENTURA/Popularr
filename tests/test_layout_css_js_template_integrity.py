"""Guards for the layout CSS / JS / template audit.

Four failure modes found in the audit that produce NO error, NO warning and NO
failing test — the feature is simply dead or subtly wrong. Each gets a guard
here so the next occurrence fails loudly instead.

1. **Orphaned stylesheet.** ``test_site/static/css/artist.css`` styled the
   artist page for a long time and was referenced by nothing, so none of its
   rules — the iOS focus-zoom guard, the 44px touch target, the mobile table
   layout — ever applied. A file existing is not evidence it is served.

2. **Dead selectors.** That same file styled ``.artist-page tr.album-row``,
   but the artist page renders album rows as ``<div class="album-row">``. The
   selectors could never match, which is also why nobody noticed the file was
   broken. Markup shape is the contract; assert against it.

3. **``data-mobile-cards`` without ``data-label``.** The rule hides ``<thead>``
   at <768px and rebuilds the column name from each cell's ``data-label``.
   Adding the attribute alone therefore *removes* the headers rather than
   restacking the table — a regression dressed as an improvement.

4. **``100vh`` inside a viewport ``calc()``.** On mobile ``vh`` resolves to the
   LARGEST viewport, so a panel sized against it always overflows behind the
   URL bar. ``dvh`` tracks the visible area. ``100vh`` as a plain
   ``min-height`` on a full-page shell is fine and is not flagged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Trees that ship to users. old_system/ is a frozen reference snapshot.
LIVE_TEMPLATES = REPO_ROOT / "templates"
REBUILT_TEMPLATES = REPO_ROOT / "test_site" / "templates"
LIVE_STATIC = REPO_ROOT / "static"
REBUILT_STATIC = REPO_ROOT / "test_site" / "static"

TEMPLATE_ROOTS = (LIVE_TEMPLATES, REBUILT_TEMPLATES)


def _templates() -> list[Path]:
    files: list[Path] = []
    for root in TEMPLATE_ROOTS:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.html")))
    return files


def _stylesheet_dirs() -> list[Path]:
    return [
        d for d in (LIVE_STATIC / "css", REBUILT_STATIC / "css") if d.is_dir()
    ]


# --------------------------------------------------------------------------
# 1. Every stylesheet must be linked by some template.
# --------------------------------------------------------------------------


def _linked_stylesheets() -> set[str]:
    linked: set[str] = set()
    for tpl in _templates():
        body = tpl.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"versioned_static\(\s*['\"]css/([^'\"]+)['\"]", body):
            linked.add(m.group(1))
        for m in re.finditer(r"<link[^>]+href=\"[^\"]*css/([^\"?]+)", body):
            linked.add(m.group(1))
    return linked


def _all_stylesheet_names() -> set[str]:
    names: set[str] = set()
    for d in _stylesheet_dirs():
        names |= {p.name for p in d.glob("*.css")}
    return names


def test_stylesheet_scan_is_not_vacuous() -> None:
    """The link scan must find files, or the orphan guard proves nothing."""
    assert _all_stylesheet_names(), "no stylesheets discovered - paths are wrong"
    assert "artist.css" in _all_stylesheet_names()
    assert _linked_stylesheets(), "no stylesheet links found - the regex is wrong"


# Stylesheets that are KNOWN to be unreferenced, with the reason recorded in
# the file's own header. Keep this list empty if at all possible: an entry is a
# promise that someone is coming back to delete the file.
_KNOWN_ORPHANS = {
    # Its header says outright: "Nothing loads this file. `grep
    # versioned_static('css/search.css')` across the tree returns nothing."
    # It is the leftover styling of a superseded search implementation, kept
    # only as a marker by test_site/templates/Pages/search.html ("Delete both").
    "search.css",
}


@pytest.mark.parametrize(
    "name", sorted(_all_stylesheet_names()), ids=lambda n: n
)
def test_every_stylesheet_is_linked_by_a_template(name: str) -> None:
    """An unreferenced stylesheet is dead weight that reads as active.

    ``test_site/static/css/artist.css`` sat unlinked for a long time while its
    own header claimed ``artist_detail.html`` loaded it. Nothing rendered that
    template, so the file was inert — and because a stylesheet never throws,
    the loss was invisible.
    """
    if name in _KNOWN_ORPHANS:
        pytest.skip(f"{name} is a documented orphan awaiting deletion")

    linked = _linked_stylesheets()
    assert name in linked, (
        f"{name} is not loaded by any template. Either link it (page-scoped "
        "styles belong at the end of {% block content %} or in {% block "
        "scripts %}) or delete it. A stylesheet that nothing loads cannot "
        "affect the page, however specific its rules look."
    )


# --------------------------------------------------------------------------
# 2. The artist page stylesheet must match the artist page's markup.
# --------------------------------------------------------------------------


def _artist_stylesheets() -> list[Path]:
    found: list[Path] = []
    for d in _stylesheet_dirs():
        p = d / "artist.css"
        if p.is_file():
            found.append(p)
    return found


def test_artist_stylesheets_exist_in_both_trees() -> None:
    """Cutover mode reads the rebuilt tree first, so both copies must exist.

    ``versioned_static()`` searches ``test_site/static`` before ``static``
    (helpers/test_site_mode.py::static_roots), so a rule present only in the
    live copy silently disappears under ``features.use_test_site`` — and vice
    versa in live mode, which is the DEFAULT.
    """
    paths = {p.relative_to(REPO_ROOT).as_posix() for p in _artist_stylesheets()}
    assert "static/css/artist.css" in paths, "live artist.css is missing"
    assert "test_site/static/css/artist.css" in paths, (
        "test_site artist.css is missing; the rebuilt tree would serve the live "
        "copy, so the two trees would diverge"
    )


@pytest.mark.parametrize(
    "css_file", _artist_stylesheets(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_artist_css_has_no_dead_album_row_selectors(css_file: Path) -> None:
    """``tr.album-row`` can never match — album rows are ``<div>`` elements.

    This is the defect that made the original file dead on arrival. Asserting
    the element type against the real template is the only way to catch it,
    because a non-matching selector is perfectly valid CSS.
    """
    body = css_file.read_text(encoding="utf-8")
    offenders = re.findall(r"tr\.album-row", body)
    assert not offenders, (
        f"{css_file.relative_to(REPO_ROOT)} styles 'tr.album-row', but "
        "render_release_category emits album rows as "
        '<div class="album-row ...">. Those selectors match nothing. Use '
        "'.artist-page .album-row' or style the div directly."
    )


def test_artist_page_markup_matches_that_assertion() -> None:
    """Pin the markup shape the rule above depends on."""
    tpl = LIVE_TEMPLATES / "pages" / "artist_detail_v2.html"
    body = tpl.read_text(encoding="utf-8")
    assert re.search(r'<div[^>]*class="[^"]*\balbum-row\b', body), (
        "artist_detail_v2.html no longer renders .album-row as a <div>; if the "
        "markup changed to <tr>, update the artist.css guard to match"
    )
    assert not re.search(r"<tr[^>]*\balbum-row", body), (
        "artist_detail_v2.html now uses <tr class=\"album-row\">, so the "
        "tr.album-row prohibition may be obsolete - re-check before relaxing it"
    )


def test_artist_page_links_its_stylesheet_and_has_no_duplicate_inline_block() -> None:
    """The page must load css/artist.css, not carry its own copy of the rules.

    The rules were extracted from a 155-line inline ``<style>`` block. An
    inline block is re-parsed with every HTML response and defeats caching; a
    stylesheet is parsed and cached once.
    """
    tpl = LIVE_TEMPLATES / "pages" / "artist_detail_v2.html"
    body = tpl.read_text(encoding="utf-8")

    assert "versioned_static('css/artist.css')" in body, (
        "artist_detail_v2.html does not link css/artist.css"
    )

    inline = re.findall(r"<style>(.*?)</style>", body, re.DOTALL)
    assert not inline, (
        f"artist_detail_v2.html has {len(inline)} inline <style> block(s) again. "
        "Page-scoped styles belong in css/artist.css so they are cached and "
        "reviewable."
    )


# --------------------------------------------------------------------------
# 3. data-mobile-cards requires data-label on the cells.
# --------------------------------------------------------------------------


def _tables(body: str) -> list[str]:
    return re.findall(r"<table\b.*?</table>", body, re.DOTALL)


def _real_data_cells(table: str) -> list[str]:
    """``<td>`` openings that represent DATA, not a loading/empty placeholder.

    ``<td colspan="7">Loading…</td>`` is an empty state and carries no column
    meaning, so it must not be required to have a ``data-label``. Real data
    cells are the ones that do not span the whole row.
    """
    return [
        cell
        for cell in re.findall(r"<td\b[^>]*>", table)
        if "colspan" not in cell
    ]


@pytest.mark.parametrize(
    "tpl", _templates(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_mobile_cards_tables_label_every_cell(tpl: Path) -> None:
    """``data-mobile-cards`` hides ``<thead>``; the labels must come from cells.

    popularr.css rebuilds each column name from ``td::before { content:
    attr(data-label) }``. A table with the attribute but no labels therefore
    loses its headers entirely below 768px — strictly worse than not opting in.
    """
    body = tpl.read_text(encoding="utf-8")
    for index, table in enumerate(_tables(body), 1):
        if "data-mobile-cards" not in table:
            continue
        thead = re.search(r"<thead\b.*?</thead>", table, re.DOTALL)
        if not thead:
            continue
        header_cells = len(re.findall(r"<th\b", thead.group(0)))
        if header_cells < 2:
            # A single-column or layout-only table has nothing to label.
            continue

        # Rows injected by JS carry their own labels, so an empty tbody is not
        # a template defect — but it must be asserted on the JS side.
        tbody = re.search(r"<tbody\b.*?</tbody>", table, re.DOTALL)
        if tbody and not _real_data_cells(tbody.group(0)):
            continue

        data_cells = _real_data_cells(table)
        if not data_cells:
            continue

        labelled = [c for c in data_cells if "data-label" in c]
        assert labelled, (
            f"{tpl.relative_to(REPO_ROOT)} table {index} is marked "
            f"data-mobile-cards but none of its {len(data_cells)} data <td>s "
            "carry data-label. Below 768px the <thead> is hidden, so every "
            'column header would vanish. Add data-label="..." to each <td>.'
        )


def _js_files() -> list[Path]:
    files: list[Path] = []
    for root in (LIVE_STATIC, REBUILT_STATIC):
        if root.is_dir():
            files.extend(sorted(root.rglob("*.js")))
    return files


def _tbodies_inside_mobile_cards() -> dict[str, str]:
    """{tbody id: template path} for every data-mobile-cards table's tbody.

    These tables have no server-rendered cells, so the template-side guard
    cannot inspect them — the labels must be written by whichever script fills
    the tbody.
    """
    found: dict[str, str] = {}
    for tpl in _templates():
        body = tpl.read_text(encoding="utf-8", errors="replace")
        for table in _tables(body):
            if "data-mobile-cards" not in table:
                continue
            tbody = re.search(r"<tbody\b[^>]*>", table)
            if not tbody:
                continue
            id_match = re.search(r'\bid="([^"]+)"', tbody.group(0))
            if id_match:
                found[id_match.group(1)] = tpl.relative_to(REPO_ROOT).as_posix()
    return found


def test_js_filling_mobile_cards_tables_sets_data_labels() -> None:
    """JS that injects rows into a data-mobile-cards table must label them.

    Some pages (``artist_corrections.html``) render their tables empty and let a
    script fill them. Nothing in the template carries a ``data-label`` for the
    template-side guard to find, so the labels have to come from the JS. Without
    them the mobile view shows values with no column names.

    A script that injects only ``colspan`` sub-rows (a "not in MusicBrainz"
    note spanning the row) is exempt: those are annotations, not columns, and
    the stacked layout already gives them the full width.
    """
    tbodies = _tbodies_inside_mobile_cards()
    assert tbodies, (
        "no JS-filled data-mobile-cards tables found; if the pages changed, "
        "update or remove this guard rather than leaving it vacuous"
    )

    checked = 0
    for js in _js_files():
        body = js.read_text(encoding="utf-8", errors="replace")
        writers = [tid for tid in tbodies if f"'{tid}'" in body or f'"{tid}"' in body]
        if not writers:
            continue

        # Does this script build any REAL (non-colspan) cell for those tables?
        cells = re.findall(r"<td\b[^>]*>", body)
        columnar = [c for c in cells if "colspan" not in c]
        if not columnar:
            continue

        checked += 1
        assert "data-label=" in body, (
            f"{js.relative_to(REPO_ROOT)} writes rows into {writers[0]} — a "
            "tbody inside a data-mobile-cards table — but never sets "
            "data-label. Below 768px that table's <thead> is hidden, so the "
            "rows would render values with no column names."
        )

    assert checked, (
        "no script was found that fills a data-mobile-cards tbody with columnar "
        "rows; the guard did not actually run"
    )


def test_mobile_cards_guard_is_not_vacuous() -> None:
    """At least one table must opt in, or the guard never runs."""
    opted_in: list[str] = []
    for tpl in _templates():
        body = tpl.read_text(encoding="utf-8")
        if "data-mobile-cards" in body:
            opted_in.append(tpl.relative_to(REPO_ROOT).as_posix())
    assert opted_in, "no template uses data-mobile-cards - the guard is vacuous"


# --------------------------------------------------------------------------
# 4. No 100vh inside a viewport calc(); tap targets must be reachable.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tpl", _templates(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_100vh_inside_viewport_calc(tpl: Path) -> None:
    """``calc(... 100vh ...)`` overflows behind mobile browser chrome.

    ``vh`` is the LARGEST viewport height, so a panel sized with it sits under
    the URL bar until the bar hides. ``dvh`` follows the visible area.
    A bare ``min-height: 100vh`` on a page shell is not flagged — that is the
    documented "fill the screen" idiom and has no bottom edge to clip.
    """
    body = tpl.read_text(encoding="utf-8")
    offenders = re.findall(r"calc\([^)]*\b100vh\b[^)]*\)", body)
    assert not offenders, (
        f"{tpl.relative_to(REPO_ROOT)} sizes a box with {offenders[0]!r}. On "
        "mobile 100vh is the largest viewport, so this always overflows behind "
        "the URL bar. Use 100dvh (as popularr.css already does)."
    )


@pytest.mark.parametrize(
    "base",
    (LIVE_TEMPLATES / "base.html", REBUILT_TEMPLATES / "base.html"),
    ids=lambda p: p.relative_to(REPO_ROOT).as_posix(),
)
def test_player_transport_controls_meet_touch_target(base: Path) -> None:
    """Previous / play-pause / next must be at least 44x44 CSS px.

    These are the most-tapped controls in the app and sit in a fixed bottom
    bar, where a miss is most likely. They were 32px while play/pause was 40px.
    """
    body = base.read_text(encoding="utf-8")
    for control in ("playerPrev", "playerPlayPause", "playerNext"):
        match = re.search(
            r'id="' + control + r'"[^>]*style="([^"]*)"', body, re.DOTALL
        )
        assert match, f"{control} has no inline style in {base.name}"
        style = match.group(1)
        width = re.search(r"width:\s*(\d+)px", style)
        height = re.search(r"height:\s*(\d+)px", style)
        assert width and height, f"{control} has no explicit size in {base.name}"
        for label, dim in (("width", int(width.group(1))), ("height", int(height.group(1)))):
            assert dim >= 44, (
                f"{control} in {base.name} is {dim}px {label}, below the 44px "
                "minimum touch target. These are the most-tapped controls in "
                "the app."
            )
