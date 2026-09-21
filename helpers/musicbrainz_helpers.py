"""MusicBrainz UUID extraction helpers.

Provides:
- ``normalize_single_mbid`` – Extract one valid MBID from a multi-MBID string.
- ``get_mbid_from_metadata`` – Find first non-empty MBID across multiple dict keys.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from services.enrichment.musicbrainz_service import get_shared_mb_client

logger = structlog.get_logger(__name__)

_MUSICBRAINZ_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)


def normalize_single_mbid(value: str) -> str:
    """Extract a single MusicBrainz UUID from a string that may contain multiple MBIDs."""
    if not value or not isinstance(value, str):
        return ""
    for part in re.split(r'[;|,\s/]+', value.strip()):
        part = part.strip()
        if _MUSICBRAINZ_UUID_RE.match(part):
            return part
    return ""


def get_mbid_from_metadata(metadata: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty string found in *metadata* under any of *keys*."""
    for key in keys:
        val = str(metadata.get(key) or "").strip()
        if val:
            return val
    return ""


def fetch_writer_credits(title: str, artist: str) -> dict[str, list[str]]:
    """Fetch composer/writer/lyricist credits from MusicBrainz for a track."""
    try:
        mb = get_shared_mb_client()
        recordings = mb.search_recordings(f'artist:"{artist}" recording:"{title}"', limit=5)
        if not recordings:
            return {}

        recording = recordings[0]
        rec_id = recording.get("id", "") if isinstance(recording, dict) else ""
        if not rec_id:
            return {}

        data = mb.get_recording(rec_id, inc="artists work-level-rels work-rels artist-rels")

        composers: list[str] = []
        writers: list[str] = []
        lyricists: list[str] = []

        # Check both modern 'relations' and legacy list keys
        all_rels = (data.get("relations", []) or []) + (data.get("work-relation-list", []) or [])
        for rel in all_rels:
            target_type = (rel.get("target-type") or "").lower()
            if target_type != "work":
                continue
            work = rel.get("work", {})
            work_rels = (work.get("relations", []) or []) + (work.get("artist-relation-list", []) or [])
            for work_rel in work_rels:
                wtype = (work_rel.get("type") or "").lower()
                ac = work_rel.get("artist-credit", []) or []
                for credit in ac:
                    name = credit.get("name") or (credit.get("artist") or {}).get("name") or ""
                    if name:
                        if wtype == "composer" and name not in composers:
                            composers.append(name)
                        elif wtype == "lyricist" and name not in lyricists:
                            lyricists.append(name)
                        elif wtype in ("writer", "text") and name not in writers:
                            writers.append(name)

        return {"composers": composers, "writers": writers, "lyricists": lyricists}
    except Exception as exc:
        logger.debug("Could not fetch writer credits", title=title, artist=artist, error=str(exc))
        return {}


def _coerce_position_to_int(value: Any, default: int) -> int:
    """Convert MusicBrainz position strings (e.g. 'A1', '1/12') into an integer."""
    raw = str(value or '').strip()
    if not raw:
        return default
    if raw.isdigit():
        return int(raw)
    match = re.search(r"\d+", raw)
    if match:
        return int(match.group(0))
    return default


def _build_artist_credit_string(artist_credit: list[Any]) -> str:
    """Build a display string from a MusicBrainz artist-credit array."""
    result = ''
    for credit in artist_credit:
        if isinstance(credit, dict):
            result += credit.get('name', '')
            result += credit.get('joinphrase', '')
        else:
            result += str(credit)
    return result.strip()


# =============================================================================
# MusicBrainz JSON response helpers
# =============================================================================

def artist_from_credit(artist_credit: list[dict[str, Any]]) -> str:
    """Extract the first artist name from a MusicBrainz artist-credit array."""
    for entry in artist_credit or []:
        if isinstance(entry, dict):
            name = (entry.get("artist") or {}).get("name")
            if name:
                return name
    return ""


def year_from_recording(rec: dict[str, Any]) -> int | None:
    """Extract the earliest release year from a MusicBrainz recording dict."""
    date_str = str(rec.get("first-release-date") or "").strip()
    if len(date_str) >= 4 and date_str[:4].isdigit():
        try:
            return int(date_str[:4])
        except (ValueError, TypeError):
            pass
    for rel in rec.get("release-list") or []:
        if isinstance(rel, dict):
            rel_date = str(rel.get("date") or "").strip()
            if len(rel_date) >= 4 and rel_date[:4].isdigit():
                try:
                    return int(rel_date[:4])
                except (ValueError, TypeError):
                    pass
    return None


def extract_work_ids(recording: dict[str, Any]) -> set[str]:
    """Extract all work IDs from a recording's relations."""
    ids: set[str] = set()
    all_rels = (recording.get("relations", []) or []) + (recording.get("work-relation-list", []) or [])
    for rel in all_rels:
        if not isinstance(rel, dict):
            continue
        work = rel.get("work") or (rel.get("target") if rel.get("target-type") == "work" else {})
        wid = work.get("id") if isinstance(work, dict) else None
        if wid:
            ids.add(wid)
    return ids


def extract_cover_work_ids(recording: dict[str, Any]) -> set[str]:
    """Extract work IDs linked by MB 'performance (cover)' or 'cover' relations."""
    ids: set[str] = set()
    all_rels = (recording.get("relations", []) or []) + (recording.get("work-relation-list", []) or [])
    for rel in all_rels:
        if not isinstance(rel, dict):
            continue
            
        rel_type = str(rel.get("type", "")).strip().lower()
        attrs = rel.get("attributes") or rel.get("attribute-list") or []
        is_performance_cover = (rel_type == "performance" and any(str(a).lower() == "cover" for a in attrs))
        is_cover_rel = (rel_type == "cover")
        
        if not is_performance_cover and not is_cover_rel:
            continue
            
        direction = str(rel.get("direction", "")).strip().lower()
        if direction and direction != "forward":
            continue
            
        work = rel.get("work") or (rel.get("target") if rel.get("target-type") == "work" else {})
        wid = work.get("id") if isinstance(work, dict) else None
        if wid:
            ids.add(wid)
    return ids
