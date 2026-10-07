"""Queue Metadata Matcher.

Performs metadata-based matching between queue items and downloaded files.
Uses a tiered matching strategy: metadata comparison, variant detection,
and duration-based fallback matching.

Matching Strategy:
    1. Metadata Match (highest confidence) - artist, title, album comparison.
    2. Variant Detection - soft variants (edit, radio) vs hard (live, remix).
    3. Duration Matching (fallback) - strict (±2s) and lenient (±5s) tolerances.

Configuration: All thresholds configurable via config.yaml under queue.matching.
"""

from __future__ import annotations
from helpers.types_queue import QueueItem
from helpers.metadata_reader import read_mp3_metadata
from helpers.normalization_service import (
    normalize_string,
    normalize_artist,
    normalize_core_title,
    extract_version_info,
    queue_duration_seconds,
    edition_annotations_compatible,
)
from helpers.config_helpers import _GENERIC_COMPILATION_ARTISTS

# Import centralized configuration
from helpers.config_helpers import get_queue_matching_config_v2
from services.queue.queue_matching_config import SOFT_VARIANT_TOKENS

# Load configuration at module initialization
_config = get_queue_matching_config_v2()
THRESHOLD = _config["threshold"]
PARTIAL_MATCH = _config["partial_match"]
STRICT_DURATION_SEC = _config["strict_duration_sec"]
TOLERANCE_DURATION_SEC = _config["tolerance_duration_sec"]


# =============================================================================
# ✅ SIMILARITY
# =============================================================================

def _simple_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    if a in b or b in a:
        return PARTIAL_MATCH

    return 0.0


# =============================================================================
# ✅ VARIANT HANDLING
# =============================================================================

def _variant_conflict(queue_v: set[str], file_v: set[str]) -> bool:
    if queue_v and file_v:
        return queue_v.isdisjoint(file_v)

    if file_v and not queue_v:
        return not file_v.issubset(SOFT_VARIANT_TOKENS)

    if queue_v and not file_v:
        return not queue_v.issubset(SOFT_VARIANT_TOKENS)

    return False


# =============================================================================
# ✅ MATCHER
# =============================================================================

def _metadata_matches_queue_item(file_path: str, queue_item: QueueItem, threshold: float = THRESHOLD):

    try:
        metadata = read_mp3_metadata(file_path) or {}
    except Exception:
        return None

    # ------------------------------------------------------------------
    # Extract fields
    # ------------------------------------------------------------------

    file_artist = (metadata.get("artist") or "").strip()
    file_album_artist = (metadata.get("album_artist") or "").strip()
    file_title = (metadata.get("title") or "").strip()
    
    file_duration_ms = metadata.get("duration_ms")
    file_duration = (file_duration_ms / 1000.0) if file_duration_ms else None

    queue_artist = (queue_item.get("artist") or "").strip()
    queue_album_artist = (queue_item.get("album_artist") or "").strip()
    queue_title = (queue_item.get("title") or "").strip()
    queue_duration = queue_duration_seconds(queue_item.get("duration"))

    # ------------------------------------------------------------------
    # Missing data → defer
    # ------------------------------------------------------------------

    if not file_title:
        return None

    if not queue_title:
        return None

    # ------------------------------------------------------------------
    # Normalize
    # ------------------------------------------------------------------

    file_artist_norm = normalize_artist(file_artist)
    file_album_artist_norm = normalize_artist(file_album_artist)

    queue_artist_norm = normalize_artist(queue_artist)
    queue_album_artist_norm = normalize_artist(queue_album_artist)

    file_title_norm = normalize_core_title(file_title)
    queue_title_norm = normalize_core_title(queue_title)

    # ------------------------------------------------------------------
    # Edition annotations ("Valhalla (Epic Edition)") must never collapse
    # onto the plain "Valhalla" — normalize_core_title strips brackets on
    # both sides, so without this gate an epic-edition download is falsely
    # imported against the non-edition queue item.
    # ------------------------------------------------------------------

    if not edition_annotations_compatible(file_title, queue_title):
        return False

    # ------------------------------------------------------------------
    # Variant logic
    # ------------------------------------------------------------------

    _, queue_variants = extract_version_info(queue_title)
    _, file_variants = extract_version_info(file_title)

    variant_penalty = 0.0
    if _variant_conflict(queue_variants, file_variants):
        variant_penalty = 0.2

    # ------------------------------------------------------------------
    # Artist match (multi-source)
    # ------------------------------------------------------------------

    artist_scores = []

    for candidate in [queue_artist_norm, queue_album_artist_norm]:
        if not candidate:
            continue

        artist_scores.append(_simple_similarity(file_artist_norm, candidate))
        artist_scores.append(_simple_similarity(file_album_artist_norm, candidate))

    artist_score = max(artist_scores) if artist_scores else 0.0

    # ------------------------------------------------------------------
    # Title match
    # ------------------------------------------------------------------

    title_score = _simple_similarity(file_title_norm, queue_title_norm)

    # ------------------------------------------------------------------
    # Compilation tolerance
    # ------------------------------------------------------------------
    # Compilation releases (e.g. "Greatest Hits") routinely tag the embedded
    # artist as "Various Artists"/"VA" — or omit it entirely — while the queue
    # item carries the per-track artist. An artist mismatch under those
    # circumstances is NOT evidence the file is wrong; defer to filename
    # matching (None) rather than hard-rejecting a correctly-named download.

    file_artist_generic = (
        not file_artist_norm
        or file_artist_norm in _GENERIC_COMPILATION_ARTISTS
    )
    queue_is_compilation = (
        queue_album_artist_norm in _GENERIC_COMPILATION_ARTISTS
        or queue_artist_norm in _GENERIC_COMPILATION_ARTISTS
    )

    # ------------------------------------------------------------------
    # Early hard rejection
    # ------------------------------------------------------------------

    if title_score == 0.0:
        return False

    if artist_score == 0.0 and title_score < 1.0:
        if file_artist_generic or queue_is_compilation:
            return None
        return False

    # ------------------------------------------------------------------
    # Duration logic
    # ------------------------------------------------------------------

    if queue_duration and file_duration:
        diff = abs(file_duration - queue_duration)

        if diff > 30:
            return False  # strong reject

        if diff <= STRICT_DURATION_SEC:
            # Exact title + duration is only "very strong evidence" when the
            # artist ALSO agrees — a same-length track by a DIFFERENT artist
            # (e.g. an unmatched download that shares the title) must never be
            # claimed on duration alone.
            if artist_score > 0.0:
                return True
            if (
                queue_is_compilation
                and title_score >= 0.99
                and not _variant_conflict(queue_variants, file_variants)
            ):
                # VA queue row ("Various Artists"): the real credited artist
                # can NEVER string-match the placeholder, so exact title +
                # strict duration IS the verification.  Reported case:
                # "Weak and Powerless" / "Unholy Confessions" queued under
                # Various Artists were deleted the moment their file tags
                # named A Perfect Circle / Avenged Sevenfold.
                return True
            if file_artist_generic or queue_is_compilation:
                # Artist unknown / compilation → defer to filename matching.
                return None
            return False  # concrete artist mismatch → reject

    # ------------------------------------------------------------------
    # Combined score
    # ------------------------------------------------------------------

    combined = ((artist_score + title_score) / 2) - variant_penalty

    if combined >= threshold:
        return True

    # ------------------------------------------------------------------
    # Soft fallback vs hard mismatch
    # ------------------------------------------------------------------

    if artist_score == 0.0:
        if file_artist_generic or queue_is_compilation:
            return None
        return False

    # unclear → let filename/scoring decide
    return None
