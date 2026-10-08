"""A real ``download_queue.musicbrainz_releasegroupid`` column + an import source map.

Two asks, one theme: **stop depending on a JSON blob, and stop guessing.**

1. THE COLUMN.  The release group lived only inside ``download_queue.metadata``
   (``metadata.album_metadata``) and was read back as the *second* source of
   ``_resolve_album_level_metadata``.  Any queue path that does not build that
   blob — the single-row ``queue_add`` fallback in ``api_musicbrainz_download``,
   a re-queue that knows only artist/title/album — left the key absent, and
   nothing could read or write it as a column.  The artist page has keyed albums
   on ``musicbrainz_releasegroupid`` since
   2026-10-06-albums-split-by-release-group, so such a row imported as its OWN
   album next to the release it was downloaded for.

   It is now a real column, written at queue time alongside ``release_mbid`` /
   ``recording_mbid``, and it is the FIRST source the import consults.  Rows
   queued before the migration keep working through the blob / sibling /
   MusicBrainz paths.

2. THE DIAGNOSTIC.  ``_resolve_album_level_metadata`` consults three sources
   (stored → sibling → MusicBrainz) and used to log nothing at all unless the
   last one failed outright.  It now writes ONE line per import naming which
   source supplied each field:

       [QUEUE] album metadata sources stored=[…] sibling=[…] MusicBrainz=[…] missing=[…]

   so the next import answers "where did this come from?" instead of us
   guessing which of the three paths silently returned nothing.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.downloads import download_completion_service as dcs  # noqa: E402
from services.metadata import tag_file_service as tfs  # noqa: E402

RG = "11111111-1111-1111-1111-111111111111"
FAKE_PATH = "/music/Powderfinger/2004 - Fingerprints/01 - Track.mp3"

#: The four labels the diagnostic must always be able to print, in resolver order.
FOUR_SOURCES = ("stored", "sibling", "MusicBrainz", "missing")


# ===========================================================================
# 1. The column is declared everywhere the schema is declared
# ===========================================================================


class TestTheColumnIsDeclaredEverywhereTheSchemaIs:
    """Four declarations, four different failure modes if one is missed."""

    def test_the_column_registry_declares_it(self):
        from db.schema import COLUMN_REGISTRY

        ddl = COLUMN_REGISTRY["download_queue"].get("musicbrainz_releasegroupid")
        assert ddl, (
            "db/schema.py drives `_ensure_columns`, so an unlisted column is "
            "never added to an existing install"
        )
        assert "TEXT" in ddl.upper()

    def test_the_orm_model_declares_it(self):
        """ORM ↔ registry drift is invisible until a query selects it.

        ``tests/conftest.py`` builds ``tracks`` from the ORM, and the model is
        what ``SELECT *`` maps back onto — a registry-only column cannot be
        read through the ORM.
        """
        from db.models import DownloadQueue

        assert "musicbrainz_releasegroupid" in DownloadQueue.__table__.c, (
            "the registry and the ORM must agree"
        )

    def test_the_migration_adds_it_and_revises_016(self):
        """The Alembic path must exist independently of the runtime bootstrap."""
        source = (
            Path(REPO_ROOT) / "migrations" / "versions"
            / "017_add_download_queue_release_group.py"
        ).read_text(encoding="utf-8")
        assert 'down_revision: Union[str, None] = "016_add_tracks_raw_score"' in source
        assert 'op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))' in source
        assert "_COLUMN = \"musicbrainz_releasegroupid\"" in source
        # Inspector-guarded: the chain also runs against the SQLite test engine.
        assert "_existing_columns()" in source

    def test_the_startup_schema_helper_ensures_it(self):
        """``entrypoint.sh`` runs this BEFORE the app starts."""
        source = (
            Path(REPO_ROOT) / "migrations" / "ensure_queue_startup_schema.py"
        ).read_text(encoding="utf-8")
        assert '"musicbrainz_releasegroupid": "TEXT"' in source


# ===========================================================================
# 2. It is populated at queue time
# ===========================================================================


class _DBSession:
    """``db_session()`` stand-in over a real SQLite engine (commits on exit)."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def __call__(self):  # noqa: ANN001
        return self._engine.begin()


@pytest.fixture()
def queue_engine(tmp_path: Any) -> Iterator[Any]:
    """A real ``download_queue`` holding every column the queue INSERTs."""
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE download_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artist TEXT, title TEXT, album TEXT, album_artist TEXT,
                    source TEXT DEFAULT 'soulseek', status TEXT DEFAULT 'queued',
                    priority INTEGER DEFAULT 5,
                    track_number TEXT, disc_number TEXT,
                    year TEXT, release_year INTEGER,
                    release_id TEXT, release_mbid TEXT, recording_mbid TEXT,
                    musicbrainz_releasegroupid TEXT,
                    duration REAL, import_group TEXT, import_type TEXT,
                    metadata TEXT, search_query TEXT,
                    file_path TEXT, found_filename TEXT,
                    created_at TIMESTAMP, updated_at TIMESTAMP
                )
                """
            )
        )
    yield engine
    engine.dispose()


def _queue_rows(engine: Any, order_by: str = "id") -> list[dict[str, Any]]:
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT * FROM download_queue ORDER BY {order_by}")
        ).fetchall()
        return [dict(r._mapping) for r in rows]


class TestItIsPopulatedAtQueueTime:
    def test_the_mb_release_adder_writes_the_column(
        self, queue_engine: Any, monkeypatch: Any
    ) -> None:
        """THE queue-time path: the release group travels with ``album_metadata``."""
        from services.queue import queue_processing_service as qps

        monkeypatch.setattr(qps, "db_session", _DBSession(queue_engine))
        monkeypatch.setattr(qps, "find_library_track", lambda **k: None)
        monkeypatch.setattr(qps, "signal_new_item", lambda: None)

        result = qps.add_release_tracks_to_queue_detailed(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            [{"title": "Track", "track_number": 1, "recording_mbid": "rec-1"}],
            "Powderfinger",
            "Fingerprints",
            album_metadata={
                "musicbrainz_releasegroupid": RG,
                "musicbrainz_albumtype": "Album",
            },
        )
        assert result["queue_ids"], f"nothing was queued: {result}"

        rows = _queue_rows(queue_engine)
        assert rows[0]["musicbrainz_releasegroupid"] == RG, (
            "the release group must be a COLUMN, not only a key in `metadata`"
        )
        # The blob still carries it — this is additive, not a replacement.
        blob = json.loads(rows[0]["metadata"] or "{}")
        assert blob["album_metadata"]["musicbrainz_releasegroupid"] == RG

    def test_a_row_queued_without_album_metadata_leaves_the_column_null(
        self, queue_engine: Any, monkeypatch: Any
    ) -> None:
        """CONTROL — no release known ⇒ no invented id (the column may be NULL)."""
        from services.queue import queue_processing_service as qps

        monkeypatch.setattr(qps, "db_session", _DBSession(queue_engine))
        monkeypatch.setattr(qps, "find_library_track", lambda **k: None)
        monkeypatch.setattr(qps, "signal_new_item", lambda: None)

        qps.add_release_tracks_to_queue_detailed(
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            [{"title": "Track", "track_number": 1, "recording_mbid": "rec-2"}],
            "Powderfinger",
            "Fingerprints",
        )
        rows = _queue_rows(queue_engine)
        assert rows[0]["musicbrainz_releasegroupid"] in (None, "")

    def test_insert_queue_item_stores_it(
        self, queue_engine: Any, monkeypatch: Any
    ) -> None:
        """The single-row path (playlist import, album-page add, re-queue)."""
        from db.repositories import queue as queue_repo

        monkeypatch.setattr(queue_repo, "db_session", _DBSession(queue_engine))
        row = queue_repo.insert_queue_item(
            artist="Powderfinger",
            title="Track",
            album="Fingerprints",
            musicbrainz_releasegroupid=RG,
        )
        assert row.get("musicbrainz_releasegroupid") == RG, (
            "insert_queue_item must accept the field the way it accepts "
            "release_mbid / recording_mbid"
        )

    def test_queue_add_forwards_the_payload_field(self, monkeypatch: Any) -> None:
        """The UI payload path — dropping it here was how it used to vanish."""
        from services.queue import queue_processing_service as qps

        captured: dict[str, Any] = {}

        def _fake(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"id": 7}

        monkeypatch.setattr(qps, "insert_queue_item", _fake)
        qps.queue_add(
            {"artist": "A", "title": "T", "musicbrainz_releasegroupid": RG}
        )
        assert captured.get("musicbrainz_releasegroupid") == RG

    def test_update_queue_item_can_write_it(
        self, queue_engine: Any, monkeypatch: Any
    ) -> None:
        """Backfilling an existing row must not need to parse JSON first."""
        from db.repositories import queue as queue_repo

        monkeypatch.setattr(queue_repo, "db_session", _DBSession(queue_engine))
        row = queue_repo.insert_queue_item(artist="A", title="T", album="Al")
        queue_repo.update_queue_item(row["id"], musicbrainz_releasegroupid=RG)

        assert _queue_rows(queue_engine)[0]["musicbrainz_releasegroupid"] == RG

    def test_the_sentinel_key_is_not_updatable(self) -> None:
        """The private diagnostic flag must never reach a real column."""
        from db.repositories.queue import UPDATE_ALLOWED_COLUMNS

        assert "musicbrainz_releasegroupid" in UPDATE_ALLOWED_COLUMNS
        assert dcs._SOURCE_MAP_LOGGED_KEY not in UPDATE_ALLOWED_COLUMNS


# ===========================================================================
# 3. The import reads it first, and a fallback cannot lose it
# ===========================================================================


class _Row(dict):
    @property
    def _mapping(self):  # mirrors SQLAlchemy's Row mapping
        return self


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _Session:
    def __init__(self, row: Any) -> None:
        self._row = row

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _Result:
        return _Result(self._row)


class _Ctx:
    """``db_session`` stand-in returning a canned sibling row (or none)."""

    def __init__(self, row: Any) -> None:
        self._row = row

    def __enter__(self) -> _Session:
        return _Session(self._row)

    def __exit__(self, *exc: Any) -> bool:
        return False


@pytest.fixture
def capture(monkeypatch: Any) -> dict[str, Any]:
    """Capture the metadata the import would write; no file I/O, no network."""
    captured: dict[str, Any] = {}

    def _update(file_path: str, metadata: Any) -> bool:  # noqa: ANN001
        captured["meta"] = dict(metadata or {})
        return True

    monkeypatch.setattr(tfs, "update_file_metadata", _update)
    monkeypatch.setattr(tfs, "write_tags_to_file", lambda *a, **k: True)
    return captured


@pytest.fixture
def no_siblings(monkeypatch: Any) -> None:
    monkeypatch.setattr(dcs, "db_session", lambda: _Ctx(None))


@pytest.fixture
def sibling_row(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        dcs,
        "db_session",
        lambda: _Ctx(_Row({"recordlabel": "Universal", "disctotal": "1"})),
    )


@pytest.fixture
def mb_down(monkeypatch: Any) -> None:
    """MusicBrainz unreachable — the state the report's file was written in."""
    import services.enrichment.musicbrainz_service as mb

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("MusicBrainz overloaded")

    monkeypatch.setattr(mb, "fetch_musicbrainz_release_metadata", _boom)


@pytest.fixture
def mb_answers(monkeypatch: Any) -> None:
    """MusicBrainz answers — the third source must be attributable too."""
    import services.enrichment.musicbrainz_service as mb

    def _ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"album_type": "Album+Compilation", "status": "Official"}

    monkeypatch.setattr(mb, "fetch_musicbrainz_release_metadata", _ok)


@pytest.fixture
def unified_log() -> Iterator[list[str]]:
    """Every line ``log_unified`` writes to the unified scan log."""
    records: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger_ = logging.getLogger("popularr.unified")
    handler = _Handler()
    previous_level = logger_.level
    logger_.addHandler(handler)
    logger_.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger_.removeHandler(handler)
        logger_.setLevel(previous_level)


def make_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": 42,
        "artist": "Powderfinger",
        "album_artist": "Powderfinger",
        "album": "Fingerprints",
        "title": "Track",
        "track_number": 1,
        "disc_number": 1,
        "year": 2004,
        "release_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "release_mbid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "recording_mbid": "22222222-2222-2222-2222-222222222222",
        "metadata": {},
    }
    item.update(overrides)
    return item


def _source_lines(records: list[str]) -> list[str]:
    return [r for r in records if "[QUEUE] album metadata sources" in r]


class TestTheFallbackCannotLoseTheReleaseGroup:
    """THE point of the column: no blob ⇒ still resolved, from the row itself."""

    def test_a_row_with_no_metadata_blob_still_resolves_from_the_column(
        self, no_siblings: None, mb_down: None
    ) -> None:
        item = make_item(musicbrainz_releasegroupid=RG, metadata=None)
        resolved = dcs._resolve_album_level_metadata(item)
        assert resolved["musicbrainz_releasegroupid"] == RG

    def test_the_column_is_consulted_before_the_blob(
        self, no_siblings: None, mb_down: None
    ) -> None:
        """CONTROL — a stale blob must not shadow the queue-time write."""
        item = make_item(
            musicbrainz_releasegroupid=RG,
            metadata={"album_metadata": {"musicbrainz_releasegroupid": "stale-rg"}},
        )
        resolved = dcs._resolve_album_level_metadata(item)
        assert resolved["musicbrainz_releasegroupid"] == RG

    def test_nothing_is_invented_when_no_source_has_it(
        self, no_siblings: None, mb_down: None
    ) -> None:
        """CONTROL — the column existing must not make empty rows look filled."""
        resolved = dcs._resolve_album_level_metadata(
            make_item(musicbrainz_releasegroupid="", metadata=None)
        )
        assert not resolved.get("musicbrainz_releasegroupid")

    def test_the_import_still_writes_it_onto_the_file(
        self, capture: dict[str, Any], no_siblings: None, mb_down: None
    ) -> None:
        dcs._apply_stored_metadata(
            make_item(musicbrainz_releasegroupid=RG, metadata=None), FAKE_PATH
        )
        assert capture["meta"]["musicbrainz_releasegroupid"] == RG


# ===========================================================================
# 4. The diagnostic — one line per import, naming every source
# ===========================================================================


class TestTheDiagnosticNamesEverySource:
    def test_the_formatter_always_names_all_four_labels(self) -> None:
        """Empty groups must print, not vanish — the absence IS the signal."""
        rendered = dcs._describe_album_source_map({})
        for label in FOUR_SOURCES:
            assert f"{label}=[" in rendered, f"{label} missing from {rendered!r}"
        assert rendered == "stored=[-] sibling=[-] MusicBrainz=[-] missing=[-]"

    def test_one_line_covers_the_field_a_report_asked_about(
        self,
        capture: dict[str, Any],
        unified_log: list[str],
        sibling_row: None,
        mb_down: None,
    ) -> None:
        """stored / MusicBrainz / sibling / missing in ONE line."""
        dcs._apply_stored_metadata(
            make_item(musicbrainz_releasegroupid=RG), FAKE_PATH
        )
        lines = _source_lines(unified_log)
        assert len(lines) == 1, f"expected one diagnostic line, got {lines}"
        line = lines[0]

        assert "stored=[" in line and "sibling=[" in line
        assert "MusicBrainz=[" in line and "missing=[" in line
        # The field itself is attributed to the queue row's own column.
        stored_group = line.split("stored=[", 1)[1].split("]", 1)[0]
        assert "musicbrainz_releasegroupid" in stored_group.split(), (
            f"the release group was not attributed to `stored`: {line}"
        )
        sibling_group = line.split("sibling=[", 1)[1].split("]", 1)[0]
        assert "recordlabel" in sibling_group.split()

    def test_musicbrainz_is_named_only_when_it_actually_answers(
        self,
        capture: dict[str, Any],
        unified_log: list[str],
        no_siblings: None,
        mb_answers: None,
    ) -> None:
        dcs._apply_stored_metadata(make_item(), FAKE_PATH)
        line = _source_lines(unified_log)[0]

        mb_group = line.split("MusicBrainz=[", 1)[1].split("]", 1)[0]
        assert "musicbrainz_albumtype" in mb_group.split(), (
            f"MusicBrainz answered but was not credited: {line}"
        )
        # …and the fields nothing supplied stay named as missing.
        missing_group = line.split("missing=[", 1)[1].split("]", 1)[0]
        assert "barcode" in missing_group.split(), (
            f"an unfilled field disappeared instead of being reported: {line}"
        )

    def test_exactly_one_line_per_import(
        self,
        capture: dict[str, Any],
        unified_log: list[str],
        no_siblings: None,
        mb_down: None,
    ) -> None:
        """``_apply_stored_metadata`` runs pre-move AND post-move on one row."""
        item = make_item()
        dcs._apply_stored_metadata(item, FAKE_PATH)
        dcs._apply_stored_metadata(item, FAKE_PATH)
        assert len(_source_lines(unified_log)) == 1, _source_lines(unified_log)

    def test_a_different_row_gets_its_own_line(
        self,
        capture: dict[str, Any],
        unified_log: list[str],
        no_siblings: None,
        mb_down: None,
    ) -> None:
        """CONTROL — de-duping must be per import, not per process."""
        dcs._apply_stored_metadata(make_item(id=1), FAKE_PATH)
        dcs._apply_stored_metadata(make_item(id=2), FAKE_PATH)
        assert len(_source_lines(unified_log)) == 2

    def test_the_import_still_works_when_the_log_channel_is_unwritable(
        self, capture: dict[str, Any], no_siblings: None, mb_down: None, monkeypatch: Any
    ) -> None:
        """A diagnostic must never be able to break the import it describes."""
        import helpers.logging_config as logging_config

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("logger exploded")

        monkeypatch.setattr(logging_config, "log_unified", _boom)
        meta = dcs._apply_stored_metadata(make_item(), FAKE_PATH)
        assert isinstance(meta, bool), "the import must return, not raise"

    def test_the_line_is_written_to_the_unified_log_channel(self) -> None:
        """It must sit next to the neighbouring ``[QUEUE] … imported`` line."""
        source = (
            Path(REPO_ROOT) / "services" / "downloads"
            / "download_completion_service.py"
        ).read_text(encoding="utf-8")
        resolver = source[
            source.index("def _resolve_album_level_metadata") :
            source.index("def _match_release_track")
        ]
        assert "from helpers.logging_config import log_unified" in resolver
        assert resolver.index("_describe_album_source_map(sources)") < resolver.index(
            "return resolved"
        ), "the diagnostic must be emitted before the resolver returns"
