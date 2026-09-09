"""Navidrome track metadata extraction.

Extracts and normalises metadata from raw Navidrome/OpenSubsonic track
payloads. Intentionally separate from API, DB, and import concerns.

Key Functions:
    - extract_track_metadata(): Normalise a Navidrome track dict into
      structured metadata ready for DB persistence.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)

WRITER_ROLES = {
    "composer",
    "lyricist",
    "writer",
    "author",
    "textwriter",
    "lyricswriter",
    "lyrics_writer",
}


def _safe_int(value: Any) -> int | None:
    """Return value as int, or None if it cannot be converted."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_isrc(value: Any) -> str:
    """Normalize a raw ISRC / tag-list value to a bare 12-char code."""
    try:
        from helpers.normalization_service import normalize_isrc
        return normalize_isrc(value)
    except Exception:
        return str(value or "").strip()


def _safe_float(value: Any) -> float | None:
    """Return value as float, or None if it cannot be converted."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_people(value: Any) -> list[str]:
    """Normalise a person/credit field into a list of display names."""
    if not value:
        return []

    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                candidate = str(item.get("name", "")).strip()
            else:
                candidate = str(item).strip()
            if candidate:
                names.append(candidate)
        return names

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if "\\" in raw or ";" in raw or "," in raw:
            normalised = raw.replace("\\", ",").replace(";", ",")
            return [part.strip() for part in normalised.split(",") if part.strip()]
        return [raw]

    return []


def _extract_genres(track: dict[str, Any]) -> tuple[str, str]:
    """Return Navidrome genre string and primary genre."""
    genres: list[str] = []

    if isinstance(track.get("genres"), list):
        for entry in track.get("genres") or []:
            if isinstance(entry, dict):
                name = str(entry.get("name", "")).strip()
            else:
                name = str(entry).strip()
            if name:
                genres.append(name)
    elif track.get("genre"):
        raw = str(track.get("genre") or "")
        raw = raw.replace("•", "\\").replace(";", "\\").replace(",", "\\")
        genres = [part.strip() for part in raw.split("\\") if part.strip()]

    return "\\".join(genres), genres[0] if genres else ""


def _people_array_to_string(track: dict[str, Any], field_name: str, get_tag_value: Callable[..., Any]) -> str:
    """Convert OpenSubsonic people arrays into backslash-separated strings."""
    raw = track.get(field_name)
    if isinstance(raw, list):
        names = []
        for item in raw:
            if isinstance(item, dict):
                candidate = str(item.get("name", "")).strip()
            else:
                candidate = str(item).strip()
            if candidate:
                names.append(candidate)
        return "\\".join(names)
    return str(get_tag_value(field_name) or "")


def _extract_writers(track: dict[str, Any], get_song: Callable[[str], dict[str, Any]] | None = None) -> list[str]:
    """Extract writer/composer/lyricist names from direct fields, tags and contributors."""
    writers: list[str] = []

    def add(value: Any) -> None:
        for name in _normalize_people(value):
            if name and name not in writers:
                writers.append(name)

    for field in (
        "writer", "writers", "lyricist", "lyricists", "author", "authors", "composer", "composers"
    ):
        add(track.get(field))

    tags = track.get("tags") if isinstance(track.get("tags"), dict) else {}
    if isinstance(tags, dict):
        for field in (
            "lyricist", "writer", "textwriter", "lyricswriter", "lyrics_writer",
            "musicbrainz_lyricist", "tmcl:lyricist",
        ):
            add(tags.get(field))

    contributors = track.get("contributors")
    if isinstance(contributors, list):
        for contributor in contributors:
            if not isinstance(contributor, dict):
                continue
            role = str(contributor.get("role", "")).lower().strip()
            if role not in WRITER_ROLES:
                continue
            add(contributor.get("name"))
            artist_info = contributor.get("artist")
            if isinstance(artist_info, dict):
                add(artist_info.get("name"))
            elif artist_info:
                add(artist_info)

    if not writers and get_song and track.get("id"):
        try:
            extended = get_song(str(track.get("id"))) or {}
            if extended and extended is not track:
                writers.extend(_extract_writers(extended, get_song=None))
        except Exception as exc:
            logger.debug("getSong fallback failed", track_id=track.get("id"), error=str(exc))

    return writers


def extract_track_metadata(
    track: dict[str, Any],
    *,
    get_song: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract DB-ready metadata from a raw Navidrome track dictionary."""
    raw_track = track.get("trackNumber") if "trackNumber" in track else track.get("track")
    raw_disc = track.get("discNumber") if "discNumber" in track else track.get("disc")
    tags = track.get("tags") if isinstance(track.get("tags"), dict) else {}

    def get_tag_value(*keys: str) -> Any:
        for key in keys:
            value = track.get(key)
            if value not in (None, ""):
                return value
            if isinstance(tags, dict):
                value = tags.get(key)
                if value not in (None, ""):
                    return value
        return ""

    navidrome_genres, navidrome_genre = _extract_genres(track)
    writers = _extract_writers(track, get_song=get_song)
    writer_json = json.dumps(writers) if writers else json.dumps([])
    album_mbid = get_tag_value("musicbrainz_albumid", "musicbrainz_album_mbid", "musicbrainz_releaseid", "release_mbid") or ""

    return {
        "duration": track.get("duration"),
        "track_number": _safe_int(raw_track),
        "disc_number": _safe_int(raw_disc),
        "year": track.get("year"),
        "artist": track.get("artist", ""),
        "album_artist": track.get("albumArtist", ""),
        "bitrate": track.get("bitRate"),
        "sample_rate": track.get("samplingRate"),
        "navidrome_genres": navidrome_genres,
        "navidrome_genre": navidrome_genre,
        "writer": writer_json,
        "stars": int(track.get("userRating", 0) or 0),
        "file_path": track.get("path", ""),
        "mbid": track.get("mbid", "") or "",
        "musicbrainz_albumid": album_mbid,
        "musicbrainz_album_mbid": album_mbid,
        "musicbrainz_trackid": get_tag_value("musicbrainz_recordingid", "musicbrainz_trackid", "musicbrainz_track_id") or "",
        "musicbrainz_releasegroupid": get_tag_value("musicbrainz_releasegroupid", "musicbrainz_releasegroup_id", "release_group_mbid") or "",
        "musicbrainz_releasetrackid": get_tag_value("musicbrainz_trackid", "musicbrainz_releasetrackid", "musicbrainz_release_track_id", "release_track_mbid") or "",
        "musicbrainz_albumstatus": get_tag_value("releasestatus", "musicbrainz_albumstatus", "musicbrainz_release_status", "release_status") or "",
        "musicbrainz_albumtype": get_tag_value("releasetype", "musicbrainz_albumtype", "musicbrainz_release_type", "release_type") or "",
        "musicbrainz_releasecountry": get_tag_value("releasecountry", "musicbrainz_releasecountry", "musicbrainz_albumcountry", "release_country") or "",
        "musicbrainz_artistid": get_tag_value("musicbrainz_artistid", "musicbrainz_artist_id") or "",
        "musicbrainz_artist_id": get_tag_value("musicbrainz_artist_id", "musicbrainz_artistid") or "",
        "musicbrainz_albumartistid": get_tag_value("musicbrainz_albumartistid", "musicbrainz_albumartist_id") or "",
        "musicbrainz_workid": get_tag_value("musicbrainz_workid", "musicbrainz_work_id") or "",
        # ── MusicBrainz enrichment read back from file tags ───────────────
        # The download-completion import writes these to the file tags
        # (``IS COVER`` / ``ORIGINAL COVER ARTIST`` / ``MUSICBRAINZ GENRES``
        # TXXX/Vorbis frames); a later Navidrome import must read them back
        # into the tracks table so the enrichment is not lost.
        "is_cover": get_tag_value("is_cover", "IS_COVER") or "",
        "original_cover_artist": get_tag_value("original_cover_artist", "ORIGINAL_COVER_ARTIST") or "",
        "musicbrainz_genres": get_tag_value("musicbrainz_genres", "MUSICBRAINZ_GENRES", "MUSICBRAINZ GENRES") or "",
        "releasetype": get_tag_value("releasetype", "release_type", "albumtype") or "",
        "releasestatus": get_tag_value("releasestatus", "release_status", "musicbrainz_albumstatus") or "",
        "releasecountry": get_tag_value("releasecountry", "release_country", "musicbrainz_releasecountry") or "",
        "media": get_tag_value("media", "mediatype", "discmedia") or "",
        "label": get_tag_value("label", "publisher", "organization") or "",
        "recordlabel": get_tag_value("recordlabel", "record_label", "label") or "",
        "tracktotal": get_tag_value("tracktotal", "totaltracks", "tracktotals", "trackcount") or None,
        "disctotal": get_tag_value("disctotal", "totaldiscs", "disccount", "discs") or None,
        "compilation": get_tag_value("compilation", "itunescompilation", "tcmp", "part_of_a_compilation") or "",
        "grouping": get_tag_value("grouping", "contentgroup", "tit1") or "",
        "albumversion": get_tag_value("albumversion", "version") or "",
        "discsubtitle": get_tag_value("discsubtitle", "setsubtitle", "disc_subtitle") or "",
        "script": get_tag_value("script") or "",
        "replaygain_track_gain": get_tag_value("replaygain_track_gain") or "",
        "replaygain_track_peak": get_tag_value("replaygain_track_peak") or "",
        "replaygain_album_gain": get_tag_value("replaygain_album_gain") or "",
        "replaygain_album_peak": get_tag_value("replaygain_album_peak") or "",
        "r128_track_gain": get_tag_value("r128_track_gain") or "",
        "r128_album_gain": get_tag_value("r128_album_gain") or "",
        "releasedate": get_tag_value("releasedate", "originalreleasedate", "release_date") or "",
        "originalyear": get_tag_value("originalyear", "original_year", "originalreleaseyear") or None,
        "originaldate": get_tag_value("originaldate", "original_date", "originalreleasedate") or None,
        "copyright": get_tag_value("copyright") or "",
        "barcode": get_tag_value("barcode", "ean", "upc") or "",
        "catalognumber": get_tag_value("catalognumber", "catalog", "catalognum", "catalog_number") or "",
        "asin": get_tag_value("asin") or "",
        "subtitle": get_tag_value("subtitle") or "",
        "lyrics": get_tag_value("lyrics", "unsyncedlyrics") or "",
        "language": get_tag_value("language", "lang") or "",
        "work": get_tag_value("work", "contentgroup") or "",
        "movement": get_tag_value("movement", "movementnumber", "mvin") or "",
        "movementname": get_tag_value("movementname", "mvnm") or "",
        "movementtotal": get_tag_value("movementtotal", "mvcn") or "",
        "key": get_tag_value("key", "initialkey") or "",
        "explicitstatus": get_tag_value("explicitstatus", "explicit", "itunesadvisory") or "",
        "composer": get_tag_value("composer", "composers") or "",
        "lyricist": get_tag_value("lyricist", "lyricists", "textwriter") or "",
        "conductor": get_tag_value("conductor") or "",
        "remixer": get_tag_value("remixer", "mixartist", "tpe4") or "",
        "producer": get_tag_value("producer") or "",
        "arranger": get_tag_value("arranger") or "",
        "mixer": get_tag_value("mixer") or "",
        "engineer": get_tag_value("engineer") or "",
        "director": get_tag_value("director") or "",
        "djmixer": get_tag_value("djmixer", "dj_mixer") or "",
        "performer": get_tag_value("performer") or "",
        "titlesort": get_tag_value("titlesort", "tsot") or "",
        "albumsort": get_tag_value("albumsort", "tsoa") or "",
        "artistsort": get_tag_value("artistsort", "tsop") or "",
        "albumartistsort": get_tag_value("albumartistsort", "tsopalbumartist", "albumartist_sort") or "",
        "albumartistssort": get_tag_value("albumartistssort") or "",
        "artistssort": get_tag_value("artistssort") or "",
        "composersort": get_tag_value("composersort") or "",
        "lyricistsort": get_tag_value("lyricistsort") or "",
        "artists": _people_array_to_string(track, "artists", get_tag_value),
        "albumartists": _people_array_to_string(track, "albumArtists", get_tag_value),
        "encodedby": get_tag_value("encodedby", "encoded_by") or "",
        "encodersettings": get_tag_value("encodersettings", "encoder", "encodingsettings") or "",
        "website": get_tag_value("website", "url", "weblink") or "",
        "license": get_tag_value("license") or "",
        "isrc": _normalize_isrc(get_tag_value("isrc", "musicbrainz_isrc") or ""),
        "bpm": _safe_int(get_tag_value("bpm", "tempo")),
        "danceability": _safe_float(get_tag_value("danceability")),
        "comment": get_tag_value("comment", "comments", "description") or "",
        "navidrome_added_at": track.get("created"),
    }
