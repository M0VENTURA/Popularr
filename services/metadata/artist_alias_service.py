"""Artist alternate names — one artist under many names.

Renamed or rebranded artists exist under more than one name (Electric Callboy
was Eskimo Callboy). The library can hold both, and every identity comparison
in the app compares artist NAMES — so a recording credited to the band's other
name looked like somebody else and was flagged as a cover of its own band.

This service owns the mapping and the identity merge:

* ``resolve_artist_alias(name)`` — the canonical display name for one artist
  name (the input, unchanged, when it is not a registered alias). Chains are
  followed with a cycle guard, so A→B added before B→C still resolves A→C.
* ``get_alias_group`` / ``add_alias`` / ``remove_alias`` — the page's CRUD,
  returning ``(payload, status)`` in the house style.
* The whole (tiny) table is cached with a TTL and invalidated on every write,
  because ``resolve_artist_alias`` runs inside ``names_match`` during cover
  detection — a hot path that must never issue one query per comparison.

Failure is deliberately boring: if the table cannot be read, resolution
returns the input unchanged. A missing alias map degrades to the pre-alias
behaviour (two names compared as two names), never to a crash.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import structlog

from db.repositories import artist_aliases as repo

logger = structlog.get_logger(__name__)

#: How long a loaded alias map is trusted before re-reading. Writes invalidate
#: immediately (``invalidate_alias_cache``), so the TTL only bounds edits made
#: OUTSIDE the app (a manual SQL fix) — it is not the freshness mechanism.
_CACHE_TTL_SECONDS = 30.0

_CACHE_LOCK = threading.Lock()
_alias_map: dict[str, str] | None = None
_alias_map_at: float = 0.0


def _key(name: str) -> str:
    """The lookup key: trimmed, casefolded. (``lower`` matches the SQL side.)"""
    return str(name or "").strip().lower()


def invalidate_alias_cache() -> None:
    """Drop the cached map — called after every write."""
    global _alias_map
    with _CACHE_LOCK:
        _alias_map = None


def _get_map() -> dict[str, str]:
    """The alias map: casefolded alias → canonical display name.

    One SELECT of the whole table per TTL window (or per write). On a read
    error the LAST GOOD map is kept; a first-load error yields an empty map
    (stamped, so a persistent failure costs one query per TTL, not one per
    comparison).
    """
    global _alias_map, _alias_map_at
    with _CACHE_LOCK:
        now = time.monotonic()
        if _alias_map is not None and (now - _alias_map_at) < _CACHE_TTL_SECONDS:
            return _alias_map
        try:
            rows = repo.load_all_aliases()
            _alias_map = {
                _key(row.get("alias_key")): str(row.get("artist") or "")
                for row in rows
                if _key(row.get("alias_key"))
            }
        except Exception as exc:
            logger.debug("Artist alias cache load failed", error=str(exc))
            if _alias_map is None:
                _alias_map = {}
        _alias_map_at = now
        return _alias_map


def resolve_artist_alias(name: str) -> str:
    """Canonical display name for one artist name.

    Returns the input unchanged when the name is not a registered alias, so
    callers with no aliases registered behave exactly as before. Chains are
    followed with a seen-set cycle guard (max 6 hops — a chain that deep is a
    cycle, not a real merge).
    """
    key = _key(name)
    if not key:
        return name or ""
    alias_map = _get_map()
    if key not in alias_map:
        return name
    seen: set[str] = {key}
    resolved = alias_map[key]
    for _ in range(6):
        next_key = _key(resolved)
        if not next_key or next_key in seen or next_key not in alias_map:
            break
        seen.add(next_key)
        resolved = alias_map[next_key]
    return resolved


def get_alias_group(artist: str) -> tuple[dict[str, Any], int]:
    """Every name in this artist's group, minus the one asked for.

    Both pages of a merged pair show the SAME group: asking by either name
    resolves to the canonical artist first, so the Electric Callboy page lists
    "Eskimo Callboy" and the Eskimo Callboy page lists "Electric Callboy".

    Membership follows RESOLUTION, not the stored pointer: under a chain
    (A→B, B→C) the row for A points at B, but A belongs to C's group because
    that is where it resolves. Listing by ``artist_key`` alone would show A on
    B's page but never on C's — two pages of one group disagreeing.
    """
    artist = str(artist or "").strip()
    if not artist:
        return {"error": "artist is required"}, 400

    try:
        rows = repo.load_all_aliases()
    except Exception as exc:
        logger.error("Failed to list artist aliases", artist=artist, error=str(exc))
        return {"error": str(exc)}, 500

    canonical = resolve_artist_alias(artist)
    canonical_key = _key(canonical)

    # The group is the canonical name plus every alias that RESOLVES into it.
    # The page's OWN name is excluded — a list of one name (itself) is noise.
    members: dict[str, str] = {canonical_key: canonical}
    for row in rows:
        display = str(row.get("alias") or "").strip()
        if not display:
            continue
        if _key(resolve_artist_alias(display)) == canonical_key:
            members[_key(display)] = display

    aliases = sorted(
        (display for key, display in members.items() if key != _key(artist)),
        key=str.lower,
    )
    return {
        "success": True,
        "artist": artist,
        "canonical": canonical,
        "aliases": aliases,
    }, 200


def add_alias(artist: Any, alias: Any) -> tuple[dict[str, Any], int]:
    """Link ``alias`` to ``artist``'s group. Idempotent when already linked."""
    artist = str(artist or "").strip()
    alias = str(alias or "").strip()
    if not artist or not alias:
        return {"error": "artist and alias are required"}, 400
    if _key(artist) == _key(alias):
        return {"error": "an artist cannot be an alternate name of itself"}, 400

    canonical = resolve_artist_alias(artist)
    alias_resolved = resolve_artist_alias(alias)

    # Already in this group (either name asked, either spelling given): report
    # success with the group so the UI just re-renders it.
    if _key(alias_resolved) == _key(canonical):
        group, code = get_alias_group(artist)
        return {**group, "already": True}, code

    existing = None
    try:
        existing = repo.find_alias(_key(alias))
    except Exception as exc:
        logger.error("Artist alias lookup failed", alias=alias, error=str(exc))
        return {"error": str(exc)}, 500

    if existing:
        if _key(existing.get("artist")) == _key(canonical):
            group, code = get_alias_group(artist)
            return {**group, "already": True}, code
        # A name can only belong to one artist — re-pointing it silently would
        # tear the merge out from under whoever owns it today.
        return {
            "error": (
                f"'{alias}' is already an alternate name of "
                f"{existing.get('artist')}"
            ),
        }, 409

    try:
        inserted = repo.insert_alias(
            alias_key=_key(alias),
            alias=alias,
            artist_key=_key(canonical),
            artist=canonical,
        )
    except Exception as exc:
        logger.error(
            "Artist alias insert failed",
            artist=artist, alias=alias, error=str(exc),
        )
        return {"error": str(exc)}, 500

    if not inserted:
        # Lost a race for the key: same response as the conflict above.
        owner = repo.find_alias(_key(alias)) or {}
        return {
            "error": (
                f"'{alias}' is already an alternate name of "
                f"{owner.get('artist') or 'another artist'}"
            ),
        }, 409

    invalidate_alias_cache()
    group, code = get_alias_group(artist)
    return group, code


def remove_alias(artist: Any, alias: Any) -> tuple[dict[str, Any], int]:
    """Unlink a name from ``artist``'s group. Idempotent (gone == done).

    Two shapes of removal, because the group list shows the CANONICAL name as
    an entry on an alias's own page:

    * removing a SIBLING alias unlinks that name from the group;
    * removing the group's canonical name (or the page's own name) from an
      alias's page unlinks the page's own artist — the row binding it into
      the group — because there is no "canonical's row" to delete: under a
      plain pair the canonical never has one.
    """
    artist = str(artist or "").strip()
    alias = str(alias or "").strip()
    if not artist or not alias:
        return {"error": "artist and alias are required"}, 400

    canonical = resolve_artist_alias(artist)
    if _key(alias) in (_key(canonical), _key(artist)):
        target_alias_key = _key(artist)
    else:
        target_alias_key = _key(alias)

    try:
        repo.delete_alias_by_key(target_alias_key)
    except Exception as exc:
        logger.error(
            "Artist alias delete failed",
            artist=artist, alias=alias, error=str(exc),
        )
        return {"error": str(exc)}, 500

    invalidate_alias_cache()
    group, code = get_alias_group(artist)
    return group, code
