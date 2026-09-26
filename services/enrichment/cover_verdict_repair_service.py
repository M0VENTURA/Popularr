"""Repair cover verdicts destroyed by the shallow clearing branch.

BACKGROUND
----------
``services/popularity/stages/track_stage.py`` used to clear a stored cover
verdict whenever ``detect_cover_song`` (a SHALLOW check) returned ``no_match``
and the track's title had carried "(X Cover)" wording. It set
``is_cover = False`` and deleted "Cover" from both genre columns, before the
DEEP detection pass — MusicBrainz work relations, ISRC, recording relations,
writer coverage — had run.

The shallow check's only real evidence path needs ``work_mbid``, which is not
populated that early, so it answered ``no_match`` for GENUINE covers. Genuine
covers are therefore already damaged in the database, and the reason string it
wrote is the one durable marker of exactly which rows:

    is_cover_reason = 'cover attribution removed from title'

⚠️ The flag alone cannot be used to find them — ``is_cover = 0`` is the normal
state of almost every track in a library. The reason string is the fingerprint:
it was written by that branch and by nothing else, so a row carrying it was
cleared unconditionally rather than on evidence.

WHAT THIS DOES
--------------
Re-flags those tracks so the deep detector assesses them properly on the next
album cover pass, instead of leaving them permanently un-flagged.

It deliberately does NOT try to decide the cover question itself. Deciding is
the deep detector's job, and it needs network evidence this repair has no
business blocking a startup on. Setting ``is_cover = 1`` puts the row back in
front of that pass; if the deep detector finds nothing, its own
``_clear_unconfirmed_verdicts`` clears it again — with evidence, which is the
whole point.

The saved title is left alone. The wording strip was the legitimate half of the
old branch.

⚠️ ``cover_last_checked`` IS reset to NULL, and that is load-bearing. The deep
pass skips any track checked within ``COVER_RECHECK_DAYS`` (90) — so re-flagging
a row while leaving a recent timestamp would put it in front of the detector and
have it SKIPPED as fresh, changing nothing for up to three months. Clearing the
timestamp forces an actual re-assessment.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: The reason string written by the removed destructive branch. Unique to it.
DAMAGED_REASON = "cover attribution removed from title"

#: Written by this repair, so it is idempotent — a second run finds nothing.
#: ⚠️ Must NOT be ``DAMAGED_REASON``: re-flagging keeps the row identifiable as
#: "was repaired", and a rerun must not re-flag it a second time.
REPAIRED_REASON = (
    "Re-flagged for deep detection: the shallow cover check cleared this "
    "verdict without evidence (see cover_verdict_repair_service)"
)


def repair_shallowly_cleared_cover_verdicts() -> dict[str, Any]:
    """Re-flag tracks whose cover verdict was cleared without evidence.

    Returns ``{"repaired": int}``, or ``{"error": str}`` when the table is
    unavailable / the query fails structurally.
    """
    from sqlalchemy import text
    from db.engine import db_session

    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE tracks
                    SET is_cover = 1,
                        is_cover_reason = :repaired_reason,
                        cover_last_checked = NULL
                    WHERE is_cover_reason = :damaged_reason
                      AND is_cover IS NOT TRUE
                      AND cover_manual_override IS NOT TRUE
                """),
                {
                    "repaired_reason": REPAIRED_REASON,
                    "damaged_reason": DAMAGED_REASON,
                },
            )
            repaired = result.rowcount or 0
    except Exception as exc:
        logger.warning("Cover verdict repair skipped", error=str(exc))
        return {"repaired": 0, "error": str(exc)}

    if repaired:
        logger.info(
            "Re-flagged cover verdicts that were cleared without evidence",
            repaired=repaired,
        )
    else:
        logger.debug("No shallowly-cleared cover verdicts to repair")

    return {"repaired": repaired}
