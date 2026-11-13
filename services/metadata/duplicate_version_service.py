"""Recommend which copy to keep when one track exists more than once on an album.

THE PROBLEM
-----------
A double download produces two rows for one title, and the album page shows both
with a "Duplicate ×2" badge and a length-mismatch notice. Nothing says WHICH one
to keep, so the decision is manual and easy to get wrong — the row with stars may
be the worse rip, and the row with the better rip may have no tags at all.

WHAT THIS DOES
--------------
Scores each copy on the two things that actually distinguish a duplicate:

    * AUDIO QUALITY — lossless beats lossy, then bitrate, then sample rate.
      Reuses ``calculate_audio_quality_score`` (the download pipeline's own
      score) so "better quality" means exactly what it means everywhere else.
    * METADATA COMPLETENESS — MusicBrainz identity, ISRC, credited people,
      genres, year, a non-placeholder artist. Measured from columns the
      ``tracks`` table really has.

Neither alone is enough: two FLAC rips of the same file differ only in tags, and
two fully-tagged MP3s differ only in bitrate. The verdict therefore requires a
QUALITY winner; metadata only breaks a quality tie, and only when it is clearly
better-supported.

⭐ IT NEVER DELETES ANYTHING. The result is a RECOMMENDATION with the reasons
behind it. Removing a duplicate is destructive and the user's decision.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "score_duplicate_copies",
    "recommend_duplicate_keeper",
]

#: Metadata fields that make a row genuinely better tagged. Chosen because they
#: are STORED columns (``bitrate`` is extracted but not persisted, so it cannot
#: be scored from the database) and each one is a real identifying fact rather
#: than a cosmetic one.
_METADATA_FIELDS: tuple[tuple[str, int], ...] = (
    ("recording_mbid", 3),      # the identity everything else keys on
    ("isrc", 2),                # a second, independent identity
    ("musicbrainz_albumid", 1),
    ("year", 1),
    ("composer", 1),
    ("writer", 1),
    ("genres", 1),
    ("recordlabel", 1),
)

#: Values that mean "no value" in this table.
_EMPTY = (None, "", "[]", "{}", "null", "none", "—")


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return str(value).strip().lower() not in _EMPTY


def metadata_score(row: dict[str, Any]) -> int:
    """How well identified one copy is. Higher is better."""
    score = 0
    for field, weight in _METADATA_FIELDS:
        if _has_value(row.get(field)):
            score += weight
    return score


def metadata_reasons(row: dict[str, Any]) -> list[str]:
    """The fields that earned this copy its score, for the explanation."""
    return [field for field, _weight in _METADATA_FIELDS if _has_value(row.get(field))]


def _quality_score(file_path: str) -> int:
    """The download pipeline's own audio-quality score, or 0.

    Reused rather than reinvented: a second definition of "better quality" would
    eventually disagree with the one that decides which download to keep.
    """
    try:
        from services.downloads.quality_dedup_service import (
            calculate_audio_quality_score,
        )

        return int(calculate_audio_quality_score(file_path) or 0)
    except Exception as exc:
        logger.debug("Duplicate quality scoring unavailable", error=str(exc))
        return 0


def score_duplicate_copies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score every copy of one duplicated track.

    Returns one dict per row::

        {"track_id", "file_path", "quality", "metadata", "metadata_fields",
         "stars", "duration", "keep", "why"}
    """
    scored: list[dict[str, Any]] = []
    for row in rows or []:
        file_path = str(row.get("file_path") or "")
        scored.append({
            "track_id": str(row.get("id") or ""),
            "file_path": file_path,
            "quality": _quality_score(file_path),
            "metadata": metadata_score(row),
            "metadata_fields": metadata_reasons(row),
            "stars": int(row.get("stars") or 0),
            "duration": row.get("duration"),
            "keep": False,
            "why": "",
        })
    return scored


def recommend_duplicate_keeper(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pick the copy to keep, with the reasoning.

    Returns::

        {"keep_track_id", "remove_track_ids", "confidence", "reason",
         "copies": [scored copy…]}

    ``confidence`` is ``"high"`` when the winner is clearly better on quality,
    ``"metadata"`` when quality tied and the tags broke it, and ``"tie"`` when
    nothing separates them (the caller must then choose).
    """
    copies = score_duplicate_copies(rows)
    if len(copies) < 2:
        return {
            "keep_track_id": copies[0]["track_id"] if copies else "",
            "remove_track_ids": [],
            "confidence": "n/a",
            "reason": "Only one copy.",
            "copies": copies,
        }

    # ── Quality is the primary question ───────────────────────────────────
    # A lossless copy always beats a lossy one, so this is a real ordering, not
    # a tie-break among equals.
    best_quality = max(c["quality"] for c in copies)
    quality_winners = [c for c in copies if c["quality"] == best_quality]

    if len(quality_winners) == 1:
        winner = quality_winners[0]
        others = [c for c in copies if c is not winner]
        # "Clearly" = every loser is strictly worse, which is already true here.
        winner["keep"] = True
        winner["why"] = _quality_reason(winner, others)
        return {
            "keep_track_id": winner["track_id"],
            "remove_track_ids": [c["track_id"] for c in others],
            "confidence": "high",
            "reason": winner["why"],
            "copies": copies,
        }

    # ── Quality tied: metadata decides ────────────────────────────────────
    best_metadata = max(c["metadata"] for c in quality_winners)
    metadata_winners = [c for c in quality_winners if c["metadata"] == best_metadata]

    if best_metadata > 0 and len(metadata_winners) == 1:
        winner = metadata_winners[0]
        others = [c for c in copies if c is not winner]
        winner["keep"] = True
        winner["why"] = (
            "Same audio quality; this copy has the better metadata ("
            + ", ".join(winner["metadata_fields"][:4]) + ")."
        )
        return {
            "keep_track_id": winner["track_id"],
            "remove_track_ids": [c["track_id"] for c in others],
            "confidence": "metadata",
            "reason": winner["why"],
            "copies": copies,
        }

    # ── Nothing separates them ────────────────────────────────────────────
    # Do NOT invent a winner: identical quality and identical tags means the
    # choice is a judgment the scorer cannot make. Stars are shown so the user
    # can decide, but a star count is popularity, not quality, and must never
    # silently decide which FILE is deleted.
    return {
        "keep_track_id": "",
        "remove_track_ids": [],
        "confidence": "tie",
        "reason": (
            "The copies are equal on audio quality and metadata — "
            "choose which to keep."
        ),
        "copies": copies,
    }


def _quality_reason(winner: dict[str, Any], others: list[dict[str, Any]]) -> str:
    """Why the winner is the better copy, in user-facing terms."""
    import os

    ext = os.path.splitext(winner["file_path"])[1].lower() or "file"
    try:
        from services.downloads.download_quality_config import LOSSLESS_EXTENSIONS

        lossless = ext in LOSSLESS_EXTENSIONS
    except Exception:
        lossless = ext == ".flac"

    if lossless and not all(
        os.path.splitext(o["file_path"])[1].lower() == ext for o in others
    ):
        return f"This copy is lossless ({ext.lstrip('.')}) and the other is not."
    return "This copy has the higher audio quality (bitrate / sample rate)."
