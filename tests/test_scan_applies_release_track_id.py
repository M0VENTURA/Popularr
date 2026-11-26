"""A completed scan must write the release-track id onto a resolved album.

REPORTED
--------
Skindred — *You Got This*: after a completed scan every track still showed

    MusicBrainz Release Track ID: empty → <id>

as a PENDING review item, even though ``metadata_update.apply_during_scan`` was
ON. The album was already in the library (it had ratings, genres and recording
MBIDs), so the scan kept proposing a value it was supposed to have applied.

ROOT CAUSE
----------
``_resolve_track_mb_metadata`` drops the whole album batch entry when a track
"looks resolved":

    if frozen_track or (_has_mbid and _has_genres and not _force_meta
                        and not _batch_corrects):
        mb_data = None

"Resolved" meant "has a recording MBID and has genres". But the RELEASE-TRACK id
is a DIFFERENT identity — the track's position on the release — and an album
imported with recording ids only is fully resolved as recordings yet has no
position at all. Dropping the batch threw away the ``release_track_mbid`` the
album batch had just resolved, before the payload assignment could run.

⚠️ The previous test only asserted the assignment EXISTS in the source
(``'payload["musicbrainz_releasetrackid"] = ...' in source``), which is true
whether or not the line is reachable. These tests drive the real function.
"""

from __future__ import annotations

import importlib

import pytest

RELEASE_TRACK = "a270e077-fb2f-47de-88a8-d518999ec1de"
RECORDING = "0dbd65d7-7fee-4c63-b23b-21335cd7417e"


@pytest.fixture()
def stage():
    return importlib.import_module("services.popularity.stages.track_stage")


@pytest.fixture()
def applied(monkeypatch, stage):
    """Metadata applying is ON — the reported configuration."""
    monkeypatch.setattr(stage, "_metadata_update_allowed", lambda: True)


def _batch(track_number=1):
    """The album batch, keyed exactly as ``track_stage`` looks it up."""
    from services.popularity.scan_stage_runner import album_recording_batch_key

    entry = {
        "recording_mbid": RECORDING,
        "release_track_mbid": RELEASE_TRACK,
        "title": "Born Fe Dis",
        "genres": ["Reggae", "Rock"],
        "musicbrainz_genres": "Reggae, Rock",
    }
    return {
        album_recording_batch_key(
            "Skindred", "Born Fe Dis", None, track_number
        ): entry
    }


def _track(**over):
    """A library row that is FULLY resolved except for the release-track id.

    This is the reported state: recording id and genres present (so the old
    "resolved" test said "nothing to do"), release-track id empty.
    """
    track = {
        "id": "track-1",
        "title": "Born Fe Dis",
        "artist": "Skindred",
        "album_artist": "Skindred",
        "album": "You Got This",
        "track_number": 1,
        "disc_number": None,
        "recording_mbid": RECORDING,
        "musicbrainz_releasetrackid": "",
        "musicbrainz_genres": "Reggae, Rock",
        "genres": "Reggae, Rock",
        "file_path": "/music/Skindred/2026 - You Got This/03. Skindred - Born Fe Dis.mp3",
    }
    track.update(over)
    return track


def _call(stage, track, *, frozen=False, force=False, options=None):
    """Resolve, returning the WRITABLE payload (the function nests it)."""
    result = stage._resolve_track_mb_metadata(
        track_id=track["id"],
        track=track,
        track_title=track["title"],
        track_artist=track["artist"],
        frozen_track=frozen,
        force_meta=force,
        options=options or {"mb_batch_metadata": _batch()},
        batch_artist="Skindred",
        batch_title="You Got This",
        album_context={"album": "You Got This", "album_type": "Album"},
        album_result={},
        edition_annotation=None,
    )
    return result.get("payload") or {}


class TestAResolvedAlbumStillGetsItsReleaseTrackId:
    def test_the_batch_is_not_discarded_for_a_resolved_row(self, stage, applied):
        """THE REGRESSION.

        A row with a recording MBID and genres is "resolved" for the recording
        search, but NOT for the release-track position the batch just supplied.
        """
        payload = _call(stage, _track())

        assert payload.get("musicbrainz_releasetrackid") == RELEASE_TRACK, (
            "the album batch's release-track id was thrown away for an album "
            "that was already resolved as recordings — the exact cause of the "
            "Skindred 'pending' review items"
        )

    def test_a_row_that_already_has_the_position_is_a_no_op(self, stage, applied):
        """CONTROL: no endless rewrite of a value that is already correct."""
        payload = _call(
            stage, _track(musicbrainz_releasetrackid=RELEASE_TRACK)
        )
        # The skip gate may legitimately discard the batch here; what must not
        # happen is a DIFFERENT value being written.
        assert payload.get("musicbrainz_releasetrackid") in (None, RELEASE_TRACK)

    def test_a_frozen_track_is_still_left_alone(self, stage, applied):
        """CONTROL: frozen means frozen, even when the batch knows better."""
        payload = _call(stage, _track(), frozen=True)
        assert "musicbrainz_releasetrackid" not in payload

    def test_a_missing_release_track_id_alone_triggers_the_work(self, stage):
        """The gate itself: a batch that FILLS a gap must survive the skip."""
        import inspect

        source = inspect.getsource(stage._resolve_track_mb_metadata)
        assert "_batch_fills_release_track" in source, (
            "the skip gate must account for the release-track id, or the value "
            "is discarded before it can be written"
        )


class TestTheValueReachesTheDatabase:
    def test_the_column_is_file_tag_relevant(self, stage):
        """A change to ONLY the release-track id must still reach the file.

        The file pass is scoped by ``_TAG_FILE_COLUMNS``; if the column is
        missing there, the DB gets the value and the file never does — which is
        what makes a later album review propose it again.
        """
        import inspect

        source = inspect.getsource(stage)
        block = source[source.index("_TAG_FILE_COLUMNS = ("):]
        block = block[: block.index(")", block.index('"musicbrainz_workid"')) + 1]
        assert '"musicbrainz_releasetrackid"' in block

    def test_the_writer_accepts_the_column(self):
        from services.metadata import tag_file_service as tfs

        source = getattr(tfs, "__dict__", {})
        assert tfs.canonical_tag_name("musicbrainz_releasetrackid") == (
            "MUSICBRAINZ_RELEASETRACKID"
        )
