"""Main-genre taxonomy for generated playlists.

A **main genre** is a ROOT entry in the genre vocabulary (``specificity == 0``):
Rock, Pop, Punk, Metal, Electronic, Hip-Hop and so on. Every subgenre resolves
to its declared family root — ``nu metal`` -> Metal, ``grunge`` -> Rock,
``pop punk`` -> Punk — so a generated playlist is never named after a subgenre.

The vocabulary is data (``services/enrichment/genre_vocabulary.json``), so the
root list grows with it rather than being hard-coded here.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: Used only when the vocabulary cannot be read at all.
_FALLBACK_CHOICES: tuple[tuple[str, str], ...] = (
    ("rock", "Rock"),
    ("pop", "Pop"),
    ("punk", "Punk"),
    ("metal", "Metal"),
)

#: The selection a fresh install gets for the per-genre Great Covers playlists.
DEFAULT_COVER_MAIN_GENRES: tuple[str, ...] = ("rock", "pop", "punk", "metal")


def normalize_main_genre_key(value: Any) -> str:
    """Canonical id form for a main-genre value (``"R&B"`` -> ``"r_and_b"``)."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def main_genre_choices() -> list[dict[str, str]]:
    """Every root genre as ``{"id", "display"}``, sorted by display name."""
    try:
        from services.enrichment.genre_vocabulary import get_vocabulary

        vocab = get_vocabulary()
        choices = [
            {"id": gid, "display": entry.display}
            for gid, entry in vocab.genres.items()
            if entry.specificity == 0
        ]
        if choices:
            return sorted(choices, key=lambda c: c["display"].casefold())
    except Exception:
        pass
    return [{"id": gid, "display": display} for gid, display in _FALLBACK_CHOICES]


def main_genre_ids() -> list[str]:
    return [choice["id"] for choice in main_genre_choices()]


def classify_main_genre(tokens: Iterable[Any]) -> dict[str, str] | None:
    """Resolve genre *tokens* to the main genre that decides them, or ``None``.

    Uses the vocabulary's deciding-family rule (most specific genre wins, given
    order breaks ties), so ``["Grunge", "Alternative Rock"]`` answers Rock and
    ``["Nu Metal"]`` answers Metal. Values the vocabulary does not recognise as
    genres (moods, geography, performance tags) never vote.
    """
    values = [str(token) for token in (tokens or []) if str(token or "").strip()]
    if not values:
        return None

    try:
        from services.enrichment.genre_vocabulary import get_vocabulary

        vocab = get_vocabulary()
        family, _unknown = vocab.deciding_family(values)
        if not family:
            return None

        entry = vocab.genres.get(family)
        if entry is not None and entry.specificity == 0:
            return {"id": entry.id, "display": entry.display}

        # A family normally names its root's id, but resolve through the
        # vocabulary rather than assuming that spelling.
        for gid, candidate in vocab.genres.items():
            if candidate.specificity == 0 and (
                gid == family or candidate.family == family
            ):
                return {"id": gid, "display": candidate.display}

        return {
            "id": str(family),
            "display": entry.display if entry is not None else family.replace("_", " ").title(),
        }
    except Exception:
        return None
