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

⭐ AND IT REBUILDS THE GENERATED PLAYLISTS.

A rating change is only half of "apply the new scoring". Every generated
playlist (each artist's Essential Collection, ``{Genre} - Top Tracks``, New
Music, the curated lists) selects on stars and orders on the score, so a resync
that rewrites stars but leaves the playlists untouched leaves them showing the
OLD ordering — which is exactly the reported *"the essential playlists aren't
being created with the new ordering of 5★ followed by the new score"*. A scan
gets the rebuild for free (``finalise_scan`` → ``_refresh_all_essential_
collections`` + ``_run_global_playlist_rebuild``); the resync has no finalise
stage, so it must ask for one explicitly. It does so only when at least one
star actually changed, and never on a dry run.

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
from services.popularity.album_identity import album_identity

logger = structlog.get_logger(__name__)

__all__ = [
    "resync_star_ratings",
    "preview_star_ratings",
    "start_star_resync",
    "is_resync_running",
    "SCAN_TYPE",
    "PROGRESS_FILE",
]

#: The ``scan_states`` row the BACKGROUND apply writes.
#:
#: Kept apart from real scan types so it can never be mistaken for a scan,
#: resumed, or picked up by the scan scheduler. Listed in
#: ``progress_service.SCAN_TYPES`` so the dashboard poller surfaces it — that is
#: the whole point of publishing here rather than streaming from the response.
SCAN_TYPE = "star_resync"
PROGRESS_FILE = f"{SCAN_TYPE}_progress.json"

#: Phrases rendered in the dashboard's one-line status bar.
PHASE_RATING = "Re-rating tracks"
PHASE_PLAYLISTS = "Rebuilding playlists"
PHASE_DONE = "Complete"

#: A progress row older than this is a DEAD job, not a running one.
#:
#: The apply runs on a daemon thread, so a restart (or a killed worker) leaves
#: its last row saying ``is_running: true`` forever — and because that row is the
#: only "is running" signal, every retry would answer 409 even though nothing was
#: running. The live job republishes per album chunk, so a quarter of an hour of
#: silence means the writer is gone.
_PROGRESS_STALE_SECONDS = 15 * 60

_APPLY_LOCK = __import__("threading").Lock()
_APPLY_RUNNING = False


# ---------------------------------------------------------------------------
# Progress plumbing (mirrors genre_playlist_resync_service)
# ---------------------------------------------------------------------------

def _write_progress(
    *,
    running: bool,
    percent: float,
    phase: str,
    processed: int = 0,
    total: int = 0,
    detail: str = "",
    status: str | None = None,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    """Publish one progress frame. Never fatal — a progress write must not
    abort the job it is describing."""
    try:
        from services.scanning.scan_state import write_progress_with_current_artist

        extra: dict[str, Any] = {
            "status": status or ("running" if running else "complete"),
            "mode": "star_resync",
            "percent_complete": max(0.0, min(100.0, float(percent))),
            "processed_items": int(processed),
            "total_items": int(total),
            "processed_artists": int(processed),
            "total_artists": int(total),
            "current_stage": phase,
            "current_item": detail,
        }
        if result is not None:
            extra["resync_result"] = result
        if error:
            extra["error"] = error
        write_progress_with_current_artist(
            PROGRESS_FILE, SCAN_TYPE, running, extra=extra
        )
    except Exception as exc:
        logger.debug("Star resync progress write failed", error=str(exc))


def _progress_row_is_stale(
    state: dict[str, Any], stale_after: float = _PROGRESS_STALE_SECONDS
) -> bool:
    """True when a row claims to be running but has not been touched recently."""
    if not state.get("is_running"):
        return False
    raw = str(state.get("last_updated") or "").strip()
    if not raw:
        return False
    try:
        from datetime import datetime

        updated = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    now = datetime.now(updated.tzinfo) if updated.tzinfo else datetime.now()
    return (now - updated).total_seconds() > stale_after


def is_resync_running() -> bool:
    """True while the background apply holds the progress row.

    A stale row (last frame longer ago than ``_PROGRESS_STALE_SECONDS``) is a
    dead job, so the caller may start a new one instead of waiting forever.
    """
    if _APPLY_RUNNING:
        return True
    try:
        from services.scanning.scan_state import read_progress_file

        state = read_progress_file(PROGRESS_FILE) or {}
        return bool(state.get("is_running")) and not _progress_row_is_stale(state)
    except Exception:
        return bool(_APPLY_RUNNING)


def _claim_apply() -> bool:
    global _APPLY_RUNNING
    with _APPLY_LOCK:
        if _APPLY_RUNNING:
            return False
        _APPLY_RUNNING = True
        return True


def _release_apply() -> None:
    global _APPLY_RUNNING
    with _APPLY_LOCK:
        _APPLY_RUNNING = False

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
    # ⭐ The album identity key. Without this the resync fell back to the
    # (album_artist, album) name strings for every row and split one
    # MusicBrainz release into two albums whenever its files disagreed about
    # the title — the same split Navidrome does NOT make.
    "musicbrainz_albumid",
    "musicbrainz_album_mbid",
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
        # Needed by album_identity: a release's MBID, not its title, is what
        # decides one album.
        "musicbrainz_albumid": str(_get("musicbrainz_albumid") or ""),
        "musicbrainz_album_mbid": str(_get("musicbrainz_album_mbid") or ""),
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

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    with db_session() as session:
        rows = session.execute(text(sql), params).fetchall() or []
    for row in rows:
        track = _row_to_track(row)
        # ⭐ By album IDENTITY. Two files of one release can disagree about the
        # album title ("Here to Stay" vs "Here to Stay (T Ray's mix)") while
        # carrying the same musicbrainz_albumid; grouping on the name split
        # them, leaving the re-release a one-track album whose album-relative
        # star rating could not mean anything.
        groups[album_identity(track)].append(track)

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

    # ⭐ WHY, not just WHAT. The rules record their verdict on the track
    # (``_force_floor``, ``_album_curve_stars``, ``_era_5star``, …) and until now
    # only the log read them. A preview that says "4 → 3" without saying which
    # cap did it makes the ten overlapping 5★ paths impossible to audit.
    from services.popularity.stages.finalise_stage import star_rating_reason

    rows = [
        {
            "track_id": str(r.get("track_id") or ""),
            "title": str(r.get("title") or ""),
            "before": before.get(str(r.get("track_id") or ""), 0),
            "after": after.get(str(r.get("track_id") or ""), 0),
            "lastfm_listeners": int(r.get("lastfm_listeners") or 0),
            "reason": star_rating_reason(r),
        }
        for r in album_results
    ]
    return rows, summary


def _refresh_generated_playlists(artist: str | None = None) -> dict[str, Any]:
    """Rebuild the generated playlists a rating change just invalidated.

    WHY THIS IS NEEDED. Every generated playlist SELECTS on stars and ORDERS on
    the score, so changing star ratings without rebuilding them leaves the
    playlists on the old ordering. A scan does this in ``finalise_scan``; the
    resync has no finalise stage, so it performs the same two steps here:

    * the per-artist best-of lists (Essential Collections) — only the resynced
      artist when the resync was artist-scoped, otherwise every artist;
    * the library-wide lists (``{Genre} - Top Tracks``, New Music, curated
      playlists, favourites, playlist index) via ``_run_global_playlist_rebuild``.

    Each callee applies its own enable flag, so nothing is written that the
    configuration does not ask for. Never fatal: a playlist failure must not
    discard a rating pass that already succeeded.
    """
    stats: dict[str, Any] = {"essential": 0, "global": False}

    try:
        from services.popularity.stages import finalise_stage as fs
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Playlist refresh helpers unavailable", error=str(exc))
        return stats

    # The genre pool cache is keyed on a TTL and would otherwise hand the
    # rebuild the star values this resync just replaced.
    try:
        fs.invalidate_genre_playlist_rows()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Genre pool cache invalidation failed", error=str(exc))

    try:
        if not fs._essential_playlists_enabled({}):
            pass
        elif artist:
            if fs._sync_essential_playlist(str(artist)):
                stats["essential"] = 1
        else:
            stats["essential"] = int(fs._refresh_all_essential_collections() or 0)
    except Exception as exc:
        logger.warning("Essential playlist refresh failed", artist=artist, error=str(exc))

    try:
        fs._run_global_playlist_rebuild({}, force=True)
        stats["global"] = True
    except Exception as exc:
        logger.warning("Global playlist rebuild failed", error=str(exc))

    return stats


def _apply_velocity_adjustments(
    groups: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Re-weight each row's score by age-adjusted velocity + collision damping.

    ⭐ WHY THIS RUNS HERE, ON THE WHOLE CATALOGUE.

    ``calculate_combined_popularity_score`` only ever sees ONE track's counts, so
    it cannot answer either question: velocity is only meaningful relative to the
    artist's fastest track, and a ListenBrainz collision is only visible by
    comparing two DIFFERENT songs' ``(listens, score)`` pairs. Both need the
    artist's entire catalogue, which is exactly what the resync has loaded.

    ``effective_year`` is the EARLIEST year any non-compilation, non-holiday
    album of that artist gives the song — so a greatest-hits reissue cannot make
    a 1994 single look new and hand it a velocity its recording never earned.

    Returns a small report so a preview can say what changed. A no-op when
    ``popularity.velocity`` is off, which is the default: an existing
    installation's ratings stay exactly as they were until it is switched on.
    """
    from services.popularity.popularity_config import get_velocity_scoring_config
    from services.popularity.popularity_math import (
        age_adjusted_velocity,
        collision_damping,
        find_collision_keys,
        normalise_velocity,
    )

    cfg = get_velocity_scoring_config()
    # ``enabled`` gates the whole pass; ``weight`` only decides how much of the
    # score the velocity term carries. Collision dampening is useful on its own
    # (weight 0), so it must not be switched off by a zero velocity weight.
    if not cfg.get("enabled"):
        return {"enabled": False, "artists": 0, "tracks": 0, "collisions": 0}

    weight = float(cfg["weight"])
    min_listens = int(cfg["collision_min_listens"])
    damping = float(cfg["collision_damping"])
    current_year = time.localtime().tm_year

    # One pass to build each artist's catalogue, keyed the same way the rating
    # path keys an artist (album_artist when present).
    by_artist: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rows in groups.values():
        for row in rows:
            # Keyed the same way the rating path keys an artist.
            by_artist[
                str(row.get("album_artist") or row.get("artist") or "").strip()
            ].append(row)

    tracks = 0
    collisions = 0
    for artist_rows in by_artist.values():
        # ── effective year: the EARLIEST studio release of the song ──────────
        earliest: dict[str, int] = {}
        for row in artist_rows:
            title_key = str(row.get("title") or "").strip().casefold()
            if not title_key:
                continue
            try:
                year = int(row.get("release_year") or row.get("year") or 0)
            except (TypeError, ValueError):
                year = 0
            if year <= 0 or year > current_year + 1:
                continue
            # A compilation must not define the song's age.
            if int(row.get("is_compilation") or 0):
                continue
            if title_key not in earliest or year < earliest[title_key]:
                earliest[title_key] = year

        # ── velocity, normalised against this artist's fastest track ─────────
        velocities: dict[int, float] = {}
        for index, row in enumerate(artist_rows):
            title_key = str(row.get("title") or "").strip().casefold()
            effective_year = earliest.get(title_key) or row.get("year")
            velocities[index] = age_adjusted_velocity(
                row.get("listenbrainz_listens"),
                effective_year,
                current_year=current_year,
            )
        max_velocity = max(velocities.values(), default=0.0)

        # ── collision pairs shared by two DIFFERENT songs ────────────────────
        collision_keys = find_collision_keys(artist_rows, min_listens=min_listens)

        for index, row in enumerate(artist_rows):
            base = float(
                row.get("popularity_score") or row.get("final_score") or 0
            )
            if base <= 0:
                continue
            vel_norm = normalise_velocity(velocities[index], max_velocity)
            adjusted = (1.0 - weight) * base + weight * vel_norm
            multiplier = collision_damping(
                row.get("listenbrainz_listens"),
                row.get("listenbrainz_score"),
                collision_keys,
                damping=damping,
            )
            if multiplier < 1.0:
                collisions += 1
            adjusted *= multiplier
            adjusted = max(0.0, adjusted)
            # Both keys: the rating path reads ``popularity_score`` first and
            # falls back to ``final_score``, and ``_load_album_groups`` fills
            # them from the same stored columns.
            row["popularity_score"] = adjusted
            row["final_score"] = adjusted
            tracks += 1

    return {
        "enabled": True,
        "artists": len(by_artist),
        "tracks": tracks,
        "collisions": collisions,
        "weight": weight,
    }


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

    # ⭐ AGE-ADJUSTED VELOCITY + COLLISION DAMPENING, applied to the loaded rows
    # BEFORE anything is rated. This is the only place the artist's whole
    # catalogue is in memory at once, and both terms need that breadth.
    velocity_report = _apply_velocity_adjustments(groups)

    # Artist catalogue scores — the album curve's catalogue cap and the
    # artist-eligibility gate both read this, so it must be the WHOLE artist
    # (every loaded album), not just the album being rated. Building it from
    # the rows already in hand avoids a second query per album.
    artist_score_map: dict[str, list[float]] = defaultdict(list)
    for rows in groups.values():
        for r in rows:
            score_value = float(r.get("popularity_score") or r.get("final_score") or 0)
            if score_value > 0:
                artist_score_map[
                    str(r.get("album_artist") or r.get("artist") or "").strip()
                ].append(score_value)

    options: dict[str, Any] = {
        "write_file_tags": bool(write_file_tags),
        "sync_navidrome": bool(sync_navidrome),
        # A resync has no scan loop to post per-album, so nothing is deferred.
        "_per_album_posted": True,
        "_resync": True,
        # Makes ``post_album_star_ratings`` skip ALL database writes, the
        # favourite floor and the reload. Rating a copy is not enough on its
        # own — the copy keeps the same track ids, so the shared path would
        # still have persisted the new stars.
        "_dry_run": bool(dry_run),
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
    # ⭐ LIBRARY STAR HISTOGRAM — the fastest way to see whether a change made
    # the library too harsh or too generous. Counted over EVERY rated track, not
    # just the changed ones, so it describes the resulting library rather than
    # the diff. Albums that failed still count toward ``errors``.
    star_histogram_after: dict[int, int] = {n: 0 for n in range(1, 6)}
    star_histogram_before: dict[int, int] = {n: 0 for n in range(1, 6)}
    five_star_albums: list[dict[str, Any]] = []

    logger.info(
        "Star resync started",
        albums=total_albums, dry_run=dry_run,
        write_file_tags=bool(write_file_tags), sync_navidrome=bool(sync_navidrome),
        artist=artist, album=album,
    )

    for album_results in groups.values():
        # The display artist/album come from the rows: the grouping key is the
        # release IDENTITY (which can be ("mbid", id) or ("name", artist, album)),
        # not a fixed pair of strings.
        group_artist = str(
            album_results[0].get("album_artist") or album_results[0].get("artist") or ""
        ).strip()
        group_album = str(album_results[0].get("album") or "").strip()
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
        _album_five = 0
        for row in rows:
            _before_stars = int(row.get("before") or 0)
            _after_stars = int(row.get("after") or 0)
            if 1 <= _before_stars <= 5:
                star_histogram_before[_before_stars] += 1
            if 1 <= _after_stars <= 5:
                star_histogram_after[_after_stars] += 1
            if _after_stars == 5:
                _album_five += 1
            if row["before"] != row["after"]:
                row["artist"] = group_artist
                row["album"] = group_album
                changed_rows.append(row)
        if _album_five:
            five_star_albums.append({
                "artist": group_artist,
                "album": group_album,
                "five_star_tracks": _album_five,
                "tracks": len(rows),
            })

    # Sorted most-5★-first, so "which albums carry the top end" is the first
    # thing a preview answers.
    five_star_albums.sort(key=lambda a: (-a["five_star_tracks"], a["artist"], a["album"]))

    elapsed = round(time.monotonic() - started, 2)
    logger.info(
        "Star resync finished",
        albums=albums_done, tracks=tracks_total, changed=tracks_changed,
        failed=len(failed), seconds=elapsed, dry_run=dry_run,
    )

    # ⭐ Playlists reuse the stars, so a pass that changed any of them must
    # rebuild the playlists or they keep the OLD ordering. Skipped on a dry run
    # and when nothing changed (a rebuild that cannot differ is pure cost).
    playlists: dict[str, Any] = {}
    if not dry_run and tracks_changed:
        if progress is not None:
            _write_progress(
                running=True, percent=95.0, phase=PHASE_PLAYLISTS,
                processed=albums_done, total=total_albums,
                detail="Rebuilding playlists…", status="running",
            )
        playlists = _refresh_generated_playlists(artist)
        logger.info(
            "Generated playlists refreshed after star resync",
            essential=playlists.get("essential"),
            global_rebuild=playlists.get("global"),
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
        "playlists": playlists,
        "velocity": velocity_report,
        # ⭐ Library shape after the run, plus which albums carry the 5★ top end.
        # A change that makes the library too harsh or too generous shows up here
        # immediately instead of after a scan.
        "star_histogram": {
            "before": star_histogram_before,
            "after": star_histogram_after,
            "total": sum(star_histogram_after.values()),
        },
        "five_star_albums": five_star_albums[:50],
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


# ---------------------------------------------------------------------------
# Public: apply (background)
# ---------------------------------------------------------------------------

def start_star_resync(
    *,
    artist: str | None = None,
    album: str | None = None,
) -> dict[str, Any]:
    """Start the library-wide re-rate on a BACKGROUND thread. Returns at once.

    WHY THIS EXISTS
    ---------------
    The rating pass was a synchronous request: ``/api/ratings/resync`` awaited
    ``resync_star_ratings`` to completion. On a real library that outlives the
    reverse proxy's ~100s origin timeout — the proxy answers with its OWN HTML
    error page and the card's ``resp.json()`` dies on ``Unexpected token '<'``,
    the same failure the genre→playlist resync already fixed. The work completed;
    only the response was lost, and there was no progress to show meanwhile.

    So the apply moves to a daemon thread and publishes progress to the shared
    ``scan_states`` row (``star_resync``), which the dashboard poller already
    renders once the type is listed in ``progress_service.SCAN_TYPES``.

    Returns ``{"ok": True, "started": bool}`` — ``started=False`` (not an error)
    when a run is already in flight.
    """
    from helpers.config_helpers import get_star_resync_config

    if not _claim_apply():
        return {"ok": True, "started": False, "already_running": True}

    cfg = get_star_resync_config()
    if not cfg.get("enabled"):
        _release_apply()
        return {
            "ok": False,
            "error": "Star resync is disabled in configuration.",
        }

    # ``albums_per_batch`` is the PROGRESS-REPORT cadence, not a load chunk.
    #
    # ⚠️ The whole catalogue must be loaded for velocity: ``_apply_velocity_
    # adjustments`` normalises each track against its artist's FASTEST track and
    # detects ListenBrainz collisions across the catalogue, so splitting the load
    # into batches would silently change the ratings. Batching the progress
    # writes keeps `albums_per_batch` meaningful without touching that maths.
    try:
        batch = max(1, int(cfg.get("albums_per_batch") or 25))
    except (TypeError, ValueError):
        batch = 25

    _write_progress(
        running=True, percent=1.0, phase=PHASE_RATING,
        detail="Loading scored tracks…", status="running",
    )

    def _run() -> None:
        try:
            def _on_progress(done: int, total: int, label: str) -> None:
                # Throttled: one DB write per ``batch`` albums, plus the first
                # and the last, so a 5,000-album library is not 5,000 writes.
                if done != 1 and done % batch != 0 and done != total:
                    return
                frac = (done / total) if total else 0.0
                _write_progress(
                    running=True, percent=2.0 + 90.0 * min(1.0, frac),
                    phase=PHASE_RATING, processed=done, total=total,
                    detail=label, status="running",
                )

            result = resync_star_ratings(
                artist=artist, album=album, progress=_on_progress,
            )

            if not result.get("ok"):
                _write_progress(
                    running=False, percent=0.0, phase="Failed", status="failed",
                    error=(
                        result.get("error")
                        or f"{len(result.get('failed') or [])} album(s) failed"
                    ),
                    result={
                        "albums": result.get("albums", 0),
                        "tracks": result.get("tracks", 0),
                        "changed": result.get("changed", 0),
                        "seconds": result.get("seconds", 0),
                        "velocity": result.get("velocity", {}),
                    },
                )
                return

            _write_progress(
                running=False, percent=100.0, phase=PHASE_DONE,
                processed=result.get("albums", 0), total=result.get("albums", 0),
                detail=(
                    f"{result.get('changed', 0)} rating(s) changed across "
                    f"{result.get('tracks', 0)} track(s)"
                ),
                status="complete",
                result={
                    "albums": result.get("albums", 0),
                    "tracks": result.get("tracks", 0),
                    "changed": result.get("changed", 0),
                    "seconds": result.get("seconds", 0),
                    "velocity": result.get("velocity", {}),
                    "playlists": result.get("playlists", {}),
                },
            )
        except Exception as exc:
            logger.error("Background star resync failed", error=str(exc), exc_info=True)
            _write_progress(
                running=False, percent=0.0, phase="Failed", status="failed",
                error=str(exc),
            )
        finally:
            _release_apply()

    import threading

    threading.Thread(target=_run, name="star-resync", daemon=True).start()
    return {"ok": True, "started": True}
