"""Metadata authority, import fingerprints and Navidrome ID reconciliation.

This module is the single place that answers three related questions:

1. **Is a local correction newer than Navidrome's indexed view?**
   ``local_tags_written_at`` + ``metadata_dirty_for_navidrome`` +
   ``navidrome_imported_at`` are stamped when Popularr writes file tags and
   only cleared after a VERIFIED import observed the expected values.

2. **Is this album unchanged?**
   A metadata fingerprint over the fields the Navidrome import owns. Equal
   song IDs alone are not proof: titles, artists, album, year, positions,
   paths, duration, recording MBID and genres can all change while the IDs
   stay identical.

3. **Did a Navidrome song ID change for the same logical track?**
   Adoption is conservative and identity-first: exact normalised file path,
   then exact recording MBID plus compatible title/artist. Title alone and
   fuzzy matches are never used.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Iterable

import structlog

from db.engine import db_session
from helpers.normalization_service import clean_artist_name_for_storage, strip_diacritics

logger = structlog.get_logger(__name__)

#: Fields the Navidrome import owns and that may need local authority.
#: Popularity/scoring columns are handled separately and always protected.
LOCAL_AUTHORITY_FIELDS: frozenset[str] = frozenset({
    "title", "artist", "album", "album_artist", "year", "release_year",
    "track_number", "disc_number", "isrc",
    "mbid", "recording_mbid", "musicbrainz_trackid",
    "musicbrainz_albumid", "musicbrainz_album_mbid",
    "musicbrainz_artistid", "musicbrainz_albumartistid",
    "musicbrainz_releasegroupid", "musicbrainz_releasetrackid",
    "musicbrainz_workid",
    "releasestatus", "releasecountry", "recordlabel", "media",
    "catalognumber", "barcode", "asin", "copyright", "language",
    "originalyear", "originaldate", "releasedate", "tracktotal",
    "disctotal", "discsubtitle", "albumversion", "script", "work",
    "composer", "lyricist",
})

#: Stable fields used for the album freshness fingerprint.
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "id", "file_path", "title", "artist", "album_artist", "album",
    "year", "track_number", "disc_number", "duration",
    "musicbrainz_trackid", "navidrome_genres",
)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _number_text(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    if number.is_integer():
        return str(int(number))
    return format(number, ".3f").rstrip("0").rstrip(".")


def _duration_text(value: Any) -> str:
    try:
        return format(round(float(value or 0), 3), ".3f").rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _text(value)


def _year_text(value: Any) -> str:
    text = _text(value)
    if len(text) >= 4 and text[:4].isdigit():
        return text[:4]
    return text


def _genre_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        raw = [
            str(item.get("name", "")) if isinstance(item, dict) else str(item)
            for item in value
        ]
    else:
        text = str(value).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                raw = [str(v) for v in parsed]
            elif isinstance(parsed, dict):
                raw = [str(v) for v in parsed.values()]
            else:
                raw = text.replace("\\", ",").split(",")
        except (TypeError, ValueError):
            raw = text.replace("\\", ",").split(",")
    values = {
        strip_diacritics(str(item).strip()).casefold()
        for item in raw
        if str(item or "").strip()
    }
    return sorted(values)


def _music_roots() -> list[str]:
    roots: list[str] = []
    try:
        from services.infrastructure.filesystem_service import resolve_music_dir

        configured = resolve_music_dir()
        if configured:
            roots.append(str(configured))
    except Exception:
        pass
    for key in ("MUSIC_FOLDER", "MUSIC_ROOT", "MUSIC_DIR"):
        value = os.environ.get(key)
        if value:
            roots.append(str(value))
    roots.append("/music")
    return roots


def path_keys(value: Any) -> set[str]:
    """Normalised path spellings used for exact identity comparison."""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return set()
    raw = os.path.normpath(raw).replace("\\", "/")
    keys = {os.path.normcase(raw)}
    for root in _music_roots():
        root_norm = os.path.normpath(str(root or "")).replace("\\", "/").rstrip("/")
        if not root_norm:
            continue
        if raw == root_norm:
            continue
        if raw.startswith(root_norm + "/"):
            keys.add(os.path.normcase(raw[len(root_norm) + 1:]))
        elif not os.path.isabs(raw):
            keys.add(os.path.normcase(os.path.join(root_norm, raw).replace("\\", "/")))
    return {key for key in keys if key}


def _navidrome_track_genres(track: dict[str, Any]) -> Any:
    for key in ("genres", "genre", "navidrome_genres", "navidrome_genre"):
        value = track.get(key)
        if value not in (None, ""):
            return value
    tags = track.get("tags")
    if isinstance(tags, dict):
        for key in ("genres", "genre", "GENRE"):
            value = tags.get(key)
            if value not in (None, ""):
                return value
    return ""


def _record(
    *,
    track_id: Any,
    file_path: Any,
    title: Any,
    artist: Any,
    album_artist: Any,
    album: Any,
    year: Any,
    track_number: Any,
    disc_number: Any,
    duration: Any,
    recording_mbid: Any,
    genres: Any,
) -> dict[str, str]:
    return {
        "id": _text(track_id),
        "file_path": next(iter(sorted(path_keys(file_path))), ""),
        "title": _text(title),
        "artist": _text(artist),
        "album_artist": _text(album_artist),
        "album": _text(album),
        "year": _year_text(year),
        "track_number": _number_text(track_number),
        "disc_number": _number_text(disc_number),
        "duration": _duration_text(duration),
        "musicbrainz_trackid": _text(recording_mbid).casefold(),
        "navidrome_genres": "|".join(_genre_values(genres)),
    }


def navidrome_row_record(
    track: dict[str, Any], *, album_name: str, album_artist: str = "",
) -> dict[str, str]:
    return _record(
        track_id=track.get("id"),
        file_path=track.get("path") or track.get("file_path"),
        title=track.get("title"),
        artist=clean_artist_name_for_storage(track.get("artist") or ""),
        album_artist=clean_artist_name_for_storage(
            track.get("albumArtist") or album_artist or ""
        ),
        album=album_name,
        year=track.get("year"),
        track_number=track.get("trackNumber") if "trackNumber" in track else track.get("track"),
        disc_number=track.get("discNumber") if "discNumber" in track else track.get("disc"),
        duration=track.get("duration"),
        recording_mbid=(
            track.get("musicBrainzId")
            or track.get("musicbrainz_trackid")
            or track.get("recording_mbid")
            or track.get("mbid")
        ),
        genres=_navidrome_track_genres(track),
    )


def db_row_record(row: dict[str, Any]) -> dict[str, str]:
    return _record(
        track_id=row.get("id"),
        file_path=row.get("file_path"),
        title=row.get("title"),
        artist=row.get("artist"),
        album_artist=row.get("album_artist"),
        album=row.get("album"),
        year=row.get("year") or row.get("release_year"),
        track_number=row.get("track_number"),
        disc_number=row.get("disc_number"),
        duration=row.get("duration"),
        recording_mbid=(
            row.get("recording_mbid")
            or row.get("musicbrainz_trackid")
            or row.get("mbid")
        ),
        genres=row.get("navidrome_genres") or row.get("navidrome_genre"),
    )


def _fingerprint(records: Iterable[dict[str, str]]) -> str:
    ordered = sorted(
        ({field: record.get(field, "") for field in FINGERPRINT_FIELDS} for record in records),
        key=lambda item: (item.get("id", ""), item.get("file_path", "")),
    )
    payload = json.dumps(ordered, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def album_fingerprint(records: Iterable[dict[str, str]]) -> str:
    return _fingerprint(records)


def navidrome_album_fingerprint(
    tracks: list[dict[str, Any]], *, album_name: str, album_artist: str = "",
) -> str:
    return album_fingerprint(
        navidrome_row_record(t, album_name=album_name, album_artist=album_artist)
        for t in tracks
    )


def db_album_fingerprint(rows: list[dict[str, Any]]) -> str:
    return album_fingerprint(db_row_record(row) for row in rows)


def album_metadata_dirty(rows: list[dict[str, Any]]) -> bool:
    return any(bool(row.get("metadata_dirty_for_navidrome")) for row in rows)


# ---------------------------------------------------------------------------
# Local-write marking / import clearing
# ---------------------------------------------------------------------------

def mark_local_tags_written_for_track_ids(track_ids: Iterable[Any]) -> int:
    ids = [str(value).strip() for value in track_ids or [] if str(value or "").strip()]
    if not ids:
        return 0
    placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
    params = {f"id{i}": value for i, value in enumerate(ids)}
    try:
        from sqlalchemy import text

        with db_session() as session:
            result = session.execute(
                text(
                    "UPDATE tracks SET "
                    "local_tags_written_at = CURRENT_TIMESTAMP, "
                    "metadata_dirty_for_navidrome = TRUE, "
                    "metadata_source = 'popularr' "
                    f"WHERE CAST(id AS TEXT) IN ({placeholders})"
                ),
                params,
            )
            return int(result.rowcount or 0)
    except Exception as exc:
        logger.debug("Local tag write stamp failed", count=len(ids), error=str(exc))
        return 0


def mark_local_tags_written_for_path(file_path: str) -> int:
    keys = sorted(path_keys(file_path))
    if not keys:
        return 0
    placeholders = ", ".join(f":p{i}" for i in range(len(keys)))
    params = {
        f"p{i}": key.replace("\\", "/").casefold()
        for i, key in enumerate(keys)
    }
    try:
        from sqlalchemy import text

        with db_session() as session:
            result = session.execute(
                text(
                    "UPDATE tracks SET "
                    "local_tags_written_at = CURRENT_TIMESTAMP, "
                    "metadata_dirty_for_navidrome = TRUE, "
                    "metadata_source = 'popularr' "
                    "WHERE LOWER(REPLACE(COALESCE(file_path, ''), '\\', '/')) "
                    f"IN ({placeholders})"
                ),
                params,
            )
            return int(result.rowcount or 0)
    except Exception as exc:
        logger.debug("Local tag path stamp failed", path=file_path, error=str(exc))
        return 0


def has_dirty_navidrome_metadata() -> bool:
    try:
        from sqlalchemy import text

        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT 1 FROM tracks "
                    "WHERE COALESCE(metadata_dirty_for_navidrome, FALSE) = TRUE "
                    "LIMIT 1"
                )
            ).fetchone()
        return bool(row)
    except Exception as exc:
        logger.debug("Dirty Navidrome metadata check failed", error=str(exc))
        # Unknown is not "clean": force the safe path (scan/full reconcile).
        return True


def mark_navidrome_import_confirmed(track_ids: Iterable[Any]) -> int:
    ids = [str(value).strip() for value in track_ids or [] if str(value or "").strip()]
    if not ids:
        return 0
    placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
    params = {f"id{i}": value for i, value in enumerate(ids)}
    try:
        from sqlalchemy import text

        with db_session() as session:
            result = session.execute(
                text(
                    "UPDATE tracks SET "
                    "metadata_dirty_for_navidrome = FALSE, "
                    "navidrome_imported_at = CURRENT_TIMESTAMP, "
                    "metadata_source = 'navidrome_confirmed' "
                    f"WHERE CAST(id AS TEXT) IN ({placeholders})"
                ),
                params,
            )
            return int(result.rowcount or 0)
    except Exception as exc:
        logger.debug("Navidrome import confirmation stamp failed", error=str(exc))
        return 0


def mark_navidrome_import_seen(track_ids: Iterable[Any]) -> int:
    ids = [str(value).strip() for value in track_ids or [] if str(value or "").strip()]
    if not ids:
        return 0
    placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
    params = {f"id{i}": value for i, value in enumerate(ids)}
    try:
        from sqlalchemy import text

        with db_session() as session:
            result = session.execute(
                text(
                    "UPDATE tracks SET "
                    "navidrome_imported_at = CURRENT_TIMESTAMP "
                    f"WHERE CAST(id AS TEXT) IN ({placeholders}) "
                    "AND COALESCE(metadata_dirty_for_navidrome, FALSE) = FALSE"
                ),
                params,
            )
            return int(result.rowcount or 0)
    except Exception as exc:
        logger.debug("Navidrome import seen stamp failed", error=str(exc))
        return 0


def _authority_value(field: str, source: dict[str, Any]) -> str:
    if field in ("recording_mbid", "musicbrainz_trackid", "mbid"):
        return _text(
            source.get("recording_mbid")
            or source.get("musicbrainz_trackid")
            or source.get("mbid")
        ).casefold()
    if field == "file_path":
        return next(iter(sorted(path_keys(source.get("file_path")))), "")
    if field == "duration":
        return _duration_text(source.get("duration"))
    if field in {"year", "release_year"}:
        return _year_text(source.get(field))
    if field in {"track_number", "disc_number"}:
        return _number_text(source.get(field))
    if field in {"navidrome_genres", "navidrome_genre"}:
        return "|".join(
            _genre_values(source.get(field) or source.get("genres") or source.get("genre"))
        )
    return _text(source.get(field))


def local_authority_conflicts(
    existing: dict[str, Any], payload: dict[str, Any],
) -> list[str]:
    conflicts: list[str] = []
    for field in LOCAL_AUTHORITY_FIELDS:
        if field not in payload:
            continue
        if _authority_value(field, existing) != _authority_value(field, payload):
            conflicts.append(field)
    return conflicts


def local_authority_values_match(
    existing: dict[str, Any], payload: dict[str, Any],
) -> bool:
    """True when an import payload agrees with the stored local values.

    Only fields PRESENT in the payload are compared. A field Navidrome never
    echoes (some MBIDs) must not keep a row dirty forever; a differing value
    on a field it DID send keeps the local correction protected.
    """
    return not local_authority_conflicts(existing, payload)


# ---------------------------------------------------------------------------
# Existing-row indexes / ID adoption
# ---------------------------------------------------------------------------

def build_existing_indexes(rows: list[dict[str, Any]]) -> tuple[dict[str, list[str]], dict[str, list[dict[str, Any]]]]:
    by_path: dict[str, list[str]] = {}
    by_mbid: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        track_id = _text(row.get("id"))
        if not track_id:
            continue
        for key in path_keys(row.get("file_path")):
            by_path.setdefault(key, []).append(track_id)
        mbid = _text(
            row.get("recording_mbid")
            or row.get("musicbrainz_trackid")
            or row.get("mbid")
        ).casefold()
        if mbid:
            by_mbid.setdefault(mbid, []).append(row)
    return by_path, by_mbid


def find_adoptable_track_id(
    track: dict[str, Any],
    *,
    album_name: str,
    album_artist: str = "",
    by_path: dict[str, list[str]],
    by_mbid: dict[str, list[dict[str, Any]]],
) -> tuple[str, str] | None:
    """Return ``(old_id, match_reason)`` for the same logical track, or None.

    Path is the strongest evidence. MBID is accepted only when the title and
    artist corroborate — a Work/recording ID alone must not merge a live,
    cover, remix, acoustic or re-recorded version.
    """
    new_id = _text(track.get("id"))
    if not new_id:
        return None

    path_matches: set[str] = set()
    for key in path_keys(track.get("path") or track.get("file_path")):
        for old_id in by_path.get(key, []):
            if old_id and old_id != new_id:
                path_matches.add(old_id)
    if len(path_matches) == 1:
        return next(iter(path_matches)), "file_path"
    if len(path_matches) > 1:
        logger.warning(
            "Ambiguous Navidrome ID adoption — several rows share the file path",
            new_id=new_id, candidates=sorted(path_matches),
        )
        return None

    mbid = _text(
        track.get("musicBrainzId")
        or track.get("musicbrainz_trackid")
        or track.get("recording_mbid")
        or track.get("mbid")
    ).casefold()
    if not mbid:
        return None
    candidates = [
        row for row in by_mbid.get(mbid, [])
        if _text(row.get("id")) and _text(row.get("id")) != new_id
    ]
    if not candidates:
        return None

    incoming_title = strip_diacritics(_text(track.get("title"))).casefold()
    incoming_artist = strip_diacritics(_text(track.get("artist"))).casefold()
    incoming_album = strip_diacritics(_text(album_name)).casefold()
    incoming_duration = _duration_text(track.get("duration"))

    def _corroborates(row: dict[str, Any]) -> bool:
        if strip_diacritics(_text(row.get("title"))).casefold() != incoming_title:
            return False
        if strip_diacritics(_text(row.get("artist"))).casefold() != incoming_artist:
            return False
        row_album = strip_diacritics(_text(row.get("album"))).casefold()
        if row_album and incoming_album and row_album != incoming_album:
            return False
        row_duration = _duration_text(row.get("duration"))
        if row_duration and incoming_duration:
            try:
                if abs(float(row_duration) - float(incoming_duration)) > 2.0:
                    return False
            except (TypeError, ValueError):
                pass
        return True

    compatible = [row for row in candidates if _corroborates(row)]
    if len(compatible) == 1:
        return _text(compatible[0].get("id")), "recording_mbid"
    logger.warning(
        "Ambiguous Navidrome ID adoption — recording MBID did not uniquely corroborate",
        new_id=new_id, mbid=mbid, candidates=len(candidates),
    )
    return None


def adopt_navidrome_track_id(old_id: str, new_id: str) -> bool:
    """Move an existing logical row to the new Navidrome song ID.

    Preserves every Popularr-owned column because only the primary key is
    changed. ID-bearing references are migrated in the same transaction.
    """
    old_id = _text(old_id)
    new_id = _text(new_id)
    if not old_id or not new_id or old_id == new_id:
        return False
    try:
        from sqlalchemy import text

        with db_session() as session:
            existing_new = session.execute(
                text("SELECT 1 FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": new_id},
            ).fetchone()
            if existing_new:
                logger.warning(
                    "Refusing Navidrome ID adoption — new ID already has a row",
                    old_id=old_id, new_id=new_id,
                )
                return False

            # ``metadata_conflicts.track_id`` has a foreign key to tracks(id)
            # WITHOUT ON UPDATE CASCADE. Retire the old conflict rows BEFORE
            # moving the primary key, or PostgreSQL rejects the parent update.
            try:
                with session.begin_nested():
                    _removed_conflicts = session.execute(
                        text("DELETE FROM metadata_conflicts WHERE track_id = :old"),
                        {"old": old_id},
                    ).rowcount or 0
            except Exception:
                _removed_conflicts = 0
            if _removed_conflicts:
                logger.info(
                    "Retired metadata conflicts for adopted Navidrome ID",
                    old_id=old_id, new_id=new_id, count=_removed_conflicts,
                )

            session.execute(
                text("UPDATE tracks SET id = :new WHERE CAST(id AS TEXT) = :old"),
                {"new": new_id, "old": old_id},
            )
            session.execute(
                text("UPDATE playlist_entries SET track_id = :new WHERE track_id = :old"),
                {"new": new_id, "old": old_id},
            )
            session.execute(
                text("UPDATE genre_updates SET track_id = :new WHERE track_id = :old"),
                {"new": new_id, "old": old_id},
            )
            try:
                with session.begin_nested():
                    session.execute(
                        text(
                            "UPDATE user_favourites SET entity_id = :new "
                            "WHERE entity_type = 'track' AND entity_id = :old"
                        ),
                        {"new": new_id, "old": old_id},
                    )
                    session.execute(
                        text("UPDATE user_favourites SET navidrome_id = :new WHERE navidrome_id = :old"),
                        {"new": new_id, "old": old_id},
                    )
            except Exception:
                # A unique favourite for the NEW id already exists; retire the
                # old pointer instead of aborting the ID migration.
                try:
                    with session.begin_nested():
                        session.execute(
                            text(
                                "DELETE FROM user_favourites "
                                "WHERE entity_type = 'track' AND entity_id = :old"
                            ),
                            {"old": old_id},
                        )
                except Exception:
                    logger.debug(
                        "Could not retire old user_favourites pointer",
                        old_id=old_id,
                    )
        logger.info(
            "[NAVIDROME_IMPORT] track ID adopted",
            old_id=old_id, new_id=new_id,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Navidrome track ID adoption failed",
            old_id=old_id, new_id=new_id, error=str(exc),
        )
        return False


def invalidate_track_id_caches() -> None:
    """Invalidate caches that contain Navidrome song IDs, after commit."""
    try:
        from services.popularity.stages.finalise_stage import (
            invalidate_genre_playlist_rows,
        )

        invalidate_genre_playlist_rows()
    except Exception as exc:
        logger.debug("Genre playlist row cache invalidation skipped", error=str(exc))
