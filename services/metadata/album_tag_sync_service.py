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
from sqlalchemy import bindparam

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


#: Verified identifier tags. A title/genre differs when the user chose a
#: spelling; an identifier differs when the FILE is stale. Navidrome echoes the
#: file's tag back as the song's MBID, so a stale identifier can never be
#: confirmed by a rescan — the import preserves the DB correction and warns on
#: every pass ("local correction preserved … mbid"). Fill-if-missing therefore
#: cannot self-heal an identifier; a differing one is CORRECTED.
_FILE_IDENTITY_TAGS = frozenset({
    "musicbrainz_trackid",
    "musicbrainz_albumid",
    "musicbrainz_releasegroupid",
    "musicbrainz_artistid",
    "musicbrainz_releasetrackid",
    "musicbrainz_workid",
})


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
                # The original-year pair. Without these the sync could not see
                # whether a file already carries them (fill-if-missing would
                # rewrite them every scan) nor compare them against DATE.
                "originalyear": "originalyear",
                "originaldate": "originaldate",
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
                "musicbrainz_workid": "musicbrainz_workid",                # Vorbis twins of the MP3 original-year pair above.
                "originalyear": "originalyear",
                "originaldate": "originaldate",            }
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


# ---------------------------------------------------------------------------
# DB → file-tag mapping
# ---------------------------------------------------------------------------

def _resolve_album_year(tracks: list[dict[str, Any]]) -> str:
    """The album's ORIGINAL year — the value most tracks agree on.

    Determines a single unified ORIGINAL year for the entire album group, from
    the ``year`` column (which the scan stores as the release GROUP's first
    release year). This is the year that belongs in ORIGINALYEAR/ORIGINALDATE,
    never in the file's DATE tag — see ``_resolve_album_edition_year``.
    """
    years = []
    for t in tracks:
        y = str(t.get("year") or "").strip()
        match = re.search(r"(19|20)\d{2}", y)
        if match:
            years.append(match.group(0))
            
    if not years:
        return ""
        
    counts = Counter(years)
    best_year = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return best_year


def _resolve_album_edition_year(tracks: list[dict[str, Any]]) -> str:
    """The album's EDITION (re-release/remaster) year, or "".

    Read from ``release_year`` — the year of the specific release held in the
    collection, which the scan resolves per album (see
    ``scan_stage_runner._resolve_album_authoritative_year``).

    This is the year the file's DATE/YEAR tag must carry: DATE describes the
    release the FILE is from, so a 2026 remaster of a 1995 album is dated 2026
    and carries 1995 as its original year. Writing the ORIGINAL year into DATE
    (what this service did) made every remaster claim to be the original
    release, which is the reported problem.

    Empty when no track knows an edition year — the caller then falls back to
    the original year so a file is never left without a DATE.
    """
    years = []
    for t in tracks:
        raw = str(t.get("release_year") or "").strip()
        match = re.search(r"(19|20)\d{2}", raw)
        if match:
            years.append(match.group(0))

    if not years:
        return ""

    counts = Counter(years)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _db_tag_candidates(
    track: dict[str, Any],
    album_year: str = "",
    perfect: bool = False,
    include_lyrics: bool = False,
    artist_genres: dict[str, int] | None = None,
    edition_year: str = "",
) -> dict[str, str]:
    """Map a track's fresh DB values to file-tag keys (empty values omitted).

    ``album_year`` is the ORIGINAL year and ``edition_year`` this release's; see
    the two resolvers above for why the DATE tag takes the edition year.
    """
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

    # ---- years ---------------------------------------------------------- 
    # DATE/YEAR = the EDITION's year (falling back to the original when no
    # edition is known, so the tag is never left empty); the album's ORIGINAL
    # year goes to the ORIGINALYEAR/ORIGINALDATE pair, which is what Navidrome
    # and Picard read for "when was this song first released".
    _original_year = str(track.get("originalyear") or "").strip()
    _original_match = re.search(r"(19|20)\d{2}", _original_year or album_year or "")
    _original = _original_match.group(0) if _original_match else ""
    _edition_match = re.search(r"(19|20)\d{2}", edition_year or "")
    _edition = _edition_match.group(0) if _edition_match else ""

    _put("year", _edition or _original or album_year)
    _put("originalyear", _original or album_year)
    # A full original DATE is kept verbatim when the track carries one; a bare
    # year is a valid Vorbis/ID3 original date and is what we fall back to.
    _put("originaldate", track.get("originaldate") or _original or album_year)

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

    # Output strictly relies on the active guardrail-cleared db value
    _put("genres", track.get("genres"))

    if include_lyrics:
        _put("lyrics", track.get("lyrics"))

    if perfect:
        _put("musicbrainz_trackid", track.get("recording_mbid") or track.get("mbid"))
        _put("musicbrainz_albumid", track.get("musicbrainz_albumid") or track.get("musicbrainz_album_mbid"))
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

def is_various_artists_album(
    tracks: list[dict[str, Any]],
    artist: str,
    album: str,
) -> bool:
    """True when this album's tracks must keep their OWN genres.

    A various-artists compilation puts a DIFFERENT performer on each track, so
    it has no single album genre to distribute. The album-level genre sync below
    aggregates every track's genre sources into ONE list and writes that list to
    EVERY track, which on a compilation replaced each performer's own genre with
    a blend of the whole record (reported: every track on a VA album ended up
    carrying the same value).

    Deliberately asks for ``"va"`` and NOT for "any compilation". The classifier
    also reports ``"single_artist"`` for a greatest-hits or other one-artist
    compilation, and such an album legitimately has one artist's genre to share.
    This mirrors the distinction the popularity pipeline already draws between
    ``is_compilation`` and ``is_va_compilation``.

    Only the classifier's album-artist and distinct-track-artist rules can
    produce ``"va"``; its release-type rule runs LAST and can only return
    ``"single_artist"``, so no album-type field is passed in — a type tag
    cannot change this verdict, and reading one would be a dead branch.
    """
    try:
        from services.catalog.album_classification_service import (
            classify_compilation_category,
        )
    except Exception as exc:
        logger.debug("Compilation classifier unavailable", error=str(exc))
        return False

    rows = [t for t in tracks or [] if isinstance(t, dict)]
    album_artist = next(
        (str(t.get("album_artist") or "").strip() for t in rows if t.get("album_artist")),
        "",
    )
    try:
        category = classify_compilation_category(
            artist=artist,
            album=album,
            tracks=rows,
            album_artist=album_artist or artist,
        ) or ""
    except Exception as exc:
        logger.debug(
            "Compilation classification failed",
            artist=artist, album=album, error=str(exc),
        )
        return False
    return category == "va"


#: Genre tokens that describe ONE recording and must survive the album-wide
#: genre write. Mirrors ``genre_aggregation_service._TRACK_AFFILIATION_FILTER_TAGS``
#: (the set the ALBUM BLEND strips), read here so a track can be given back the
#: markers it earned.
_AFFILIATION_MARKERS: tuple[str, ...] = (
    "Cover", "Live", "Acoustic", "Unplugged", "Remix", "Remixed",
    "Instrumental", "Comedy", "Orchestral", "Symphonic", "Soundtrack",
)

#: The per-track FLAGS that ARE an affiliation, and the marker each one means.
#: The flag is authoritative — the cover pass sets ``is_cover = 1`` and writes
#: "Cover" into ``musicbrainz_genres``, but NOT into the display ``genres``
#: column the album-wide write below replaces.
_AFFILIATION_FLAGS: tuple[tuple[str, str], ...] = (
    ("is_cover", "Cover"),
    ("is_live", "Live"),
    ("is_acoustic", "Acoustic"),
    ("is_remix", "Remix"),
)


def _track_affiliation_markers(track: dict[str, Any]) -> list[str]:
    """The per-TRACK markers this recording earned, from its own evidence.

    ⭐ REQUIRED because the album-wide genre write below replaces EVERY track's
    ``genres`` with the album blend — and the blend deliberately has the
    affiliation tags stripped (they are per-track facts, not album properties).
    So a track the cover pass had just marked "Cover" came out of the same scan
    with that marker gone: ``is_cover = 1`` survived but the genre did not, which
    is the reported "the Cover genre isn't being applied".

    Read from the track's own FLAGS first (authoritative — they are what the
    detection actually decided), then from its own genre sources, so a
    hand-tagged "Live" in ``lastfm_tags`` is honoured too.
    """
    markers: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        key = name.casefold()
        if key and key not in seen:
            seen.add(key)
            markers.append(name)

    for column, marker in _AFFILIATION_FLAGS:
        value = track.get(column)
        if value in (1, True, "1", "true", "True"):
            _add(marker)

    # The track's OWN source columns. ``genres`` is deliberately included: it is
    # what the track stage wrote for this recording before this pass ran.
    for column in (
        "genres", "musicbrainz_genres", "manual_genres", "navidrome_genres",
        "lastfm_tags", "lastfm_genres", "listenbrainz_genres", "discogs_genres",
        "spotify_genres", "essentia_genres", "audiodb_genres", "wikidata_genres",
    ):
        raw = track.get(column)
        if not raw:
            continue
        values: list[str] = raw if isinstance(raw, list) else []
        if not values:
            text_value = str(raw).strip()
            if text_value.startswith("["):
                try:
                    parsed = json.loads(text_value)
                    if isinstance(parsed, list):
                        values = [str(v) for v in parsed]
                except Exception:
                    values = []
            if not values:
                values = [g for g in re.split(r"[,;/\\|]+", text_value) if g]
        for value in values:
            name = str(value).strip()
            if name and name.casefold() in {m.casefold() for m in _AFFILIATION_MARKERS}:
                _add(name)

    return markers


def _genre_tokens(value: Any) -> list[str]:
    """Split any stored genre form into display names, order preserved."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text_value = str(value).strip()
    if not text_value or text_value.lower() in ("[]", "{}", "null", "none"):
        return []
    if text_value.startswith("["):
        try:
            parsed = json.loads(text_value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass
    return [g.strip() for g in re.split(r"[,;/\\|]+", text_value) if g.strip()]


def _merge_album_blend_with_affiliations(
    blend: str, markers: list[str]
) -> str:
    """The album blend plus the affiliation markers THIS track earned.

    Dedupes case-insensitively and appends, so the album's musical genres stay
    in the leading positions (the playlist builder reads only the first few).
    """
    merged = _genre_tokens(blend)
    seen = {g.casefold() for g in merged}
    for marker in markers or []:
        if marker.casefold() not in seen:
            seen.add(marker.casefold())
            merged.append(marker)
    return ", ".join(merged)


def sync_album_file_tags(
    artist: str,
    album: str,
    corrected_title_ids: set[str] | None = None,
    changed_track_ids: set[str] | None = None,
    previous_genres: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill missing file tags + record corrections for one album's tracks.

    ``corrected_title_ids`` — the set of track ids whose ``title`` the scan
    JUST corrected (a MusicBrainz release-tracklist title promoted to the DB
    column). For exactly those tracks the TITLE tag is rewritten even when
    the file already holds a value; everything else stays strictly
    fill-if-missing, so a deliberately-chosen local title is never clobbered.

    ``changed_track_ids`` — the set of track ids the scan WROTE to this pass.
    When the running scan changed nothing for a track, that track's tag
    reconciliation was already proved clean on the pass that wrote it (or
    earlier), so re-reading its audio file with mutagen on every scan is pure
    waste — the reported ~90-120s per already-synced album was exactly this
    repeat. Tracks in the set (or never synced) are re-read and stamped
    ``file_tags_synced_at``; the rest are skipped.

    ⚠️ The skip is applied AFTER the album genre blend, not before it. The
    blend is written to EVERY track of the album in the database, so a track
    whose own metadata did not change can still have NEW genres — filtering
    first left those genres in the DB but never in the file (which is what
    Navidrome reads). A track therefore stays in the file pass when it is in
    ``changed_track_ids``, was never synced, OR its genres differ from what
    it held before this scan.

    ``previous_genres`` — ``{track_id: genres}`` as the track held them
    BEFORE this scan (the runner still has the load-time rows). This is the
    baseline for "did the genres change". Without it the baseline is the
    freshly loaded DB value, which the track stage has already overwritten
    with its own per-track list — so nearly every track would read as changed
    and the skip would save nothing.

    The album year and the MusicBrainz perfect-match check are also resolved
    over the WHOLE album before the skip: computed over a filtered subset,
    ``_is_perfect_match`` (which requires ``len(tracks) == mb_count``) always
    failed and the year majority vote ran on a partial album.
    """
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

    # Genres each track held BEFORE this pass's album blend, keyed by id.
    # Used after the blend to tell which tracks' genres really changed.
    _genres_before: dict[str, str] = {}
    for _t in tracks:
        _tid = str(_t.get("id") or "")
        if not _tid:
            continue
        if previous_genres is not None and _tid in {str(k) for k in previous_genres}:
            _prev = {str(k): v for k, v in previous_genres.items()}.get(_tid)
        else:
            _prev = _t.get("genres")
        _genres_before[_tid] = ", ".join(_genre_tokens(_prev))

    # -------------------------------------------------------------------------
    # ACTIVE GENRE CLEANUP
    # Generate perfect genres from the strict guardrail output. Force update
    # the DB so that finalise_stage.py generates clean Navidrome playlists,
    # and bind them to the track instances so the physical files are overwritten.
    #
    # A VARIOUS-ARTISTS compilation is excluded: its tracks are different
    # performers, so there is no album genre to hand out, and distributing the
    # aggregated blend destroyed each track's own genre. The per-track genres
    # computed by the scan's track stage are left exactly as they are.
    # -------------------------------------------------------------------------
    try:
        from services.enrichment.genre_aggregation_service import (
            get_track_recommendations,
            sync_various_artists_track_genres,
        )
        from db.engine import db_session
        from sqlalchemy import text

        if is_various_artists_album(tracks, artist, album):
            # The ALBUM-level write stays skipped — it hands one blended list
            # to every performer — but nothing replaced it, so a VA track's
            # genres were frozen at whatever Navidrome imported. Each track now
            # gets ITS OWN aggregated genres, with Navidrome excluded.
            updated = sync_various_artists_track_genres(tracks, album=album)
            # The helper writes the DB; re-read so the rows here (and the
            # genre-change check below) carry what it actually stored.
            _va_ids = [str(t.get("id")) for t in tracks if t.get("id") is not None]
            if _va_ids:
                with db_session() as session:
                    _rows = session.execute(
                        text(
                            "SELECT CAST(id AS TEXT) AS id, genres FROM tracks "
                            "WHERE CAST(id AS TEXT) IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": _va_ids},
                    ).mappings().all()
                _fresh_genres = {str(r["id"]): r["genres"] for r in _rows}
                for t in tracks:
                    _tid = str(t.get("id") or "")
                    if _tid in _fresh_genres:
                        t["genres"] = _fresh_genres[_tid]
            logger.debug(
                "Per-track genre sync on a various-artists album",
                artist=artist, album=album, tracks=len(tracks), updated=updated,
            )
        else:
            rec_data = get_track_recommendations(artist, album)
            if rec_data and rec_data.get("genres"):
                clean_genres = ", ".join(rec_data["genres"])

                # ⭐⭐ A track keeps the affiliation markers IT earned.
                #
                # The blend above is ALBUM-scoped and deliberately has the
                # per-track affiliation tags stripped from it (Cover/Live/
                # Acoustic/… describe one recording's performance). Writing it
                # to every track therefore DELETED the marker the scan had just
                # applied — a cover track came out of the same scan with
                # ``is_cover = 1`` but no "Cover" genre, which is the reported
                # "the Cover genre isn't being applied".
                #
                # One album-wide UPDATE cannot express that, so when any track
                # earned a marker the write is split: the un-marked tracks take
                # the album blend in one statement (the common case stays a
                # single query), and each marked track gets the blend plus its
                # own markers.
                _marked: list[tuple[dict[str, Any], list[str]]] = []
                for t in tracks:
                    _markers = _track_affiliation_markers(t)
                    if _markers:
                        _marked.append((t, _markers))

                _marked_ids = {str(t.get("id")) for t, _m in _marked}

                # ⚠️ Written BY THE IDS ALREADY LOADED, never by artist/album.
                # ``_load_fresh_tracks`` matches case-insensitively and across
                # every "X feat. Y" album-artist variant; the old exact
                # ``album_artist = :a AND album = :alb`` UPDATE could match
                # NOTHING for exactly those albums, while the file pass below
                # still wrote the new genres — file and DB then disagreed.
                _unmarked_ids = sorted(
                    str(t.get("id")) for t in tracks
                    if t.get("id") is not None and str(t.get("id")) not in _marked_ids
                )

                with db_session() as session:
                    if _unmarked_ids:
                        session.execute(
                            text(
                                "UPDATE tracks SET genres = :g "
                                "WHERE CAST(id AS TEXT) IN :ids"
                            ).bindparams(bindparam("ids", expanding=True)),
                            {"g": clean_genres, "ids": _unmarked_ids},
                        )
                    for _track, _markers in _marked:
                        _tid = str(_track.get("id") or "")
                        if not _tid:
                            continue
                        session.execute(
                            text("UPDATE tracks SET genres = :g WHERE CAST(id AS TEXT) = :id"),
                            {
                                "g": _merge_album_blend_with_affiliations(
                                    clean_genres, _markers
                                ),
                                "id": _tid,
                            },
                        )

                for t in tracks:
                    _markers = _track_affiliation_markers(t)
                    t["genres"] = (
                        _merge_album_blend_with_affiliations(clean_genres, _markers)
                        if _markers else clean_genres
                    )
    except Exception as exc:
        logger.warning("Active genre database cleanup failed", error=str(exc))
    # -------------------------------------------------------------------------

    artist_genres = _fetch_artist_genres(artist)
    # ``year`` is the album's ORIGINAL year and ``release_year`` this EDITION's;
    # see the two resolvers for why the DATE tag takes the edition.
    album_year = _resolve_album_year(tracks)
    album_edition_year = _resolve_album_edition_year(tracks)
    
    release_mbid, mb_index, mb_count = _resolve_mb_release(tracks)
    perfect = bool(release_mbid) and _is_perfect_match(tracks, mb_index, mb_count)

    files_updated = 0
    corrections_recorded = 0
    _date_needs_correction = bool(
        album_edition_year
        and album_year
        and _norm(album_edition_year) != _norm(album_year)
    )
    # The scan reports corrected track ids under their DB ``id``; the freshly
    # loaded rows carry the same ids, but normalise anyway (ids are cast to
    # strings somewhere in the pipeline).
    _corrected_ids = {str(_i) for _i in (corrected_title_ids or set())}

    # ── Skip tracks with nothing new for their file ──────────────────────
    # Applied HERE — after the genre blend, the album year and the
    # perfect-match check have all seen the WHOLE album (see the docstring).
    # A track is kept when the scan wrote it, its title was corrected, it was
    # never synced, its genres differ from what it held before this pass, or a
    # local correction is still waiting for Navidrome to confirm it.
    _file_tracks = tracks
    _genre_changed = 0
    if changed_track_ids is not None:
        _changed = {str(i) for i in changed_track_ids} | _corrected_ids
        _file_tracks = []
        for t in tracks:
            _tid = str(t.get("id") or "")
            _now = ", ".join(_genre_tokens(t.get("genres")))
            _genres_moved = _norm(_now) != _norm(_genres_before.get(_tid, ""))
            if _genres_moved and _tid not in _changed:
                _genre_changed += 1
            if (
                _tid in _changed
                or not t.get("file_tags_synced_at")
                or _genres_moved
                # A dirty row is an UNCONFIRMED correction. Re-reading its
                # file is what lets a stale identifier be corrected and its
                # Navidrome conflict finally clear — otherwise the row never
                # enters this pass again and the import warns on every scan.
                or bool(t.get("metadata_dirty_for_navidrome"))
            ):
                _file_tracks.append(t)
        if not _file_tracks:
            return {
                "artist": artist,
                "album": album,
                "skipped": "no_track_changes",
                "perfect_match": perfect,
                "files_updated": 0,
                "corrections_recorded": 0,
                "tracks": 0,
            }

    try:
        from services.metadata.tag_file_service import (
            resolve_music_file_path,
            write_tags_to_file,
        )
    except Exception as exc:
        logger.warning("Tag writer unavailable — file tag sync skipped", error=str(exc))
        return {
            "artist": artist, "album": album, "skipped": "writer_unavailable",
            "files_updated": 0, "corrections_recorded": 0, "tracks": 0,
        }

    _synced_ids: list[str] = []
    for track in _file_tracks:
        stored_path = str(track.get("file_path") or "").strip()
        if not stored_path:
            continue
        # ⚠️ RESOLVE FIRST, then test existence. The stored path may be
        # RELATIVE to the music root (``rename-file`` stores it that way);
        # ``os.path.exists`` on the raw value was False for every such track,
        # so they were silently skipped — never tagged, never stamped, and
        # re-visited (and re-skipped) on every scan.
        try:
            file_path = resolve_music_file_path(stored_path) or stored_path
        except Exception:
            file_path = stored_path
        if not file_path or not os.path.exists(file_path):
            continue
        _synced_ids.append(str(track.get("id") or ""))

        file_values = _read_file_values(file_path)
        db_candidates = _db_tag_candidates(
            track, album_year, perfect, include_lyrics, artist_genres,
            edition_year=album_edition_year,
        )

        # A dirty row is a local correction the import has not been able to
        # confirm. Its recording identity is what Navidrome echoes as the
        # song's MBID, so it MUST reach the file even when the album is not a
        # perfect tracklist match — the perfect gate protects a MASS sync, not
        # a row the app already corrected. Without this, the conflict is
        # unresolvable: the import preserves the DB value, Navidrome keeps the
        # stale tag, and every scan warns again.
        if track.get("metadata_dirty_for_navidrome"):
            _rec_mbid = str(
                track.get("recording_mbid") or track.get("mbid") or ""
            ).strip()
            if _rec_mbid:
                db_candidates["musicbrainz_trackid"] = _rec_mbid

        # A track whose title the scan just corrected must have the TITLE tag
        # rewritten even if the file already holds a value (a deliberately
        # chosen local spelling is never clobbered — only the ids in this set
        # are touched, and they are exactly the ones the scan promoted).
        _title_was_corrected = str(track.get("id") or "") in _corrected_ids

        fill: dict[str, str] = {}
        for k, v in db_candidates.items():
            file_val = str(file_values.get(k) or "").strip()
            if k == "genres":
                # Always force overwrite genres if the tag has structurally changed
                if _norm(file_val) != _norm(v):
                    fill[k] = v
            elif k == "title" and _title_was_corrected:
                # The scan corrected this title; the file must follow.
                if _norm(file_val) != _norm(v):
                    fill[k] = v
            elif k in _FILE_IDENTITY_TAGS:
                # Verified IDs are corrected, never merely filled: a stale
                # tag is what keeps the Navidrome import warning forever.
                if _norm(file_val) != _norm(v):
                    fill[k] = v
            elif k == "year" and _date_needs_correction and _norm(file_val) == _norm(album_year):
                # The file's DATE holds the album's ORIGINAL year while the DB
                # knows a different EDITION year: that is the inverted pairing
                # the previous writer produced, not a user edit. Correct it —
                # fill-if-missing could never fix it, so a remaster would keep
                # claiming to BE the original release forever.
                fill[k] = v
            else:
                # Other metadata is strictly fill-if-missing
                if not file_val:
                    fill[k] = v

        if fill:
            try:
                # ``file_path`` is already resolved and known to exist.
                if write_tags_to_file(file_path, fill):
                    files_updated += 1
                # ⚠️ WARNING, not DEBUG: a tag fill that silently did nothing is
                # exactly the "I saved it and nothing changed" report, and DEBUG
                # is invisible in a normal log tail.
                else:
                    logger.warning(
                        "Tag fill did not write the file",
                        track_id=track.get("id"), file_path=file_path,
                    )
            except Exception as exc:
                logger.warning(
                    "Tag fill failed", track_id=track.get("id"), error=str(exc),
                )

        # We auto-overwrite genres, so explicitly remove it before recording manual corrections
        if "genres" in fill:
            db_candidates.pop("genres", None)

        # Identifiers we just corrected in the FILE are no longer a conflict:
        # recording them would re-open the very discrepancy this pass healed.
        for _key in list(fill):
            if _key in _FILE_IDENTITY_TAGS:
                db_candidates.pop(_key, None)

        corrections_recorded += _record_corrections(track, file_values, db_candidates)

    # ── Stamp the tracks this pass actually reconciled ───────────────────
    # The skip gate above reads ``file_tags_synced_at``: a track stamped here
    # was verified against the DB on THIS pass, so the NEXT scan (which re-runs
    # the skip gate) can drop the track without re-reading its audio file.
    # Only tracks whose FILE was read are stamped — a track whose file did not
    # exist is NOT marked synced, so it is still visited once the file appears.
    if _synced_ids:
        _stamp_tag_sync(artist, album, _synced_ids)

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
        # Tracks whose FILE was examined this pass (after the skip).
        "tracks": len(_file_tracks),
        # Kept only because the album genre blend changed their genres.
        "genre_only_tracks": _genre_changed,
    }


def _stamp_tag_sync(artist: str, album: str, track_ids: list[str]) -> None:
    """Mark exactly ``track_ids`` as file-tag-reconciled as of now.

    Executed as ONE UPDATE (the skip-gate reads ``file_tags_synced_at`` per
    track). Tracks whose files did not exist are deliberately EXCLUDED by the
    caller: a track with no file must not read as "already synced", or the
    missing file would never be filled when it appears. Failures are logged,
    never fatal: a missing stamp only costs a future re-read.
    """
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session

    ids = [str(i) for i in track_ids if str(i or "").strip()]
    if not ids:
        return
    placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
    params: dict[str, Any] = {f"id{i}": tid for i, tid in enumerate(ids)}
    try:
        with _db_session() as session:
            session.execute(
                _text(f"UPDATE tracks SET file_tags_synced_at = CURRENT_TIMESTAMP WHERE CAST(id AS TEXT) IN ({placeholders})"),
                params,
            )
    except Exception as exc:
        # WARNING: a stamp that never lands makes every scan re-read every
        # file of the album — the exact cost the skip exists to remove.
        logger.warning("File-tag sync stamp failed", album=album, error=str(exc))


def _load_fresh_tracks(artist: str, album: str) -> list[dict[str, Any]]:
    from helpers.normalization_service import album_artist_key_variants

    # The scan relocates a featured credit from a track's TITLE onto its ARTIST
    # field, and the album key used everywhere is
    # COALESCE(NULLIF(album_artist, ''), artist) — so one album can be keyed
    # under both "X" and "X feat. Y" during a single scan. Matching every
    # spelling is what stops this lookup from silently returning nothing for
    # exactly the albums the relocation touched.
    raw_keys = album_artist_key_variants(artist)
    if not raw_keys:
        return []
    placeholders = ", ".join(f":k{i}" for i in range(len(raw_keys)))
    params: dict[str, Any] = {f"k{i}": key.lower() for i, key in enumerate(raw_keys)}
    params["album"] = album
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            rows = session.execute(
                _text(f"""
                    SELECT * FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) IN ({placeholders})
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                    ORDER BY COALESCE(disc_number, '1'), COALESCE(track_number, '999')
                """),
                params,
            ).mappings().all() or []
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("Track load failed", artist=artist, album=album, error=str(exc))
        return []
