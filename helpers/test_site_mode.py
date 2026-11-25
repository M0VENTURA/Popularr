"""Serve the rebuilt UI tree (``test_site/``) at the ROOT of the app.

The former live/test-site switch has been removed: there is now ONE UI. This
module keeps the two startup levers the cutover always needed:

* ``app.jinja_loader`` — a case-insensitive loader over ``test_site/templates``.
  The routes ask for lowercase ``pages/dashboard.html`` while the tree stores
  ``Pages/dashboard.html``; on Linux that would be a hard 404 without it.
* the ``static`` view function — serving ``test_site/static``. Assigning
  ``app.static_folder`` after the route is built has no effect.

Both are applied once during startup.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from jinja2 import FileSystemLoader, TemplateNotFound
from quart import Quart, send_from_directory

logger = structlog.get_logger(__name__)

#: Repo root (this file is helpers/…).
_REPO_ROOT = Path(__file__).resolve().parent.parent

TEST_SITE_DIR = _REPO_ROOT / "test_site"
TEST_SITE_TEMPLATES = TEST_SITE_DIR / "templates"
TEST_SITE_STATIC = TEST_SITE_DIR / "static"


class CaseInsensitiveFileSystemLoader(FileSystemLoader):
    """``FileSystemLoader`` that retries a lookup case-insensitively."""

    def get_source(self, environment: Any, template: str) -> Any:
        try:
            return super().get_source(environment, template)
        except TemplateNotFound:
            resolved = self._resolve_case_insensitive(template)
            if resolved is None or resolved == template:
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
    """Kept for callers that still gate work on the UI tree; always True."""
    return True


def templates_available() -> bool:
    return (TEST_SITE_TEMPLATES / "base.html").is_file()


def static_roots() -> list[str]:
    return [str(TEST_SITE_STATIC)] if TEST_SITE_STATIC.is_dir() else []


def _make_static_view(app: Quart) -> Any:
    async def _static(filename: str) -> Any:
        return await send_from_directory(str(TEST_SITE_STATIC), filename)

    return _static


def apply_test_site_cutover(app: Quart) -> bool:
    """Point the app at the single UI tree. Returns True when applied."""
    if not templates_available():
        logger.error(
            "UI tree missing; keeping the default template loader",
            templates_dir=str(TEST_SITE_TEMPLATES),
        )
        return False

    app.jinja_loader = CaseInsensitiveFileSystemLoader(str(TEST_SITE_TEMPLATES))
    app.view_functions["static"] = _make_static_view(app)

    logger.info(
        "UI active — serving the rebuilt tree at /",
        templates=str(TEST_SITE_TEMPLATES),
        static=str(TEST_SITE_STATIC),
    )
    return True


def maybe_apply_cutover(app: Quart) -> bool:
    try:
        return apply_test_site_cutover(app)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("UI cutover failed; keeping the default loader", error=str(exc))
        return False


def status() -> dict[str, Any]:
    available = templates_available()
    return {
        "enabled": True,
        "available": available,
        "active": available,
        "templates_dir": str(TEST_SITE_TEMPLATES),
        "static_dir": str(TEST_SITE_STATIC),
    }
