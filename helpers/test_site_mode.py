"""Test-site cutover mode.

Renders the rebuilt UI tree (``test_site/``) at the ROOT of the app instead of
serving it under a ``/test-site`` prefix.  Because every URL stays identical,
every absolute link, form action, ``url_for`` call and year-scoped album URL
works unmodified — there is nothing to shim, which is what makes this a *usable*
site rather than a set of islands.

Two modes
---------
* **live** (default) — ``templates/`` and ``static/`` as they have always been.
* **test_site** — ``test_site/templates`` and ``test_site/static`` take
  priority, with the LIVE tree as a fallback.

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
between requests.  Toggling the config therefore needs a restart, which is the
correct trade for not mutating the app under concurrent requests.

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
#:
#: This exists because ``test_site/`` cannot be edited destructively from the
#: environment it is maintained in, so a known-bad file can be *shadowed*
#: instead of deleted.
#:
#: ``pages/downloads/monitor.html`` — the rebuilt copy is a stray artist-page
#: snapshot (title "{{ artist_name }}", thousands of lines, none of the monitor
#: ids). Serving it raised ``BuildError: Could not build url for endpoint
#: 'dashboard'`` on GET /downloads/monitor, because its links use BARE endpoint
#: names while every blueprint in this app is namespaced (``ui.dashboard``).
#: The live ``templates/pages/downloads/monitor.html`` is the correct page.
#:
#: NOTE on ``components/_release_section.html``: the rebuilt copy used to be a
#: stray FULL ARTIST PAGE (byte-identical to templates/pages/artist_detail.html)
#: that defined NO MACRO, so importing it raised
#: ``ImportError: cannot import name 'render_release_section'`` — and because the
#: rebuilt tree is a PREFERRED loader, that broken copy also won over the correct
#: live macro. It has since been REPAIRED in place (it now defines the macro and
#: keeps the superseded snapshot as an inert comment), so it is deliberately NOT
#: shadowed: shadowing would make the rebuilt copy dead weight. Keep both copies
#: equivalent; tests/test_artist_page_contract.py asserts that they are.
#:
#: REMOVE an entry once the rebuilt file is replaced or deleted.
_SHADOWED_TEMPLATES: Final[frozenset[str]] = frozenset({
    "pages/downloads/monitor.html",
})


class CaseInsensitiveFileSystemLoader(FileSystemLoader):
    """``FileSystemLoader`` that retries a lookup case-insensitively.

    The rebuilt tree uses ``Pages/`` and ``Playlists/``; the live routes request
    ``pages/`` and ``playlists/``. That resolves by accident on Windows/macOS and
    fails outright on Linux, so the match is done explicitly here instead of
    relying on the host filesystem.

    ``shadowed`` additionally refuses named templates, so a known-bad rebuilt
    file falls through to the live tree rather than raising.  This matters
    because the rebuilt tree is a PREFERRED loader: without the check, a broken
    file there is served in preference to a working live file.

    ``shadowed`` is PER INSTANCE, not global, and that is essential — the live
    loader is wrapped in this same class, and a global check would make the live
    tree refuse the very file it is supposed to provide, turning a shadowed
    template into a 500 instead of a fallback.
    """

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
            # A case-insensitive hit must still honour the shadow list: the
            # caller asked for the lowercase form, but the file that resolved
            # may be the shadowed one.
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
    """True when config.yaml asks for the rebuilt UI.

    Reads through the normal config layer so the value is honoured from
    ``config.yaml``, the env override and the Config page alike.  Any failure
    resolves to False: the live UI is the safe default.
    """
    try:
        from helpers.config_helpers import get_config

        cfg = get_config() or {}
        features = cfg.get("features") or {}
        if not isinstance(features, dict):
            return False
        raw = features.get(CONFIG_KEY, False)
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Test-site mode check failed; defaulting to live UI", error=str(exc))
        return False


def templates_available() -> bool:
    """True when the rebuilt tree is actually on disk with a usable shell."""
    return (TEST_SITE_TEMPLATES / "base.html").is_file()


def static_roots() -> list[str]:
    """Static directories to search, in priority order.

    Used by the ``versioned_static`` cache-buster so a rebuilt asset is stat-ed in
    the rebuilt tree first.  Always ends with the live root so the fallback
    assets (``dist/vendor/*``) still resolve.
    """
    roots: list[str] = []
    if config_enables_test_site() and TEST_SITE_STATIC.is_dir():
        roots.append(str(TEST_SITE_STATIC))
    if LIVE_STATIC.is_dir():
        roots.append(str(LIVE_STATIC))
    return roots


def _make_static_view(app: Quart) -> Any:
    """A ``/static/<path>`` view that serves the rebuilt tree, then the live one.

    Rebound onto ``app.view_functions['static']``.  The live directory is the
    fallback so ``dist/vendor`` (only present in the live tree) still resolves.

    Every lookup goes through ``send_from_directory`` rather than a manual
    ``root / filename`` check: that helper is what rejects ``../`` traversal and
    absolute paths, so probing the filesystem directly would let a crafted
    ``/static/../../etc/passwd`` at least disclose whether a file exists.
    """

    async def _static(filename: str) -> Any:
        try:
            return await send_from_directory(str(TEST_SITE_STATIC), filename)
        except Exception:
            # Not in the rebuilt tree (or unsafe): fall back to the live assets.
            return await send_from_directory(str(LIVE_STATIC), filename)

    return _static


def apply_test_site_cutover(app: Quart) -> bool:
    """Point the app at the rebuilt tree. Returns True when applied.

    Safe to call when the tree is absent — it logs and leaves the live UI in
    place, so a bad config value cannot take the app down.
    """
    if not templates_available():
        logger.error(
            "Test-site mode requested but the rebuilt tree is missing; keeping the live UI",
            templates_dir=str(TEST_SITE_TEMPLATES),
        )
        return False

    app.jinja_loader = ChoiceLoader([
        # The rebuilt tree is PREFERRED, so it carries the shadow list; the live
        # tree must be able to serve anything (it is the fallback for every
        # shadowed template), so it gets none.
        CaseInsensitiveFileSystemLoader(
            str(TEST_SITE_TEMPLATES), shadowed=_SHADOWED_TEMPLATES
        ),
        CaseInsensitiveFileSystemLoader(str(LIVE_TEMPLATES)),
    ])

    # Rebinding the view function is what actually re-points /static; assigning
    # app.static_folder here would be silently ignored.
    app.view_functions["static"] = _make_static_view(app)

    logger.info(
        "Test-site cutover ACTIVE — the rebuilt UI is served at /",
        templates=str(TEST_SITE_TEMPLATES),
        static=str(TEST_SITE_STATIC),
    )
    return True


def maybe_apply_cutover(app: Quart) -> bool:
    """Apply the cutover when the config asks for it. Returns True when applied."""
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
