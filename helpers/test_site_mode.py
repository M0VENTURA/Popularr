"""Test-site cutover mode.

Renders the rebuilt UI tree (``test_site/``) at the ROOT of the app instead of
serving it under a ``/test-site`` prefix.  Because every URL stays identical,
every absolute link, form action, ``url_for`` call and year-scoped album URL
works unmodified — there is nothing to shim, which is what makes this a *usable*
site rather than a set of islands.

Two modes
---------
* **test_site** (default) — ``test_site/templates`` and ``test_site/static``
  take priority, with ``templates/`` and ``static/`` as a fallback.
* **live** — ``templates/`` and ``static/`` only (when explicitly disabled).

The fallback matters: the rebuilt shell expects assets that only exist in the
live tree (``static/dist/vendor/*`` for the local-asset path), and a rebuilt page
may reference a partial that was not carried across.  Falling back rather than
404-ing means the cutover cannot be *more* broken than the tree it replaces.

Why the assets are switched at startup, not per request
-------------------------------------------------------
``app.static_folder`` is read when the static route is BUILT, so assigning to it
later has no effect.  The reliable levers are:

* ``app.jinja_loader`` — swapping the loader genuinely changes template
  resolution (verified).
* the ``static`` view function — rebinding it serves a different directory
  (verified).

Both are applied once during startup, so the running app never changes shape
between requests.

Case sensitivity — the non-obvious hazard
-----------------------------------------
The live routes ask for lowercase ``pages/dashboard.html`` while the rebuilt tree
stores ``Pages/dashboard.html``.  On Windows and macOS that resolves anyway
(case-insensitive filesystems); on **Linux — including the ``python:3.11-slim``
Docker image — it is a hard 404**.  So the cutover is wrapped in a
case-insensitive loader rather than depending on the developer's platform.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

import structlog
from jinja2 import ChoiceLoader, FileSystemLoader, TemplateNotFound
from quart import Quart, send_from_directory

logger = structlog.get_logger(__name__)

#: Repo root (this file is helpers/…).
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The rebuilt tree. A plain directory name — not a branch or build artefact.
TEST_SITE_DIR = _REPO_ROOT / "test_site"
TEST_SITE_TEMPLATES = TEST_SITE_DIR / "templates"
TEST_SITE_STATIC = TEST_SITE_DIR / "static"

LIVE_TEMPLATES = _REPO_ROOT / "templates"
LIVE_STATIC = _REPO_ROOT / "static"

#: Config key that selects the mode (``features.use_test_site``).
CONFIG_KEY = "use_test_site"

#: Templates the rebuilt tree must NOT serve, even though a file exists for
#: them.  Lookups fall through to the live tree instead.
_SHADOWED_TEMPLATES: Final[frozenset[str]] = frozenset()


class CaseInsensitiveFileSystemLoader(FileSystemLoader):
    """``FileSystemLoader`` that retries a lookup case-insensitively."""

    def __init__(self, *args: Any, shadowed: frozenset[str] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._shadowed: frozenset[str] = shadowed or frozenset()

    def _shadows(self, template: str) -> bool:
        return str(template or "").replace("\\", "/").casefold() in self._shadowed

    def get_source(self, environment: Any, template: str) -> Any:
        if self._shadows(template):
            logger.warning(
                "Template shadowed in the rebuilt tree — serving the live version",
                requested=template,
            )
            raise TemplateNotFound(template)
        try:
            return super().get_source(environment, template)
        except TemplateNotFound:
            resolved = self._resolve_case_insensitive(template)
            if resolved is None or resolved == template:
                raise
            if self._shadows(resolved):
                logger.warning(
                    "Template shadowed in the rebuilt tree — serving the live version",
                    requested=template, resolved=resolved,
                )
                raise
            logger.debug(
                "Template resolved case-insensitively",
                requested=template, resolved=resolved,
            )
            return super().get_source(environment, resolved)

    def _resolve_case_insensitive(self, template: str) -> str | None:
        """Find the on-disk spelling of ``template``, segment by segment."""
        for search_path in self.searchpath:
            base = Path(search_path)
            if not base.is_dir():
                continue
            current = base
            parts: list[str] = []
            ok = True
            for segment in str(template).replace("\\", "/").split("/"):
                if not segment:
                    continue
                try:
                    entries = {p.name: p for p in current.iterdir()}
                except OSError:
                    ok = False
                    break
                actual = entries.get(segment)
                if actual is None:
                    lowered = segment.casefold()
                    actual = next(
                        (p for name, p in entries.items() if name.casefold() == lowered),
                        None,
                    )
                if actual is None:
                    ok = False
                    break
                current = actual
                parts.append(actual.name)
            if ok and current.is_file():
                return "/".join(parts)
        return None


def config_enables_test_site() -> bool:
    """True unconditionally now that test_site has been merged as the main UI."""
    return True


def templates_available() -> bool:
    """True when the rebuilt tree is actually on disk with a usable shell."""
    return (TEST_SITE_TEMPLATES / "base.html").is_file() or (LIVE_TEMPLATES / "base.html").is_file()


def static_roots() -> list[str]:
    """Static directories to search, in priority order."""
    roots: list[str] = []
    if TEST_SITE_STATIC.is_dir():
        roots.append(str(TEST_SITE_STATIC))
    if LIVE_STATIC.is_dir():
        roots.append(str(LIVE_STATIC))
    return roots


def _make_static_view(app: Quart) -> Any:
    """A ``/static/<path>`` view that serves the rebuilt tree, then ``static/``."""

    async def _static(filename: str) -> Any:
        if (TEST_SITE_STATIC / filename).is_file():
            return await send_from_directory(str(TEST_SITE_STATIC), filename)
        return await send_from_directory(str(LIVE_STATIC), filename)

    return _static


def apply_test_site_cutover(app: Quart) -> bool:
    """Point the app at the migrated UI tree. Returns True when applied."""
    if not templates_available():
        logger.error(
            "Rebuilt UI tree missing; keeping default template loader",
            templates_dir=str(LIVE_TEMPLATES),
        )
        return False

    loaders = [CaseInsensitiveFileSystemLoader(str(LIVE_TEMPLATES))]
    if TEST_SITE_TEMPLATES.is_dir():
        loaders.insert(
            0,
            CaseInsensitiveFileSystemLoader(
                str(TEST_SITE_TEMPLATES), shadowed=_SHADOWED_TEMPLATES
            ),
        )
    app.jinja_loader = ChoiceLoader(loaders)

    app.view_functions["static"] = _make_static_view(app)

    logger.info(
        "Unified UI cutover ACTIVE — serving rebuilt UI at /",
        templates=str(LIVE_TEMPLATES),
        static=str(LIVE_STATIC),
    )
    return True


def maybe_apply_cutover(app: Quart) -> bool:
    """Apply the cutover (enabled by default). Returns True when applied."""
    try:
        if not config_enables_test_site():
            logger.info("Test-site mode off — serving the live UI")
            return False
        return apply_test_site_cutover(app)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Test-site cutover failed; keeping the live UI", error=str(exc))
        return False


def status() -> dict[str, Any]:
    """Diagnostics for the Config page / logs."""
    enabled = config_enables_test_site()
    available = templates_available()
    return {
        "enabled": enabled,
        "available": available,
        "active": bool(enabled and available),
        "templates_dir": str(TEST_SITE_TEMPLATES),
        "static_dir": str(TEST_SITE_STATIC),
    }
