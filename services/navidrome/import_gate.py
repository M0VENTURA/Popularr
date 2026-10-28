"""Cross-process gate: "is a Navidrome import currently running?"

WHY THIS EXISTS
---------------
The Navidrome import now runs on a schedule (every four hours by default), so it
overlaps ordinary work far more often than a manual button press ever did. While
it runs it is already asking Navidrome for every artist, album and track; the
popularity/album pipelines meanwhile push star ratings, re-sync playlists, re-read
playlist contents and fetch cover art — every one of those a separate Subsonic
request. Lining them up behind the import means the import finishes sooner and
Navidrome is not answering the same question twice, from two directions.

WHAT "POLLING" MEANS HERE
-------------------------
The work that is DEFERRED is the incidental round trip a scan makes while it is
really doing something else:

  * pushing a star rating (``setRating``)      — one request per changed track
  * syncing a playlist (``createPlaylist`` / ``updatePlaylist`` / ``getPlaylist``)
  * a playlist-index content read
  * a cover-art lookup (``getCoverArt``)

The import's OWN requests are never deferred — that would deadlock it against
itself. Import endpoints (``startScan``, ``getScanStatus``, ``getArtists``,
``getAlbum``, …) are not in the deferred set at all.

⚠️ DEFER, DO NOT BLOCK. Every consumer here already has a correct "could not do
it now" path: a rating push returns ``False`` and the album logs it; a cover
lookup returns ``None`` and the stored artwork stands; a playlist sync reports
``skipped``. Blocking would instead stall a scan for minutes behind an import it
cannot influence — the opposite of the intent.

WHY A FILE AND NOT A FLAG
-------------------------
The app runs **4 hypercorn workers** plus a standalone queue worker and the
scheduler. An in-process boolean would be true in exactly one of them, so a scan
running in worker 2 would keep polling while the import ran in worker 3.

The state is therefore the SAME progress file the import already writes
(``<state>/navidrome_scan_progress.json``, see
``helpers.config_helpers.get_navidrome_progress_file``). That file is the
established cross-process signal in this codebase — ``scan_state`` already uses
it for progress, stop requests and checkpoints — and it means there is no second
source of truth to keep in sync.

⚠️ AND IT IS CROSS-HOST TOO. The state directory is a shared volume, so the
standalone queue worker sees the same file as the web workers.

FAILURE MODE: FAIL OPEN
-----------------------
An unreadable or missing file means "no import running", so the caller proceeds
exactly as it did before this module existed. A missed defer turns into traffic
that is merely not optimal; failing closed would silently disable rating sync,
playlist sync and cover art whenever the state directory was unavailable.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Endpoints whose traffic a running import should not have to compete with.
#:
#: Deliberately an ALLOW-LIST of the incidental work rather than a deny-list of
#: everything else: a new import endpoint must never be accidentally deferred,
#: and an endpoint added later that is neither here nor an import call simply
#: behaves as it does today.
DEFERRED_ENDPOINTS: frozenset[str] = frozenset({
    # Rating pushes (one per changed track during a finalise pass).
    "setRating",
    "star",
    "unstar",
    # Playlist sync + the playlist index's content reads.
    "getPlaylists",
    "getPlaylist",
    "createPlaylist",
    "updatePlaylist",
    "deletePlaylist",
    # Cover-art lookups from the album-art pipeline and page loads.
    "getCoverArt",
})

#: How long a read of the progress file is trusted before being re-read.
#:
#: The file is small and the read is local, but a scan pushes a rating per track
#: and we should not stat/parse the file for each one. Kept very short: a stale
#: "no import running" would delay the deferral by at most this long, and a stale
#: "import running" would suppress polling for the same window after it ends.
_CACHE_TTL_SECONDS = 2.0

_state_lock = __import__("threading").Lock()
_cached_verdict: tuple[float, bool] | None = None


def _progress_file() -> str:
    from helpers.config_helpers import get_navidrome_progress_file

    return get_navidrome_progress_file()


def _read_import_running() -> bool:
    """Read the cross-process import state.

    The canonical state is now the DB-backed ``scan_states`` row written by
    ``scan_state.write_progress_with_current_artist``. The JSON progress file
    is still honoured for legacy/standalone workers and tests, but a DB row
    saying ``is_running`` must win: otherwise the playlist/rating gate never
    defers during a modern import.
    """
    file_running = False
    try:
        path = _progress_file()
        with open(path, "r", encoding="utf-8") as handle:
            state: Any = json.load(handle)
        file_running = bool((state or {}).get("is_running"))
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("Navidrome import gate: file state unreadable", error=str(exc))

    try:
        from db.engine import db_session
        from db.models import ScanState

        with db_session() as session:
            row = session.query(ScanState).filter_by(scan_type="navidrome_scan").first()
            if row is not None and bool(row.is_running):
                return True
    except Exception as exc:
        # Fail OPEN — see the module docstring.
        logger.debug("Navidrome import gate: DB state unreadable", error=str(exc))

    return file_running


def is_import_running() -> bool:
    """True while a Navidrome import scan is running anywhere in the deployment.

    Cached for :data:`_CACHE_TTL_SECONDS` so a per-track caller does not read the
    file thousands of times per album. ``force=True`` bypasses the cache for the
    import itself and for tests.
    """
    global _cached_verdict
    now = time.monotonic()
    with _state_lock:
        if _cached_verdict is not None and (now - _cached_verdict[0]) < _CACHE_TTL_SECONDS:
            return _cached_verdict[1]
    verdict = _read_import_running()
    with _state_lock:
        _cached_verdict = (now, verdict)
    return verdict


def is_import_running_uncached() -> bool:
    """Read the current state, ignoring the cache. Used by tests and the gate itself."""
    global _cached_verdict
    verdict = _read_import_running()
    with _state_lock:
        _cached_verdict = (time.monotonic(), verdict)
    return verdict


def reset_cache() -> None:
    """Forget the cached verdict (tests, and after the import toggles)."""
    global _cached_verdict
    with _state_lock:
        _cached_verdict = None


def should_defer(endpoint: str) -> bool:
    """True when *endpoint* is incidental polling AND an import is running.

    ``endpoint`` is the bare Subsonic name (``"setRating"``), as passed to
    ``NavidromeClient._get_subsonic_response`` / ``_post_subsonic_response``.
    """
    name = str(endpoint or "").strip()
    if not name or name not in DEFERRED_ENDPOINTS:
        return False
    return is_import_running()


def defer_reason() -> str:
    """Short human explanation for a log line when polling is deferred."""
    return "Navidrome import in progress — deferring incidental polling"
