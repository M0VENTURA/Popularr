"""Regression: the album page's Save must report what it actually did.

Four defects combined so that a save which DID write data was reported as
"No changes were made." — or, worse, so that data the user typed was written
nowhere at all:

1. GENRES BYPASSED THE COUNTER.  Genres are written by
   ``db.repositories.metadata.update_track_genres``, not through the per-track
   ``payload``, so the loop never incremented ``updated_count``.  Saving an
   album whose only edit was the genre chips wrote the genres and then flashed
   "No changes were made."  Fixed by counting those writes (``genre_only_writes``)
   and reporting "Album genres saved — N track(s) updated."

2. NOTHING-TO-SAVE WAS MASKED.  When the album matched NO track rows at all,
   the same message appeared.  That claimed the values were already correct
   when nothing had even been considered.  Now warns "No tracks found for this
   album — nothing could be saved."

3. A SILENTLY DROPPED FIELD.  ``track_comment`` was read from the form and then
   never used — the same shape of bug.  It is NOT fixed by assigning it to a
   payload key, because the ``tracks`` table has NO ``comment`` column:
   ``save_to_db`` filters unknown keys out, so such an assignment would be a
   silent no-op that only looked like a fix.  The form field is also gone from
   every current template (it existed in ``old_system`` only).  This test pins
   that fact so nobody "fixes" it by writing to a non-existent column.

4. The album-level form fields that ARE applied are pinned too, so a future
   refactor of the loop cannot quietly drop one the way ``track_comment`` was.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"


@pytest.fixture(scope="module")
def ui_source() -> str:
    return UI_ROUTES.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def album_save(ui_source: str) -> str:
    """The POST branch of ``album_detail`` (from the genre block to the flash)."""
    start = ui_source.index("genre_only_writes = 0")
    end = ui_source.index('flash("No changes were made.", "info")')
    return ui_source[start:end]


class TestGenresAreCounted:
    def test_genre_writes_are_counted(self, album_save: str):
        """``update_track_genres`` returns a rowcount that must be tallied.

        Without this the genres reached the database and the page still said
        nothing had changed.
        """
        assert "genre_only_writes = 0" in album_save
        assert "_genre_rows = update_track_genres(" in album_save
        assert "if _genre_rows:" in album_save
        assert "genre_only_writes += 1" in album_save

    def test_genre_only_save_reports_success(self, ui_source: str):
        """A genres-only save reports the genre write, not "no changes"."""
        idx = ui_source.index("if updated_count == 0 and reverted_live_count == 0")
        window = ui_source[idx: idx + 1200]
        assert "if genre_only_writes:" in window
        assert "Album genres saved" in window
        # The genre branch must come BEFORE the nothing-to-save fallbacks, or a
        # genres-only save would hit the warning first.
        assert window.index("if genre_only_writes:") < window.index("elif not tracks:")


class TestNothingToSaveIsNotMasked:
    def test_empty_album_warns_instead_of_claiming_no_changes(self, ui_source: str):
        idx = ui_source.index("if updated_count == 0 and reverted_live_count == 0")
        window = ui_source[idx: idx + 1200]
        assert "elif not tracks:" in window
        assert "No tracks found for this album" in window

    def test_no_changes_message_still_exists_as_the_final_fallback(self, ui_source: str):
        idx = ui_source.index("if updated_count == 0 and reverted_live_count == 0")
        window = ui_source[idx: idx + 1200]
        assert 'await flash("No changes were made.", "info")' in window
        # It must be the LAST branch (a real no-op), never the first.
        assert window.index("elif not tracks:") < window.index(
            'await flash("No changes were made.", "info")'
        )


class TestNoCommentColumn:
    def test_tracks_table_has_no_comment_column(self):
        """Writing ``payload["comment"]`` would be silently dropped.

        ``save_to_db`` filters the payload to real columns, so a "fix" that
        assigns a comment key compiles, runs, and changes nothing.  The form
        field does not exist in the current templates either.
        """
        schema = (REPO_ROOT / "db" / "schema.py").read_text(encoding="utf-8")
        match = re.search(r'"tracks": \{(.*?)\n    \},', schema, re.S)
        assert match, "tracks column registry not found in db/schema.py"
        block = match.group(1)
        assert '"comment"' not in block, (
            "tracks gained a comment column — the album page's track_comment "
            "field can now be wired up for real"
        )

    def test_save_only_writes_known_columns(self):
        """The guard that makes an unknown payload key a no-op."""
        repo = (REPO_ROOT / "db" / "repositories" / "popularity_repository.py").read_text(
            encoding="utf-8"
        )
        assert "if k in columns and k != \"_navidrome_sync\"" in repo


class TestAlbumFieldsAreApplied:
    """The album-level values that ARE wired into the payload stay wired."""

    @pytest.mark.parametrize(
        "snippet",
        [
            'payload["release_title"] = release_title',
            'payload["release_year"] = int(release_year)',
            'payload["musicbrainz_album_mbid"] = album_mbid',
            'payload["musicbrainz_releasegroupid"] = album_rg_mbid',
            'payload["musicbrainz_artistid"] = artist_mbid',
            'payload["discogs_album_id"] = discogs_id',
            'payload["musicbrainz_albumtype"] = album_type',
            'payload["writer"] = track_composer',
        ],
    )
    def test_field_is_written(self, album_save: str, snippet: str):
        assert snippet in album_save, f"album-level field stopped being applied: {snippet}"

    def test_original_year_is_preferred_over_edition_year(self, album_save: str):
        """``year`` is the ORIGINAL year; ``release_year`` is the edition's.

        Swapping them made every remaster claim to be the original release.
        """
        idx = album_save.index('payload["year"] = original_year')
        assert album_save.index('payload["year"] = release_year') > idx, (
            "the publication order changed — original_year must win"
        )
