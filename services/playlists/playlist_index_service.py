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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

def _enumerate(clients: list[Any]) -> tuple[list[dict[str, Any]], int, list[list[dict[str, Any]]]]:
    """Every playlist the app can see, file-backed ones first.

    Mirrors ``routes/playlists.py::api_playlists_all``'s merge rule: an ``.nsp``
    file WINS over a same-named Navidrome entry, so the index must make the same
    choice or the artist page would list a playlist the Playlists page does not
    show (and vice versa).

    Returns ``(entries, navidrome_error_count, listings)``, where ``listings``
    holds the RAW ``getPlaylists`` payload per client, in client order.

    ⭐ THE LISTINGS ARE RETURNED ON PURPOSE. ``getPlaylists`` is already the most
    expensive request in this sync — one per server, carrying every playlist's
    metadata — and throwing the payload away forced ``_track_ids_for`` to call
    ``client.find_playlist_by_name(...)`` for each rule-based ``.nsp``, which
    re-fetches the WHOLE listing to answer one name lookup. Keeping the payload
    makes those lookups free, and inside one sync the listing cannot change.
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
    listings: list[list[dict[str, Any]]] = []
    for client in clients or []:
        try:
            remote = client.fetch_all_playlists() or []
        except Exception as exc:
            # Counted, not raised: one unreachable server must not abort the
            # sync for the others (nor cause the prune below to delete what it
            # could not enumerate).
            errors += 1
            listings.append([])
            _logger().warning("Playlist index: could not list Navidrome playlists: %s", exc)
            continue

        listings.append([nav for nav in remote if isinstance(nav, dict)])

        # ⚠️ AN EMPTY LISTING DURING AN IMPORT IS NOT AN EMPTY LIBRARY.
        #
        # ``fetch_all_playlists`` returns ``[]`` when the request was DEFERRED by
        # the import gate (it does not raise), which is the same value a genuinely
        # playlist-free server returns. Trusting it would make ``errors`` stay 0,
        # the prune would run against a listing that contains only the file-backed
        # playlists, and EVERY Navidrome playlist would be deleted from the index
        # — the exact "transient outage becomes a permanently empty artist page"
        # failure the prune guard exists to prevent.
        #
        # Counting it as an error makes the prune refuse, which is the safe
        # direction: one cycle indexes nothing, nothing is lost.
        if not remote:
            try:
                from services.navidrome.import_gate import is_import_running

                if is_import_running():
                    errors += 1
                    _logger().info(
                        "Playlist index: listing deferred by a running Navidrome import "
                        "— nothing indexed and pruning refused this pass"
                    )
                    continue
            except Exception:
                pass

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

    return entries, errors, listings


# ---------------------------------------------------------------------------
# Track resolution
# ---------------------------------------------------------------------------

def _find_in_listings(
    name: str,
    listings: list[list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Find a playlist by name in the listings ALREADY fetched this sync.

    Replaces ``client.find_playlist_by_name(name)``, which calls
    ``fetch_all_playlists()`` — re-downloading every playlist's metadata to
    answer one question, once per rule-based ``.nsp``. Same matching rule the
    client uses (case-insensitive, trimmed).

    A caller that needs to tell "not found" from "could not list" gets that
    from the ``errors`` count the sync already tracks, so this stays a pure
    lookup.
    """
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    for listing in listings or []:
        for candidate in listing or []:
            if str(candidate.get("name") or "").strip().lower() == wanted:
                return candidate
    return None


def _navidrome_ids_from_detail(detail: dict[str, Any]) -> list[str] | None:
    """Track ids from a ``getPlaylist`` payload, or ``None`` when UNREADABLE.

    ⚠️ ``NavidromeClient.fetch_playlist`` swallows every failure and returns
    ``{}``, so "the server did not answer" and "the playlist is empty" arrive as
    THE SAME VALUE. Telling them apart is the whole point of this helper: an
    empty playlist has a ``tracks`` key (possibly ``[]``) or reports
    ``songCount == 0``, while a failed fetch has neither.

    Returning ``None`` for the failed case is what stops a transient error being
    persisted as "this playlist has no tracks".
    """
    if not isinstance(detail, dict) or not detail:
        return None

    entries = detail.get("tracks")
    if entries is None:
        entries = detail.get("entry")
    if entries is None:
        # A playlist with songCount 0 legitimately carries no entries list.
        song_count = detail.get("songCount")
        try:
            if song_count is not None and int(song_count) == 0:
                return []
        except (TypeError, ValueError):
            pass
        return None

    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        return None

    ids: list[str] = []
    for track in entries:
        if isinstance(track, dict):
            song_id = str(track.get("id") or "").strip()
            if song_id:
                ids.append(song_id)
    return ids

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


def _track_ids_for(
    entry: dict[str, Any],
    clients: list[Any],
    *,
    listings: list[list[dict[str, Any]]] | None = None,
    detail: dict[str, Any] | None = None,
) -> tuple[list[str] | None, int]:
    """Resolve one playlist's entries to track ids.

    Returns ``(ids, unresolved)``. ``unresolved`` counts entries that were in the
    playlist but could not be mapped to a library track — reported by the caller
    rather than silently dropped, because "this playlist has 40 tracks but 0
    appear on the artist page" is otherwise indistinguishable from a broken
    query.
    """
    if entry.get("source") == _SOURCE_NAVIDROME:
        if detail is not None:
            prefetched = _navidrome_ids_from_detail(detail)
            if prefetched is not None:
                return prefetched, max(0, int(entry.get("song_count") or 0) - len(prefetched))
            # Unusable prefetched payload: fall through to a live fetch rather
            # than indexing the playlist as empty.

        for client in clients or []:
            try:
                live = client.fetch_playlist(entry["playlist_id"]) or {}
            except Exception as exc:
                _logger().warning(
                    "Playlist index: could not load playlist %s: %s",
                    entry.get("playlist_id"), exc,
                )
                continue
            ids = _navidrome_ids_from_detail(live)
            if ids is None:
                _logger().debug(
                    "Playlist index: unreadable payload for %s — not treating it as empty",
                    entry.get("playlist_id"),
                )
                continue
            return ids, max(0, int(entry.get("song_count") or 0) - len(ids))

        # No client could read it definitively. "Unknown", never "empty".
        return None, 0

    # ── File-backed ─────────────────────────────────────────────────────
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
        #
        # ⭐ The NAME is resolved from the listing this sync already fetched, so
        # this costs no extra ``getPlaylists`` request.
        found = _find_in_listings(str(entry.get("name") or ""), list(listings or []))
        found_id = str((found or {}).get("id") or "")
        if found_id:
            live = detail or {}
            if not live:
                for client in clients or []:
                    try:
                        live = client.fetch_playlist(found_id) or {}
                    except Exception as exc:
                        _logger().debug(
                            "Playlist index: rule-based lookup failed for %s: %s",
                            entry.get("name"), exc,
                        )
                        continue
                    if live:
                        break
            ids.extend(_navidrome_ids_from_detail(live) or [])

    # `read_nsp_playlist` synthesises `<file>#<n>` for an entry with no id.
    # Those can never join to a track, so they are counted as unresolved rather
    # than stored as rows that will never match.
    real = [i for i in ids if "#" not in i]
    return real, max(0, len(ids) - len(real))


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------

#: Default number of concurrent ``getPlaylist`` fetches.
#:
#: Each one is a read of one independent playlist, and the cost is dominated by
#: Navidrome building/serialising the entry list (~5s for ~1,200 tracks in the
#: reported run), so the win is close to linear. Four is deliberately
#: conservative: it is the same order as the tag-write pool, it keeps a small
#: Navidrome (a NAS, typically) from being asked for more than a handful of
#: simultaneous large queries, and it turns 66 sequential reads from ~5½ minutes
#: into well under one.
#:
#: Overridable with ``PLAYLIST_INDEX_FETCH_WORKERS`` for a server that can take
#: more (or needs fewer), and clamped so a typo cannot spawn a thread per
#: playlist.
_DEFAULT_FETCH_WORKERS = 4
_MAX_FETCH_WORKERS = 16


def _fetch_worker_count() -> int:
    """Concurrent ``getPlaylist`` fetches to use, from env with a safe clamp."""
    raw = str(os.environ.get("PLAYLIST_INDEX_FETCH_WORKERS") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_FETCH_WORKERS
    if value < 1:
        return 1
    return min(value, _MAX_FETCH_WORKERS)


def _prefetch_navidrome_details(
    entries: list[dict[str, Any]],
    clients: list[Any],
) -> dict[str, dict[str, Any]]:
    """Fetch ``getPlaylist`` for every changed Navidrome entry, bounded.

    Returns ``{playlist_id: detail}``. A playlist that could not be fetched is
    simply ABSENT — ``_track_ids_for`` then does its own (sequential, retrying)
    live fetch, so a partial result can only ever cost time, never correctness.

    ⚠️ ONLY Navidrome entries are prefetched. File-backed ones read a local file
    and (for ``.m3u``) issue a small SQL query; those are cheap and, more to the
    point, the query would touch the shared DB connection from a worker thread.
    Keeping every DB access on the calling thread is what makes this safe to run
    against the in-memory test engine, which hands the whole suite ONE
    connection.

    Results are guarded by an id check: a client that answers for the WRONG
    playlist is ignored rather than stored under the right one.
    """
    targets = [
        str(entry.get("playlist_id") or "")
        for entry in entries
        if entry.get("source") == _SOURCE_NAVIDROME
        and str(entry.get("playlist_id") or "").strip()
    ]
    if not targets or not clients:
        return {}

    workers = min(_fetch_worker_count(), len(targets))
    if workers <= 1:
        result: dict[str, dict[str, Any]] = {}
        for playlist_id in targets:
            detail = _fetch_one_playlist(playlist_id, clients)
            if detail:
                result[playlist_id] = detail
        return result

    result = {}
    try:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="playlist-index",
        ) as pool:
            futures = {
                pool.submit(_fetch_one_playlist, playlist_id, clients): playlist_id
                for playlist_id in targets
            }
            for future in as_completed(futures):
                playlist_id = futures[future]
                try:
                    detail = future.result()
                except Exception as exc:
                    _logger().debug(
                        "Playlist index: prefetch failed for %s: %s", playlist_id, exc
                    )
                    continue
                if detail:
                    result[playlist_id] = detail
    except Exception as exc:
        # A pool that cannot be built must not cost the sync its work: fall back
        # to the sequential path the loop already handles.
        _logger().warning("Playlist index: prefetch pool unavailable: %s", exc)
        return {}
    return result


def _fetch_one_playlist(playlist_id: str, clients: list[Any]) -> dict[str, Any]:
    """One playlist's detail from the first client that answers with an id match.

    ``fetch_playlist`` returns ``{}`` on failure (it swallows the exception), so
    an empty payload is treated as "this client could not answer" and the next
    one is tried. An empty-but-valid playlist comes back with a ``tracks`` key or
    ``songCount == 0`` and is therefore still a real answer.
    """
    for client in clients or []:
        try:
            detail = client.fetch_playlist(playlist_id) or {}
        except Exception as exc:
            _logger().debug(
                "Playlist index: fetch failed for %s: %s", playlist_id, exc
            )
            continue
        if not detail:
            continue
        reported_id = str(detail.get("id") or "").strip()
        if reported_id and reported_id != playlist_id:
            # A client answered for a DIFFERENT playlist; never store it under
            # this id.
            _logger().debug(
                "Playlist index: ignoring mismatched playlist payload",
                requested=playlist_id,
                received=reported_id,
            )
            continue
        return detail
    return {}

def sync_playlist_index(clients: list[Any] | None = None) -> dict[str, int]:
    """Bring the playlist index up to date. Safe to call repeatedly.

    Skips every playlist whose stamp has not moved, so a steady state makes one
    ``fetch_all_playlists`` call per server and no writes.

    TWO PHASES, and the split is deliberate:

    1. every CHANGED playlist's contents are fetched in a bounded thread pool
       (the expensive part — see ``_prefetch_navidrome_details``), then
    2. the resolved rows are written one playlist at a time.

    Reads are parallel because they are independent and read-only; writes are
    sequential because one ``db_session`` per call, a single shared in-memory
    connection under test, and one transaction per playlist all favour a single
    writer.

    ⚠️ NULL IS NOT A DECISION. The prune at the end only runs when EVERY
    configured server was enumerable. A Navidrome that is down or misconfigured
    returns no playlists, and pruning on that would wipe the index — turning a
    transient outage into a permanently empty artist-page section. An
    unenumerable source is a SKIP, never an empty result.

    The same rule applies one level down: a playlist whose contents could not be
    READ is skipped entirely rather than stored as empty, because an empty write
    deletes its rows.
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
        entries, errors, listings = _enumerate(clients)
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
    changed_entries: list[dict[str, Any]] = []
    for entry in entries:
        playlist_id = str(entry.get("playlist_id") or "")
        if not playlist_id:
            continue
        seen_ids.append(playlist_id)

        if stored.get(playlist_id) == str(entry.get("changed") or ""):
            stats["skipped"] += 1
            continue
        changed_entries.append(entry)

    # ── Phase 1: fetch the changed playlists' contents, BOUNDED-CONCURRENT ──
    #
    # ⭐ WHY CONCURRENCY HERE AND ONLY HERE. Reading one playlist is dominated by
    # the ``getPlaylist`` round trip: Navidrome has to build and serialise the
    # whole entry list, which measured at ~5 s for a ~1,200-track playlist. The
    # reported run had 66 changed playlists at ~1,200 entries each and spent
    # 5 m 36 s — i.e. every second of it was these calls, one after another.
    #
    # They are independent and read-only, so a bounded pool is safe. The DB
    # writes stay SEQUENTIAL in phase 2: ``db_session`` hands out a connection
    # per call and the in-memory test engine is a single shared connection, so
    # concurrent writers would serialise anyway (and on SQLite would contend).
    # One writer, many readers.
    prefetched = _prefetch_navidrome_details(changed_entries, clients)

    # ── Phase 2: resolve remaining, then write — sequentially ──────────────
    for entry in changed_entries:
        playlist_id = str(entry.get("playlist_id") or "")
        track_ids, unresolved = _track_ids_for(
            entry,
            clients,
            listings=listings,
            detail=prefetched.get(playlist_id),
        )
        if track_ids is None:
            # UNREADABLE, not empty. Writing now would delete every indexed row
            # for this playlist and drop it from the artist page until a later
            # sync happens to succeed.
            stats["errors"] += 1
            _logger().warning(
                "Playlist index: keeping previous entries for %s — its contents "
                "could not be read this pass",
                entry.get("name"),
            )
            continue

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
    max_rows: int = 5000,
) -> list[dict[str, Any]]:
    """Playlists containing any of ``artist``'s tracks, grouped by playlist.

    Returns ``[{playlist_id, name, type, owner, is_public, song_count,
    source, comment, tracks: [{title, album, artist, position}]}]``, ordered by
    playlist name then position.

    The artist match is ``COALESCE(NULLIF(album_artist, ''), artist)``,
    case-insensitively — the SAME expression the artist page uses to find the
    artist's albums. Matching on the bare ``artist`` column instead would list
    the playlists a guest credit sits in under the HOST artist's page, and miss
    them on the artist's own. The join lives in the repository, so the
    expression is written once.

    ⚠️ ``max_rows`` bounds the RESULT, never the input. The first version of this
    function fetched the artist's track ids with a LIMIT and queried them back,
    which meant an artist with more tracks than that cap reported only the
    playlists its first N tracks appeared in — a partial answer indistinguishable
    from a complete one. See ``artist_playlist_tracks_by_artist``.
    """
    name = str(artist or "").strip()
    if not name:
        return []

    try:
        rows = _repo.artist_playlist_tracks_by_artist(name, max_rows=max_rows)
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
