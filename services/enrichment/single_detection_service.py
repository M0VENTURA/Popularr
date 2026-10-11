"""Single detection service (Enhanced 8-Stage Algorithm).

Single detection is metadata/enrichment classification, not popularity math.
Implements the comprehensive 8-stage detection algorithm:

1. Pre-filter & validation
2. Z-score threshold gate (artist + album)
3. Discogs confirmation
4. MusicBrainz confirmation + compilation check
5. Radio edit / single marker detection
6. Title normalization & duration matching
7. Last.fm album track count check
8. Final hybrid confidence decision

Popularity scans call this service; the result is a classification signal.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from statistics import median as stat_median
from typing import Any

import structlog

try:
    from rapidfuzz import fuzz as _fuzz  # type: ignore[import-untyped]
    _HAVE_RAPIDFUZZ = True
except ImportError:
    from difflib import SequenceMatcher as _difflib_matcher
    _HAVE_RAPIDFUZZ = False

from services.popularity.popularity_zscore import composite_listener_z
from services.catalog.album_classification_service import is_instrumental_track_title
from helpers.normalization_service import (
    strip_single_release_suffix,
    normalize_title_for_lookup,
    normalize_title_for_lucene_query,
    strip_remaster_suffix,
    is_remastered_only_variant,
    strip_featured_artist,
    strip_featured_guest_suffix,
    edition_annotations_compatible,
)

from api_clients.musicbrainz_http import escape_lucene_special_chars

logger = structlog.get_logger(__name__)

# =============================================================================
# THREAD-SAFE MEMORY CACHES
# =============================================================================

_CACHE_LOCK = threading.Lock()
_lb_artist_context_cache: dict[str, dict[str, Any]] = {}
_mb_single_rg_cache: dict[str, list[dict[str, Any]]] = {}
_MB_SINGLE_RG_CACHE_MAX = 4000
_album_track_count_cache: dict[tuple[str, str], int] = {}
_album_title_track_cache: dict[tuple[str, str], bool] = {}

# ── Constants ──────────────────────────────────────────────────────────────

IGNORE_SINGLE_KEYWORDS = frozenset({
    "intro", "outro", "jam",
    "live", "unplugged",
    "remix", "edit", "mix",
    "acoustic", "orchestral",
    "demo", "instrumental", "karaoke",
})

_IGNORE_KEYWORD_RE = re.compile(
    r"\b(?:" + "|".join(
        re.escape(kw) + r"(?:es|s)?" for kw in sorted(IGNORE_SINGLE_KEYWORDS, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)

_STRIPPABLE_SUFFIXES = [
    "radio edit", "single edit", "edit",
    "single version", "radio version", "radio mix",
]
_SEPARATORS = [" - ", " (", " ["]

_COMPILATION_KEYWORDS = [
    "greatest hits", "best of", "the very best", "anthology",
    "singles", "collection", "ultimate", "gold", "platinum",
]
_SPECIAL_EDITION_KEYWORDS = [
    "deluxe", "expanded", "reissue", "anniversary", "bonus",
    "special edition", "extended edition", "tour edition",
    "limited edition", "collector's edition", "remastered",
]

_METHOD_FAIL_THRESHOLD = 3


# ── Stage 0: Title normalisation helpers ──────────────────────────────────

def normalize_title_strict(title: str) -> str:
    t = strip_release_variant_suffix(title or "")
    preserved = ""
    m = re.search(r'([!+?]+)\s*$', t)
    if m:
        preserved = m.group(1)
        t = t[:m.start()]
    roman = ""
    m = re.search(r'\s+(I{1,3}|IV|V|VI{0,3}|IX|X{1,3}|XI{0,3}|XIV|XV|XVI{0,3}|XIX|XX)\s*$', t, re.IGNORECASE)
    if m:
        roman = " " + m.group(1).lower()
        t = t[:m.start()]
    t = re.sub(r'\s*[\(\[].*?[\)\]]', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    t = t.lower().strip()
    t = re.sub(r'^(?:a|an|the)\s+', '', t)
    t = re.sub(r'\s+', ' ', t)
    if roman:
        t += roman
    if preserved:
        t += preserved
    return t


def strip_release_variant_suffix(title: str) -> str:
    if not title:
        return title
    for sep in _SEPARATORS:
        for suffix in _STRIPPABLE_SUFFIXES:
            m = re.search(re.escape(sep) + r'\s*' + re.escape(suffix) + r'\s*$', title, re.IGNORECASE)
            if m:
                return title[:m.start()].rstrip()
    return title


def is_non_canonical_version_strict(title: str) -> bool:
    t = strip_remaster_suffix(title or "").lower()
    for pat in [r'\(radio\s+edit\s*\)', r'\(single\s*\)']:
        t = re.sub(pat, '', t)
    markers = [r'\bremix\b', r'\bacoustic\b', r'\blive\b', r'\bunplugged\b',
               r'\borchestral\b', r'\bsymphonic\b', r'\bdemo\b', r'\binstrumental\b',
               r'\bedit\b', r'\bextended\b', r'\bversion\b', r'\balt\b', r'\balternate\b']
    return any(re.search(p, t) for p in markers)


def has_single_or_radio_edit_marker(title: str) -> bool:
    return bool(re.search(r'\b(?:radio\s+(?:edit|mix|version)|single\s+(?:version|edit|mix))\b',
                          title or "", re.IGNORECASE))


# ── Stage 0: Album-type helpers ───────────────────────────────────────────

def is_compilation_album(album_type: str | None, album_title: str) -> bool:
    if album_type and ("compilation" in album_type.lower() or "soundtrack" in album_type.lower()):
        return True
    t = (album_title or "").lower()
    if "various artists" in t:
        return True
    return any(kw in t for kw in _COMPILATION_KEYWORDS)


def is_special_edition_album(album_title: str) -> bool:
    t = (album_title or "").lower()
    if any(kw in t for kw in _SPECIAL_EDITION_KEYWORDS):
        return True
    if ":" in t:
        parts = t.split(":", 1)
        if len(parts) > 1 and "edition" in parts[1]:
            return True
    return False


# ── Stage 1: Pre-filter ───────────────────────────────────────────────────

def should_skip_single_detection(title: str, album_type: str | None = None) -> bool:
    # Safely exempt true radio/single edits from being ignored by "mix"/"edit" keywords
    if has_single_or_radio_edit_marker(title):
        return False
        
    t = (title or "").lower()
    at = (album_type or "").lower()
    if _IGNORE_KEYWORD_RE.search(t):
        return True
    if "live" in at:
        return True
    return False


# ── Stage 2: Z-score calculation (median + MAD) ───────────────────────────

def calculate_z_score_strict(popularity: float, pop_median: float, pop_mad_scaled: float) -> float:
    if pop_mad_scaled == 0:
        return 0.0
    return (popularity - pop_median) / pop_mad_scaled


def _parse_release_year(value: Any) -> int | None:
    if not value:
        return None
    try:
        match = re.search(r"(?<!\d)(\d{4})(?!\d)", str(value).strip())
        return int(match.group(1)) if match else None
    except Exception:
        return None


def _album_release_year(artist: str, album: str | None) -> int | None:
    if not album:
        return None
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session
        with db_session() as session:
            row = session.execute(
                _text(
                    "SELECT MIN(release_year) AS y FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "AND album = :album AND release_year IS NOT NULL"
                ),
                {"artist": artist, "album": album},
            ).first()
            year = row._mapping.get("y") if row else None
        return int(year) if year else None
    except Exception:
        return None


SINGLE_RELEASE_LEAD_YEARS = 1


def get_dynamic_z_threshold(track_count: int, release_year: int | None = None, is_compilation: bool = False) -> float:
    if track_count < 5:
        threshold = 1.5
    elif track_count < 10:
        threshold = 1.7
    elif track_count < 50:
        threshold = 1.8
    elif track_count < 200:
        threshold = 1.7
    else:
        threshold = 1.6
        
    if release_year and release_year < 2000:
        reduction = min(0.3, (2000 - release_year) * 0.02)
        threshold = max(1.2, threshold - reduction)
        
    if is_compilation:
        threshold = min(threshold + 0.2, 2.5)
    return threshold


# ── Stage 3-4: Source confidence ──────────────────────────────────────────

def _source_confidence_levels() -> dict[str, str]:
    feats: dict[str, Any] = {}
    try:
        from helpers.config_helpers import get_config
        cfg = get_config()
        feats = cfg.get("features", {}) or {}
    except Exception:
        feats = {}
        
    defaults = {
        "discogs": "high",
        "musicbrainz": "medium",
        "discogs_video": "medium",
        "musicbrainz_compilation": "medium",
        "lastfm": "medium",
        "radio_edit": "medium",
    }
    keys = {
        "discogs": "source_discogs_confidence",
        "musicbrainz": "source_musicbrainz_confidence",
        "discogs_video": "source_discogs_video_confidence",
        "musicbrainz_compilation": "source_musicbrainz_compilation_confidence",
        "lastfm": "source_lastfm_confidence",
        "radio_edit": "source_radio_edit_confidence",
    }
    
    result: dict[str, str] = {}
    for src, default in defaults.items():
        val = str(feats.get(keys[src], default) or default).lower()
        result[src] = val if val in ("high", "medium", "low") else default
    return result


def _always_check_discogs_video() -> bool:
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        sd = cfg.get("single_detection", {}) or {}
        return bool(sd.get("always_check_discogs_video", False))
    except Exception:
        return False


# ── Stage 5-7: Source detection methods ───────────────────────────────────

def _get_lb_artist_context_cached(artist_mbid: str) -> dict[str, Any]:
    if not artist_mbid:
        return {"threshold": 0, "total": 0}
        
    with _CACHE_LOCK:
        if artist_mbid in _lb_artist_context_cache:
            return _lb_artist_context_cache[artist_mbid]
            
    try:
        from services.enrichment.single_detection_context_service import get_artist_listenbrainz_context
        ctx = get_artist_listenbrainz_context(artist_mbid)
    except Exception:
        ctx = {"threshold": 0, "total": 0}
        
    with _CACHE_LOCK:
        _lb_artist_context_cache[artist_mbid] = ctx
    return ctx


def _is_promo_only_group(group: dict[str, Any]) -> bool:
    releases = group.get("releases") or []
    statuses = [
        str(r.get("status") or "").strip().lower()
        for r in releases
        if isinstance(r, dict) and r.get("status")
    ]
    return bool(statuses) and all(s == "promotion" for s in statuses)


def _cached_mb_release_group_search(query: str, limit: int, mb_client: Any) -> list[dict[str, Any]]:
    with _CACHE_LOCK:
        cached = _mb_single_rg_cache.get(query)
    if cached is not None:
        return cached
        
    try:
        found = mb_client.search_release_groups(query, limit=limit) or []
    except Exception:
        found = []
        
    with _CACHE_LOCK:
        _mb_single_rg_cache[query] = found
        while len(_mb_single_rg_cache) >= _MB_SINGLE_RG_CACHE_MAX:
            try:
                _mb_single_rg_cache.pop(next(iter(_mb_single_rg_cache)))
            except (StopIteration, KeyError):
                break
                
    return found


def _detect_musicbrainz(title: str, artist: str, artist_mbid: str | None,
                        album_track_count: int | None, mb_client: Any = None,
                        mb_cached_singles: set[str] | None = None,
                        recording_mbid: str | None = None) -> dict[str, Any]:
                        
    if mb_cached_singles:
        normalized = (title or "").lower().strip()
        if normalized in {str(t).lower().strip() for t in mb_cached_singles}:
            return {"source": "musicbrainz", "matched": True, "confidence": 0.9,
                    "metadata": {}, "cached": True}
    try:
        if mb_client is None:
            from services.enrichment.musicbrainz_service import get_shared_mb_client
            mb_client = get_shared_mb_client()

        import time as _time
        _MB_BUDGET_S = 45.0
        _mb_deadline = _time.monotonic() + _MB_BUDGET_S

        def _within_budget() -> bool:
            return _time.monotonic() < _mb_deadline

        matched = False
        promo_only = False

        if recording_mbid and not matched and _within_budget():
            try:
                _rec = mb_client.get_recording(recording_mbid)
                if _rec:
                    _rec_title = str(_rec.get("title") or "")
                    _norm_rec = normalize_title_for_lookup(
                        strip_single_release_suffix(_rec_title) or _rec_title
                    )
                    _norm_track = normalize_title_for_lookup(
                        strip_single_release_suffix(title) or title
                    )
                    _title_ok = bool(
                        _norm_rec
                        and _norm_track
                        and (
                            _norm_rec == _norm_track
                            or (
                                _HAVE_RAPIDFUZZ
                                and (_fuzz.token_set_ratio(_norm_rec, _norm_track) / 100.0) >= 0.85
                            )
                        )
                    )
                    if _title_ok:
                        for _rel in _rec.get("releases") or []:
                            if not isinstance(_rel, dict):
                                continue
                            _rg = _rel.get("release-group") or {}
                            _pt = str(_rg.get("primary-type") or "").lower()
                            if _pt in ("single", "ep"):
                                matched = True
                                break
                    if matched:
                        logger.debug("Recording release-group confirms single", recording_mbid=recording_mbid, artist=artist, track=title)
            except Exception as exc:
                logger.debug("Recording release-group single check failed", recording_mbid=recording_mbid, error=str(exc))
                
        clean_title = strip_featured_guest_suffix(
            strip_single_release_suffix(title) or title
        )
        
        if artist_mbid and hasattr(mb_client, "search_release_groups") and _within_budget() and not matched:
            try:
                from difflib import SequenceMatcher as _SM
                target = normalize_title_for_lookup(clean_title)
                lucene_title = normalize_title_for_lucene_query(clean_title)
                candidates: list[dict[str, Any]] = []
                for _pt in ("single", "ep"):
                    if not _within_budget():
                        break
                    _found = _cached_mb_release_group_search(
                        f'arid:{artist_mbid} AND primarytype:{_pt} '
                        f'AND releasegroup:"{escape_lucene_special_chars(lucene_title)}"',
                        limit=25,
                        mb_client=mb_client,
                    )
                    if not _found:
                        _found = _cached_mb_release_group_search(
                            f"arid:{artist_mbid} AND primarytype:{_pt}", limit=100,
                            mb_client=mb_client,
                        )
                    candidates += _found
                    
                for group in candidates:
                    rg_title = str(group.get("title") or "")
                    if not edition_annotations_compatible(title, rg_title):
                        continue
                    norm_rg = normalize_title_for_lookup(strip_single_release_suffix(rg_title) or rg_title)
                    
                    if norm_rg == target or (
                        (_fuzz.token_set_ratio(norm_rg, target) / 100.0) >= 0.85
                        if _HAVE_RAPIDFUZZ
                        else _SM(None, norm_rg, target).ratio() >= 0.85
                    ):
                        matched = True
                        promo_only = _is_promo_only_group(group)
                        break
            except Exception as exc:
                logger.debug("Artist-scoped MB single lookup failed", artist=artist, track=title, error=str(exc))

        if not matched and _within_budget():
            from services.enrichment.musicbrainz_service import get_shared_mb_service
            svc = get_shared_mb_service()
            matched = bool(svc.is_single(clean_title, artist, album_track_count=album_track_count))
            
        release_date = None
        if matched and _within_budget():
            try:
                if hasattr(mb_client, "get_single_release_date"):
                    release_date = mb_client.get_single_release_date(title, artist, artist_mbid=artist_mbid)
            except Exception:
                pass
                
        metadata: dict[str, Any] = {}
        if release_date:
            metadata["release_date"] = release_date
        if matched and promo_only:
            metadata["is_promo"] = True
            
        return {"source": "musicbrainz", "matched": matched, "confidence": 0.9 if matched else 0.0,
                "metadata": metadata}
                
    except Exception as exc:
        logger.debug("MusicBrainz single detection failed", artist=artist, track=title, error=str(exc))
        return {"source": "musicbrainz", "matched": False, "confidence": 0.0,
                "metadata": {}, "error": True}


def _detect_discogs(title: str, artist: str, album: str | None,
                    discogs_token: str | None, duration: float | None = None,
                    is_special_edition: bool = False,
                    cached_single_titles: set[str] | None = None,
                    cached_promo_titles: set[str] | None = None) -> dict[str, Any]:
    token = discogs_token or os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        try:
            from helpers.config_helpers import get_config
            cfg = get_config()
            token = (cfg.get("api_integrations", {}).get("discogs", {}) or {}).get("token", "") or ""
        except Exception:
            token = ""
            
    if not token or token.lower() in ("your_discogs_token", "your_token", "placeholder"):
        return {"source": "discogs", "matched": False, "confidence": 0.0, "metadata": {}}
        
    if cached_single_titles:
        normalized = (title or "").lower().strip()
        if normalized in {str(t).lower().strip() for t in cached_single_titles}:
            is_promo = bool(cached_promo_titles) and normalized in {
                str(t).lower().strip() for t in cached_promo_titles
            }
            return {"source": "discogs", "matched": True,
                    "confidence": 0.85,
                    "metadata": {"is_promo": is_promo, "similarity_ratio": 1.0},
                    "cached": True}
    try:
        from services.enrichment.discogs_service import (
            _get_service as _get_discogs_service,
            calculate_discogs_confidence,
        )
        svc = _get_discogs_service(token)
        ctx = {"album": album, "is_special_edition": is_special_edition} if album else {"is_special_edition": is_special_edition}
        
        if hasattr(svc, "get_single_status"):
            st = svc.get_single_status(title, artist, album_context=ctx)
            matched_raw = bool(st.get("is_single"))
            is_promo = bool(st.get("is_promo"))
            year = st.get("release_year")
            similarity = float(st.get("similarity") or 0.0)
            artist_verified = bool(st.get("artist_verified", False))
        else:
            matched_raw = bool(svc.is_single(title, artist, album_context=ctx))
            is_promo = False
            year = None
            similarity = 1.0
            artist_verified = True
            if matched_raw and hasattr(svc, "get_single_release_year"):
                year = svc.get_single_release_year(title, artist)
                
        calc = calculate_discogs_confidence(title, similarity, artist_verified)
        matched = bool(matched_raw and calc["matched"])
        # ``calc`` was called WITHOUT ``is_promo`` (a verified exact promo
        # match stays at the full 0.85 here; ``determine_final_status`` caps
        # it at "medium" downstream via the flag), but calc's metadata ALWAYS
        # carries ``is_promo=False`` — updating with it first clobbered the
        # service's real flag, so a full-path promo-only match could never be
        # downgraded and counted as a commercial single instead.
        metadata: dict[str, Any] = dict(calc.get("metadata") or {})
        metadata["is_promo"] = bool(is_promo)
        
        if year:
            metadata["release_year"] = year
            
        return {"source": "discogs", "matched": matched,
                "confidence": calc["confidence"] if matched else 0.0,
                "metadata": metadata}
    except Exception as exc:
        logger.debug("Discogs single detection failed", artist=artist, track=title, error=str(exc))
        return {"source": "discogs", "matched": False, "confidence": 0.0,
                "metadata": {}, "error": True}


def _detect_discogs_video(title: str, artist: str,
                          discogs_token: str | None = None) -> dict[str, Any]:
    token = discogs_token or os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        try:
            from helpers.config_helpers import get_config
            cfg = get_config()
            token = (cfg.get("api_integrations", {}).get("discogs", {}) or {}).get("token", "") or ""
        except Exception:
            token = ""
            
    if not token or token.lower() in ("your_discogs_token", "your_token", "placeholder"):
        return {"source": "discogs_video", "matched": False, "confidence": 0.0, "metadata": {}}
        
    try:
        from services.enrichment.discogs_service import _get_service
        svc = _get_service(token)
        matched = bool(svc.has_official_video(title, artist)) if hasattr(svc, "has_official_video") else False
        return {"source": "discogs_video", "matched": matched,
                "confidence": 0.5 if matched else 0.0, "metadata": {}}
    except Exception as exc:
        logger.debug("Discogs video detection failed", artist=artist, track=title, error=str(exc))
        return {"source": "discogs_video", "matched": False, "confidence": 0.0, "metadata": {}}


def _detect_lastfm(artist: str, album: str, title: str, lastfm_client: Any = None) -> bool:
    if not lastfm_client:
        return False
        
    try:
        if lastfm_client.check_track_as_single(artist, title):
            return True
    except Exception:
        pass

    album_key = (artist.casefold(), album.casefold())
    
    with _CACHE_LOCK:
        count = _album_track_count_cache.get(album_key)
        
    if count is None:
        try:
            count = lastfm_client.get_album_track_count(artist, album)
            with _CACHE_LOCK:
                _album_track_count_cache[album_key] = count
        except Exception:
            count = 0
            
    if 1 <= count <= 3:
        return True
    if 4 <= count <= 6:
        with _CACHE_LOCK:
            title_track = _album_title_track_cache.get(album_key)
            
        if title_track is None:
            try:
                title_track = bool(lastfm_client.has_title_track(artist, album))
            except Exception:
                return True
            with _CACHE_LOCK:
                _album_title_track_cache[album_key] = title_track
        return title_track

    if count == 0 or count >= 7:
        try:
            if hasattr(lastfm_client, "search_album"):
                target = normalize_title_for_lookup(strip_featured_guest_suffix(title) or title)
                single_marker = re.compile(
                    r"\s*[-–—]?\s*(?:single|ep)\s*$", flags=re.IGNORECASE
                )
                for alb in lastfm_client.search_album(title, artist=artist, limit=30) or []:
                    alb_name = str(alb.get("name") or "").strip()
                    if not alb_name or not single_marker.search(alb_name):
                        continue
                    base = single_marker.sub("", alb_name).strip()
                    if not edition_annotations_compatible(title, base):
                        continue
                    if normalize_title_for_lookup(base) == target:
                        return True
        except Exception:
            pass
            
    return False


# ── Stage 8: Final decision ────────────────────────────────────────────────

def determine_final_status(
    discogs: bool = False, musicbrainz: bool = False,
    album_z: float = 0.0, artist_z: float = 0.0,
    is_remastered_only: bool = False,
    is_title_track: bool = False, is_compilation: bool = False,
    zscore_high: float = 1.0, zscore_medium: float = 0.6,
    high_sources: int = 0, medium_sources: int = 0,
    metadata_medium_sources: int = 0,
    discogs_promo: bool = False, musicbrainz_promo: bool = False,
    z_standout: bool = False,
) -> str:
    max_z = album_z if album_z else artist_z
    if is_compilation:
        max_z = 0.0
        
    high = high_sources
    medium = medium_sources
    metadata_medium = max(0, metadata_medium_sources)
    if metadata_medium > medium:
        metadata_medium = medium

    if discogs_promo and high == 0:
        return 'medium'
    if musicbrainz_promo and high == 0:
        return 'medium'

    if max_z >= max(0.0, zscore_high):
        if high >= 1 or medium >= 2:
            verdict = 'high'
        elif z_standout and medium >= 1:
            verdict = 'high'
        elif metadata_medium >= 1:
            verdict = 'medium'
        else:
            verdict = 'none'
    elif max_z > max(0.0, zscore_medium):
        if high >= 1:
            verdict = 'high'
        elif metadata_medium >= 1 or medium >= 2:
            verdict = 'medium'
        else:
            verdict = 'none'
    else:
        if is_remastered_only or high >= 1 or medium >= 2 or metadata_medium >= 1:
            if high >= 1:
                verdict = 'high'
            elif medium >= 1 and not is_title_track:
                verdict = 'medium'
            else:
                verdict = 'none'
        elif is_title_track and medium >= 1:
            verdict = 'medium'
        else:
            verdict = 'none'

    if verdict == 'none' and (discogs or musicbrainz) and (high >= 1 or medium >= 1):
        return 'high' if high >= 1 else 'medium'
        
    return verdict


# ── Main entry point ──────────────────────────────────────────────────────

def detect_single_for_track(
    title: str,
    artist: str,
    album_track_count: int = 1,
    spotify_results_cache: dict[str, Any] | None = None,
    verbose: bool = False,
    discogs_token: str | None = None,
    track_id: str | None = None,
    album: str | None = None,
    isrc: str | None = None,
    duration: float | None = None,
    popularity: float | None = None,
    album_type: str | None = None,
    use_advanced_detection: bool = True,
    zscore_threshold: float = 1.0,
    album_is_underperforming: bool = False,
    artist_median_popularity: float = 0.0,
    lastfm_client: Any = None,
    track_repo: Any = None,
    persist_result: bool = True,
    mb_cached_singles: set[str] | None = None,
    discogs_cached_singles: set[str] | None = None,
    discogs_cached_promos: set[str] | None = None,
    artist_mbid: str | None = None,
    recording_mbid: str | None = None,
    mb_client: Any = None,
    listenbrainz_listens: int | None = None,
    lastfm_listeners: int | None = None,
    album_lf_listeners: list[float] | None = None,
    album_lb_listens: list[float] | None = None,
    artist_stats_override: list[float] | None = None,
    artist_listen_override: list[float] | None = None,
    is_va_compilation: bool | None = None,
) -> dict[str, Any]:
    title = title or ""
    artist = artist or ""
    lookup_title = strip_single_release_suffix(title)
    logger.debug("Checking track single status", artist=artist, track=title)

    if not title or not artist:
        return {"is_single": False, "confidence": "low", "confidence_score": 0.0, "sources": [], "reasons": ["missing_title_or_artist"]}

    if should_skip_single_detection(title, album_type=album_type):
        return {"is_single": False, "confidence": "low", "confidence_score": 0.0, "sources": [], "reasons": ["alternate_or_live_version"]}

    if is_va_compilation is not None:
        is_compilation = is_va_compilation
    else:
        is_compilation = is_compilation_album(album_type, album or "")
        
    is_special = is_special_edition_album(album or "")
    is_remastered = is_remastered_only_variant(title)

    _stats_artist = artist
    artist_vals: list[float] = []
    album_vals: list[float] = []
    album_z = 0.0
    artist_z = 0.0
    
    if popularity is not None and popularity > 0 and not is_compilation:
        try:
            if artist_listen_override and lastfm_listeners is not None:
                import math as _math
                _log_listens = [
                    _math.log10(max(float(v), 1.0))
                    for v in artist_listen_override
                    if float(v or 0) > 0
                ]
                if len(_log_listens) >= 5:
                    _track_log = _math.log10(max(float(lastfm_listeners), 1.0))
                    _med = stat_median(_log_listens)
                    _mad = stat_median([abs(v - _med) for v in _log_listens])
                    _spread = max(_mad * 1.4826, 0.25)
                    artist_z = (_track_log - _med) / _spread if _spread > 0 else 0
            else:
                if artist_stats_override:
                    artist_vals = list(artist_stats_override)
                else:
                    from services.popularity.popularity_stats_service import calculate_artist_stats
                    _, _, _raw_vals = calculate_artist_stats(None, _stats_artist)
                    if not _raw_vals:
                        _canon = strip_featured_artist(_stats_artist)
                        if _canon and _canon != _stats_artist:
                            _, _, _canon_vals = calculate_artist_stats(None, _canon)
                            if _canon_vals:
                                _stats_artist = _canon
                                _raw_vals = _canon_vals
                    artist_vals = _raw_vals

                if artist_vals:
                    art_med = stat_median(artist_vals)
                    art_mad = stat_median([abs(v - art_med) for v in artist_vals]) if artist_vals else 0
                    art_spread = max(art_mad * 1.4826, 10.0, 0.10 * art_med)
                    artist_z = (popularity - art_med) / art_spread if art_spread > 0 else 0

            if album:
                from services.popularity.popularity_stats_service import calculate_album_stats
                _, _, album_vals = calculate_album_stats(None, _stats_artist, album)
                if album_vals:
                    alb_med = stat_median(album_vals)
                    alb_mad = stat_median([abs(v - alb_med) for v in album_vals]) if album_vals else 0
                    alb_spread = max(alb_mad * 1.4826, 10.0, 0.10 * alb_med)
                    album_z = (popularity - alb_med) / alb_spread if alb_spread > 0 else 0
        except Exception as exc:
            logger.debug("Z-score calculation failed", error=str(exc))

    z_composite = 0.0
    if popularity is not None and popularity > 0 and not is_compilation:
        z_composite = composite_listener_z(
            lastfm_listeners,
            listenbrainz_listens,
            _stats_artist,
            album,
            album_lf_listeners=album_lf_listeners,
            album_lb_listens=album_lb_listens,
        )

    z_low = artist_z < -1.0 and not is_compilation and not is_remastered

    discogs_confirmed = False
    musicbrainz_confirmed = False
    discogs_video_confirmed = False
    lastfm_confirmed = False
    radio_edit_found = False
    mb_compilation_confirmed = False
    single_release_date_match = False

    reasons: list[str] = []
    sources: list[dict[str, Any]] = []
    
    if z_low:
        reasons.append("z_score_low")

    z_standout = False
    try:
        if is_instrumental_track_title(title):
            reasons.append("instrumental_version")
        else:
            artist_track_count = max(len(artist_vals or []), len(album_vals or []))
            standout_z = z_composite or album_z
            if artist_track_count >= 3 and standout_z > 0:
                dyn_threshold = get_dynamic_z_threshold(
                    artist_track_count,
                    None,
                    is_compilation,
                )
                if standout_z >= dyn_threshold:
                    z_standout = True
                    reasons.append("z_score_standout")
    except Exception as exc:
        logger.debug("Dynamic z-score standout check failed", artist=artist, track=title, error=str(exc))

    dr: dict[str, Any] = {"source": "discogs", "matched": False, "confidence": 0.0, "metadata": {}}
    if use_advanced_detection:
        dr = _detect_discogs(lookup_title, artist, album, discogs_token, duration=duration,
                             is_special_edition=is_special,
                             cached_single_titles=discogs_cached_singles,
                             cached_promo_titles=discogs_cached_promos)
        sources.append(dr)
        if dr["matched"]:
            discogs_confirmed = True
            reasons.append("discogs_matched")
            
    discogs_promo = bool((dr.get("metadata") or {}).get("is_promo"))

    # ── Early exit on full-confidence Discogs match ─────────────────────
    # A Discogs match with confidence >= 0.85 is the strongest single signal
    # the pipeline has (the final-status logic already treats it as a
    # definitive high source).  The old system returned at the first
    # high-confidence source; running the remaining MB / video / Last.fm /
    # ISRC arms for such tracks adds 2-10+ rate-limited calls each for no
    # change in the outcome.  Skip them and keep only the lightweight local
    # checks (radio-edit marker, release-date comparison, LB top-10 context).
    _discogs_full_confidence = bool(
        discogs_confirmed and float(dr.get("confidence") or 0) >= 0.85
    )

    if _discogs_full_confidence:
        mr = {"source": "musicbrainz", "matched": False, "confidence": 0.0, "metadata": {}}
        musicbrainz_promo = False
        sources.append(mr)
    else:
        mr = _detect_musicbrainz(lookup_title, artist, artist_mbid, album_track_count, mb_client=mb_client,
                                 mb_cached_singles=mb_cached_singles,
                                 recording_mbid=recording_mbid)
        sources.append(mr)
        if mr["matched"]:
            musicbrainz_confirmed = True
            reasons.append("musicbrainz_matched")
        elif mr.get("error"):
            reasons.append("mb_unavailable")
            
        musicbrainz_promo = bool((mr.get("metadata") or {}).get("is_promo"))

    if dr.get("error"):
        reasons.append("discogs_unavailable")

    if use_advanced_detection and not discogs_confirmed and (
        not musicbrainz_confirmed
        or _always_check_discogs_video()
    ):
        dv = _detect_discogs_video(lookup_title, artist, discogs_token)
        if dv.get("matched"):
            discogs_video_confirmed = True
            reasons.append("discogs_video")

    lb_top10 = False
    if listenbrainz_listens is not None and listenbrainz_listens > 0 and artist_mbid:
        _lb_ctx = _get_lb_artist_context_cached(artist_mbid)
        _lb_threshold = int(_lb_ctx.get("threshold") or 0)
        if _lb_threshold > 0 and int(listenbrainz_listens) >= _lb_threshold:
            # Protect against the "Legendary Album" effect where EVERY track on a classic album
            # surpasses the global artist threshold just by virtue of the album's massive popularity.
            is_legendary_rider = False
            if album_lb_listens and len(album_lb_listens) >= 3:
                _valid_lb = [float(x) for x in album_lb_listens if x > 0]
                if _valid_lb:
                    alb_med = stat_median(_valid_lb)
                    # A true single should perform noticeably better than the album median.
                    # If it's performing at or below the median (even with a 10% buffer), it's a deep cut.
                    if listenbrainz_listens <= alb_med * 1.1:
                        is_legendary_rider = True
                        
            if not is_legendary_rider:
                lb_top10 = True
                reasons.append("lb_top10")

    if has_single_or_radio_edit_marker(title):
        radio_edit_found = True
        reasons.append("radio_edit_marker")

    if lastfm_client and not _discogs_full_confidence:
        lastfm_confirmed = _detect_lastfm(artist, album or "", lookup_title, lastfm_client)
        if lastfm_confirmed:
            reasons.append("lastfm_confirmed")

    if discogs_confirmed or musicbrainz_confirmed:
        single_release_year = _parse_release_year(
            mr.get("metadata", {}).get("release_date")
            or dr.get("metadata", {}).get("release_year")
        )
        album_release_year = _album_release_year(artist, album)
        if (
            single_release_year
            and album_release_year
            and (album_release_year - single_release_year) >= SINGLE_RELEASE_LEAD_YEARS
        ):
            single_release_date_match = True
            reasons.append("release_date_match")

    isrc_single_confirmed = False
    if isrc and not _discogs_full_confidence:
        try:
            if mb_client is None:
                from api_clients.musicbrainz_http import MusicBrainzHttpClient
                mb_client = MusicBrainzHttpClient()
            recordings = mb_client.lookup_by_isrc(isrc, inc="releases")
            for recording in recordings:
                for release in recording.get("releases", []):
                    rg = release.get("release-group") or {}
                    pt = (rg.get("primary-type") or rg.get("primary_type") or "").lower()
                    if pt in ("single", "ep"):
                        isrc_single_confirmed = True
                        reasons.append("isrc_single")
                        break
                if isrc_single_confirmed:
                    break
        except Exception as exc:
            logger.debug("ISRC single lookup failed", isrc=isrc, error=str(exc))

    if musicbrainz_confirmed and mb_client and hasattr(mb_client, "appears_on_various_artists"):
        try:
            if mb_client.appears_on_various_artists(lookup_title, artist):
                mb_compilation_confirmed = True
                reasons.append("mb_compilation")
        except Exception:
            pass

    for _src_flag, _src_name in (
        (isrc_single_confirmed, "isrc"),
        (lb_top10, "listenbrainz_top10"),
        (z_standout, "popularity_z_standout"),
        (radio_edit_found, "radio_edit"),
        (mb_compilation_confirmed, "musicbrainz_compilation"),
        (lastfm_confirmed, "lastfm"),
        (discogs_video_confirmed, "discogs_video"),
        (single_release_date_match, "release_date_match"),
    ):
        if _src_flag:
            sources.append({"source": _src_name, "matched": True, "confidence": 0.5})

    has_meta = discogs_confirmed or musicbrainz_confirmed
    is_title = normalize_title_strict(title) == normalize_title_strict(album or "")

    if isrc_single_confirmed:
        musicbrainz_confirmed = True

    try:
        from services.popularity.popularity_config import get_zscore_thresholds
        _zth = get_zscore_thresholds()
        zscore_high = float(_zth.get("high", 1.0) or 1.0)
        zscore_medium = float(_zth.get("medium", 0.6) or 0.6)
    except Exception:
        zscore_high, zscore_medium = 1.0, 0.6

    # ``_discogs_full_confidence`` was computed before the early-exit above.
    _levels = _source_confidence_levels()
    high_sources = 0
    medium_sources = 0
    metadata_medium_sources = 0
    
    for _src, _confirmed in (
        ("discogs", discogs_confirmed),
        ("musicbrainz", musicbrainz_confirmed),
    ):
        if not _confirmed or _levels.get(_src, "high") == "low":
            continue
        _level = _levels.get(_src, "high")
        if _src == "discogs" and not _discogs_full_confidence and _level == "high":
            _level = "medium"
        if _src == "discogs" and discogs_promo and _level == "high":
            _level = "medium"
        if _src == "musicbrainz" and musicbrainz_promo and _level == "high":
            _level = "medium"
        if _level == "high":
            high_sources += 1
        else:
            medium_sources += 1
            metadata_medium_sources += 1
            
    if discogs_video_confirmed and _levels.get("discogs_video", "medium") != "low":
        medium_sources += 1
        metadata_medium_sources += 1
    if lastfm_confirmed and _levels.get("lastfm", "medium") != "low":
        medium_sources += 1
        metadata_medium_sources += 1
    if mb_compilation_confirmed and _levels.get("musicbrainz_compilation", "medium") != "low":
        medium_sources += 1
        metadata_medium_sources += 1
    if radio_edit_found and _levels.get("radio_edit", "medium") != "low":
        medium_sources += 1
    if single_release_date_match:
        medium_sources += 1
        metadata_medium_sources += 1
    if isrc_single_confirmed:
        medium_sources += 1
        metadata_medium_sources += 1
    if lb_top10:
        medium_sources += 1

    _discogs_med_slot = 1 if (
        discogs_confirmed
        and _levels.get("discogs", "high") != "low"
        and not _discogs_full_confidence
    ) else 0
    _corroborating_medium = medium_sources - _discogs_med_slot

    final = determine_final_status(
        discogs=discogs_confirmed, musicbrainz=musicbrainz_confirmed,
        album_z=z_composite or album_z, artist_z=artist_z,
        is_remastered_only=is_remastered,
        is_title_track=is_title,
        is_compilation=is_compilation,
        zscore_high=zscore_high,
        zscore_medium=zscore_medium,
        high_sources=high_sources,
        medium_sources=medium_sources,
        metadata_medium_sources=metadata_medium_sources,
        discogs_promo=discogs_promo,
        musicbrainz_promo=musicbrainz_promo,
        z_standout=z_standout,
    )

    if (
        discogs_confirmed
        and not _discogs_full_confidence
        and final in ("none", "medium")
        and (_corroborating_medium >= 1 or z_standout)
    ):
        final = "high"

    if z_low and final == "high" and high_sources < 2 and medium_sources == 0:
        final = "medium"

    label_map = {"high": "high", "medium": "medium", "none": "low"}
    score_map = {"high": 1.0, "medium": 0.67, "none": 0.0}
    is_single = final in ("high", "medium")

    logger.debug(
        "Single detection concluded",
        artist=artist,
        track=title,
        album_z=album_z,
        artist_z=artist_z,
        discogs=discogs_confirmed,
        mb=musicbrainz_confirmed,
        lastfm=lastfm_confirmed,
        radio=radio_edit_found,
        isrc=isrc_single_confirmed,
        lb_top10=lb_top10,
        z_standout=z_standout,
        high_sources=high_sources,
        medium_sources=medium_sources,
        final_status=final,
    )

    result = {
        "is_single": is_single,
        "confidence": label_map.get(final, "low"),
        "confidence_score": score_map.get(final, "low"),
        "sources": sources,
        "reasons": reasons or ["no_source_match"],
        "single_status": final,
        "decision": {
            "album_z": round(album_z, 2),
            "artist_z": round(artist_z, 2),
            "z_composite": round(z_composite, 2),
            "z_low": bool(z_low),
            "high_sources": high_sources,
            "medium_sources": medium_sources,
            "is_title_track": is_title,
            "z_standout": bool(z_standout),
            "source_levels": {
                k: _levels.get(k) for k in (
                    "discogs", "musicbrainz", "discogs_video", "lastfm"
                )
            },
        },
    }

    if persist_result and track_repo and track_id:
        try:
            track_repo.update_track_single_status(track_id, is_single, result["confidence"])
        except Exception as exc:
            logger.debug("Persistence failed", track_id=track_id, error=str(exc))

    return result
