"""Scan-time track identity + the file year-tag split.

Four reported behaviours, all decided by the SCAN (``scan_hooks``) so the DB
row and the file tags share one canonical record:

1. a featured credit found in the TITLE moves onto the ARTIST
   ("Evanescence - Bring Me To Life feat 12 stones" -> artist
   "Evanescence feat. 12 Stones", title "Bring Me To Life"), while the album
   artist stays "Evanescence" (and is left alone entirely for a
   various-artists compilation);
2. a title falsely carrying a cover attribution — "Song (Disturbed Cover)" —
   loses the wording, and the cover verdict that wording caused is CLEARED;
3. "Remastered" / "Remastered 2026" is removed from the end of a title (the
   year inside it is deliberately discarded);
4. the file's DATE/YEAR tag carries the EDITION (remaster/re-release) year
   while ORIGINALYEAR/ORIGINALDATE carry the album's ORIGINAL year — for MP3
   (ID3) and FLAC (Vorbis) alike. ``year`` stays the ORIGINAL in the DB, which
   is what album sorting reads.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# 1. Featured credit relocation
# ---------------------------------------------------------------------------

class TestFeaturedCreditMovesToArtist:
    def _identity(self, **kwargs):
        from helpers.normalization_service import normalise_scan_track_identity
        return normalise_scan_track_identity(**kwargs)

    def test_the_reported_example(self):
        result = self._identity(
            artist="Evanescence",
            title="Bring Me To Life feat 12 stones",
            album_artist="Evanescence",
        )
        assert result["title"] == "Bring Me To Life"
        assert result["artist"] == "Evanescence feat. 12 Stones"
        assert result["album_artist"] == "Evanescence"
        assert result["featured_artist"] == "12 Stones"
        assert result["title_had_featured_credit"] is True

    def test_bracketed_credit_is_moved_too(self):
        result = self._identity(
            artist="Evanescence",
            title="Bring Me To Life (feat. 12 Stones)",
            album_artist="Evanescence",
        )
        assert result["title"] == "Bring Me To Life"
        assert result["artist"] == "Evanescence feat. 12 Stones"

    def test_an_existing_credit_is_never_doubled(self):
        result = self._identity(
            artist="Evanescence feat. Amy Lee",
            title="Song feat. 12 Stones",
            album_artist="Evanescence",
        )
        assert result["artist"] == "Evanescence feat. Amy Lee"
        assert result["title"] == "Song"

    def test_album_artist_is_not_replaced_by_the_track_artist(self):
        """A guest track on someone else's album keeps that album artist."""
        result = self._identity(
            artist="Guests",
            title="Song feat. Someone",
            album_artist="The Album Owner",
        )
        assert result["artist"] == "Guests feat. Someone"
        assert result["album_artist"] == "The Album Owner"

    def test_album_artist_credit_is_reduced_to_the_primary(self):
        result = self._identity(
            artist="Guests",
            title="Song",
            album_artist="Evanescence feat. Someone",
        )
        assert result["album_artist"] == "Evanescence"

    def test_various_artists_compilation_keeps_its_album_artist(self):
        result = self._identity(
            artist="Some Band",
            title="Song feat. Guest",
            album_artist="Various Artists",
            is_va_compilation=True,
        )
        assert result["album_artist"] == "Various Artists"
        assert result["artist"] == "Some Band feat. Guest"

    def test_an_empty_album_artist_is_filled_from_the_primary_artist(self):
        result = self._identity(
            artist="Evanescence",
            title="Song feat. 12 Stones",
            album_artist="",
        )
        assert result["album_artist"] == "Evanescence"

    @pytest.mark.parametrize(
        "title",
        [
            "Me & You",
            "Dance with the Devil",
            "Featuring You",  # a title, not a credit — no whitespace-guest form
            "Farewell",
            "With or Without You",
        ],
    )
    def test_titles_that_only_look_like_credits_are_untouched(self, title):
        result = self._identity(artist="Artist", title=title, album_artist="Artist")
        assert result["title"] == title
        assert result["artist"] == "Artist"

    def test_a_title_that_is_only_a_credit_is_never_emptied(self):
        result = self._identity(artist="Artist", title="feat. Somebody", album_artist="Artist")
        assert result["title"].strip() != ""

    def test_guest_casing_is_preserved_unless_entirely_lower_case(self):
        from helpers.normalization_service import _clean_featured_name

        assert _clean_featured_name("12 stones") == "12 Stones"
        assert _clean_featured_name("MC Solaar") == "MC Solaar"
        # str.title() would produce "Will.I.Am" — the guard must not.
        assert _clean_featured_name("will.i.am") == "will.i.am"


class TestAlbumKeyVariants:
    def test_both_spellings_are_matched(self):
        from helpers.normalization_service import album_artist_key_variants

        assert album_artist_key_variants("Evanescence feat. 12 Stones") == [
            "Evanescence feat. 12 Stones", "Evanescence",
        ]
        assert album_artist_key_variants("Evanescence") == ["Evanescence"]
        assert album_artist_key_variants("") == []


class TestAlbumKeyCannotMove:
    """The album key is ``COALESCE(NULLIF(album_artist,''), artist)``.

    Moving a featured credit onto ``artist`` (or filling an empty album artist)
    would move the whole album to a DIFFERENT key mid-scan, and every
    album-scoped lookup issued with the pre-move spelling — file-tag sync,
    release MBID, extended metadata — would silently find nothing. The rule
    below is what makes that impossible.
    """

    def _writes_album_artist(self, identity, existing_album_artist: str) -> str:
        """Mirror of the rule in ``scan_hooks.prepare_track_context``."""
        if str(existing_album_artist or "").strip():
            return ""
        if identity["artist_already_had_credit"]:
            return ""
        return identity["album_artist"]

    def _identity(self, **kwargs):
        from helpers.normalization_service import normalise_scan_track_identity
        return normalise_scan_track_identity(**kwargs)

    def test_filling_an_empty_album_artist_preserves_the_key(self):
        identity = self._identity(
            artist="Evanescence", title="Song feat. 12 Stones", album_artist="",
        )
        # Key before the move: the feat.-free artist. After: the album artist.
        assert self._writes_album_artist(identity, "") == "Evanescence"

    def test_a_credit_laden_artist_gets_no_album_artist_write(self):
        identity = self._identity(
            artist="Evanescence feat. Amy Lee", title="Song feat. 12 Stones", album_artist="",
        )
        assert identity["artist_already_had_credit"] is True
        assert self._writes_album_artist(identity, "") == ""

    def test_a_populated_album_artist_is_never_written(self):
        identity = self._identity(
            artist="Evanescence", title="Song feat. 12 Stones", album_artist="Evanescence",
        )
        assert self._writes_album_artist(identity, "Evanescence") == ""


# ---------------------------------------------------------------------------
# 2. False cover wording
# ---------------------------------------------------------------------------

class TestCoverWording:
    def _identity(self, title):
        from helpers.normalization_service import normalise_scan_track_identity
        return normalise_scan_track_identity(artist="Artist", title=title, album_artist="Artist")

    def test_the_reported_wording_is_removed(self):
        result = self._identity("Song (Disturbed Cover)")
        assert result["title"] == "Song"
        assert result["title_had_cover_wording"] is True

    def test_bracket_variants(self):
        assert self._identity("Song [Cover Version]")["title"] == "Song"

    def test_a_title_merely_containing_the_word_is_not_a_cover(self):
        result = self._identity("Cover Me")
        assert result["title"] == "Cover Me"
        assert result["title_had_cover_wording"] is False

    def test_the_legacy_probe_no_longer_flags_a_bare_word(self):
        from helpers.normalization_service import detect_cover_and_normalize_title

        assert detect_cover_and_normalize_title("Cover Me")[0] is False
        assert detect_cover_and_normalize_title("Song (Disturbed Cover)")[0] is True


class TestCoverFlagIsCleared:
    """The reported "falsely created as a cover" must actually go away."""

    class _Sink:
        """``process_track`` pushes the effective row through
        ``options['_deferred_persist']``. The production contract is ``.add(row)``
        and a plain ``set`` cannot hold a dict, so this mirrors it."""

        def __init__(self):
            self.rows: list[dict] = []

        def add(self, row):
            self.rows.append(row)

    def _run(self, track_overrides, sink):
        from services.popularity.stages import track_stage

        track = {
            "id": "t1",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "album_artist": "Artist",
            "recording_mbid": "rec-1",
            "musicbrainz_genres": '["Cover", "Rock"]',
            "file_path": "Artist/Album/1.mp3",
            "duration": 200,
        }
        track.update(track_overrides)
        track_stage.process_track(
            track=track,
            track_context={"track": track, "artist": "Artist", "title": "Song"},
            album_context={"album": "Album", "artist": "Artist", "tracks": [track]},
            album_result={},
            options={"metadata_only": True, "_deferred_persist": sink},
        )
        return sink.rows[0]

    def test_a_false_cover_from_the_wording_is_not_cleared_by_the_stage(self):
        """⚠️ INVERTED 2026-09-24 — the stage must no longer clear the flag.

        This test previously asserted the opposite. That behaviour was the
        reported bug: the stage runs a SHALLOW cover check, so a ``no_match``
        there is not evidence of anything, and acting on it deleted the flag
        and genre of genuine covers (Kenny Rogers' "Ruby, Don't Take Your Love
        to Town", "Mahna, Mahna", "Multiply the Heartaches") before the deep
        detection pass — MusicBrainz work relations, ISRC, writer coverage —
        ever ran.

        Clearing now happens at the END of the deep pass. See
        ``test_cover_verdict_cleared_only_after_deep_detection.py``.
        """
        sink = self._Sink()
        result = self._run(
            {
                "is_cover": 1,
                "original_cover_artist": "Disturbed",
                "title_had_cover_wording": True,
            },
            sink,
        )
        assert result["is_cover"] in (1, True), (
            "the track stage cleared the flag from a shallow `no_match`; only "
            "the deep detection pass may clear a cover verdict"
        )
        # The genre the verdict had added is left for the deep pass to re-judge.
        assert "Cover" in str(result.get("musicbrainz_genres") or "")
        assert "Rock" in str(result.get("musicbrainz_genres") or "")

    def test_the_wording_is_still_stripped_from_the_title(self):
        """The legitimate half of the branch must keep working."""
        sink = self._Sink()
        result = self._run(
            {
                "is_cover": 1,
                "original_cover_artist": "Disturbed",
                "title_had_cover_wording": True,
            },
            sink,
        )
        assert result.get("title") == "Song", (
            "the '(Disturbed Cover)' suffix must still be removed from the title"
        )

    def test_a_manual_override_is_never_cleared(self):
        sink = self._Sink()
        result = self._run(
            {
                "is_cover": 1,
                "original_cover_artist": "Disturbed",
                "cover_manual_override": 1,
                "title_had_cover_wording": True,
            },
            sink,
        )
        assert result["is_cover"] in (1, True)

    def test_an_unrelated_track_keeps_its_cover_verdict(self):
        sink = self._Sink()
        result = self._run({"is_cover": 1, "original_cover_artist": "Disturbed"}, sink)
        assert result["is_cover"] in (1, True)


# ---------------------------------------------------------------------------
# 3. Remastered markers
# ---------------------------------------------------------------------------

class TestRemasterMarkers:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Song (Remastered)", "Song"),
            ("Song (Remastered 2026)", "Song"),
            ("Song (2026 Remaster)", "Song"),
            ("Song (2011 Remastered)", "Song"),
            ("Song - 2026 Remastered", "Song"),
            ("Song (Remastered Version)", "Song"),
            ("Song [Remastered]", "Song"),
        ],
    )
    def test_markers_are_removed(self, title, expected):
        from helpers.normalization_service import normalise_scan_track_identity

        result = normalise_scan_track_identity(artist="Artist", title=title, album_artist="Artist")
        assert result["title"] == expected
        assert result["title_had_remaster_wording"] is True

    def test_a_degenerate_title_is_never_emptied(self):
        from helpers.normalization_service import normalise_scan_track_identity

        result = normalise_scan_track_identity(artist="A", title="(Remastered)", album_artist="A")
        assert result["title"].strip() != ""

    def test_the_year_in_the_marker_is_discarded(self):
        """Agreed rule: years come from MusicBrainz, never from a title."""
        from helpers.normalization_service import normalise_scan_track_identity

        result = normalise_scan_track_identity(
            artist="Artist", title="Song (Remastered 2026)", album_artist="Artist",
        )
        assert "2026" not in result["title"]
        # No field may smuggle the title's year through. (``isinstance(v, int)``
        # is not usable here — ``bool`` is a subclass of ``int``.)
        assert all("2026" not in str(v) for v in result.values())

    def test_stacked_markers_all_come_off(self):
        from helpers.normalization_service import normalise_scan_track_identity

        result = normalise_scan_track_identity(
            artist="Artist",
            title="Song (Disturbed Cover) (2011 Remastered)",
            album_artist="Artist",
        )
        assert result["title"] == "Song"
        assert result["title_had_cover_wording"] is True
        assert result["title_had_remaster_wording"] is True


# ---------------------------------------------------------------------------
# 4. Year tags: DATE = edition, ORIGINALDATE/YEAR = original
# ---------------------------------------------------------------------------

class TestDbTagCandidatesYearSplit:
    def _cands(self, **overrides):
        from services.metadata.album_tag_sync_service import _db_tag_candidates

        track = {"title": "Song", "artist": "A", "album": "Album", "album_artist": "A"}
        track.update(overrides)
        return _db_tag_candidates(track, "1995", perfect=False, include_lyrics=False, edition_year="2026")

    def test_date_takes_the_edition_year_and_the_original_goes_to_the_pair(self):
        cands = self._cands(year="1995", release_year="2026")
        assert cands["year"] == "2026"
        assert cands["originalyear"] == "1995"
        assert cands["originaldate"] == "1995"

    def test_without_an_edition_year_the_original_still_dates_the_file(self):
        from services.metadata.album_tag_sync_service import _db_tag_candidates

        cands = _db_tag_candidates(
            {"title": "Song", "year": "1995"}, "1995",
            perfect=False, include_lyrics=False, edition_year="",
        )
        assert cands["year"] == "1995"
        assert cands["originalyear"] == "1995"

    def test_a_stored_originalyear_wins_over_the_column_fallback(self):
        cands = self._cands(year="1995", originalyear="1994", release_year="2026")
        assert cands["year"] == "2026"
        assert cands["originalyear"] == "1994"

    def test_a_full_original_date_is_kept_verbatim(self):
        cands = self._cands(year="1995", originaldate="1995-03-14", release_year="2026")
        assert cands["originaldate"] == "1995-03-14"


class TestEditionYearResolver:
    def test_majority_wins(self):
        from services.metadata.album_tag_sync_service import _resolve_album_edition_year

        tracks = [
            {"release_year": 2026}, {"release_year": 2026}, {"release_year": 2025},
        ]
        assert _resolve_album_edition_year(tracks) == "2026"

    def test_no_edition_year_is_empty_not_guessed(self):
        from services.metadata.album_tag_sync_service import _resolve_album_edition_year

        assert _resolve_album_edition_year([{"year": "1995"}]) == ""

    def test_original_year_resolver_reads_the_year_column(self):
        from services.metadata.album_tag_sync_service import _resolve_album_year

        assert _resolve_album_year([{"year": "1995"}, {"year": "1995"}, {"year": "2026"}]) == "1995"


class _FakeTags:
    """Records what the sync wrote to each file."""

    def __init__(self, existing: dict[str, dict[str, str]] | None = None):
        self.existing = existing or {}
        self.written: dict[str, dict[str, str]] = {}

    def read(self, file_path: str) -> dict[str, str]:
        return dict(self.existing.get(file_path) or {})

    def write(self, file_path: str, tags: dict) -> bool:
        self.written.setdefault(file_path, {}).update(
            {k: v for k, v in tags.items() if v is not None and str(v).strip() != ""}
        )
        return True


class TestSyncAlbumFileTagsYearCorrection:
    """A remaster that had the ORIGINAL year stamped into DATE is corrected."""

    def _run(self, monkeypatch, tmp_path, file_date: str, track_extra: dict | None = None):
        from services.metadata import album_tag_sync_service as svc

        mp3 = tmp_path / "1.mp3"
        mp3.write_bytes(b"x")

        track = {
            "id": "t1", "title": "Song", "artist": "A", "album": "Album",
            "album_artist": "A", "year": "1995", "release_year": 2026,
            "track_number": "1", "disc_number": "1", "file_path": str(mp3),
        }
        track.update(track_extra or {})

        fake = _FakeTags({str(mp3): {"year": file_date}})
        monkeypatch.setattr(svc, "_load_fresh_tracks", lambda artist, album: [track])
        monkeypatch.setattr(svc, "_read_file_values", fake.read)
        monkeypatch.setattr(svc, "_fetch_artist_genres", lambda artist: {})
        monkeypatch.setattr(svc, "_resolve_mb_release", lambda tracks: ("", {}, 0))
        monkeypatch.setattr(svc, "_record_corrections", lambda *a, **k: 0)
        monkeypatch.setattr(svc, "os", svc.os)
        monkeypatch.setattr(svc.os.path, "exists", lambda p: True)

        import services.metadata.tag_file_service as tfs
        monkeypatch.setattr(tfs, "write_tags_to_file", fake.write)

        svc.sync_album_file_tags("A", "Album")
        return fake.written.get(str(mp3), {})

    def test_the_inverted_date_is_corrected(self, monkeypatch, tmp_path):
        written = self._run(monkeypatch, tmp_path, file_date="1995")
        assert written.get("year") == "2026"
        assert written.get("originalyear") == "1995"

    def test_a_user_edited_date_is_left_alone(self, monkeypatch, tmp_path):
        # 1999 matches neither the original nor the edition -> not ours to fix.
        written = self._run(monkeypatch, tmp_path, file_date="1999")
        assert "year" not in written

    def test_an_already_correct_date_is_not_rewritten(self, monkeypatch, tmp_path):
        written = self._run(monkeypatch, tmp_path, file_date="2026")
        assert "year" not in written


class TestTagMapperYearSplit:
    def test_build_tag_updates_prefers_the_edition_year_for_date(self):
        from services.metadata.tag_file_service import build_tag_updates

        tags = build_tag_updates({"year": "1995", "release_year": "2026", "title": "Song"})
        assert tags["year"] == "2026"
        assert tags["originalyear"] == "1995"

    def test_an_explicit_originalyear_wins(self):
        from services.metadata.tag_file_service import build_tag_updates

        tags = build_tag_updates({
            "year": "1995", "release_year": "2026", "originalyear": "1994",
        })
        assert tags["year"] == "2026"
        assert tags["originalyear"] == "1994"

    def test_no_edition_year_leaves_the_original_in_date(self):
        from services.metadata.tag_file_service import build_tag_updates

        tags = build_tag_updates({"year": "1995"})
        assert tags["year"] == "1995"

    def test_the_download_writer_uses_the_edition_year_too(self, monkeypatch):
        import services.metadata.tag_file_service as tfs

        captured: dict = {}

        def _fake_write(path, tags):
            captured.update(tags)
            return True

        monkeypatch.setattr(tfs, "write_tags_to_file", _fake_write)
        tfs.update_file_metadata(
            "/music/x.mp3",
            {"title": "Song", "year": "1995", "release_year": "2026"},
        )
        assert captured["year"] == "2026"
        assert captured["originalyear"] == "1995"


class TestWritersKnowBothYearTags:
    def test_mp3_writer_routes_year_to_tdrc_and_the_original_to_txxx(self):
        from services.metadata import tag_file_service as tfs

        assert tfs._MP3_FRAME_FOR_FIELD.get("year") == "TDRC"
        # ``originalyear``/``originaldate`` reach the file through the canonical
        # registry, so pin that they are known names rather than invented here.
        from services.metadata.tag_names import CANONICAL_TAG_NAMES

        assert CANONICAL_TAG_NAMES.get("originalyear")
        assert CANONICAL_TAG_NAMES.get("originaldate")

    def test_flac_writer_routes_year_to_date(self):
        from services.metadata import tag_file_service as tfs

        assert tfs._VORBIS_FIELD_MAP.get("year") == "date"

    def test_the_album_reader_can_see_the_original_year_pair(self):
        """Without this the sync would rewrite them on every scan."""
        import inspect

        from services.metadata import album_tag_sync_service as svc

        source = inspect.getsource(svc._read_file_values)
        assert '"originalyear"' in source
        assert '"originaldate"' in source


class TestAlbumSortUsesTheOriginalYear:
    """``year`` must stay the ORIGINAL year: album sorting reads it."""

    def test_the_album_listing_prefers_year_over_release_year(self):
        import inspect

        from routes import ui_routes

        source = inspect.getsource(ui_routes)
        # The album year key is built from ``year`` first, with ``release_year``
        # only as a fallback — the opposite order would make a 2026 remaster
        # sort as a 2026 album instead of by its original year. Checked by
        # POSITION inside the query rather than by matching the SQL's exact
        # formatting, which would break on any reformat.
        marker = source.find("AS album_year")
        assert marker != -1, "the album listing must derive an album_year"
        window = source[max(0, marker - 800):marker]
        year_at = window.find("COALESCE(year")
        release_at = window.find("release_year")
        assert year_at != -1, "the album year key must consider the year column"
        assert release_at != -1, "the album year key must fall back to release_year"
        assert year_at < release_at, (
            "year (the ORIGINAL year) must be preferred over release_year"
        )

    def test_the_scan_stores_the_edition_year_separately(self):
        from services.popularity.stages.track_stage import process_track  # noqa: F401
        import inspect

        from services.popularity import scan_stage_runner

        source = inspect.getsource(scan_stage_runner)
        assert "authoritative_year" in source
        assert "authoritative_release_year" in source
