"""Navidrome import: read the file's tags, and give an unclassified album a type.

REPORTED (three parts, all on the import path)

> I re-imported an album from Navidrome that was downloaded from Soulseek.
> These are the tags added to the track, but the album didn't pick up the tags
> on import from Navidrome.

> I also imported another … a manual match to a release from matched and
> unmatched folders and these are the fields that were imported [a much
> shorter list]. It also didn't attach the album art for the release.

> Navidrome imports also aren't assigning an album type. Could it smartly auto
> assign it to Album, EP or Single … Not sure if there is a metadata field
> that shows whether its an album, compilation, EP or Single.

ROOT CAUSES
-----------

1. **Navidrome keeps what it cannot send.** ``osChildFromMediaFile`` (its
   own source) exposes title, ids, ISRC, replaygain, genres/moods,
   participants, works and movements — and nothing else. Barcode, catalog
   number, media, script, release country/status, language, record label,
   release type and the track/disc totals are read into
   ``model.MediaFile.Tags`` and never echoed. So an import of a file tagged
   elsewhere arrived with every one of them EMPTY. The file is local; the
   import now reads it back.

2. **The manual folder match wrote only per-track identity.**
   ``_apply_release_metadata_to_files`` dropped the album-scoped half of the
   release it had already fetched, and nothing on that path fetched artwork.

3. **Nothing assigns an album type at import time.** The column every UI
   reads — ``musicbrainz_albumtype`` — is written by the album stage of the
   metadata scan, so an imported album classifies as nothing until then. The
   answer to "is there a field?" is yes: ``musicbrainz_albumtype`` (confirmed)
   and ``releasetype`` (the tag). The guess goes in the SECOND one so a
   track-count guess can never outrank MusicBrainz.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from helpers import metadata_reader as mr  # noqa: E402
from services.catalog import album_classification_service as acs  # noqa: E402
from services.scanning import payload_builder as pb  # noqa: E402


# ===========================================================================
# 1. Reading back what Navidrome never echoes
# ===========================================================================


class TestTheReleaseTagLookupIsBuiltFromTheWriter:
    def test_every_release_field_resolves_to_at_least_one_tag_key(self):
        """Reader and writer must agree on the spelling — one declaration."""
        from services.metadata.tag_names import clear_keys_for, normalise_tag_name

        lookup = mr._release_tag_lookup()
        missing = [
            field
            for field in mr._RELEASE_TAG_FIELDS
            if not any(k in lookup for k in clear_keys_for(field))
            and normalise_tag_name(field) not in lookup
        ]
        assert not missing, f"no tag key maps back to these fields: {missing}"

    def test_a_label_tag_lands_on_recordlabel(self):
        """``LABEL`` is Picard's spelling; ``recordlabel`` is the column."""
        from services.metadata.tag_names import normalise_tag_name

        lookup = mr._release_tag_lookup()
        assert lookup.get(normalise_tag_name("LABEL")) == "recordlabel"

    def test_the_synonyms_a_tagger_may_have_used_all_resolve(self):
        """``ORIGYEAR``/``ORIGINALYEAR``, ``TOTALTRACKS``/``TRACKTOTAL`` …"""
        from services.metadata.tag_names import normalise_tag_name

        lookup = mr._release_tag_lookup()
        for spelling, field in (
            ("ORIGYEAR", "originalyear"),
            ("TOTALTRACKS", "tracktotal"),
            ("BARCODE", "barcode"),
            ("MUSICBRAINZ RELEASE GROUP ID", "musicbrainz_releasegroupid"),
            ("RELEASETYPE", "releasetype"),
        ):
            assert lookup.get(normalise_tag_name(spelling)) == field, spelling


class TestFileTagsOnlyFillGaps:
    """Navidrome's own answer always wins; the file only repairs the holes."""

    @pytest.fixture
    def merge(self, monkeypatch):
        """``_merge_release_tags_from_file`` with a resolvable path."""
        import services.scanning.navidrome_import as ni
        from services.metadata import tag_file_service as tfs

        monkeypatch.setattr(tfs, "resolve_music_file_path", lambda p: p)
        monkeypatch.setattr(ni.os.path, "isfile", lambda p: True)
        reads: list[dict] = []

        def _install(values: dict) -> None:
            reads.append(dict(values))
            monkeypatch.setattr(mr, "read_release_tag_values", lambda _p: dict(values))

        def _run(extracted: dict, track: dict | None = None) -> dict:
            ni._merge_release_tags_from_file(extracted, track or {"path": "/music/x.mp3"})
            return extracted

        _run.installs = _install
        _run.reads = reads
        return _run

    def test_a_field_navidrome_did_not_send_comes_from_the_file(self, merge):
        merge.installs({"barcode": "5012980290024", "catalognumber": "VUSCD 29"})
        out = merge({"barcode": "", "catalognumber": ""})
        assert out["barcode"] == "5012980290024"
        assert out["catalognumber"] == "VUSCD 29"

    def test_a_value_navidrome_did_send_is_never_replaced(self, merge):
        merge.installs({"releasetype": "album"})
        out = merge({"releasetype": "single"})
        assert out["releasetype"] == "single"

    def test_a_path_that_cannot_be_resolved_is_a_no_op(self, monkeypatch):
        import services.scanning.navidrome_import as ni
        from services.metadata import tag_file_service as tfs

        monkeypatch.setattr(tfs, "resolve_music_file_path", lambda p: None)
        monkeypatch.setattr(ni.os.path, "isfile", lambda p: False)
        extracted = {"barcode": ""}
        ni._merge_release_tags_from_file(extracted, {"path": "missing.mp3"})
        assert extracted["barcode"] == ""

    def test_no_path_at_all_reads_nothing(self, merge, monkeypatch):
        import services.scanning.navidrome_import as ni

        monkeypatch.setattr(
            mr, "read_release_tag_values",
            lambda _p: pytest.fail("the reader must not run without a path"),
        )
        extracted = {"barcode": ""}
        ni._merge_release_tags_from_file(extracted, {})
        assert extracted["barcode"] == ""


def test_the_navidrome_extractor_actually_calls_the_readback():
    """The hook must be in the path, not merely defined beside it."""
    import inspect

    import services.scanning.navidrome_import as ni

    source = inspect.getsource(ni.extract_and_backfill_track_metadata)
    assert "_merge_release_tags_from_file(extracted, track)" in source


# ===========================================================================
# 2. The provisional album type
# ===========================================================================


class TestTheTrackCountGuess:
    @pytest.mark.parametrize(
        "count,expected",
        [(None, ""), (0, ""), (-1, ""), ("x", ""), (1, "single"), (2, "single"),
         (3, "ep"), (6, "ep"), (7, "album"), (40, "album"), ("9", "album")],
    )
    def test_the_bands(self, count, expected):
        assert acs.guess_album_type_from_track_count(count) == expected

    def test_the_documented_reason_it_is_only_a_guess(self):
        """A local count demoted a MusicBrainz EP once (fa34e0b7)."""
        assert "releasetype" in acs.guess_album_type_from_track_count.__doc__
        assert "musicbrainz_albumtype" in acs.guess_album_type_from_track_count.__doc__


class TestTheGuessGoesIntoReleasetypeOnly:
    def _payload(self, count, extracted=None, album_tags=None):
        return pb.build_track_payload(
            track={"id": "t1", "title": "T", "artist": "A", "path": ""},
            album_name="Al",
            album_artist_value="A",
            canonical_artist_name="A",
            extracted=extracted if extracted is not None else {},
            album_tags=album_tags,
            album_track_count=count,
        )

    def test_a_three_track_album_is_typed_ep(self):
        assert self._payload(3)["releasetype"] == "ep"

    def test_a_long_album_is_typed_album(self):
        assert self._payload(12)["releasetype"] == "album"

    def test_the_confirmed_column_is_never_written(self):
        payload = self._payload(3)
        assert payload.get("musicbrainz_albumtype", "") in ("", None), (
            "a track-count guess in musicbrainz_albumtype could outrank "
            "MusicBrainz for ever — _rich_stored_album_type treats it as "
            "authoritative"
        )

    def test_a_real_tag_beats_the_guess(self):
        payload = self._payload(3, extracted={"releasetype": "single"})
        assert payload["releasetype"] == "single"

    def test_an_album_tag_beats_the_guess(self):
        payload = self._payload(3, album_tags={"releasetype": "album"})
        assert payload["releasetype"] == "album"

    def test_no_count_means_no_guess(self):
        assert self._payload(None).get("releasetype", "") in ("", None)


class TestTheTypeIsActuallyVisible:
    def test_the_artist_page_buckets_fall_back_to_releasetype(self):
        from services.catalog.release_categories import category_for_album_row

        assert category_for_album_row({"album": "X", "releasetype": "ep"}) == "ep"

    def test_the_confirmed_type_still_wins(self):
        from services.catalog.release_categories import category_for_album_row

        row = {"album": "X", "musicbrainz_albumtype": "album", "releasetype": "ep"}
        assert category_for_album_row(row) == "album", (
            "the fallback must not compete with a type the scan resolved"
        )

    def test_the_recent_albums_query_reads_the_fallback(self):
        import inspect

        import routes.ui_routes as ui

        source = inspect.getsource(ui)
        assert "NULLIF(releasetype, '')" in source, (
            "the dashboard's recent-albums type would still come back empty"
        )


# ===========================================================================
# 3. Fill-if-empty, not never-touch
# ===========================================================================


class TestTheTypeColumnsAreFillButNeverOverwrite:
    def test_the_set_exists_and_holds_the_two_type_columns(self):
        from db.repositories.popularity_repository import (
            _FILL_IF_EMPTY_PROTECTED_COLUMNS,
        )

        assert "releasetype" in _FILL_IF_EMPTY_PROTECTED_COLUMNS
        assert "musicbrainz_albumtype" in _FILL_IF_EMPTY_PROTECTED_COLUMNS

    def test_a_navidrome_sync_cannot_still_wipe_genres(self):
        """CONTROL — the soft grade must not weaken the hard one."""
        from db.repositories.popularity_repository import (
            _FILL_IF_EMPTY_PROTECTED_COLUMNS,
            _POPULARITY_PROTECTED_COLUMNS,
        )

        for column in ("genres", "final_score", "stars", "is_single"):
            assert column in _POPULARITY_PROTECTED_COLUMNS
            assert column not in _FILL_IF_EMPTY_PROTECTED_COLUMNS

    def test_the_update_clause_fills_a_blank_but_keeps_a_real_value(self):
        import inspect

        from db.repositories import popularity_repository as pr

        source = inspect.getsource(pr._execute_save)
        assert "CASE WHEN tracks." in source, (
            "without the CASE the type columns stay blank on every EXISTING "
            "row — the INSERT half only runs the first time"
        )
        assert source.index("CASE WHEN tracks.") < source.index(
            'if is_navidrome_sync and k in _POPULARITY_PROTECTED_COLUMNS'
        )


# ===========================================================================
# 4. The manual folder match
# ===========================================================================


class TestTheFolderMatchWritesTheWholeRelease:
    def test_the_album_level_fields_are_applied(self, monkeypatch, tmp_path):
        import services.downloads.download_folder_service as dfs

        _install_cover(monkeypatch, b"JPEGDATA")
        _stub_release_fetch(monkeypatch)

        target = tmp_path / "t.mp3"
        target.write_bytes(b"\x00")

        written: dict = {}

        def _fake_update(path, metadata):
            written.update(metadata)
            return True

        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_metadata", _fake_update
        )

        result = dfs._apply_release_metadata_to_files(
            files=[{"file_path": str(target), "title": "Song", "track_number": 1}],
            album_artist="Eleine",
            album="Blood in Their Eyes",
            year="2023",
            release_mbid="6fbb2924-c1f4-447b-8538-ca185985684d",
        )
        assert result, "no file was matched"
        # Album-scoped identity the per-track matcher cannot produce.
        assert written.get("musicbrainz_releasegroupid") == "rg-1"
        # ``_album_level_mb_fields`` maps MusicBrainz's ``album_type`` onto the
        # CONFIRMED column — this is MusicBrainz's own answer, not a tag guess,
        # so it belongs in ``musicbrainz_albumtype`` (see the type section).
        assert written.get("musicbrainz_albumtype") == "Single"
        assert written.get("recordlabel") == "Virgin America"
        assert written.get("barcode") == "5012980290024"
        assert written.get("catalognumber") == "VUSCD 29"
        assert written.get("media") == "CD"
        assert written.get("releasecountry") == "United Kingdom"
        # …and the per-track identity is still there.
        assert written.get("recording_mbid")

    def test_the_cover_art_is_fetched_once_and_embedded(self, monkeypatch, tmp_path):
        import services.downloads.download_folder_service as dfs

        calls = []
        _install_cover(monkeypatch, b"JPEGDATA", calls)

        target = tmp_path / "a.mp3"
        target.write_bytes(b"\x00")
        written: list[dict] = []
        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_metadata",
            lambda p, m: (written.append(dict(m)), True)[1],
        )
        _stub_release_fetch(monkeypatch)

        dfs._apply_release_metadata_to_files(
            files=[
                {"file_path": str(target), "title": "One", "track_number": 1},
                {"file_path": str(target), "title": "Two", "track_number": 2},
            ],
            album_artist="Eleine",
            album="Blood in Their Eyes",
            year="2023",
            release_mbid="6fbb2924-c1f4-447b-8538-ca185985684d",
        )

        assert len(calls) == 1, f"the CAA was hit {len(calls)} times — fetch once"
        assert written and all(w.get("cover_art_data") == b"JPEGDATA" for w in written)

    def test_no_artwork_available_is_not_an_error(self, monkeypatch, tmp_path):
        import services.downloads.download_folder_service as dfs

        _install_cover(monkeypatch, None)
        target = tmp_path / "b.mp3"
        target.write_bytes(b"\x00")
        written: dict = {}
        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_metadata",
            lambda p, m: (written.update(m), True)[1],
        )
        _stub_release_fetch(monkeypatch)

        result = dfs._apply_release_metadata_to_files(
            files=[{"file_path": str(target), "title": "Song", "track_number": 1}],
            album_artist="A",
            album="Al",
            year="2023",
            release_mbid="6fbb2924-c1f4-447b-8538-ca185985684d",
        )
        assert result
        assert "cover_art_data" not in written


def _install_cover(monkeypatch, data, calls=None):
    def _fake(mbid, *a, **k):
        if calls is not None:
            calls.append(mbid)
        return data

    monkeypatch.setattr(
        "api_clients.coverartarchive.get_release_front_image_bytes", _fake
    )
    monkeypatch.setattr(
        "services.enrichment.album_art_service.save_album_art_to_db",
        lambda *a, **k: True,
    )


def _release_stub(mbid):
    """A flattened release carrying BOTH halves of the identity.

    ``_apply_release_metadata_to_files`` imports the fetcher from
    ``services.enrichment.musicbrainz_service`` INSIDE the function, so that is
    where the stub has to land; the track carries the ``mb_*`` keys because
    that is the shape ``_flatten_release`` emits and ``match_mb_tracks_to_files``
    reads.
    """
    track = {
        "mb_title": "Blood in Their Eyes",
        "mb_track_number": 1,
        "mb_disc_number": 1,
        "mb_recording_mbid": "ac3fd55d-bfe6-43bb-a067-a6daa6755847",
        "mb_duration": 240000,
        "title": "Blood in Their Eyes",
        "track_number": 1,
        "disc_number": 1,
        "recording_mbid": "ac3fd55d-bfe6-43bb-a067-a6daa6755847",
        "duration": 240000,
    }
    return {
        "release_mbid": mbid,
        "release_group_mbid": "rg-1",
        "album_type": "Single",
        "status": "Official",
        "releasecountry": "United Kingdom",
        "original_year": "2023",
        "original_date": "2023",
        "releasedate": "2023-06-02",
        "disctotal": "1",
        "recordlabel": "Virgin America",
        "catalognumber": "VUSCD 29",
        "barcode": "5012980290024",
        "media": "CD",
        "album_artist_mbid": "f37b3f31-b1f8-4b88-8cb5-b34f709b17d7",
        "tracks": [track],
    }


def _stub_release_fetch(monkeypatch):
    monkeypatch.setattr(
        "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
        _release_stub,
    )
