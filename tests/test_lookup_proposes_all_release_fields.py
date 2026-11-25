"""Lookup MBID must propose EVERY release field the album page exposes.

Reported gap: the page had boxes for Track Total, Release Status, Original
Date, Script and ASIN, but a lookup never proposed them; and the review's
"Artist ID" read ``musicbrainz_artistid`` (a track column) instead of the
release's ``musicbrainz_albumartistid`` — the same confusion that fanned an
album-artist id onto every track (Vitro / Various Artists).
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest


class _Row:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[_Row]:
        return [_Row(r) for r in self._rows]


class _Session:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def execute(self, statement: Any, params: Any = None) -> _Result:
        return _Result(self._rows if "FROM tracks" in str(statement) else [])

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _local(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "t1",
        "title": "Song",
        "track_number": "1",
        "disc_number": "1",
        "mbid": "old-rec",
        "recording_mbid": "old-rec",
        "artist": "Korn",
        "album": "Untouchables",
        "album_artist": "Korn",
        "musicbrainz_artistid": "",
        "musicbrainz_albumartistid": "",
        "musicbrainz_releasetrackid": "",
        "musicbrainz_workid": "",
        "isrc": "",
        "genres": "",
        "musicbrainz_genres": None,
        "mb_ignored_fields": None,
        "is_cover": 0,
        "writer": "",
    }
    row.update(overrides)
    return row


@pytest.fixture()
def modules(monkeypatch):
    proposal = importlib.import_module("services.metadata.metadata_proposal_service")
    mbs = importlib.import_module("services.enrichment.musicbrainz_service")
    return proposal, mbs


def _propose(monkeypatch, proposal, mbs, local, metadata, comparison):
    monkeypatch.setattr(proposal, "db_session", lambda: _Session(local))
    monkeypatch.setattr(
        mbs, "compare_musicbrainz_release",
        lambda *a, **k: {
            "success": True,
            "mb_release_mbid": "release-1",
            "mb_release_group_mbid": "group-1",
            "comparison": comparison,
        },
        raising=False,
    )
    monkeypatch.setattr(
        mbs, "fetch_musicbrainz_release_metadata", lambda rid: metadata, raising=False,
    )
    return proposal.propose_album_metadata("Korn", "Untouchables", "release-1")


def _entry() -> dict[str, Any]:
    return {
        "mb_track_number": 1,
        "mb_disc_number": 1,
        "mb_title": "Song",
        "mb_recording_mbid": "rec-1",
        "mb_duration": 200000,
        "matched": True,
        "library_track_id": "t1",
        "library_title": "Song",
        "library_track_number": "1",
        "release_track_mbid": "reltrack-1",
    }


def _metadata() -> dict[str, Any]:
    return {
        "release_mbid": "release-1",
        "release_group_mbid": "group-1",
        "release_group_title": "Untouchables",
        "release_title": "Untouchables",
        "tracktotal": "14",
        "status": "Official",
        "original_date": "2002-06-11",
        "script": "Latn",
        "asin": "B0000669GQ",
        "language": "eng",
        "discsubtitle": "Disc 1: The Storm",
        "artist": "Korn",
        "album_artist_mbid": "album-artist-1",
        "album_type": "album",
        "tracks": [
            {
                "mb_recording_mbid": "rec-1",
                "mb_title": "Song",
                "mb_track_number": 1,
                "mb_disc_number": 1,
                "artist_mbid": "track-artist-1",
                "work_mbid": "work-1",
                "isrc": "USSM10200001",
                "release_track_mbid": "reltrack-1",
            }
        ],
    }


def test_album_fields_the_page_exposes_are_all_proposed(modules, monkeypatch):
    proposal, mbs = modules
    out = _propose(monkeypatch, proposal, mbs, [_local()], _metadata(), [_entry()])

    fields = {change["field"]: change["proposed"] for change in out["album_changes"]}
    assert fields["album_tracktotal"] == "14"
    assert fields["album_releasestatus"] == "Official"
    assert fields["album_originaldate"] == "2002-06-11"
    assert fields["album_script"] == "Latn"
    assert fields["album_asin"] == "B0000669GQ"
    assert fields["album_language"] == "eng"
    assert fields["album_discsubtitle"] == "Disc 1: The Storm"


def test_per_track_entity_ids_follow_the_chosen_release(modules, monkeypatch):
    proposal, mbs = modules
    out = _propose(monkeypatch, proposal, mbs, [_local()], _metadata(), [_entry()])

    changes = {c["field"]: c["proposed"] for c in out["track_changes"][0]["changes"]}
    assert changes["musicbrainz_releasetrackid"] == "reltrack-1"
    assert changes["musicbrainz_artistid"] == "track-artist-1"
    assert changes["musicbrainz_workid"] == "work-1"
    assert changes["isrc"] == "USSM10200001"


def test_the_artist_id_reads_the_album_artist_column(modules, monkeypatch):
    proposal, mbs = modules
    local = _local(
        musicbrainz_artistid="track-performer-id",  # must NOT be the current side
        musicbrainz_albumartistid="album-artist-1",
    )
    out = _propose(monkeypatch, proposal, mbs, [local], _metadata(), [_entry()])

    fields = {c["field"]: c for c in out["album_changes"]}
    # Already correct: no proposal — the compare must use the album-artist id.
    assert "artist_mbid" not in fields

    local["musicbrainz_albumartistid"] = "other-album-artist"
    out = _propose(monkeypatch, proposal, mbs, [local], _metadata(), [_entry()])
    fields = {c["field"]: c for c in out["album_changes"]}
    assert fields["artist_mbid"]["current"] == "other-album-artist"
    assert fields["artist_mbid"]["proposed"] == "album-artist-1"
