"""Fuzzy sanity check before an album MusicBrainz ID is trusted.

WHY THIS EXISTS
---------------
Two places used to treat a stored album MBID as proof of identity:

  * ``services/enrichment/musicbrainz_service._lookup_existing_mbid`` resolves
    the stored ID and returns it with ``confidence: 1.0``, which the album page
    AUTO-SELECTS ahead of every text-search candidate;
  * ``services/metadata/album_service.apply_mbid_to_album`` then writes that ID
    -- plus the MB album-artist/type/country/year tags -- onto **every** track
    and **every** audio file whose ``(artist, album)`` match, without ever
    comparing the ID's own data against the album's text.

Neither step looks at the files.  So a single errant batch-tagging run that
stamps a wrong ID onto a folder is enough to merge unrelated albums: the
reported Metallica / d'Artagnan case, where one poisoned ID grew a 66-track
"super album" out of two different records.

WHAT THIS DOES
--------------
1. **Text check.** Compares the LOCAL artist + album against the MBID's
   resolved artist + title and refuses when they disagree by more than a
   conservative threshold.  Normalisation is deliberately generous --
   edition markers, featured-artist credits ("dArtagnan & The Dark Tenor" vs
   "dArtagnan"), punctuation, case, "Vol. 1" vs "Volume 1" and a leading
   year prefix must all still match -- because a guard that cries wolf gets
   switched off, which is worse than not having one.

2. **Physical boundary.** An album may span more than one folder ONLY when
   those folders are disc folders of one parent (a genuine multi-disc
   release).  Anything else is two different albums that happen to share a
   name, and the fan-out would unify them into one release.

On failure the caller abandons the stored ID and falls back to the normal
text search -- the ID is never written.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable

import structlog

from helpers.normalization_service import (
    normalize_artist,
    normalize_album,
    normalize_string,
    strip_album_edition_marker,
    strip_featured_artist,
)

logger = structlog.get_logger(__name__)

#: Default similarity floor.  Conservative on purpose: 0.65 keeps legitimate
#: variations (editions, "&" credits, "Volume"/"Vol." spellings) matching while
#: rejecting a genuinely different record.
DEFAULT_MIN_SIMILARITY = 0.65

#: Reasons a verdict can be refused.  Empty string means "accepted".
REASON_TEXT_MISMATCH = "text_mismatch"
REASON_MULTIPLE_FOLDERS = "multiple_folders"

#: NOT a refusal: the ID could not be looked up, so there is nothing to compare.
#: See ``compare_album_text`` for why this is allowed rather than blocked.
REASON_UNVERIFIABLE = "unverifiable"

# Disc subfolder names that may legitimately split one album across folders.
_DISC_FOLDER_RE = re.compile(r"^(?:cd|disc|disk)\s*[-_.]?\s*(\d{1,3})$", re.IGNORECASE)

# A leading release year ("2011 - Album") is a folder-naming convention, not
# part of the title, so it must not count against the title comparison.
_LEADING_YEAR_RE = re.compile(r"^\s*(?:19|20)\d{2}\s*[-–—._]?\s*")

# Cheap equivalence fixes for the abbreviations that differ constantly between
# local tags and MusicBrainz without changing meaning.
_VOLUME_WORDS = (
    (re.compile(r"\bvol\b\.?", re.IGNORECASE), "volume"),
    (re.compile(r"\bpt\b\.?", re.IGNORECASE), "part"),
    (re.compile(r"\bpt\b", re.IGNORECASE), "part"),
)

# Placeholders meaning "the track artist is unknown", where the artist field
# carries no identity to compare.
_GENERIC_ARTISTS = frozenset({
    "various artists", "various artist", "various", "va", "v/a",
    "unknown artist", "unknown", "unidentified", "unidentified artist",
    "soundtrack", "ost", "compilation", "-",
})


def _similarity(a: str, b: str) -> float:
    """Token-set similarity in 0..1, order- and subset-tolerant.

    Uses RapidFuzz's ``token_set_ratio`` when available (the same function the
    old ``queue_processor`` used for artist matching), with a pure-Python
    token-overlap fallback so the guard behaves identically without it.
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    try:
        from rapidfuzz import fuzz as _fuzz  # type: ignore[import-untyped]
    except ImportError:
        _fuzz = None

    norm_a = normalize_string(a)
    norm_b = normalize_string(b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    if norm_a in norm_b or norm_b in norm_a:
        return 1.0

    if _fuzz is not None:
        return round(_fuzz.token_set_ratio(norm_a, norm_b) / 100.0, 4)

    tokens_a = set(norm_a.split())
    tokens_b = set(norm_b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = tokens_a & tokens_b
    if not overlap:
        return 0.0
    union = tokens_a | tokens_b
    score = len(overlap) / len(union)
    # All of the shorter side's tokens present = a very strong match even when
    # the longer side carries extra words ("d'Artagnan" vs "d'Artagnan live").
    if overlap == tokens_a or overlap == tokens_b:
        score = max(score, 0.85)
    return round(score, 4)


def _normalise_title_text(value: Any) -> str:
    """Album/title text reduced to its comparable core."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = _LEADING_YEAR_RE.sub("", text)
    text = strip_album_edition_marker(text)
    text = strip_featured_artist(text)
    for pattern, replacement in _VOLUME_WORDS:
        text = pattern.sub(replacement, text)
    return normalize_album(text) or normalize_string(text)


def _normalise_artist_text(value: Any) -> str:
    """Artist text reduced to its comparable core ("& guest" credits removed)."""
    text = str(value or "").strip()
    if not text:
        return ""
    return normalize_artist(text) or normalize_string(strip_featured_artist(text))


def _is_generic_artist(value: str) -> bool:
    return (value or "").strip().lower() in _GENERIC_ARTISTS


def compare_album_text(
    local_artist: str,
    local_album: str,
    mb_artist: str,
    mb_album: str,
) -> dict[str, Any]:
    """Compare local artist/album text against the MBID's resolved text.

    Returns ``{"ok", "reason", "album_similarity", "artist_similarity",
    "local", "mb"}``.  The ARTIST comparison is skipped when either side is a
    generic placeholder ("Various Artists", "Unknown"), because a compilation's
    per-track artists legitimately differ from the release-group credit; the
    album title alone then decides.
    """
    local_album_norm = _normalise_title_text(local_album)
    mb_album_norm = _normalise_title_text(mb_album)
    local_artist_norm = _normalise_artist_text(local_artist)
    mb_artist_norm = _normalise_artist_text(mb_artist)

    album_similarity = _similarity(local_album_norm, mb_album_norm)

    skip_artist = (
        not mb_artist_norm
        or not local_artist_norm
        or _is_generic_artist(local_artist_norm)
        or _is_generic_artist(mb_artist_norm)
    )
    artist_similarity = 1.0 if skip_artist else _similarity(local_artist_norm, mb_artist_norm)

    detail = {
        "local_album": local_album_norm,
        "mb_album": mb_album_norm,
        "local_artist": local_artist_norm,
        "mb_artist": mb_artist_norm,
        "album_similarity": album_similarity,
        "artist_similarity": artist_similarity,
        "artist_compared": not skip_artist,
    }

    if not mb_album_norm:
        # The ID could not be looked up, so there is NO evidence of a conflict.
        #
        # Allowed deliberately, and NOT treated as a mismatch.  Refusing here
        # would mean a MusicBrainz outage (or a release MB that answers without
        # a title) makes every legitimate "Use This Album" a silent no-op —
        # replacing a rare data-corruption bug with a frequent one.  The guard
        # only has standing to refuse when it has actually MEASURED a
        # contradiction, which is the case that matters: a poisoned ID still
        # exists on MusicBrainz and resolves to the wrong record's title.
        return {"ok": True, "reason": REASON_UNVERIFIABLE, **detail}

    threshold = _min_similarity()
    if album_similarity < threshold:
        return {"ok": False, "reason": REASON_TEXT_MISMATCH, **detail}
    if artist_similarity < threshold:
        return {"ok": False, "reason": REASON_TEXT_MISMATCH, **detail}
    return {"ok": True, "reason": "", **detail}


def _min_similarity() -> float:
    try:
        return float(get_album_mbid_guard_config().get("min_similarity") or DEFAULT_MIN_SIMILARITY)
    except Exception:
        return DEFAULT_MIN_SIMILARITY


def get_album_mbid_guard_config() -> dict[str, Any]:
    """Guard config (lazy import keeps config_helpers out of the import cycle)."""
    try:
        from helpers.config_helpers import get_album_mbid_guard_config as _getter
        return _getter()
    except Exception:
        return {
            "enabled": True,
            "min_similarity": DEFAULT_MIN_SIMILARITY,
            "allow_disc_folders": True,
        }


def is_album_mbid_guard_enabled() -> bool:
    return bool(get_album_mbid_guard_config().get("enabled", True))


# ---------------------------------------------------------------------------
# Physical boundary: one folder = one album (except genuine multi-disc)
# ---------------------------------------------------------------------------

def is_disc_folder_name(name: str) -> bool:
    """True for "CD1", "Disc 2", "disk-3", "cd_04"."""
    return bool(_DISC_FOLDER_RE.match((name or "").strip()))


def album_roots_for_paths(file_paths: Iterable[Any]) -> list[str]:
    """Collapse file paths to the set of physical album roots they belong to.

    A disc subfolder belongs to the folder ABOVE it, so
    ``Album/CD1`` + ``Album/CD2`` + bare ``Album`` files all resolve to
    ``Album`` -- one root.  Two unrelated folders stay two roots.
    """
    roots: set[str] = set()
    for raw in file_paths or []:
        path = str(raw or "").strip().replace("\\", "/")
        if not path:
            continue
        folder = os.path.dirname(path)
        if not folder:
            # A bare filename carries no folder evidence; ignore it rather than
            # inventing a root of "" that would look like a second album.
            continue
        parts = folder.split("/")
        if parts and is_disc_folder_name(parts[-1]):
            folder = "/".join(parts[:-1])
        if folder:
            roots.add(folder)
    return sorted(roots)


def check_folder_boundary(
    file_paths: Iterable[Any],
    *,
    allow_disc_folders: bool = True,
) -> dict[str, Any]:
    """Verify the album's tracks live in ONE physical folder.

    Multi-disc releases are the only legitimate reason for an album to occupy
    several folders, and only when every extra folder is a disc folder under
    the same parent.
    """
    roots = album_roots_for_paths(file_paths)
    if len(roots) <= 1:
        return {"ok": True, "reason": "", "folders": roots}
    if allow_disc_folders:
        # ``album_roots_for_paths`` already folded disc subfolders into their
        # parent, so any surplus root is a genuinely different folder.
        pass
    return {"ok": False, "reason": REASON_MULTIPLE_FOLDERS, "folders": roots}


# ---------------------------------------------------------------------------
# Combined verdict
# ---------------------------------------------------------------------------

def guard_album_mbid(
    *,
    artist: str,
    album: str,
    mb_artist: str = "",
    mb_album: str = "",
    file_paths: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """One entry point for every caller that is about to trust an album MBID.

    ``file_paths`` is optional: the album-page lookup has no file list yet and
    only the text check applies there.  When paths ARE supplied the physical
    boundary is enforced too.
    """
    config = get_album_mbid_guard_config()
    if not config.get("enabled", True):
        return {"ok": True, "reason": "disabled", "skipped": True}

    text = compare_album_text(artist, album, mb_artist, mb_album)
    verdict: dict[str, Any] = {
        "ok": text["ok"],
        "reason": text["reason"],
        **{k: v for k, v in text.items() if k not in ("ok", "reason")},
        "threshold": _min_similarity(),
        "folders": [],
    }
    if not text["ok"]:
        return verdict

    if file_paths is not None:
        boundary = check_folder_boundary(
            file_paths,
            allow_disc_folders=bool(config.get("allow_disc_folders", True)),
        )
        verdict["folders"] = boundary["folders"]
        if not boundary["ok"]:
            verdict["ok"] = False
            verdict["reason"] = boundary["reason"]

    return verdict


def log_verdict(verdict: dict[str, Any], *, artist: str, album: str, mbid: str = "", rg_mbid: str = "") -> None:
    """Log a verdict worth seeing: a refusal, or an accepted-but-unverified ID.

    The unverified case is logged because it is the ONLY way a bad ID can still
    slip through, so it has to be greppable in the logs.
    """
    if verdict.get("ok") and verdict.get("reason") != REASON_UNVERIFIABLE:
        return
    if verdict.get("ok"):
        logger.warning(
            "[MB-GUARD] album MBID could NOT be verified - applying anyway",
            artist=artist,
            album=album,
            mbid=mbid or rg_mbid,
            reason=verdict.get("reason"),
        )
        return
    logger.warning(
        "[MB-GUARD] album MBID rejected",
        artist=artist,
        album=album,
        mbid=mbid or rg_mbid,
        reason=verdict.get("reason"),
        album_similarity=verdict.get("album_similarity"),
        artist_similarity=verdict.get("artist_similarity"),
        local_album=verdict.get("local_album"),
        mb_album=verdict.get("mb_album"),
        local_artist=verdict.get("local_artist"),
        mb_artist=verdict.get("mb_artist"),
        folders=verdict.get("folders"),
        threshold=verdict.get("threshold"),
    )


# ---------------------------------------------------------------------------
# The silent fallback: what the ID claimed, and what the text search offers
# ---------------------------------------------------------------------------

def _artist_credit_name(credit: Any) -> str:
    """Flatten a MusicBrainz artist-credit array into one display string.

    Local: avoids importing the private helper from musicbrainz_service, which
    would drag that module (and its importer) into an import cycle.
    """
    if not isinstance(credit, list):
        return ""
    out = ""
    for entry in credit:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or (entry.get("artist") or {}).get("name") or ""
        out += str(name) + str(entry.get("joinphrase") or "")
    return out.strip()


def resolve_mbid_text(mbid: str = "", rg_mbid: str = "") -> dict[str, str]:
    """Return the title/artist the ID ITSELF claims, for the comparison.

    Tries the concrete release first (its release-group supplies the album
    identity), then the release-group directly.  Any failure returns ``{}``,
    which the guard treats as "nothing to compare" and therefore a refusal —
    never as a silent pass.
    """
    try:
        from services.enrichment.musicbrainz_service import get_shared_mb_client

        client = get_shared_mb_client()
    except Exception as exc:
        logger.debug("[MB-GUARD] MusicBrainz client unavailable", error=str(exc))
        return {}

    if mbid:
        try:
            release = client.get_release(mbid, inc="artist-credits+release-groups") or {}
            group = release.get("release-group") or {}
            title = group.get("title") or release.get("title") or ""
            artist = _artist_credit_name(release.get("artist-credit") or [])
            if title:
                return {"title": str(title), "artist": artist, "kind": "release"}
        except Exception as exc:
            logger.debug("[MB-GUARD] release lookup failed", mbid=mbid, error=str(exc))

    if rg_mbid:
        try:
            group = client.get_release_group(rg_mbid, inc="artist-credits") or {}
            title = group.get("title") or ""
            if title:
                return {
                    "title": str(title),
                    "artist": _artist_credit_name(group.get("artist-credit") or []),
                    "kind": "release-group",
                }
        except Exception as exc:
            logger.debug("[MB-GUARD] release-group lookup failed", rg_mbid=rg_mbid, error=str(exc))

    return {}


def text_search_candidates(artist: str, album: str, limit: int = 5) -> list[dict[str, Any]]:
    """Release-group candidates found by TEXT — the fallback when an ID is refused.

    This is the same search the album page's picker uses, so a refused ID still
    leaves the user one click from the right album instead of a dead end.
    """
    try:
        from services.enrichment.musicbrainz_service import get_shared_mb_service

        return get_shared_mb_service().search_releasegroup_matches(
            artist_name=artist, album_name=album, limit=limit,
        ) or []
    except Exception as exc:
        logger.debug("[MB-GUARD] text fallback search failed", artist=artist, album=album, error=str(exc))
        return []


def rejection_message(verdict: dict[str, Any], artist: str, album: str) -> str:
    """Human-readable reason, suitable for an API response or a flash message."""
    if verdict.get("reason") == REASON_MULTIPLE_FOLDERS:
        folders = verdict.get("folders") or []
        return (
            f"'{artist} - {album}' spans {len(folders)} folders that are not disc "
            f"folders of one album, so they are different albums sharing a name — "
            f"refusing to unify them: {', '.join(folders)}"
        )
    return (
        f"Refused '{verdict.get('mb_album') or '?'}' by '{verdict.get('mb_artist') or '?'}': "
        f"it does not match the files tagged '{album}' by '{artist}' "
        f"(album match {verdict.get('album_similarity')}, artist match "
        f"{verdict.get('artist_similarity')}, needs {verdict.get('threshold')})"
    )


def rejection_payload(
    verdict: dict[str, Any],
    *,
    artist: str,
    album: str,
    mbid: str = "",
    rg_mbid: str = "",
    include_candidates: bool = True,
) -> dict[str, Any]:
    """API/return shape for a refused album MBID.

    Carries the refused ID, the reason, the measured similarities, the folders
    involved (boundary failures) and — unless suppressed — the text-search
    candidates to use instead, so no caller has to guess what to do next.
    """
    payload: dict[str, Any] = {
        "success": False,
        "rejected": True,
        "reason": verdict.get("reason"),
        "error": rejection_message(verdict, artist, album),
        "message": rejection_message(verdict, artist, album),
        "refused_mbid": mbid or rg_mbid or "",
        "album_similarity": verdict.get("album_similarity"),
        "artist_similarity": verdict.get("artist_similarity"),
        "threshold": verdict.get("threshold"),
        "folders": verdict.get("folders") or [],
    }
    if include_candidates:
        payload["candidates"] = text_search_candidates(artist, album)
    return payload

