"""Tests for release-title album naming (edition-marker stripping).

Album naming should be based on the RELEASE TITLE, not the edition folder
Navidrome uses.  "Slipknot (Clean)" must store as "Slipknot"; the
parenthetical is an edition marker (Clean / Deluxe Edition / Remaster ...),
not part of the release's canonical title.  Live/Remix markers are preserved
because they change what the album IS.
"""

from __future__ import annotations

from services.scanning.navidrome_import import compute_artist_album_diff
from helpers.normalization_service import strip_album_edition_marker


class TestStripAlbumEditionMarker:
    def test_strips_clean_and_explicit(self):
        assert strip_album_edition_marker("Slipknot (Clean)") == "Slipknot"
        assert strip_album_edition_marker("Slipknot [Clean]") == "Slipknot"
        assert strip_album_edition_marker("Eminem (Explicit)") == "Eminem"

    def test_strips_deluxe_and_edition_suffixes(self):
        assert strip_album_edition_marker("Weezer (Deluxe Edition)") == "Weezer"
        assert strip_album_edition_marker("Weezer (Deluxe)") == "Weezer"
        assert strip_album_edition_marker("OK Computer (Special Edition)") == "OK Computer"
        assert strip_album_edition_marker("Abbey Road (Anniversary Edition)") == "Abbey Road"
        assert strip_album_edition_marker("The Wall (Remastered)") == "The Wall"

    def test_preserves_album_type_markers(self):
        # Live / Remix / Acoustic change what the album IS — never stripped.
        assert strip_album_edition_marker("Live at Wembley") == "Live at Wembley"
        assert strip_album_edition_marker("The Remixes") == "The Remixes"
        assert strip_album_edition_marker("Unplugged (Live)") == "Unplugged (Live)"

    def test_preserves_plain_titles_and_mid_string_parens(self):
        assert strip_album_edition_marker("Slipknot") == "Slipknot"
        assert strip_album_edition_marker("(What's the Story) Morning Glory?") == "(What's the Story) Morning Glory?"

    def test_no_marker_returns_input(self):
        assert strip_album_edition_marker("") == ""

    def test_strips_repress_marker(self):
        """Reported (2026-10-09): Navidrome builds ``AlbumID3.name`` as
        ``Title + " (" + Edition + ")"``, so a repress arrived as
        "Nine Destinies and a Downfall (repress)".  ``repress`` was missing
        from the edition vocabulary, so the import pasted it into
        ``tracks.album`` and every subsequent scan reverted the album the user
        had renamed back to the edition form."""
        clean = "Nine Destinies and a Downfall"

        assert strip_album_edition_marker(f"{clean} (repress)") == clean
        assert strip_album_edition_marker(f"{clean} (Repress)") == clean
        # A dated pressing annotation — the year is set aside, not discarded.
        assert strip_album_edition_marker(f"{clean} (2011 Repress)") == clean
        # The unbracketed spelling Navidrome writes for "X Repress".
        assert strip_album_edition_marker(f"{clean} Repress") == clean

        # Idempotent: re-stripping a clean name is a no-op.
        stripped = strip_album_edition_marker(f"{clean} (repress)")
        assert strip_album_edition_marker(stripped) == stripped

        # A title that IS only the marker keeps its original form.
        assert strip_album_edition_marker("Repress") == "Repress"

    def test_preserves_recordings_and_plain_year_markers(self):
        """Adding ``repress`` must not widen what gets stripped."""
        assert strip_album_edition_marker("Nine Destinies and a Downfall (2016)") == (
            "Nine Destinies and a Downfall (2016)"
        )
        assert strip_album_edition_marker("Unplugged (Live)") == "Unplugged (Live)"
        assert strip_album_edition_marker("Slipknot (Clean)") == "Slipknot"


class TestArtistAlbumNameDiffEditionStripping:
    """The diff compares Navidrome names against release-title DB names."""

    def test_legacy_edition_suffixed_db_row_not_removed_and_flagged_changed(self, monkeypatch):
        """DB stores "Slipknot (Clean)" (pre-migration); Navidrome (stripped)
        says "Slipknot".  The album must NOT be removed, and must be flagged
        CHANGED so the import re-runs and the upsert rewrites the album column
        to the release title."""

        class _FakeSession:
            def execute(self, sql, params=None):
                rows = [("Slipknot (Clean)", 12)]
                if "album_artist" in str(sql) and "COUNT" in str(sql):
                    rows = [("Slipknot (Clean)", 12)]
                return _FakeResult(rows)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        import services.scanning.navidrome_import as ndi

        # Patch the name as BOUND IN navidrome_import: it does
        # `from db.engine import db_session` at import time, so patching
        # db.engine.db_session would leave the already-bound reference alone.
        monkeypatch.setattr(ndi, "db_session", lambda: _FakeSession())

        class _FakeClient:
            def fetch_artist_albums(self, artist_id):
                return [{"id": "al-1", "name": "Slipknot (Clean)", "songCount": 12}]

        skip, changed, removed = compute_artist_album_diff(
            "Slipknot",
            _FakeClient().fetch_artist_albums("ar-1"),
        )
        assert skip is False
        assert changed == {"Slipknot"}
        assert removed == set()

    def test_genuinely_removed_album_still_removed(self, monkeypatch):
        """A DB album with no Navidrome counterpart (even after stripping) is
        still reported as removed."""

        class _FakeSession:
            def execute(self, sql, params=None):
                return _FakeResult([("Gone Album", 5)])

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        import services.scanning.navidrome_import as ndi

        monkeypatch.setattr(ndi, "db_session", lambda: _FakeSession())

        class _FakeClient:
            def fetch_artist_albums(self, artist_id):
                return [{"id": "al-2", "name": "Current Album", "songCount": 3}]

        skip, changed, removed = compute_artist_album_diff(
            "Some Artist",
            _FakeClient().fetch_artist_albums("ar-2"),
        )
        assert skip is False
        assert removed == {"Gone Album"}

    def test_db_row_still_carrying_repress_is_flagged_changed(self, monkeypatch):
        """A row stored BEFORE ``repress`` joined the edition vocabulary keeps
        the marker; the Navidrome name now strips to the clean title, so the
        raw DB name no longer matches.  The diff must flag it CHANGED rather
        than skip the artist, or the corrupted name would never self-heal."""

        class _FakeSession:
            def execute(self, sql, params=None):
                return _FakeResult([("Nine Destinies and a Downfall (repress)", 10)])

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        import services.scanning.navidrome_import as ndi

        monkeypatch.setattr(ndi, "db_session", lambda: _FakeSession())

        class _FakeClient:
            def fetch_artist_albums(self, artist_id):
                # Navidrome still serves the edition form in AlbumID3.name.
                return [{
                    "id": "al-3",
                    "name": "Nine Destinies and a Downfall (repress)",
                    "songCount": 10,
                }]

        skip, changed, removed = compute_artist_album_diff(
            "Sirenia",
            _FakeClient().fetch_artist_albums("ar-3"),
        )
        assert skip is False, "the corrupted row must be re-imported, not skipped"
        assert changed == {"Nine Destinies and a Downfall"}
        assert removed == set()
