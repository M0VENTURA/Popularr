"""Order an album's tracks the way a listener expects.

Reported:

    "The ordering on the album page is sometimes off. For instance Track 13 is
     showing before track 1."

``tracks.disc_number`` and ``tracks.track_number`` are **TEXT**, and every
ordering that existed compared them as text — three different ways, three
different bugs:

* ``album_detail`` ordered by ``COALESCE(disc_number, '1')``. ``COALESCE``
  only replaces NULL, so a **blank** disc tag stayed ``''`` — and ``''`` sorts
  *before* ``'1'``. The one track whose DISC tag was empty therefore jumped to
  the top of the album. That is the reported symptom exactly.
* The same query's numeric key, ``NULLIF(regexp_replace(track_number::text,
  '[^0-9].*$', ''), '')::int``, was right, but the ``track_number`` fallback
  after it was raw text: ``1, 10, 11, …, 2, 3``.
* The artist page sorted in Python with ``safe_int(track_number) or 0``, which
  sends tracks with **no** number to the FRONT of every album.

This module is the single definition both pages share: disc first (numeric,
blank/zero defaulting to 1), then track number (numeric, unnumbered **last**),
then title.
"""

from __future__ import annotations

from typing import Any

#: Where a track with no usable number goes: after every numbered track of its
#: disc. Large enough to sit past any real tracklist (a 10 000-track disc is
#: not a thing) and small enough to stay an int.
_UNNUMBERED = 1_000_000


def leading_int(value: Any) -> int | None:
    """The integer a tag value *starts* with, or ``None`` when it has none.

    Mirrors the SQL ``regexp_replace(x, '[^0-9].*$', '')`` the album page used
    to rely on, so ``'1/2'`` and ``'01'`` behave identically in both:

        '13'    -> 13       '1/2'  -> 1      '01'  -> 1
        'A1'    -> None     ''     -> None   None  -> None
    """
    if value is None:
        return None
    digits = ""
    for char in str(value).strip():
        if char.isdigit():
            digits += char
        else:
            break
    return int(digits) if digits else None


def album_track_sort_key(track: dict[str, Any]) -> tuple[int, int, str]:
    """Sort key: ``(disc, track, title)`` with blanks and blanks-only handled.

    * **disc** — numeric, and a blank/zero/absent disc is disc 1. This is the
      line that fixes "Track 13 before Track 1": without it an empty DISC tag
      sorts ahead of every ``'1'``.
    * **track** — numeric, unnumbered tracks sort AFTER numbered ones rather
      than in front of them.
    * **title** — the tie-break for duplicate or missing numbers.
    """
    disc = leading_int(track.get("disc_number"))
    if not disc or disc < 1:
        disc = 1

    number = leading_int(track.get("track_number"))
    return (disc, number if number is not None else _UNNUMBERED,
            str(track.get("title") or "").casefold())


def sort_album_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return *tracks* in listening order.

    Applied AFTER the query so the ordering no longer depends on SQL that only
    looks right: the album page's ``ORDER BY`` remains as a pre-sort, but this
    is what the user actually sees.
    """
    return sorted(tracks, key=album_track_sort_key)
