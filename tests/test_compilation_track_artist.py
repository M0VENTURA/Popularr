"""A compilation's tracks must end up with the SONG's artist, not "Various Artists".

Reported: "The Track artist is still not updating for a compilation during a
scan" — with an Edit Track dialog showing ``Cave`` still credited to Various
Artists, and "some of the songs are still being detected as covers".

Two causes, both visible in the scan log of ``Various Artists - Little Nicky``:

1. The per-track MusicBrainz recording search was constrained to the row's artist,
   so every compilation track was searched as ``artist:"Various Artists"`` and came
   back empty:

       [MB] recording suggestion completed mbid=None score=0.0 candidate_count=0
            artist='Various Artists' track='Cave'
       [MB] recording suggestion completed mbid=None score=0.0 candidate_count=0
            artist='Various Artists' track='Take a Picture'

   Titles with no match cannot correct anything, so the placeholder stayed.

2. Even when a match WAS made (the album-release batch resolved 7 of the 12
   ``recording.bulk_get returned=7``), the artist was only replaced when
   ``_force_meta`` was set — which a track that already had an MBID and genres did
   not have. Those 7 logged ``Artist corrected from MusicBrainz``; the rest kept
   ``Various Artists``.

The knock-on effect is the false covers: cover detection compares the ISRC's
original artist ("Muse", "Filter", "Powerman 5000") with the PERFORMER, and no
real performer can ever match an album-level placeholder.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLACEHOLDERS = ["Various Artists", "various artists", "VARIOUS ARTISTS", "Various",
                "VA", "v/a", "Soundtrack", " Unknown Artist "]
REAL_ARTISTS = ["Muse", "Disturbed", "P.O.D.", "Linkin Park", "Deftones"]


class TestThePlaceholderTest:
    @pytest.mark.parametrize("value", PLACEHOLDERS)
    def test_album_level_placeholders_are_recognised(self, value):
        from helpers.normalization_service import is_track_artist_placeholder

        assert is_track_artist_placeholder(value) is True

    @pytest.mark.parametrize("value", REAL_ARTISTS)
    def test_real_performers_are_not(self, value):
        from helpers.normalization_service import is_track_artist_placeholder

        assert is_track_artist_placeholder(value) is False

    def test_empty_is_not_a_placeholder(self):
        from helpers.normalization_service import is_track_artist_placeholder

        assert is_track_artist_placeholder("") is False
        assert is_track_artist_placeholder(None) is False


class TestTheRecordingSearchKeepsTheArtistConstraint:
    """A title-only search is NOT the fix — it binds the wrong artist.

    Measured on the reported album once the constraint was dropped:
    ``Cave`` -> Kroke, ``Natural High`` -> Jonathan Maron, ``Nothing`` ->
    Paradise Now!, ``When Worlds Collide`` -> Distorted. All are covers/tribute
    recordings of the right title by the wrong artist, so the placeholder must
    keep producing NO match and the identity must come from the album's OWN
    release tracklist instead.
    """

    def test_a_placeholder_still_constrains_the_search(self):
        import inspect

        from services.enrichment import musicbrainz_service as mbs

        source = inspect.getsource(mbs.MusicBrainzService.get_suggested_mbid)
        assert "is_track_artist_placeholder" not in source, (
            "dropping the artist for a placeholder binds unrelated recordings of "
            "the same title"
        )
        assert "_artist_clause" not in source
        # The artist clause is unconditional again.
        assert 'f\'AND artist:"{Escape_lucene_special_chars(artist)}"\'' in source

    def test_the_reason_is_recorded_in_the_code(self):
        import inspect

        from services.enrichment import musicbrainz_service as mbs

        source = inspect.getsource(mbs.MusicBrainzService.get_suggested_mbid)
        assert "Kroke" in source and "Powerman 5000" in source, (
            "the concrete wrong bindings must stay documented so nobody 'fixes' "
            "this by dropping the constraint again"
        )


class TestThePlaceholderArtistIsReplacedFromMusicBrainz:
    def _resolve(self, existing_artist: str, monkeypatch, mb_artist: str = "P.O.D."):
        from services.popularity.stages import track_stage

        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(
            track_stage, "get_shared_mb_service",
            lambda: pytest.fail("a batch hit must not search or fetch"),
        )
        return track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={
                "title": "School of Hard Knocks",
                "artist": existing_artist,
                "album": "Little Nicky",
                "album_artist": "Various Artists",
                "recording_mbid": "various-studio-rec",
                "musicbrainz_genres": '["nu metal"]',
            },
            track_title="School of Hard Knocks",
            track_artist=existing_artist,
            frozen_track=False,
            force_meta=False,
            options={
                "mb_batch_metadata": {
                    "various artists::school of hard knocks": {
                        "recording_mbid": "pod-rec",
                        "title": "School of Hard Knocks",
                        "artist": mb_artist,
                    }
                }
            },
        )

    def test_the_placeholder_is_replaced_without_a_forced_pass(self, monkeypatch):
        result = self._resolve("Various Artists", monkeypatch)
        assert result["payload"].get("artist") == "P.O.D.", (
            "the album-level placeholder must be replaced by the recording's own "
            "artist credit — this is the reported 'still not updating'"
        )

    @pytest.mark.parametrize("existing", REAL_ARTISTS)
    def test_a_real_track_artist_is_not_clobbered(self, existing, monkeypatch):
        result = self._resolve(existing, monkeypatch, mb_artist="Someone Else")
        assert "artist" not in result["payload"], (
            "a real track artist must only change on a forced metadata pass"
        )
