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


def _length_seconds(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    return seconds if seconds > 0 else 0.0


#: Two copies whose lengths differ by less than this are the same length. Round
#: trips through INT seconds make a 1 s difference noise, not a different take.
_LENGTH_SAME_TOLERANCE = 3.0

#: How far the outlier must sit from the winning group to be called wrong.
_LENGTH_OUTLIER_GAP = 5.0


def _length_outlier_winner(
    copies: list[dict[str, Any]],
    expected_duration: float = 0.0,
) -> dict[str, Any] | None:
    """The copy whose length matches the reference, or None.

    ⭐ AN AUTHORITATIVE REFERENCE BEATS A MAJORITY. ``expected_duration`` is the
    release's own length for this track (MusicBrainz / the CSV tracklist), so
    when it is known the answer is exact even with only TWO copies — which is
    the common case ("downloaded twice": one right, one truncated). Without one
    it falls back to the largest cluster of near-equal durations, so 3 copies at
    ~4:05 beat one at 3:12.
    """
    with_duration = [c for c in copies if _length_seconds(c.get("duration")) > 0]
    if len(with_duration) < 2:
        return None

    if _length_seconds(expected_duration) > 0:
        reference = _length_seconds(expected_duration)
        near = [
            c for c in with_duration
            if abs(_length_seconds(c.get("duration")) - reference)
            <= _LENGTH_OUTLIER_GAP
        ]
        # Exactly one copy matches the release → that is the keeper.
        if len(near) == 1 and len(near) < len(with_duration):
            return near[0]
        # None or several match → the reference cannot separate them.
        return None

    remaining = list(with_duration)
    best_cluster: list[dict[str, Any]] = []
    while remaining:
        pivot = _length_seconds(remaining[0].get("duration"))
        cluster = [
            c for c in remaining
            if abs(_length_seconds(c.get("duration")) - pivot) <= _LENGTH_SAME_TOLERANCE
        ]
        # ``>`` only, so the FIRST cluster wins a tie — and a tie means there is
        # no majority, so the check below rejects it.
        if len(cluster) > len(best_cluster):
            best_cluster = cluster
        remaining = [c for c in remaining if c not in cluster]
    # ⚠️ A MAJORITY IS REQUIRED. Every copy agreeing means nothing to decide;
    # and one copy alone is not a majority (240/260/285 has no correct length,
    # so naming the shortest "the winner" would be an invented verdict).
    if len(best_cluster) < 2:
        return None
    if len(best_cluster) == len(with_duration):
        return None
    reference = sorted(
        _length_seconds(c.get("duration")) for c in best_cluster
    )[len(best_cluster) // 2]
    # Every copy outside the winning cluster must be clearly wrong, or the
    # lengths do not identify a single bad file (e.g. 4:00 / 4:20 / 4:45).
    outside = [c for c in with_duration if c not in best_cluster]
    if not outside:
        return None
    if any(
        abs(_length_seconds(c.get("duration")) - reference) <= _LENGTH_OUTLIER_GAP
        for c in outside
    ):
        return None
    return best_cluster[0]


def _copies_disagree_on_length(copies: list[dict[str, Any]]) -> bool:
    """True when the copies do not agree on how long the song is.

    Deliberately the same ``_LENGTH_OUTLIER_GAP`` the outlier rule uses: a
    difference the majority vote would call "clearly wrong" is the same
    difference that makes a Keep/Delete verdict unprovable without a reference.
    Missing durations are ignored — "unknown" is not "different".
    """
    known = [
        seconds for seconds in
        (_length_seconds(c.get("duration")) for c in copies)
        if seconds > 0
    ]
    if len(known) < 2:
        return False
    return (max(known) - min(known)) > _LENGTH_OUTLIER_GAP


def _length_conflict_reason(copies: list[dict[str, Any]]) -> str:
    """Explain a length disagreement the scorer refuses to rule on."""
    seconds = sorted({
        int(round(_length_seconds(c.get("duration"))))
        for c in copies
        if _length_seconds(c.get("duration")) > 0
    })
    shown = ", ".join(f"{s // 60}:{s % 60:02d}" for s in seconds)
    return (
        f"These copies run to different lengths ({shown}) and nothing in their "
        f"tags or encoding says which is the correct one — so neither is marked "
        f"Keep or Delete. Compare the lengths before removing either."
    )


def _length_reason(winner: dict[str, Any], others: list[dict[str, Any]]) -> str:
    def _fmt(value: Any) -> str:
        seconds = int(round(_length_seconds(value)))
        return f"{seconds // 60}:{seconds % 60:02d}" if seconds else "unknown"

    lose = ", ".join(
        _fmt(c.get("duration")) for c in others if _length_seconds(c.get("duration"))
    )
    return (
        f"This copy's length ({_fmt(winner.get('duration'))}) matches the "
        f"correct version; the other copy/copies are {lose}."
    )


def recommend_duplicate_keeper(
    rows: list[dict[str, Any]],
    expected_duration: float = 0.0,
) -> dict[str, Any]:
    """Pick the copy to keep, with the reasoning.

    Returns::

        {"keep_track_id", "remove_track_ids", "confidence", "reason",
         "copies": [scored copy…]}

    ``confidence`` is ``"high"`` when the winner is clearly better on quality,
    ``"length"`` when a PROVEN length (the release's own, or a clear majority)
    names the right copy, ``"metadata"`` when quality tied and the tags broke it,
    and ``"tie"`` when nothing separates them — which now includes copies that
    disagree on length with no reference to settle it. A tie has no
    ``keep_track_id``; the caller must then choose.
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

    # ── ⭐ A PROVEN LENGTH COMES BEFORE QUALITY ────────────────────────────
    #
    # REPORTED: "the tags to show which tracks should be kept and deleted in the
    # album pages are falsely showing to keep tracks with the wrong song length."
    #
    # Quality used to be consulted first, so a higher-bitrate copy of a
    # truncated download (or of a radio edit) was named the keeper however far
    # its length sat from the copy beside it. A wrong-length file is not a
    # BETTER COPY of the song — it is a different recording — so when a length
    # can be proven wrong, that outranks bitrate.
    #
    # "Proven" means the release's own length (``expected_duration``, from the
    # missing-track tracklist) or a clear majority among three or more copies.
    # Both are decided by ``_length_outlier_winner``.
    length_winner = _length_outlier_winner(copies, expected_duration)
    if length_winner is not None:
        winner_seconds = _length_seconds(length_winner.get("duration"))
        same_length = [
            c for c in copies
            if c is not length_winner
            and abs(_length_seconds(c.get("duration")) - winner_seconds)
            <= _LENGTH_SAME_TOLERANCE
        ]
        # Several copies share the winning length: the length rule cannot pick
        # one of them, so fall through to quality/metadata for THOSE — but the
        # outliers have still been proven wrong, so they must never win.
        if not same_length:
            others = [c for c in copies if c is not length_winner]
            length_winner["keep"] = True
            length_winner["why"] = _length_reason(length_winner, others)
            return {
                "keep_track_id": length_winner["track_id"],
                "remove_track_ids": [c["track_id"] for c in others],
                "confidence": "length",
                "reason": length_winner["why"],
                "copies": copies,
            }

    # ── Lengths disagree and nothing can settle it ────────────────────────
    # A "Keep" badge here would be an invented verdict: the rows disagree on
    # how long the song is, and neither tags nor bitrate say which file is the
    # real one. Return a TIE so the page shows a plain "Duplicate ×N" and the
    # user compares the lengths themselves.
    if _copies_disagree_on_length(copies):
        return {
            "keep_track_id": "",
            "remove_track_ids": [],
            "confidence": "tie",
            "reason": _length_conflict_reason(copies),
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
    #
    # ⚠️ LENGTH IS NO LONGER CHECKED HERE. It runs FIRST, over every copy, so a
    # proven-wrong length cannot be outvoted by a quality winner that happened
    # to be in the same quality tier — and an unresolvable length disagreement
    # has already returned a tie. Everything still standing at this point has
    # copies whose lengths agree, so only tags can separate them.
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
