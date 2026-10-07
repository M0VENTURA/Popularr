"""A Lookup MBID save may not write genres to the audio files.

Reported:

> Can you confirm that no genres save to the albums when doing an mbid
> lookup? They should only be saving to the tables, genres should only be
> written to the files during the metadata popularity or finalise scan.

Confirmed defect: the Lookup-MBID review stages per-track values (including
``musicbrainz_genres``) into the save's payload, and the save's file phase ran
``build_tag_updates(payload)`` — so the lookup WROTE genre tags to disk. The
database half was always correct; only the file half was wrong.

The full genre→file map after this fix:

==========================================  ================================
Writer                                      Status
==========================================  ================================
album save / Lookup MBID review             **DB only** (this fix)
``apply_genres_to_album`` (``/api/album/apply-genres``)  explicit "write these genres to every file" action
``bulk_tag_tracks`` (bulk tag endpoint)     explicit bulk action
Essentia scanner                            analysis scan
``sync_album_file_tags``                    THE scan sync (popularity / finalise)
==========================================  ================================

The "Cover" marker written by a cover-verdict save is deliberately kept — it
is the cover convention that mirrors the cover detector, not genre data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from routes import ui_routes as ui  # noqa: E402


@pytest.fixture
def file_writes(monkeypatch):
    """Capture what reaches update_file_tags from the album save's file phase."""
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: "/music/x.mp3" if p else None)
    monkeypatch.setattr(
        ui, "update_file_tags",
        lambda path, tags: (seen.append((str(path), dict(tags))) or True),
    )
    return seen


GENRE_KEYS = ("genres", "genre", "musicbrainz_genres", "lastfm_tags", "manual_genres")


class TestTheLookupNeverWritesGenresToFiles:
    def test_a_staged_mb_genre_stays_out_of_the_file(self, file_writes):
        """THE reported path: Lookup MBID stages a per-track MB genre, the
        save persists it to the table — and must write nothing to disk."""
        ok, path = ui._write_album_track_file_tags(
            "t1",
            {"file_path": "/music/x.mp3"},
            {"title": "Car Underwater", "musicbrainz_genres": "Hardcore, Punk"},
            strip_disc_numbers=False,
            disc_staged=False,
        )

        assert ok is True
        assert file_writes, "nothing was written at all — the title should still land"
        written = file_writes[0][1]
        assert not any(key in written for key in GENRE_KEYS), (
            f"the lookup wrote genre tags to the file: {written}"
        )
        assert "title" in written, "non-genre fields must still reach the file"

    def test_every_genre_family_key_is_stripped(self, file_writes):
        """Whatever genre shape the payload carries, the file sees none of it."""
        ui._write_album_track_file_tags(
            "t1",
            {"file_path": "/music/x.mp3"},
            {
                "title": "T",
                "genres": "Rock",
                "musicbrainz_genres": '["Metalcore"]',
                "lastfm_tags": '["punk"]',
                "manual_genres": '["x"]',
            },
            strip_disc_numbers=False,
            disc_staged=False,
        )

        written = file_writes[0][1]
        assert not any(key in written for key in GENRE_KEYS), written

    def test_the_writer_itself_still_understands_genres(self):
        """CONTROL — the strip lives in the SAVE, not in the tag writer; the
        popularity/finalise scan's sync must keep writing genres."""
        from services.metadata import tag_file_service as tfs

        tags = tfs.build_tag_updates({"musicbrainz_genres": "Hardcore, Punk"})
        assert "musicbrainz_genres" in tags


class TestTheCoverMarkerIsUnaffected:
    def test_a_cover_verdict_still_writes_its_marker(self, file_writes):
        """CONTROL — the Cover marker is the cover convention, not genre
        data; it is set AFTER the strip."""
        ok, _path = ui._write_album_track_file_tags(
            "t1",
            {"file_path": "/music/x.mp3"},
            {"title": "T", "is_cover": True},
            strip_disc_numbers=False,
            disc_staged=False,
        )

        assert ok is True
        assert file_writes[0][1].get("genre") == "Cover"

    def test_the_marker_wins_even_when_the_payload_had_genres(self, file_writes):
        ui._write_album_track_file_tags(
            "t1",
            {"file_path": "/music/x.mp3"},
            {"title": "T", "is_cover": True, "musicbrainz_genres": "Hardcore"},
            strip_disc_numbers=False,
            disc_staged=False,
        )

        written = file_writes[0][1]
        assert written.get("genre") == "Cover"
        assert "musicbrainz_genres" not in written


class TestTheStripStaysInTheSavePath:
    def test_the_file_phase_drops_genre_fields(self):
        source = (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")
        assert "_FILE_GENRE_TAG_FIELDS" in source
        # Inside the file-phase helper, between build_tag_updates and the write.
        fn = source[source.index("def _write_album_track_file_tags"):]
        fn = fn[: fn.index("def _write_album_track_files_concurrently")]
        assert "for _genre_field in _FILE_GENRE_TAG_FIELDS:" in fn
        assert "file_tags.pop(_genre_field, None)" in fn
        assert fn.index("file_tags.pop(_genre_field, None)") < fn.index("update_file_tags(resolved, file_tags)"), (
            "the strip must run BEFORE the file write"
        )

    @pytest.mark.parametrize(
        ("rel", "marker"),
        [
            # The explicit actions and the scan — documented as the allowed
            # genre→file writers (see the module docstring).
            ("services/metadata/album_service.py", "apply_genres_to_album"),
            ("services/metadata/album_service.py", "bulk_tag_tracks"),
            ("services/metadata/album_tag_sync_service.py", "sync_album_file_tags("),
            ("services/scanning/pipelines/essentia_scanner.py", "update_file_tags(file_path"),
        ],
    )
    def test_the_documented_writers_still_exist(self, rel: str, marker: str):
        """The confirmation inventory — if one of these disappears, the
        changelog's map is stale."""
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert marker in source, f"{rel}: expected `{marker}`"
