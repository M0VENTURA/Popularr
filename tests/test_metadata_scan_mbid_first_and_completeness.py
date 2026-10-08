"""MBID-first album-type lookup, and a completeness check that cannot loop.

Three changes made together because they share one rule — *ask only for what is
actually missing, and only require what a re-run can actually produce*.

REPORTED
--------
> Does the metadata scan check whether a track/album already has an MBID and
> use that to confirm the release is correct? If not, would it speed the scan up?
>
> Does "metadata scan is recent" check that all the metadata fields are filled
> in and run for any missing the main fields — genres from each source, MBIDs,
> Discogs id, etc.?
>
> When a track has no genres from Discogs/Last.fm/MusicBrainz, are the album
> genres added to the track? Then would it only check again on a forced scan?

WHAT CHANGED
------------
1. ``_lookup_musicbrainz_album_type`` now reads the album's OWN MusicBrainz
   identity before spending a request:
   * **already bound** — a rich stored type *and* a stored release-group MBID
     → nothing is looked up at all;
   * a stored release-group MBID **confirms** a below-threshold text match, so
     the ``score < 0.6`` gate can no longer leave a bound album typeless.
2. ``is_album_incomplete`` gained the two external ids the report named — each
   gated on evidence that it *can* be filled, because requiring an unfillable
   field re-runs the album forever.
3. The genre rule changed from an **OR** to an **AND**: a track whose genres
   came from an album blend (or a manual edit, or Navidrome) used to be
   permanently incomplete, because its per-source columns could never reproduce
   them — that is the endless re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _complete_track(**overrides) -> dict:
    """A track that satisfies every rule in ``is_album_incomplete``."""
    track = {
        "id": "t1",
        "title": "Track",
        "genres": "Rock",
        "lastfm_tags": "rock",
        "musicbrainz_genres": None,
        "discogs_genres": None,
        "listenbrainz_genres": None,
        "spotify_genres": None,
        "essentia_genres": None,
        "manual_genres": None,
        "navidrome_genres": None,
        "final_score": 55.0,
        "musicbrainz_albumtype": "album",
        "recording_mbid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "lastfm_last_updated": "2026-01-01T00:00:00",
        "listenbrainz_last_updated": None,
    }
    track.update(overrides)
    return track


def _incomplete(track: dict) -> tuple[bool, str]:
    from services.popularity.scan_stage_runner import is_album_incomplete

    return is_album_incomplete([track])


class _FakeMbService:
    """Stand-in for ``get_shared_mb_service`` — records every search."""

    def __init__(self, matches):
        self.matches = matches
        self.calls: list[tuple] = []

    def search_releasegroup_matches(self, artist, album, limit=3, log_context=None):
        self.calls.append((artist, album))
        return self.matches


def _lookup(monkeypatch, matches, tracks):
    from services.popularity.stages import album_stage

    service = _FakeMbService(matches)
    monkeypatch.setattr(album_stage, "get_shared_mb_service", lambda: service)
    result = album_stage._lookup_musicbrainz_album_type(
        "Artist", "Album", "Artist", tracks
    )
    return result, service


# ---------------------------------------------------------------------------
# 1. MBID-first
# ---------------------------------------------------------------------------


class TestAnAlreadyBoundAlbumIsNeverLookedUp:
    def test_a_rich_stored_type_and_mbid_skip_the_search(self, monkeypatch):
        """THE SPEEDUP: zero MusicBrainz requests for an album we already know."""
        tracks = [
            {
                "musicbrainz_albumtype": "ep+soundtrack",
                "musicbrainz_releasegroupid": "rg-123",
            }
        ]
        (rg, mbid), service = _lookup(monkeypatch, [], tracks)

        assert rg == "ep+soundtrack", f"stored type not used, got {rg!r}"
        assert mbid == "rg-123"
        assert service.calls == [], (
            "a throttled release-group SEARCH was spent on an album whose type "
            "and binding are already on the row"
        )

    @pytest.mark.parametrize("stored", ["ep", "single", "album+live", "album+remix", "album+soundtrack"])
    def test_every_rich_type_skips_the_search(self, monkeypatch, stored):
        tracks = [{"musicbrainz_albumtype": stored, "musicbrainz_releasegroupid": "rg-1"}]
        (rg, _mbid), service = _lookup(monkeypatch, [], tracks)
        assert rg == stored
        assert service.calls == []

    def test_a_plain_album_still_asks(self, monkeypatch):
        """CONTROL — a bare 'album' carries nothing a guess cannot refine.

        It is the one value MusicBrainz can still add (a live/soundtrack/EP
        hiding behind it), so it must not take the fast path.
        """
        tracks = [{"musicbrainz_albumtype": "album", "musicbrainz_releasegroupid": "rg-1"}]
        (_rg, _mbid), service = _lookup(monkeypatch, [], tracks)
        assert service.calls, "a plain album must still consult MusicBrainz"

    def test_no_mbid_means_the_search_still_runs(self, monkeypatch):
        """CONTROL — the binding is half of the fast path's precondition."""
        tracks = [{"musicbrainz_albumtype": "ep+live"}]
        (_rg, _mbid), service = _lookup(monkeypatch, [], tracks)
        assert service.calls, "a stored type without a binding must still search"

    def test_a_mixed_track_set_is_not_voted_on(self, monkeypatch):
        """CONTROL — majority-voting would pick a winner nobody chose."""
        tracks = [
            {"musicbrainz_albumtype": "ep+live", "musicbrainz_releasegroupid": "rg-1"},
            {"musicbrainz_albumtype": "album+live", "musicbrainz_releasegroupid": "rg-1"},
        ]
        (_rg, _mbid), service = _lookup(monkeypatch, [], tracks)
        assert service.calls, "disagreeing rows must fall back to the lookup"


class TestAStoredMbidConfirmsABelowThresholdMatch:
    MATCH = {"id": "rg-real", "primary_type": "Album", "secondary_types": ["soundtrack"], "match_score": 0.41}

    def test_a_stored_mbid_rescues_the_rejection(self, monkeypatch):
        """THE REPORTED 'score < 0.6 leaves albums typeless'."""
        tracks = [{"musicbrainz_albumtype": "", "musicbrainz_releasegroupid": "rg-real"}]
        (rg, mbid), service = _lookup(monkeypatch, [dict(self.MATCH)], tracks)

        assert service.calls, "the search still runs — only the GATE is bypassed"
        assert mbid == "rg-real"
        assert rg == "album+soundtrack", (
            f"the album was left typeless despite its own binding, got {rg!r}"
        )

    def test_a_below_threshold_match_without_a_mbid_is_still_rejected(self, monkeypatch):
        """CONTROL — the gate must still reject an unconfirmable proposal."""
        tracks = [{"musicbrainz_albumtype": "", "musicbrainz_releasegroupid": ""}]
        (rg, mbid), _svc = _lookup(monkeypatch, [dict(self.MATCH)], tracks)
        assert rg is None and mbid is None

    def test_a_confident_match_is_left_alone(self, monkeypatch):
        """CONTROL — the stored MBID must not override a confident proposal."""
        confident = {"id": "rg-other", "primary_type": "Album", "secondary_types": [], "match_score": 0.93}
        tracks = [{"musicbrainz_albumtype": "", "musicbrainz_releasegroupid": "rg-real"}]
        (rg, mbid), _svc = _lookup(monkeypatch, [dict(confident)], tracks)
        assert mbid == "rg-other", "the accepted search result must win"
        assert rg is not None

    def test_a_mbid_not_among_the_candidates_does_not_force_it(self, monkeypatch):
        """CONTROL — confirmation needs the id to actually appear."""
        tracks = [{"musicbrainz_albumtype": "", "musicbrainz_releasegroupid": "rg-absent"}]
        (rg, _mbid), _svc = _lookup(monkeypatch, [dict(self.MATCH)], tracks)
        assert rg is None, "a binding the search never proposed must not be invented"


# ---------------------------------------------------------------------------
# 2 + 3. Completeness: require only what a re-run can produce
# ---------------------------------------------------------------------------


class TestTheGenreRuleNoLongerLoops:
    def test_a_genres_from_an_album_blend_is_complete(self):
        """THE LOOP: genres arrived from outside the per-source aggregation.

        The old rule was ``not genres OR not any_source`` — with sources empty
        it flagged this forever and every metadata scan re-ran it.
        """
        incomplete, reason = _incomplete(_complete_track(genres="Rock", lastfm_tags=None,
            musicbrainz_genres=None, discogs_genres=None, listenbrainz_genres=None,
            spotify_genres=None, essentia_genres=None, manual_genres=None,
            navidrome_genres=None, lastfm_last_updated=None,
            listenbrainz_last_updated=None))
        assert incomplete is False, f"a blended-genre track was still flagged: {reason}"

    def test_a_track_with_no_genres_and_no_sources_still_counts(self, monkeypatch):
        """CONTROL — the genuinely-missing case must still re-run."""
        incomplete, reason = _incomplete(_complete_track(
            genres="", lastfm_tags=None, musicbrainz_genres=None, discogs_genres=None,
            listenbrainz_genres=None, spotify_genres=None, essentia_genres=None,
            manual_genres=None, navidrome_genres=None, lastfm_last_updated=None,
            listenbrainz_last_updated=None))
        assert incomplete is True
        assert "genres" in reason

    def test_sources_that_were_consulted_and_found_nothing_are_enough(self):
        """CONTROL — checking them again produces the same nothing."""
        incomplete, reason = _incomplete(_complete_track(
            genres="", lastfm_tags=None, musicbrainz_genres=None, discogs_genres=None,
            listenbrainz_genres=None, spotify_genres=None, essentia_genres=None,
            manual_genres=None, navidrome_genres=None))
        assert incomplete is False, (
            "sources were consulted (timestamps present) but returned nothing "
            f"— still flagged: {reason}"
        )

    def test_a_track_with_any_source_genre_is_complete(self):
        incomplete, reason = _incomplete(_complete_track(genres="", lastfm_tags=None))
        assert incomplete is False, f"a source column was filled but flagged: {reason}"


class TestTheExternalIdsAreGatedOnFillability:
    def test_a_release_without_its_release_group_is_incomplete(self):
        incomplete, reason = _incomplete(_complete_track(
            musicbrainz_album_mbid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            musicbrainz_releasegroupid="",
        ))
        assert incomplete is True
        assert "release-group" in reason

    def test_a_stored_release_group_is_complete(self):
        incomplete, _r = _incomplete(_complete_track(
            musicbrainz_album_mbid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            musicbrainz_releasegroupid="rg-1",
        ))
        assert incomplete is False

    def test_no_release_mbid_means_the_rule_does_not_apply(self):
        """We cannot resolve a release-group we are not holding."""
        incomplete, _r = _incomplete(_complete_track(
            musicbrainz_album_mbid="", musicbrainz_releasegroupid="",
        ))
        assert incomplete is False

    def test_a_row_without_the_columns_is_not_penalised(self):
        """A caller whose SELECT omits them must not loop forever."""
        track = _complete_track()
        for column in ("musicbrainz_releasegroupid", "musicbrainz_album_mbid",
                       "discogs_album_id", "discogs_genres"):
            track.pop(column, None)
        incomplete, _r = _incomplete(track)
        assert incomplete is False, (
            "a column the row never carried is not a field the row is missing"
        )

    def test_an_album_discogs_answers_for_but_has_no_id_is_incomplete(self):
        incomplete, reason = _incomplete(_complete_track(
            discogs_genres="Soundtrack", discogs_album_id="",
        ))
        assert incomplete is True
        assert "Discogs" in reason

    def test_an_album_discogs_never_answered_for_is_not_incomplete(self):
        """CONTROL — the endless-rerun guard.

        An album that simply is not on Discogs has no genres from it either;
        requiring the id anyway would re-run it on every scan forever.
        """
        incomplete, _r = _incomplete(_complete_track(discogs_album_id=""))
        assert incomplete is False

    def test_the_pre_existing_rules_still_fire(self):
        """CONTROL — the four original checks are untouched."""
        assert _incomplete(_complete_track(final_score=0))[0] is True
        assert _incomplete(_complete_track(musicbrainz_albumtype=""))[0] is True
        assert _incomplete(_complete_track(recording_mbid=""))[0] is True
        assert _incomplete(_complete_track())[0] is False
