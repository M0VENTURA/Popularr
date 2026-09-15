"""Album-name cleaning + write-back during popularity scans.

Driven by the ``metadata_update`` config block ("Updating Metadata" section
on the Config page):

- ``album_name_source`` — how the target name is derived:
    * ``dedupe``  — collapse duplicated annotations, PRESERVE the edition
                    (recommended; "X (tour edition) (tour edition)" becomes
                    "X (tour edition)").
    * ``album``   — collapse duplicates AND strip edition markers entirely
                    ("X (tour edition)" becomes "X"). Legacy behaviour.
    * ``release`` — prefer the MusicBrainz release title when a confident
                    match exists, falling back to ``dedupe``.
- ``album_name_update_target`` — ``db`` (tracks table only) or ``files``
  (tracks table AND the ALBUM tag on the audio files).
- ``update_on_files`` — per-field file-tag write switches; ``album_name``
  gates the file write for the album name specifically.

The scan calls :func:`repair_album_annotations` once per album (inline, early)
to collapse duplicated annotations in the album name and track titles, and
:func:`resolve_album_name` / :func:`apply_album_name_update` for the
configured rename. When the resolved name differs from the stored name, the
tracks rows are updated and, if configured, the audio file ALBUM tags are
rewritten (Navidrome reads file tags, so the new name then re-serves from
Navidrome too).
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def get_metadata_update_config() -> dict[str, Any]:
    """Thin wrapper around the config helper (avoids circular imports)."""
    from helpers.config_helpers import get_metadata_update_config as _get
    return _get()


def _repair_enabled() -> bool:
    """Inline per-album annotation repair toggle."""
    try:
        from helpers.config_helpers import get_feature
        return bool(get_feature("repair_album_annotations", True))
    except Exception:
        return True


def _repaired_name(current: str) -> str:
    """Collapse duplicated annotations, PRESERVING one copy of each.

    "The Fall of Hearts (tour edition) (tour edition)"
        -> "The Fall of Hearts (tour edition)"
    """
    from helpers.normalization_service import repair_annotations
    return repair_annotations(current or "")


def _cleaned_name(current: str) -> str:
    """Repair duplicates, then strip trailing edition markers entirely.

    NOTE: this REMOVES the edition ("X (tour edition)" -> "X"). Duplicates
    are collapsed first so a mangled name still reduces correctly even when
    the marker itself is not in the strip keyword list.
    """
    from helpers.normalization_service import strip_album_edition_marker
    return strip_album_edition_marker(_repaired_name(current))


def _release_title_for_album(artist: str, album: str) -> str | None:
    """Return a confident MusicBrainz release title for the album, or None.

    Uses the release-group search; only returns a title when the best match
    clears the confidence floor (``match_score``) and differs from the
    cleaned album name — "Release Name" should only win over the current
    name when MusicBrainz is genuinely confident about the release.
    """
    try:
        from services.enrichment.musicbrainz_service import get_shared_mb_service
        svc = get_shared_mb_service()
        if not svc.enabled:
            return None
        matches = svc.search_releasegroup_matches(artist, album, limit=5) or []
        if not matches:
            return None
        best = matches[0]
        # Confidence floor: a match_score around 0.5+ means the title+artist
        # genuinely matched; below that the search is noise and renaming to
        # it would be wrong.
        try:
            score = float(best.get("match_score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score < 0.5:
            return None
        title = str(best.get("title") or "").strip()
        if not title:
            return None
        # Never "correct" to something that looks like the same name.
        cleaned_current = _cleaned_name(album)
        from helpers.normalization_service import normalize_title_for_lookup
        if normalize_title_for_lookup(title) == normalize_title_for_lookup(cleaned_current):
            return None
        return title
    except Exception as exc:
        logger.debug(
            "MB release-title lookup failed",
            artist=artist,
            album=album,
            error=str(exc),
        )
        return None


def album_name_collides(artist: str, candidate: str, current: str) -> bool:
    """True when ``candidate`` already exists as a DIFFERENT album for ``artist``.

    Guards against silently merging two distinct releases — e.g. renaming
    "X (tour edition)" to "X" when a separate "X" already exists. Fails safe:
    an unverifiable candidate is treated as colliding.
    """
    if not artist or not candidate:
        return False
    try:
        from sqlalchemy import text
        from db.engine import db_session
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT 1 FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "  AND LOWER(TRIM(album)) = LOWER(TRIM(:candidate)) "
                    "  AND LOWER(TRIM(album)) <> LOWER(TRIM(:current)) "
                    "LIMIT 1"
                ),
                {"artist": artist, "candidate": candidate, "current": current},
            ).first()
            return bool(row)
    except Exception as exc:
        logger.warning(
            "[ALBUM_NAME] Collision check failed — treating as collision",
            artist=artist,
            candidate=candidate,
            error=str(exc),
        )
        return True


def resolve_album_name(
    *,
    artist: str,
    album: str,
    config: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Resolve the target album name + the reason it changed.

    Returns ``(new_name, reason)`` where ``reason`` is None when the name is
    unchanged (no write needed).

    Every candidate is validated through ``safe_album_rename()``, which
    rejects a proposal that only repeats an annotation the album already
    carries, and — in ``dedupe``/``release`` modes — refuses one that would
    drop the edition annotation the library uses to keep pressings distinct.
    """
    from helpers.normalization_service import safe_album_rename

    cfg = config or get_metadata_update_config()
    source = str(cfg.get("album_name_source") or "dedupe").strip().lower()
    current = (album or "").strip()

    if not current:
        return album, None

    if source == "release":
        release_title = _release_title_for_album(artist, album)
        if release_title:
            resolved, verdict = safe_album_rename(current, release_title)
            if resolved and resolved != current:
                return resolved, f"release ({verdict})"
            logger.debug(
                "[ALBUM_NAME] Release-title rename rejected",
                artist=artist, album=album, proposed=release_title, reason=verdict,
            )
        # Fall through to annotation repair.

    if source == "album":
        # Legacy mode: strip the edition entirely. `safe_album_rename` would
        # veto that as "would drop edition annotation", so this mode compares
        # directly and is explicitly opt-in.
        cleaned = _cleaned_name(current)
        if cleaned and cleaned != current:
            return cleaned, "cleaned"
        return album, None

    # Default: `dedupe` — collapse duplicated annotations, keep the edition.
    #
    # This is deliberately NOT routed through `safe_album_rename()`.
    # That validator exists to vet EXTERNAL proposals (a MusicBrainz release
    # title), and one of its rejection rules is "no change after annotation
    # normalisation" — which is true by definition of a dedupe, since the
    # repaired name IS the normalised form of the current one. Passing a
    # self-repair through it vetoes every repair and the damaged name
    # survives.
    #
    # A dedupe is safe by construction: `repair_annotations()` only removes
    # annotations that duplicate an earlier one, so no distinct annotation
    # and no edition can be lost.
    repaired = _repaired_name(current)
    if repaired and repaired != current:
        return repaired, "deduped"

    return album, None


def _album_file_paths(artist: str, album: str) -> list[str]:
    """File paths for every track of the album. MUST be called BEFORE the rename."""
    try:
        from sqlalchemy import text
        from db.engine import db_session
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT file_path FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                      AND file_path IS NOT NULL AND TRIM(file_path) <> ''
                """),
                {"artist": artist, "album": album},
            ).fetchall() or []
        return [str(r[0] or "") for r in rows if r and r[0]]
    except Exception as exc:
        logger.warning(
            "[ALBUM_NAME] File-list load failed",
            artist=artist,
            album=album,
            error=str(exc),
        )
        return []


def update_album_name_in_db(
    *,
    artist: str,
    album: str,
    new_name: str,
) -> int:
    """Rewrite the album name for every track of the album in the DB.

    Returns the number of rows updated (0 when nothing changed).
    """
    if not artist or not album or not new_name or new_name == album:
        return 0
    try:
        from sqlalchemy import text
        from db.engine import db_session
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE tracks
                    SET album = :new_name
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                """),
                {"artist": artist, "album": album, "new_name": new_name},
            )
            return result.rowcount or 0
    except Exception as exc:
        logger.warning(
            "[ALBUM_NAME] DB update failed",
            artist=artist,
            album=album,
            new_name=new_name,
            error=str(exc),
        )
        return 0


def write_album_tag_to_files(
    *,
    file_paths: list[str],
    new_name: str,
) -> int:
    """Rewrite the ALBUM tag on the supplied audio files.

    Takes explicit paths rather than re-querying, because by the time this
    runs the DB rename has already happened and the old album name no longer
    matches anything.

    Returns the number of files written.
    """
    if not file_paths or not new_name:
        return 0

    written = 0
    failed = 0
    for fp in file_paths:
        if not fp:
            continue
        try:
            # Only the ALBUM frame is rewritten — passing a dict with
            # other fields None would make the FLAC/MP3 writer DELETE
            # those frames (None = "clear this field").
            from services.metadata.tag_file_service import write_tags_to_file
            if write_tags_to_file(fp, {"album": new_name}):
                written += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "[ALBUM_NAME] File tag write failed",
                file_path=fp,
                new_name=new_name,
                error=str(exc),
            )

    if failed:
        logger.warning(
            "[ALBUM_NAME] Some album tags could not be written — "
            "DB and file tags are now out of sync",
            new_name=new_name,
            written=written,
            failed=failed,
        )
    return written


def update_album_name_in_files(
    *,
    artist: str,
    album: str,
    new_name: str,
) -> int:
    """Rewrite the ALBUM tag on the audio files of the album.

    Retained for backward compatibility / manual use. Resolves the file list
    from the CURRENT album name, so it must be called BEFORE the DB rename.
    Internally :func:`apply_album_name_update` uses the explicit-path form.
    """
    if not artist or not album or not new_name or new_name == album:
        return 0
    return write_album_tag_to_files(
        file_paths=_album_file_paths(artist, album),
        new_name=new_name,
    )


def apply_album_name_update(
    *,
    artist: str,
    album: str,
    new_name: str,
    config: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply a resolved album-name rename to the DB and (optionally) files.

    Returns a summary dict:
    ``{"changed": bool, "new_name": str, "reason": str|None, "db_updated": int, "files_updated": int, "skipped_reason": str}``

    Safe no-op when ``new_name`` equals the current name.

    ORDERING: the audio file paths are collected BEFORE the DB rename.
    Previously the DB was updated first and the file query then filtered on
    the OLD album name, which by then matched zero rows — so the ALBUM tag
    was never actually written to disk. Navidrome reads file tags, so the
    rename appeared to "not update" there, and a later library re-import
    could reintroduce the stale name from the untouched tags.
    """
    cfg = config or get_metadata_update_config()
    result: dict[str, Any] = {
        "changed": False,
        "new_name": new_name,
        "reason": reason,
        "db_updated": 0,
        "files_updated": 0,
        "skipped_reason": "",
    }
    if not album or not artist or not new_name or new_name == album:
        result["skipped_reason"] = "no rename required"
        return result

    # Never merge two distinct releases together.
    if album_name_collides(artist, new_name, album):
        logger.warning(
            "[ALBUM_NAME] Rename skipped — target name already exists",
            artist=artist, album=album, new_name=new_name,
        )
        _log_unified(
            f"[ALBUM_NAME] Skipped '{artist} - {album}' → '{new_name}' "
            f"(an album already exists under that name; resolve manually)"
        )
        result["skipped_reason"] = "target name collides with an existing album"
        return result

    target = str(cfg.get("album_name_update_target") or "db").strip().lower()
    update_on_files = bool((cfg.get("update_on_files") or {}).get("album_name", False))
    want_files = target == "files" and update_on_files

    # Collect paths BEFORE the rename — afterwards the old name matches nothing.
    file_paths = _album_file_paths(artist, album) if want_files else []

    db_updated = update_album_name_in_db(artist=artist, album=album, new_name=new_name)

    if db_updated <= 0:
        result["skipped_reason"] = "database update affected no rows"
        return result

    files_updated = 0
    if want_files:
        if file_paths:
            files_updated = write_album_tag_to_files(
                file_paths=file_paths,
                new_name=new_name,
            )
        else:
            logger.debug(
                "[ALBUM_NAME] No audio files resolved for album",
                artist=artist, album=album,
            )

    result.update({
        "changed": True,
        "new_name": new_name,
        "db_updated": db_updated,
        "files_updated": files_updated,
    })
    return result


# ---------------------------------------------------------------------------
# Inline per-album annotation repair (called early in the scan loop)
# ---------------------------------------------------------------------------

def _log_unified(message: str) -> None:
    try:
        from helpers.logging_config import log_unified
        log_unified(message)
    except Exception:
        logger.info(message)


def repair_album_annotations(
    artist: str,
    album: str,
    tracks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Collapse duplicated annotations in ONE album's name and track titles.

    Runs inline during a metadata/full scan, so the library self-heals
    incrementally instead of needing a separate maintenance pass. Must be
    called BEFORE ``prepare_tracks_for_album()`` so every downstream stage —
    including the ``sync_album_file_tags()`` call later in the same album
    iteration — operates on the corrected values and propagates them to the
    audio file tags on the same pass.

    Returns::

        {
            "album_repaired": bool,
            "album": str,            # name to use for the rest of the scan
            "old_album": str,
            "titles_repaired": int,
            "skipped_reason": str,
        }

    The caller MUST adopt the returned ``album`` value. Repairs only collapse
    DUPLICATED annotations — one copy of the edition is always preserved, so
    "(tour edition)" is never lost.
    """
    from helpers.normalization_service import repair_annotations

    result: dict[str, Any] = {
        "album_repaired": False,
        "album": album,
        "old_album": album,
        "titles_repaired": 0,
        "skipped_reason": "",
    }

    if not _repair_enabled():
        result["skipped_reason"] = "disabled by features.repair_album_annotations"
        return result

    if not artist or not album:
        result["skipped_reason"] = "artist or album missing"
        return result

    cleaned_album = _repaired_name(album)
    album_needs_repair = bool(cleaned_album) and cleaned_album != album

    # Track titles are repaired regardless of whether the album name is dirty:
    # the live/acoustic double-suffix bug damages titles on albums whose names
    # are perfectly fine.
    title_updates: list[tuple[str, str, str]] = []
    for track in tracks or []:
        track_id = str(track.get("id") or "").strip()
        title = str(track.get("title") or "")
        if not track_id or not title:
            continue
        cleaned_title = repair_annotations(title)
        if cleaned_title and cleaned_title != title:
            title_updates.append((track_id, title, cleaned_title))

    if not album_needs_repair and not title_updates:
        return result

    try:
        from sqlalchemy import text
        from db.engine import db_session

        if album_needs_repair and album_name_collides(artist, cleaned_album, album):
            logger.warning(
                "Album annotation repair skipped — cleaned name already exists",
                artist=artist, album=album, cleaned=cleaned_album,
            )
            _log_unified(
                f"[ALBUM_REPAIR] Skipped '{artist} - {album}' → '{cleaned_album}' "
                f"(an album already exists under the cleaned name; resolve manually)"
            )
            album_needs_repair = False
            result["skipped_reason"] = "cleaned name collides with an existing album"

        with db_session() as session:
            if album_needs_repair:
                updated = session.execute(
                    text(
                        "UPDATE tracks SET album = :new_album "
                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                        "  AND album = :old_album"
                    ),
                    {"new_album": cleaned_album, "artist": artist, "old_album": album},
                ).rowcount or 0

                result["album_repaired"] = True
                result["album"] = cleaned_album
                _log_unified(
                    f"[ALBUM_REPAIR] '{artist} - {album}' → '{cleaned_album}' "
                    f"(collapsed duplicate annotation, {updated} track(s))"
                )
                logger.info(
                    "Album annotation repaired",
                    artist=artist, old=album, new=cleaned_album, rows=updated,
                )

            for track_id, old_title, new_title in title_updates:
                nested = session.begin_nested()
                try:
                    session.execute(
                        text("UPDATE tracks SET title = :title WHERE id = :tid"),
                        {"title": new_title, "tid": track_id},
                    )
                    if nested.is_active:
                        nested.commit()
                    result["titles_repaired"] += 1
                    logger.info(
                        "Track title annotation repaired",
                        track_id=track_id, old=old_title, new=new_title,
                    )
                except Exception as exc:
                    logger.warning(
                        "Track title repair failed",
                        track_id=track_id, old=old_title, new=new_title, error=str(exc),
                    )
                    try:
                        nested.rollback()
                    except Exception as rollback_exc:
                        logger.debug(
                            "Title repair rollback failed",
                            track_id=track_id, error=str(rollback_exc),
                        )

        if result["titles_repaired"]:
            _log_unified(
                f"[ALBUM_REPAIR] {artist} - {result['album']}: collapsed duplicate "
                f"annotations in {result['titles_repaired']} track title(s)"
            )

    except Exception as exc:
        logger.warning(
            "Album annotation repair failed", artist=artist, album=album, error=str(exc)
        )
        result["skipped_reason"] = f"{type(exc).__name__}: {exc}"
        # Never advance the album name if the write failed.
        result["album_repaired"] = False
        result["album"] = album
        return result

    # Apply repaired values back onto the caller's in-memory dicts so the rest
    # of this album iteration sees the corrected values.
    if title_updates:
        by_id = {tid: new for tid, _old, new in title_updates}
        for track in tracks or []:
            tid = str(track.get("id") or "").strip()
            if tid in by_id:
                track["title"] = by_id[tid]

    if result["album_repaired"]:
        for track in tracks or []:
            track["album"] = result["album"]

    return result


def clean_album_name_for_scan(
    *,
    artist: str,
    album: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve + apply the configured album-name cleaning in one call.

    Convenience wrapper around :func:`resolve_album_name` +
    :func:`apply_album_name_update` for one-shot / manual use.  The scan
    runner uses the two-step form so the DB rename can be DEFERRED until
    after the artist's star-rating finalise.

    Returns the :func:`apply_album_name_update` summary (``changed`` False
    when nothing needs renaming).
    """
    cfg = config or get_metadata_update_config()
    if not album or not artist:
        return {
            "changed": False,
            "new_name": album,
            "reason": None,
            "db_updated": 0,
            "files_updated": 0,
            "skipped_reason": "artist or album missing",
        }

    new_name, reason = resolve_album_name(artist=artist, album=album, config=cfg)
    if not reason or new_name == album:
        return {
            "changed": False,
            "new_name": album,
            "reason": None,
            "db_updated": 0,
            "files_updated": 0,
            "skipped_reason": "no rename required",
        }

    result = apply_album_name_update(
        artist=artist,
        album=album,
        new_name=new_name,
        config=cfg,
        reason=reason,
    )
    result["reason"] = reason
    if result.get("changed"):
        _log_unified(
            f"[ALBUM_NAME] '{artist} - {album}' → '{new_name}' "
            f"(reason={reason}, db={result.get('db_updated')}, "
            f"files={result.get('files_updated')})"
        )
    elif result.get("skipped_reason"):
        logger.debug(
            "[ALBUM_NAME] Rename not applied",
            artist=artist, album=album, new_name=new_name,
            reason=result.get("skipped_reason"),
        )

    return result
