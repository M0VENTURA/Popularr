"""Regression: Navidrome glues the album VERSION (MusicBrainz disambiguation)
onto ``tracks.album`` on every import.

Reported (2026-10-10): "This album is still being grabbed from Navidrome with
the subtitle added to the track -- 17: Greatest Hits (1999 Album)". The file's
own ``album`` tag is the clean ``17: Greatest Hits``; the extra
``(1999 Album)`` is the MusicBrainz release DISAMBIGUATION carried in the
``musicbrainz_albumcomment`` tag.

Root cause (proven from Navidrome source):

1. ``resources/mappings.yaml`` maps ``musicbrainz_albumcomment`` onto the
   internal ``albumversion`` tag.
2. ``model/album.go::FullName()`` appends that tag via ``appendSuffix`` when
   ``Subsonic.AppendAlbumVersion`` is on (the default), so
   ``server/subsonic/helpers.go::buildAlbumID3`` sets ``Name = FullName()``.
3. The Subsonic API therefore only ever hands out the MERGED name, and the
   edition-keyword stripper cannot catch a free-text disambiguation -- so the
   subtitle was written into ``tracks.album`` and glued onto every track.

The API ALSO carries the same value in the album's separate ``version`` field
(``OpenSubsonicAlbumID3.Version``), so the fix strips the suffix by MATCHING
the value Navidrome appended rather than by guessing keywords.
"""

from __future__ import annotations

from helpers.normalization_service import (
    strip_album_edition_marker,
    strip_appended_album_version,
)
from services.scanning.navidrome_import import (
    clean_navidrome_album_name,
    compute_artist_album_diff,
)


class TestStripAppendedAlbumVersion:
    """``strip_appended_album_version`` removes only what Navidrome appended."""

    def test_reported_greatest_hits_case(self):
        assert strip_appended_album_version(
            "17: Greatest Hits (1999 Album)", "1999 Album"
        ) == "17: Greatest Hits"

    def test_already_bracketed_version_is_appended_bare(self):
        # appendSuffix appends an already-bracketed version with a bare space.
        assert strip_appended_album_version(
            "Nine Destinies and a Downfall (repress)", "(repress)"
        ) == "Nine Destinies and a Downfall"

    def test_case_insensitive_match(self):
        assert strip_appended_album_version(
            "Some Album (1999 album)", "1999 Album"
        ) == "Some Album"

    def test_year_inside_version_is_stripped(self):
        assert strip_appended_album_version(
            "Some Album (2011 Remaster)", "2011 Remaster"
        ) == "Some Album"

    # --- guards: never a blind "strip the last bracket" ---------------------

    def test_mismatched_version_is_left_alone(self):
        """A real subtitle that is NOT the version must survive."""
        assert strip_appended_album_version(
            "Some Album (Live at Wembley)", "1999 Album"
        ) == "Some Album (Live at Wembley)"

    def test_missing_version_is_left_alone(self):
        assert strip_appended_album_version("Some Album", "1999 Album") == "Some Album"

    def test_empty_version_is_left_alone(self):
        assert strip_appended_album_version("Some Album (reissue)", "") == (
            "Some Album (reissue)"
        )

    def test_name_that_is_only_the_version_is_not_emptied(self):
        assert strip_appended_album_version("1999 Album", "1999 Album") == "1999 Album"

    def test_inner_year_survives_the_version_strip(self):
        # The version is the OUTERMOST suffix; an inner year must survive.
        assert strip_appended_album_version(
            "Live Aid (2011) (1999 Album)", "1999 Album"
        ) == "Live Aid (2011)"

    def test_idempotent(self):
        once = strip_appended_album_version("17: Greatest Hits", "1999 Album")
        assert strip_appended_album_version(once, "1999 Album") == once


class TestCleanNavidromeAlbumName:
    """The import's single album-name derivation point."""

    def test_strips_version_then_edition_marker(self):
        # Version is outermost, so it is stripped first; the edition marker
        # underneath is then removed by the usual stripper.
        album = {
            "name": "Some Album (deluxe edition) (1999 Album)",
            "version": "1999 Album",
        }
        assert clean_navidrome_album_name(album) == "Some Album"

    def test_reported_case(self):
        album = {"name": "17: Greatest Hits (1999 Album)", "version": "1999 Album"}
        assert clean_navidrome_album_name(album) == "17: Greatest Hits"

    def test_no_version_falls_back_to_edition_strip(self):
        album = {"name": "Some Album (reissue)", "version": ""}
        assert clean_navidrome_album_name(album) == "Some Album"

    def test_recording_form_marker_survives(self):
        album = {"name": "Unplugged (Live)", "version": ""}
        assert clean_navidrome_album_name(album) == "Unplugged (Live)"

    def test_plain_title_unchanged(self):
        album = {"name": "The Wall", "version": ""}
        assert clean_navidrome_album_name(album) == "The Wall"


class _FakeDiffSession:
    """Minimal ``db_session`` stand-in returning a fixed (album, count) list."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql, _params=None):
        rows = self._rows

        class _Result:
            def fetchall(self):
                return rows

        return _Result()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDiffSelfHealsLegacyVersionSuffix:
    """A row stored BEFORE this fix keeps the glued subtitle and must self-heal.

    ``compute_artist_album_diff`` strips only the edition marker on the DB side
    (the version is not recoverable from ``tracks.album`` alone), so the legacy
    raw name no longer matches the cleaned Navidrome name. That mismatch is
    deliberate and load-bearing: it flags the album CHANGED so the import
    re-runs and rewrites ``tracks.album`` to the clean title.
    """

    def _run(self, monkeypatch, db_rows, nav_albums):
        import services.scanning.navidrome_import as ndi

        monkeypatch.setattr(ndi, "db_session", lambda: _FakeDiffSession(db_rows))
        return compute_artist_album_diff("Ricky Martin", nav_albums)

    def test_legacy_row_is_changed_not_skipped(self, monkeypatch):
        skip, changed, removed = self._run(
            monkeypatch,
            [("17: Greatest Hits (1999 Album)", 14)],
            [
                {
                    "id": "al-1",
                    "name": "17: Greatest Hits (1999 Album)",
                    "version": "1999 Album",
                    "songCount": 14,
                }
            ],
        )
        assert skip is False, "the corrupted row must be re-imported, not skipped"
        assert "17: Greatest Hits" in changed
        # The legacy name DOES appear in the name-level `removed` set (the DB
        # side cannot recover the version from ``tracks.album`` alone), but that
        # is harmless: the removed-album cleanup subtracts the LIVE track ids
        # the import collected, and a re-homed album's ids are still alive — so
        # nothing is deleted. The id-level safety net is what protects the rows,
        # not the name-level set.
        assert "17: Greatest Hits (1999 Album)" in removed

    def test_fully_cleaned_row_is_skipped(self, monkeypatch):
        """After one self-heal the DB holds the clean name and the scan skips."""
        skip, changed, removed = self._run(
            monkeypatch,
            [("17: Greatest Hits", 14)],
            [
                {
                    "id": "al-1",
                    "name": "17: Greatest Hits (1999 Album)",
                    "version": "1999 Album",
                    "songCount": 14,
                }
            ],
        )
        assert skip is True
        assert changed == set()
        assert removed == set()


class TestLegacySubtitleIsNotStrippedByEditionAlone:
    """Pins WHY the version field is required: no keyword list can catch it."""

    def test_edition_stripper_alone_keeps_the_disambiguation(self):
        assert strip_album_edition_marker("17: Greatest Hits (1999 Album)") == (
            "17: Greatest Hits (1999 Album)"
        )
