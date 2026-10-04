"""Regression tests: queue rows must carry year/track number into the import.

The reported bug:

    /music/Stray Kids/Unknown - SKZ‐REPLAY 2026 Pt.1/00. Stray Kids - LOVER.mp3

...imported into an ``Unknown - <album>`` folder with a ``00.`` file prefix,
i.e. the queue row had NO ``year`` and NO ``track_number``.  Two compounding
defects caused it:

1. ``queue_add`` (the single-item ``/api/queue/add`` endpoint) forwarded only
   artist/title/album/source/priority — the album page's missing-track button
   and the dashboard also sent ``year``/``track_number``/``release_mbid``, but
   they were dropped on the floor (``queue_add_batch`` always forwarded them).
2. ``update_file_metadata`` unconditionally re-added the base fields with
   ``None`` when the caller did not supply them; the writers treat ``None`` as
   "delete this frame", so an incomplete queue row actively WIPED the
   downloaded file's own date/track-number frames instead of leaving them.

Rows queued through the broken path before the fix are healed at import time
by ``_resolve_missing_identity`` (row columns → stored album metadata →
MusicBrainz release refresh → the source file's own tags/filename), with the
fills persisted back to the queue row so path reconciliation agrees.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services.downloads import download_completion_service as dcs
from services.downloads.download_organize_helpers import _build_target_path
from services.queue import queue_processing_service as qps


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Row:
    def __init__(self, mapping: dict):
        self._mapping = mapping


def _install_fake_db(monkeypatch, rowcount: int = 1):
    """Fake module-level ``db_session``: the claim UPDATE reports rowcount."""
    class _Result:
        def __init__(self):
            self.rowcount = rowcount

        def fetchall(self):
            return []

    class _Session:
        def execute(self, stmt, params=None):
            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Fake:
        def __call__(self):
            return _Session()

    monkeypatch.setattr(dcs, "db_session", _Fake())


def _install_update_recorder(monkeypatch):
    calls: list[tuple[int, dict]] = []

    def fake_update(queue_id, **kwargs):
        calls.append((queue_id, kwargs))
        return {"id": queue_id, **kwargs}

    monkeypatch.setattr("db.repositories.queue.update_queue_item", fake_update)
    return calls


# ---------------------------------------------------------------------------
# 1. queue_add must forward the identity payload
# ---------------------------------------------------------------------------

class TestQueueAddForwardsIdentity:
    def test_year_track_and_mbids_reach_the_insert(self, monkeypatch):
        captured: dict = {}

        def fake_add_to_queue(**kwargs):
            captured.update(kwargs)
            return {"success": True, "inserted": True}

        monkeypatch.setattr(qps, "add_to_queue", fake_add_to_queue)

        qps.queue_add({
            "artist": "Stray Kids",
            "title": "LOVER",
            "album": "SKZ‐REPLAY 2026 Pt.1",
            "album_artist": "Stray Kids",
            "year": "2026",
            "track_number": "3",
            "disc_number": "1",
            "release_mbid": "rel-1",
            "release_id": "rel-1",
            "recording_mbid": "rec-3",
            "duration": 183000,
            "import_type": "song",
            "source": "soulseek",
            "priority": 7,
        })

        # The regression: every one of these used to be silently dropped, so
        # the row imported as "Unknown - <album>/00. ..." with no MB identity.
        assert captured.get("year") == "2026"
        assert captured.get("track_number") == "3"
        assert captured.get("disc_number") == "1"
        assert captured.get("album_artist") == "Stray Kids"
        assert captured.get("release_mbid") == "rel-1"
        assert captured.get("release_id") == "rel-1"
        assert captured.get("recording_mbid") == "rec-3"
        assert captured.get("duration") == 183000
        assert captured.get("import_type") == "song"
        # base fields still forwarded
        assert captured.get("artist") == "Stray Kids"
        assert captured.get("title") == "LOVER"
        assert captured.get("priority") == 7

    def test_empty_strings_normalize_to_null(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            qps, "add_to_queue", lambda **kw: captured.update(kw) or {"success": True}
        )

        # missing_releases.html renders String(t.track_number || '') — an empty
        # dataset attribute must store NULL, not an unparseable "".
        qps.queue_add({"artist": "A", "title": "T", "album": "Al", "track_number": ""})

        assert captured.get("track_number") is None
        assert captured.get("year") is None
        assert captured.get("release_mbid") is None

    def test_minimal_payload_still_works(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            qps, "add_to_queue", lambda **kw: captured.update(kw) or {"success": True}
        )

        result = qps.queue_add({"artist": "A", "title": "T"})

        assert result["success"] is True
        assert captured.get("artist") == "A"
        assert captured.get("title") == "T"
        assert captured.get("album") is None


# ---------------------------------------------------------------------------
# 2. update_file_metadata must not turn "not supplied" into a frame delete
# ---------------------------------------------------------------------------

class TestUpdateFileMetadataNeverDeletesAbsentFields:
    @pytest.fixture()
    def capture(self, monkeypatch):
        written: list[dict] = []

        def fake_write(path, tags):
            written.append(dict(tags))
            return True

        monkeypatch.setattr(
            "services.metadata.tag_file_service.write_tags_to_file", fake_write
        )
        return written

    def test_absent_base_fields_are_not_forwarded(self, capture):
        from services.metadata.tag_file_service import update_file_metadata

        update_file_metadata("/music/x.mp3", {"artist": "New Artist"})

        assert capture, "no write happened"
        tags = capture[-1]
        # The regression: these used to arrive as None → the writers DELETED
        # the frames the downloaded file already had.
        assert "year" not in tags
        assert "track_number" not in tags
        assert "disc_number" not in tags
        assert "title" not in tags
        assert "album" not in tags
        assert tags["artist"] == "New Artist"

    def test_explicit_none_is_treated_as_absent(self, capture):
        from services.metadata.tag_file_service import update_file_metadata

        update_file_metadata(
            "/music/x.mp3", {"title": "T", "year": None, "track_number": None}
        )

        tags = capture[-1]
        assert tags["title"] == "T"
        assert "year" not in tags
        assert "track_number" not in tags

    def test_explicit_empty_string_still_clears(self, capture):
        from services.metadata.tag_file_service import update_file_metadata

        # The single-disc disc_number="" clear must survive the None filter.
        update_file_metadata("/music/x.mp3", {"disc_number": "", "title": "T"})

        tags = capture[-1]
        assert tags["disc_number"] == ""
        assert tags["title"] == "T"

    def test_supplied_values_are_written(self, capture):
        from services.metadata.tag_file_service import update_file_metadata

        update_file_metadata(
            "/music/x.mp3",
            {"title": "Lover", "track_number": "3", "release_year": 2026, "year": 2018},
        )

        tags = capture[-1]
        assert tags["title"] == "Lover"
        assert tags["track_number"] == "3"
        # release_year (edition) wins the DATE tag; year falls back to originalyear.
        assert tags["year"] == 2026
        assert tags["originalyear"] == 2018


# ---------------------------------------------------------------------------
# 3. _resolve_missing_identity — the import-time backfill
# ---------------------------------------------------------------------------

class TestResolveMissingIdentity:
    def _row(self, **overrides) -> dict:
        row = {
            "id": 42,
            "artist": "Stray Kids",
            "album_artist": "Stray Kids",
            "album": "SKZ‐REPLAY 2026 Pt.1",
            "title": "LOVER",
            "year": None,
            "track_number": None,
            "disc_number": None,
            "release_year": None,
            "release_date": None,
            "release_mbid": None,
            "release_id": None,
            "recording_mbid": None,
            "metadata": None,
        }
        row.update(overrides)
        return row

    def test_complete_row_returns_nothing_and_never_fetches(self, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("network must not be touched for a complete row")

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
            explode,
        )
        fills = dcs._resolve_missing_identity(
            self._row(year="2026", track_number="3")
        )
        assert fills == {}

    def test_release_columns_fill_year(self):
        fills = dcs._resolve_missing_identity(self._row(release_year=2026))
        assert fills.get("year") == "2026"

        fills = dcs._resolve_missing_identity(
            self._row(release_date="2026-07-31")
        )
        assert fills.get("year") == "2026"

    def test_stored_album_metadata_fills_year(self):
        stored = json.dumps(
            {"album_metadata": {"releasedate": "2026-07-31", "originalyear": "2026"}}
        )
        fills = dcs._resolve_missing_identity(self._row(metadata=stored))
        assert fills.get("year") == "2026", "releasedate (edition) must win"

    def test_stored_originalyear_is_the_fallback(self):
        stored = json.dumps({"album_metadata": {"originalyear": "1994"}})
        fills = dcs._resolve_missing_identity(self._row(metadata=stored))
        assert fills.get("year") == "1994"

    @pytest.fixture()
    def mb_release(self, monkeypatch):
        """Fake flattened release: LOVER is disc 1 track 3."""
        release = {
            "release_mbid": "rel-1",
            "release_year": 2026,
            "releasedate": "2026-07-31",
            "original_year": "2026",
            "tracks": [
                {"recording_mbid": "rec-battle", "track_number": 1,
                 "disc_number": 1, "title": "Battle Ground"},
                {"recording_mbid": "rec-lover", "track_number": 3,
                 "disc_number": 1, "title": "LOVER"},
            ],
        }
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
            lambda mbid: release,
        )
        return release

    def test_musicbrainz_refresh_fills_year_and_track_by_recording_mbid(
        self, mb_release
    ):
        fills = dcs._resolve_missing_identity(
            self._row(release_mbid="rel-1", recording_mbid="rec-lover")
        )
        assert fills.get("year") == "2026"
        assert fills.get("track_number") == 3
        assert fills.get("disc_number") == 1

    def test_musicbrainz_refresh_matches_by_title_without_mbid(self, mb_release):
        fills = dcs._resolve_missing_identity(self._row(release_mbid="rel-1"))
        assert fills.get("year") == "2026"
        assert fills.get("track_number") == 3

    def test_unknown_title_leaves_track_number_alone(self, mb_release):
        fills = dcs._resolve_missing_identity(
            self._row(release_mbid="rel-1", title="Not On This Release")
        )
        # Never guess a track number: a wrong one renames the file wrongly.
        assert "track_number" not in fills
        assert fills.get("year") == "2026"

    def test_existing_values_are_never_overwritten(self, mb_release):
        fills = dcs._resolve_missing_identity(
            self._row(year="2019", track_number="7", release_mbid="rel-1")
        )
        assert fills == {}

    def test_source_file_tags_and_filename_fill_the_gaps(self, tmp_path, monkeypatch):
        # A real (tagged) file: TDRC/TRCK survive the reader.
        from mutagen.id3 import ID3, TIT2, TRCK, TDRC

        source = tmp_path / "peer - LOVER.mp3"
        id3 = ID3()
        id3.add(TIT2(encoding=3, text=["LOVER"]))
        id3.add(TRCK(encoding=3, text=["3"]))
        id3.add(TDRC(encoding=3, text=["2026-07-31"]))
        id3.save(str(source))

        fills = dcs._resolve_missing_identity(self._row(), str(source))
        assert fills.get("year") == "2026"
        assert str(fills.get("track_number")) == "3"

    def test_filename_track_prefix_is_used_when_untagged(self, tmp_path):
        source = tmp_path / "03 - LOVER.mp3"
        source.write_bytes(b"not really audio")

        fills = dcs._resolve_missing_identity(self._row(), str(source))
        assert str(fills.get("track_number")) == "03"
        assert "year" not in fills

    def test_no_source_file_is_fine(self):
        assert dcs._resolve_missing_identity(self._row(), None) == {}
        assert dcs._resolve_missing_identity(self._row(), "/nope/missing.mp3") == {}


# ---------------------------------------------------------------------------
# 4. _move_and_import applies the fills BEFORE tags + path build
# ---------------------------------------------------------------------------

class TestMoveAndImportBackfillsIdentity:
    def test_fills_reach_the_tag_write_the_path_and_the_row(
        self, monkeypatch, tmp_path
    ):
        _install_fake_db(monkeypatch, rowcount=1)
        updates = _install_update_recorder(monkeypatch)

        # Source file: untagged, but the filename carries the track prefix.
        source = tmp_path / "03 - LOVER.mp3"
        source.write_bytes(b"fake audio payload")
        target = tmp_path / "music" / "SKZ" / "imported.mp3"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"moved")

        tagged: list[dict] = []

        def fake_apply(item, path):
            tagged.append({"path": path, "year": item.get("year"),
                           "track_number": item.get("track_number")})
            return True

        monkeypatch.setattr(dcs, "_apply_stored_metadata", fake_apply)

        moved: dict = {}

        def fake_move(track, release_metadata, music_root):
            moved["track"] = dict(track)
            moved["release_metadata"] = dict(release_metadata)
            return {"success": True, "target_path": str(target)}

        monkeypatch.setattr(
            "services.downloads.download_organize_helpers.move_track_to_library",
            fake_move,
        )
        monkeypatch.setattr(
            "services.downloads.download_verification_service.verify_file_in_music",
            lambda queue_id, path: {"success": True},
        )
        monkeypatch.setattr(
            "services.downloads.download_verification_service.mark_queue_item_moved",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "services.downloads.download_organize_helpers.dedupe_library_folder",
            lambda *a, **k: None,
        )

        # The broken row: queued via the single-item endpoint before the fix.
        item = {
            "id": 42,
            "artist": "Stray Kids",
            "album_artist": "Stray Kids",
            "album": "SKZ‐REPLAY 2026 Pt.1",
            "title": "LOVER",
            "year": None,
            "track_number": None,
            "disc_number": None,
            "release_year": 2026,      # the MB release adder stored this
            "release_mbid": None,
            "recording_mbid": None,
            "metadata": None,
            "duration": 183000,
        }

        result = dcs._move_and_import(item, str(source), "metadata")

        assert result["success"] is True, result

        # (a) the fills were persisted to the queue ROW, so
        #     _reconcile_stale_moving rebuilds the SAME target path later.
        persisted = {k: v for _, kw in updates for k, v in kw.items()
                     if k in ("year", "track_number", "disc_number")}
        assert persisted.get("year") == "2026"
        assert str(persisted.get("track_number")) == "03"

        # (b) the tag write saw the fills (not None → no frame deletion).
        assert tagged, "no tag write happened"
        for write in tagged:
            assert write["year"] == "2026"
            assert str(write["track_number"]) == "03"

        # (c) the MOVE got the fills — this is what names the folder/file.
        assert moved["release_metadata"].get("year") == "2026"
        assert str(moved["track"].get("track_number")) == "03"

        # (d) and therefore the built library path is correct, not
        #     "Unknown - <album>/00. ...".
        built = _build_target_path(
            str(tmp_path / "music"),
            moved["release_metadata"].get("album_artist"),
            moved["release_metadata"].get("year"),
            moved["release_metadata"].get("album"),
            moved["track"].get("artist"),
            moved["track"].get("title"),
            moved["track"].get("track_number"),
            str(source),
        )
        assert "Unknown" not in built
        assert "2026 - " in built
        assert "03. " in built

    def test_backfill_failure_does_not_abort_the_import(self, monkeypatch, tmp_path):
        """Identity healing is best-effort — a bad row still imports."""
        _install_fake_db(monkeypatch, rowcount=1)
        updates = _install_update_recorder(monkeypatch)

        source = tmp_path / "song.mp3"
        source.write_bytes(b"payload")
        target = tmp_path / "out.mp3"
        target.write_bytes(b"moved")

        def boom(*args, **kwargs):
            raise RuntimeError("MB exploded")

        monkeypatch.setattr(dcs, "_resolve_missing_identity", boom)
        monkeypatch.setattr(dcs, "_apply_stored_metadata", lambda *a: True)
        monkeypatch.setattr(
            "services.downloads.download_organize_helpers.move_track_to_library",
            lambda *a, **k: {"success": True, "target_path": str(target)},
        )
        monkeypatch.setattr(
            "services.downloads.download_verification_service.verify_file_in_music",
            lambda *a, **k: {"success": True},
        )
        monkeypatch.setattr(
            "services.downloads.download_verification_service.mark_queue_item_moved",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "services.downloads.download_organize_helpers.dedupe_library_folder",
            lambda *a, **k: None,
        )

        result = dcs._move_and_import(
            {"id": 7, "artist": "A", "title": "T", "album": "Al", "duration": 1},
            str(source),
            "metadata",
        )

        assert result["success"] is True, result
        assert not any(
            k in kw for _, kw in updates for k in ("year", "track_number")
        ), "a failed backfill must not persist partial junk"


# ---------------------------------------------------------------------------
# 5. the reader now exposes year (feeds the backfill + discovered report)
# ---------------------------------------------------------------------------

class TestReaderExposesYear:
    def test_mp3_date_survives_a_v23_round_trip(self, tmp_path):
        from mutagen.id3 import ID3, TDRC

        path = tmp_path / "t.mp3"
        id3 = ID3()
        id3.add(TDRC(encoding=3, text=["2026-07-31"]))
        id3.save(str(path), v2_version=3)  # what our writer produces

        from helpers.metadata_reader import read_mp3_metadata

        meta = read_mp3_metadata(str(path)) or {}
        assert meta.get("year") == "2026-07-31"

    def test_flac_date_is_read(self, tmp_path, monkeypatch):
        class _FakeFLAC(dict):
            info = SimpleNamespace(length=12.5)

        path = tmp_path / "t.flac"
        path.write_bytes(b"fLaC fake")

        monkeypatch.setattr(
            "helpers.metadata_reader.FLAC",
            lambda p: _FakeFLAC({"DATE": ["2026"], "TITLE": ["Lover"]}),
        )

        from helpers.metadata_reader import read_mp3_metadata

        meta = read_mp3_metadata(str(path)) or {}
        assert meta.get("year") == "2026"

    def test_discovered_metadata_year_is_now_derivable(self, monkeypatch):
        monkeypatch.setattr(
            "services.downloads.download_scan_service.read_mp3_metadata",
            lambda p: {"year": "2026-07-31"},
        )
        from services.downloads.download_scan_service import _extract_discovered_metadata

        result = _extract_discovered_metadata("/downloads/whatever.mp3", "whatever.mp3")
        assert result.get("year") == "2026"
