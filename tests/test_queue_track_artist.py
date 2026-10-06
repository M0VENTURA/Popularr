"""A queue row must be searched by the TRACK's artist, never the album's.

Reported
--------
> When adding to the queue, it's adding the album artist, not the track artist
> so it's searching for Various Artists rather than the actual artist of the
> track.

Where it actually broke
-----------------------
``album_missing_service.persist_missing_from_comparison`` already knew the right
answer::

    "track_artist": entry.get("mb_artist") or artist,

…but ``_match_mb_tracks_to_library`` NEVER SET ``mb_artist`` on a comparison
entry, so ``entry.get("mb_artist")`` was always ``None`` and the fallback —
the album's own artist — was stored for every track. On a compilation that is
"Various Artists", so the row that reached Soulseek asked for an artist no
shared file has.

The client side then repeated the mistake independently: the missing-track
button and the "Redownload correct version" button both hardcoded
``pageArtist()`` (the album's artist) into the queue payload, so even a row
whose ``track_artist`` was right was queued wrong.

Both halves are fixed here, plus the knock-on in
``_build_fallback_search_queries``: a placeholder album artist no longer seeds
fallback queries, because a title-only/placeholder query that WINS binds a
wrong-artist hit — the failure already documented in
``tests/test_compilation_track_artist.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

LIVE_ALBUM = REPO_ROOT / "static" / "js" / "album_detail.js"
REBUILT_ALBUM = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
LIVE_REVIEW = REPO_ROOT / "static" / "js" / "metadata-review.js"
REBUILT_REVIEW = REPO_ROOT / "test_site" / "static" / "js" / "services" / "metadata-review.js"

# A compilation track: the recording has its own credit, the ALBUM does not
# belong to it.  271000 ms on the MusicBrainz side, 271 s on the library side
# — the same length, in the two units each source uses.
_MB_TRACK = {
    "mb_track_number": 1,
    "mb_disc_number": 1,
    "mb_title": "Forgotten Years",
    "mb_recording_mbid": "0b1f8f9e-6f3a-4d2c-9a11-5c7d2e9f4a6b",
    "mb_duration": 271000,
    "artist": "Skulker",
}
_LIB_ROW = {
    "id": "42",
    "title": "Forgotten Years",
    "track_number": 1,
    "disc_number": 1,
    "duration": 271,
    "mbid": "0b1f8f9e-6f3a-4d2c-9a11-5c7d2e9f4a6b",
    "artist": "Skulker",
}


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestTheComparisonCarriesTheTrackArtist:
    """The key ``album_missing_service`` has always read but never received."""

    def test_a_matched_entry_has_both_credits(self):
        from services.enrichment.musicbrainz_service import _match_mb_tracks_to_library

        comparison, _extra = _match_mb_tracks_to_library([dict(_MB_TRACK)], [dict(_LIB_ROW)])
        entry = comparison[0]

        assert entry["matched"] is True
        assert entry["mb_artist"] == "Skulker"
        # The library row's own credit — what a redownload has to search for.
        assert entry["library_artist"] == "Skulker"

    def test_an_unmatched_entry_keeps_the_recording_credit(self):
        from services.enrichment.musicbrainz_service import _match_mb_tracks_to_library

        comparison, extra = _match_mb_tracks_to_library([dict(_MB_TRACK)], [])
        entry = comparison[0]

        assert entry["matched"] is False
        assert entry["mb_artist"] == "Skulker"
        assert entry["library_artist"] == ""

    def test_a_recording_with_no_credit_is_blank_not_fabricated(self):
        from services.enrichment.musicbrainz_service import _match_mb_tracks_to_library

        mb_track = dict(_MB_TRACK)
        mb_track.pop("artist")
        comparison, _extra = _match_mb_tracks_to_library([mb_track], [dict(_LIB_ROW)])

        assert comparison[0]["mb_artist"] == ""


class TestThePersistedMissingTrackGetsThePerformer:
    """The user-visible half: what is stored, then what the page sends."""

    def test_a_compilation_row_stores_the_recording_credit(self, monkeypatch):
        from services.enrichment.musicbrainz_service import _match_mb_tracks_to_library
        from services.metadata import album_missing_service as ams

        comparison, _extra = _match_mb_tracks_to_library([dict(_MB_TRACK)], [])
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            ams,
            "_persist_missing_tracks",
            lambda artist, album, missing: captured.update(
                {"artist": artist, "album": album, "rows": list(missing)}
            ),
        )

        ams.persist_missing_from_comparison(
            "Various Artists", "Battlefield Vietnam", {"comparison": comparison}
        )

        rows = captured["rows"]  # type: ignore[index]
        assert rows, "nothing was persisted"
        # NOT the album artist — that is the reported bug.
        assert rows[0]["track_artist"] == "Skulker"

    def test_a_recording_with_no_credit_falls_back_to_the_album(self, monkeypatch):
        """The fallback must keep working: blank is worse than the album name."""
        from services.enrichment.musicbrainz_service import _match_mb_tracks_to_library
        from services.metadata import album_missing_service as ams

        mb_track = dict(_MB_TRACK)
        mb_track.pop("artist")
        comparison, _extra = _match_mb_tracks_to_library([mb_track], [])
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            ams,
            "_persist_missing_tracks",
            lambda artist, album, missing: captured.update({"rows": list(missing)}),
        )

        ams.persist_missing_from_comparison(
            "Various Artists", "Battlefield Vietnam", {"comparison": comparison}
        )

        rows = captured["rows"]  # type: ignore[index]
        assert rows[0]["track_artist"] == "Various Artists"


class TestTheDurationCheckReportsWhoToSearchFor:
    def _entry(self, **overrides) -> dict:
        entry = {
            "matched": True,
            "library_track_id": "42",
            "library_title": "Forgotten Years",
            "library_track_number": "1",
            "library_duration": "4:31",
            "mb_duration_display": "4:30",
            "mb_disc_number": 1,
            "mb_recording_mbid": "0b1f8f9e-6f3a-4d2c-9a11-5c7d2e9f4a6b",
            "mb_duration": 270000,
            "diff_fields": ["duration"],
            "library_artist": "",
            "mb_artist": "",
        }
        entry.update(overrides)
        return entry

    def test_the_library_artist_is_reported(self):
        from services.metadata.metadata_proposal_service import _duration_checks

        checks = _duration_checks([self._entry(library_artist="Skulker")])

        assert len(checks) == 1
        assert checks[0]["artist"] == "Skulker"

    def test_the_recording_credit_is_the_fallback(self):
        from services.metadata.metadata_proposal_service import _duration_checks

        checks = _duration_checks([self._entry(mb_artist="Skulker")])

        assert checks[0]["artist"] == "Skulker"

    def test_it_is_blank_never_invented_from_the_album(self):
        """No credit on either side → an empty artist, not "Various Artists".

        The duration payload has no album-artist field at all, so a blank here
        can only degrade to the client's fallback; it can never re-introduce
        the placeholder on its own.
        """
        from services.metadata.metadata_proposal_service import _duration_checks

        checks = _duration_checks([self._entry()])

        assert checks[0]["artist"] == ""


class TestTheQueueButtonsSendTheTrackArtist:
    """Client side: three buttons, both trees."""

    def test_the_live_missing_row_prefers_the_track_artist(self):
        src = _src(LIVE_ALBUM)
        assert (
            'data-artist="${escapeHtml(trackComp.track_artist || trackComp.mb_artist || pageArtist)}"'
            in src
        ), "the live missing-row button still queues the album artist"
        # The album artist keeps its own, correct, meaning.
        assert 'data-album-artist="${escapeHtml(pageArtist)}"' in src

    def test_the_rebuilt_missing_row_payload_prefers_the_track_artist(self):
        src = _src(REBUILT_ALBUM)
        assert (
            "artist: trackComp.track_artist || trackComp.mb_artist || pageArtist(),"
            in src
        ), "the rebuilt missing-row payload still queues the album artist"
        assert "album_artist: pageArtist()," in src

    def test_both_redownload_buttons_use_the_check_artist(self):
        live = _src(LIVE_REVIEW)
        assert "btn.dataset.artist = check.artist || pageArtist();" in live

        rebuilt = _src(REBUILT_REVIEW)
        assert "artist: check.artist || pageArtist()," in rebuilt


class TestAPlaceholderAlbumArtistNeverSeedsAFallback:
    """A wrong-artist hit is worse than no hit.

    ``tests/test_compilation_track_artist.py`` records what happens when the
    artist constraint is dropped: the title binds someone else's recording.
    """

    def test_various_artists_never_reaches_a_query(self):
        from services.downloads.download_pipeline_service import (
            _build_fallback_search_queries,
        )

        item = {
            "artist": "Skulker",
            "album_artist": "Various Artists",
            "title": "Forgotten Years",
            "album": "Battlefield Vietnam",
        }
        queries = _build_fallback_search_queries(item, "Skulker - Forgotten Years")

        assert queries, "the non-placeholder fallbacks must still exist"
        assert not any("various artist" in q.lower() for q in queries)

    def test_a_real_album_artist_still_earns_one(self):
        from services.downloads.download_pipeline_service import (
            _build_fallback_search_queries,
        )

        item = {
            "artist": "Muse",
            "album_artist": "Not The Band",
            "title": "Knights of Cydonia",
            "album": "BH&R",
        }
        queries = _build_fallback_search_queries(item, "Muse - Knights of Cydonia")

        assert any("not the band" in q.lower() for q in queries)
