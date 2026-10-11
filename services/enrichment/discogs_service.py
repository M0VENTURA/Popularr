"""Discogs enrichment service.

Rebuilt with the following corrections:

- ``EP`` is no longer treated as a single release format. EP membership is
  reported separately, and an EP *lead* track is promoted to a single at
  medium confidence, since lead tracks are commonly issued as singles while
  deep cuts are not.
- Confidence no longer requires a perfect title match. Verified non-exact
  matches now produce usable medium-confidence evidence, while only exact,
  artist-verified, non-promo, non-EP matches reach the 0.85 "full
  confidence" band used by the single-detection early exit.
- ``artist_verified``, ``is_promo`` and ``is_ep_lead`` are propagated in the
  returned confidence metadata so callers can gate the early exit correctly.
- Cached release rows preserve release type and track count; an unknown
  release type is no longer silently coerced to "album".
- Failed artist-release, release-tracklist and official-video lookups are no
  longer cached as definitive negatives.
- HTTP and database work no longer happens while holding the service lock.
- Special-edition album context no longer disables detection outright.
- Global search artist matching is token-aware rather than substring-based.
- Service resolution uses a per-token registry, so an empty token can never
  evict a working, cache-warm instance.
"""

from __future__ import annotations

import logging
import re
import threading
from difflib import SequenceMatcher
from typing import Any, TypedDict

import structlog

try:  # Optional C-speed fuzzy matching
    from rapidfuzz import fuzz as _rapidfuzz_fuzz
except ImportError:
    _rapidfuzz_fuzz = None

from api_clients.discogs_http import DiscogsHttpClient
from helpers.normalization_service import (
    normalize_title_for_lookup,
    strip_parentheses,
    strip_featured_artist,
    strip_featured_guest_suffix,
    clean_discogs_biography,
    edition_annotations_compatible,
)

logger = structlog.get_logger(__name__)
# Force this specific module to emit DEBUG logs regardless of global config
logging.getLogger(__name__).setLevel(logging.DEBUG)

# --- CONSTANTS ---------------------------------------------------------------

MIN_DISCOGS_SIMILARITY = 0.75

# Confidence at or above which a Discogs match is treated as a definitive
# high-confidence source (and may short-circuit the remaining detection arms).
DISCOGS_FULL_CONFIDENCE = 0.85

# Minimum confidence for an artist-verified match to count as evidence at all.
DISCOGS_MIN_MATCH_CONFIDENCE = 0.75

# Minimum confidence for an unverified (global search) match to count.
DISCOGS_MIN_UNVERIFIED_CONFIDENCE = 0.50

# Weight applied to unverified global-search matches. Deliberately below
# DISCOGS_FULL_CONFIDENCE so these can never trigger the early exit.
DISCOGS_UNVERIFIED_WEIGHT = 0.60

# Promo-only matches are capped below the full-confidence band.
DISCOGS_PROMO_CONFIDENCE_CAP = 0.74

# EP lead tracks are frequently issued as singles, so they count as evidence,
# but capped below the full-confidence band: an EP lead track is supporting
# evidence requiring corroboration, never a definitive single on its own.
DISCOGS_EP_LEAD_CONFIDENCE_CAP = 0.74

# Maximum tracks on a release still considered EP-sized for lead-track logic.
MAX_EP_TRACKS = 6

ALBUM_FORMAT_TOKENS = frozenset({"album", "lp", "compilation", "mixtape"})

# NOTE: "ep" is intentionally excluded here. A track appearing on an EP is not
# by itself evidence that the track was released as a single; only an EP lead
# track is promoted, and then only at medium confidence.
SINGLE_FORMAT_TOKENS = frozenset({"single", "maxi", "maxi-single"})
SUPPORTING_RELEASE_FORMAT_TOKENS = frozenset({"ep"})

MAX_SINGLE_TRACKS = 6

# Cap master-format resolutions per artist fetch (see resolve_master_formats).
_MAX_MASTER_FORMAT_RESOLUTIONS = 15

INVERTED_RETRY_MIN_SIMILARITY = 0.50


def _sanitize_release_name(album_name: str) -> str:
    """Strips '(Topshelf Edition)', '[Deluxe Version]', etc. for exact API matches."""
    if not album_name:
        return ""
    cleaned = re.sub(
        r"\s*[\(\[].*?(edition|deluxe|remaster|version|bonus|expanded|explicit|clean).*?[\)\]]",
        "",
        album_name,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned if cleaned else album_name


def _normalise_discogs_artist(value: str) -> str:
    """Normalise a Discogs artist string, stripping numeric disambiguators."""
    value = strip_featured_artist(value or "")
    value = re.sub(r"\s+\(\d+\)\s*$", "", value)
    value = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", value)
    value = normalize_title_for_lookup(value)
    return re.sub(r"\s+", " ", value).strip()


def _artist_cache_key(artist: str) -> str:
    return _normalise_discogs_artist(artist) or str(artist or "").strip().lower()


# --- CONFIDENCE --------------------------------------------------------------

def calculate_discogs_confidence(
    title: str,
    similarity_ratio: float,
    artist_verified: bool,
    is_promo: bool = False,
    is_ep_lead: bool = False,
) -> dict[str, Any]:
    """Calculate Discogs match confidence.

    Match eligibility and full confidence are separate concepts:

    - ``matched`` indicates the result is usable evidence.
    - ``confidence >= DISCOGS_FULL_CONFIDENCE`` indicates an exact,
      artist-verified, non-promo, non-EP match that may be treated as
      definitive.

    ``is_ep_lead`` marks a lead/opening track on an EP-sized release. These
    are commonly issued as singles and count as evidence, but are capped in
    the medium band so they always require corroboration.
    """
    sim = max(0.0, min(1.0, float(similarity_ratio or 0.0)))

    metadata: dict[str, Any] = {
        "similarity_ratio": round(sim, 2),
        "artist_verified": bool(artist_verified),
        "is_promo": bool(is_promo),
        "is_ep_lead": bool(is_ep_lead),
    }

    if sim < MIN_DISCOGS_SIMILARITY:
        return {"matched": False, "confidence": 0.0, "metadata": metadata}

    if artist_verified:
        confidence = DISCOGS_FULL_CONFIDENCE * sim
    else:
        confidence = DISCOGS_UNVERIFIED_WEIGHT * sim

    # Very short titles are prone to collision unless the match is near exact.
    if len(str(title or "").split()) <= 2 and sim < 0.95:
        confidence *= 0.75

    if is_promo:
        confidence = min(confidence, DISCOGS_PROMO_CONFIDENCE_CAP)

    if is_ep_lead:
        confidence = min(confidence, DISCOGS_EP_LEAD_CONFIDENCE_CAP)

    final = round(max(0.0, min(1.0, confidence)), 2)

    minimum_required = (
        DISCOGS_MIN_MATCH_CONFIDENCE
        if artist_verified
        else DISCOGS_MIN_UNVERIFIED_CONFIDENCE
    )

    logger.debug(
        "Discogs confidence calculated", 
        title=title, 
        similarity=round(sim, 3), 
        artist_verified=artist_verified, 
        is_promo=is_promo, 
        is_ep_lead=is_ep_lead, 
        final_confidence=final
    )

    return {
        "matched": final >= minimum_required,
        "confidence": final,
        "metadata": metadata,
    }


# --- TITLE MATCHING ----------------------------------------------------------

DISCOGS_NOISE_SUFFIX_RE = re.compile(
    r"\s*[-–—]\s*(?:the\s+)?"
    r"(?:single|ep|promo|radio\s+edit|edit|explicit|clean|remaster(?:ed)?|mono|stereo|album\s+version)"
    r"\s*$",
    re.IGNORECASE,
)


def _clean_title_for_comparison(title: str) -> str:
    if not title:
        return ""
    value = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title)
    value = DISCOGS_NOISE_SUFFIX_RE.sub("", value)
    return normalize_title_for_lookup(value)


def release_format_key(formats: Any) -> str:
    """Normalize a Discogs ``format`` value (str or list) to a token string."""
    if not formats:
        return ""
    if isinstance(formats, str):
        parts = re.split(r"[,/]", formats)
    elif isinstance(formats, (list, tuple)):
        parts = [p for f in formats for p in re.split(r"[,/]", str(f))]
    else:
        parts = [str(formats)]
    return " ".join(p.strip().lower() for p in parts if p and p.strip())


_release_format_key = release_format_key


def release_format_tokens(formats: Any) -> set[str]:
    """Return a normalised token set for a Discogs ``format`` value."""
    key = release_format_key(formats)
    if not key:
        return set()
    return set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", key))


def _discogs_title_similarity(local_title: str, candidate_title: str) -> float:
    local_key = _clean_title_for_comparison(local_title)
    candidate_key = _clean_title_for_comparison(candidate_title)
    if not local_key or not candidate_key:
        return 0.0
    if local_key == candidate_key:
        return 1.0

    shorter, longer = (
        (local_key, candidate_key)
        if len(local_key) <= len(candidate_key)
        else (candidate_key, local_key)
    )
    if shorter in longer and len(shorter) / len(longer) >= 0.70:
        return 0.95

    if "/" in (local_title or "") or "/" in (candidate_title or ""):
        for raw in (local_title, candidate_title):
            if "/" not in (raw or ""):
                continue
            primary = re.split(r"\s*/\s*", raw.strip(), maxsplit=1)[0] if raw else ""
            primary_key = _clean_title_for_comparison(primary)
            if not primary_key:
                continue
            if primary_key == local_key or primary_key == candidate_key:
                return 0.95
            for other_key in (local_key, candidate_key):
                if primary_key in other_key and len(primary_key) / len(other_key) >= 0.70:
                    return 0.95

    def _sorted(value: str) -> str:
        return " ".join(sorted(value.split()))

    sim = (
        max(
            _rapidfuzz_fuzz.token_set_ratio(local_key, candidate_key),
            _rapidfuzz_fuzz.partial_ratio(local_key, candidate_key),
        )
        / 100.0
        if _rapidfuzz_fuzz is not None
        else max(
            SequenceMatcher(None, local_key, candidate_key).ratio(),
            SequenceMatcher(None, _sorted(local_key), _sorted(candidate_key)).ratio(),
        )
    )

    local_words = re.findall(r"[a-z0-9]+", local_key)
    cand_words = re.findall(r"[a-z0-9]+", candidate_key)
    if cand_words and len(local_words) > 2 * len(cand_words):
        local_set = set(local_words)
        if all(w in local_set for w in cand_words):
            return 0.60

    return sim


def _release_artist_matches(result_artist: str, query_artist: str) -> bool:
    """Token-aware artist comparison for global Discogs search results."""
    query_key = _normalise_discogs_artist(query_artist)
    result_key = _normalise_discogs_artist(result_artist)

    if not query_key or not result_key:
        return False

    if query_key == result_key:
        return True

    query_tokens = set(query_key.split())
    result_tokens = set(result_key.split())

    # Single-token artist names are too collision-prone for partial matching.
    if len(query_tokens) < 2 or len(result_tokens) < 2:
        return False

    overlap = len(query_tokens & result_tokens)
    denominator = max(len(query_tokens), len(result_tokens))

    return denominator > 0 and (overlap / denominator) >= 0.80


def _search_result_matches_artist(result: dict[str, Any], artist: str) -> bool:
    """True when a Discogs global-search result is credited to ``artist``.

    Discogs ``/database/search`` results carry the artist credit in ONE of two
    places depending on the release: a dedicated ``artist`` field, OR embedded
    in the ``title`` as ``"AFI - The Leaving Song Pt. II"``. The old filter read
    only the ``artist`` field, so every title-embedded result was dropped and the
    global-search fallback could never fire — a second, independent reason the
    reported single was missed even after the artist-releases scan failed.

    Returns False when neither the field nor the title's leading credit segment
    matches, so a genuinely different artist's release is still excluded.
    """
    if _release_artist_matches(str(result.get("artist") or ""), artist):
        return True

    # No usable artist field — fall back to the leading credit segment of the
    # title. Discogs separates "Artist - Title" with an ASCII hyphen or an
    # en/em dash, so split on any of them and compare just the credit.
    title = str(result.get("title") or "")
    if not title:
        return False
    parts = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        return _release_artist_matches(parts[0].strip(), artist)
    return False


# --- TYPES -------------------------------------------------------------------

class DiscogsTrack(TypedDict):
    number: str
    title: str
    artist: str
    duration: int | None
    isrc: str


class DiscogsArtistProfile(TypedDict):
    profile: str
    real_name: str | None
    urls: list[str]
    images: list[dict[str, Any]]


def _parse_discogs_duration(duration_str: str) -> int | None:
    if not duration_str:
        return None
    try:
        parts = duration_str.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        pass
    return None


def resolve_master_formats(releases: list[dict[str, Any]], http: DiscogsHttpClient) -> None:
    resolved = 0
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        if (
            rel.get("type") == "master"
            and not rel.get("format")
            and str(rel.get("role") or "Main").lower() == "main"
            and rel.get("main_release")
        ):
            if resolved >= _MAX_MASTER_FORMAT_RESOLUTIONS:
                continue
            resolved += 1
            try:
                main = http.get_release(rel["main_release"])
                rel["format"] = [
                    " ".join(
                        part
                        for part in (
                            str(f.get("name") or ""),
                            " ".join(str(d) for d in (f.get("descriptions") or [])),
                        )
                        if part
                    )
                    for f in (main.get("formats") or [])
                ]
                rel["track_count"] = len(main.get("tracklist") or []) or None
            except Exception as exc:
                logger.debug(
                    "Master format lookup failed",
                    title=rel.get("title"),
                    error=str(exc),
                )


# --- SERVICE -----------------------------------------------------------------

class DiscogsService:
    def __init__(
        self,
        token: str,
        http_client: DiscogsHttpClient | None = None,
        enabled: bool = True,
    ):
        self.token = token or ""
        self.enabled = enabled
        self.http = http_client or DiscogsHttpClient(token=token)
        self._single_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._video_cache: dict[tuple[str, str], bool] = {}
        self._artist_releases_cache: dict[str, list[dict[str, Any]]] = {}
        self._release_tracks_cache: dict[str, list[DiscogsTrack]] = {}
        self._lock = threading.Lock()

    # -- helpers ----------------------------------------------------------

    def _normalize_title(self, title: str) -> str:
        base = strip_parentheses(strip_featured_guest_suffix(title) or title)
        return normalize_title_for_lookup(base or title)

    def _usable(self) -> bool:
        return bool(
            self.enabled
            and self.token
            and self.token.strip().lower()
            not in ("your_discogs_token", "your_token", "placeholder")
        )

    # -- artist releases --------------------------------------------------

    def _get_artist_releases(self, artist: str) -> list[dict[str, Any]]:
        """Return cached artist releases, fetching outside the service lock.

        Only a completed lookup is cached. A failed or unresolved lookup
        returns an empty list without poisoning the cache for the rest of
        the process.
        """
        key = _artist_cache_key(artist)

        with self._lock:
            cached = self._artist_releases_cache.get(key)
        if cached is not None:
            logger.debug("Discogs artist releases cache hit", artist=artist)
            return cached

        releases: list[dict[str, Any]] = []
        lookup_succeeded = False

        rows = None
        try:
            from services.popularity.release_cache_service import (
                get_cached_artist_release_rows,
            )

            rows = get_cached_artist_release_rows(artist, source="discogs")
        except Exception as exc:
            logger.debug("Release-cache read failed", artist=artist, error=str(exc))

        if rows is not None:
            lookup_succeeded = True
            releases = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                release_type = str(row.get("release_type") or "").strip().lower()
                formats = [release_type] if release_type else []
                if row.get("is_promo"):
                    formats.append("promo")
                releases.append(
                    {
                        "title": str(row.get("title") or ""),
                        "role": "Main",
                        "id": str(row.get("release_id") or ""),
                        "year": row.get("year"),
                        "format": formats,
                        "track_count": row.get("track_count"),
                    }
                )
        else:
            artist_id = self.get_artist_id(artist)
            if artist_id:
                try:
                    releases = (
                        self.http.get_artist_releases_all(artist_id, max_pages=10) or []
                    )
                    lookup_succeeded = True
                    resolve_master_formats(releases, self.http)
                    try:
                        from services.popularity.release_cache_service import (
                            upsert_artist_release_rows,
                        )

                        upsert_artist_release_rows(artist, releases)
                    except Exception as exc:
                        logger.debug(
                            "Release-cache write-back failed",
                            artist=artist,
                            error=str(exc),
                        )
                except Exception as exc:
                    logger.debug(
                        "Artist release lookup failed",
                        artist=artist,
                        artist_id=artist_id,
                        error=str(exc),
                    )

        if lookup_succeeded:
            with self._lock:
                existing = self._artist_releases_cache.get(key)
                if existing is not None:
                    return existing
                self._artist_releases_cache[key] = releases

        return releases

    @staticmethod
    def _release_is_promo(rel: dict[str, Any]) -> bool:
        return "promo" in release_format_tokens(rel.get("format"))

    # -- release scanning -------------------------------------------------

    def _scan_releases(
        self,
        title: str,
        releases: list[dict[str, Any]],
        artist_verified: bool = True,
    ) -> dict[str, Any] | None:
        best_commercial: dict[str, Any] | None = None
        best_promo: dict[str, Any] | None = None
        best_ep: dict[str, Any] | None = None
        best_commercial_score = 0.0
        best_promo_score = 0.0
        best_ep_score = 0.0

        title = strip_featured_guest_suffix(title) or title

        def _status(
            rel: dict[str, Any],
            formats: str,
            is_promo: bool,
            is_single_format: bool,
            is_ep_format: bool,
            sim: float,
        ) -> dict[str, Any]:
            return {
                "is_single": bool(is_single_format),
                "appears_on_ep": bool(is_ep_format),
                "is_ep_lead": False,
                "is_promo": is_promo,
                "release_year": rel.get("year") if isinstance(rel.get("year"), int) else None,
                "release_id": str(rel.get("id") or "") or None,
                "format": formats,
                "similarity": round(sim, 2),
                "artist_verified": artist_verified,
            }

        # The title gate runs FIRST and is purely local. It used to run last,
        # after a `format` requirement that the artist-releases endpoint cannot
        # satisfy: that endpoint reports the PHYSICAL medium ("CD", "Vinyl") or
        # nothing at all, while the SINGLE/EP type token lives in the release
        # detail's `formats[].descriptions` — so a genuine single arrived
        # format-less and was skipped before its (perfect) title match was ever
        # computed. Gating on the title first means a matching release is never
        # discarded for want of a field we can fetch, and the expensive format
        # resolution below only runs for releases that already match.
        for rel in releases:
            if not isinstance(rel, dict):
                continue
            if str(rel.get("role") or "Main").strip().lower() != "main":
                continue

            rel_title = str(rel.get("title") or "")
            if not edition_annotations_compatible(title, rel_title):
                continue
            if not self._normalize_title(rel_title):
                continue

            sim = _discogs_title_similarity(title, rel_title)
            if sim < MIN_DISCOGS_SIMILARITY:
                continue

            tokens = release_format_tokens(rel.get("format"))
            if not (SINGLE_FORMAT_TOKENS & tokens or SUPPORTING_RELEASE_FORMAT_TOKENS & tokens):
                # No single/EP type on the row (common on the artist-releases
                # endpoint) — resolve it from the release detail now that the
                # title has matched. The result is cached on the release dict,
                # so this is paid once per artist, not once per track.
                if not self._resolve_release_format(rel):
                    continue
                tokens = release_format_tokens(rel.get("format"))

            if ALBUM_FORMAT_TOKENS & tokens:
                continue

            is_single_format = bool(SINGLE_FORMAT_TOKENS & tokens)
            is_ep_format = bool(SUPPORTING_RELEASE_FORMAT_TOKENS & tokens)
            if not is_single_format and not is_ep_format:
                continue

            track_count = rel.get("track_count")
            try:
                if track_count and int(track_count) > MAX_SINGLE_TRACKS:
                    continue
            except (TypeError, ValueError):
                pass

            formats = release_format_key(rel.get("format"))
            is_promo = "promo" in tokens
            status = _status(rel, formats, is_promo, is_single_format, is_ep_format, sim)

            if is_single_format and not is_promo:
                if sim > best_commercial_score:
                    best_commercial_score = sim
                    best_commercial = status
                    logger.debug("New best commercial single candidate found", title=title, rel_title=rel_title, sim=round(sim, 3))
            elif is_single_format and is_promo:
                if sim > best_promo_score:
                    best_promo_score = sim
                    best_promo = status
                    logger.debug("New best promo single candidate found", title=title, rel_title=rel_title, sim=round(sim, 3))
            elif is_ep_format:
                if sim > best_ep_score:
                    best_ep_score = sim
                    best_ep = status
                    logger.debug("New best EP candidate found", title=title, rel_title=rel_title, sim=round(sim, 3))

        return best_commercial or best_promo or best_ep

    def _resolve_release_format(self, rel: dict[str, Any]) -> bool:
        """Fetch ``format``/``track_count`` for one release and cache them on it.

        The Discogs ``/artists/{id}/releases`` endpoint does NOT carry the
        single/EP TYPE token — it reports the physical medium ("CD", "Vinyl")
        or nothing — while ``resolve_master_formats`` only covers the first
        ``_MAX_MASTER_FORMAT_RESOLUTIONS`` masters. A single past that cap
        (or a non-master release row, which it skips entirely) therefore
        arrives with no single/EP signal and was invisible to
        ``_scan_releases``.

        Called only after a release's title has already matched the track, so
        the cost is bounded to real candidates rather than the whole
        discography. The resolved values are written back onto ``rel`` — the
        dicts live in ``_artist_releases_cache`` — so the lookup is paid once
        per artist, not once per track. Returns True when a single/EP type
        token was recovered.
        """
        release_id = rel.get("main_release") or rel.get("id")
        if not release_id:
            return False
        try:
            detail = self.http.get_release(release_id)
        except Exception as exc:
            logger.debug(
                "Release format lookup failed",
                release_id=release_id,
                error=str(exc),
            )
            return False
        if not detail:
            return False

        formats = [
            " ".join(
                part
                for part in (
                    str(f.get("name") or ""),
                    " ".join(str(d) for d in (f.get("descriptions") or [])),
                )
                if part
            )
            for f in (detail.get("formats") or [])
        ]
        if not formats:
            return False
        rel["format"] = formats
        if detail.get("tracklist") and not rel.get("track_count"):
            rel["track_count"] = len(detail.get("tracklist") or []) or None
        return bool(release_format_tokens(formats) & (SINGLE_FORMAT_TOKENS | SUPPORTING_RELEASE_FORMAT_TOKENS))

    # -- EP lead track ----------------------------------------------------

    @staticmethod
    def _is_lead_position(position: Any) -> bool:
        """True when a Discogs tracklist position denotes the opening track.

        Handles plain numbering ("1", "01"), vinyl sides ("A1", "A"), and
        disc-qualified numbering ("1-1", "1.1").
        """
        value = str(position or "").strip().upper()
        if not value:
            return False

        # Disc-qualified numbering: only disc one's first track leads.
        match = re.fullmatch(r"(\d+)\s*[-.]\s*(\d+)", value)
        if match:
            return int(match.group(1)) == 1 and int(match.group(2)) == 1

        if value.isdigit():
            return int(value) == 1

        # Vinyl: side A position 1, or a side-A-only marker.
        match = re.fullmatch(r"A(\d*)", value)
        if match:
            return match.group(1) in ("", "1")

        return False

    def _get_release_tracks_cached(self, release_id: str) -> list[DiscogsTrack]:
        if not release_id:
            return []

        with self._lock:
            cached = self._release_tracks_cache.get(release_id)
        if cached is not None:
            logger.debug("Discogs release tracks cache hit", release_id=release_id)
            return cached

        tracks = self.get_release_tracks(release_id)

        # Only cache a non-empty result so a transient failure is retried.
        if tracks:
            with self._lock:
                self._release_tracks_cache[release_id] = tracks

        return tracks

    def _is_ep_lead_track(self, release_id: str, title: str) -> bool:
        """True when ``title`` is the opening track of an EP-sized release.

        EP lead tracks are frequently issued as singles, so they are treated
        as medium-confidence evidence. Deep cuts on the same EP are not.
        """
        tracks = self._get_release_tracks_cached(release_id)
        if not tracks or len(tracks) > MAX_EP_TRACKS:
            logger.debug("Rejected EP lead track: track count missing or exceeds max", track=title, release_id=release_id, track_count=len(tracks))
            return False

        lead: DiscogsTrack | None = None
        for track in tracks:
            if self._is_lead_position(track.get("number")):
                lead = track
                break
        if lead is None:
            lead = tracks[0]

        lead_title = str(lead.get("title") or "")
        if not lead_title:
            return False

        sim = _discogs_title_similarity(title, lead_title)
        is_lead = sim >= MIN_DISCOGS_SIMILARITY
        
        logger.debug(
            "EP lead track evaluation", 
            query_title=title, 
            lead_title=lead_title, 
            similarity=round(sim, 3), 
            accepted=is_lead
        )
        
        return is_lead

    # -- single status ----------------------------------------------------

    def get_single_status(
        self,
        title: str,
        artist: str,
        album_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        empty = {
            "is_single": False,
            "appears_on_ep": False,
            "is_ep_lead": False,
            "is_promo": False,
            "release_year": None,
            "release_id": None,
            "format": "",
            "similarity": 0.0,
            "artist_verified": False,
        }

        if not self._usable() or not title or not artist:
            return dict(empty)

        # NOTE: A special-edition local album does not mean the track was never
        # released as a single. Detection continues; the caller may still use
        # edition context for stricter compatibility checks.
        del album_context

        title_key = self._normalize_title(title)
        cache_key = (_artist_cache_key(artist), title_key)

        with self._lock:
            cached = self._single_cache.get(cache_key)
        if cached is not None:
            logger.debug("Discogs single status cache hit", artist=artist, track=title)
            return cached

        artist_releases = self._get_artist_releases(artist) or []
        # Which artist's cache the match came from — the inverted retry below
        # scans the INVERTED artist's release list, whose cache rows must be
        # the ones corrected by the write-back at the end.
        _cache_artist = artist
        status = self._scan_releases(title, artist_releases, artist_verified=True)

        if status is None:
            logger.debug(
                "No single match on artist releases, trying global search",
                artist=artist,
                track=title,
                release_count=len(artist_releases),
            )

            try:
                results = (
                    self.http.search_database(
                        {
                            "q": f"{strip_featured_artist(artist)} {title_key}",
                            "type": "release",
                            "per_page": 25,
                        }
                    )
                    or []
                )
            except Exception as exc:
                logger.debug(
                    "Discogs global release search failed",
                    artist=artist,
                    track=title,
                    error=str(exc),
                )
                results = []

            results = [
                r
                for r in results
                if isinstance(r, dict)
                and _search_result_matches_artist(r, artist)
            ]
            status = self._scan_releases(title, results, artist_verified=False)
            
            if status:
                logger.debug("Global search found match", track=title, artist=artist, release_id=status.get("release_id"), similarity=status.get("similarity"))

        _inv_used = False
        if status is None or float(status.get("similarity") or 0.0) < INVERTED_RETRY_MIN_SIMILARITY:
            try:
                from services.popularity.popularity_sources import invert_featured_artist

                inverted = invert_featured_artist(artist)
            except Exception:
                inverted = artist

            if inverted and inverted != artist:
                logger.debug("Trying inverted artist search", original=artist, inverted=inverted)
                _std_sim = float(status.get("similarity") or 0.0) if status else 0.0
                inv_status = self._scan_releases(
                    title,
                    self._get_artist_releases(inverted) or [],
                    artist_verified=True,
                )
                if inv_status is None:
                    try:
                        inv_results = (
                            self.http.search_database(
                                {
                                    "q": f"{inverted} {title_key}",
                                    "type": "release",
                                    "per_page": 25,
                                }
                            )
                            or []
                        )
                    except Exception as exc:
                        logger.debug(
                            "Discogs inverted search failed",
                            artist=inverted,
                            track=title,
                            error=str(exc),
                        )
                        inv_results = []
                    inv_status = self._scan_releases(
                        title, inv_results, artist_verified=False
                    )
                if inv_status and float(inv_status.get("similarity") or 0.0) > _std_sim:
                    inv_status["inverted_match_used"] = True
                    status = inv_status
                    _inv_used = True
                    _cache_artist = inverted
                    logger.info(
                        "Inverted artist match retry succeeded",
                        standard_sim=_std_sim,
                        inverted=inverted,
                        sim=inv_status.get("similarity", 0.0),
                    )

        if status is None:
            status = dict(empty)

        # An EP-only match is promoted to a single when the track opens the
        # EP, since lead tracks are commonly issued as singles. Confidence is
        # capped in the medium band by calculate_discogs_confidence().
        if (
            status.get("appears_on_ep")
            and not status.get("is_single")
            and status.get("release_id")
        ):
            if self._is_ep_lead_track(str(status["release_id"]), title):
                status["is_single"] = True
                status["is_ep_lead"] = True
                logger.debug(
                    "EP lead track accepted as single",
                    artist=artist,
                    track=title,
                    release_id=status.get("release_id"),
                    similarity=status.get("similarity"),
                )

        if _inv_used:
            status["inverted_match_used"] = True

        # Persist a confirmed single back to the artist release cache. A
        # release past the 15-master cap (or a non-master row) carries NO
        # format on the artist-releases endpoint, so its cache row holds an
        # UNKNOWN classification and the single just confirmed here came from
        # the on-demand detail lookup in ``_scan_releases``. Without this
        # write-back the answer died with the process and every scan re-paid
        # the Discogs detail call; the next scan's fast path
        # (``get_artist_single_titles``) reads the row instead.
        # UPDATE-only by contract (``persist_resolved_discogs_single``): a
        # global-search match for an uncached release invents no rows.
        # EP-lead promotions are deliberately NOT persisted — an EP is capped
        # at medium confidence and must not enter the fast path as an exact
        # 0.85 cached hit.
        if (
            status.get("is_single")
            and not status.get("is_ep_lead")
            and status.get("release_id")
        ):
            try:
                from services.popularity.release_cache_service import (
                    persist_resolved_discogs_single,
                )

                persist_resolved_discogs_single(
                    artist=_cache_artist,
                    release_id=str(status["release_id"]),
                    is_promo=bool(status.get("is_promo")),
                )
            except Exception as exc:
                logger.debug(
                    "Discogs single write-back failed",
                    artist=_cache_artist,
                    release_id=status.get("release_id"),
                    error=str(exc),
                )

        with self._lock:
            self._single_cache[cache_key] = status

        return status

    def is_single(
        self,
        title: str,
        artist: str,
        album_context: dict[str, Any] | None = None,
    ) -> bool:
        return bool(
            self.get_single_status(title, artist, album_context=album_context).get(
                "is_single"
            )
        )

    # -- official video ---------------------------------------------------

    @staticmethod
    def _is_official_video_for_track(video: dict[str, Any], track_title_lower: str) -> bool:
        video_title = (video.get("title") or "").lower()
        video_desc = (video.get("description") or "").lower()

        official_pattern = re.compile(r"\b(official|promo)\b")
        is_official_or_promo = bool(
            official_pattern.search(video_title) or official_pattern.search(video_desc)
        )

        def _canonical(value: str) -> str:
            return normalize_title_for_lookup(value.replace("'", "").replace("’", ""))

        video_title_cleaned = re.sub(
            r"\s*[\(\[]?(official|music|promo)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*$",
            "",
            video_title,
            flags=re.IGNORECASE,
        ).strip()
        if " - " in video_title_cleaned:
            parts = video_title_cleaned.split(" - ", 1)
            if len(parts) == 2:
                video_title_cleaned = parts[1].strip()

        matches_title = _canonical(track_title_lower) == _canonical(video_title_cleaned)

        if not matches_title and video_desc:
            desc_cleaned = re.sub(
                r"\s*[\(\[]?(official|music|promo)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*",
                "",
                video_desc,
                flags=re.IGNORECASE,
            ).strip()
            if " - " in desc_cleaned:
                parts = desc_cleaned.split(" - ", 1)
                if len(parts) == 2:
                    desc_cleaned = parts[1].strip()
            matches_title = _canonical(track_title_lower) == _canonical(desc_cleaned)

        return is_official_or_promo and matches_title

    def has_official_video(self, title: str, artist: str) -> bool:
        if not self._usable() or not title or not artist:
            return False

        cache_key = (_artist_cache_key(artist), self._normalize_title(title))

        with self._lock:
            cached = self._video_cache.get(cache_key)
        if cached is not None:
            logger.debug("Discogs video cache hit", artist=artist, track=title)
            return cached

        matched = False
        lookup_succeeded = False
        try:
            results = (
                self.http.search_database(
                    {"q": f"{artist} {title}", "type": "master", "per_page": 10}
                )
                or []
            )
            for rel in results[:5]:
                if not isinstance(rel, dict):
                    continue
                master_id = rel.get("id")
                if not master_id:
                    continue
                master = self.http.get_master(master_id)
                if not master:
                    continue
                for video in master.get("videos") or []:
                    if isinstance(video, dict) and self._is_official_video_for_track(
                        video, title.lower()
                    ):
                        matched = True
                        break
                if matched:
                    break
            lookup_succeeded = True
        except Exception as exc:
            logger.debug(
                "Official video check failed",
                artist=artist,
                track=title,
                error=str(exc),
            )

        # A failed lookup must not become a permanent negative result.
        if lookup_succeeded:
            with self._lock:
                self._video_cache[cache_key] = matched

        return matched

    # -- misc lookups -----------------------------------------------------

    def get_artist_id(self, artist: str) -> str | None:
        if not self._usable() or not artist:
            return None
        try:
            results = self.http.search_database(
                {"q": artist, "type": "artist", "per_page": 5}
            )
            if results and isinstance(results, list):
                first = results[0]
                if isinstance(first, dict) and first.get("id"):
                    return str(first["id"])
        except Exception as exc:
            logger.debug("Artist ID lookup failed", artist=artist, error=str(exc))
        return None

    def get_genres(self, title: str = "", artist: str = "") -> list[str]:
        """Return Discogs genres and styles.

        Either or both of ``title`` and ``artist`` may be supplied. Passing
        only ``artist`` performs an artist-level genre lookup.
        """
        if not self._usable():
            return []

        query = " ".join(part for part in (str(artist or ""), str(title or "")) if part).strip()
        if not query:
            return []

        try:
            results = (
                self.http.search_database(
                    {"q": query, "type": "release", "per_page": 5}
                )
                or []
            )
        except Exception as exc:
            logger.debug(
                "Discogs genre lookup failed",
                artist=artist,
                title=title,
                error=str(exc),
            )
            return []

        genres: list[str] = []
        for r in results:
            if not isinstance(r, dict):
                continue
            for value in list(r.get("genre") or []) + list(r.get("style") or []):
                value = str(value or "").strip()
                if value and value not in genres:
                    genres.append(value)
        return genres

    def get_artist_biography(self, artist: str) -> DiscogsArtistProfile:
        empty: DiscogsArtistProfile = {
            "profile": "",
            "real_name": None,
            "urls": [],
            "images": [],
        }
        if not self._usable() or not artist:
            return empty

        try:
            results = self.http.search_database(
                {"q": artist, "type": "artist", "per_page": 1}
            )
            if not results:
                return empty
            artist_id = results[0].get("id") if isinstance(results[0], dict) else None
            data = self.http.get_artist(artist_id) if artist_id else {}
        except Exception as exc:
            logger.debug("Discogs biography lookup failed", artist=artist, error=str(exc))
            return empty

        if not isinstance(data, dict):
            return empty

        return {
            "profile": clean_discogs_biography(data.get("profile", "")),
            "real_name": data.get("realname"),
            "urls": data.get("urls", []) or [],
            "images": data.get("images", []) or [],
        }

    def get_release_tracks(self, release_id: str) -> list[DiscogsTrack]:
        if not self._usable() or not release_id:
            return []
        try:
            release = self.http.get_release(release_id)
        except Exception as exc:
            logger.debug(
                "Discogs release tracklist lookup failed",
                release_id=release_id,
                error=str(exc),
            )
            return []

        if not isinstance(release, dict):
            return []

        tracks: list[DiscogsTrack] = []
        for track in release.get("tracklist", []) or []:
            if not isinstance(track, dict):
                continue
            track_artist = str(track.get("artist") or "")
            if not track_artist:
                artists = track.get("artists") or []
                if isinstance(artists, list) and artists and isinstance(artists[0], dict):
                    track_artist = str(artists[0].get("name") or "")
            tracks.append(
                {
                    "number": track.get("position", ""),
                    "title": track.get("title", ""),
                    "artist": track_artist,
                    "duration": _parse_discogs_duration(track.get("duration", "")),
                    "isrc": "",
                }
            )
        return tracks


# --- BRIDGE FUNCTIONS --------------------------------------------------------

_SERVICE_REGISTRY: dict[str, DiscogsService] = {}
_LAST_GOOD_TOKEN: str | None = None
_INIT_LOCK = threading.RLock()


def _get_service(token: str) -> DiscogsService:
    """Return a per-token Discogs service.

    A per-token registry prevents an empty or placeholder token from evicting
    a working, cache-warm instance.
    """
    global _LAST_GOOD_TOKEN

    token = str(token or "")
    placeholder = token.strip().lower() in (
        "",
        "your_discogs_token",
        "your_token",
        "placeholder",
    )

    if placeholder:
        with _INIT_LOCK:
            if _LAST_GOOD_TOKEN and _LAST_GOOD_TOKEN in _SERVICE_REGISTRY:
                return _SERVICE_REGISTRY[_LAST_GOOD_TOKEN]

    with _INIT_LOCK:
        service = _SERVICE_REGISTRY.get(token)
        if service is None:
            service = DiscogsService(token=token)
            _SERVICE_REGISTRY[token] = service
        if not placeholder:
            _LAST_GOOD_TOKEN = token
        return service


def _config_token() -> str:
    try:
        from helpers.config_helpers import get_config

        cfg = get_config() or {}
        return str(
            (cfg.get("api_integrations", {}).get("discogs", {}) or {}).get("token", "")
            or ""
        )
    except Exception:
        return ""


def is_discogs_single(
    title: str,
    artist: str,
    token: str = "",
    album_context: dict[str, Any] | None = None,
) -> bool:
    return _get_service(token or _config_token()).is_single(
        title, artist, album_context=album_context
    )


def get_discogs_genres(title: str = "", artist: str = "", token: str = "") -> list[str]:
    """Fetch Discogs genres/styles.

    Callers performing an artist-level lookup should pass ``artist=`` by
    keyword rather than relying on positional order.
    """
    return _get_service(token or _config_token()).get_genres(title=title, artist=artist)


def get_discogs_artist_genres(artist: str, token: str = "") -> list[str]:
    """Convenience wrapper for artist-level genre lookups."""
    return _get_service(token or _config_token()).get_genres(title="", artist=artist)


def get_discogs_artist_biography(artist: str, token: str = "") -> DiscogsArtistProfile:
    return _get_service(token or _config_token()).get_artist_biography(artist)


def has_discogs_video(title: str, artist: str, token: str = "") -> bool:
    return _get_service(token or _config_token()).has_official_video(title, artist)


def lookup_discogs_album(artist: str, album: str) -> dict[str, Any]:
    token = _config_token()
    if not token or token.strip().lower() in (
        "your_discogs_token",
        "your_token",
        "placeholder",
    ):
        return {"success": False, "error": "Discogs token not configured"}
    try:
        clean_album = _sanitize_release_name(album)
        service = _get_service(token)
        results = service.http.search_database(
            {"q": f"{artist} {clean_album}", "type": "release", "per_page": 5}
        )
        return {"success": True, "results": results or []}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
