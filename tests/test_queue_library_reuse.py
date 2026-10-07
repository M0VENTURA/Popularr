"""The queue checks the main music directory before it downloads.

Reported:

> Can we adjust the download queue so that it checks to see if a track exists
> in the main music directory first before it starts downloading. If a
> matching track is found on a different album (as long as it's not a
> different format such as live, or acoustic) and matches the track length,
> the file is copied to the downloads folder, metadata updated, then moved
> into the new location as if it were a downloaded file. This will save
> unnecessary downloads, especially for compilations.

Implementation: ``process_queue_item`` asks ``_library_reuse_source`` BEFORE
building a single Soulseek query. On a hit, ``_stage_library_copy`` copies the
file into the downloads folder under a name the completion matcher knows and
puts the row into ``downloading`` — the file then travels the NORMAL
completion route (``check_completed_downloads``: stored metadata written,
file moved into the library, row ``imported``), exactly like a file Soulseek
delivered. No completion/mover call lives in the queue module on purpose: a
guard test pins that no scan entry point can reach a file-relocating helper.
Any staging failure falls through to the normal search: a broken reuse must
never stop a download.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.downloads import download_pipeline_service as pipeline  # noqa: E402


@pytest.fixture(autouse=True)
def _empty_tracks(db_session):
    """Fixed ids + ON CONFLICT DO NOTHING = silently seeing another test's rows."""
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()


def _seed(
    db_session,
    *,
    title: str = "Song",
    artist: str = "Madball",
    album_artist: str | None = None,
    album: str = "Greatest Hits",
    duration: float | int | None = 240,
    file_path: str | None = None,
):
    db_session.execute(
        text("""
            INSERT INTO tracks (id, artist, album, album_artist, title, duration, file_path)
            VALUES (:id, :artist, :album, :album_artist, :title, :duration, :file_path)
            ON CONFLICT DO NOTHING
        """),
        {
            "id": "reuse-1",
            "artist": artist,
            "album": album,
            "album_artist": album_artist,
            "title": title,
            "duration": duration,
            "file_path": file_path or "/library/reuse-1.flac",
        },
    )
    db_session.commit()


def _item(**over) -> dict:
    item = {
        "id": 4242,
        "title": "Song",
        "artist": "Madball",
        "album": "Greatest Hits",
        "duration": 240,
    }
    item.update(over)
    return item


@pytest.fixture
def library_file(tmp_path) -> str:
    path = tmp_path / "01 - Song.flac"
    path.write_bytes(b"audio-bytes")
    return str(path)


# ---------------------------------------------------------------------------
# 1. The matching rules
# ---------------------------------------------------------------------------

class TestTheLibrarySourceRules:
    @pytest.fixture(autouse=True)
    def _identity_resolver(self, monkeypatch):
        """Rows carry the real path; the resolver passes it straight through."""
        monkeypatch.setattr(
            "services.metadata.tag_file_service.resolve_music_file_path",
            lambda p: p or None,
        )

    def test_an_identical_recording_on_another_album_is_found(self, db_session, library_file):
        _seed(db_session, file_path=library_file)

        assert pipeline._library_reuse_source(_item()) == library_file

    def test_millisecond_track_lengths_still_match(self, db_session, library_file):
        """MusicBrainz stores length in ms; the queue stores seconds."""
        _seed(db_session, duration=240_000, file_path=library_file)

        assert pipeline._library_reuse_source(_item()) == library_file

    def test_a_live_copy_never_stands_in_for_a_studio_track(self, db_session, library_file):
        _seed(db_session, album="Live in Tokyo", file_path=library_file)

        assert pipeline._library_reuse_source(_item()) is None, (
            "a live recording is a different format"
        )

    def test_an_acoustic_copy_never_stands_in_for_a_studio_track(self, db_session, library_file):
        _seed(db_session, album="Acoustic Dreams", file_path=library_file)

        assert pipeline._library_reuse_source(_item()) is None

    def test_a_studio_copy_never_stands_in_for_a_live_track(self, db_session, library_file):
        """The guard runs BOTH ways: a live request may not grab studio audio."""
        _seed(db_session, file_path=library_file)

        assert pipeline._library_reuse_source(_item(album="Live in Tokyo")) is None

    def test_the_track_length_must_match(self, db_session, library_file):
        _seed(db_session, duration=200, file_path=library_file)  # 240s queued

        assert pipeline._library_reuse_source(_item()) is None, (
            "200s vs 240s is a different edit/take"
        )

    def test_an_unknown_queue_duration_never_reuses(self, db_session, library_file):
        """Without the queued length the length rule cannot be verified."""
        _seed(db_session, file_path=library_file)

        assert pipeline._library_reuse_source(_item(duration=None)) is None
        assert pipeline._library_reuse_source(_item(duration="")) is None

    def test_the_performer_must_match(self, db_session, library_file):
        _seed(db_session, artist="A.N. Other", file_path=library_file)

        assert pipeline._library_reuse_source(_item()) is None

    def test_the_file_must_exist_on_disk(self, db_session):
        _seed(db_session, file_path="/nope/never-downloaded.flac")

        assert pipeline._library_reuse_source(_item()) is None

    def test_a_different_title_never_matches(self, db_session, library_file):
        _seed(db_session, title="Something Else", file_path=library_file)

        assert pipeline._library_reuse_source(_item()) is None


# ---------------------------------------------------------------------------
# 2. The flow: copy → completion, or fall back to searching
# ---------------------------------------------------------------------------

class TestTheReuseStagesLikeADownload:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch, tmp_path):
        state = {
            "downloads": tmp_path / "downloads",
            "moves": [],
            "queue_updates": [],
            "events": [],
        }
        state["downloads"].mkdir()

        monkeypatch.setattr(
            "services.metadata.tag_file_service.resolve_music_file_path",
            lambda p: p or None,
        )
        monkeypatch.setattr(
            "services.downloads.download_folder_service.resolve_downloads_dir",
            lambda *a, **k: str(state["downloads"]),
        )
        monkeypatch.setattr(
            "services.downloads.download_pipeline_service.update_queue_item",
            lambda qid, **kw: state["queue_updates"].append((qid, kw)),
        )
        monkeypatch.setattr(
            "services.downloads.download_pipeline_service._log_queue_event",
            lambda kind, msg, qid=None, **kw: state["events"].append((kind, msg)),
        )
        # The queue module must NOT call a mover directly (see the scan-graph
        # guard) — the completion cycle does. Recording the call proves it.
        monkeypatch.setattr(
            "services.downloads.download_completion_service._move_and_import",
            lambda item, staged, match_source: state["moves"].append((item, staged, match_source))
            or {"success": True, "target_path": "/music/x"},
        )
        return state

    def test_a_hit_is_staged_for_the_normal_completion(self, db_session, library_file, _env):
        _seed(db_session, file_path=library_file)
        # slskd=None is an assertion in itself: reaching the search with a
        # None client would raise, and the result would not be library_reuse.
        result = pipeline.process_queue_item(_item(), slskd=None)

        assert result["status"] == "library_reuse"
        assert result["success"] is True
        staged = Path(_env["downloads"]) / "Madball - Greatest Hits" / "Song.flac"
        assert staged.is_file(), "the file must be copied into the downloads folder"
        assert result["staged_path"] == str(staged)

        # No mover runs here — the row waits for the completion cycle.
        assert not _env["moves"]

        # The row is in the state the completion snapshots…
        assert (4242, {"status": "downloading"}) in [
            (qid, kw) for qid, kw in _env["queue_updates"]
        ]
        # …and the staged file PASSES THE REAL completion matcher, i.e. the
        # cycle will recognise it as this queue item's download.
        from services.downloads import download_completion_service as completion

        matched, _reason = completion._file_matches_queue_item(str(staged), _item())
        assert matched is True, "the completion cycle would never pick this file up"
        assert any(kind == "library_reuse" for kind, _msg in _env["events"])

    def test_a_failed_stage_falls_back_to_searching(self, db_session, library_file, _env, monkeypatch):
        """A broken reuse must never strand the item — the download proceeds."""
        _seed(db_session, file_path=library_file)
        monkeypatch.setattr(
            "services.downloads.download_pipeline_service._stage_library_copy",
            lambda item, src: {"success": False, "error": "disk full"},
        )

        result = pipeline.process_queue_item(_item(), slskd=None)

        assert result.get("success") is False
        assert result.get("status") != "library_reuse"
        assert not _env["moves"]

    def test_no_candidate_means_the_normal_flow_runs(self, db_session, _env):
        """CONTROL — an empty library reaches the search (which fails here
        only because slskd is None)."""
        result = pipeline.process_queue_item(_item(), slskd=None)

        assert result.get("status") != "library_reuse"
        assert result.get("success") is False
        assert not _env["moves"]
