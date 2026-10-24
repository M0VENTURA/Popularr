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

from difflib import SequenceMatcher
import os
import re
from typing import Any

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

_SPLIT_CONTRACTION_RE = re.compile(r"\b([a-z]+)\s+(s|t|m|re|ve|ll|d)\b")
_LEADING_TRACK_NUM_RE = re.compile(r"^\s*\d{1,3}\s+(?=[a-z])")
_NON_LATIN_SCRIPT_RE = re.compile(
    r"[\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F\u4E00-\u9FFF\u3040-\u30FF\u0400-\u04FF]"
)


def _length_tolerance_seconds() -> float:
    """The shared configured length tolerance (see ``get_length_tolerance_seconds``).

    ⚠️ A FUNCTION, not a module constant like the three above. Those are captured
    at import, so a Config-page edit needs a restart; this one is read per
    comparison. A failure falls back to 5s — a broken config must not make the
    matcher reject every download.
    """
    try:
        from helpers.config_helpers import get_length_tolerance_seconds

        return float(get_length_tolerance_seconds())
    except Exception:
        return 5.0


# =============================================================================
# ✅ SIMILARITY
# =============================================================================

def _collapse_for_compare(text: str) -> str:
    """Collapse split English contractions and strip leading track numbers.

    ``normalize_string`` turns ``"Can't"`` into ``"can t"`` while files tagged
    without an apostrophe normalize to ``"cant"``. Likewise embedded TITLE tags
    occasionally carry a leading track number (``"01 The Hollow"``). Collapsing
    both before comparison prevents false ``title_mismatch`` rejections.
    """
    if not text:
        return ""
    collapsed = _LEADING_TRACK_NUM_RE.sub("", text).strip()
    collapsed = _SPLIT_CONTRACTION_RE.sub(r"\1\2", collapsed)
    return re.sub(r"\s+", " ", collapsed).strip()


def _simple_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    a_col = _collapse_for_compare(a)
    b_col = _collapse_for_compare(b)
    if a_col and b_col and a_col == b_col:
        return 1.0

    if a in b or b in a:
        return PARTIAL_MATCH

    if a_col and b_col and (a_col in b_col or b_col in a_col):
        return PARTIAL_MATCH

    ratio = SequenceMatcher(None, a_col or a, b_col or b).ratio()
    if ratio >= 0.85:
        return round(ratio, 3)

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

def _metadata_matches_queue_item(
    file_path: str,
    queue_item: QueueItem,
    threshold: float = THRESHOLD,
    detail: dict[str, Any] | None = None,
):
    """Does this file's metadata match the queue item?

    ``detail`` is an optional out-parameter. When supplied it is filled with WHY
    the verdict came out as it did — the file's own artist and duration, the
    queue item's expected duration, and a reason code for a rejection.

    ⭐ WHY IT EXISTS. Every rejection below returns a bare ``False``, so the
    caller could only ever say "downloaded file did not match queue item" and
    the downloaded file was DELETED on that basis. Reported: *"a section at the
    top for errors that list queue items discarded and the reasons for being
    discarded. Such as wrong artist (noting found artist name), wrong length
    (noting the length)"* — and the artist name and the length were both already
    computed here, then discarded one line later. Nothing needed to be looked up
    again; it needed to be handed back.

    ⚠️ Filling the detail can never change the VERDICT: the value is written
    alongside each return, never in place of it. A caller that ignores ``detail``
    sees exactly the behaviour it saw before.
    """
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
    file_album = (metadata.get("album") or "").strip()

    file_duration_ms = metadata.get("duration_ms")
    file_duration = (file_duration_ms / 1000.0) if file_duration_ms else None

    queue_artist = (queue_item.get("artist") or "").strip()
    queue_album_artist = (queue_item.get("album_artist") or "").strip()
    queue_title = (queue_item.get("title") or "").strip()
    queue_duration = queue_duration_seconds(queue_item.get("duration"))

    # Everything a rejection message needs, recorded up front so each `return`
    # below only has to name its REASON.
    if detail is not None:
        detail.clear()
        detail.update(
            {
                "found_artist": file_artist or file_album_artist,
                "found_album_artist": file_album_artist,
                "found_title": file_title,
                "found_length_seconds": round(file_duration, 1) if file_duration else None,
                "expected_length_seconds": round(queue_duration, 1) if queue_duration else None,
                "expected_artist": queue_artist or queue_album_artist,
                "expected_title": queue_title,
            }
        )

        def _reject(code: str, **extra: Any) -> None:
            """Name the reason for the verdict about to be returned."""
            detail["reason"] = code
            if extra:
                detail.update(extra)

    else:

        def _reject(code: str, **extra: Any) -> None:  # noqa: ARG001
            return None

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
        _reject("edition_mismatch")
        return False

    # ------------------------------------------------------------------
    # Variant logic
    # ------------------------------------------------------------------

    _, queue_variants = extract_version_info(queue_title)
    _, file_variants = extract_version_info(file_title)

    variant_penalty = 0.0
    if _variant_conflict(queue_variants, file_variants):
        # When the queue item explicitly requests a variant (e.g. "Living Dead Girl (remix)")
        # and the embedded TITLE tag omits the suffix because the file comes from a
        # dedicated remix album or carries the remix credit in its filename, check
        # the filename and album tag before applying the conflict penalty.
        if queue_variants:
            _, path_variants = extract_version_info(os.path.basename(file_path))
            _, album_variants = extract_version_info(file_album)
            supplemented = (
                file_variants
                | (path_variants & queue_variants)
                | (album_variants & queue_variants)
            )
            if not _variant_conflict(queue_variants, supplemented):
                file_variants = supplemented
            else:
                variant_penalty = 0.2
        else:
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
        # When one side uses non-Latin script (e.g. Hangul "에라 모르겠다") and the
        # other uses an English/romanised title ("FXXK IT (KR ver.)"), embedded
        # tags cannot string-match even when the filename clearly contains the
        # expected title. Defer to filename matching instead of hard-rejecting.
        file_has_non_latin = bool(_NON_LATIN_SCRIPT_RE.search(file_title))
        queue_has_non_latin = bool(_NON_LATIN_SCRIPT_RE.search(queue_title))
        if file_has_non_latin != queue_has_non_latin:
            return None
        _reject("title_mismatch")
        return False

    if artist_score == 0.0 and title_score < 1.0:
        if file_artist_generic or queue_is_compilation:
            return None
        _reject("wrong_artist")
        return False

    # ------------------------------------------------------------------
    # Duration logic
    # ------------------------------------------------------------------

    if queue_duration and file_duration:
        diff = abs(file_duration - queue_duration)

        # ⚠️ THE STRONG-REJECT USES THE SHARED CONFIGURED TOLERANCE.
        #
        # It was a hardcoded 30 seconds, which made this the most permissive of
        # the app's five length checks — the album page called a 19s difference
        # "a different version of the recording" while this accepted up to 30s.
        # Read per call rather than captured at import so a Config-page edit
        # takes effect without a restart.
        if diff > _length_tolerance_seconds():
            # The numbers the report asks for: what the file actually is, and
            # what the queue item expected.
            _reject(
                "wrong_length",
                length_difference_seconds=round(float(diff), 1),
                length_tolerance_seconds=_length_tolerance_seconds(),
            )
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
            _reject("wrong_artist", length_matched=True)
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
        _reject("wrong_artist", scores={"artist": round(artist_score, 3), "title": round(title_score, 3)})
        return False

    # unclear → let filename/scoring decide
    # ⚠️ NO `_reject` HERE. This is a DEFERRAL, not a rejection: the caller falls
    # through to filename matching and the file may still be accepted. Recording
    # a reason against it would report a discard that never happened.
    return None
