"""Explicit ownership types for the playlists Popularr manages.

Why this exists
---------------
Genre playlists and decade playlists share a name shape::

    Alternative Rock - Top Tracks
    1980s - Top Tracks

The genre orphan cleaner used to treat "anything matching the genre name
template" as a genre playlist, so every ``1980s - Top Tracks`` written by the
decade builder was classified as an orphan, deleted, and then re-created by the
next global rebuild. Ownership is a property of the BUILDER, not of the name, so
it is recorded explicitly here and stored in the playlist index.

``classify_managed_playlist`` is deliberately conservative: it returns ``None``
when it cannot prove ownership. Destructive callers must treat ``None`` as
"leave it alone".
"""

from __future__ import annotations

import re
from typing import Any


GENRE = "genre"
DECADE = "decade"
NEW_MUSIC = "new_music"
ESSENTIAL = "essential"
FAVOURITES = "favourites"
CURATED = "curated"
MOOD = "mood"

MANAGED_TYPES = frozenset(
    {GENRE, DECADE, NEW_MUSIC, ESSENTIAL, FAVOURITES, CURATED, MOOD}
)

_DECADE_RE = re.compile(r"^(?:19|20)\d{2}$")
_DEFAULT_GENRE_TEMPLATE = "{genre} - Top Tracks"
_DEFAULT_DECADE_TEMPLATE = "{decade}s - Top Tracks"
_DEFAULT_ESSENTIAL_TEMPLATE = "{artist} - Essential Collection"
_DEFAULT_MOOD_TEMPLATE = "{mood} - Mix"


def _config() -> dict[str, Any]:
    try:
        from helpers.config_helpers import get_playlists_config

        return get_playlists_config() or {}
    except Exception:
        return {}


def _template_value(name: str, template: str, token: str) -> str | None:
    """Return the part of *name* that fills *token* in *template*.

    ``None`` means the name does not fit the template. A template that does not
    contain the token proves nothing, so it also returns ``None``.
    """
    template = str(template or "")
    if token not in template:
        return None
    prefix, suffix = template.split(token, 1)
    text = str(name or "").strip()
    if not text:
        return None
    prefix_cf = prefix.casefold()
    suffix_cf = suffix.casefold()
    text_cf = text.casefold()
    if prefix_cf and not text_cf.startswith(prefix_cf):
        return None
    if suffix_cf and not text_cf.endswith(suffix_cf):
        return None
    start = len(prefix)
    end = len(text) - len(suffix) if suffix else len(text)
    if end <= start:
        return None
    return text[start:end].strip()


def _matches_exact(name: str, candidates: list[str]) -> bool:
    key = str(name or "").strip().casefold()
    return bool(key) and key in {str(c).strip().casefold() for c in candidates if str(c).strip()}


def managed_name_value(
    name: str,
    managed_type: str,
    *,
    config: dict[str, Any] | None = None,
) -> str | None:
    """The ``{genre}``/``{artist}``/``{decade}``/``{mood}`` a name encodes.

    The inverse of the naming templates, used to rebuild ONE managed playlist
    from its name (the playlists page's per-playlist Regenerate action). Returns
    ``None`` when the type carries no name value or the name does not fit the
    configured template.
    """
    cfg = config if isinstance(config, dict) else _config()
    text = str(name or "").strip()
    if not text:
        return None
    if managed_type == GENRE:
        return _template_value(
            text,
            str(cfg.get("genre_playlists_name_template") or _DEFAULT_GENRE_TEMPLATE),
            "{genre}",
        )
    if managed_type == DECADE:
        return _template_value(
            text,
            str(cfg.get("decade_playlists_name_template") or _DEFAULT_DECADE_TEMPLATE),
            "{decade}",
        )
    if managed_type == ESSENTIAL:
        return _template_value(
            text,
            str(cfg.get("essential_name_template") or _DEFAULT_ESSENTIAL_TEMPLATE),
            "{artist}",
        )
    if managed_type == MOOD:
        return _template_value(
            text,
            str(cfg.get("mood_playlists_name_template") or _DEFAULT_MOOD_TEMPLATE),
            "{mood}",
        )
    return None


def classify_managed_playlist(
    name: str,
    *,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Return the ownership type for *name*, or ``None`` when unproven.

    ORDER MATTERS. Decade is checked before genre because both templates end in
    ``- Top Tracks``: ``1980s - Top Tracks`` fits ``{genre} - Top Tracks`` with
    ``genre="1980s"``, which is exactly the mistake that deleted the decade
    playlists.
    """
    cfg = config if isinstance(config, dict) else _config()
    text = str(name or "").strip()
    if not text:
        return None

    # 1. Decade ------------------------------------------------------------
    decade_value = _template_value(
        text,
        str(cfg.get("decade_playlists_name_template") or _DEFAULT_DECADE_TEMPLATE),
        "{decade}",
    )
    if decade_value is not None and _DECADE_RE.match(decade_value):
        return DECADE

    # 2. Fixed-name playlists ---------------------------------------------
    if _matches_exact(text, ["New Music"]):
        return NEW_MUSIC

    try:
        from helpers.config_helpers import get_favourite_playlists_config

        fav = get_favourite_playlists_config() or {}
        favourite_names = [
            str(fav.get("favourite_artists_name") or "Favourite Artists"),
            str(fav.get("favourite_albums_name") or "Favourite Albums"),
        ]
    except Exception:
        favourite_names = ["Favourite Artists", "Favourite Albums"]
    if _matches_exact(text, favourite_names):
        return FAVOURITES

    curated_names = [
        str(cfg.get("hit_singles_playlist_name") or "Hit Singles"),
        str(cfg.get("hidden_gems_playlist_name") or "Hidden Gems"),
        str(cfg.get("new_releases_playlist_name") or "New Releases"),
        str(cfg.get("covers_playlist_name") or "Great Covers"),
        str(cfg.get("acoustic_playlist_name") or "Acoustic Essentials"),
        str(cfg.get("high_energy_playlist_name") or "High Energy & Upbeat"),
    ]
    if _matches_exact(text, curated_names):
        return CURATED

    # 2b. Per-genre Great Covers ------------------------------------------
    # One curated builder owns these, but their names carry a genre token, so
    # the generic genre rule must not see them first.
    covers_genre_template = str(
        cfg.get("covers_genre_playlists_name_template") or "{genre} - Great Covers"
    )
    genre_template = str(
        cfg.get("genre_playlists_name_template") or _DEFAULT_GENRE_TEMPLATE
    )
    # If a hand-edited config gave both families the SAME template, the name
    # cannot prove which builder owns it. Leave it to the genre rule rather than
    # reclassifying every Top Tracks chart as a covers list.
    if covers_genre_template.strip().casefold() != genre_template.strip().casefold():
        covers_genre_value = _template_value(text, covers_genre_template, "{genre}")
        if covers_genre_value is not None and not _DECADE_RE.match(covers_genre_value):
            return CURATED

    # 3. Essential collection ---------------------------------------------
    if _template_value(
        text,
        str(cfg.get("essential_name_template") or _DEFAULT_ESSENTIAL_TEMPLATE),
        "{artist}",
    ) is not None:
        return ESSENTIAL

    # 4. Mood mixes --------------------------------------------------------
    if _template_value(
        text,
        str(cfg.get("mood_playlists_name_template") or _DEFAULT_MOOD_TEMPLATE),
        "{mood}",
    ) is not None:
        return MOOD

    # 5. Genre top tracks --------------------------------------------------
    # Only reached once every other ownership shape has been ruled out, so a
    # decade or curated name can never be mistaken for a genre.
    genre_value = _template_value(
        text,
        str(cfg.get("genre_playlists_name_template") or _DEFAULT_GENRE_TEMPLATE),
        "{genre}",
    )
    if genre_value is None:
        return None
    if _DECADE_RE.match(genre_value):
        # A custom decade template that does not contain "{decade}s" can still
        # leave the bare decade here. Refuse rather than delete.
        return None
    return GENRE
