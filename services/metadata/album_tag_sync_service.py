"""End-of-album file-tag fill + correction recording (album metadata scan).

Runs once per album at the end of a metadata / full scan, AFTER the per-track
MusicBrainz metadata has been persisted to the ``tracks`` table.  For every
track of the album it:

  • Fills MISSING file tags from the freshly scanned DB values — title,
    artist, album, album_artist, year, track/disc number, ISRC,
    composer/writer. MusicBrainz IDs (recording / release / release-group /
    artist / release-track / work) are ALSO filled, but only when the album's
    local tracklist **perfectly matches** the MusicBrainz release.
  • OVERWRITES genre tags automatically with the freshly aggregated genre list.
  • Records a per-track correction (``metadata_conflicts``, provider
    ``"musicbrainz"``) whenever a file tag already holds a value that differs
    from what the scan resolved (excluding genres, which are auto-fixed).
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import structlog

from services.enrichment.musicbrainz_service import get_shared_mb_client

logger = structlog.get_logger(__name__)


def _norm(s: Any) -> str:
    """Normalise for value comparison (case + punctuation insensitive)."""
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _norm_desc(s: Any) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _num(value: Any, default: int) -> int:
    s = str(value or "").strip()
    if not s:
        return default
    try:
        return int(s.split("/")[0].strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# File-tag readers
# ---------------------------------------------------------------------------

def _read_file_values(file_path: str) -> dict[str, str]:
    """Read the current (non-empty) tag values from an MP3/FLAC file."""
    values: dict[str, str] = {}

    def _put(key: str, raw: Any) -> None:
        s = str(raw or "").strip()
        if s:
            values[key] = s

    try:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".mp3":
            from mutagen.id3 import ID3 as _ID3

            tag_obj = _ID3(file_path)

            _FRAME_TEXT = {
                "title": "TIT2", "artist": "TPE1", "album": "TALB",
                "album_artist": "TPE2", "composer": "TCOM",
                "track_number": "TRCK", "disc_number": "TPOS",
                "year": "TDRC", "genres": "TCON", "isrc": "TSRC",
                "lyrics": "USLT",
            }
            for key, frame_id in _FRAME_TEXT.items():
                frames = tag_obj.getall(frame_id)
                for f in frames:
                    text = getattr(f, "text", None)
                    if text:
                        _put(key, ", ".join(str(t) for t in text))
                        break

            _TXXX_DESC = {
                "musicbrainz_trackid": "musicbrainztrackid",
                "musicbrainz_albumid": "musicbrainzalbumid",
                "musicbrainz_artistid": "musicbrainzartistid",
                "musicbrainz_albumartistid": "musicbrainzalbumartistid",
                "musicbrainz_releasegroupid": "musicbrainzreleasegroupid",
                "musicbrainz_releasetrackid": "musicbrainzreleasetrackid",
                "musicbrainz_workid": "musicbrainzworkid",
            }
            for key, desc_norm in _TXXX_DESC.items():
                for f in tag_obj.getall("TXXX"):
                    if _norm_desc(getattr(f, "desc", "")) == desc_norm:
                        text = getattr(f, "text", None)
                        if text:
                            _put(key, str(text[0]))
                        break
        elif suffix == ".flac":
            from mutagen.flac import FLAC as _FLAC

            audio = _FLAC(file_path)
            _VORBIS_KEY = {
                "title": "title", "artist": "artist", "album": "album",
                "album_artist": "albumartist", "composer": "composer",
                "track_number": "tracknumber", "disc_number": "discnumber",
                "year": "date", "genres": "genre", "isrc": "isrc",
                "lyrics": "lyrics",
                "musicbrainz_trackid": "musicbrainz_trackid",
                "musicbrainz_albumid": "musicbrainz_albumid",
                "musicbrainz_artistid": "musicbrainz_artistid",
                "musicbrainz_albumartistid": "musicbrainz_albumartistid",
                "musicbrainz_releasegroupid": "musicbrainzreleasegroupid",
                "musicbrainz_releasetrackid": "musicbrainz_releasetrackid",
                "musicbrainz_workid": "musicbrainz_workid",
            }
            for key, vkey in _VORBIS_KEY.items():
                vals = audio.get(vkey) or audio.get(vkey.upper()) or []
                joined = ", ".join(str(v).strip() for v in vals if str(v).strip())
                _put(key, joined)
    except Exception as exc:
        logger.debug("Could not read tags from file", file_path=file_path, error=str(exc))
    return values


# ---------------------------------------------------------------------------
# Genre Consolidation 
# ---------------------------------------------------------------------------

def _fetch_artist_genres(artist: str) -> dict[str, int]:
    scores: dict[str, int] = {}
    if not artist:
        return scores
        
    try:
        from sqlalchemy import text
        from db.engine import db_session
        with db_session() as session:
            rows = []
            try:
                rows = session.execute(
                    text("SELECT tag, weight FROM artist_tags WHERE LOWER(artist_name) = LOWER(:a) ORDER BY weight DESC LIMIT 15"),
                    {"a": artist}
                ).fetchall()
            except Exception:
                try:
                    rows = session.execute(
                        text("SELECT tag, weight FROM artist_tags WHERE LOWER(artist) = LOWER(:a) ORDER BY weight DESC LIMIT 15"),
                        {"a": artist}
                    ).fetchall()
                except Exception:
                    pass
            
            for row in rows:
                t = str(row[0]).strip().title()
                if t and t.lower() not in {"cover", "live"}:
                    weight_val = 1
                    try:
                        weight_val = int(row[1] or 1)
                    except Exception:
                        pass
                    scores[t] = scores.get(t, 0) + weight_val
    except Exception as exc:
        logger.debug("Failed to fetch artist tags", artist=artist, error=str(exc))

    try:
        from sqlalchemy import text
        from db.engine import db_session
        with db_session() as session:
            row = None
            try:
                row = session.execute(
                    text("SELECT genres, lastfm_tags, musicbrainz_genres FROM artist_metadata WHERE LOWER(artist_name) = LOWER(:a) LIMIT 1"),
                    {"a": artist}
                ).mappings().first()
            except Exception:
                try:
                    row = session.execute(
                        text("SELECT genres, lastfm_tags, musicbrainz_genres FROM artist_metadata WHERE LOWER(artist) = LOWER(:a) LIMIT 1"),
                        {"a": artist}
                    ).mappings().first()
                except Exception:
                    pass
                    
            if row:
                for k in ("genres", "musicbrainz_genres", "lastfm_tags"):
                    val = row.get(k)
                    if not val: continue
                    val_str = str(val).strip()
                    if val_str.startswith("["):
                        try:
                            parsed = json.loads(val_str)
                            for item in parsed:
                                if isinstance(item, dict) and "name" in item:
                                    t = str(item["name"]).strip().title()
                                    if t and t.lower() not in {"cover", "live"}:
                                        scores[t] = scores.get(t, 0) + int(item.get("count", 1))
                        except Exception:
                            pass
                    else:
                        for p in re.split(r"[,;\\]+", val_str):
                            t = p.strip().title()
                            if t and t.lower() not in {"cover", "live"}:
                                scores[t] = scores.get(t, 0) + 2
    except Exception as exc:
        logger.debug("Failed to fetch artist metadata tags", artist=artist, error=str(exc))
        
    return scores


def _consolidate_top_3_genres(track: dict[str, Any], artist_genres: dict[str, int]) -> str:
    genre_scores: dict[str, int] = dict(artist_genres)
    
    def _add_single(g: str, score: int) -> None:
        g = g.strip()
        if not g or len(g) < 2: return
        
        g_lower = g.lower()
        junk = {
            "cover", "live", "seen live", "favorite", "favourites", 
            "good music", "awesome", "loved", "favorite tracks", "tracks"
        }
        if g_lower in junk: 
            return
            
        g_title = g.title()
        genre_scores[g_title] = genre_scores.get(g_title, 0) + score
        
    def _parse_and_add(raw: Any, base_score: int) -> None:
        if not raw: return
        raw_str = str(raw).strip()
        if raw_str.startswith("["):
            try:
                parsed = json.loads(raw_str)
                for item in parsed:
                    if isinstance(item, dict) and "name" in item:
                        _add_single(item["name"], base_score + int(item.get("count", 1)))
                    elif isinstance(item, str):
                        _add_single(item, base_score)
                return
            except Exception:
                pass
        
        parts = re.split(r"[,;\\]+", raw_str)
        for i, p in enumerate(parts):
            _add_single(p, base_score + max(0, 3 - i))
            
    _parse_and_add(track.get("genres"), 5)
    _parse_and_add(track.get("musicbrainz_genres"), 4)
    _parse_and_add(track.get("discogs_genres"), 4)
    _parse_and_add(track.get("discogs_styles"), 3)
    _parse_and_add(track.get("lastfm_tags"), 3)
    _parse_and_add(track.get("listenbrainz_tags"), 2)
    _parse_and_add(track.get("musicbrainz_tags"), 2)
    
    if not genre_scores:
        return ""
        
    sorted_genres = sorted(genre_scores.items(), key=lambda x: x[1], reverse=True)
    top_3 = [g[0] for g in sorted_genres[:3]]
    return ", ".join(top_3)


# ---------------------------------------------------------------------------
# DB → file-tag mapping
# ---------------------------------------------------------------------------

def _resolve_album_year(tracks: list[dict[str, Any]]) -> str:
    """Determine a single unified release year for the entire album group.
    
    Pulls the 4-digit year strings from all tracks, filters out blanks/zeros,
    and returns the most frequent year. If tied, prefers the oldest year.
    """
    years = []
    for t in tracks:
        y = str(t.get("year") or "").strip()
        # Extract 4-digit year if present
        match = re.search(r"(19|20)\d{2}", y)
        if match:
            years.append(match.group(0))
            
    if not years:
        return ""
        
    # Count frequency, then sort by frequency descending, then year ascending (oldest first)
    counts = Counter(years)
    best_year = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return best_year


def _db_tag_candidates(track: dict[str, Any], album_year: str, perfect: bool, include_lyrics: bool, artist_genres: dict[str, int]) -> dict[str, str]:
    """Map a track's fresh DB values to file-tag keys (empty values omitted)."""
    out: dict[str, str] = {}

    def _put(key: str, value: Any) -> None:
        s = str(value or "").strip()
        if s and s.lower() not in ("[]", "null", "none", "unknown", "0"):
            out[key] = s

    _put("title", track.get("title"))
    _put("artist", track.get("artist"))
    _put("album", track.get("album"))
    album_artist = str(track.get("album_artist") or "").strip() or str(track.get("artist") or "").strip()
    _put("album_artist", album_artist)
    
    # Use the unified album-level year across all tracks to prevent player splitting
    _put("year", album_year)
    
    _put("track_number", track.get("track_number"))
    _put("disc_number", track.get("disc_number"))
    _put("isrc", track.get("isrc"))

    writer = track.get("writer")
    if writer:
        try:
            parsed = json.loads(writer) if isinstance(writer, str) else writer
            if isinstance(parsed, list):
                names = [str(w).strip() for w in parsed if str(w).strip()]
                if names:
                    _put("composer", ", ".join(names))
            else:
                _put("composer", writer)
        except Exception:
            _put("composer", writer)

    _put("genres", _consolidate_top_3_genres(track, artist_genres))

    if include_lyrics:
        _put("lyrics", track.get("lyrics"))

    if perfect:
        _put("musicbrainz_trackid", track.get("recording_mbid") or track.get("mbid"))
        _put(
            "musicbrainz_albumid",
            track.get("musicbrainz_albumid") or track.get("musicbrainz_album_mbid"),
        )
        _put("musicbrainz_releasegroupid", track.get("musicbrainz_releasegroupid"))
        _put("musicbrainz_artistid", track.get("musicbrainz_artistid"))
        _put("musicbrainz_releasetrackid", track.get("musicbrainz_releasetrackid"))
        _put("musicbrainz_workid", track.get("musicbrainz_workid"))

    return out


# ---------------------------------------------------------------------------
# MusicBrainz release match
# ---------------------------------------------------------------------------

def _resolve_mb_release(tracks: list[dict[str, Any]]) -> tuple[str, dict[tuple[int, int], dict[str, Any]], int]:
    release_mbid = ""
    for t in tracks:
        release_mbid = str(
            t.get("musicbrainz_albumid") or t.get("musicbrainz_album_mbid") or ""
        ).strip()
        if release_mbid:
            break
    if not release_mbid:
        return "", {}, 0

    try:
        data = get_shared_mb_client().get_release(release_mbid, inc="recordings") or {}
    except Exception as exc:
        logger.debug("MB release fetch failed", release_mbid=release_mbid, error=str(exc))
        return release_mbid, {}, 0

    index: dict[tuple[int, int], dict[str, Any]] = {}
    count = 0
    for medium in data.get("media") or []:
        if not isinstance(medium, dict):
            continue
        try:
            disc = int(medium.get("position") or 1)
        except (TypeError, ValueError):
            disc = 1
        for trk in medium.get("tracks") or []:
            if not isinstance(trk, dict):
                continue
            count += 1
            try:
                pos = int(trk.get("position"))
            except (TypeError, ValueError):
                continue
            rec = trk.get("recording") or {}
            index[(disc, pos)] = {
                "recording_mbid": str(rec.get("id") or "").strip(),
                "title": str(trk.get("title") or "").strip(),
            }
    return release_mbid, index, count


def _is_perfect_match(tracks: list[dict[str, Any]], mb_index: dict[Any, Any], mb_count: int) -> bool:
    if not mb_index or mb_count <= 0 or not tracks:
        return False
    for t in tracks:
        disc = _num(t.get("disc_number"), 1)
        tn = _num(t.get("track_number"), 0)
        if tn <= 0 or (disc, tn) not in mb_index:
            return False
    return len(tracks) == mb_count


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------

def _record_corrections(
    track: dict[str, Any],
    file_values: dict[str, str],
    db_candidates: dict[str, str],
) -> int:
    local: dict[str, str] = {}
    remote: dict[str, str] = {}
    for key, db_val in db_candidates.items():
        file_val = str(file_values.get(key) or "").strip()
        if not file_val:
            continue
        if _norm(file_val) == _norm(db_val):
            continue
        local[key] = file_val
        remote[key] = db_val
    if not remote:
        return 0
    try:
        from services.metadata.conflict_service import detect_and_record_conflicts
        result = detect_and_record_conflicts(
            track_id=str(track.get("id") or ""),
            provider="musicbrainz",
            local_data=local,
            remote_data=remote,
            artist_name=str(track.get("artist") or ""),
            album_name=str(track.get("album") or ""),
            track_title=str(track.get("title") or ""),
        )
        return int(result.get("conflicts_recorded") or 0)
    except Exception as exc:
        logger.debug("Correction record failed", track_id=track.get("id"), error=str(exc))
        return 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def sync_album_file_tags(artist: str, album: str) -> dict[str, Any]:
    """Fill missing file tags + record corrections for one album's tracks."""
    try:
        from helpers.config_helpers import get_tagging_config
        tagging = get_tagging_config()
        if not tagging.get("sync_album_tags_on_scan", True):
            return {"skipped": "feature_disabled", "files_updated": 0, "corrections_recorded": 0}
        include_lyrics = bool(tagging.get("embed_lyrics", False))
    except Exception:
        include_lyrics = False

    tracks = _load_fresh_tracks(artist, album)
    if not tracks:
        return {"skipped": "no_tracks", "files_updated": 0, "corrections_recorded": 0}

    artist_genres = _fetch_artist_genres(artist)
    
    # Calculate a single unified year for the whole album group
    album_year = _resolve_album_year(tracks)
    
    release_mbid, mb_index, mb_count = _resolve_mb_release(tracks)
    perfect = bool(release_mbid) and _is_perfect_match(tracks, mb_index, mb_count)

    files_updated = 0
    corrections_recorded = 0
    for track in tracks:
        file_path = str(track.get("file_path") or "").strip()
        if not file_path or not os.path.exists(file_path):
            continue
            
        file_values = _read_file_values(file_path)
        db_candidates = _db_tag_candidates(track, album_year, perfect, include_lyrics, artist_genres)

        fill: dict[str, str] = {}
        for k, v in db_candidates.items():
            file_val = str(file_values.get(k) or "").strip()
            if k == "genres":
                # Always force overwrite genres if the tag has structurally changed
                if _norm(file_val) != _norm(v):
                    fill[k] = v
            else:
                # Other metadata is strictly fill-if-missing
                if not file_val:
                    fill[k] = v

        if fill:
            try:
                from services.metadata.tag_file_service import write_tags_to_file
                if write_tags_to_file(file_path, fill):
                    files_updated += 1
            except Exception as exc:
                logger.debug("Tag fill failed", track_id=track.get("id"), error=str(exc))

        # We auto-overwrite genres, so explicitly remove it before recording manual corrections
        if "genres" in fill:
            db_candidates.pop("genres", None)

        corrections_recorded += _record_corrections(track, file_values, db_candidates)

    if files_updated or corrections_recorded:
        logger.info(
            "Album file tags synced",
            artist=artist, album=album, files_updated=files_updated, corrections_recorded=corrections_recorded, perfect_match=perfect, album_year=album_year,
        )
    return {
        "artist": artist,
        "album": album,
        "perfect_match": perfect,
        "files_updated": files_updated,
        "corrections_recorded": corrections_recorded,
        "tracks": len(tracks),
    }


def _load_fresh_tracks(artist: str, album: str) -> list[dict[str, Any]]:
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            rows = session.execute(
                _text("""
                    SELECT * FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                    ORDER BY COALESCE(disc_number, '1'), COALESCE(track_number, '999')
                """),
                {"artist": artist, "album": album},
            ).mappings().all() or []
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("Track load failed", artist=artist, album=album, error=str(exc))
        return []
