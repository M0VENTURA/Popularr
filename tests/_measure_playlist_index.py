"""Measure what `sync_playlist_index` actually costs per changed playlist.

Usage:  python tests/_measure_playlist_index.py

Not a test — a MEASUREMENT, kept in the tree so the numbers in the changelog can
be reproduced rather than taken on trust.

Counts the SQL statements a single `replace_playlist` emits via SQLAlchemy's
`before_cursor_execute` event, which is DIALECT-INDEPENDENT: it shows how many
round trips the driver is asked to make, on SQLite exactly as on PostgreSQL.
That is the number that matters here — `session.execute(text(...), rows)` routes
to `cursor.executemany`, and psycopg2's `executemany` is NOT batched: it loops
and issues one statement per row. The row COUNT is what makes the sync slow, not
the row size.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

# Run from anywhere: importing the app requires the REPO ROOT on sys.path, and
# running this file puts ``tests/`` there instead.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("CONFIG_PATH", "/dev/null")
os.environ.setdefault("LOG_PATH", os.path.join(tempfile.mkdtemp(), "app.log"))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import event  # noqa: E402
from sqlalchemy import text  # noqa: E402

from db.engine import get_engine  # noqa: E402

# ── Realistic shape, taken from the reported run ────────────────────────────
#   Playlist index synced playlists=615 changed=66 entries=79434 skipped=549
CHANGED_PLAYLISTS = 66
TOTAL_ENTRIES = 79434
ENTRIES_PER_PLAYLIST = TOTAL_ENTRIES // CHANGED_PLAYLISTS
ENUMERATED_PLAYLISTS = 615


def _create_schema() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS playlists ("
            " playlist_id TEXT PRIMARY KEY, name TEXT, source TEXT, type TEXT,"
            " owner TEXT, is_public BOOLEAN, song_count INTEGER, comment TEXT,"
            " changed TEXT, managed_type TEXT, synced_at TIMESTAMP)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS playlist_entries ("
            " playlist_id TEXT, track_id TEXT, position INTEGER,"
            " PRIMARY KEY (playlist_id, position))"
        ))


def main() -> int:
    _create_schema()
    engine = get_engine()

    counter = {"n": 0, "insert_entries": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        counter["n"] += 1
        if "INSERT INTO playlist_entries" in statement:
            counter["insert_entries"] += 1

    from db.repositories.playlists import (
        delete_playlists_not_in,
        replace_playlist,
        stored_changed_map,
    )

    # ---------------------------------------------------------------- read stamp
    counter["n"] = 0
    t0 = time.perf_counter()
    stored_changed_map()
    stamp_ms = (time.perf_counter() - t0) * 1000

    # ------------------------------------------------- one playlist, 1203 rows
    track_ids = [f"track-{i}" for i in range(ENTRIES_PER_PLAYLIST)]
    counter["n"] = 0
    counter["insert_entries"] = 0
    t0 = time.perf_counter()
    result = replace_playlist(
        playlist_id="navidrome::p1",
        name="Alternative Rock - Top Tracks",
        source="navidrome",
        playlist_type="regular",
        owner="moventura",
        is_public=True,
        song_count=len(track_ids),
        comment="",
        changed="1750000000",
        track_ids=track_ids,
    )
    one_ms = (time.perf_counter() - t0) * 1000
    one_statements = counter["n"]

    # ------------------------------------------------------- all changed ones
    counter["n"] = 0
    t0 = time.perf_counter()
    total_rows = 1
    for i in range(1, CHANGED_PLAYLISTS):
        replace_playlist(
            playlist_id=f"navidrome::p{i}",
            name=f"Playlist {i}",
            source="navidrome",
            playlist_type="regular",
            owner="moventura",
            is_public=True,
            song_count=len(track_ids),
            comment="",
            changed="1750000000",
            track_ids=track_ids,
        )
        total_rows += 1
    all_ms = (time.perf_counter() - t0) * 1000
    total_statements = counter["n"] + one_statements

    # ---------------------------------------------------------------- the prune
    counter["n"] = 0
    t0 = time.perf_counter()
    delete_playlists_not_in([f"navidrome::p{i}" for i in range(ENUMERATED_PLAYLISTS)])
    prune_ms = (time.perf_counter() - t0) * 1000
    prune_statements = counter["n"]

    # ASCII only: this is often run from a Windows console on cp1252, where a
    # box-drawing character raises UnicodeEncodeError and kills the run.
    print("-- companion read (stamp map) ------------------------------")
    print(f"  stored_changed_map()      {stamp_ms:8.1f} ms   {ENUMERATED_PLAYLISTS} ids")
    print()
    print(f"-- ONE playlist ({ENTRIES_PER_PLAYLIST} entries) ---------------------")
    print(f"  SQL statements            {one_statements:8d}      "
          f"(1 upsert + 1 delete + {counter['insert_entries']} entry inserts)")
    print(f"  wall clock                {one_ms:8.1f} ms")
    print(f"  wrote                     {result['entries']:8d} rows")
    print()
    print(f"-- ALL {CHANGED_PLAYLISTS} changed playlists "
          f"({total_rows * ENTRIES_PER_PLAYLIST} rows) ----")
    print(f"  SQL statements            {total_statements:8d}")
    print(f"  wall clock                {all_ms:8.1f} ms  (SQLite, in-process)")
    print()
    print(f"-- prune ({ENUMERATED_PLAYLISTS} kept ids) --------------------")
    print(f"  SQL statements            {prune_statements:8d}")
    print(f"  wall clock                {prune_ms:8.1f} ms")
    print()
    print("==============================================================")
    print(f"  Statement ratio: 1 statement per "
          f"{total_rows * ENTRIES_PER_PLAYLIST / total_statements:.1f} rows")
    print(f"  i.e. {total_statements} round trips for "
          f"{total_rows * ENTRIES_PER_PLAYLIST} rows.")
    print()
    print("  SQLite is in-process, so the wall clock here is a LOWER BOUND.")
    print("  The statement count is dialect-independent -- it is the number of")
    print("  round trips the driver is asked to make. On PostgreSQL over a")
    print("  socket or network, ~79k round trips is the reported cost.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
