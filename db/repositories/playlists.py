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

from db.engine import db_session


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

        session.execute(
            text(
                "INSERT INTO playlists "
                "(playlist_id, name, source, type, owner, is_public, song_count, "
                " comment, changed, synced_at) "
                "VALUES (:playlist_id, :name, :source, :type, :owner, :is_public, "
                "        :song_count, :comment, :changed, CURRENT_TIMESTAMP) "
                "ON CONFLICT (playlist_id) DO UPDATE SET "
                "  name = EXCLUDED.name, source = EXCLUDED.source, "
                "  type = EXCLUDED.type, owner = EXCLUDED.owner, "
                "  is_public = EXCLUDED.is_public, song_count = EXCLUDED.song_count, "
                "  comment = EXCLUDED.comment, changed = EXCLUDED.changed, "
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
            },
        )

        session.execute(
            text("DELETE FROM playlist_entries WHERE playlist_id = :playlist_id"),
            {"playlist_id": playlist_id},
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


def delete_playlists_not_in(playlist_ids: list[str]) -> int:
    """Drop every playlist that is no longer in the source of truth.

    Entries first: the tables have no foreign key, so the order is what keeps a
    half-deleted playlist out of the artist query for the duration of the
    transaction.
    """
    with db_session() as session:
        if not _tables_exist(session):
            return 0

        # An empty keep-list means "delete everything". That is the correct
        # reading when the source genuinely returned no playlists — and it is
        # ALSO what a transient Navidrome failure looks like, so the decision
        # to call this at all belongs to the service, not here.
        ids = [str(v or "").strip() for v in (playlist_ids or []) if str(v or "").strip()]

        # The empty case is handled EXPLICITLY rather than delegated to the
        # expanding parameter, so the "delete everything" meaning is READABLE at
        # the call site instead of inferred from an empty-IN rendering rule.
        #
        # ⚠️ Be precise about why: this branch is DEFENSIVE, not load-bearing.
        # SQLAlchemy substitutes an empty expanding list for `NOT IN` with a
        # rendering that matches every row, so removing the branch still clears
        # the tables — verified by mutation, which ESCAPED for exactly that
        # reason. It is kept because "an empty IN list" is the kind of dialect
        # detail that has already bitten this module once (see the json_each note
        # above), and the semantics here are destructive enough to spell out.
        if not ids:
            session.execute(text("DELETE FROM playlist_entries"))
            deleted_all = session.execute(text("DELETE FROM playlists")).rowcount or 0
            return int(deleted_all or 0)

        # Entries first, so a half-deleted playlist cannot surface in the artist
        # query between the two statements. Not counted: the playlist count is
        # the number that means something to a caller (a playlist is the unit a
        # user recognises; its rows are an implementation detail).
        session.execute(
            text("DELETE FROM playlist_entries WHERE playlist_id NOT IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        )
        deleted_playlists = session.execute(
            text("DELETE FROM playlists WHERE playlist_id NOT IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ).rowcount or 0
        return int(deleted_playlists or 0)


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


def index_sizes() -> dict[str, int]:
    """Row counts, for the sync's report. Both zero when the tables are absent."""
    with db_session() as session:
        if not _tables_exist(session):
            return {"playlists": 0, "entries": 0}
        playlists = session.execute(text("SELECT COUNT(*) FROM playlists")).scalar() or 0
        entries = session.execute(text("SELECT COUNT(*) FROM playlist_entries")).scalar() or 0
        return {"playlists": int(playlists), "entries": int(entries)}
