"""Async route handlers must not run blocking work on the event loop.

Regression: the app froze when a scan was started from the artist page.

``routes/ui_routes.py::artist_detail`` was an ``async`` handler whose entire
body ran inline on the event loop. That body performs blocking work:

  - 5 ``db_session`` reads (synchronous SQLAlchemy/C PostgreSQL I/O), and
  - ``get_artist_members_cached()`` / ``get_artist_genre_sources()``, which
    reach MusicBrainz/Last.fm through the shared client.

MusicBrainz access is globally rate-limited to ~1 req/s by
``api_clients/musicbrainz_http.py::_strict_throttle``, which enforces the budget
by SLEEPING to reserve a future slot. A running popularity scan saturates that
budget (the production log shows 30-40s MusicBrainz calls and repeated
"[MB] call still running" warnings), so the artist page's lookup could block the
event loop for tens of seconds. Because hypercorn serves each worker with a
single asyncio loop, EVERY other request in that worker stalled behind it —
the reported "app freezes when running a scan from the artist page".

The fix moves the context build into ``_build_artist_detail_payload`` and calls
it via ``asyncio.to_thread``, keeping only ``render_template`` (which needs the
app/request context) on the loop.

Why this guard is structural
----------------------------
It is not possible to catch this by importing modules: the problem is a
*call-graph* property (async handler -> blocking callee), and the blocking
callees are ordinary synchronous functions that are perfectly valid everywhere
else. So this walks the AST of the route modules and flags any ``async`` handler
that contains blocking work WITHOUT any ``await``-based offloading.

Deliberate scope limits, to avoid false positives:
  - Only handlers decorated with a route decorator are checked.
  - Only modules under ``routes/`` are checked.
  - A handler is exempt when it offloads anywhere (``asyncio.to_thread`` /
    ``run_in_executor`` / ``run_sync``), because the blocking work may sit next
    to properly-offloaded I/O in the same handler.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ROUTES_DIR = REPO_ROOT / "routes"

# Synchronous calls that block the event loop for a meaningful time.
# Kept to the DB/network primitives that are safe to detect by name.
# NOTE: ``async_db_session`` is deliberately NOT here - it is an async context
# manager, so awaiting it yields the loop rather than blocking it.
BLOCKING_CALLS = frozenset({
    "db_session",
    "read_progress_file",
    "write_progress_file",
    "write_progress_with_current_artist",
    "clear_progress_file",
    "record_scan",
    "get_artist_members_cached",
    "get_artist_genre_sources",
    "get_album_tag_inconsistencies",
    "get_conflict_stats",
    "get_artist_corrections",
    "sync_rss_playlists_for_user",
})

OFFLOAD_MARKERS = frozenset({"to_thread", "run_in_executor", "run_sync"})

# ---------------------------------------------------------------------------
# Known, pre-existing offenders. Offload one -> delete its line here.
# Do NOT add to this list without a specific reason; prefer asyncio.to_thread.
# ``ui_routes.py::artist_detail`` is deliberately ABSENT - it is fixed.
# ---------------------------------------------------------------------------
_KNOWN_OFFENDERS = frozenset({
    "api_v1/albums.py::bulk_delete_album_tracks",
    "api_v1/artists.py::get_artist",
    "api_v1/tracks.py::apply_track_mb_field",
    "api_v1/tracks.py::get_track",
    "api_v1/tracks.py::get_track_genres",
    "api_v1/tracks.py::ignore_track_mb_field",
    "downloads.py::api_batch_group",
    "downloads.py::api_queue_upcoming",
    "metadata.py::api_get_conflict_stats",
    "misc_routes.py::api_apply_country_as_genre",
    "misc_routes.py::api_apply_genres",
    "misc_routes.py::api_bookmarks",
    "misc_routes.py::api_cleanup_duplicates",
    "misc_routes.py::api_correcting_ignore",
    "misc_routes.py::api_correcting_list_ignores",
    "misc_routes.py::api_correcting_unignore",
    "misc_routes.py::api_fetch_artist_country",
    "misc_routes.py::api_merge_duplicate_artists",
    "misc_routes.py::api_remove_genres",
    "misc_routes.py::api_track_tags",
    "misc_routes.py::api_update_artist_country",
    "musicbrainz_routes.py::api_link_album_mbids",
    "musicbrainz_routes.py::api_musicbrainz_search",
    "playlists.py::api_playlist_import_csv",
    "playlists.py::api_playlists_add_track",
    "queue/matching_routes.py::api_queue_apply_mbid_match",
    "queue/matching_routes.py::api_queue_link_track",
    "queue/processing_routes.py::api_queue_import_missing_tracks",
    "queue/processing_routes.py::api_queue_update_album_mbid",
    "scan_routes/api.py::api_popularity_run_compat",
    "scan_routes/control.py::scan_clear_stuck",
    "scan_routes/popularity.py::api_scan_from_artist",
    "scan_routes/popularity.py::scan_popularity_route",
    "track_routes.py::api_fetch_track_credits",
    "track_routes.py::api_fetch_track_lyrics",
    "track_routes.py::api_rescan_single_track",
    "track_routes.py::api_track_apply_mb_release",
    "track_routes.py::api_track_audio",
    "track_routes.py::api_track_ignore_mb_field",
    "track_routes.py::api_track_match_missing",
    "track_routes.py::api_track_update_metadata",
    "ui_routes.py::album_detail",
    "ui_routes.py::artist_corrections",
    "ui_routes.py::artist_genre_management",
    "ui_routes.py::artists",
    "ui_routes.py::correcting",
    "ui_routes.py::dashboard",
    "ui_routes.py::metadata_compare",
    "ui_routes.py::metadata_compare_accept_navidrome",
    "ui_routes.py::metadata_compare_apply_mb",
    "ui_routes.py::track_detail",
    "upcoming_releases_routes.py::api_add_upcoming_release",
    "upcoming_releases_routes.py::api_match_upcoming_release",
})


def _route_modules() -> list[pathlib.Path]:
    return sorted(p for p in ROUTES_DIR.rglob("*.py") if p.name != "__init__.py")


def _is_route_handler(fn: ast.AST) -> bool:
    return any("route" in ast.unparse(d) for d in getattr(fn, "decorator_list", []))


def _has_offload(fn: ast.AST) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            text = ast.unparse(n.func)
            if any(m in text for m in OFFLOAD_MARKERS):
                return True
    return False


def _blocking_uses(fn: ast.AST) -> list[tuple[int, str]]:
    """Return (line, name) for blocking calls/with-blocks inside ``fn``."""
    found: list[tuple[int, str]] = []

    for n in ast.walk(fn):
        if isinstance(n, ast.With):
            text = ast.unparse(n)
            for name in BLOCKING_CALLS:
                if name in text:
                    found.append((n.lineno, name))
        elif isinstance(n, ast.Call):
            callee = ast.unparse(n.func).split(".")[-1]
            if callee in BLOCKING_CALLS:
                found.append((n.lineno, callee))

    # Deduplicate: a `with db_session()` shows up as both a With and a Call.
    return sorted(set(found))


def _offenders() -> dict[str, str]:
    """Return {dotted_key: detail} for async handlers blocking with no offload."""
    found: dict[str, str] = {}

    for path in _route_modules():
        rel = path.relative_to(ROUTES_DIR).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            if not _is_route_handler(fn) or _has_offload(fn):
                continue
            blocking = _blocking_uses(fn)
            if not blocking:
                continue
            names = sorted({name for _, name in blocking})
            key = f"{rel}::{fn.name}"
            found[key] = f"{key} (line {fn.lineno}) -> {', '.join(names)}"

    return found


def test_route_scan_finds_handlers():
    """Guard the guard: the audit must actually be looking at real handlers."""
    handlers = 0
    for path in _route_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        handlers += sum(
            1
            for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef) and _is_route_handler(fn)
        )

    assert handlers > 50, f"only found {handlers} async route handlers - walk is broken"


def test_no_new_async_handler_blocks_the_event_loop():
    """Fail when an async handler blocks the loop and is not a known offender."""
    new = {
        key: detail
        for key, detail in _offenders().items()
        if key not in _KNOWN_OFFENDERS
    }

    assert not new, (
        "New async route handler(s) run blocking work on the event loop. "
        "Under load (e.g. a scan saturating the MusicBrainz 1 req/s budget) "
        "this freezes EVERY request served by the same hypercorn worker. "
        "Move the blocking work into a sync helper and call it with "
        "`await asyncio.to_thread(...)`.\n\n" + "\n".join(sorted(new.values()))
    )


def test_known_offender_list_has_no_stale_entries():
    """The allow-list must not drift - fix an entry and remove it here."""
    current = set(_offenders())
    stale = sorted(_KNOWN_OFFENDERS - current)

    assert not stale, (
        "These entries are in the allow-list but no longer block the loop "
        "(they were probably offloaded). Delete them from _KNOWN_OFFENDERS so "
        "the ratchet keeps tightening:\n  " + "\n  ".join(stale)
    )


def test_artist_detail_is_offloaded_and_not_an_offender():
    """Pin the specific fix: the artist page must never regress to blocking."""
    src = (ROUTES_DIR / "ui_routes.py").read_text(encoding="utf-8")

    assert "async def _build_artist_detail_payload" not in src, (
        "the builder must be a PLAIN function - an async builder would be "
        "awaited, not offloaded, and the blocking work would be back on the loop"
    )
    assert "def _build_artist_detail_payload" in src
    assert "asyncio.to_thread(_build_artist_detail_payload" in src
    assert "import asyncio" in src

    assert "ui_routes.py::artist_detail" not in _KNOWN_OFFENDERS, (
        "artist_detail must not be re-added to the allow-list - it is fixed"
    )

    tree = ast.parse(src)
    handler = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "artist_detail"
    )
    assert _has_offload(handler), "artist_detail no longer offloads its blocking build"
    assert not _blocking_uses(handler), (
        "artist_detail must not touch db_session / network helpers directly - "
        "those belong in the offloaded builder"
    )
