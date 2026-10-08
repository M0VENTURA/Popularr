"""Download-import: album MBIDs are inherited, and multi-disc keeps disc 1.

REPORTED

> When tracks are added to the queue, when they are downloaded and copied into
> the directory, they don't have the same mbid data added to them as the rest of
> the album they were added from so are coming in as separate albums. Also, some
> multiple disk albums are adding the tracks as disk 0 rather than disk 1 when
> importing the tracks.

TWO DEFECTS, BOTH IN ``download_completion_service._apply_stored_metadata``

**1. Missing album MBIDs.** The album-level fields came from (a) what was stored
on the queue row, else (b) a LIVE MusicBrainz call at import time. The throttle
is 1 req/s, imports run per track, and that failure is logged only at DEBUG — so
when both were absent the file was written with **no** album MBIDs at all. The
artist page has keyed albums on ``musicbrainz_releasegroupid`` since
2026-10-06-albums-split-by-release-group, so those rows were filed as their own
album from the release they were downloaded for.

FIX: inherit the album-scoped fields from the release's own sibling rows already
in the library — LOCAL, before the MusicBrainz refresh (and therefore free of
the shared MusicBrainz budget).

**2. Disc 0 instead of disc 1.** The strip rule was ``"" for every disc < 2``,
so the FIRST disc of a MULTI-disc release had its TPOS cleared too; Navidrome
then reported ``discNumber`` 0 for those tracks while disc 2+ kept their number.

FIX: ``disctotal`` (now resolved before the decision) says whether the release
is multi-disc. A single-disc release still loses its stray ``1``; a multi-disc
release keeps it.

Disc 1 of a single-disc album was already stripped and stays stripped — see
``tests/test_album_missing_and_disc_cleanup.py`` (the "disc 1 and disc 0" split).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.downloads import download_completion_service as dcs  # noqa: E402
from services.metadata import tag_file_service as tfs  # noqa: E402

MBID_FIELDS = (
    "musicbrainz_albumtype",
    "musicbrainz_albumstatus",
    "musicbrainz_albumartistid",
    "musicbrainz_releasegroupid",
)

SIBLING_ROW = {
    "musicbrainz_albumtype": "album+compilation",
    "musicbrainz_albumstatus": "Official",
    "musicbrainz_albumartistid": "00000000-0000-0000-0000-0000000000aa",
    "musicbrainz_releasegroupid": "11111111-1111-1111-1111-111111111111",
    "releasecountry": "AU",
    "originalyear": "2004",
    "originaldate": "2004",
    "releasedate": "2004-11-01",
    "disctotal": "2",
    "recordlabel": "Universal",
    "catalognumber": "0000000000",
    "barcode": "0000000000000",
    "media": "CD",
}

FAKE_PATH = "/music/Powderfinger/2004 - Fingerprints/01 - Track.mp3"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Row(dict):
    @property
    def _mapping(self):  # mirrors SQLAlchemy's Row mapping
        return self


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Session:
    def __init__(self, row):
        self._row = row
        self.statements: list[str] = []

    def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))
        return _Result(self._row)


class _Ctx:
    """db_session stand-in returning a canned sibling row (or none)."""

    def __init__(self, row):
        self._row = row
        self.last: _Session | None = None

    def __enter__(self) -> _Session:
        self.last = _Session(self._row)
        return self.last

    def __exit__(self, *exc):
        return False


#: Filled by the ``capture`` fixture; ``run_import`` reads it back.
_CAPTURED: dict = {}


@pytest.fixture
def capture(monkeypatch):
    """Capture the metadata the import would write; no file I/O, no network."""
    _CAPTURED.clear()

    def _capture_update(file_path, metadata):  # noqa: ANN001
        _CAPTURED["meta"] = dict(metadata or {})
        return True

    monkeypatch.setattr(tfs, "update_file_metadata", _capture_update)
    monkeypatch.setattr(tfs, "write_tags_to_file", lambda *a, **k: True)
    return _CAPTURED


@pytest.fixture
def mb_down(monkeypatch):
    """MusicBrainz unreachable — the state the report's file was written in."""
    def _boom(*args, **kwargs):
        raise RuntimeError("MusicBrainz overloaded")

    import services.enrichment.musicbrainz_service as mb

    monkeypatch.setattr(mb, "fetch_musicbrainz_release_metadata", _boom)


@pytest.fixture
def sibling_row(monkeypatch):
    ctx = _Ctx(_Row(dict(SIBLING_ROW)))
    monkeypatch.setattr(dcs, "db_session", lambda: ctx)
    return ctx


@pytest.fixture
def no_siblings(monkeypatch):
    ctx = _Ctx(None)
    monkeypatch.setattr(dcs, "db_session", lambda: ctx)
    return ctx


def make_item(disc: int | None, album_metadata: dict | None = None) -> dict:
    item = {
        "id": 42,
        "artist": "Powderfinger",
        "album_artist": "Powderfinger",
        "album": "Fingerprints",
        "title": "Track",
        "track_number": 1,
        "disc_number": disc,
        "year": 2004,
        "release_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "release_mbid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "recording_mbid": "22222222-2222-2222-2222-222222222222",
        "metadata": {},
    }
    if album_metadata:
        item["metadata"] = {"album_metadata": dict(album_metadata)}
    return item


def run_import(item: dict) -> dict:
    """Apply the stored metadata (no file I/O, no network) and return it.

    Requires the ``capture`` fixture in the test signature — it is what
    installs the recorder.
    """
    _CAPTURED.clear()
    dcs._apply_stored_metadata(item, FAKE_PATH)
    return _CAPTURED.get("meta") or {}


# ---------------------------------------------------------------------------
# 1. The disc number
# ---------------------------------------------------------------------------


class TestMultiDiscReleasesKeepDiscOne:
    MULTI = {"disctotal": "2", "musicbrainz_releasegroupid": "g2"}

    def test_disc_one_is_written_as_one(self, capture, sibling_row, mb_down):
        meta = run_import(make_item(1, self.MULTI))
        assert meta.get("disc_number") == "1", (
            "disc 1 of a multi-disc release must keep its number; clearing it "
            "is what made Navidrome report discNumber 0 (the reported "
            "'disk 0 rather than disk 1')"
        )

    def test_disc_two_is_written_as_two(self, capture, sibling_row, mb_down):
        assert run_import(make_item(2, self.MULTI)).get("disc_number") == "2"

    def test_an_unknown_disc_on_a_multi_disc_release_is_left_alone(
        self, capture, sibling_row, mb_down
    ):
        """Guessing a disc produced the 0 in the first place."""
        meta = run_import(make_item(None, self.MULTI))
        assert "disc_number" not in meta, (
            "no disc number means no frame change — never an explicit clear"
        )


class TestSingleDiscReleasesStillStripDiscOne:
    """CONTROL — the stray-1 strip is deliberate and must not be undone."""

    SINGLE = {"disctotal": "1", "musicbrainz_releasegroupid": "g1"}

    def test_disc_one_is_cleared(self, capture, sibling_row, mb_down):
        meta = run_import(make_item(1, self.SINGLE))
        assert meta.get("disc_number") == "", (
            "a single-disc release must not gain a disc number (it renders as "
            "its own 'disc 0' group)"
        )

    def test_disc_two_is_still_written(self, capture, sibling_row, mb_down):
        assert run_import(make_item(2, self.SINGLE)).get("disc_number") == "2"

    def test_the_decision_reads_disctotal(self):
        """The strip must be decided by the release, not by the number alone."""
        import inspect

        source = inspect.getsource(dcs._apply_stored_metadata)
        assert "_album_level.get(\"disctotal\")" in source
        assert "if _disc_num < 2:" not in source, (
            "the unconditional disc-1 strip is the reported bug"
        )


# ---------------------------------------------------------------------------
# 2. Album MBIDs
# ---------------------------------------------------------------------------


class TestAlbumMbidsComeFromTheLibrary:
    def test_an_unreachable_musicbrainz_inherits_the_sibling_rows(
        self, capture, sibling_row, mb_down
    ):
        """The reported defect: no MB call, no album MBIDs, separate album."""
        meta = run_import(make_item(1, None))          # nothing stored on the row

        for field in MBID_FIELDS:
            assert meta.get(field), f"{field} missing from the imported file"
        assert meta["musicbrainz_releasegroupid"] == SIBLING_ROW["musicbrainz_releasegroupid"]
        assert meta["musicbrainz_albumartistid"] == SIBLING_ROW["musicbrainz_albumartistid"]

    def test_the_stored_values_win_over_the_sibling(self, capture, sibling_row, mb_down):
        """CONTROL — inheritance must not overwrite what was stored at queue time."""
        stored = {"disctotal": "4", "musicbrainz_releasegroupid": "stored-rg"}
        meta = run_import(make_item(1, stored))
        assert meta["musicbrainz_releasegroupid"] == "stored-rg"
        assert meta["disctotal"] == "4"

    def test_inheritance_does_not_spend_musicbrainz(
        self, capture, sibling_row, monkeypatch
    ):
        """The point of reading the library first: no throttled call at all."""
        calls: list = []

        def _spy(*args, **kwargs):
            calls.append(args)
            return None

        import services.enrichment.musicbrainz_service as mb

        monkeypatch.setattr(mb, "fetch_musicbrainz_release_metadata", _spy)

        # A COMPLETELY resolved row — album-level AND track-level — so nothing
        # is left for either MusicBrainz refresh to fill. (A partial row would
        # legitimately still call for the fields it lacks.)
        stored = {column: "x" for column in dcs._ALBUM_LEVEL_COLUMNS}
        stored["musicbrainz_releasegroupid"] = "stored-rg"
        item = make_item(1, stored)
        item["metadata"]["isrc"] = "USUM70000000001"
        item["metadata"]["tracktotal"] = "10"
        item["metadata"]["musicbrainz_artistid"] = "00000000-0000-0000-0000-0000000000bb"

        meta = run_import(item)
        assert meta["musicbrainz_releasegroupid"] == "stored-rg"
        assert calls == [], (
            f"MusicBrainz was queried despite a fully-resolvable row ({calls})"
        )

    def test_the_inheritance_query_skips_queue_stub_rows(self):
        """A stub's NULL file_path must never donate MBIDs to a real album."""
        import inspect

        source = inspect.getsource(dcs._resolve_album_level_metadata)
        assert "__queued_for_download__" in source, (
            "queue placeholders must be excluded from the sibling lookup"
        )
        assert "WHEN COALESCE(musicbrainz_releasegroupid, '') <> '' THEN 0" in source, (
            "the lookup must prefer a sibling that carries the release group "
            "— that is the field the artist page groups on"
        )

    def test_the_refresh_now_runs_as_the_last_step(self):
        """CONTROL — ordering is the fix: library first, MusicBrainz after."""
        import inspect

        source = inspect.getsource(dcs._resolve_album_level_metadata)
        inherit = source.index("inherit from the album's OWN rows")
        refresh = source.index("refresh from MusicBrainz")
        assert inherit < refresh, (
            "inheriting after the refresh would still spend the MB call first"
        )


# ---------------------------------------------------------------------------
# 3. The album level is resolved ONCE, before the disc decision
# ---------------------------------------------------------------------------


class TestTheAlbumLevelIsResolvedOnce:
    def test_it_is_resolved_before_the_disc_logic(self):
        import inspect

        source = inspect.getsource(dcs._apply_stored_metadata)
        resolved = source.index("_album_level = _resolve_album_level_metadata(item)")
        disc = source.index("_disc_raw = item.get(\"disc_number\")")
        assert resolved < disc, (
            "disctotal decides the disc behaviour, so it must be resolved first"
        )
        assert source.count("_resolve_album_level_metadata(item)") == 1, (
            "resolving twice would repeat the MusicBrainz call per track"
        )


# ---------------------------------------------------------------------------
# 4. The failure mode the fix exists for, pinned both ways
# ---------------------------------------------------------------------------


class TestNoLibraryRowNoMusicBrainzStillImports:
    """Nothing to inherit from and no network — the import must still work."""

    def test_the_import_degrades_without_raising(
        self, capture, no_siblings, mb_down
    ):
        meta = run_import(make_item(1, None))
        assert meta.get("title") == "Track"
        assert meta.get("release_mbid"), "the release MBID comes from the row"
        assert "musicbrainz_releasegroupid" not in meta, (
            "documents that nothing can be inherited — the import proceeds "
            "with the fields it has rather than failing"
        )

    def test_the_missing_release_group_is_warned_about(
        self, no_siblings, mb_down, capture
    ):
        """The silent split must be diagnosable — it used to log NOTHING."""
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            run_import(make_item(1, None))

        messages = [
            str(entry.get("event") or entry.get("message") or "") for entry in logs
        ]
        assert any("no release-group MBID" in m for m in messages), (
            f"expected the warning; captured: {logs}"
        )
