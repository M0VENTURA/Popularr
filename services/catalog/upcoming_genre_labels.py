"""Main-genre labels for upcoming releases.

WHY THIS EXISTS
---------------
The upcoming-releases page lists an artist and an album and nothing that says
what KIND of music it is. Scanning a list of fifty rows, "is this the metal one
or the pop one" is the first question and the page could not answer it.

This module turns a release's MusicBrainz genre/tag names into:

* **main genres** — the vocabulary ROOTS a release sits under (Metal, Rock, Pop,
  Punk, Electronic …), used for the row's coloured label;
* **sub genres** — the specific names that produced them (``nu metal``,
  ``alternative rock``), shown as small secondary chips.

Both come from ``services.playlists.main_genres.classify_main_genre``, so the
label on an upcoming row means exactly what a generated playlist name means. A
second genre taxonomy here would eventually disagree with the one the playlist
builder uses, which is the drift this avoids.

⚠️ COLOUR IS NOT DECIDED HERE. The backend answers ``main_genres`` as
``[{"id", "display"}]``; ``static/css/upcoming-genres.css`` maps the id to a
colour. Two reasons: a theme change should not need a server deploy, and the
palette has to stay legible against both the light and dark surfaces, which is
a CSS question rather than a data one.
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = ["describe_release_genres", "genre_names_by_vote", "MAX_MAIN_GENRES", "MAX_SUB_GENRES"]

#: How many main-genre labels a row carries. Two is the useful answer ("is this
#: metal or rock"); beyond that the row reads as an unordered tag cloud and the
#: colour coding stops distinguishing anything.
MAX_MAIN_GENRES = 2

#: How many sub-genre chips ride along in the tooltip/secondary line.
MAX_SUB_GENRES = 4


def _genre_names(entries: Iterable[Any]) -> list[str]:
    """Provider genre/tag entries -> names, strongest first.

    MusicBrainz returns ``[{"name"/"tag": "nu metal", "count": 3}, …]``; a
    couple of call sites store a plain list of strings instead, so both shapes
    are accepted. ``count`` is real evidence (the release-group page shows
    ``pop 14``, ``dance-pop 4``) — discarding it made "whichever happened to be
    first" look like the strongest name.
    """
    ranked: list[tuple[int, int, str]] = []
    for index, entry in enumerate(entries or []):
        if isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("tag") or "").strip()
            try:
                count = int(entry.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
        else:
            name = str(entry or "").strip()
            count = 0
        if name:
            ranked.append((count, -index, name))
    ranked.sort(reverse=True)

    seen: set[str] = set()
    names: list[str] = []
    for _count, _order, name in ranked:
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def genre_names_by_vote(entries: Iterable[Any]) -> list[str]:
    """Provider genre/tag entries -> unique names, strongest first.

    Public because the fetch side needs the same ordering the classifier uses:
    two orderings would let the stored list and the derived label disagree about
    which genre is the release's main one.
    """
    return _genre_names(entries)


def describe_release_genres(entries: Iterable[Any]) -> dict[str, Any]:
    """Classify a release's genre names into main + sub genres.

    Returns ``{"main_genres": [{"id", "display"}], "sub_genres": [str]}``.

    ``main_genres`` is capped at :data:`MAX_MAIN_GENRES` and ordered by how many
    of the release's own names fall under each root, so a release tagged
    ``["metalcore", "metal", "hardcore"]`` leads with Metal. A tie keeps the
    order the roots were first seen in, which follows the vote ranking fed in.

    ``sub_genres`` excludes any name that is itself a main-genre root — a row
    labelled "Metal" does not need a "Metal" chip beside it — and is capped at
    :data:`MAX_SUB_GENRES`.

    A release with no recognisable genre answers empty lists. That is a
    deliberate "unknown", not an error: the vocabularies do not know every tag,
    and an upcoming row with no label is honest where a guessed one is not.
    """
    names = _genre_names(entries)
    if not names:
        return {"main_genres": [], "sub_genres": []}

    from services.playlists.main_genres import (
        classify_main_genre,
        normalize_main_genre_key,
    )

    # One classification per NAME, then vote by root. Classifying the whole list
    # at once returns only the single deciding family, which would throw away the
    # second bucket a two-label row is supposed to show.
    tallies: dict[str, int] = {}
    displays: dict[str, str] = {}
    for name in names:
        found = classify_main_genre([name])
        if not found:
            continue
        root_id = str(found.get("id") or "").strip()
        if not root_id:
            continue
        tallies[root_id] = tallies.get(root_id, 0) + 1
        displays.setdefault(root_id, str(found.get("display") or root_id))

    ordered = sorted(
        tallies,
        key=lambda rid: (-tallies[rid], list(tallies).index(rid)),
    )
    main_genres = [
        {"id": rid, "display": displays[rid]}
        for rid in ordered[:MAX_MAIN_GENRES]
    ]

    root_keys = {normalize_main_genre_key(rid) for rid in tallies}
    root_display_keys = {normalize_main_genre_key(displays[rid]) for rid in tallies}
    sub_genres = [
        name for name in names
        if normalize_main_genre_key(name) not in root_keys
        and normalize_main_genre_key(name) not in root_display_keys
    ][:MAX_SUB_GENRES]

    return {"main_genres": main_genres, "sub_genres": sub_genres}
