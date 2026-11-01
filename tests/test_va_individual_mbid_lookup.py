"""Individual recording MBIDs must resolve on an unmatched VA compilation.

Reported follow-up: *"The manual mbid lookup works, can we adjust so that
individual mbid is looked up via musicbrainz when an album isn't matched to a
musicbrainz release such as a Various Artists compilation?"* — the AUTOMATIC
metadata scan, not the album button.

``_resolve_track_mb_metadata`` already searches per track, but it searched with
the row's artist. A VA compilation's unresolved row can carry the album
placeholder ("Various Artists"), and ``artist:"Various Artists"`` finds nothing
— then the miss is stamped durably and locks the track out of the per-track
lookup for the negative-cache TTL. The scan now uses the FILE's ARTIST tag as
the search performer for those rows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


class _StubMBService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def lookup_recording_metadata(self, title, artist, **kwargs):
        self.calls.append((str(title), str(artist)))
        return {
            "recording_mbid": "rec-individual",
            # The recording's own release, from the same lookup.
            "release_mbid": "rel-individual",
            "confidence": 0.9,
        }


def _track(**overrides) -> dict:
    track = {
        "id": "t1",
        "title": "The One",
        "artist": "Various Artists",
        "album": "Metal Covers",
        "album_artist": "Various Artists",
        "file_path": "/music/Various Artists/Metal Covers/01. Bushido - The One.mp3",
        "disc_number": 1,
        "track_number": 1,
        "recording_mbid": "",
        "mbid": "",
        "musicbrainz_trackid": "",
    }
    track.update(overrides)
    return track


def _resolve(monkeypatch, service, **overrides) -> dict:
    from services.popularity.stages import track_stage

    track = _track(**overrides)
    monkeypatch.setattr(track_stage, "get_shared_mb_service", lambda: service)
    monkeypatch.setattr(track_stage, "_metadata_update_allowed", lambda: True)
    monkeypatch.setattr(track_stage, "_has_real_genres", lambda _t: False)
    return track_stage._resolve_track_mb_metadata(
        track_id=track["id"],
        track=track,
        track_title=track["title"],
        track_artist=track["artist"],
        frozen_track=False,
        force_meta=False,
        options={},
    )


class TestTheFilesArtistIsTheSearchPerformer:
    def test_a_placeholder_row_uses_the_files_artist(self, monkeypatch):
        from services.popularity.stages import track_stage

        monkeypatch.setattr(
            "services.metadata.tag_file_service.resolve_music_file_path",
            lambda path: path,
        )
        monkeypatch.setattr(
            "helpers.metadata_reader.read_mp3_metadata",
            lambda path: {"artist": "Bushido"},
        )

        assert track_stage._file_tag_artist(_track()) == "Bushido"

    def test_a_placeholder_inside_the_file_is_ignored(self, monkeypatch):
        from services.popularity.stages import track_stage

        monkeypatch.setattr(
            "services.metadata.tag_file_service.resolve_music_file_path",
            lambda path: path,
        )
        monkeypatch.setattr(
            "helpers.metadata_reader.read_mp3_metadata",
            lambda path: {"artist": "Various Artists"},
        )

        assert track_stage._file_tag_artist(_track()) == ""

    def test_a_row_without_a_file_has_no_fallback(self):
        from services.popularity.stages import track_stage

        assert track_stage._file_tag_artist(_track(file_path="")) == ""


class TestTheScanUsesThatPerformer:
    def test_an_unmatched_compilation_searches_individually(self, monkeypatch):
        """⭐ THE REPORTED CASE: no album release, placeholder row artist."""
        from services.popularity.stages import track_stage

        service = _StubMBService()
        monkeypatch.setattr(track_stage, "_file_tag_artist", lambda track: "Bushido")

        result = _resolve(monkeypatch, service)

        assert service.calls == [("The One", "Bushido")]
        assert result["payload"].get("recording_mbid") == "rec-individual"
        # The recording's own release rides along; cover detection uses it.
        assert result["payload"].get("recording_release_mbid") == "rel-individual"

    def test_a_real_track_artist_never_reads_the_file(self, monkeypatch):
        from services.popularity.stages import track_stage

        service = _StubMBService()
        monkeypatch.setattr(
            track_stage,
            "_file_tag_artist",
            lambda track: pytest.fail("a real artist must not open the file"),
        )

        result = _resolve(monkeypatch, service, artist="Mothica")

        assert service.calls == [("The One", "Mothica")]
        assert result["payload"].get("recording_mbid") == "rec-individual"
        assert result["payload"].get("recording_release_mbid") == "rel-individual"

    def test_no_performer_means_no_search_and_no_durable_miss(self, monkeypatch):
        """Without a usable performer the search is skipped, and no
        ``recording_mbid_checked_at`` stamp is written — a miss we could not
        learn anything from must not lock the track out for the TTL."""
        from services.popularity.stages import track_stage

        service = _StubMBService()
        monkeypatch.setattr(track_stage, "_file_tag_artist", lambda track: "")

        result = _resolve(monkeypatch, service)

        assert service.calls == []
        assert "recording_mbid_checked_at" not in result["payload"]
