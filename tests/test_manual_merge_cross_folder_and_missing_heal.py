"""Three reported faults:

1. "Merge track into release doesn't work if they are in different folders. As
   this is a manual option, it should override that."
2. "When a missing item is downloaded, it doesn't auto heal. I need to manually
   look up a release again."
3. "The Recording ID and Release Track ID aren't populating when importing from
   soulseek."
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "services" / "metadata" / "album_mbid_guard.py"
ALBUM_SVC = REPO_ROOT / "services" / "metadata" / "album_service.py"
ARTIST_SVC = REPO_ROOT / "services" / "metadata" / "artist_service.py"
ALBUM_ROUTES = REPO_ROOT / "routes" / "album_routes.py"
ARTIST_CORR_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist-corrections.js"
MISSING_SVC = REPO_ROOT / "services" / "metadata" / "album_missing_service.py"
COMPLETION = REPO_ROOT / "services" / "downloads" / "download_completion_service.py"


def _src(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _func(source: str, name: str) -> str:
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1: node.end_lineno])
    raise AssertionError(f"no function {name}")


# ---------------------------------------------------------------------------
# 1. A manual merge may cross folders
# ---------------------------------------------------------------------------

class TestTheFolderBoundaryIsOverrideable:
    def _verdict(self, **kwargs):
        from services.metadata.album_mbid_guard import guard_album_mbid

        return guard_album_mbid(
            artist="Muse", album="Absolution",
            mb_artist="Muse", mb_album="Absolution",
            file_paths=["/music/Muse/2003 - Absolution/01.mp3",
                        "/music/Elsewhere/1999 - Other/02.mp3"],
            **kwargs,
        )

    def test_the_boundary_still_refuses_by_default(self):
        verdict = self._verdict()
        assert verdict["ok"] is False
        assert verdict["reason"] == "multiple_folders"

    def test_a_manual_override_is_accepted(self):
        verdict = self._verdict(enforce_folder_boundary=False)
        assert verdict["ok"] is True, "a manual choice must not be vetoed by a heuristic"
        assert verdict["folder_boundary_overridden"] is True
        # The folders are still REPORTED so the caller can show what it did.
        assert len(verdict["folders"]) == 2

    def test_the_text_check_still_applies_under_override(self):
        """Overriding the folder heuristic must not bypass the identity check."""
        from services.metadata.album_mbid_guard import guard_album_mbid

        verdict = guard_album_mbid(
            artist="Muse", album="Absolution",
            mb_artist="Completely Different Band", mb_album="Nothing Alike",
            file_paths=["/music/A/1.mp3", "/music/B/2.mp3"],
            enforce_folder_boundary=False,
        )
        assert verdict["ok"] is False

    def test_apply_mbid_to_album_accepts_the_override(self):
        body = _func(_src(ALBUM_SVC), "apply_mbid_to_album")
        assert "allow_cross_folder" in body
        assert "enforce_folder_boundary=not allow_cross_folder" in body

    def test_the_route_reads_the_flag(self):
        body = _func(_src(ALBUM_ROUTES), "api_album_apply_mbid")
        assert "allow_cross_folder" in body

    def test_the_artist_corrections_path_accepts_it_too(self):
        body = _func(_src(ARTIST_SVC), "apply_album_mbid")
        assert "enforce_folder_boundary=not bool(payload.get(\"allow_cross_folder\"))" in body

    def test_the_client_offers_the_override_on_a_folder_refusal(self):
        src = _src(ARTIST_CORR_JS)
        assert "applyAlbumMbidRequest" in src
        assert "allow_cross_folder: true" in src
        assert "'Apply anyway'" in src


# ---------------------------------------------------------------------------
# 2. A downloaded missing track stops showing as missing
# ---------------------------------------------------------------------------

class TestTheMissingListHealsAgainstTheLibrary:
    def test_the_read_path_no_longer_returns_rows_verbatim(self):
        body = _func(_src(MISSING_SVC), "get_missing_tracks_from_db")
        assert "_owned_titles" in body, (
            "returning persisted rows verbatim is why a downloaded track kept "
            "showing as missing until a manual lookup"
        )

    def test_it_queries_the_library_in_the_same_session(self):
        body = _func(_src(MISSING_SVC), "get_missing_tracks_from_db")
        assert "FROM tracks " in body
        assert "owned_rows" in body

    def test_it_uses_the_same_title_normalisation_as_the_persistence_layer(self):
        body = _func(_src(MISSING_SVC), "get_missing_tracks_from_db")
        assert "_title_match_key(" in body, (
            "_persist_missing_tracks keys on _title_match_key, so the read-path "
            "filter must use the same normalisation or the two disagree"
        )

    def test_placeholder_rows_are_excluded_from_the_anti_join(self):
        body = _func(_src(MISSING_SVC), "get_missing_tracks_from_db")
        assert "__queued_for_download__" in body, (
            "a queued placeholder row is not a file on disk"
        )

    def test_a_matching_title_is_dropped(self):
        body = _func(_src(MISSING_SVC), "get_missing_tracks_from_db")
        assert "_row_title in _owned_titles" in body

    def test_a_matching_position_is_dropped(self):
        body = _func(_src(MISSING_SVC), "get_missing_tracks_from_db")
        assert "_owned_positions" in body

    def test_the_heal_is_logged(self):
        body = _func(_src(MISSING_SVC), "get_missing_tracks_from_db")
        assert "Missing-track list healed against the library" in body


# ---------------------------------------------------------------------------
# 3. The MBIDs reach the DATABASE at import
# ---------------------------------------------------------------------------

class TestTheImportPersistsTheReleaseTrackId:
    def _mb_cols(self) -> str:
        body = _func(_src(COMPLETION), "_apply_stored_metadata")
        start = body.index("_mb_cols")
        return body[start:start + 2000]

    def test_the_release_track_id_is_allowed_through(self):
        assert '"musicbrainz_releasetrackid"' in self._mb_cols(), (
            "the import resolved it, wrote it to the FILE, and then dropped it "
            "from the DB payload — so the column stayed NULL until a Navidrome "
            "import read the tag back"
        )

    def test_the_legacy_recording_alias_is_allowed_through(self):
        cols = self._mb_cols()
        assert '"mbid"' in cols, (
            "the album review reads `mbid` as Recording ID; without it the "
            "review keeps proposing a value the import already stored"
        )

    def test_the_two_recording_spellings_stay_in_step(self):
        body = _func(_src(COMPLETION), "_apply_stored_metadata")
        assert '_db_payload["mbid"] = _db_payload["recording_mbid"]' in body

    def test_the_column_really_exists_in_the_schema(self):
        schema = _src(REPO_ROOT / "db" / "schema.py")
        assert '"musicbrainz_releasetrackid"' in schema
        assert '"mbid": "TEXT"' in schema
