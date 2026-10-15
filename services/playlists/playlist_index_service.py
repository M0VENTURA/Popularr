"""Playlist index sync, and the artist page's playlist-membership query.

Why this module exists
----------------------
The artist page gained an "Included in Playlists" section. Nothing in the app
could answer "which playlists contain this artist's tracks?" — playlists were
never stored in Postgres, only in Navidrome (Subsonic) and in local
``.nsp``/``.m3u`` files, and Subsonic has no "which playlists contain track X"
call. Reading every playlist live would cost one HTTP request per playlist on
every artist page load.

So the mapping is SNAPSHOTTED into ``playlists`` + ``playlist_entries``
(migration 025) during the playlist/library sync, and the page reads it as a
plain indexed join.

What the sync costs
-------------------
Navidrome reports a ``changed`` stamp per playlist. The index stores it, and a
playlist whose stamp has not moved is skipped WITHOUT a request — so a steady
state costs one ``fetch_all_playlists`` call and nothing else. File-backed
playlists have no such stamp, so a size+mtime digest stands in for one.

⚠️ Clients are INJECTED, not resolved here.
The playlist-facing client factory is server-scoped (one client per Navidrome
SERVER, not per account) and lives in
``services.popularity.stages.finalise_stage._navidrome_clients`` because that is
where playlist syncing lives. Importing that module from here would drag a
5000-line popularity stage into every artist-page request, and resolving clients
in two places is exactly how the duplicate-playlist ping-pong started. The
caller passes the list; tests pass fakes.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from helpers.logging_config import log_unified
from db.repositories import playlists as _repo

_SOURCE_FILE = "file"
_SOURCE_NAVIDROME = "navidrome"


def _logger():
    """Module logger, resolved lazily (structlog is not always configured)."""
    import logging

    return logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stamps
# ---------------------------------------------------------------------------

def _file_stamp(file_path: str) -> str:
    """A change stamp for a file-backed playlist.

    ``.nsp``/``.m3u`` files carry no modification stamp of their own, so this
    digests the two cheap signals that change when the file does: its size and
    its mtime. Reading and hashing the CONTENT would be more precise but would
    mean doing the expensive half of the work to decide whether to do it.

    A missing file stamps as ``""``, which never equals a previously stored
    stamp — so a deleted-then-recreated playlist is re-read rather than
    assumed unchanged.
    """
    try:
        stat = os.stat(file_path)
    except OSError:
        return ""
    return hashlib.sha1(f"{stat.st_size}:{int(stat.st_mtime)}".encode()).hexdigest()[:16]


def _navidrome_stamp(playlist: dict[str, Any]) -> str:
    """Navidrome's own modification stamp, or a fallback when it has none.

    ⚠️ The fallback is NOT ``""``. A playlist the server reports without a
    ``changed`` value would otherwise compare equal to the stored ``""`` of a
    playlist that has never been stamped, and be skipped forever. Falling back
    to the song count means it is re-read whenever the size changes, which is
    the best signal available in that case.
    """
    changed = str(playlist.get("changed") or "").strip()
    if changed:
        return changed
    return f"count:{int(playlist.get('songCount') or 0)}"


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def _enumerate(clients: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """Every playlist the app can see, file-backed ones first.

    Mirrors ``routes/playlists.py::api_playlists_all``'s merge rule: an ``.nsp``
    file WINS over a same-named Navidrome entry, so the index must make the same
    choice or the artist page would list a playlist the Playlists page does not
    show (and vice versa).

    Returns ``(entries, navidrome_error_count)``.
    """
    from services.playlists.playlist_service import list_nsp_playlists

    entries: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for item in list_nsp_playlists():
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        file_name = str(item.get("file_name") or "")
        seen_names.add(name.casefold())
        entries.append({
            "playlist_id": f"file::{file_name}",
            "source": _SOURCE_FILE,
            "name": name,
            "playlist_type": "regular" if item.get("kind") == "m3u" else "smart",
            "owner": None,
            "is_public": None,
            "song_count": int(item.get("track_count") or 0),
            "comment": str(item.get("comment") or ""),
            "changed": _file_stamp(str(item.get("file_path") or "")),
            "file_path": str(item.get("file_path") or ""),
            "kind": str(item.get("kind") or "nsp"),
            "rule_based": bool(item.get("rule_based")),
        })

    errors = 0
    for client in clients or []:
        try:
            remote = client.fetch_all_playlists() or []
        except Exception as exc:
            # Counted, not raised: one unreachable server must not abort the
            # sync for the others (nor cause the prune below to delete what it
            # could not enumerate).
            errors += 1
            _logger().warning("Playlist index: could not list Navidrome playlists: %s", exc)
            continue

        for nav in remote:
            if not isinstance(nav, dict):
                continue
            name = str(nav.get("name") or "").strip()
            playlist_id = str(nav.get("id") or "").strip()
            if not name or not playlist_id:
                continue
            if name.casefold() in seen_names:
                continue  # already provided by the file-backed copy
            nav_type = nav.get("type")
            entries.append({
                "playlist_id": playlist_id,
                "source": _SOURCE_NAVIDROME,
                "name": name,
                "playlist_type": nav_type if nav_type in ("smart", "regular") else "regular",
                "owner": str(nav.get("owner") or "") or None,
                "is_public": nav.get("public") if isinstance(nav.get("public"), bool) else None,
                "song_count": int(nav.get("songCount") or 0) or len(nav.get("entry") or []),
                "comment": str(nav.get("comment") or ""),
                "changed": _navidrome_stamp(nav),
                "file_path": "",
                "kind": "navidrome",
                "rule_based": bool(nav.get("criteria")),
            })

    return entries, errors


# ---------------------------------------------------------------------------
# Track resolution
# ---------------------------------------------------------------------------

def _ids_from_paths(paths: list[str]) -> list[str]:
    """Map .m3u file paths onto library track ids.

    An ``.m3u`` is a list of PATHS, while the index keys on track ids, so the
    two have to be bridged. Exact ``file_path`` match only: a fuzzier rule
    (basename, normalised separators) would be guessing, and a wrong bridge puts
    a track in a playlist it is not in — which is worse than the track being
    absent.

    Unmatched paths are simply absent from the result; the caller reports how
    many were dropped rather than hiding them.
    """
    wanted = [str(p or "").strip() for p in paths if str(p or "").strip()]
    if not wanted:
        return []

    from sqlalchemy import bindparam, text

    from db.engine import db_session

    try:
        with db_session() as session:
            rows = session.execute(
                text("SELECT id FROM tracks WHERE file_path IN :paths")
                .bindparams(bindparam("paths", expanding=True)),
                {"paths": wanted},
            ).fetchall() or []
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception as exc:
        _logger().debug("Playlist index: path->id resolution failed: %s", exc)
        return []


def _track_ids_for(entry: dict[str, Any], clients: list[Any]) -> tuple[list[str], int]:
    """Resolve one playlist's entries to track ids.

    Returns ``(ids, unresolved)``. ``unresolved`` counts entries that were in the
    playlist but could not be mapped to a library track — reported by the caller
    rather than silently dropped, because "this playlist has 40 tracks but 0
    appear on the artist page" is otherwise indistinguishable from a broken
    query.
    """
    if entry.get("source") == _SOURCE_NAVIDROME:
        for client in clients or []:
            try:
                detail = client.fetch_playlist(entry["playlist_id"]) or {}
            except Exception as exc:
                _logger().warning(
                    "Playlist index: could not load playlist %s: %s",
                    entry.get("playlist_id"), exc,
                )
                continue
            ids: list[str] = []
            for track in detail.get("tracks") or []:
                if isinstance(track, dict):
                    song_id = str(track.get("id") or "").strip()
                    if song_id:
                        ids.append(song_id)
            return ids, max(0, int(entry.get("song_count") or 0) - len(ids))
        return [], int(entry.get("song_count") or 0)

    # ── File-backed ────────────────────────────────────────────────────────
    file_path = str(entry.get("file_path") or "")
    if entry.get("kind") == "m3u":
        from services.playlists.playlist_service import read_m3u_file

        rows = read_m3u_file(file_path) or []
        ids = _ids_from_paths([r.get("file_path") for r in rows if isinstance(r, dict)])
        return ids, max(0, len(rows) - len(ids))

    from services.playlists.playlist_service import read_nsp_playlist

    playlist = read_nsp_playlist(file_path) or {}
    tracks = playlist.get("_tracks") or []
    ids = [str(t.get("id") or "").strip() for t in tracks if isinstance(t, dict)]
    ids = [i for i in ids if i]

    if not ids:
        # Rule-based or trackIds-only .nsp: the file has no embedded list, so
        # the entries only exist behind Navidrome (the same fallback
        # `_fetch_playlist_tracks` uses for this exact case).
        for client in clients or []:
            try:
                found = client.find_playlist_by_name(str(entry.get("name") or ""))
                if not found:
                    continue
                detail = client.fetch_playlist(found.get("id")) or {}
                for track in detail.get("tracks") or []:
                    if isinstance(track, dict):
                        song_id = str(track.get("id") or "").strip()
                        if song_id:
                            ids.append(song_id)
                if ids:
                    break
            except Exception as exc:
                _logger().debug(
                    "Playlist index: rule-based lookup failed for %s: %s",
                    entry.get("name"), exc,
                )

    # `read_nsp_playlist` synthesises `<file>#<n>` for an entry with no id.
    # Those can never join to a track, so they are counted as unresolved rather
    # than stored as rows that will never match.
    real = [i for i in ids if "#" not in i]
    return real, max(0, len(ids) - len(real))


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------

def sync_playlist_index(clients: list[Any] | None = None) -> dict[str, int]:
    """Bring the playlist index up to date. Safe to call repeatedly.

    Skips every playlist whose stamp has not moved, so a steady state makes one
    ``fetch_all_playlists`` call per server and no writes.

    ⚠️ NULL IS NOT A DECISION. The prune at the end only runs when EVERY
    configured server was enumerable. A Navidrome that is down or misconfigured
    returns no playlists, and pruning on that would wipe the index — turning a
    transient outage into a permanently empty artist-page section. An
    unenumerable source is a SKIP, never an empty result.
    """
    stats = {
        "playlists": 0,
        "changed": 0,
        "entries": 0,
        "skipped": 0,
        "pruned": 0,
        "unresolved": 0,
        "errors": 0,
    }
    clients = list(clients or [])

    try:
        entries, errors = _enumerate(clients)
    except Exception as exc:
        _logger().warning("Playlist index: enumeration failed: %s", exc)
        stats["errors"] = 1
        return stats

    stats["errors"] = errors
    stats["playlists"] = len(entries)

    try:
        stored = _repo.stored_changed_map()
    except Exception as exc:
        _logger().warning("Playlist index: could not read stored stamps: %s", exc)
        stored = {}

    seen_ids: list[str] = []
    for entry in entries:
        playlist_id = str(entry.get("playlist_id") or "")
        if not playlist_id:
            continue
        seen_ids.append(playlist_id)

        if stored.get(playlist_id) == str(entry.get("changed") or ""):
            stats["skipped"] += 1
            continue

        track_ids, unresolved = _track_ids_for(entry, clients)
        stats["unresolved"] += unresolved
        try:
            written = _repo.replace_playlist(
                playlist_id=playlist_id,
                name=str(entry.get("name") or playlist_id),
                source=str(entry.get("source") or ""),
                playlist_type=entry.get("playlist_type"),
                owner=entry.get("owner"),
                is_public=entry.get("is_public"),
                song_count=int(entry.get("song_count") or 0),
                comment=str(entry.get("comment") or ""),
                changed=str(entry.get("changed") or ""),
                track_ids=track_ids,
            )
        except Exception as exc:
            _logger().warning(
                "Playlist index: could not store playlist %s: %s", entry.get("name"), exc
            )
            stats["errors"] += 1
            continue

        stats["changed"] += 1
        stats["entries"] += int(written.get("entries") or 0)

    # Prune only when the enumeration can be trusted (see the docstring).
    if errors == 0:
        try:
            stats["pruned"] = _repo.delete_playlists_not_in(seen_ids)
        except Exception as exc:
            _logger().warning("Playlist index: prune failed: %s", exc)
            stats["errors"] += 1
    elif stored:
        _logger().warning(
            "Playlist index: prune SKIPPED — not every server could be listed, "
            "and pruning on a partial view would delete playlists that still exist"
        )

    if stats["changed"] or stats["pruned"]:
        log_unified(
            f"🎧 Playlists: indexed {stats['playlists']} playlist(s) "
            f"({stats['changed']} updated, {stats['entries']} entries, "
            f"{stats['pruned']} removed)"
        )
    if stats["unresolved"]:
        # Never silent: an entry that resolves to no library track is either a
        # stale id, a file Navidrome has not indexed yet, or a path that did not
        # match — all things the operator can act on.
        _logger().info(
            "Playlist index: %s entr(ies) did not resolve to a library track",
            stats["unresolved"],
        )

    return stats


# ---------------------------------------------------------------------------
# The artist page's query
# ---------------------------------------------------------------------------

def playlists_for_artist(
    artist: str,
    *,
    max_tracks: int = 200,
) -> list[dict[str, Any]]:
    """Playlists containing any of ``artist``'s tracks, grouped by playlist.

    Returns ``[{playlist_id, name, type, owner, is_public, song_count,
    source, comment, tracks: [{title, album, artist, position}]}]``, ordered by
    playlist name then position.

    ⚠️ The artist match is ``COALESCE(NULLIF(album_artist, ''), artist)``,
    case-insensitively — the SAME expression the artist page uses to find the
    artist's albums. Matching on the bare ``artist`` column instead would list
    the playlists a guest credit sits in under the HOST artist's page, and miss
    them on the artist's own.
    """
    name = str(artist or "").strip()
    if not name:
        return []

    try:
        ids = _artist_track_ids(name, max_tracks=max_tracks)
    except Exception as exc:
        _logger().debug("Playlist membership: track id lookup failed: %s", exc)
        return []
    if not ids:
        return []

    try:
        rows = _repo.artist_playlist_tracks(ids)
    except Exception as exc:
        _logger().warning("Playlist membership: index query failed: %s", exc)
        return []

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        playlist_id = str(row.get("playlist_id") or "")
        entry = grouped.get(playlist_id)
        if entry is None:
            entry = {
                "playlist_id": playlist_id,
                "name": str(row.get("name") or ""),
                "type": row.get("type"),
                "owner": row.get("owner"),
                "is_public": row.get("is_public"),
                "song_count": int(row.get("song_count") or 0),
                "source": row.get("source"),
                "comment": str(row.get("comment") or ""),
                "tracks": [],
            }
            grouped[playlist_id] = entry
        entry["tracks"].append({
            "title": str(row.get("title") or ""),
            "album": str(row.get("album") or ""),
            "artist": str(row.get("artist") or ""),
            "position": int(row.get("position") or 0),
        })

    return list(grouped.values())


def _artist_track_ids(artist: str, *, max_tracks: int) -> list[str]:
    """The library track ids credited to ``artist`` (album-artist keyed)."""
    from sqlalchemy import text

    from db.engine import db_session

    with db_session() as session:
        rows = session.execute(
            text(
                "SELECT id FROM tracks "
                "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
                "LIMIT :limit"
            ),
            {"artist": artist, "limit": int(max_tracks)},
        ).fetchall() or []
    return [str(r[0]) for r in rows if r and r[0]]
