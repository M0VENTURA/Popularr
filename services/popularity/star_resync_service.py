"""Re-rate the library from STORED data — no provider calls, no full scan.

Why this exists
---------------
Scoring changes (weights, tier thresholds, the album star curve) only reach the
library through a scan, and a scan re-queries Last.fm / ListenBrainz / Discogs
for every track. That is the wrong tool for "I changed a threshold": every
popularity input a rating depends on is already in the ``tracks`` table, so
re-rating is a pure recomputation.

This service therefore does NOT reimplement rating. It groups stored rows into
albums and calls the same ``post_album_star_ratings`` entry point a scan uses,
so every downstream rule still applies — compilation handling, live-album caps,
era slot limits, short-track clamps, favourite floors, genre invalidation and
Navidrome sync.

Usage
-----
    result = resync_star_ratings(dry_run=True)   # preview
    result = resync_star_ratings()               # apply
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Iterable

from sqlalchemy import text

import structlog

from db.engine import db_session

logger = structlog.get_logger(__name__)

__all__ = [
    "resync_star_ratings",
    "preview_star_ratings",
]

#: Columns the rating path reads. Kept explicit so the query stays cheap and
#: the mapping into the scan-shaped dict is obvious.
_TRACK_COLUMNS = (
    "id",
    "title",
    "artist",
    "album",
    "album_artist",
    "year",
    "release_year",
    "lastfm_listeners",
    "listenbrainz_listens",
    "popularity",
    "final_score",
    "raw_score",
    "lastfm_score",
    "listenbrainz_score",
    "stars",
    "single_confidence",
    "is_live",
    "is_compilation",
    "duration",
    "file_path",
    "musicbrainz_albumtype",
    "spotify_album_type",
    "disc_number",
    "track_number",
)


def _row_to_track(row: Any) -> dict[str, Any]:
    """Map one ``tracks`` row onto the dict shape the rating path expects."""
    m = row._mapping if hasattr(row, "_mapping") else row

    def _get(name: str, default: Any = None) -> Any:
        try:
            return m.get(name, default)
        except Exception:
            return default

    track_id = str(_get("id") or "").strip()
    return {
        "track_id": track_id,
        "id": track_id,
        "title": str(_get("title") or ""),
        "artist": str(_get("artist") or ""),
        "album": str(_get("album") or ""),
        "album_artist": str(_get("album_artist") or ""),
        "year": _get("year") or _get("release_year"),
        "release_year": _get("release_year"),
        "lastfm_listeners": int(_get("lastfm_listeners") or 0),
        "listenbrainz_listens": int(_get("listenbrainz_listens") or 0),
        # ``popularity_score`` is what _assign_stars prefers. Do not fabricate
        # it: when it is absent the code falls back to final_score, and when
        # both are absent the track has no score and is skipped.
        "popularity_score": _get("popularity") or 0,
        "popularity": _get("popularity") or 0,
        "final_score": _get("final_score") or 0,
        "raw_score": _get("raw_score") or 0,
        "lastfm_score": _get("lastfm_score") or 0,
        "listenbrainz_score": _get("listenbrainz_score") or 0,
        "stars": int(_get("stars") or 0),
        "single_confidence": str(_get("single_confidence") or "low"),
        "is_live": int(_get("is_live") or 0),
        "is_compilation": int(_get("is_compilation") or 0),
        "duration": _get("duration"),
        "file_path": str(_get("file_path") or ""),
        "musicbrainz_albumtype": _get("musicbrainz_albumtype"),
        "spotify_album_type": _get("spotify_album_type"),
        "_resync": True,
    }


def _load_album_groups(
    *,
    artist: str | None = None,
    album: str | None = None,
    limit_albums: int | None = None,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Load scored tracks grouped by ``(album_artist_key, album)``.

    A track with no popularity score on either column cannot be rated — it was
    never scored, so re-rating it would invent a rating from nothing. Those are
    counted and reported rather than silently defaulted.
    """
    where = [
        "COALESCE(NULLIF(popularity, 0), NULLIF(final_score, 0)) IS NOT NULL",
        "COALESCE(album, '') <> ''",
    ]
    params: dict[str, Any] = {}
    if artist:
        where.append("COALESCE(NULLIF(album_artist, ''), artist) = :artist")
        params["artist"] = artist
    if album:
        where.append("album = :album")
        params["album"] = album

    sql = (
        "SELECT " + ", ".join(_TRACK_COLUMNS) + " FROM tracks "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY COALESCE(NULLIF(album_artist, ''), artist), album, disc_number, track_number"
    )

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    with db_session() as session:
        rows = session.execute(text(sql), params).fetchall() or []
    for row in rows:
        track = _row_to_track(row)
        key_artist = (track["album_artist"] or track["artist"]).strip()
        groups[(key_artist, track["album"].strip())].append(track)

    if limit_albums:
        keys = list(groups)[: int(limit_albums)]
        groups = {k: groups[k] for k in keys}
    return dict(groups)


def _apply_exclusion_flags(album_results: list[dict[str, Any]]) -> None:
    """Stamp ``exclude_from_stats`` the way the scan does.

    The flag is an in-memory verdict (``should_exclude_track_from_stats``), not
    a column, so a resync must compute it or outtakes and interludes would enter
    the album/artist distributions the curve and z-bands are built from.
    """
    try:
        from services.catalog.album_classification_service import (
            should_exclude_track_from_stats,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("exclusion helper unavailable", error=str(exc))
        return

    album_live = any(int(r.get("is_live") or 0) for r in album_results)
    album_type = ""
    for _candidate in album_results:
        album_type = str(
            _candidate.get("musicbrainz_albumtype")
            or _candidate.get("spotify_album_type")
            or ""
        )
        if album_type:
            break

    for row in album_results:
        try:
            dur = row.get("duration")
            try:
                dur_seconds = float(dur) if dur not in (None, "") else None
            except (TypeError, ValueError):
                dur_seconds = None
            row["exclude_from_stats"] = should_exclude_track_from_stats(
                title=str(row.get("title") or ""),
                album=str(row.get("album") or ""),
                is_live=int(row.get("is_live") or 0),
                album_context_live=1 if album_live else 0,
                album_type=album_type,
                duration=dur_seconds,
            )
        except Exception as exc:
            logger.debug(
                "exclusion verdict failed", title=row.get("title"), error=str(exc)
            )


def _compute_stars_for_album(
    album_results: list[dict[str, Any]],
    *,
    options: dict[str, Any],
    artist_scores: list[float],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Rate one album via the shared path and return before/after rows."""
    from services.popularity.stages.finalise_stage import post_album_star_ratings

    _apply_exclusion_flags(album_results)
    artist = str(album_results[0].get("album_artist") or album_results[0].get("artist") or "")

    before = {
        str(r.get("track_id") or ""): int(r.get("stars") or 0) for r in album_results
    }

    posted = post_album_star_ratings(
        album_results=album_results,
        artist=artist,
        artist_scores=artist_scores,
        options=options,
        stars_reconciled=True,
    )

    after = {
        str(r.get("track_id") or ""): int(r.get("stars") or 0) for r in album_results
    }
    changed = sum(1 for tid, st in after.items() if before.get(tid, 0) != st)
    summary = {
        "tracks": len(album_results),
        "changed": changed,
        "star_ratings": int(posted.get("star_ratings") or 0),
        "navidrome_synced": int(posted.get("navidrome_synced") or 0),
    }
    rows = [
        {
            "track_id": str(r.get("track_id") or ""),
            "title": str(r.get("title") or ""),
            "before": before.get(str(r.get("track_id") or ""), 0),
            "after": after.get(str(r.get("track_id") or ""), 0),
            "lastfm_listeners": int(r.get("lastfm_listeners") or 0),
        }
        for r in album_results
    ]
    return rows, summary


def resync_star_ratings(
    *,
    artist: str | None = None,
    album: str | None = None,
    limit_albums: int | None = None,
    dry_run: bool = False,
    write_file_tags: bool | None = None,
    sync_navidrome: bool | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Re-rate stored tracks from their existing popularity data.

    Args:
        artist: Restrict to one artist (``album_artist`` when present).
        album: Restrict to one album title (combine with ``artist``).
        limit_albums: Cap the number of albums processed (smoke tests).
        dry_run: Compute and report but write nothing — no DB, tag or provider
            writes. Implemented by rating a COPY of each album and discarding
            the result.
        write_file_tags: Override the configured file-tag behaviour.
        sync_navidrome: Override the configured Navidrome behaviour.
        progress: Optional callable ``(done, total, album_label)``.

    Returns:
        Summary dict with album/track counts and a list of changed tracks.
    """
    from helpers.config_helpers import get_star_resync_config

    cfg = get_star_resync_config()
    if not cfg.get("enabled") and not dry_run:
        return {
            "ok": False,
            "error": "Star resync is disabled in configuration.",
            "albums": 0, "tracks": 0, "changed": 0,
        }

    if write_file_tags is None:
        write_file_tags = bool(cfg.get("write_file_tags"))
    if sync_navidrome is None:
        sync_navidrome = bool(cfg.get("sync_navidrome"))
    if dry_run:
        write_file_tags = False
        sync_navidrome = False

    groups = _load_album_groups(artist=artist, album=album, limit_albums=limit_albums)
    total_albums = len(groups)

    # Artist catalogue scores — the album curve's catalogue cap and the
    # artist-eligibility gate both read this, so it must be the WHOLE artist
    # (every loaded album), not just the album being rated. Building it from
    # the rows already in hand avoids a second query per album.
    artist_score_map: dict[str, list[float]] = defaultdict(list)
    for (group_artist, _group_album), rows in groups.items():
        for r in rows:
            score_value = float(r.get("popularity_score") or r.get("final_score") or 0)
            if score_value > 0:
                artist_score_map[group_artist].append(score_value)

    options: dict[str, Any] = {
        "write_file_tags": bool(write_file_tags),
        "sync_navidrome": bool(sync_navidrome),
        # A resync has no scan loop to post per-album, so nothing is deferred.
        "_per_album_posted": True,
        "_resync": True,
    }
    if dry_run:
        # Suppress the Navidrome client build entirely.
        options["sync_navidrome"] = False

    started = time.monotonic()
    albums_done = 0
    tracks_total = 0
    tracks_changed = 0
    changed_rows: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    logger.info(
        "Star resync started",
        albums=total_albums, dry_run=dry_run,
        write_file_tags=bool(write_file_tags), sync_navidrome=bool(sync_navidrome),
        artist=artist, album=album,
    )

    for (group_artist, group_album), album_results in groups.items():
        albums_done += 1
        if progress is not None:
            try:
                progress(albums_done, total_albums, f"{group_artist} — {group_album}")
            except Exception:
                pass
        try:
            _artist_pool = artist_score_map.get(group_artist, [])
            if dry_run:
                # Rate a copy so no mutation reaches the caller's dicts.
                import copy
                working = [copy.copy(r) for r in album_results]
                rows, summary = _compute_stars_for_album(
                    working, options=options, artist_scores=_artist_pool,
                )
            else:
                rows, summary = _compute_stars_for_album(
                    album_results, options=options, artist_scores=_artist_pool,
                )
        except Exception as exc:
            logger.error(
                "Star resync failed for album",
                artist=group_artist, album=group_album, error=str(exc),
            )
            failed.append({"artist": group_artist, "album": group_album, "error": str(exc)})
            continue

        tracks_total += summary["tracks"]
        tracks_changed += summary["changed"]
        for row in rows:
            if row["before"] != row["after"]:
                row["artist"] = group_artist
                row["album"] = group_album
                changed_rows.append(row)

    elapsed = round(time.monotonic() - started, 2)
    logger.info(
        "Star resync finished",
        albums=albums_done, tracks=tracks_total, changed=tracks_changed,
        failed=len(failed), seconds=elapsed, dry_run=dry_run,
    )

    return {
        "ok": not failed,
        "dry_run": dry_run,
        "albums": albums_done,
        "tracks": tracks_total,
        "changed": tracks_changed,
        "changed_tracks": changed_rows,
        "failed": failed,
        "seconds": elapsed,
        "write_file_tags": bool(write_file_tags),
        "sync_navidrome": bool(sync_navidrome),
    }


def preview_star_ratings(
    *,
    artist: str | None = None,
    album: str | None = None,
    limit_albums: int | None = None,
) -> dict[str, Any]:
    """Convenience wrapper: a dry-run that writes nothing."""
    return resync_star_ratings(
        artist=artist, album=album, limit_albums=limit_albums, dry_run=True,
    )
