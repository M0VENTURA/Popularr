"""The queue metadata heal: correct tracks that do not match the release queued.

The reported problem
--------------------
> When metadata is added to a track that is downloaded from the queue, it
> doesn't always match the metadata of the release that was added to the queue.
> Sometimes the album year or the release mbid is different.
>
> Could a heal also be added to tracks already in the queue to correct any that
> have the wrong information?

The import fault itself is fixed at the source (see
``test_album_metadata_release_scoping.py``). This covers the heal for rows that
were imported BEFORE that fix, and nothing revisits: the track is internally
consistent, so no later scan has a reason to touch it.

The scope rule these tests exist to pin
---------------------------------------
A correction is applied to the tracks that THE QUEUE ROW PRODUCED, not to every
track sharing the album name. A library can legitimately hold two editions of one
album under the same artist+album name — and the fault being repaired is exactly
one of them having donated its identity to the other. A heal that swept the whole
album group would do that damage in reverse and the two editions could never be
told apart again. ``test_a_different_release_sharing_the_album_name_is_left_alone``
is the guard.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from db.engine import db_session

RELEASE_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"  # what the queue row names
GROUP_A = "aaaa1111-1111-4111-8111-aaaa11111111"
RELEASE_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"  # what the import wrongly wrote
GROUP_B = "bbbb2222-2222-4222-8222-bbbb22222222"

MB_RELEASE_A = {
    "release_year": 1994,
    "releasedate": "1994-08-22",
    "original_year": 1994,
    "original_date": "1994-08-22",
    "release_group_mbid": GROUP_A,
    "barcode": "A-BARCODE",
    "recordlabel": "Go! Beat",
    "catalognumber": "A-CAT",
    "media": "CD",
    "disctotal": 1,
    "album_type": "Album",
    "status": "Official",
    "releasecountry": "GB",
    "album_artist_mbid": "AAAA1111-1111-4111-8111-AAAA11111111",
}

#: The columns this module's assertions actually require. The table itself is
#: built from the PRODUCTION shape (see ``_queue_ddl_columns``) — a hand-listed
#: subset would make this module a competitor to the other modules that create
#: ``download_queue``, and whoever runs first would define the table while the
#: loser's INSERTs failed on missing columns.
_REQUIRED_COLUMNS: tuple[str, ...] = (
    "artist",
    "album",
    "album_artist",
    "title",
    "status",
    "release_id",
    "release_mbid",
    "musicbrainz_releasegroupid",
    "recording_mbid",
    "music_file_path",
    "year",
    "release_year",
    "metadata",
)


def _queue_ddl_columns() -> dict[str, str]:
    """Every column the REAL ``download_queue`` has, in SQLite-friendly form.

    Derived from ``db/schema.py`` (the base CREATE TABLE plus the column
    registry) rather than hand-written, so the test database is the production
    shape and any sibling module's INSERT works whichever order the suite runs
    in. The only rewrite needed is stripping Postgres type casts, which SQLite
    cannot parse.
    """
    columns: dict[str, str] = {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        # The base CREATE TABLE columns (db/schema.py TABLES_TO_ENSURE).
        "artist": "TEXT",
        "title": "TEXT",
        "album": "TEXT",
        "status": "TEXT DEFAULT 'queued'",
        "source": "TEXT DEFAULT 'soulseek'",
        "priority": "INTEGER DEFAULT 5",
        "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    }

    from db.schema import COLUMN_REGISTRY

    for name, ddl in COLUMN_REGISTRY["download_queue"].items():
        # ``DEFAULT '{}'::jsonb`` is a Postgres cast SQLite rejects.
        columns.setdefault(name, ddl.replace("::jsonb", ""))

    return columns


def _ensure_queue_table() -> None:
    """Create/extend ``download_queue`` on the suite's shared in-memory database.

    ⚠️⚠️ DDL GOES THROUGH A RAW CONNECTION, NOT ``db_session``. That context
    manager DISPOSES the shared engine on ANY ``OperationalError``, and with
    ``sqlite:///:memory:`` a dispose erases the entire database — so a single
    failed statement here would delete the ``download_queue`` this function had
    just created, and the next ``DELETE`` would die with "no such table". That
    is not hypothetical: running this module AFTER
    ``test_download_completion_not_found_loop`` (which leaves the engine
    disposed) reproduced it every time.

    ⚠️ ``CREATE TABLE IF NOT EXISTS`` alone is also NOT enough: another module
    may already have created a MINIMAL ``download_queue``, after which every
    later ``CREATE TABLE IF NOT EXISTS`` is a silent no-op and the columns this
    module needs never appear. The ``ALTER TABLE ADD COLUMN`` backfill handles
    that, which is what the real startup path does too.
    """
    from db.engine import get_engine
    from db.models import Track

    engine = get_engine()
    ddl_columns = _queue_ddl_columns()
    _ddl = ", ".join(f"{name} {sql}" for name, sql in ddl_columns.items())

    with engine.begin() as conn:
        Track.__table__.create(conn, checkfirst=True)
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS download_queue ({_ddl})"))

    with engine.begin() as conn:
        existing = {
            str(row[1])
            for row in conn.execute(text("PRAGMA table_info(download_queue)")).fetchall()
        }

    for name, sql_type in ddl_columns.items():
        if name in existing or name == "id":
            continue
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(f"ALTER TABLE download_queue ADD COLUMN {name} {sql_type}")
                )
        except Exception:
            # Concurrently added, or the dialect refused it — either way the
            # assertion below is what decides whether the table is usable.
            pass

    with engine.begin() as conn:
        present = {
            str(row[1])
            for row in conn.execute(text("PRAGMA table_info(download_queue)")).fetchall()
        }
    absent = sorted(set(_REQUIRED_COLUMNS) - present)
    if absent:
        raise AssertionError(
            f"download_queue is missing {absent} — the heal cannot be tested on it"
        )


_ensure_queue_table()


@pytest.fixture(autouse=True)
def _queue_schema():
    """Ensure the queue table exists before anything in this module touches it.

    ``_clean_slate`` DEPENDS on this explicitly rather than relying on autouse
    definition order. A ``DELETE`` against a table another fixture has not yet
    created is an ``OperationalError``, and ``db_session`` DISPOSES the shared
    engine on one of those — which with ``sqlite:///:memory:`` erases the entire
    database and takes every following test with it. That is exactly what
    happened when this module ran late in the full suite.
    """
    _ensure_queue_table()
    yield


@pytest.fixture
def mb(monkeypatch):
    """MusicBrainz knows release A only; every call is recorded."""
    calls: list[str] = []

    def _fake_fetch(release_mbid: str):
        calls.append(release_mbid)
        return dict(MB_RELEASE_A) if release_mbid == RELEASE_A else None

    import services.enrichment.musicbrainz_service as mbs

    monkeypatch.setattr(mbs, "fetch_musicbrainz_release_metadata", _fake_fetch)
    return calls


@pytest.fixture
def tag_writes(monkeypatch):
    """Capture tag writes instead of needing real audio files."""
    writes: list[tuple[str, dict[str, Any]]] = []

    import services.metadata.tag_file_service as tfs

    monkeypatch.setattr(tfs, "resolve_music_file_path", lambda p: p)
    monkeypatch.setattr(
        tfs, "write_tags_to_file", lambda path, tags: (writes.append((path, dict(tags))) or True)
    )
    return writes


def _clear_queue() -> None:
    _ensure_queue_table()
    with db_session() as session:
        session.execute(text("DELETE FROM download_queue"))
        session.commit()


def _clear_tracks() -> None:
    with db_session() as session:
        session.execute(text("DELETE FROM tracks"))
        session.commit()


@pytest.fixture(autouse=True)
def _clean_slate(_queue_schema):
    _clear_queue()
    _clear_tracks()
    yield
    _clear_queue()
    _clear_tracks()


def _add_track(
    *,
    track_id: str,
    title: str,
    file_path: str,
    release_mbid: str = RELEASE_B,
    group: str = GROUP_B,
) -> None:
    """A library row carrying the WRONG release's album identity."""
    with db_session() as session:
        session.execute(
            text(
                """
                INSERT INTO tracks (
                    id, artist, album_artist, album, title, file_path,
                    recording_mbid, musicbrainz_album_mbid,
                    musicbrainz_releasegroupid, musicbrainz_albumtype,
                    musicbrainz_albumstatus, musicbrainz_albumartistid,
                    releasecountry, recordlabel, catalognumber, barcode,
                    media, disctotal, releasedate, originaldate, originalyear,
                    year, release_year
                ) VALUES (
                    :id, 'Portishead', 'Portishead', 'Dummy', :title, :file_path,
                    :rec, :release_mbid, :group, 'Album', 'Official',
                    'BBBB2222-2222-4222-8222-BBBB22222222', 'GB', 'London',
                    'B-CAT', 'B-BARCODE', 'CD', '2', '1997-08-25', '1997-08-25',
                    '1997', '1997', 1997
                )
                """
            ),
            {
                "id": track_id,
                "title": title,
                "file_path": file_path,
                "rec": f"rec-{track_id}",
                "release_mbid": release_mbid,
                "group": group,
            },
        )
        session.commit()


def _add_queue_row(
    *,
    queue_id: int,
    title: str,
    file_path: str,
    release_mbid: str | None = RELEASE_A,
    status: str = "imported",
    metadata: str = "{}",
) -> None:
    with db_session() as session:
        session.execute(
            text(
                """
                INSERT INTO download_queue (
                    id, artist, album, album_artist, title, status,
                    release_id, release_mbid, recording_mbid, music_file_path,
                    year, release_year, metadata
                ) VALUES (
                    :id, 'Portishead', 'Dummy', 'Portishead', :title, :status,
                    :release_mbid, :release_mbid, :rec, :file_path,
                    '1994', 1994, :metadata
                )
                """
            ),
            {
                "id": queue_id,
                "title": title,
                "status": status,
                "release_mbid": release_mbid,
                "rec": f"rec-t{queue_id - 1}",
                "file_path": file_path,
                "metadata": metadata,
            },
        )
        session.commit()


def _track(track_id: str) -> dict[str, Any]:
    with db_session() as session:
        row = session.execute(
            text("SELECT * FROM tracks WHERE id = :i"), {"i": track_id}
        ).fetchone()
    return dict(getattr(row, "_mapping", None) or {})


def _heal(**kwargs):
    from services.downloads.download_metadata_heal_service import heal_queue_metadata

    return heal_queue_metadata(**kwargs)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_dry_run_reports_drift_without_writing(mb, tag_writes) -> None:
    _add_track(track_id="t1", title="Mysterons", file_path="/music/Dummy/01.mp3")
    _add_queue_row(queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3")

    before = _track("t1")
    result = _heal(dry_run=True)

    assert result["dry_run"] is True
    assert result["drifted"] == 1
    assert result["healed"] == 0
    assert result["tracks_updated"] == 0
    assert tag_writes == []

    assert len(result["albums"]) == 1
    entry = result["albums"][0]
    assert entry["applied"] is False
    assert "musicbrainz_album_mbid" in entry["fields"]
    assert "barcode" in entry["fields"]

    assert _track("t1") == before, "a dry run must not modify the row"


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def test_heal_aligns_the_album_identity(mb, tag_writes) -> None:
    _add_track(track_id="t1", title="Mysterons", file_path="/music/Dummy/01.mp3")
    _add_queue_row(queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3")

    result = _heal()

    assert result["healed"] == 1
    assert result["tracks_updated"] == 1
    assert result["files_written"] == 1
    assert result["files_failed"] == 0

    row = _track("t1")
    assert row["musicbrainz_album_mbid"] == RELEASE_A, "the queued release MBID is what was asked for"
    assert row["musicbrainz_releasegroupid"] == GROUP_A
    assert row["barcode"] == "A-BARCODE"
    assert row["catalognumber"] == "A-CAT"
    assert str(row["originalyear"]) == "1994"
    assert str(row["release_year"]) == "1994"
    assert str(row["releasedate"]) == "1994-08-22"


def test_year_and_release_year_are_mapped_not_copied(mb, tag_writes) -> None:
    """``year`` is the ORIGINAL year and ``release_year`` the EDITION year.

    ``tracks.year`` feeds ORIGINALYEAR and ``tracks.release_year`` feeds the DATE
    tag, while on a QUEUE row ``year`` is the EDITION year. Copying the queue's
    ``year`` straight across would relabel every original-year field with the
    edition year — so the mapping is asserted on both the column and the tag.
    """
    _add_track(track_id="t1", title="Mysterons", file_path="/music/Dummy/01.mp3")
    _add_queue_row(queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3")

    _heal()

    row = _track("t1")
    assert str(row["year"]) == "1994", "``year`` is the release group's original year"
    assert str(row["release_year"]) == "1994", "``release_year`` is the edition year"

    _path, tags = tag_writes[0]
    assert str(tags.get("year")) == "1994", "the DATE tag takes the EDITION year"
    assert str(tags.get("originalyear")) == "1994", "ORIGINALYEAR takes the original year"


def test_a_different_release_sharing_the_album_name_is_left_alone(mb, tag_writes) -> None:
    """THE SCOPE GUARD.

    ``t-other`` is the same artist+album NAME but a different release, and no
    queue row produced it. Healing by album name would drag it onto release A and
    make the two editions indistinguishable — the fault this whole area is
    fixing, in reverse.
    """
    _add_track(track_id="t1", title="Mysterons", file_path="/music/Dummy/01.mp3")
    _add_track(track_id="t-other", title="Roads", file_path="/music/Dummy/09.mp3")
    _add_queue_row(queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3")

    _heal()

    assert _track("t1")["musicbrainz_album_mbid"] == RELEASE_A

    other = _track("t-other")
    assert other["musicbrainz_album_mbid"] == RELEASE_B, (
        "a track from a different release must never be rewritten"
    )
    assert other["barcode"] == "B-BARCODE"
    assert other["musicbrainz_releasegroupid"] == GROUP_B
    assert [p for p, _ in tag_writes] == ["/music/Dummy/01.mp3"], (
        "only the produced track's file may be retagged"
    )


def test_only_the_fields_that_drifted_are_written(mb, tag_writes) -> None:
    """The writers treat an empty value as 'delete this frame'.

    Sending a full payload with uncorrected keys blank would strip tags the heal
    was never asked to touch, so the correction set must be drift-only.
    """
    _add_track(track_id="t1", title="Mysterons", file_path="/music/Dummy/01.mp3")
    _add_queue_row(queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3")

    result = _heal()

    fields = set(result["albums"][0]["fields"])
    _path, tags = tag_writes[0]

    assert fields, "a correction happened"
    # ``disctotal`` is 2 on the row and 1 on the release, so it legitimately
    # drifts; ``album``/``title`` are NOT heal columns at all.
    assert "album" not in tags
    assert "title" not in tags
    assert "album_artist" not in tags


# ---------------------------------------------------------------------------
# Safety rails
# ---------------------------------------------------------------------------

def test_idempotent(mb, tag_writes) -> None:
    _add_track(track_id="t1", title="Mysterons", file_path="/music/Dummy/01.mp3")
    _add_queue_row(queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3")

    first = _heal()
    tag_writes.clear()
    second = _heal()

    assert first["healed"] == 1
    assert second["healed"] == 0
    assert second["drifted"] == 0
    assert second["tracks_updated"] == 0
    assert tag_writes == []


def test_rows_without_a_release_identity_are_skipped(mb, tag_writes) -> None:
    """No release named means no authority to heal toward — never guess."""
    _add_track(track_id="t1", title="Mysterons", file_path="/music/Dummy/01.mp3")
    _add_queue_row(
        queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3", release_mbid=None
    )

    result = _heal()

    assert result["examined"] == 0, "the row is not even a candidate"
    assert result["healed"] == 0
    assert _track("t1")["musicbrainz_album_mbid"] == RELEASE_B
    assert tag_writes == []


def test_rows_that_have_not_landed_are_skipped(mb, tag_writes) -> None:
    """A download still in flight has no library row to compare against."""
    _add_track(track_id="t1", title="Mysterons", file_path="/music/Dummy/01.mp3")
    _add_queue_row(
        queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3", status="downloading"
    )

    result = _heal()

    assert result["examined"] == 0
    assert result["healed"] == 0
    assert _track("t1")["musicbrainz_album_mbid"] == RELEASE_B


def test_a_queue_row_whose_track_is_missing_is_reported_as_skipped(mb, tag_writes) -> None:
    _add_queue_row(queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3")

    result = _heal()

    assert result["examined"] == 1
    assert result["skipped"] == 1
    assert result["healed"] == 0


def test_limit_caps_the_rows_examined(mb, tag_writes) -> None:
    for idx in (1, 2):
        _add_track(track_id=f"t{idx}", title=f"T{idx}", file_path=f"/music/Dummy/0{idx}.mp3")
        _add_queue_row(queue_id=idx, title=f"T{idx}", file_path=f"/music/Dummy/0{idx}.mp3")

    result = _heal(limit=1)

    assert result["examined"] == 1


def test_queue_ids_restricts_the_run(mb, tag_writes) -> None:
    for idx in (1, 2):
        _add_track(track_id=f"t{idx}", title=f"T{idx}", file_path=f"/music/Dummy/0{idx}.mp3")
        _add_queue_row(queue_id=idx, title=f"T{idx}", file_path=f"/music/Dummy/0{idx}.mp3")

    result = _heal(queue_ids=[2])

    assert result["examined"] == 1
    assert _track("t1")["musicbrainz_album_mbid"] == RELEASE_B, "t1 was not selected"
    assert _track("t2")["musicbrainz_album_mbid"] == RELEASE_A


def test_musicbrainz_is_consulted_once_per_release(mb, tag_writes) -> None:
    """One lookup per release, not per track — the MB client is rate-limited."""
    for idx in (1, 2, 3):
        _add_track(track_id=f"t{idx}", title=f"T{idx}", file_path=f"/music/Dummy/0{idx}.mp3")
        _add_queue_row(queue_id=idx, title=f"T{idx}", file_path=f"/music/Dummy/0{idx}.mp3")

    result = _heal()

    assert result["releases"] == 1
    assert result["tracks_updated"] == 3
    assert mb == [RELEASE_A], f"expected exactly one MusicBrainz call, got {mb}"


def test_a_failing_tag_write_is_reported_not_hidden(mb, monkeypatch) -> None:
    """A file that cannot be retagged leaves DB and file tags disagreeing.

    That must be visible: a silent failure is how the original "imported file
    keeps its downloaded tags" report stayed undiagnosed.
    """
    import services.metadata.tag_file_service as tfs

    monkeypatch.setattr(tfs, "resolve_music_file_path", lambda p: p)
    monkeypatch.setattr(tfs, "write_tags_to_file", lambda path, tags: False)

    _add_track(track_id="t1", title="Mysterons", file_path="/music/Dummy/01.mp3")
    _add_queue_row(queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3")

    result = _heal()

    assert result["tracks_updated"] == 1, "the DB row is still corrected"
    assert result["files_written"] == 0
    assert result["files_failed"] == 1, "and the file failure is counted"


def test_an_unreachable_musicbrainz_never_relabels_the_original_year(
    monkeypatch, tag_writes
) -> None:
    """⚠️ THE EDITION-YEAR TRAP.

    On a QUEUE row ``year`` is the EDITION year; on a TRACKS row ``year`` is the
    release group's ORIGINAL year. If the reference copied the queue's ``year``
    across, then with MusicBrainz unreachable there would be nothing to overwrite
    it — and the heal would relabel the original-year field with the edition year
    on every row it touched. The original year is knowable ONLY from
    MusicBrainz, so when that cannot answer the field must be left alone.
    """
    import services.enrichment.musicbrainz_service as mbs

    def _unreachable(release_mbid):
        raise RuntimeError("MusicBrainz is down")

    monkeypatch.setattr(mbs, "fetch_musicbrainz_release_metadata", _unreachable)

    # The track's ORIGINAL year is 1997 in its current state; the queued row's
    # EDITION year is 1994.
    _add_track(track_id="t1", title="Mysterons", file_path="/music/Dummy/01.mp3")
    _add_queue_row(queue_id=1, title="Mysterons", file_path="/music/Dummy/01.mp3")

    _heal()

    row = _track("t1")
    assert str(row["year"]) == "1997", (
        "the original year must not be overwritten with the queue's edition year"
    )
    _path, tags = tag_writes[0] if tag_writes else ("", {})
    assert str(tags.get("originalyear") or "") != "1994", (
        "the ORIGINALYEAR tag must not be given the edition year"
    )
