"""Playlist index repository queries.

The two tables recorded by migration ``025_playlist_index``. This module is the
ONLY writer of them (see the layering rules): everything else goes through
``services/playlists/playlist_index_service``.

Why the index exists
--------------------
The artist page gained an "Included in Playlists" section. There is no cheaper
way to answer it than a playlist -> track mapping — Subsonic has no "which
playlists contain track X" call — and reading every playlist live would cost one
request per playlist on every artist page load. So the mapping is snapshotted
here during the library sync and the page reads it as a plain join.

⚠️ EVERY read is guarded by ``_tables_exist``.
``db.engine.db_session`` DISPOSES the shared engine on a transient error, and
for an in-memory test database that erases EVERY table. A single "no such
table" from this module used to wipe ``tracks`` mid-suite and fail an unrelated
test five queries later (see ``db/repositories/artist_aliases.py`` for the
same reasoning on a much smaller table). A missing table is a QUIET answer —
empty list / empty dict / 0 — never an error.

⚠️ IN-LISTS USE ``bindparam(..., expanding=True)``, NOT ``json_each``.
``json_each`` is SQLite's array deconstructor; PostgreSQL's ``json_each``
deconstructs an OBJECT and raises "cannot deconstruct an array as an object"
when handed the array this module would pass it. Production is PostgreSQL-only,
so the idiom would have failed there while passing every test. ``expanding=True``
is the house pattern (``finalise_stage``, ``album_stage``,
``musicbrainz_persistence_service``) and renders the right number of
placeholders per dialect, so it cannot drift the same way.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, inspect, text

import structlog

from db.engine import db_session

logger = structlog.get_logger(__name__)


def _tables_exist(session: Any) -> bool:
    """True when both index tables are present. See the module note."""
    try:
        inspector = inspect(session.get_bind())
        return inspector.has_table("playlists") and inspector.has_table("playlist_entries")
    except Exception:
        return False


def stored_changed_map() -> dict[str, str]:
    """``playlist_id -> changed`` for every indexed playlist.

    This is the sync's FAST PATH: a playlist whose ``changed`` stamp is
    unchanged needs no Subsonic request and no row writes at all. Only the
    stamp values are returned, so the caller never loads playlist rows it will
    not touch.

    Empty when the table is absent (see ``_tables_exist``).
    """
    with db_session() as session:
        if not _tables_exist(session):
            return {}
        rows = session.execute(
            text("SELECT playlist_id, changed FROM playlists")
        ).mappings().all()
        return {str(r["playlist_id"]): str(r["changed"] or "") for r in rows}


def stored_managed_type_map() -> dict[str, str]:
    """``playlist_id -> managed_type`` for every indexed playlist.

    The sync compares this alongside the content stamp so a classification
    written by a NEWER build (or a changed config template) is backfilled even
    when the playlist's contents have not moved.
    """
    with db_session() as session:
        if not _tables_exist(session):
            return {}
        try:
            rows = session.execute(
                text("SELECT playlist_id, managed_type FROM playlists")
            ).mappings().all()
        except Exception:
            return {}
        return {
            str(row.get("playlist_id") or ""): str(row.get("managed_type") or "")
            for row in rows
            if str(row.get("playlist_id") or "")
        }


def update_playlist_managed_type(playlist_id: str, managed_type: str | None) -> int:
    """Backfill one playlist's ownership type WITHOUT touching its entries.

    The sync calls this when the content stamp is unchanged but the stored type
    differs (a newer build, or a changed naming template). Re-fetching every
    playlist just to record a type would be a full-library read for a metadata
    change.
    """
    with db_session() as session:
        if not _tables_exist(session):
            return 0
        try:
            result = session.execute(
                text(
                    "UPDATE playlists SET managed_type = :managed_type "
                    "WHERE playlist_id = :playlist_id"
                ),
                {"managed_type": managed_type, "playlist_id": playlist_id},
            )
            return int(result.rowcount or 0)
        except Exception:
            return 0


def playlist_index_available() -> bool:
    """True when the playlist index tables are present and queryable.

    Destructive cleanup uses this as its proof gate: an unavailable index means
    "ownership cannot be proven", and the safe answer is to delete nothing.
    """
    with db_session() as session:
        return _tables_exist(session)


def managed_playlist_types() -> dict[str, str]:
    """``playlist name (casefolded) -> managed_type`` for the orphan cleaner.

    Returns only rows whose type is KNOWN. A playlist absent from this map is
    one the cleaner must not treat as its own, which is why the caller cannot
    use a missing entry as "orphan".

    Tolerant by design: an un-migrated database (no ``managed_type`` column)
    reads as ``{}``, so cleanup refuses until the index has been migrated and
    re-synced rather than guessing from names.
    """
    with db_session() as session:
        if not _tables_exist(session):
            return {}
        try:
            rows = session.execute(
                text(
                    "SELECT name, managed_type FROM playlists "
                    "WHERE managed_type IS NOT NULL AND TRIM(managed_type) <> ''"
                )
            ).mappings().all()
        except Exception:
            return {}
        return {
            str(row.get("name") or "").strip().casefold(): str(row.get("managed_type") or "")
            for row in rows
            if str(row.get("name") or "").strip()
        }


def replace_playlist(
    *,
    playlist_id: str,
    name: str,
    source: str,
    playlist_type: str | None,
    owner: str | None,
    is_public: bool | None,
    song_count: int,
    comment: str | None,
    changed: str | None,
    track_ids: list[str],
    managed_type: str | None = None,
) -> dict[str, int]:
    """Replace one playlist's row and its entries in a SINGLE transaction.

    Entries are deleted and re-inserted rather than diffed: a playlist's
    ordering is part of its meaning, so every row's position can move when one
    track is added, and diffing would have to rewrite most of them anyway. The
    whole playlist is one atomic unit — a crash must never leave the new
    header against the old entries.

    ``position`` is 1-based and is the PRIMARY KEY half, because a playlist may
    legitimately contain the same track twice. Re-deriving it from enumerate()
    is what makes that safe.

    Returns ``{"entries": n}`` so the caller can report what it wrote.
    """
    with db_session() as session:
        if not _tables_exist(session):
            return {"entries": 0}

        # ⭐ SAME LOCK, SAME ORDER AS ``delete_playlists_not_in``.
        #
        # ⚠️ THIS FUNCTION USED TO UPSERT THE HEADER BEFORE DELETING ENTRIES —
        # the exact reverse of the sweep, which deletes entries first. Two
        # concurrent writers therefore touched ``playlists`` and
        # ``playlist_entries`` in opposite orders and PostgreSQL resolved the
        # cycle by killing one. Entries are now deleted FIRST here as well, so
        # every writer to these two tables follows one lock order.
        _acquire_write_lock(session)

        session.execute(
            text("DELETE FROM playlist_entries WHERE playlist_id = :playlist_id"),
            {"playlist_id": playlist_id},
        )

        session.execute(
            text(
                "INSERT INTO playlists "
                "(playlist_id, name, source, type, owner, is_public, song_count, "
                " comment, changed, managed_type, synced_at) "
                "VALUES (:playlist_id, :name, :source, :type, :owner, :is_public, "
                "        :song_count, :comment, :changed, :managed_type, CURRENT_TIMESTAMP) "
                "ON CONFLICT (playlist_id) DO UPDATE SET "
                "  name = EXCLUDED.name, source = EXCLUDED.source, "
                "  type = EXCLUDED.type, owner = EXCLUDED.owner, "
                "  is_public = EXCLUDED.is_public, song_count = EXCLUDED.song_count, "
                "  comment = EXCLUDED.comment, changed = EXCLUDED.changed, "
                "  managed_type = EXCLUDED.managed_type, "
                "  synced_at = CURRENT_TIMESTAMP"
            ),
            {
                "playlist_id": playlist_id,
                "name": name,
                "source": source,
                "type": playlist_type,
                "owner": owner,
                "is_public": is_public,
                "song_count": int(song_count or 0),
                "comment": comment,
                "changed": changed,
                "managed_type": managed_type,
            },
        )

        # One executemany, not N round-trips: a 500-track playlist is 500 rows
        # and a per-row INSERT would dominate the sync's cost.
        rows = [
            {"playlist_id": playlist_id, "track_id": str(tid), "position": position}
            for position, tid in enumerate(track_ids or [], start=1)
            if str(tid or "").strip()
        ]
        if rows:
            session.execute(
                text(
                    "INSERT INTO playlist_entries (playlist_id, track_id, position) "
                    "VALUES (:playlist_id, :track_id, :position)"
                ),
                rows,
            )

        return {"entries": len(rows)}


#: Serialises playlist INDEX writes across processes.
#:
#: ⚠️ REPORTED: "playlist sync, the orphan sweep and the genre playlist rebuild"
#: overlapped, and the index sync deadlocked. Two writers each deleting from
#: ``playlists`` in one order and inserting into ``playlist_entries`` in another
#: is the classic cycle. ``pg_advisory_xact_lock`` makes the phase exclusive and
#: releases itself at COMMIT/ROLLBACK, so a crash cannot strand the lock.
_PLAYLIST_INDEX_LOCK_NAME = "playlist_index_write"


def _is_postgres(session: Any) -> bool:
    """True when this session talks to PostgreSQL.

    ⚠️ DO NOT TOUCH ``session.bind``. Reading it materialises the engine, and
    ``db.engine.get_engine()`` CONSTRUCTS one when none exists — which, under
    pytest, means creating the PostgreSQL engine and importing psycopg2. In this
    environment that module is not installed, so a single ``session.bind.dialect``
    access raised ``ModuleNotFoundError`` and **disposed the shared in-memory
    SQLite engine**, wiping every table for the rest of the session. It turned 7
    unrelated suites into 40+ collection errors.

    ``session.get_bind()`` returns the engine the session is ACTUALLY bound to
    without triggering construction, so the dialect can be read safely.
    """
    try:
        bind = session.get_bind()
        return getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"
    except Exception:  # pragma: no cover - defensive
        return False


def _acquire_write_lock(session: Any) -> None:
    """Take the playlist-index advisory lock for this transaction.

    ⭐ ``pg_advisory_xact_lock``, not ``pg_try_advisory_lock``: the caller is a
    background sync that should WAIT for the other writer rather than skip a
    pass and leave the index stale. The lock dies with the transaction, so
    nothing has to unwind it on an exception.

    ⚠️ SILENTLY SKIPPED OFF POSTGRESQL. SQLite (the test engine) has no advisory
    locks and needs none — it serialises writers anyway.
    """
    if not _is_postgres(session):
        return
    try:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:name))"),
            {"name": _PLAYLIST_INDEX_LOCK_NAME},
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Playlist index lock unavailable", error=str(exc))


def delete_playlists_not_in(playlist_ids: list[str]) -> int:
    """Drop every playlist that is no longer in the source of truth.

    ⭐ ``DELETE FROM playlists WHERE playlist_id NOT IN :ids`` in BOTH branches —
    never an unfiltered ``DELETE FROM playlists``.

    ⚠️ THE UNFILTERED DELETE WAS A DEADLOCK, NOT JUST A SHORTCUT. It ran
    ``DELETE FROM playlist_entries`` (no predicate, so every row) and then
    ``DELETE FROM playlists`` while a concurrent ``replace_playlist`` was doing
    ``DELETE ... WHERE playlist_id = ?`` followed by INSERTs. A full-table delete
    takes row locks in SCAN order; the targeted delete takes them in PK order.
    Two transactions acquiring the same rows in opposite orders is a cycle, and
    PostgreSQL kills one of them. Expressing the empty case as
    ``NOT IN ()`` — which the expanding parameter already renders as "matches
    everything" — keeps ONE lock order for every path.

    ⚠️ That also means the empty branch no longer needs to be special-cased for
    CORRECTNESS. It is kept because the semantics here are destructive and
    "an empty IN list" is a dialect detail this module has already been bitten
    by (see the ``json_each`` note in the module docstring).
    """
    with db_session() as session:
        if not _tables_exist(session):
            return 0
        _acquire_write_lock(session)

        ids = [str(v or "").strip() for v in (playlist_ids or []) if str(v or "").strip()]

        # An empty keep-list means "delete everything": with no ids, the NOT IN
        # renders as a predicate that matches every row, which is the same
        # statement shape as the non-empty case. One lock order, always.
        #
        # Entries first, so a half-deleted playlist cannot surface in the artist
        # query between the two statements.
        session.execute(
            text(
                "DELETE FROM playlist_entries WHERE playlist_id NOT IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        )
        deleted = session.execute(
            text(
                "DELETE FROM playlists WHERE playlist_id NOT IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).rowcount or 0
        return int(deleted or 0)


def artist_playlist_tracks_by_artist(
    artist: str,
    *,
    max_rows: int = 5000,
) -> list[dict[str, Any]]:
    """Every indexed playlist row for tracks credited to ``artist``.

    The artist page's only query, and it joins on the artist expression
    DIRECTLY rather than taking a list of track ids.

    ⚠️ WHY NOT ids. The first version asked for the artist's track ids first
    and passed them back in an IN list, which needed a LIMIT — and any LIMIT
    silently truncates the answer for a large discography. An artist with more
    tracks than the cap would report only the playlists its FIRST N tracks
    appear in, and nothing would say so: a partial answer that looks complete.
    The join cannot have that failure mode, because there is no intermediate
    list to truncate, and it is one query instead of two.

    ``max_rows`` bounds only the RESULT (it protects the page render from a
    pathological index), never the INPUT, so hitting it can only trim rows from
    the tail — never hide a playlist that a different cap would have included.

    ⚠️ The artist expression is ``COALESCE(NULLIF(album_artist, ''), artist)``,
    the SAME one the artist page uses to find its albums: a guest-credited track
    ("Nirvana feat. X") has its own `artist` but an `album_artist` of the host, so
    matching the bare column would file that track under the guest.

    Display fields come from ``tracks`` (this library's own row) rather than
    from whatever Navidrome reported at sync time, so a track renamed since the
    last sync still displays correctly — and a track deleted from the library
    disappears from the section without waiting for a re-sync.
    """
    name = str(artist or "").strip()
    if not name:
        return []

    with db_session() as session:
        if not _tables_exist(session):
            return []
        rows = session.execute(
            text(
                "SELECT p.playlist_id, p.name, p.type, p.owner, p.is_public, "
                "       p.song_count, p.comment, p.source, "
                "       e.position, e.track_id, "
                "       t.title, t.album, t.artist "
                "FROM tracks t "
                "JOIN playlist_entries e ON e.track_id = t.id "
                "JOIN playlists p ON p.playlist_id = e.playlist_id "
                "WHERE LOWER(COALESCE(NULLIF(t.album_artist, ''), t.artist)) "
                "      = LOWER(:artist) "
                "ORDER BY LOWER(COALESCE(p.name, '')), e.position "
                "LIMIT :max_rows"
            ),
            {"artist": name, "max_rows": int(max_rows)},
        ).mappings().all()
        return [dict(r) for r in rows]


def playlist_names_by_track_id() -> dict[str, str]:
    """``track_id -> "; "-joined playlist names``, for the whole library.

    Backs the popularity CSV's "playlists_included_in" column: one track per
    line, so the reader can see WHY a track scored where it did — a track that
    only ever appears in a "Hidden Gems" playlist is being judged differently
    from one that sits in five.

    ⚠️ THIS IS NOT SCOPED, ON PURPOSE. The collection export IS the whole
    library, so a per-artist variant would just be this query with a filter the
    caller does not need. Reading it once here also costs ONE statement for the
    entire report rather than one per row.

    ⚠️ A track in the same playlist TWICE appears once. ``playlist_entries``
    is keyed on ``(playlist_id, position)`` so a legitimate duplicate is two
    rows; naming the playlist twice says nothing extra.

    Returns ``{}`` when the index tables are absent (see ``_tables_exist``) —
    a quiet answer, never an error, because the report must still export.
    """
    with db_session() as session:
        if not _tables_exist(session):
            return {}
        try:
            # ⭐ GROUP BY, not DISTINCT: the same track may sit in one playlist
            # TWICE (``playlist_entries`` is keyed on ``(playlist_id, position)``),
            # and DISTINCT over the pair then emitted that name twice. Grouping
            # by the pair collapses it while still allowing one track to name
            # several playlists.
            #
            # ⚠️ ``ORDER BY e.track_id`` matters even here: the rows are folded
            # into a dict below, so the order is invisible in the result — but
            # keeping it makes the statement deterministic under a future change,
            # and lets a caller stream it without sorting first.
            rows = session.execute(
                text(
                    "SELECT e.track_id, p.name "
                    "FROM playlist_entries e "
                    "JOIN playlists p ON p.playlist_id = e.playlist_id "
                    "WHERE COALESCE(TRIM(p.name), '') <> '' "
                    "GROUP BY e.track_id, p.name "
                    "ORDER BY e.track_id, LOWER(p.name)"
                )
            ).mappings().all()
        except Exception:
            return {}

    grouped: dict[str, list[str]] = {}
    for row in rows:
        track_id = str(row.get("track_id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not track_id or not name:
            continue
        names = grouped.setdefault(track_id, [])
        if name.casefold() not in {n.casefold() for n in names}:
            names.append(name)
    return {tid: "; ".join(names) for tid, names in grouped.items()}


def index_sizes() -> dict[str, int]:
    """Row counts, for the sync's report. Both zero when the tables are absent."""
    with db_session() as session:
        if not _tables_exist(session):
            return {"playlists": 0, "entries": 0}
        playlists = session.execute(text("SELECT COUNT(*) FROM playlists")).scalar() or 0
        entries = session.execute(text("SELECT COUNT(*) FROM playlist_entries")).scalar() or 0
        return {"playlists": int(playlists), "entries": int(entries)}
