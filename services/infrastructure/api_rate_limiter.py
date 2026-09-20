"""Cross-provider API rate limiter.

This manages thread-safe API request throttling and state tracking
across external providers (MusicBrainz, ListenBrainz, Last.fm, Spotify).

⚠️ THE BUDGET IS PER DEPLOYMENT, NOT PER PROCESS
------------------------------------------------
``state`` is persisted to a JSON file in the shared state directory, but the
reservation used to happen purely in memory (and the file was only written
once every 30s).  Every PROCESS therefore had its own idea of when the last
request was, so ``MUSICBRAINZ_MIN_INTERVAL`` was enforced once per process:
hypercorn serves 4 workers plus the standalone queue worker, so MusicBrainz
received several requests per second instead of one — the 503s and the circuit
breaker opening in the middle of a scan.

The slot reservation below is now done while holding an exclusive **flock** on
a per-provider lock file, reading and writing the shared state inside that lock,
so the interval holds across every process that can see the state directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from datetime import datetime
from typing import Any

import structlog

try:  # POSIX (the Linux container)
    import fcntl
except ImportError:  # pragma: no cover - Windows development/tests
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

logger = structlog.get_logger(__name__)

SPOTIFY_RATE_LIMIT_PER_30S = 250
SPOTIFY_DAILY_LIMIT = 500000
LASTFM_RATE_LIMIT_PER_SECOND = 1.0
LASTFM_DAILY_LIMIT = 50000
MUSICBRAINZ_MIN_INTERVAL = 1.0
LISTENBRAINZ_MIN_INTERVAL = 1.0
LISTENBRAINZ_DAILY_LIMIT = 50000

_shared_lock_warned = False


@contextlib.contextmanager
def _shared_state_lock(lock_path: str):
    """Hold an exclusive advisory lock on *lock_path* for the duration.

    ``flock`` rather than a lock FILE because the kernel releases it when the
    holder dies, so a crashed worker can never leave the API budget permanently
    locked.  If the filesystem cannot support locking, this degrades to
    in-process coordination with ONE warning — a rate limiter must never be the
    reason the app fails.
    """
    global _shared_lock_warned
    handle = None
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        handle = open(lock_path, "a+b")
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        elif not _shared_lock_warned:
            _shared_lock_warned = True
            logger.warning(
                "No file-locking primitive available - API rate limits are enforced per process only"
            )
    except Exception as exc:
        if not _shared_lock_warned:
            _shared_lock_warned = True
            logger.warning(
                "Cross-process API rate-limit lock unavailable; falling back to per-process limits",
                lock=lock_path,
                error=str(exc),
            )
        with contextlib.suppress(Exception):
            if handle is not None:
                handle.close()
        handle = None

    try:
        yield
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            with contextlib.suppress(Exception):
                handle.close()


class APIRateLimiter:
    _STATE_SAVE_INTERVAL_SECONDS = 30

    def __init__(self, state_file: str | None = None) -> None:
        if state_file is None:
            from helpers.config_helpers import get_api_rate_limiter_state_file
            state_file = get_api_rate_limiter_state_file()
        self.state_file = state_file
        self.state = self._load_state()
        self._last_save_time = 0.0
        self._mb_lock = threading.Lock()
        self._lastfm_lock = threading.Lock()
        self._listenbrainz_lock = threading.Lock()

    def _load_state(self) -> dict[str, Any]:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
                last_reset = state.get("last_reset", "")
                if last_reset and datetime.fromisoformat(last_reset).date() < datetime.now().date():
                    state["spotify_daily_count"] = 0
                    state["lastfm_daily_count"] = 0
                    state["musicbrainz_daily_count"] = 0
                    state["listenbrainz_daily_count"] = 0
                    state["last_reset"] = datetime.now().isoformat()
                    self.state = state
                    self._save_state(force=True)
                return state
            except Exception as exc:
                logger.debug("Could not load API rate limiter state", error=str(exc))
        return {
            "spotify_daily_count": 0,
            "lastfm_daily_count": 0,
            "musicbrainz_daily_count": 0,
            "listenbrainz_daily_count": 0,
            "spotify_recent_requests": [],
            "lastfm_last_request": 0.0,
            "musicbrainz_last_request": 0.0,
            "listenbrainz_last_request": 0.0,
            "last_reset": datetime.now().isoformat(),
        }

    def _save_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save_time < self._STATE_SAVE_INTERVAL_SECONDS:
            return
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2)
            self._last_save_time = now
        except Exception as exc:
            logger.debug("Could not save API rate limiter state", error=str(exc))

    def _lock_path(self, provider: str) -> str:
        """One lock per provider so MusicBrainz never waits on Last.fm."""
        return f"{self.state_file}.{provider}.lock"

    def _read_state_from_disk(self) -> dict[str, Any]:
        """Re-read the persisted state.  Callers must hold the provider lock."""
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
        return dict(self.state)

    def _write_state_to_disk(self, state: dict[str, Any]) -> None:
        """Persist *state* atomically (temp file + ``os.replace``)."""
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp_path = f"{self.state_file}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2)
            os.replace(tmp_path, self.state_file)
            self._last_save_time = time.time()
        except Exception as exc:
            logger.debug("Could not save API rate limiter state", error=str(exc))

    def _reserve_shared_slot(self, provider: str, interval: float, count_key: str) -> float:
        """Reserve the next *provider* slot ACROSS PROCESSES; return seconds to wait.

        THE BUG THIS FIXES: ``self.state`` is an in-memory copy loaded once at
        construction, and ``_save_state`` wrote it at most every 30s.  Each
        process therefore believed IT had waited long enough, so the provider
        interval was enforced per process — with 4 hypercorn workers plus the
        queue worker that is several requests per second against a 1 req/s
        budget, which is what MusicBrainz answered with 503s.

        The reservation is a read-modify-write of the SHARED file under an
        exclusive lock, so two processes can never claim the same slot.  The
        sleep stays OUTSIDE the lock (see the module docstring): sleeping while
        holding it would serialise every caller behind one sleeper.
        """
        now = time.time()
        with _shared_state_lock(self._lock_path(provider)):
            state = self._read_state_from_disk()

            # Daily counters reset on a date change; doing it inside the lock
            # keeps that correct across processes too (it used to depend on
            # whichever process happened to reload the file first).
            today = datetime.now().date().isoformat()
            last_reset = str(state.get("last_reset") or "")
            if last_reset[:10] != today:
                state["spotify_daily_count"] = 0
                state["lastfm_daily_count"] = 0
                state["musicbrainz_daily_count"] = 0
                state["listenbrainz_daily_count"] = 0
                state["last_reset"] = datetime.now().isoformat()

            last_request = float(state.get(f"{provider}_last_request") or 0.0)
            allowed_time = max(now, last_request + interval)
            state[f"{provider}_last_request"] = allowed_time
            state[count_key] = int(state.get(count_key) or 0) + 1

            self._write_state_to_disk(state)
            self.state.update(state)

        return max(0.0, allowed_time - now)

    def throttle_musicbrainz(self) -> None:
        with self._mb_lock:
            wait_time = self._reserve_shared_slot(
                "musicbrainz", MUSICBRAINZ_MIN_INTERVAL, "musicbrainz_daily_count"
            )

        if wait_time > 0:
            time.sleep(wait_time)

    def throttle_lastfm(self) -> None:
        with self._lastfm_lock:
            wait_time = self._reserve_shared_slot(
                "lastfm", LASTFM_RATE_LIMIT_PER_SECOND, "lastfm_daily_count"
            )

        if wait_time > 0:
            time.sleep(wait_time)

    def throttle_listenbrainz(self) -> None:
        with self._listenbrainz_lock:
            wait_time = self._reserve_shared_slot(
                "listenbrainz", LISTENBRAINZ_MIN_INTERVAL, "listenbrainz_daily_count"
            )

        if wait_time > 0:
            time.sleep(wait_time)

    def wait_if_needed_lastfm(self, max_wait_seconds: float = 2.0) -> bool:
        """Reserve a Last.fm slot only when the wait is short enough.

        The slot is claimed inside the shared lock, so two processes cannot both
        decide there is room for the same slot.  A wait longer than
        *max_wait_seconds* leaves the state untouched (nothing is consumed).
        """
        with self._lastfm_lock:
            now = time.time()
            should_wait = False
            wait_time = 0.0
            with _shared_state_lock(self._lock_path("lastfm")):
                state = self._read_state_from_disk()
                last_request = float(state.get("lastfm_last_request") or 0.0)
                allowed_time = max(now, last_request + LASTFM_RATE_LIMIT_PER_SECOND)
                wait_time = allowed_time - now

                if wait_time <= max_wait_seconds:
                    state["lastfm_last_request"] = allowed_time
                    state["lastfm_daily_count"] = int(state.get("lastfm_daily_count") or 0) + 1
                    self._write_state_to_disk(state)
                    self.state.update(state)
                    should_wait = True
                else:
                    should_wait = False
                    wait_time = 0.0

        if should_wait and wait_time > 0:
            time.sleep(wait_time)
            return True

        return should_wait

    def get_stats(self) -> dict[str, Any]:
        now = time.time()
        recent_spotify = [ts for ts in self.state.get("spotify_recent_requests", []) if now - ts < 30]
        return {
            "spotify_daily_count": self.state.get("spotify_daily_count", 0),
            "spotify_daily_limit": SPOTIFY_DAILY_LIMIT,
            "spotify_recent_30s": len(recent_spotify),
            "spotify_30s_limit": SPOTIFY_RATE_LIMIT_PER_30S,
            "lastfm_daily_count": self.state.get("lastfm_daily_count", 0),
            "lastfm_daily_limit": LASTFM_DAILY_LIMIT,
            "musicbrainz_daily_count": self.state.get("musicbrainz_daily_count", 0),
            "listenbrainz_daily_count": self.state.get("listenbrainz_daily_count", 0),
            "listenbrainz_daily_limit": LISTENBRAINZ_DAILY_LIMIT,
            "last_reset": self.state.get("last_reset", ""),
        }


_rate_limiter: APIRateLimiter | None = None
_rate_limiter_lock = threading.Lock()


def get_rate_limiter() -> APIRateLimiter:
    """The process-wide limiter (created under a lock — see below).

    The lock matters even though the reservation itself is now cross-process:
    two threads racing the ``is None`` check would build two limiters, and the
    ``api_clients/musicbrainz_http.py`` fast path caches whichever instance it
    happened to receive, so the two would then disagree about the daily counts.
    """
    global _rate_limiter
    if _rate_limiter is None:
        with _rate_limiter_lock:
            if _rate_limiter is None:
                _rate_limiter = APIRateLimiter()
    return _rate_limiter
