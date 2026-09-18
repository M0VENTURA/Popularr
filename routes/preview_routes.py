"""Test-site preview routes.

Serves the rebuilt templates and assets under ``test_site/`` so the ported pages
can be clicked through in a browser **before** the real routes are switched over.

Mounted at ``/test-site`` — nothing here touches the live routes, so the existing
UI keeps working exactly as it does today. The point is to give a side-by-side:
use the app normally, and open ``/test-site`` to exercise the replacement.

The dashboard and config pages both link here, so it is reachable from inside the
app rather than by typing the URL.

Why a blueprint rather than a flag on the existing routes
---------------------------------------------------------
The rebuilt templates are drop-in replacements that read the same context the
live ones do, and they extend ``base.html``, which in the test tree lives at
``test_site/templates/base.html``. Rendering them from the app's normal template
folder is therefore impossible — they would resolve the *old* base.html and the
*old* component partials. A separate blueprint with its own ``template_folder``
keeps the two trees independent right up to the cutover.

The ``versioned_static`` override
---------------------------------
The New templates call ``versioned_static('js/pages/…')``. The app's global
implementation resolves against the live ``static/`` folder, which still holds
the OLD scripts — the extracted modules only exist under ``test_site/static``. Passing
a shadowing ``versioned_static`` in the render context (Jinja resolves context
names before globals) points every asset at this blueprint's own static route, so
the preview loads the new JS/CSS instead of the legacy files.

Docker
------
``Dockerfile`` does ``COPY . /app``, so ``test_site/`` is already in the image. Check
``.dockerignore`` only if a rebuild does not pick the folder up.

Scope
-----
Every ported page is listed by ``/test-site``. Pages that need their live route's
context (album / artist / track detail, config, dashboard, …) are listed with a
reason rather than a link — those are verified after the cutover, or by adding a
context shim here. Previewing them with empty context would fail on the first
unguarded Jinja expression and prove nothing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog
from quart import Blueprint, abort, render_template, request, send_from_directory, url_for

from helpers.config_helpers import get_config
from helpers.logging_config import resolve_log_dir
from services.metadata.correction_service import get_album_tag_inconsistencies

logger = structlog.get_logger(__name__)

#: The test tree. Renamed from ``New/`` when the migration finished — this is a
#: plain directory name, not a git branch or a build artefact.
_TEST_SITE = "test_site"

# routes/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent
PREVIEW_TEMPLATES_DIR = _REPO_ROOT / _TEST_SITE / "templates"
PREVIEW_STATIC_DIR = _REPO_ROOT / _TEST_SITE / "static"

preview_bp = Blueprint(
    "preview",
    __name__,
    url_prefix="/test-site",
    template_folder=str(PREVIEW_TEMPLATES_DIR),
)

#: Pages that render correctly with no server context (their data arrives via
#: the API from their own module), or with the small context built below.
#: slug -> (live route it replaces, short note)
PREVIEWABLE: dict[str, tuple[str, str]] = {
    "analytics": ("/analytics/genres-moods", "All data comes from /api/analytics — no server context needed."),
    "banned_words": ("/downloads/banned-words", "Data loaded client-side from /api/slsk/banned-words."),
    "sandbox": ("/sandbox", "Metrics loaded client-side from /api/sandbox/metrics."),
    "discover": ("/discover", "Recommendations loaded client-side from /api/recommended-playlists."),
    "missing_releases": ("/missing", "Both accordions load from the API."),
    "similar_artists": ("/downloads/discover/similar-artists", "Grid loads from /api/library/artists/similar."),
    "search": ("/search", "Now a thin entry point onto the global search flyout."),
    "logs": ("/logs", "Needs log_dir / log_files, which are built here the same way the live route builds them."),
    "corrections": ("/correcting", "Album tag inconsistencies are computed here via the same service."),
    "queue": ("/downloads", "Needs slskd_config + queue_status_config, supplied below."),
    "monitor": ("/downloads/monitor", "Needs slskd_config + queue_status_config, supplied below."),
    "upcoming": ("/downloads/discover/upcoming", "Needs slskd_config, supplied below."),
    "bookmarks": ("/bookmarks", "Bookmark rows load client-side; the page itself needs no server context."),
    "playlist_import_csv": ("/playlists/import-csv", "Reads the uploaded CSV client-side; no server context needed."),
}

#: Pages that cannot be previewed standalone, and why. Kept explicit so the
#: index is honest rather than silently omitting them.
NEEDS_CONTEXT: dict[str, str] = {
    "dashboard": "Needs nav_users, stats, recent_scans and launch flags from the live dashboard route.",
    "album_detail": "Needs album, album_name, artist_name, album_tracks, stats… from the live album route.",
    "artist_detail": "Needs albums_by_category, stats, top_tracks, genres, appearances… from the live artist route.",
    "artist_list": "Needs artist_groups, total_stats and total_artists from the live artists route.",
    "track_detail": "Needs the full track row plus artwork URL and scan flag lists.",
    "config": "Needs config + config_raw and the sanitised section tree.",
    "setup": "Needs the setup wizard's defaults and PostgreSQL probe state.",
    "metadata_compare": "Needs its comparison payload from the live route.",
    "artist_genres": "Needs artist_name plus the per-source genre breakdown.",
    "artist_corrections": "Needs get_artist_corrections() output for one artist.",
    "help": "Needs the help doc content resolved from disk.",
}


def preview_versioned_static(filename: str) -> str:
    """Asset URL for the preview tree.

    Shadows the app's global ``versioned_static`` for the duration of a preview
    render so the New templates load the NEW scripts and stylesheets rather than
    the legacy ones still sitting in ``static/``.

    No cache-busting query string: this is a development surface, and a stale
    asset is more useful here than a missing one.
    """
    return url_for("preview.preview_static", filename=filename)


@preview_bp.route("/static/<path:filename>")
async def preview_static(filename: str) -> Any:
    """Serve the New tree's static files (js/ and CSS/).

    An explicit route rather than ``Blueprint(static_folder=…)`` so the resulting
    URL does not depend on how the blueprint's prefix and static path are joined.
    """
    if not PREVIEW_STATIC_DIR.is_dir():
        abort(404)
    return await send_from_directory(str(PREVIEW_STATIC_DIR), filename)


def _discover_pages() -> list[str]:
    """Slugs of every renderable page in the rebuilt tree.

    Walks the WHOLE template tree, not just ``Pages/``, because some pages live
    elsewhere — ``auth/setup.html`` and ``Playlists/index.html``. Shared partials
    (``base.html``, ``components/``, ``layouts/``) are excluded since they are
    fragments rather than pages.
    """
    if not PREVIEW_TEMPLATES_DIR.is_dir():
        return []

    skip_dirs = {"components", "layouts", "modals"}

    found: list[str] = []
    for path in sorted(PREVIEW_TEMPLATES_DIR.rglob("*.html")):
        rel = path.relative_to(PREVIEW_TEMPLATES_DIR)
        if any(part.casefold() in skip_dirs for part in rel.parts[:-1]):
            continue
        if path.name.startswith("_"):          # _preview_index.html
            continue
        if path.stem in ("base",) or path.stem in found:
            continue
        found.append(path.stem)
    return found


def _template_path(slug: str) -> Path | None:
    """On-disk template for a slug, searching the WHOLE tree.

    The tree nests pages in several places — ``Pages/downloads/queue.html``,
    ``Playlists/index.html`` and ``auth/setup.html`` — while the slug is just the
    bare stem. Checking ``Pages/<slug>.html`` alone reported every nested page as
    missing, which is how four present pages came to be listed as absent.
    """
    if not PREVIEW_TEMPLATES_DIR.is_dir():
        return None

    # Prefer an exact Pages/ or root match so the common case never depends on
    # directory-walk ordering.
    for direct in (
        PREVIEW_TEMPLATES_DIR / f"Pages/{slug}.html",
        PREVIEW_TEMPLATES_DIR / f"{slug}.html",
    ):
        if direct.is_file():
            return direct

    for candidate in sorted(PREVIEW_TEMPLATES_DIR.rglob(f"{slug}.html")):
        if candidate.is_file():
            return candidate
    return None


def _relative_template(slug: str) -> str | None:
    """Template name relative to the template root, for ``render_template``."""
    path = _template_path(slug)
    if path is None:
        return None
    try:
        return path.relative_to(PREVIEW_TEMPLATES_DIR).as_posix()
    except ValueError:
        return None


def _preview_context(slug: str) -> dict[str, Any]:
    """Context for one previewable page.

    Deliberately small. A page that needs more than this is listed under
    NEEDS_CONTEXT instead of being rendered with holes in it.
    """
    cfg = get_config()
    base: dict[str, Any] = {
        "versioned_static": preview_versioned_static,
        # base.html reads this; empty is fine (the bookmarks dropdown is guarded).
        "custom_bookmark_links": [],
    }

    # The Config page's User Interface card renders cutover state, so it must not
    # be undefined when that page is previewed.
    try:
        from helpers.test_site_mode import status as _test_site_status
        base["test_site"] = _test_site_status()
    except Exception:
        base["test_site"] = {"enabled": False, "available": False, "active": False}

    if slug == "playlist_import_csv":
        # CSV importer: no server data, the page reads the uploaded file.
        pass

    if slug == "logs":
        log_dir = resolve_log_dir()
        log_files: list[dict[str, Any]] = []
        if os.path.isdir(log_dir):
            for name in sorted(os.listdir(log_dir)):
                if not name.endswith(".log"):
                    continue
                full = os.path.join(log_dir, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                log_files.append({"name": name, "path": full, "size": size})
            log_files.sort(key=lambda f: (f["name"] != "unified_scan.log", f["name"]))
        base.update({"log_dir": log_dir, "log_files": log_files})

    elif slug == "corrections":
        # Same pagination shape the live `correcting` route builds.
        per_page = 50
        try:
            inconsistencies = get_album_tag_inconsistencies(artist_filter=None)
        except Exception as exc:  # pragma: no cover - defensive, mirrors the live route
            logger.warning("Preview: inconsistencies unavailable", error=str(exc))
            inconsistencies = []
            base["error"] = str(exc)

        try:
            page = max(1, int(request.args.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        start = (page - 1) * per_page
        base.update({
            "inconsistencies": inconsistencies[start:start + per_page],
            "total": len(inconsistencies),
            "page": page,
            "total_pages": max(1, (len(inconsistencies) + per_page - 1) // per_page),
            "conflict_stats": {"total_pending": 0},
        })

    elif slug in {"queue", "monitor"}:
        base.update({
            "slskd_config": cfg.get("slskd", {}),
            "queue_status_config": cfg.get("queue_status", {}),
        })

    elif slug == "upcoming":
        base["slskd_config"] = cfg.get("slskd", {})

    elif slug == "search":
        base["initial_query"] = request.args.get("q", "").strip()

    return base


@preview_bp.route("/")
async def preview_index() -> Any:
    """Index of the ported pages, with a link per previewable one.

    The lists are FILTERED against what is actually on disk. A slug can be in
    PREVIEWABLE while its template is absent — the four last-ported pages went
    missing during the tree move — and an index that promises a page it cannot
    open is worse than one that omits it, because it looks like the route is
    broken rather than the file. Missing entries are surfaced separately as
    `absent` so the gap is visible instead of silent.
    """
    discovered = _discover_pages()
    unknown = [slug for slug in discovered
               if slug not in PREVIEWABLE and slug not in NEEDS_CONTEXT]

    def _exists(slug: str) -> bool:
        return _template_path(slug) is not None

    previewable = {slug: info for slug, info in PREVIEWABLE.items() if _exists(slug)}
    absent = {
        slug: info for slug, info in {**PREVIEWABLE, **{k: ("", v) for k, v in NEEDS_CONTEXT.items()}}.items()
        if not _exists(slug)
    }

    return await render_template(
        "_preview_index.html",
        # The index extends the rebuilt base.html, so it needs the same asset
        # override the pages get — otherwise the rebuilt shell would pull the old
        # scripts from static/.
        versioned_static=preview_versioned_static,
        custom_bookmark_links=[],
        previewable=previewable,
        needs_context=NEEDS_CONTEXT,
        absent=absent,
        discovered=discovered,
        unknown=unknown,
        preview_templates_dir=str(PREVIEW_TEMPLATES_DIR),
    )


@preview_bp.route("/<slug>")
async def preview_page(slug: str) -> Any:
    """Render one ported page from the New tree."""
    if slug not in PREVIEWABLE:
        if slug in NEEDS_CONTEXT:
            abort(404, description=f"'{slug}' needs its live route's context — see /test-site. {NEEDS_CONTEXT[slug]}")
        abort(404)

    template = _relative_template(slug)
    if template is None:
        # The slug is in PREVIEWABLE but the file is not there — say which, so
        # this is a one-line diagnosis rather than a mystery 500.
        abort(404, description=f"No template found for '{slug}' under {PREVIEW_TEMPLATES_DIR}")

    logger.debug("Preview render", slug=slug, template=template)
    return await render_template(template, **_preview_context(slug))
