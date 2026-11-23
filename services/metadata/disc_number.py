"""Disc-position normalisation — ONE definition, used by every writer.

⭐ WHY THIS EXISTS

``disc_number`` is written to the database, to the audio files and compared by the
Navidrome import's album fingerprint. Three call sites made three different
decisions about disc 1, and the disagreement was not cosmetic:

* ``payload_builder`` clears ``disc_number`` on a one-disc release, because
  storing "1" makes Navidrome mislabel the album (reported as "disc 1 and disc 0",
  and "one track has disc 1, the rest are empty — keeps the 1").
* ``download_completion_service._apply_stored_metadata`` clears disc 1 unless the
  release is known to be multi-disc.
* the album page strips it outright for a single-disc release.

Meanwhile ``disc_number`` is a FINGERPRINT field. So a one-disc album stored the
fingerprint with no disc, Navidrome kept reporting ``discNumber: 1`` for those
very songs, the two sides could never match, and ``should_skip_cached_album``
never skipped — every album re-imported on every scan:

    Re-importing album — song IDs match but metadata changed or cannot be proven
    unchanged album='…' fingerprint_equal=False dirty=False

``dirty=False`` on every line is the tell: nothing was edited, the comparison
itself was impossible.

THE RULE THIS MODULE ENCODES

Disc 1 is the default position of a one-disc release, so it carries no
information the database is meant to hold. Treating "disc 1" as the same thing as
"no disc" wherever the release is not known to be multi-disc makes every writer,
the fingerprint and Navidrome agree. A real move (disc 1 -> disc 2) still
differs, so nothing genuine is hidden.
"""

from __future__ import annotations

from typing import Any

#: A one-disc release has no meaningful position: this CLEARS the frame/column.
NO_DISC = ""

#: Not supplied — leave whatever is stored alone. Used for a multi-disc release
#: whose position is genuinely unknown, where clearing would destroy a fact.
UNKNOWN_DISC = None


def disc_number_int(value: Any) -> int:
    """The disc position as an int, ``0`` when absent or unparseable.

    Accepts the ``"1/2"`` form some taggers write as well as ``"1"`` and ``1``.
    """
    text = str(value if value is not None else "").split("/")[0].strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def disc_total_int(value: Any) -> int:
    """The disc count as an int, ``0`` when absent or unparseable."""
    text = str(value if value is not None else "").split("/")[0].strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def canonical_disc_number(value: Any, *, multi_disc: bool | None = None) -> str:
    """A disc position for COMPARISON, where disc 1 and "no disc" are equal.

    Used by the album fingerprint so a one-disc release compares equal whether it
    stores nothing (what the import writes) or reports ``discNumber: 1`` (what
    Navidrome sends). ``multi_disc=True`` keeps a real position, so a genuine
    change from disc 2 to disc 1 is still visible.
    """
    number = disc_number_int(value)
    if number >= 2:
        return str(number)
    if number == 1 and multi_disc is True:
        return "1"
    return NO_DISC


def stored_disc_number(
    value: Any, *, disc_total: Any = None, multi_disc: bool | None = None
) -> str | None:
    """The ``disc_number`` to STORE for one track (``None`` = leave it alone).

    The write-side companion to :func:`canonical_disc_number`, and the rule
    ``download_completion_service`` already used:

    * a real position (``>= 2``)            -> that number
    * disc 1 on a multi-disc release        -> ``"1"``
    * disc 1 on a single-disc release       -> ``""``  (clear: it means nothing)
    * missing on a multi-disc release       -> ``None`` (unknown; keep the stored
      value rather than inventing a position)
    * missing on a single-disc release      -> ``""``  (clear)

    ``""`` is an EXPLICIT CLEAR for ``tag_file_service.update_file_metadata``
    (``None`` there means "not supplied"), which is what lets a copied file shed
    the disc frame its own rip carried.
    """
    number = disc_number_int(value)
    total = disc_total_int(disc_total)

    if multi_disc is True:
        return str(number) if number >= 1 else UNKNOWN_DISC
    if multi_disc is False:
        return NO_DISC
    # Unknown: fall back to the disc COUNT, then to "disc 1 is meaningless".
    if number >= 2:
        return str(number)
    if number == 1:
        return "1" if total >= 2 else NO_DISC
    return NO_DISC if total < 2 else UNKNOWN_DISC
