"""Contract guards for the artist page and its shared release component.

Every check here pins something that FAILED SILENTLY or failed only at render
time, so none of it is caught by a normal import or a syntax check.

The failure modes, all found in the rebuild:

1. **A component that defines no macro.** ``test_site/templates/components/
   _release_section.html`` was a 333-line FULL ARTIST PAGE (``{% extends
   "base.html" %}``, byte-identical to ``templates/pages/artist_detail.html``)
   that defined ``render_release_section`` — never. Importing it raised
   ``ImportError: cannot import name 'render_release_section'``. Because the
   rebuilt tree is a PREFERRED loader, that broken copy also shadowed the
   correct live macro.

2. **An included partial that does not exist.** The rebuilt page includes
   ``components/modals/_artist_modals.html`` (plural) while the tree only had
   ``_artist_modal.html`` (singular), so the page raised ``TemplateNotFound``
   and could not render at all.

3. **Bare endpoint names in ``url_for``.** Blueprints here are namespaced
   (``ui.``, ``scans.``, ``artist.``). A bare name is not a syntax error and not
   an import error — it raises ``BuildError`` only when that page renders. This
   is what killed ``/downloads/monitor`` and what made
   ``Pages/artist_detail.html`` unrenderable.

4. **A stylesheet present in only one tree.** ``versioned_static()`` searches
   the rebuilt tree FIRST under ``features.use_test_site`` and the LIVE tree
   only in the default mode, so rules living in one tree silently vanish in the
   other. The ``.release-*`` rules were rebuilt-tree-only, so release rows
   rendered completely unstyled in live mode.

5. **A page script with no file behind it.** The page loads
   ``js/pages/artist-releases.js``; only ``artist_releases.js`` (underscore)
   existed, so the request 404'd and every release interaction was dead.

6. **An unreachable page name.** The route asks for ``artist_detail_v2.html``.
   The rebuilt tree only had ``artist_detail.html``, so the rebuilt page was
   never served however many times it was edited.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

LIVE_TEMPLATES = REPO_ROOT / "templates"
REBUILT_TEMPLATES = REPO_ROOT / "test_site" / "templates"
LIVE_STATIC = REPO_ROOT / "static"
REBUILT_STATIC = REPO_ROOT / "test_site" / "static"

#: The page the artist route actually renders (routes/ui_routes.py).
ARTIST_PAGE_TEMPLATE = "artist_detail_v2.html"

#: The shared release component, which must be a macro library in BOTH trees.
RELEASE_COMPONENT = "components/_release_section.html"

#: Blueprint names in this app. ``url_for`` must always be qualified with one.
BLUEPRINT_PREFIXES = (
    "ui.",
    "scans.",
    "artist.",
    "album_routes.",
    "track_api.",
    "musicbrainz.",
    "queue_processing.",
    "misc_api.",
    "sk_api.",
    "slskd_api.",
    "preview.",
    "api_v1.",
    "favourites.",
    "upcoming_releases.",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _strip_jinja_comments(body: str) -> str:
    """Remove every INERT region: ``{# … #}`` comments and ``{% raw %}`` blocks.

    Documentation in these files legitimately QUOTES bad ``url_for`` calls,
    ``extends`` and Jinja syntax to explain what was wrong, and a superseded
    template body is parked in a ``{% raw %}`` block. None of that is live
    markup, so scanning it produces false positives — and a guard that cries
    wolf gets deleted.

    ``{% raw %}`` is stripped for the same reason as a comment: Jinja does not
    parse its contents, so it can neither render, ``extends``, nor define a
    macro.

    ORDER IS LOAD-BEARING: comments are removed FIRST. These files' comments
    MENTION the raw tags in prose, so a raw-first pass would treat such a
    mention as an opening delimiter and swallow everything up to the real
    ``{% endraw %}`` — including the live macro this test exists to protect.
    """
    body = re.sub(r"\{#.*?#\}", "", body, flags=re.DOTALL)
    return re.sub(r"\{%-?\s*raw\s*-?%\}.*?\{%-?\s*endraw\s*-?%\}", "", body, flags=re.DOTALL)


def _artist_pages() -> list[Path]:
    """Every artist-detail template that exists, in both trees."""
    candidates = [
        LIVE_TEMPLATES / "pages" / ARTIST_PAGE_TEMPLATE,
        REBUILT_TEMPLATES / "Pages" / ARTIST_PAGE_TEMPLATE,
    ]
    return [p for p in candidates if p.is_file()]


# --------------------------------------------------------------------------
# 1. The shared component must be a macro library, in both trees.
# --------------------------------------------------------------------------


def _release_components() -> list[Path]:
    return [p for p in (
        LIVE_TEMPLATES / RELEASE_COMPONENT,
        REBUILT_TEMPLATES / RELEASE_COMPONENT,
    ) if p.is_file()]


def test_release_component_exists_in_both_trees() -> None:
    """Under the cutover the rebuilt copy is preferred, so both must exist."""
    found = {p.relative_to(REPO_ROOT).as_posix() for p in _release_components()}
    assert "templates/components/_release_section.html" in found
    assert "test_site/templates/components/_release_section.html" in found, (
        "the rebuilt tree has no components/_release_section.html; the cutover "
        "would fall back to the live macro and the two trees could diverge"
    )


@pytest.mark.parametrize("path", _release_components(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_release_component_defines_the_macro_it_is_imported_for(path: Path) -> None:
    """A component imported for ``render_release_section`` must define it.

    The rebuilt copy used to be a whole artist PAGE with no macro at all, which
    made the import fail while looking like a perfectly ordinary template.
    """
    body = _read(path)
    assert "{% macro render_release_section(" in body, (
        f"{path.relative_to(REPO_ROOT)} does not define render_release_section. "
        "Pages import it with {% from ... import render_release_section %}, so a "
        "missing macro is an ImportError at render time, not at load time."
    )
    assert "{% macro render_release_row(" in body, (
        "render_release_section delegates to render_release_row; both must exist"
    )


@pytest.mark.parametrize("path", _release_components(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_release_component_does_not_extend_a_base_template(path: Path) -> None:
    """A macro library must not ``extends`` — that makes it render a page.

    ``{% extends %}`` inside an imported template is legal Jinja and produces
    output, so the defect is invisible until the wrong thing appears on screen.

    Comments are stripped first: these files legitimately QUOTE the superseded
    page (including an ``extends``) to record what went wrong, and prose must
    not be mistaken for live markup.
    """
    body = _strip_jinja_comments(_read(path))
    assert not re.search(r"^\s*\{%\s*extends\s", body, re.MULTILINE), (
        f"{path.relative_to(REPO_ROOT)} extends a base template. An imported "
        "component must be a macro library, not a page; extending means "
        "importing it tries to render a whole layout."
    )


def test_release_component_never_reads_album_title_alone() -> None:
    """``album.title`` is absent on LIBRARY albums, so it is never sufficient.

    routes/ui_routes.py builds ``albums_by_category`` from TWO shapes: owned
    albums carry ``album`` (no ``title``), missing releases carry both. Reading
    ``album.title`` alone rendered an EMPTY name for every owned album — silently,
    because Jinja's default Undefined is falsy rather than an error.
    """
    for path in _release_components():
        body = _strip_jinja_comments(_read(path))
        # Any album.title access must be guarded by an `or album.album` fallback.
        for match in re.finditer(r"album\.title", body):
            window = body[max(0, match.start() - 80): match.start() + 80]
            assert "album.album" in window, (
                f"{path.relative_to(REPO_ROOT)} reads album.title without an "
                "album.album fallback near it:\n    "
                f"{window.strip()[:160]}\n"
                "Owned albums have no `title` key, so this renders blank."
            )


# --------------------------------------------------------------------------
# 2. Every template a page includes must exist in that page's own tree.
# --------------------------------------------------------------------------


def _template_includes(body: str) -> list[str]:
    """Include targets, ignoring commented-out markup."""
    return re.findall(r"\{%-?\s*include\s+[\"']([^\"']+)[\"']", _strip_jinja_comments(body))


@pytest.mark.parametrize("page", _artist_pages(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_included_partials_exist_in_the_same_tree(page: Path) -> None:
    """``{% include %}`` raises TemplateNotFound, and Jinja has no fallback.

    The rebuilt page included ``components/modals/_artist_modals.html`` while
    the tree only had ``_artist_modal.html`` (singular), so the page could not
    render at all. A missing partial is a hard failure with no partial credit,
    which is why this is checked rather than assumed.
    """
    # pages/ or Pages/ -> the tree root that holds base.html
    tree_root = page.parent.parent
    body = _strip_jinja_comments(_read(page))

    missing: list[str] = []
    for target in _template_includes(body):
        # Case-insensitive, matching the app's loader.
        wanted = target.replace("\\", "/").casefold()
        found = any(
            p.relative_to(tree_root).as_posix().casefold() == wanted
            for p in tree_root.rglob("*.html")
        )
        if not found:
            missing.append(target)

    assert not missing, (
        f"{page.relative_to(REPO_ROOT)} includes partial(s) that do not exist in "
        f"{tree_root.relative_to(REPO_ROOT)}: {missing}. Jinja has no fallback "
        "for a missing include, so the page raises TemplateNotFound."
    )


# --------------------------------------------------------------------------
# 3. Every url_for in an artist page must use a namespaced endpoint.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("page", _artist_pages(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_artist_page_url_for_endpoints_are_namespaced(page: Path) -> None:
    """A bare endpoint name raises BuildError only when the page renders.

    This is the failure that took /downloads/monitor down and that made
    ``test_site/templates/Pages/artist_detail.html`` impossible to render.
    """
    body = _strip_jinja_comments(_read(page))
    names = re.findall(r"url_for\(\s*[\"']([A-Za-z0-9_\.]+)[\"']", body)

    assert names, f"{page.relative_to(REPO_ROOT)} has no url_for calls — wrong file?"

    bad = sorted({n for n in names if "." not in n})
    assert not bad, (
        f"{page.relative_to(REPO_ROOT)} calls url_for with UNQUALIFIED endpoint "
        f"name(s): {bad}. Every blueprint here is namespaced (ui., scans., "
        "artist., album_routes.), so a bare name raises "
        "'BuildError: Could not build url for endpoint ...' at render time."
    )

    unknown = sorted({n for n in names if "." in n and not n.startswith(BLUEPRINT_PREFIXES)})
    assert not unknown, (
        f"{page.relative_to(REPO_ROOT)} calls url_for with endpoint(s) whose "
        f"prefix is not a known blueprint: {unknown}. Update BLUEPRINT_PREFIXES "
        "if a new blueprint was added, otherwise this is a typo that will raise "
        "BuildError at render time."
    )


# --------------------------------------------------------------------------
# 4. Both artist pages must exist and load the SAME scripts.
# --------------------------------------------------------------------------


def test_both_trees_ship_the_artist_page_under_the_routed_name() -> None:
    """The route asks for ``pages/artist_detail_v2.html``.

    The rebuilt tree only had ``Pages/artist_detail.html``, so the rebuilt page
    was never served however many times it was edited — a page nobody resolves
    is a page nobody can verify.
    """
    live = LIVE_TEMPLATES / "pages" / ARTIST_PAGE_TEMPLATE
    rebuilt = REBUILT_TEMPLATES / "Pages" / ARTIST_PAGE_TEMPLATE

    assert live.is_file(), f"live artist page missing: {live.relative_to(REPO_ROOT)}"
    assert rebuilt.is_file(), (
        f"the rebuilt tree has no Pages/{ARTIST_PAGE_TEMPLATE}. The route renders "
        f"'pages/{ARTIST_PAGE_TEMPLATE}', so under features.use_test_site the "
        "loader would fall through to the LIVE page and the rebuilt page would "
        "never be served."
    )


@pytest.mark.parametrize("page", _artist_pages(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_artist_page_loads_the_release_module_that_exists(page: Path) -> None:
    """A ``<script src>`` with no file behind it 404s and silently does nothing.

    The page referenced ``js/pages/artist-releases.js`` while only
    ``artist_releases.js`` (underscore) existed, so every release interaction —
    tracklists, filters, import — was dead with no error.
    """
    tree = "test_site/static" if "test_site" in page.as_posix() else "static"
    static_root = REPO_ROOT / tree
    body = _strip_jinja_comments(_read(page))

    refs = re.findall(r"versioned_static\(\s*['\"]js/([^'\"]+)['\"]", body)
    assert refs, f"{page.relative_to(REPO_ROOT)} loads no js/ modules"

    missing = [r for r in refs if not (static_root / "js" / r).is_file()]
    assert not missing, (
        f"{page.relative_to(REPO_ROOT)} loads js/ module(s) missing from "
        f"{tree}/: {missing}. A <script src> that 404s does not stop other "
        "scripts, so the feature is simply dead with no visible error."
    )


@pytest.mark.parametrize("page", _artist_pages(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_artist_page_links_its_stylesheet(page: Path) -> None:
    """The page must load css/artist.css, and NOT carry an inline <style> copy."""
    body = _read(page)
    assert "versioned_static('css/artist.css')" in body, (
        f"{page.relative_to(REPO_ROOT)} does not link css/artist.css, so its "
        "page-scoped styles never load."
    )
    inline = re.findall(r"<style>(.*?)</style>", body, re.DOTALL)
    assert not inline, (
        f"{page.relative_to(REPO_ROOT)} has {len(inline)} inline <style> block(s). "
        "Page styles belong in css/artist.css so they are cached once."
    )


# --------------------------------------------------------------------------
# 5. Release markup classes must be styled in BOTH trees.
# --------------------------------------------------------------------------


def test_release_markup_and_release_css_agree_in_both_trees() -> None:
    """Release CSS and release markup must agree, in BOTH directions, in both trees.

    versioned_static() resolves to whichever tree is active, so a rule living in
    only one copy disappears in the other mode. The ``.release-*`` block was
    rebuilt-tree-only, which left the release rows unstyled in live mode — the
    DEFAULT.

    Two directions are asserted against EACH stylesheet copy:

    1. **Unstyled layout.** Every class in ``LAYOUT_CLASSES`` must have a rule.
    2. **Dead CSS.** A ``.release-*`` rule whose class no template emits is dead
       weight that reads as active — the trap ``test_site/static/css/artist.css``
       fell into for a long time.

    Not every emitted ``release-*`` class needs a rule: these are deliberately
    Bootstrap-styled (``release-edit-btn`` is ``btn btn-outline-secondary``,
    ``release-sub`` is ``text-muted small``). ``LAYOUT_CLASSES`` names only the
    ones this stylesheet is genuinely responsible for, so the first check cannot
    demand pointless duplication of framework utilities.
    """
    #: Classes css/artist.css owns and must define a rule for.
    LAYOUT_CLASSES = {
        "release-section",
        "release-section-header",
        "release-item",
        "release-summary",
        "release-art",
        "release-meta",
        "release-title",
        "release-actions",
        "release-tracklist",
        "release-jump-bar",
        "release-jump-chip",
    }

    sources = _release_components() + _artist_pages()
    emitted: set[str] = set()
    for path in sources:
        body = _strip_jinja_comments(_read(path))
        for group in re.findall(r'class="([^"]+)"', body):
            emitted.update(c for c in group.split() if c.startswith("release-"))

    assert emitted, (
        "no .release-* classes found in any template — the release markup was "
        "removed or renamed, so this guard would prove nothing"
    )

    # The layout list must stay tethered to real markup or it silently stops
    # meaning anything.
    stale = sorted(LAYOUT_CLASSES - emitted)
    assert not stale, (
        f"{stale} are listed in LAYOUT_CLASSES but no template emits them — the "
        "markup was renamed, so update LAYOUT_CLASSES to match."
    )

    for css in (LIVE_STATIC / "css" / "artist.css", REBUILT_STATIC / "css" / "artist.css"):
        assert css.is_file(), f"missing {css.relative_to(REPO_ROOT)}"
        body = _read(css)
        styled = {m.group(1) for m in re.finditer(r"\.(release-[a-z0-9-]+)", body)}

        unstyled = sorted(LAYOUT_CLASSES - styled)
        assert not unstyled, (
            f"{css.relative_to(REPO_ROOT)} has no rule for {unstyled}, which the "
            "release markup emits. This stylesheet is served in one of the two "
            "modes, so the release layout collapses there."
        )

        dead = sorted(styled - emitted)
        assert not dead, (
            f"{css.relative_to(REPO_ROOT)} styles {dead}, which NO template "
            "emits. A release-* rule with no matching markup is dead CSS that "
            "reads as active — delete it, or fix the class name in the markup."
        )


# --------------------------------------------------------------------------
# 6. The two copies of shared modules must not drift.
# --------------------------------------------------------------------------


def _module_body(path: Path) -> str:
    """Executable part of a module: everything after its leading header comment.

    Each copy's header legitimately names ITS OWN path — useful to a reader, and
    different by necessity. What must not differ is the CODE, so the header block
    is dropped before comparing. Requiring the headers to match too would push a
    developer to delete the path, losing the one piece of context a reader needs.
    """
    body = _read(path)
    stripped = body.lstrip()
    if stripped.startswith("/*"):
        end = stripped.find("*/")
        if end != -1:
            return stripped[end + 2:].strip()
    return body.strip()


@pytest.mark.parametrize(
    "live_rel,rebuilt_rel,canary",
    [
        # The two trees lay static/ out differently: the live tree is FLAT
        # (static/js/*.js) while the rebuilt tree is FOLDERED
        # (static/js/pages/*.js). The PAIR is what must stay equivalent, not the
        # relative path.
        (
            "js/artist-releases.js",
            "js/pages/artist-releases.js",
            # Each module publishes a different global, so the "was this file
            # gutted?" canary has to be per-module. A single shared canary would
            # falsely fail every other module.
            "global.artistReleases",
        ),
        # busy-popup is loaded by BOTH trees' base.html, so a divergence would
        # make the queue/lookup progress popup behave differently depending
        # only on the cutover flag.
        ("js/busy-popup.js", "js/ui/busy-popup.js", "global.busyPopup"),
    ],
)
def test_shared_module_copies_do_not_drift(
    live_rel: str, rebuilt_rel: str, canary: str
) -> None:
    """A module served by BOTH trees must be one implementation.

    ``artist-releases.js`` touches no tree-specific global — it feature-detects
    ``toast`` / ``api`` / ``ui.modal`` and falls back to ``showToast`` / ``fetch``
    / ``bootstrap.Modal`` — so a single body serves both trees. Divergence would
    mean the page behaves differently depending only on the cutover flag, which
    is the exact class of bug this suite exists to prevent.
    """
    live = LIVE_STATIC / live_rel
    rebuilt = REBUILT_STATIC / rebuilt_rel
    assert live.is_file(), f"missing {live.relative_to(REPO_ROOT)}"
    assert rebuilt.is_file(), f"missing {rebuilt.relative_to(REPO_ROOT)}"

    live_code = _module_body(live)
    assert canary in live_code, (
        f"{live.relative_to(REPO_ROOT)} does not publish `{canary}` — "
        "either the header-stripping above is wrong or the module was gutted"
    )
    assert live_code == _module_body(rebuilt), (
        f"The code in {live_rel} (live) and {rebuilt_rel} (rebuilt) has drifted. "
        "Both trees serve this module, so any difference is a behaviour change "
        "that appears in only one mode. Port the change to both copies."
    )


def test_release_module_is_not_jinja() -> None:
    """The module is loaded via <script src>, so it must be valid JavaScript.

    The sibling ``static/js/artist_detail.js`` is a 5346-line JINJA TEMPLATE
    loaded the same way; the browser throws SyntaxError on line 1 and discards
    the whole file, which is why nearly every artist-page function was dead.
    Ping the local copy too so this module cannot repeat that mistake.
    """
    for rel in ("js/artist-releases.js", "js/artist-page.js"):
        for root in (LIVE_STATIC, REBUILT_STATIC):
            path = root / rel
            if not path.is_file():
                continue
            body = _read(path)
            # Strip JS comments first: prose about Jinja is legitimate.
            code = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
            code = re.sub(r"^\s*//.*$", "", code, flags=re.MULTILINE)
            tokens = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", code, flags=re.DOTALL)
            assert not tokens, (
                f"{path.relative_to(REPO_ROOT)} contains Jinja syntax in code "
                f"({tokens[0][:50]!r}). It is served as JavaScript, so the "
                "browser will discard the entire file."
            )


# --------------------------------------------------------------------------
# 7. The page-load per-album MusicBrainz probes must be BOUNDED and must stand
#    down while a scan owns the shared MusicBrainz rate budget.
#
#    This is the "starting a scan from the artist page froze the whole server"
#    regression, and it is invisible to every other check here: the markup is
#    valid, the endpoints resolve, the module parses. The damage is purely
#    runtime, so it has to be pinned structurally.
# --------------------------------------------------------------------------

#: How many "N missing" probes may ever be in flight at once.
MAX_MISSING_PROBE_CONCURRENCY = 4


def _release_module_paths() -> list[Path]:
    """The shared release module, in whichever trees have it."""
    return [
        p
        for p in (
            LIVE_STATIC / "js/artist-releases.js",
            REBUILT_STATIC / "js/pages/artist-releases.js",
        )
        if p.is_file()
    ]


@pytest.mark.parametrize("path", _release_module_paths())
def test_missing_track_probes_are_bounded(path: Path) -> None:
    """The page must not fire one unbounded MusicBrainz request per album.

    ``/api/album/missing-tracks`` is a SYNCHRONOUS Quart handler that calls
    MusicBrainz, so Quart runs it in the event loop's DEFAULT executor — the very
    pool ``asyncio.to_thread`` uses for ``routes/ui_routes.py::artist_detail``.
    MusicBrainz is throttled to ~1 req/s by
    ``api_clients/musicbrainz_http.py::_strict_throttle``, which enforces the
    budget by RESERVING a future slot and only then sleeping to it.

    One probe per owned album, fired at page load, therefore:
      1. claimed N slots of the shared budget, pushing a running scan's own
         MusicBrainz calls ~1.2s x N further out (the scan looked stuck), and
      2. held one executor thread per probe while it slept, starving the pool so
         every other request in the worker — including the artist page's own
         render — could not start.

    The artist page is reloaded the instant the scan form is POSTed, which is
    exactly why starting a scan from it froze the whole server. Removing the cap
    reintroduces that bug.
    """
    code = _module_body(path)
    rel = path.relative_to(REPO_ROOT)

    match = re.search(r"var\s+MISSING_PROBE_CONCURRENCY\s*=\s*(\d+)\s*;", code)
    assert match, (
        f"{rel} no longer declares MISSING_PROBE_CONCURRENCY, so its "
        "missing-track probes are unbounded again and can saturate both the "
        "default executor and the shared MusicBrainz rate budget."
    )

    concurrency = int(match.group(1))
    assert 1 <= concurrency <= MAX_MISSING_PROBE_CONCURRENCY, (
        f"{rel} sets the probe concurrency to {concurrency}; at most "
        f"{MAX_MISSING_PROBE_CONCURRENCY} requests may be in flight, because "
        "each one blocks a worker thread while it waits for its MusicBrainz slot."
    )

    assert "i < MISSING_PROBE_CONCURRENCY" in code, (
        f"{rel} declares the concurrency cap but never uses it to prime the "
        "worker pool, so the probes still burst."
    )


@pytest.mark.parametrize("path", _release_module_paths())
def test_missing_track_probes_stand_down_during_a_scan(path: Path) -> None:
    """While a scan runs, the probes must not consume the MB budget at all.

    Bounding them is not enough on its own: a scan issues MusicBrainz calls
    continuously, and every page-load probe still queues ahead of (or beside)
    them. The route publishes ``data-scan-active`` on ``#releases-sections``
    precisely so the page can skip the probes entirely — the scan owns the
    budget and is rewriting this data anyway.

    The gate must be OPT-IN on the attribute value (``=== '1'``): an absent
    attribute has to mean "not scanning", so an older template or a cached copy
    of this script degrades to the bounded path rather than to the storm.
    """
    code = _module_body(path)
    rel = path.relative_to(REPO_ROOT)

    assert "data-scan-active" in code, (
        f"{rel} no longer reads data-scan-active, so it cannot tell that a scan "
        "is running and will probe MusicBrainz during one."
    )
    assert "if (scanIsActive()) return;" in code, (
        f"{rel} reads the scan-active flag but does not bail out before probing."
    )
    assert "=== '1'" in code or '=== "1"' in code, (
        f"{rel} must treat only an explicit \"1\" as 'a scan is running'. If a "
        "missing attribute counts as scanning, the badges never load."
    )


@pytest.mark.parametrize("page", _artist_pages())
def test_artist_page_publishes_the_scan_active_flag(page: Path) -> None:
    """The flag the module reads has to be rendered by the page.

    The module's gate is worthless if no template emits the attribute, and that
    failure is silent: the attribute is simply absent, so the probes keep
    running. Both trees must render it, since either can serve this page.
    """
    body = _strip_jinja_comments(_read(page))
    rel = page.relative_to(REPO_ROOT)

    marker = re.search(r"<div[^>]*id=\"releases-sections\"[^>]*>", body)
    assert marker, (
        f"{rel} no longer renders #releases-sections, which is the module's "
        "initialisation gate."
    )
    assert "data-scan-active=" in marker.group(0), (
        f"{rel} does not render data-scan-active on #releases-sections, so the "
        "page's per-album MusicBrainz probes keep running during a scan."
    )


def test_artist_route_supplies_scan_active() -> None:
    """``_build_artist_detail_payload`` must actually probe and export the flag.

    Checked statically (via ``ast``) rather than by importing the route module:
    this suite is deliberately import-light, and booting the whole app to read
    one dict key would make the guard depend on unrelated startup state.
    """
    import ast

    path = REPO_ROOT / "routes" / "ui_routes.py"
    source = _read(path)
    tree = ast.parse(source)

    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_build_artist_detail_payload"
        ),
        None,
    )
    assert function is not None, (
        "routes/ui_routes.py no longer defines _build_artist_detail_payload, "
        "the synchronous context builder that artist_detail offloads to a thread."
    )

    body = ast.get_source_segment(source, function) or ""
    assert "is_popularity_scan_active" in body, (
        "_build_artist_detail_payload no longer probes for a running scan, so "
        "the page has no way to know its probes would compete with one."
    )
    assert '"scan_active": scan_active' in body, (
        "_build_artist_detail_payload no longer exports scan_active, so the "
        "template cannot render data-scan-active."
    )
    assert "await" not in body and "async def" not in body, (
        "_build_artist_detail_payload must stay SYNCHRONOUS: artist_detail "
        "offloads it with asyncio.to_thread, and a coroutine-returning body "
        "would silently skip the work."
    )
