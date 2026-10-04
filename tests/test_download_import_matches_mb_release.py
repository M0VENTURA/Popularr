"""Regression tests: a downloaded album must import with the SAME MusicBrainz
metadata an album-page lookup would apply.

The reported bug: "the import of tracks after downloading isn't correctly
matching the metadata. It should be applying the same metadata as if I was
doing a lookup album via the album page... So the downloaded album should match
the MBID release that it was matched with originally when adding to the queue."

Root cause: ``_apply_stored_metadata`` applied only the subset of fields the
queue row itself carried (title / artist / album / year / track_number / the
recording MBID).  It never resolved the release MBID, so the ALBUM-level fields
the album page writes for the same release were absent:

    musicbrainz_albumtype, musicbrainz_albumartistid, musicbrainz_albumstatus,
    releasecountry, originalyear

Two additional defects are pinned here:

* the queue row stored only the per-recording enrichment (writer / cover /
  genres) in its ``metadata`` JSON, so even a queue-time-only fix could not
  supply the album fields;
* ``_flatten_release`` never emitted the release ``status`` at all, so
  ``musicbrainz_albumstatus`` had no source on either path.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from db.engine import db_session


def _ensure_queue_table() -> None:
    """Create ``download_queue`` on the suite's shared in-memory database.

    Called at MODULE import, before any test runs.  Doing it in a fixture does
    not work here: the suite shares one in-memory SQLite database and DDL issued
    through ``db_session`` inside a fixture is not visible to the test body.

    ``tracks`` is also ensured because the queue adder probes it for
    "already in the library".

    ⚠️⚠️ ``CREATE TABLE IF NOT EXISTS`` IS NOT ENOUGH.  Another test module
    (``test_download_completion_not_found_loop.py``) creates a MINIMAL
    ``download_queue`` — just ``id/status/found_filename/artist/title``.  Once
    that table exists, every later ``CREATE TABLE IF NOT EXISTS download_queue``
    is a SILENT NO-OP, so this module's richer definition never applies and the
    queue adder's INSERT fails on the missing columns.  The ``ALTER TABLE ADD
    COLUMN`` pass below backfills whatever is absent, which is also what the
    real startup path does (``migrations/ensure_queue_startup_schema.py``).
    """
    from db.models import Track

    columns = {
        "artist": "TEXT", "album": "TEXT", "title": "TEXT",
        "search_query": "TEXT", "source": "TEXT", "status": "TEXT",
        "release_id": "TEXT", "import_group": "TEXT", "album_artist": "TEXT",
        "recording_mbid": "TEXT", "duration": "REAL", "year": "TEXT",
        "release_year": "INTEGER", "metadata": "TEXT",
        "track_number": "TEXT", "disc_number": "TEXT", "priority": "INTEGER",
        "created_at": "TIMESTAMP", "updated_at": "TIMESTAMP",
    }

    with db_session() as session:
        Track.__table__.create(session.get_bind(), checkfirst=True)
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT
            )
        """))
        session.commit()

    # Read the CURRENT columns, then add only what is missing.  Probing first
    # (rather than relying on a failed ALTER being caught) keeps the transaction
    # clean — a failed statement can abort the rest of the batch.
    for name, sql_type in columns.items():
        with db_session() as session:
            existing = {
                str(row[1])
                for row in session.execute(
                    text("PRAGMA table_info(download_queue)")
                ).fetchall()
            }
            if name in existing:
                continue
            try:
                session.execute(
                    text(f"ALTER TABLE download_queue ADD COLUMN {name} {sql_type}")
                )
                session.commit()
            except Exception:
                # Column appeared concurrently, or the dialect refused it —
                # either way the table is usable for this module's assertions.
                pass


_ensure_queue_table()


# ---------------------------------------------------------------------------
# The queue row the fix must be able to enrich
# ---------------------------------------------------------------------------

def _queue_row(**overrides):
    row = {
        "id": 42,
        "artist": "Band Alpha",
        "album_artist": "Various Artists",
        "album": "Now That's Music",
        "title": "Song One",
        "track_number": "1",
        "disc_number": 1,
        "year": 1999,
        "recording_mbid": "rec-a",
        "release_id": "rel-va-1",
        "release_mbid": None,
        "metadata": json.dumps({}),
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1. The MB release flatten must expose the album-level fields
# ---------------------------------------------------------------------------

def _release() -> dict:
    return {
        "id": "rel-va-1",
        "title": "Now That's Music",
        "status": "Official",
        "date": "1999-01-01",
        "country": "GB",
        "artist-credit": [
            {"name": "Various Artists", "joinphrase": "",
             "artist": {"id": "art-va", "name": "Various Artists"}}
        ],
        "release-group": {
            "id": "rg-va-1", "title": "Now That's Music",
            "first-release-date": "1994-06-01",
            "primary-type": "Album", "secondary-types": ["Compilation"],
        },
        "media": [{
            "position": 1,
            "track-count": 1,
            "tracks": [{
                "position": 1, "title": "Song One", "length": 200000,
                "recording": {
                    "id": "rec-a",
                    "isrcs": ["US5TA2600199"],
                    "artist-credit": [{"name": "Band Alpha",
                                       "artist": {"id": "a1", "name": "Band Alpha"}}],
                },
            }],
        }],
    }


class _FakeMbClient:
    def get_release(self, release_id, inc="", **kwargs):
        return _release()


@pytest.fixture
def mb(monkeypatch):
    from services.enrichment import musicbrainz_service as mbs
    monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeMbClient())
    return mbs


class TestFlattenExposesAlbumLevelFields:
    """Every field the album page writes must have a SOURCE in the payload."""

    def test_release_status_is_exposed(self, mb):
        payload = mb.fetch_musicbrainz_release_metadata("rel-va-1")
        assert payload is not None
        assert payload.get("status") == "Official", (
            "without a 'status' key, musicbrainz_albumstatus can never be "
            "written on EITHER the import path or the album page"
        )

    def test_album_type_country_and_original_year_are_exposed(self, mb):
        payload = mb.fetch_musicbrainz_release_metadata("rel-va-1")
        # ``album_type`` is the COMPOSED primary+secondary form; this release is
        # a compilation, so it is "album+compilation" (see
        # ``_compose_album_type``).
        assert payload.get("album_type") == "album+compilation"
        assert payload.get("releasecountry") == "GB"
        # ``original_year`` is the release GROUP's first release year.
        assert str(payload.get("original_year")) == "1994"
        # ...and ``release_year`` is THIS edition's year.
        assert int(payload.get("release_year") or 0) == 1999

    def test_album_artist_mbid_is_exposed(self, mb):
        payload = mb.fetch_musicbrainz_release_metadata("rel-va-1")
        assert payload.get("album_artist_mbid") == "art-va"


# ---------------------------------------------------------------------------
# 2. The import must APPLY those album-level fields
# ---------------------------------------------------------------------------

class TestImportAppliesAlbumLevelMetadata:
    """``_apply_stored_metadata`` must reach parity with the album page."""

    @pytest.fixture(autouse=True)
    def _capture(self, monkeypatch):
        from services.downloads import download_completion_service as dcs
        from services.metadata import tag_file_service as tfs
        from db.repositories import tracks as tracks_repo

        self.tag_writes = []
        self.db_writes = []

        def _fake_tag(path, meta):
            self.tag_writes.append(dict(meta))
            return True

        def _fake_db(track_id, payload, *a, **k):
            self.db_writes.append((track_id, dict(payload)))
            return True

        # ``_apply_stored_metadata`` imports BOTH collaborators INSIDE the
        # function body, so patching the service module's names does nothing —
        # the SOURCE modules must be patched.
        monkeypatch.setattr(tfs, "update_file_metadata", _fake_tag)
        monkeypatch.setattr(tracks_repo, "insert_or_update_track", _fake_db)
        self.dcs = dcs

        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))
            session.execute(
                text(
                    "INSERT INTO tracks (id, artist, album_artist, album, title, "
                    "recording_mbid, musicbrainz_album_mbid) VALUES "
                    "('t-va-1', 'Band Alpha', 'Various Artists', "
                    "'Now That''s Music', 'Song One', 'rec-a', 'rel-va-1')"
                )
            )
        yield
        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))

    def _run(self, row, monkeypatch):
        monkeypatch.setattr(
            self.dcs, "_resolve_track_id_for_import", lambda **kw: "t-va-1"
        )
        self.dcs._apply_stored_metadata(row, "/music/Various Artists/song one.flac")

    def test_album_type_is_written_to_the_file_and_db(self, mb, monkeypatch):
        self._run(_queue_row(), monkeypatch)
        assert self.tag_writes, "no tag write happened"
        for meta in self.tag_writes:
            assert meta.get("musicbrainz_albumtype") == "album+compilation"
        assert any(
            p.get("musicbrainz_albumtype") == "album+compilation"
            for _tid, p in self.db_writes
        )

    def test_album_artist_mbid_is_written(self, mb, monkeypatch):
        self._run(_queue_row(), monkeypatch)
        for meta in self.tag_writes:
            assert meta.get("musicbrainz_albumartistid") == "art-va"

    def test_release_country_and_status_are_written(self, mb, monkeypatch):
        self._run(_queue_row(), monkeypatch)
        for meta in self.tag_writes:
            assert meta.get("releasecountry") == "GB"
            assert meta.get("musicbrainz_albumstatus") == "Official"

    def test_original_year_is_written_separately_from_the_edition_year(
        self, mb, monkeypatch
    ):
        """``originalyear`` (1994) must not be conflated with the edition (1999)."""
        self._run(_queue_row(), monkeypatch)
        for meta in self.tag_writes:
            assert meta.get("originalyear") == "1994"

    def test_per_track_artist_is_not_replaced_by_the_album_artist(
        self, mb, monkeypatch
    ):
        """The VA case: the track keeps Band Alpha, the album is Various Artists."""
        self._run(_queue_row(), monkeypatch)
        for meta in self.tag_writes:
            assert meta.get("artist") == "Band Alpha"
            assert meta.get("album_artist") == "Various Artists"

    def test_the_album_mbid_reaches_the_file(self, mb, monkeypatch):
        self._run(_queue_row(), monkeypatch)
        for meta in self.tag_writes:
            assert meta.get("release_mbid") == "rel-va-1"


# ---------------------------------------------------------------------------
# 3. Parity with the album page — the contract in the bug report
# ---------------------------------------------------------------------------

class TestTagWriterForwardsAlbumFields:
    """The WRITER must forward album-level fields, not just accept them.

    ``_apply_stored_metadata`` passes a payload to ``update_file_metadata``, but
    that function forwards only an explicit key list to the tag writer.  A test
    that mocks ``update_file_metadata`` therefore cannot see the writer dropping
    the album fields — it must run the REAL function and capture what reaches
    ``write_tags_to_file``.
    """

    def test_update_file_metadata_forwards_album_level_fields(self, monkeypatch):
        from services.metadata import tag_file_service as tfs

        captured = {}

        def _fake_write(path, tags):
            captured.update(tags)
            return True

        monkeypatch.setattr(tfs, "write_tags_to_file", _fake_write)

        tfs.update_file_metadata("/music/x.flac", {
            "title": "Song One",
            "artist": "Band Alpha",
            "album_artist": "Various Artists",
            "musicbrainz_albumtype": "album+compilation",
            "musicbrainz_albumstatus": "Official",
            "musicbrainz_albumartistid": "art-va",
            "releasecountry": "GB",
            "originalyear": "1994",
        })

        assert captured, "nothing reached the tag writer"
        assert captured.get("musicbrainz_albumtype") == "album+compilation"
        assert captured.get("musicbrainz_albumstatus") == "Official"
        assert captured.get("musicbrainz_albumartistid") == "art-va"
        assert captured.get("releasecountry") == "GB"
        assert captured.get("originalyear") == "1994"
        # The per-track artist must still be the TRACK artist.
        assert captured.get("artist") == "Band Alpha"
        assert captured.get("album_artist") == "Various Artists"

    def test_internal_key_names_are_translated_for_the_writer(self, monkeypatch):
        """``work_mbid`` / ``release_mbid`` must become the writer's field names."""
        from services.metadata import tag_file_service as tfs

        captured = {}
        monkeypatch.setattr(
            tfs, "write_tags_to_file", lambda p, t: captured.update(t) or True
        )

        tfs.update_file_metadata("/music/y.flac", {
            "recording_mbid": "rec-a",
            "release_mbid": "rel-va-1",
            "work_mbid": "work-1",
        })

        assert captured.get("musicbrainz_trackid") == "rec-a"
        assert captured.get("musicbrainz_albumid") == "rel-va-1"
        assert captured.get("musicbrainz_workid") == "work-1"


ALBUM_PAGE_FIELDS = (
    "musicbrainz_albumartistid",
    "musicbrainz_albumtype",
    "musicbrainz_albumstatus",
    "releasecountry",
    "originalyear",
)


class TestQueueTimePersistence:
    """The other half: the album metadata must be STORED when queueing.

    Without this the import has nothing to apply when MusicBrainz is
    unreachable, and the row's ``metadata`` JSON carried only the per-recording
    enrichment.
    """

    @pytest.fixture(autouse=True)
    def _capture_inserts(self, monkeypatch):
        """Capture the queue INSERT parameters instead of reading them back.

        The suite shares ONE in-memory SQLite database, and DDL issued through
        ``db_session`` in a fixture does not survive to the assertion (or to
        teardown) reliably.  Capturing the bound parameters is both immune to
        that and a stricter assertion: it inspects exactly what the adder tried
        to write.

        ``find_library_track`` is stubbed for the same reason: it queries the
        ``tracks`` table, whose columns depend on whichever OTHER test last
        (re)created it.  Depending on that made this class pass in isolation
        and fail when the whole suite ran.  Stubbing it also sharpens the test
        — it asserts what the adder WRITES, not what an unrelated table holds.
        """
        from sqlalchemy import event
        from db.engine import get_engine

        # Re-ensure the schema per test: the shared in-memory database is
        # recreated whenever the pool hands out a fresh connection, so a
        # module-import-time CREATE is not guaranteed to still be there.
        _ensure_queue_table()

        # ⚠️ MUST clear the table: the adder short-circuits with
        # ``already_active`` when ANY row for the release exists, so a row left
        # by the previous test makes this one silently insert nothing.
        try:
            with db_session() as session:
                session.execute(text("DELETE FROM download_queue"))
                session.commit()
        except Exception:
            # The table is (re)created above; a failure here means the engine
            # was disposed between statements.  The per-test create covers it.
            pass

        from services.queue import queue_processing_service as qps
        monkeypatch.setattr(qps, "find_library_track", lambda **kwargs: None)

        self.inserted = []

        def _before(conn, cursor, statement, params, context, executemany):
            if "INSERT INTO download_queue" in " ".join(statement.split()):
                self.inserted.append(params)

        engine = get_engine()
        event.listen(engine, "before_cursor_execute", _before)
        yield
        event.remove(engine, "before_cursor_execute", _before)

    def _queue(self, **kwargs):
        from services.queue import queue_processing_service as qps
        return qps.add_release_tracks_to_queue_detailed(
            "rel-va-1",
            [{
                "title": "Song One",
                "artist": "Band Alpha",
                "track_number": 1,
                "disc_number": 1,
                "recording_mbid": "rec-a",
            }],
            "Various Artists",
            "Now That's Music",
            album_artist="Various Artists",
            year=1999,
            **kwargs,
        )

    def _stored_metadata(self, params) -> dict:
        """Extract the ``metadata`` JSON from captured INSERT parameters.

        The parameters may arrive as a mapping or a positional tuple (the
        driver's chosen style), so both shapes are handled.
        """
        if isinstance(params, dict):
            raw = params.get("metadata")
        else:
            raw = next(
                (v for v in (params or ()) if isinstance(v, str) and v.strip().startswith("{")),
                None,
            )
        if not raw:
            return {}
        return json.loads(raw) if isinstance(raw, str) else dict(raw)

    def test_album_metadata_is_persisted_on_the_row(self):
        result = self._queue(album_metadata={
            "musicbrainz_albumtype": "album+compilation",
            "musicbrainz_albumstatus": "Official",
            "musicbrainz_albumartistid": "art-va",
            "releasecountry": "GB",
            "originalyear": "1994",
        })
        assert result.get("queued") is True, result
        assert self.inserted, "nothing was inserted"

        stored = self._stored_metadata(self.inserted[0]).get("album_metadata") or {}
        assert stored.get("musicbrainz_albumtype") == "album+compilation"
        assert stored.get("musicbrainz_albumstatus") == "Official"
        assert stored.get("musicbrainz_albumartistid") == "art-va"
        assert stored.get("releasecountry") == "GB"
        assert stored.get("originalyear") == "1994"

    def test_unknown_keys_are_not_persisted(self):
        """Only known album columns are stored — the JSON is not a dumping ground."""
        self._queue(album_metadata={
            "musicbrainz_albumtype": "album",
            "evil_key": "should not persist",
            "artist": "should not override the row artist",
        })
        stored = self._stored_metadata(self.inserted[0]).get("album_metadata") or {}
        assert "evil_key" not in stored
        assert "artist" not in stored
        assert stored.get("musicbrainz_albumtype") == "album"

    def test_queueing_still_works_without_album_metadata(self):
        """Back-compat: omitting the argument must not change behaviour."""
        result = self._queue()
        assert result.get("queued") is True, result
        assert "album_metadata" not in self._stored_metadata(self.inserted[0])

    def test_per_track_artist_is_still_stored_on_the_row(self):
        """The VA requirement: the ROW artist is the track artist."""
        self._queue(album_metadata={"musicbrainz_albumtype": "album"})
        params = self.inserted[0]
        if isinstance(params, dict):
            artist, album_artist = params.get("artist"), params.get("album_artist")
        else:
            positional = [v for v in params if isinstance(v, str)]
            # The INSERT column order starts artist, album, title, search_query,
            # source, release_id, import_group, then album_artist.
            artist = positional[0] if positional else None
            album_artist = "Various Artists" if "Various Artists" in positional else None
        assert artist == "Band Alpha"
        assert album_artist == "Various Artists"


class TestImportUsesStoredAlbumMetadataWithoutMusicBrainz:
    """The import must apply stored values even when MB is unreachable."""

    def test_stored_album_metadata_is_applied_when_fetch_fails(self, monkeypatch):
        from services.downloads import download_completion_service as dcs
        from services.metadata import tag_file_service as tfs
        from db.repositories import tracks as tracks_repo
        from services.enrichment import musicbrainz_service as mbs

        def _boom(_rid):
            raise RuntimeError("MusicBrainz unreachable")

        monkeypatch.setattr(mbs, "fetch_musicbrainz_release_metadata", _boom)

        writes = []
        monkeypatch.setattr(
            tfs, "update_file_metadata", lambda p, m: writes.append(dict(m)) or True
        )
        monkeypatch.setattr(tracks_repo, "insert_or_update_track", lambda *a, **k: True)
        monkeypatch.setattr(dcs, "_resolve_track_id_for_import", lambda **kw: None)

        row = _queue_row(
            metadata=json.dumps({
                "album_metadata": {
                    "musicbrainz_albumtype": "album+compilation",
                    "releasecountry": "GB",
                    "originalyear": "1994",
                }
            })
        )
        dcs._apply_stored_metadata(row, "/music/offline.flac")

        assert writes, "no tag write happened"
        merged = {}
        for meta in writes:
            merged.update(meta)
        assert merged.get("musicbrainz_albumtype") == "album+compilation"
        assert merged.get("releasecountry") == "GB"
        assert merged.get("originalyear") == "1994"

    def test_musicbrainz_values_win_when_the_row_has_none(self, monkeypatch):
        from services.downloads import download_completion_service as dcs
        from services.metadata import tag_file_service as tfs
        from db.repositories import tracks as tracks_repo
        from services.enrichment import musicbrainz_service as mbs

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeMbClient())
        writes = []
        monkeypatch.setattr(
            tfs, "update_file_metadata", lambda p, m: writes.append(dict(m)) or True
        )
        monkeypatch.setattr(tracks_repo, "insert_or_update_track", lambda *a, **k: True)
        monkeypatch.setattr(dcs, "_resolve_track_id_for_import", lambda **kw: None)

        dcs._apply_stored_metadata(_queue_row(), "/music/fresh.flac")

        merged = {}
        for meta in writes:
            merged.update(meta)
        for field in ALBUM_PAGE_FIELDS:
            assert merged.get(field), f"{field} was not applied from MusicBrainz"


class TestImportReachesAlbumPageParity:
    def test_every_album_page_field_lands_on_the_file(self, monkeypatch):
        from services.downloads import download_completion_service as dcs
        from services.metadata import tag_file_service as tfs
        from db.repositories import tracks as tracks_repo

        writes = []
        monkeypatch.setattr(
            tfs, "update_file_metadata", lambda p, m: writes.append(dict(m)) or True
        )
        monkeypatch.setattr(tracks_repo, "insert_or_update_track", lambda *a, **k: True)
        monkeypatch.setattr(
            dcs, "_resolve_track_id_for_import", lambda **kw: "t-parity-1"
        )

        from services.enrichment import musicbrainz_service as mbs
        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeMbClient())

        dcs._apply_stored_metadata(_queue_row(), "/music/parity.flac")

        assert writes, "no tag write happened"
        merged = {}
        for meta in writes:
            merged.update(meta)

        missing = [f for f in ALBUM_PAGE_FIELDS if not merged.get(f)]
        assert not missing, (
            f"the import did not apply {missing} — an album-page lookup writes "
            "all of these for the same release, so a downloaded album would not "
            "match its MBID release"
        )


# ---------------------------------------------------------------------------
# 4. Tag parity: ISRC / track totals / original date / artist MBID
# ---------------------------------------------------------------------------

class TestFlattenExposesPerTrackParityFields:
    """The four fields the coverage audit found missing must have a SOURCE."""

    def test_track_entry_carries_isrc_tracktotal_and_artist_mbid(self, mb):
        payload = mb.fetch_musicbrainz_release_metadata("rel-va-1")
        tracks = payload.get("tracks") or []
        assert tracks, "flatten produced no tracks"
        entry = tracks[0]
        assert entry.get("isrc") == "US5TA2600199", (
            "without isrc on the track entry the import can never write TSRC"
        )
        assert entry.get("tracktotal") == "1", (
            "tracktotal must be the medium's track-count as a string"
        )
        assert entry.get("artist_mbid") == "a1", (
            "artist_mbid must be the TRACK artist's MBID (recording credit first)"
        )

    def test_release_carries_original_date_and_disctotal(self, mb):
        payload = mb.fetch_musicbrainz_release_metadata("rel-va-1")
        assert payload.get("original_date") == "1994-06-01", (
            "original_date is the source for the ORIGINALDATE tag"
        )
        assert payload.get("disctotal") == "1", (
            "disctotal is the number of media (discs)"
        )

    def test_fetch_inc_requests_isrcs(self, mb, monkeypatch):
        """``isrcs`` is NOT implied by ``recordings`` — without it no ISRC."""
        captured = {}

        class _CapturingClient(_FakeMbClient):
            def get_release(self, release_id, inc="", **kwargs):
                captured["inc"] = inc
                return _release()

        import services.enrichment.musicbrainz_service as mbs
        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _CapturingClient())
        mb.fetch_musicbrainz_release_metadata("rel-va-1")
        assert "+isrcs" in (captured.get("inc") or ""), (
            "the release fetch must request isrcs or per-recording ISRC never arrives"
        )


class TestAlbumLevelMappingIncludesNewColumns:
    """``_album_level_mb_fields`` must map originaldate + disctotal."""

    def test_original_date_and_disctotal_are_mapped(self, mb):
        from services.downloads.download_completion_service import (
            _album_level_mb_fields,
        )

        payload = mb.fetch_musicbrainz_release_metadata("rel-va-1")
        mapped = _album_level_mb_fields(payload)
        assert mapped.get("originaldate") == "1994-06-01"
        assert mapped.get("disctotal") == "1"

    def test_new_columns_are_in_album_level_columns(self):
        from services.downloads.download_completion_service import (
            _ALBUM_LEVEL_COLUMNS,
        )

        assert "originaldate" in _ALBUM_LEVEL_COLUMNS
        assert "disctotal" in _ALBUM_LEVEL_COLUMNS


class TestImportAppliesPerTrackParityFields:
    """The import must write isrc / tracktotal / originaldate / disctotal /
    musicbrainz_artistid — the four gaps the coverage audit found."""

    @pytest.fixture(autouse=True)
    def _capture(self, monkeypatch):
        from services.downloads import download_completion_service as dcs
        from services.metadata import tag_file_service as tfs
        from db.repositories import tracks as tracks_repo

        self.tag_writes = []
        self.db_writes = []
        monkeypatch.setattr(
            tfs, "update_file_metadata",
            lambda p, m: self.tag_writes.append(dict(m)) or True,
        )
        monkeypatch.setattr(
            tracks_repo, "insert_or_update_track",
            lambda tid, payload, *a, **k: self.db_writes.append((tid, dict(payload))) or True,
        )
        monkeypatch.setattr(
            dcs, "_resolve_track_id_for_import", lambda **kw: "t-parity-2"
        )
        self.dcs = dcs
        yield

    def _merged(self) -> dict:
        merged: dict = {}
        for meta in self.tag_writes:
            merged.update(meta)
        return merged

    def test_stored_per_track_fields_are_applied(self, monkeypatch):
        """Queue-time stored values reach the writer without any MB fetch."""
        row = _queue_row(metadata=json.dumps({
            "isrc": "US5TA2600199",
            "tracktotal": "17",
            "musicbrainz_artistid": "a1",
            "album_metadata": {
                "musicbrainz_albumtype": "album",
                "musicbrainz_albumstatus": "Official",
                "musicbrainz_albumartistid": "a1",
                "musicbrainz_releasegroupid": "rg-x",
                "releasecountry": "GB",
                "originalyear": "1994",
                "originaldate": "1994-06-01",
                "releasedate": "1999-01-01",
                "disctotal": "2",
                "recordlabel": "Label",
                "catalognumber": "CAT1",
                "barcode": "0123456789012",
                "media": "CD",
            },
        }))
        # Make ANY fetch loudly fail: stored values must suffice.
        from services.enrichment import musicbrainz_service as mbs
        monkeypatch.setattr(
            mbs, "fetch_musicbrainz_release_metadata",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no fetch")),
        )

        self.dcs._apply_stored_metadata(row, "/music/stored.flac")
        merged = self._merged()
        assert merged.get("isrc") == "US5TA2600199"
        assert merged.get("tracktotal") == "17"
        assert merged.get("musicbrainz_artistid") == "a1"
        assert merged.get("originaldate") == "1994-06-01"
        assert merged.get("disctotal") == "2"

    def test_missing_per_track_fields_are_refreshed_from_musicbrainz(
        self, mb, monkeypatch
    ):
        """A queue_add row (no metadata JSON) must still get them from MB."""
        row = _queue_row(metadata=json.dumps({}))
        self.dcs._apply_stored_metadata(row, "/music/refreshed.flac")
        merged = self._merged()
        assert merged.get("isrc") == "US5TA2600199"
        assert merged.get("tracktotal") == "1"
        assert merged.get("musicbrainz_artistid") == "a1"
        # Album-level new columns come from the same fetch.
        assert merged.get("originaldate") == "1994-06-01"
        assert merged.get("disctotal") == "1"

    def test_per_track_fields_reach_the_db_payload(self, mb, monkeypatch):
        row = _queue_row(metadata=json.dumps({}))
        self.dcs._apply_stored_metadata(row, "/music/db.flac")
        payloads = [p for _tid, p in self.db_writes]
        assert payloads, "no tracks-table write happened"
        merged: dict = {}
        for p in payloads:
            merged.update(p)
        assert merged.get("isrc") == "US5TA2600199"
        assert merged.get("tracktotal") == "1"
        assert merged.get("musicbrainz_artistid") == "a1"
        assert merged.get("originaldate") == "1994-06-01"
        assert merged.get("disctotal") == "1"

    def test_never_overwrites_stored_values_with_refresh(self, monkeypatch):
        """Stored isrc wins; the refresh must not replace it."""
        row = _queue_row(metadata=json.dumps({
            "isrc": "STORED123456",
            "tracktotal": "12",
            "musicbrainz_artistid": "stored-artist",
        }))
        self.dcs._apply_stored_metadata(row, "/music/nowin.flac")
        merged = self._merged()
        assert merged.get("isrc") == "STORED123456"
        assert merged.get("tracktotal") == "12"
        assert merged.get("musicbrainz_artistid") == "stored-artist"


class TestUpdateFileMetadataForwardsPerTrackFields:
    """The WRITER must receive isrc / tracktotal / disctotal / artist MBID."""

    def test_per_track_and_total_fields_reach_the_writer(self, monkeypatch):
        from services.metadata import tag_file_service as tfs

        captured = {}
        monkeypatch.setattr(
            tfs, "write_tags_to_file", lambda p, t: captured.update(t) or True
        )

        tfs.update_file_metadata("/music/parity2.flac", {
            "title": "Song One",
            "isrc": "US5TA2600199",
            "tracktotal": "17",
            "disctotal": "2",
            "musicbrainz_artistid": "a1",
            "originaldate": "1994-06-01",
        })

        assert captured, "nothing reached the tag writer"
        assert captured.get("isrc") == "US5TA2600199"
        assert captured.get("tracktotal") == "17"
        assert captured.get("disctotal") == "2"
        assert captured.get("musicbrainz_artistid") == "a1"
        assert captured.get("originaldate") == "1994-06-01"

    def test_absent_fields_are_never_forwarded_as_none(self, monkeypatch):
        """The None-means-suppress contract must hold for the new fields too."""
        from services.metadata import tag_file_service as tfs

        captured = {}
        monkeypatch.setattr(
            tfs, "write_tags_to_file", lambda p, t: captured.update(t) or True
        )

        tfs.update_file_metadata("/music/parity3.flac", {
            "title": "Song One",
            "isrc": None,
            "tracktotal": None,
            "musicbrainz_artistid": None,
        })

        assert "isrc" not in captured
        assert "tracktotal" not in captured
        assert "musicbrainz_artistid" not in captured


class TestQueueTimePersistsPerTrackEnrichment:
    """The queue adder must store isrc / tracktotal / artist MBID on the row."""

    @pytest.fixture(autouse=True)
    def _capture_inserts(self, monkeypatch):
        from sqlalchemy import event
        from db.engine import get_engine

        _ensure_queue_table()
        try:
            with db_session() as session:
                session.execute(text("DELETE FROM download_queue"))
                session.commit()
        except Exception:
            pass

        from services.queue import queue_processing_service as qps
        monkeypatch.setattr(qps, "find_library_track", lambda **kwargs: None)

        self.inserted = []

        def _before(conn, cursor, statement, params, context, executemany):
            if "INSERT INTO download_queue" in " ".join(statement.split()):
                self.inserted.append(params)

        engine = get_engine()
        event.listen(engine, "before_cursor_execute", _before)
        yield
        event.remove(engine, "before_cursor_execute", _before)

    def _stored_metadata(self, params) -> dict:
        if isinstance(params, dict):
            raw = params.get("metadata")
        else:
            raw = next(
                (v for v in (params or ())
                 if isinstance(v, str) and v.strip().startswith("{")),
                None,
            )
        if not raw:
            return {}
        return json.loads(raw) if isinstance(raw, str) else dict(raw)

    def test_per_track_enrichment_is_persisted(self):
        from services.queue import queue_processing_service as qps

        result = qps.add_release_tracks_to_queue_detailed(
            "rel-va-1",
            [{
                "title": "Song One",
                "artist": "Band Alpha",
                "track_number": 1,
                "disc_number": 1,
                "recording_mbid": "rec-a",
                "isrc": "US5TA2600199",
                "tracktotal": "17",
                "artist_mbid": "a1",
            }],
            "Various Artists",
            "Now That's Music",
            album_artist="Various Artists",
            year=1999,
        )
        assert result.get("queued") is True, result
        assert self.inserted, "nothing was inserted"

        stored = self._stored_metadata(self.inserted[0])
        assert stored.get("isrc") == "US5TA2600199"
        assert stored.get("tracktotal") == "17"
        assert stored.get("musicbrainz_artistid") == "a1", (
            "artist_mbid from the flatten must be stored as musicbrainz_artistid"
        )

    def test_absent_enrichment_is_not_stored(self):
        from services.queue import queue_processing_service as qps

        qps.add_release_tracks_to_queue_detailed(
            "rel-va-1",
            [{
                "title": "Song Two",
                "artist": "Band Alpha",
                "track_number": 2,
                "recording_mbid": "rec-b",
            }],
            "Various Artists",
            "Now That's Music",
            album_artist="Various Artists",
            year=1999,
        )
        assert self.inserted, "nothing was inserted"
        stored = self._stored_metadata(self.inserted[0])
        assert "isrc" not in stored
        assert "tracktotal" not in stored
        assert "musicbrainz_artistid" not in stored

