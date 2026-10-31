"""Download pipeline service.

Orchestrates the end-to-end download processing pipeline:
1. Fetch ready-to-process queue items.
2. Resolve MusicBrainz release metadata.
3. Build structured search queries for slskd.
4. Score and rank results to pick the best match.
5. Execute downloads via slskd.
6. Update queue status and library records.

Coordinates between ``downloads/slskd_service``, ``enrichment/musicbrainz_service``,
and ``db/repositories``.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Dict

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.library import upsert_musicbrainz_release
from db.repositories.queue import (
    get_ready_for_processing,
    mark_failed,
    update_queue_item,
)
from helpers.config_helpers import get_length_tolerance_seconds
from helpers.logging_config import log_queue, log_search, log_unified
from helpers.normalization_service import queue_duration_seconds
from services.downloads.slskd_service import SlskdService
from services.enrichment.musicbrainz_service import (
    fetch_musicbrainz_release_metadata,
    fetch_release_metadata,
    resolve_release_id,
)
from services.infrastructure.filesystem_service import create_monitoring_folder
from services.queue.queue_processing_service import (
    add_release_tracks_to_queue,
    add_release_tracks_to_queue_detailed,
)

logger = structlog.get_logger(__name__)

# =============================================================================
# PEER FAILURE MEMORY
# =============================================================================

_BLOCKED_PEER_TTL_SECONDS = int(
    os.environ.get("SLSKD_BLOCKED_PEER_TTL_SECONDS", "86400")
)
_PEER_STALL_COOLDOWN_SECONDS = int(
    os.environ.get("SLSKD_PEER_STALL_COOLDOWN_SECONDS", "7200")
)
_PEER_STALL_THRESHOLD = 2
_blocked_peers: dict[tuple[str, str], float] = {}
_peer_stall_timestamps: dict[str, list[float]] = {}
_blocked_usernames: dict[str, float] = {}


def _peer_key(username: str | None, filename: str | None) -> tuple[str, str]:
    """One spelling for a (peer, file) pair.

    ⚠️ THE REASON BLOCKS NEVER WORKED. Search results carry the peer's path
    with BACKSLASHES (``shared\\d-side\\BIGBANG\\...``) while the completion
    service blocks the path it stored with FORWARD slashes
    (``shared/d-side/BIGBANG/...``). The raw strings never matched, so the log
    printed "Blocked peer" for ``mugilaw`` about 20 times while the same file
    kept being selected. Both sides now normalise separators and case.
    """
    name = str(filename or "").replace("\\", "/").strip().casefold()
    return (str(username or "").strip().casefold(), name)


def _block_peer(username: str | None, filename: str | None) -> None:
    """Remember that *username* rejected/failed this file for *ttl* seconds.

    In memory only (lost on restart). The per-queue-row record
    (``record_rejected_candidate``) is the durable one.
    """
    if not username or not filename:
        return
    _blocked_peers[_peer_key(username, filename)] = time.time() + _BLOCKED_PEER_TTL_SECONDS
    # Bounded: drop expired entries once the map gets large.
    if len(_blocked_peers) > 5000:
        now = time.time()
        for key in [k for k, until in _blocked_peers.items() if until < now]:
            _blocked_peers.pop(key, None)


def _record_peer_stall(username: str | None, filename: str | None = None) -> None:
    """Record a stalled transfer for *username* and cooldown the peer after repeated stalls."""
    if not username:
        return
    if filename:
        _block_peer(username, filename)
    uname = str(username).strip().casefold()
    if not uname:
        return
    now = time.time()
    recent = [
        ts for ts in _peer_stall_timestamps.get(uname, [])
        if now - ts < _PEER_STALL_COOLDOWN_SECONDS
    ]
    recent.append(now)
    _peer_stall_timestamps[uname] = recent
    if len(recent) >= _PEER_STALL_THRESHOLD:
        _blocked_usernames[uname] = now + _PEER_STALL_COOLDOWN_SECONDS
        logger.warning(
            "Placed peer on stall cooldown after repeated stalled transfers",
            username=username,
            stall_count=len(recent),
            cooldown_seconds=_PEER_STALL_COOLDOWN_SECONDS,
        )


def _is_peer_blocked(username: str | None, filename: str | None) -> bool:
    if not username:
        return False
    now = time.time()
    uname = str(username).strip().casefold()
    user_until = _blocked_usernames.get(uname)
    if user_until is not None:
        if user_until < now:
            _blocked_usernames.pop(uname, None)
        else:
            return True
    if not filename:
        return False
    key = _peer_key(username, filename)
    blocked_until = _blocked_peers.get(key)
    if blocked_until is None:
        return False
    if blocked_until < now:
        _blocked_peers.pop(key, None)
        return False
    return True


def _filter_blocked_peers(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop search results from peers that recently failed this download."""
    if not _blocked_peers and not _blocked_usernames:
        return results
    return [
        r for r in results
        if not _is_peer_blocked(r.get("username"), r.get("filename"))
    ]


def _filter_rejected_for_item(
    results: list[dict[str, Any]],
    item: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    """Drop candidates already rejected FOR THIS QUEUE ROW (durable, per row).

    Returns ``(kept, dropped_count)``. Reads the row's stored record once; if
    the repository helper is unavailable, nothing is dropped.
    """
    if not results:
        return results, 0
    try:
        from db.repositories.queue import _candidate_key, get_rejected_candidate_keys

        keys = get_rejected_candidate_keys(item.get("id") or item)
    except Exception:
        return results, 0
    if not keys:
        return results, 0
    kept = [
        r for r in results
        if _candidate_key(r.get("username"), r.get("filename")) not in keys
        and _candidate_key("", r.get("filename")) not in keys
    ]
    return kept, len(results) - len(kept)


# =============================================================================
# TEXT NORMALISATION HELPERS
# =============================================================================

def _normalise(text_val: str) -> str:
    """Lower-case, strip, replace underscores, and collapse whitespace for comparison."""
    return re.sub(r"\s+", " ", re.sub(r"_+", " ", text_val.lower())).strip()


def _similarity(a: str, b: str) -> float:
    """Return a 0–1 similarity score between two strings."""
    return SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()


def _length_tolerance_seconds() -> float:
    """The shared length tolerance (``matching.duration_tolerance_seconds``).

    Thin wrapper so the call sites read in this module's vocabulary, with a
    fallback: a broken config must never make the matcher reject everything.
    """
    try:
        return float(get_length_tolerance_seconds())
    except Exception:
        return 5.0


_TRACK_NUMBER_PREFIX_RE = re.compile(r"^\s*\d{1,3}(?:\s*-\s*\d{1,3})?\s*(?:[-\.\)]\s*|\s+)")
_UNICODE_DASH_SEPARATOR_RE = re.compile(r"\s+[\u2010\u2011\u2012\u2013\u2014\u2015]+\s+")
_SCENE_CRC32_SUFFIX_RE = re.compile(r"-[0-9a-fA-F]{8}$")


def _clean_filename_segment(segment: str) -> str:
    return re.sub(r"\s+", " ", segment.replace("_", " ")).strip()


def _parse_filename_parts(filename: str) -> dict[str, str | None]:
    """Try to extract artist, album, title, bitrate, and format from a Soulseek filename."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = name.rsplit(".", 1)[0] if "." in name else name
    name = _SCENE_CRC32_SUFFIX_RE.sub("", name)
    name = _UNICODE_DASH_SEPARATOR_RE.sub(" - ", name)

    result: dict[str, str | None] = {
        "artist": None,
        "album": None,
        "title": None,
        "has_track_number": False,
        "format": None,
    }

    ext_match = re.search(r"\.(flac|mp3|wav|aac|ogg|wma|m4a|opus)$", filename.lower())
    if ext_match:
        result["format"] = ext_match.group(1)

    parts = re.split(r"\s*-\s*", name)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) >= 3:
        track_match = re.match(r"^(\d{1,3})(?:\s|$)", parts[-2])
        if track_match:
            result["artist"] = _clean_filename_segment(parts[0])
            result["album"] = _clean_filename_segment(parts[1]) if len(parts) >= 4 else None
            result["title"] = _clean_filename_segment(parts[-1])
            result["has_track_number"] = True
        elif re.match(r"^\d{1,3}$", parts[0]):
            # Scene / ripper naming: "14-delilah_bon-dead_men_dont_rape"
            result["artist"] = _clean_filename_segment(parts[1])
            result["album"] = _clean_filename_segment(parts[2]) if len(parts) >= 4 else None
            result["title"] = _clean_filename_segment(parts[-1])
            result["has_track_number"] = True
        else:
            result["artist"] = _clean_filename_segment(parts[0])
            result["title"] = _clean_filename_segment(parts[-1])
    elif len(parts) == 2:
        first = parts[0]
        if re.match(r"^\d{1,3}$", first):
            result["title"] = _clean_filename_segment(parts[1])
            result["has_track_number"] = True
        else:
            result["artist"] = _clean_filename_segment(parts[0])
            result["title"] = _clean_filename_segment(parts[1])

    if not result["artist"] and not result["title"]:
        fallback = re.match(r"^(\d{1,3})\s+(.+)", name)
        if fallback:
            result["title"] = _clean_filename_segment(fallback.group(2))
            result["has_track_number"] = True

    if not result["title"]:
        _tail = _TRACK_NUMBER_PREFIX_RE.sub("", name).strip()
        if _tail and _tail != name.strip():
            result["title"] = _clean_filename_segment(_tail)
            result["has_track_number"] = True
        else:
            result["title"] = _clean_filename_segment(name) or None

    if not result["album"]:
        try:
            _parent = filename.replace("\\", "/").rsplit("/", 1)[0] if "/" in filename.replace("\\", "/") else ""
            _folder = _parent.rsplit("/", 1)[-1].strip() if _parent else ""
            if _folder and not re.match(r"^\d{1,3}$", _folder):
                _lower_folder = _folder.lower()
                if _lower_folder not in ("downloads", "music", "inbox", "completed", "torrents", "original"):
                    result["album"] = _clean_filename_segment(_folder)
        except Exception:
            pass

    return result


_SLSKD_PROBLEMATIC_PUNCT_RE = re.compile(r"[\u2018\u2019\u201A\u201B\u2039\u203A'\u201C\u201D\u201E\u201F\u2033\u2036]")
_SLSKD_ALL_PUNCT_RE = re.compile(r"[^\w\s-]")
_SLSKD_FILENAME_EXT_RE = re.compile(r"\.(flac|mp3|m4a|wav|aac|ogg|opus)$", re.IGNORECASE)
_SLSKD_HASH_SUFFIX_RE = re.compile(r"[\s_-]?\d{10,}\s*$")
_SLSKD_TRACKNUM_RE = re.compile(r"\s+-\s+\d{1,3}\s+-\s+")

_DEFAULT_BACKOFF_TIER_HOURS = (4, 12, 24)


def _search_backoff_hours() -> tuple[int, ...]:
    try:
        from helpers.config_helpers import get_config
        raw = (get_config() or {}).get("queue", {}).get("search_backoff_hours") or []
        tiers = tuple(
            max(1, int(h)) for h in raw
            if isinstance(h, (int, float))
            or (isinstance(h, str) and str(h).strip().isdigit())
        )
        return tiers or _DEFAULT_BACKOFF_TIER_HOURS
    except Exception:
        return _DEFAULT_BACKOFF_TIER_HOURS


def _backoff_hours_for(retry_count: int) -> int:
    tiers = _search_backoff_hours()
    idx = max(0, min(int(retry_count or 0), len(tiers) - 1))
    return tiers[idx]


def _pre_release_retry_weekday() -> int:
    try:
        from helpers.config_helpers import get_config
        value = int((get_config() or {}).get("queue", {}).get("pre_release_retry_weekday", 4) or 4)
        return max(0, min(6, value))
    except Exception:
        return 4


def _fmt_local(dt: datetime) -> str:
    try:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M")


def _next_weekday_slot(now_utc: datetime, weekday: int, clock: str) -> datetime:
    try:
        hour, minute = (int(part) for part in clock.split(":", 1))
        hour = max(0, min(23, hour))
        minute = max(0, min(59, minute))
    except Exception:
        hour, minute = 6, 0
    days = (weekday - now_utc.weekday()) % 7
    if days == 0 and (now_utc.hour, now_utc.minute) >= (hour, minute):
        days = 7
    return (now_utc + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)


def _next_pre_release_six_am(now_utc: datetime) -> datetime:
    weekday = _pre_release_retry_weekday()
    return _next_weekday_slot(now_utc, weekday, "06:00")


def _miss_retry_weekday() -> int:
    try:
        from helpers.config_helpers import get_config
        value = int((get_config() or {}).get("queue", {}).get("miss_retry_weekday", 3) or 3)
        return max(0, min(6, value))
    except Exception:
        return 3


def _miss_retry_time() -> str:
    try:
        from helpers.config_helpers import get_config
        raw = str((get_config() or {}).get("queue", {}).get("miss_retry_time", "18:00") or "18:00").strip()
        if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", raw):
            return raw
        return "18:00"
    except Exception:
        return "18:00"


def _next_miss_retry_slot(now_utc: datetime) -> datetime:
    return _next_weekday_slot(now_utc, _miss_retry_weekday(), _miss_retry_time())


def _resolve_item_release_date(item: dict) -> str | None:
    stored = str(item.get("release_date") or "").strip()
    if stored:
        return stored[:10]
    try:
        artist = (item.get("artist") or "").strip()
        album = (item.get("album") or item.get("title") or "").strip()
        if not artist or not album:
            return None
            
        with db_session() as session:
            row = session.execute(
                text("""
                    SELECT release_date FROM upcoming_releases
                    WHERE LOWER(artist_name) = LOWER(:a)
                      AND LOWER(album_name) = LOWER(:b)
                      AND release_date IS NOT NULL
                    ORDER BY release_date LIMIT 1
                """),
                {"a": artist, "b": album},
            ).fetchone()
            if row:
                return str(row._mapping.get("release_date"))[:10]
    except Exception:
        pass
    return None


def _schedule_search_retry(queue_id: int, item: dict, reason: str) -> None:
    from db.repositories.queue import schedule_queue_retry

    artist = (item.get("artist") or "").strip()
    title = (item.get("title") or "").strip()
    release_date = _resolve_item_release_date(item)
    future = False
    if release_date:
        try:
            future = datetime.strptime(release_date, "%Y-%m-%d").date() > datetime.now().date()
        except ValueError:
            future = False

    now_utc = datetime.now(timezone.utc)
    retry_count = int(item.get("retry_count") or 0)
    
    if future:
        next_run = _next_pre_release_six_am(now_utc)
        status = "pending_release"
        _weekday_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        _day_name = _weekday_names[_pre_release_retry_weekday()]
        note = f"{reason} — pre-release, scanning {_day_name} {_fmt_local(next_run)} local"
    else:
        tiers = _search_backoff_hours()
        if retry_count >= len(tiers):
            next_run = _next_miss_retry_slot(now_utc)
            status = "backed_off"
            _weekday_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
            _day_name = _weekday_names[_miss_retry_weekday()]
            note = f"{reason} — persistent miss, retrying {_day_name} {_fmt_local(next_run)} local"
        else:
            hours = _backoff_hours_for(retry_count)
            next_run = now_utc + timedelta(hours=hours)
            status = "backed_off"
            note = f"{reason} — backing off {hours}h until {_fmt_local(next_run)} local (attempt {retry_count + 1})"

    schedule_queue_retry(queue_id, status, next_run.isoformat(), note)
    log_unified(f"[QUEUE] {artist} - {title} → {note}")
    _log_queue_event(status, f"{artist} - {title} → {note}", queue_id)


#: How long a row waits when slskd is disconnected. Without a wait the row
#: was immediately claimable again, so every cycle claimed and skipped the
#: whole batch while slskd was down.
_DISCONNECTED_RETRY_MINUTES = 5


def search_disconnected(item: dict, queue_id: int, artist: str, title: str) -> None:
    try:
        update_queue_item(
            queue_id,
            status="queued",
            failure_reason="slskd disconnected — search not attempted",
            next_retry_at=(
                datetime.now(timezone.utc) + timedelta(minutes=_DISCONNECTED_RETRY_MINUTES)
            ).isoformat(),
        )
    except Exception:
        pass
    log_unified(f"[QUEUE] {artist} - {title} → slskd disconnected, search skipped")
    _log_queue_event("queued", f"{artist} - {title} → slskd disconnected, search skipped", queue_id)


def _sanitize_slskd_query(query: str) -> str:
    if not query:
        return query
    cleaned = _SLSKD_PROBLEMATIC_PUNCT_RE.sub("", query)
    cleaned = _SLSKD_FILENAME_EXT_RE.sub("", cleaned)
    cleaned = _SLSKD_HASH_SUFFIX_RE.sub("", cleaned)
    cleaned = _SLSKD_TRACKNUM_RE.sub(" - ", cleaned)
    cleaned = cleaned.replace("\\u0026", " ").replace("&amp;", " ").replace("&", " ")
    return " ".join(cleaned.split())


def _strip_all_query_punctuation_for_slskd(query: str) -> str:
    if not query:
        return query
    cleaned = _SLSKD_ALL_PUNCT_RE.sub("", query)
    return " ".join(cleaned.split())


def build_search_query(item: dict) -> str:
    from helpers.config_helpers import _FEAT_SUFFIX_RE, _GENERIC_COMPILATION_ARTISTS

    artist = (item.get("artist") or "").strip()
    title = (item.get("title") or "").strip()
    stored = (item.get("search_query") or "").strip()

    if artist.lower() in _GENERIC_COMPILATION_ARTISTS:
        query = title or stored
    else:
        clean_artist = _FEAT_SUFFIX_RE.sub("", artist).strip()
        if stored:
            query = stored
        elif clean_artist:
            query = f"{clean_artist} - {title}"
        else:
            query = title

    return _sanitize_slskd_query(query)


def _search_query_wait(
    *,
    idx: int,
    consecutive_empty: int,
    long_first_wait: bool,
    primary_wait: int,
    fallback_wait: int,
    shrink_after: int,
    shrunken_wait: int,
) -> int:
    wait = primary_wait if (idx == 0 and long_first_wait) else fallback_wait
    if shrink_after > 0 and consecutive_empty >= shrink_after and shrunken_wait > 0:
        wait = min(wait, shrunken_wait)
    return max(1, int(wait))


def _build_fallback_search_queries(item: dict, primary_query: str) -> list[str]:
    from helpers.config_helpers import _FEAT_SUFFIX_RE
    from helpers.normalization_service import is_track_artist_placeholder, strip_brackets

    artist = (item.get("artist") or "").strip()
    album_artist = (item.get("album_artist") or "").strip()
    title = (item.get("title") or "").strip()

    if is_track_artist_placeholder(album_artist):
        album_artist = ""

    if not title:
        return []

    fallbacks: list[str] = []
    seen: set[str] = {primary_query}

    def _add(query: str) -> None:
        query = _sanitize_slskd_query(query)
        if query and query not in seen:
            seen.add(query)
            fallbacks.append(query)

    core_title = strip_brackets(title).strip()
    if core_title and core_title.lower() != title.lower():
        if artist:
            _add(f"{artist} - {core_title}")
        if album_artist and album_artist.lower() != artist.lower():
            _add(f"{album_artist} - {core_title}")
        _add(core_title)

    if album_artist and album_artist.lower() != artist.lower():
        _add(f"{album_artist} - {title}")

    feat_stripped = _FEAT_SUFFIX_RE.sub("", artist).strip()
    if feat_stripped and feat_stripped.lower() != artist.lower():
        _add(f"{feat_stripped} - {title}")

    _ARTICLE_WORDS = {"the", "a", "an"}
    effective_artist = feat_stripped or artist
    first_word = ""
    for word in effective_artist.split():
        if word.lower() not in _ARTICLE_WORDS:
            first_word = word
            break
            
    if not first_word and effective_artist.split():
        first_word = effective_artist.split()[0]
        
    if first_word and first_word.lower() != effective_artist.lower():
        _add(f"{first_word} - {title}")

    def _drop_words(words: list[str], keep_at_least: int = 1) -> list[str]:
        phrases: list[str] = []
        seen_phrases: set[str] = set()
        n = len(words)
        if n <= keep_at_least:
            return phrases
            
        for drop_count in range(1, n - keep_at_least + 1):
            from itertools import combinations
            for combo in combinations(range(n), drop_count):
                kept = [w for i, w in enumerate(words) if i not in combo]
                phrase = " ".join(kept).strip()
                key = phrase.casefold()
                if phrase and key not in seen_phrases:
                    seen_phrases.add(key)
                    phrases.append(phrase)
        return phrases

    artist_words = [w for w in effective_artist.split() if w.lower() not in _ARTICLE_WORDS]
    title_words = [w for w in title.split()]

    _MAX_FALLBACKS = 8

    if len(artist_words) > 1:
        for a_phrase in _drop_words(artist_words)[:4]:
            _add(f"{a_phrase} - {title}")

    if len(title_words) > 1:
        for t_phrase in _drop_words(title_words)[:4]:
            _add(f"{effective_artist} - {t_phrase}")

    _add(title)

    stripped_all = _strip_all_query_punctuation_for_slskd(primary_query)
    if stripped_all:
        _add(stripped_all)

    return fallbacks[:_MAX_FALLBACKS]


def _year_mismatch_rejects(filename: str, expected_year: Any) -> bool:
    if not expected_year:
        return False
    year_str = re.search(r"\d{4}", str(expected_year))
    if not year_str:
        return False
    queue_year = int(year_str.group(0))
    path_year_match = re.search(r"[\(\[]((?:19|20)\d{2})[\)\]]", str(filename or ""))
    if not path_year_match:
        return False
    path_year = int(path_year_match.group(1))
    return abs(queue_year - path_year) > 1


def _note_reject(rejects: dict[str, int] | None, reason: str) -> None:
    if rejects is not None:
        rejects[reason] = rejects.get(reason, 0) + 1


def _queue_item_is_compilation(
    *,
    expected_artist: str,
    expected_album_artist: str | None,
) -> bool:
    try:
        from helpers.config_helpers import _GENERIC_COMPILATION_ARTISTS
        from helpers.normalization_service import normalize_artist
    except Exception:
        return False

    for value in (expected_album_artist, expected_artist):
        name = normalize_artist(value or "")
        if not name:
            continue
        if name in _GENERIC_COMPILATION_ARTISTS:
            return True
    return False


def _score_result(
    result: dict[str, Any],
    expected_artist: str,
    expected_title: str,
    expected_album: str | None = None,
    expected_duration: int | None = None,
    expected_year: Any = None,
    rejects: dict[str, int] | None = None,
    *,
    expected_album_artist: str | None = None,
) -> float:
    score = 0.0
    filename = str(result.get("filename", ""))
    parts = _parse_filename_parts(filename)

    if _year_mismatch_rejects(filename, expected_year):
        logger.debug("Rejected candidate — year mismatch", filename=filename[:180], expected_year=expected_year)
        _note_reject(rejects, "year_mismatch")
        return 0.0

    exp_artist = _sanitize_slskd_query(expected_artist or "")
    exp_title = _sanitize_slskd_query(expected_title or "")
    exp_segments = [p.strip() for p in re.split(r"\s*[-–]\s*", exp_title) if p.strip()] if exp_title else []
    
    if len(exp_segments) >= 2:
        exp_title = exp_segments[-1]
        if not exp_artist or exp_artist.lower() in ("unknown", "unidentified", "unidentified artist"):
            exp_artist = exp_segments[0]
            
    expected_artist = exp_artist or expected_artist
    expected_title = exp_title or expected_title
    if expected_album:
        expected_album = _sanitize_slskd_query(expected_album) or expected_album

    from helpers.config_helpers import _FEAT_SUFFIX_RE
    credit_artist = _FEAT_SUFFIX_RE.sub("", expected_artist or "").strip() or expected_artist

    art_score = _similarity(str(parts.get("artist") or ""), credit_artist)
    if art_score > 0.7:
        score += 30 * min(1.0, art_score)
    elif _normalise(credit_artist) in _normalise(filename):
        score += 20

    parsed_artist = str(parts.get("artist") or "")
    parsed_title = str(parts.get("title") or "")
    parsed_album_raw = str(parts.get("album") or "")

    try:
        from helpers.normalization_service import (
            edition_annotations_compatible,
            extract_version_info,
        )

        if parsed_title and expected_title and not edition_annotations_compatible(parsed_title, expected_title):
            logger.debug(
                "Rejected candidate — edition annotation mismatch",
                filename=filename[:180],
                expected_title=expected_title,
                parsed_title=parsed_title,
            )
            _note_reject(rejects, "edition_mismatch")
            return 0.0

        _HARD_VARIANT_TOKENS = frozenset({
            "live", "remix", "instrumental", "acoustic", "demo", "karaoke", "unplugged",
        })
        _, _exp_title_v = extract_version_info(expected_title or "")
        _, _exp_album_v = extract_version_info(expected_album or "")
        _exp_variants = (_exp_title_v | _exp_album_v) & _HARD_VARIANT_TOKENS

        _, _cand_title_v = extract_version_info(parsed_title)
        _, _cand_album_v = extract_version_info(parsed_album_raw)
        # Only inherit album-level hard variants that describe performance form
        # (e.g. "Live At 25", "MTV Unplugged") so a remix/acoustic track on a
        # studio album isn't misclassified, while tracks inside a Live album folder
        # are rejected when a studio version was requested.
        _ALBUM_FORM_VARIANTS = frozenset({"live", "unplugged", "karaoke", "instrumental"})
        _cand_variants = (_cand_title_v & _HARD_VARIANT_TOKENS) | (_cand_album_v & _ALBUM_FORM_VARIANTS)

        _unrequested_variants = _cand_variants - _exp_variants
        if _unrequested_variants:
            logger.debug(
                "Rejected candidate — unrequested variant",
                filename=filename[:180],
                expected_title=expected_title,
                parsed_title=parsed_title,
                unrequested_variants=sorted(_unrequested_variants),
            )
            _note_reject(rejects, "variant_mismatch")
            return 0.0
    except Exception:
        pass

    _HANGUL_CJK_RE = re.compile(r"[\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F\u4E00-\u9FFF\u3040-\u30FF]")
    _artist_script_ambiguous = bool(
        _HANGUL_CJK_RE.search(expected_artist or "")
        or _HANGUL_CJK_RE.search(parsed_artist or "")
    )

    _title_core_match = False
    _title_core_score = 0.0
    try:
        from helpers.normalization_service import normalize_core_title as _nct_title
        _cand_core = _nct_title(parsed_title or "")
        _exp_core = _nct_title(expected_title or "")
        if _cand_core and _exp_core:
            _title_core_score = _similarity(_cand_core, _exp_core)
            _title_core_match = (
                _title_core_score >= 0.6
                or _exp_core in _cand_core
                or _cand_core in _exp_core
            )
    except Exception:
        pass

    artist_evidenced = True
    if expected_artist and expected_artist.lower() not in {
        "unknown", "unidentified", "unidentified artist", "various", "various artists", "-",
    }:
        gate_artist = credit_artist
        norm_gate = _normalise(gate_artist)
        
        norm_filename = _normalise(filename)
        path_scope = norm_filename
        if parsed_title:
            path_scope = path_scope.replace(_normalise(parsed_title), " ")
            
        artist_scope = f"{parsed_artist} {path_scope}"
        artist_scope_tokens = set(re.findall(r"[a-z0-9]+", _normalise(artist_scope)))
        significant_words = [
            w for w in re.findall(r"[a-z0-9]+", norm_gate)
            if len(w) >= 4
        ]
        artist_words_3plus = [
            w for w in re.findall(r"[a-z0-9]+", norm_gate)
            if len(w) >= 3
        ]

        if _artist_script_ambiguous:
            artist_evidenced = (
                art_score >= 0.4
                or _title_core_match
                or norm_gate in norm_filename
                or _normalise(parsed_artist) in norm_filename
                or (
                    _HANGUL_CJK_RE.search(norm_gate or "")
                    and _normalise(gate_artist) in norm_filename
                )
            )
        else:
            artist_evidenced = (
                art_score >= 0.6
                or norm_gate in _normalise(artist_scope)
                or (len(significant_words) >= 2 and all(w in artist_scope_tokens for w in significant_words))
                or (len(artist_words_3plus) >= 2 and all(w in artist_scope_tokens for w in artist_words_3plus))
                or (
                    len(significant_words) == 1
                    and len(artist_words_3plus) == 1
                    and significant_words[0] in artist_scope_tokens
                )
            )
        
    if not artist_evidenced:
        logger.debug("Rejected candidate — no artist evidence", filename=filename[:180], expected_artist=expected_artist)
        _note_reject(rejects, "no_artist_evidence")
        return 0.0

    if _artist_script_ambiguous and art_score <= 0.4:
        _artist_credit = 0.0
        if _normalise(parsed_artist or "") == _normalise(expected_artist or ""):
            _artist_credit = 20.0
        elif (
            _normalise(expected_artist or "") in _normalise(filename)
            or _normalise(parsed_artist or "") in _normalise(filename)
            or _title_core_match
        ):
            _artist_credit = 15.0
        score += _artist_credit

    title_score = _similarity(str(parts.get("title") or ""), expected_title)
    exp_title_words = re.findall(r"[a-z0-9]+", _normalise(expected_title))
    exp_word_count = len(exp_title_words)
    cand_title_words = re.findall(r"[a-z0-9]+", _normalise(str(parts.get("title") or "")))
    
    if title_score >= 0.95 and cand_title_words and len(cand_title_words) > 2 * max(1, exp_word_count):
        title_score = 0.6

    title_ok = title_score >= 0.35

    # Guard against low-similarity SequenceMatcher false positives (e.g.
    # "i could give you (a mirror)" vs "would i lie to you" scoring 0.49 with
    # only stop-words in common). When similarity is below 0.72 and core titles
    # do not match, require at least one shared meaningful word.
    if title_ok and title_score < 0.72 and not _title_core_match:
        _TITLE_STOP_WORDS = {
            "the", "and", "for", "with", "you", "your", "from", "that", "this",
            "are", "was", "not", "but", "out", "all", "can", "who", "how",
            "why", "what", "when", "where", "into", "over",
        }
        _exp_meaningful = {w for w in exp_title_words if len(w) >= 3 and w not in _TITLE_STOP_WORDS}
        _cand_meaningful = {w for w in cand_title_words if len(w) >= 3 and w not in _TITLE_STOP_WORDS}
        if _exp_meaningful:
            if not (_exp_meaningful & _cand_meaningful):
                title_ok = False
        elif exp_title_words:
            if not (set(exp_title_words) & set(cand_title_words)):
                title_ok = False

    if not title_ok:
        if _title_core_match:
            title_score = max(title_score, _title_core_score if _title_core_score >= 0.6 else 0.7)
            title_ok = True
        if not title_ok and re.search(r"[\uAC00-\uD7AF\u4E00-\u9FFF]", expected_title or ""):
            if _normalise(expected_title) in _normalise(filename):
                title_ok = True
                title_score = max(title_score, 0.7)

    if not title_ok:
        logger.debug(
            "Rejected candidate — title mismatch",
            filename=filename[:180], expected_title=expected_title,
            parsed_title=str(parts.get("title") or ""), title_score=round(title_score, 2),
        )
        _note_reject(rejects, "title_mismatch")
        return 0.0

    if title_score > 0.7:
        score += 25 * min(1.0, title_score)
    elif _normalise(expected_title) in _normalise(filename):
        score += 15

    if expected_album:
        parsed_album = str(parts.get("album") or "").strip()
        album_score = _similarity(str(parts.get("album") or ""), expected_album)
        if parsed_album:
            album_mismatch = (
                album_score < 0.6
                and _normalise(expected_album) not in _normalise(filename)
                and not _normalise(parsed_album) in _normalise(expected_album)
            )
            compilation_context = _queue_item_is_compilation(
                expected_artist=expected_artist,
                expected_album_artist=expected_album_artist,
            )
            if (
                album_mismatch
                and not _title_core_match
                and (
                    (not compilation_context and title_score < 0.85)
                    or (compilation_context and title_score < 0.75)
                )
            ):
                logger.debug(
                    "Rejected candidate — album mismatch",
                    filename=filename[:180],
                    expected_album=expected_album,
                    parsed_album=parsed_album,
                    album_score=round(album_score, 2),
                    title_score=round(title_score, 2),
                )
                _note_reject(rejects, "album_mismatch")
                return 0.0

        if album_score > 0.6:
            score += 20 * min(1.0, album_score)
        elif _normalise(expected_album) in _normalise(filename):
            score += 10

    if expected_duration and result.get("length_seconds"):
        try:
            _cand_seconds = float(result["length_seconds"])
            _want_seconds = float(expected_duration)
        except (TypeError, ValueError):
            _cand_seconds = _want_seconds = 0.0

        if _cand_seconds > 0 and _want_seconds > 0:
            _length_tol = _length_tolerance_seconds()
            _length_diff = abs(_want_seconds - _cand_seconds)

            if _length_diff > _length_tol:
                logger.debug(
                    "Rejected candidate — length mismatch",
                    filename=filename[:180],
                    expected_seconds=round(_want_seconds, 1),
                    found_seconds=round(_cand_seconds, 1),
                    tolerance=_length_tol,
                )
                _note_reject(rejects, "length_mismatch")
                return 0.0

            dur_ratio = min(_want_seconds, _cand_seconds) / max(_want_seconds, _cand_seconds)
            if dur_ratio >= 0.9:
                score += 15 * dur_ratio

    bitrate = int(result.get("bitrate", 0) or 0)
    ext = (result.get("extension") or parts.get("format") or "").lower()

    is_lossless = bool(result.get("is_lossless")) or ext in ("flac", "wav", "aiff", "alac")
    if is_lossless:
        score += 10
    elif bitrate >= 320:
        score += 5
    elif 0 < bitrate < 192:
        score -= 10

    bit_depth = result.get("bit_depth")
    if bit_depth and is_lossless and int(bit_depth) >= 24:
        score += 3

    if result.get("has_free_upload_slot"):
        score += 3

    qlen = result.get("queue_length")
    if qlen is not None:
        try:
            qlen = int(qlen)
            if qlen == 0:
                score += 5
            elif qlen <= 5:
                score += 3
            elif qlen > 50:
                score -= 2
        except (TypeError, ValueError):
            pass

    uspeed = result.get("upload_speed")
    if uspeed is not None:
        try:
            if int(uspeed) > 1_000_000:
                score += 3
        except (TypeError, ValueError):
            pass

    return round(score, 1)


def _select_best_result(
    results: list[dict[str, Any]],
    expected_artist: str,
    expected_title: str,
    expected_album: str | None = None,
    expected_duration: int | None = None,
    expected_year: Any = None,
    min_score: float = 45.0,
    *,
    expected_album_artist: str | None = None,
    miss_sink: list[dict[str, Any]] | None = None,
    qualifying_sink: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Score ``results`` and return the best one at or above ``min_score``.

    ``qualifying_sink`` (optional) receives EVERY result at or above
    ``min_score``, best first — the only safe fallback candidates if the best
    one cannot be downloaded.
    """
    scored: list[tuple[float, dict]] = []
    rejects: dict[str, int] = {}

    for r in results:
        s = _score_result(
            r, expected_artist, expected_title, expected_album,
            expected_duration, expected_year,
            rejects=rejects,
            expected_album_artist=expected_album_artist,
        )
        scored.append((s, r))

    scored.sort(key=lambda pair: -pair[0])
    if qualifying_sink is not None:
        qualifying_sink.extend(r for s, r in scored if s >= min_score)

    if scored and scored[0][0] >= min_score:
        best_score, best = scored[0]
        logger.info(
            "Selected download candidate",
            artist=expected_artist,
            title=expected_title,
            album=expected_album,
            score=round(float(best_score), 1),
            filename=str(best.get("filename") or "")[:200],
            candidates=len(scored),
        )
        return best

    top_score, top = scored[0] if scored else (0.0, {})
    below_floor = sum(1 for s, _ in scored if 0 < s < min_score)
    if below_floor:
        rejects["below_floor"] = below_floor
    miss = {
        "min_score": min_score,
        "top_score": round(float(top_score), 1),
        "candidates": len(scored),
        "rejected": dict(rejects),
        "top_candidate": str(top.get("filename") or "")[:200],
    }
    if miss_sink is not None:
        miss_sink.append(miss)
        logger.debug(
            "Query yielded no qualifying candidate",
            artist=expected_artist,
            title=expected_title,
            top_score=miss["top_score"],
            candidates=miss["candidates"],
        )
        return None
    logger.warning(
        "No result met min_score — no qualifying download candidate",
        artist=expected_artist,
        title=expected_title,
        album=expected_album,
        min_score=min_score,
        top_score=round(float(top_score), 1),
        candidates=len(scored),
        rejected=rejects,
        top_candidate=str(top.get("filename") or "")[:200],
    )
    return None


def _warn_closest_search_miss(
    misses: list[dict[str, Any]],
    *,
    artist: str,
    title: str,
    album: str | None,
    queries_tried: int,
) -> dict[str, Any] | None:
    if not misses:
        return None
    best_miss = max(
        misses,
        key=lambda m: (int(m.get("candidates") or 0), float(m.get("top_score") or 0.0)),
    )
    if not int(best_miss.get("candidates") or 0):
        return None
    logger.warning(
        "No result met min_score — no qualifying download candidate",
        artist=artist,
        title=title,
        album=album,
        min_score=best_miss.get("min_score"),
        top_score=best_miss.get("top_score"),
        candidates=best_miss.get("candidates"),
        rejected=best_miss.get("rejected"),
        top_candidate=best_miss.get("top_candidate"),
        queries_tried=queries_tried,
    )
    return best_miss


def _attempt_candidate_downloads(
    slskd: Any,
    candidates: list[dict[str, Any]],
    queue_id: int,
    expected_artist: str,
    expected_title: str,
) -> tuple[bool, dict[str, Any] | None, str]:
    success = False
    chosen = None
    fail_reason = "download_failed"
    for candidate in candidates:
        if not _queue_item_exists(queue_id):
            logger.info("Queue item was removed while searching", queue_id=queue_id)
            return False, None, "item_removed"
        if not candidate.get("has_free_upload_slot", True):
            _block_peer(candidate.get("username"), candidate.get("filename"))
            fail_reason = "peer_no_free_slots"
            continue

        success = slskd.download_file(
            candidate["username"],
            candidate["filename"],
            size=int(candidate.get("size") or (candidate.get("size_mb", 0) * 1024 * 1024)),
        )
        if success:
            chosen = candidate
            break

        _block_peer(candidate.get("username"), candidate.get("filename"))
        fail_reason = "download_failed"
        log_unified(
            f"[QUEUE] {expected_artist} - {expected_title} → peer {candidate.get('username')} "
            f"failed ({fail_reason}), trying next candidate…"
        )
    return success, chosen, fail_reason


def process_queue_item(item: dict, slskd: SlskdService) -> dict:
    queue_id = item.get("id")
    logger.debug("Processing queue item", queue_id=queue_id)

    if not queue_id:
        logger.error("Missing queue_id in item", item=item)
        return {"success": False, "error": "missing_queue_id"}

    if item.get("file_path"):
        try:
            update_queue_item(queue_id, status="unmatched", failure_reason="local file — no download needed")
        except Exception:
            pass
        logger.info("Queue item has a local file_path — skipping Soulseek search", queue_id=queue_id, file_path=item.get("file_path"))
        return {"success": False, "status": "local_file_skip", "skipped": True}

    try:
        from services.queue_cleaner import clean_mangled_queue_item
        item = clean_mangled_queue_item(item)
    except Exception as exc:
        logger.debug("Pre-search cleaner skipped", error=str(exc))

    # Library reuse is handled BEFORE this pipeline runs — see
    # ``services.downloads.library_copy_service`` and
    # ``queue_processing_service.process_queue_item``. It has to happen before
    # the Soulseek availability gate, and it enforces the real quality gate;
    # the old copy attempted here duplicated it and only checked an album-name
    # format marker.

    expected_artist = (item.get("artist") or "").strip()
    expected_title = (item.get("title") or "").strip()
    expected_album = (item.get("album") or "").strip() or None
    expected_album_artist = (item.get("album_artist") or "").strip() or None
    expected_year = item.get("year") or item.get("release_year")
    expected_duration = None
    if item.get("duration"):
        expected_duration = queue_duration_seconds(item.get("duration"))

    query = build_search_query(item)
    fallback_queries = _build_fallback_search_queries(item, query)
    started_at = time.time()

    try:
        # The row stays in the CLAIM status ('searching'). ``mark_processing``
        # moved it to 'processing', which the in-flight count does not
        # include — so the orchestrator could start more searches than
        # ``max_in_flight`` allowed — and which the orchestrator's
        # "processor left it claimed" check could not recognise.
        update_queue_item(queue_id, status="searching")
        _log_queue_event("searching", f"{expected_artist} - {expected_title} → searching Soulseek", queue_id)

        from helpers.config_helpers import (
            get_queue_search_tuning,
            get_search_quality_config,
        )

        _quality_cfg = get_search_quality_config()
        floor_bitrate = int(_quality_cfg.get("min_bitrate") or 192)
        fallback_bitrate = int(_quality_cfg.get("fallback_min_bitrate") or 128)
        allow_low_quality_fallback = bool(_quality_cfg.get("allow_low_quality_fallback", False))

        _search_tuning = get_queue_search_tuning()
        _primary_wait = int(_search_tuning.get("search_max_wait_seconds") or 0)
        _fallback_wait = int(_search_tuning.get("fallback_search_max_wait_seconds") or 0)
        _empty_abort = float(_search_tuning.get("search_empty_abort_seconds") or 0)
        _shrink_after = int(_search_tuning.get("search_empty_shrink_after") or 0)
        _shrunken_wait = int(_search_tuning.get("search_shrunken_wait_seconds") or 0)

        best = None
        all_results: list[dict[str, Any]] = []
        searched_queries: list[str] = []
        used_low_quality_fallback = False
        results: list[dict[str, Any]] = []
        _misses: list[dict[str, Any]] = []
        _qualifying: list[dict[str, Any]] = []
        _rejected_dropped = 0

        bitrate_tiers: list[tuple[int, bool]] = [(floor_bitrate, True)]
        if allow_low_quality_fallback and fallback_bitrate < floor_bitrate:
            bitrate_tiers.append((fallback_bitrate, False))

        consecutive_empty = 0
        for tier_index, (tier_bitrate, long_first_wait) in enumerate(bitrate_tiers):
            for idx, q in enumerate([query] + fallback_queries):
                searched_queries.append(q)
                wait_seconds = _search_query_wait(
                    idx=idx,
                    consecutive_empty=consecutive_empty,
                    long_first_wait=long_first_wait,
                    primary_wait=_primary_wait,
                    fallback_wait=_fallback_wait,
                    shrink_after=_shrink_after,
                    shrunken_wait=_shrunken_wait,
                )
                
                # Ensure we safely clean up orphaned searches if slskd has old ones
                results = slskd.search_and_filter(
                    q,
                    min_bitrate=tier_bitrate,
                    wait_seconds=wait_seconds,
                    empty_abort_seconds=_empty_abort,
                    cancel_when_done=True,
                )
                if results is None:
                    search_disconnected(item, queue_id, expected_artist, expected_title)
                    return {"success": False, "status": "slskd_disconnected", "skipped": True}
                results = _filter_blocked_peers(results)
                # Files already rejected for THIS row (survives restarts and
                # does not expire): never select them again.
                results, _dropped = _filter_rejected_for_item(results, item)
                _rejected_dropped += _dropped
                all_results.extend(results)
                consecutive_empty = consecutive_empty + 1 if not results else 0

                best = _select_best_result(
                    results,
                    expected_artist=expected_artist,
                    expected_title=expected_title,
                    expected_album=expected_album,
                    expected_album_artist=expected_album_artist,
                    expected_duration=expected_duration,
                    expected_year=expected_year,
                    miss_sink=_misses,
                    qualifying_sink=_qualifying,
                )
                if best:
                    used_low_quality_fallback = tier_index > 0
                    break
            if best:
                break

        elapsed = round(time.time() - started_at, 1)

        if _rejected_dropped:
            logger.info(
                "Skipped candidates already rejected for this queue item",
                queue_id=queue_id,
                skipped=_rejected_dropped,
            )

        if not all_results:
            _log_search_event(
                search_type="automatic",
                query=query,
                queue_id=queue_id,
                item=item,
                result_count=0,
                duration_seconds=elapsed,
                notes="no_results",
            )
            log_unified(f"[QUEUE] {expected_artist} - {expected_title} → failed: no_results ({elapsed:.0f}s)")
            _log_queue_event("failed", f"{expected_artist} - {expected_title} → failed: no_results ({elapsed:.0f}s)", queue_id)
            _schedule_search_retry(queue_id, item, f"no_results ({elapsed:.0f}s)")
            return {"success": False, "status": "no_results"}

        if not best:
            logger.info("No qualifying result for queue item", queue_id=queue_id, artist=expected_artist, title=expected_title)
            _best_miss = _warn_closest_search_miss(
                _misses,
                artist=expected_artist,
                title=expected_title,
                album=expected_album,
                queries_tried=len(searched_queries),
            )
            # Carry WHY the candidates failed into the search event itself.
            # Otherwise "no_qualifying_result: candidates=112" is indistinguishable
            # from a parsing bug: the reject breakdown lives only in the warning
            # line, which is easy to miss in a busy log.
            _miss_note = ""
            if _best_miss:
                _miss_note = (
                    f", top_score={_best_miss.get('top_score')}"
                    f", rejected={_best_miss.get('rejected')}"
                )
            _log_search_event(
                search_type="automatic",
                query=query,
                queue_id=queue_id,
                item=item,
                result_count=len(all_results),
                duration_seconds=elapsed,
                notes=(
                    f"no_qualifying_result: candidates={len(all_results)}, "
                    f"queries={len(searched_queries)}{_miss_note}"
                ),
                selected_result=best,
                results=all_results,
            )
            log_unified(f"[QUEUE] {expected_artist} - {expected_title} → failed: no_qualifying_result ({len(all_results)} candidates)")
            _log_queue_event("failed", f"{expected_artist} - {expected_title} → failed: no_qualifying_result ({len(all_results)} candidates)", queue_id)
            _schedule_search_retry(queue_id, item, f"no_qualifying_result ({len(all_results)} candidates)")
            return {"success": False, "status": "no_qualifying_result"}

        if not _queue_item_exists(queue_id):
            logger.info("Queue item was removed while searching", queue_id=queue_id)
            return {"success": False, "status": "item_removed"}

        update_queue_item(queue_id, status="downloading")
        # Fallback candidates are ONLY other results that met ``min_score``.
        # The old code took the next raw search results UNSCORED, so when the
        # best peer failed it could download a different song, a remix or
        # another artist — which then failed verification and was deleted.
        candidates: list[dict[str, Any]] = [best]
        _best_key = _peer_key(best.get("username"), best.get("filename"))
        for r in _qualifying:
            if len(candidates) >= 3:
                break
            if _peer_key(r.get("username"), r.get("filename")) != _best_key and r not in candidates:
                candidates.append(r)

        success, chosen, fail_reason = _attempt_candidate_downloads(
            slskd, candidates, queue_id, expected_artist, expected_title
        )

        if not success and fail_reason == "item_removed":
            return {"success": False, "status": "item_removed"}

        if not success:
            _log_search_event(
                search_type="automatic",
                query=query,
                queue_id=queue_id,
                item=item,
                result_count=len(results),
                duration_seconds=elapsed,
                notes=fail_reason,
                selected_result=chosen or best,
                results=results,
            )
            log_unified(f"[QUEUE] {expected_artist} - {expected_title} → failed: {fail_reason}")
            _log_queue_event("failed", f"{expected_artist} - {expected_title} → failed: {fail_reason}", queue_id)
            mark_failed(queue_id, fail_reason)
            return {"success": False, "status": fail_reason}

        _stored_filename = str(chosen.get("filename") or "").replace("\\", "/").strip()
        update_queue_item(
            queue_id,
            found_filename=_stored_filename,
            status="downloading",
        )
        
        _fallback_note = " (low-quality fallback)" if used_low_quality_fallback else ""
        log_unified(
            f"[QUEUE] {expected_artist} - {expected_title} → downloading from {chosen.get('username')} "
            f"({_stored_filename}){_fallback_note}"
        )
        _log_search_event(
            search_type="automatic",
            query=query,
            queue_id=queue_id,
            item=item,
            result_count=len(results),
            duration_seconds=elapsed,
            notes="download_started" + _fallback_note,
            selected_result=chosen or best,
            results=results,
        )
        _log_queue_event(
            "downloading",
            f"{expected_artist} - {expected_title} → downloading from {chosen.get('username')} ({_stored_filename}){_fallback_note}",
            queue_id,
        )

        return {
            "success": True,
            "status": "downloading",
            "query": query,
            "match": best,
        }

    except Exception as e:
        logger.error("Error processing queue item", queue_id=queue_id, error=str(e), exc_info=True)
        log_unified(f"[QUEUE] {expected_artist} - {expected_title} → failed: {e}")
        _log_queue_event("failed", f"{expected_artist} - {expected_title} → failed: {e}", queue_id)
        mark_failed(queue_id, str(e))
        return {"success": False, "error": str(e)}


def _log_search_event(
    *,
    search_type: str,
    query: str,
    queue_id: int | None,
    item: dict,
    result_count: int,
    duration_seconds: float | None = None,
    notes: str | None = None,
    selected_result: dict | None = None,
    results: list | None = None,
) -> None:
    try:
        from db.repositories.search_logs import log_slskd_search
        log_slskd_search(
            search_type=search_type,
            query=query,
            queue_id=queue_id,
            artist=(item.get("artist") or "").strip(),
            title=(item.get("title") or "").strip(),
            album=(item.get("album") or "").strip(),
            result_count=result_count,
            duration_seconds=duration_seconds,
            notes=notes,
            selected_result=selected_result,
            results=results,
        )
    except Exception as exc:
        logger.debug("Could not log slskd search event", error=str(exc))

    try:
        artist = (item.get("artist") or "").strip()
        title = (item.get("title") or "").strip()
        context = " | ".join(p for p in (artist, title) if p)
        suffix = f" ({notes})" if notes else ""
        log_search(
            f"[{search_type.upper()}] {query}"
            + (f" :: {context}" if context else "")
            + f" → {result_count} results in {round(duration_seconds or 0, 1)}s{suffix}"
        )
    except Exception:
        pass


def _log_queue_event(event_type: str, message: str, queue_id: int | None) -> None:
    try:
        from services.queue.queue_diagnostics_service import log_queue_event
        log_queue_event(event_type, message, queue_id=queue_id)
    except Exception:
        pass


def _queue_item_exists(queue_id: int | None) -> bool:
    if queue_id is None:
        return False
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT 1 FROM download_queue WHERE id = :qid"),
                {"qid": int(queue_id)},
            )
            return result.fetchone() is not None
    except Exception as exc:
        logger.debug("Item existence check failed", queue_id=queue_id, error=str(exc))
        return True


def run_pipeline(slskd: SlskdService, limit: int = 10) -> Dict[str, Any]:
    queue = get_ready_for_processing(limit)

    results = {
        "processed": 0,
        "success": 0,
        "failed": 0,
        "details": [],
    }

    for item in queue:
        result = process_queue_item(item, slskd)

        results["processed"] += 1
        results["details"].append(result)

        if result.get("success"):
            results["success"] += 1
        else:
            results["failed"] += 1

    return results


def sync_transfers(slskd: SlskdService) -> Dict[str, Any]:
    transfers = slskd.get_active_downloads()
    updated = 0

    for t in transfers:
        try:
            queue_id = t.get("queue_id")
            if not queue_id:
                continue

            update_queue_item(
                queue_id,
                status="downloading",
                progress=t.get("progress"),
                speed=t.get("speed"),
            )
            updated += 1
        except Exception as e:
            logger.debug("Failed to update transfer", error=str(e))

    return {
        "success": True,
        "updated": updated,
        "total": len(transfers),
    }


def transfer_and_verify_download(
    source_path: str,
    dest_path: str,
    queue_id: int | None = None,
    *,
    convert_flac_to_mp3: bool = False,
    mp3_bitrate: int = 320,
    conversion_target: str = "mp3",
) -> dict[str, Any]:
    import subprocess
    from services.downloads.download_verification_service import (
        mark_queue_item_moved,
        verify_file_in_music,
    )
    from services.infrastructure.filesystem_service import transfer_download_to_music

    final_dest = dest_path
    source_ext = os.path.splitext(source_path)[1].lower()
    target_format = conversion_target if conversion_target in ("mp3", "m4a") else "mp3"

    if convert_flac_to_mp3 and source_ext == ".flac":
        final_dest = os.path.splitext(dest_path)[0] + f".{target_format}"
        os.makedirs(os.path.dirname(final_dest), exist_ok=True)
        codec_args = (
            ["-c:a", "aac", "-b:a", f"{mp3_bitrate}k"]
            if target_format == "m4a"
            else ["-c:a", "libmp3lame", "-b:a", f"{mp3_bitrate}k", "-id3v2_version", "3"]
        )
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", source_path, *codec_args, final_dest],
                capture_output=True, timeout=300, check=True,
            )
            logger.info(
                "Converted FLAC to %s" % target_format.upper(),
                source=source_path, dest=final_dest,
            )
            try:
                os.remove(source_path)
            except Exception:
                pass
        except Exception as exc:
            logger.error(
                "FLAC to %s conversion failed" % target_format.upper(),
                source=source_path, error=str(exc),
            )
            return {"success": False, "error": f"Conversion failed: {exc}"}
    else:
        tr = transfer_download_to_music(source_path, final_dest)
        if not tr.get("success"):
            return tr
        final_dest = tr.get("target_path", final_dest)

    if queue_id:
        mark_queue_item_moved(queue_id, final_dest)

    return verify_file_in_music(queue_id or 0, final_dest)


def start_release_download(release_id: Any, release_title: str, artist: str, method: str = 'slskd', create_folder_group: bool = True) -> dict[str, Any]:
    try:
        logger.info("Starting release download", release_id=release_id, artist=artist, title=release_title)

        resolved_release_id = resolve_release_id(release_id)
        mb_data = fetch_musicbrainz_release_metadata(resolved_release_id)

        if not mb_data:
            mb_data = fetch_release_metadata(resolved_release_id)

        if not mb_data:
            return {"success": False, "error": "MusicBrainz fetch failed"}

        release_year = mb_data.get("release_year")
        tracks = mb_data.get("tracks", [])
        total_tracks = len(tracks)

        release_album_artist = mb_data.get("artist") or artist

        monitoring_folder = None
        mb_release_db_id = None
        if create_folder_group:
            monitoring_folder = create_monitoring_folder(
                artist, release_title, release_year
            )
            mb_release_db_id = upsert_musicbrainz_release(
                resolved_release_id,
                release_title,
                artist,
                release_year,
                total_tracks,
                monitoring_folder,
                method,
                album_artist=release_album_artist,
                release_source='musicbrainz',
            )

        queue_source = 'soulseek'
        _album_metadata: dict[str, Any] = {}
        if isinstance(mb_data, dict):
            try:
                from services.downloads.download_completion_service import (
                    _album_level_mb_fields,
                )
                _album_metadata = _album_level_mb_fields(mb_data)
            except Exception as _album_exc:
                logger.debug(
                    "Could not derive album-level queue metadata",
                    release_id=resolved_release_id,
                    error=str(_album_exc),
                )
        queue_result = add_release_tracks_to_queue_detailed(
            resolved_release_id,
            tracks,
            artist,
            release_title,
            album_artist=release_album_artist,
            queue_source=queue_source,
            year=release_year,
            album_metadata=(_album_metadata or None),
        )
        queue_ids = list(queue_result.get("queue_ids") or [])

        return {
            "success": True,
            "mb_release_db_id": mb_release_db_id,
            "queue_items_created": len(queue_ids),
            "queue_ids": queue_ids,
            "queued": bool(queue_ids),
            "queue_reason": queue_result.get("reason"),
            "queue_message": queue_result.get("message") or "",
            "queue_skipped": {
                "in_library": queue_result.get("in_library") or 0,
                "already_queued": queue_result.get("already_queued") or 0,
                "already_active": queue_result.get("already_active") or 0,
                "duplicate": queue_result.get("duplicate") or 0,
            },
            "total_tracks": queue_result.get("total_tracks") or 0,
        }

    except Exception as e:
        logger.error("Failed to start release download", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}
