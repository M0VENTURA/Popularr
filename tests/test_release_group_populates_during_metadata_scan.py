"""The release group must be resolved even when the album type is already known.

REPORTED

> The release group ID doesn't seem to be populating during the metadata scan.

TWO INDEPENDENT EXITS, BOTH BEFORE THE PERSISTENCE CALL
-------------------------------------------------------

``_persist_album_type_to_tracks`` is what writes ``musicbrainz_releasegroupid``,
and both of its callers sit behind a gate that could return first:

1. **`ensure_album_type` returned on a type cache hit.**  When every track
   already carried the SAME ``musicbrainz_albumtype`` — set by the popularity
   pass, by an earlier metadata run, or by the album page's Save — it logged
   ``ensure album type cache hit`` and returned **without ever calling
   ``_resolve_album_type``**.  The type and the release group are written by
   the SAME call, so the album's release group was never even looked for.  This
   is Step 3/3 of the album pipeline
   (``album_pipeline._maybe_auto_detect_album_type``), the metadata scan's own
   album-type step and its "safety net for albums that were skipped" — exactly
   where the reported scan runs.

2. **The release-group SEARCH could come back empty.**  The only source of a
   release group was a release-group *text* search with a 0.6 score gate.  An
   album already bound to a concrete MusicBrainz release
   (``musicbrainz_album_mbid`` — from the file's tag, from Navidrome's
   ``musicBrainzId``, or from a manual match) had that binding ignored, so a
   low text score or an empty result set left it with nothing.

THE FIX
-------

* The cache hit now only short-circuits when the row **also** carries a
  release group.  Otherwise it resolves, then keeps the ROW's type: the
  resolve exists for the id, not to re-decide the type
  (``_persist_album_type_to_tracks`` is fill-only for the type anyway).
* New ``_release_group_from_stored_release`` answers the release group from the
  album's OWN release MBID through ``fetch_musicbrainz_release_metadata`` (one
  HTTP-cached call, no text matching) and is used at the two "search produced
  nothing" exits.  It returns ``(None, rg)`` — the type stays the local
  detection's call to make.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.popularity.stages import album_stage  # noqa: E402


def album_row(*, album_type: str = "album", release_group: str = "",
              release_mbid: str = "") -> dict:
    tracks = [{
        "id": "t1",
        "title": "Track",
        "musicbrainz_albumtype": album_type,
        "musicbrainz_releasegroupid": release_group,
        "musicbrainz_album_mbid": release_mbid,
    }]
    return {
        "artist": "Powderfinger",
        "album": "Fingerprints",
        "album_artist": "Powderfinger",
        "spotify_album_type": None,
        "tracks": tracks,
    }


class _Recorder:
    def __init__(self, resolved_rg: str | None = "rg-from-scan"):
        self.resolved: list[tuple] = []
        self.persisted: list[tuple] = []
        self._rg = resolved_rg

    def persist(self, artist, album, tracks, album_type, release_group_mbid):
        self.persisted.append((artist, album, album_type, release_group_mbid))


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()

    def _resolve(*args, **kwargs):
        rec.resolved.append(args)
        return "album", None, rec._rg, None

    monkeypatch.setattr(album_stage, "_resolve_album_type", _resolve)
    monkeypatch.setattr(album_stage, "_persist_album_type_to_tracks", rec.persist)
    rec.persist_calls = rec.persisted
    return rec


# ===========================================================================
# 1. The cache hit must not skip the release group
# ===========================================================================


class TestTheTypeCacheHitStillResolvesTheReleaseGroup:
    def test_a_cached_type_with_no_release_group_still_resolves(self, recorder):
        """THE REPORT: the type was known, so nothing ever looked for the id."""
        detected = album_stage.ensure_album_type(album_row(album_type="album"))

        assert recorder.resolved, (
            "ensure album_type returned on the type cache hit, so "
            "_resolve_album_type — the only thing that can produce a release "
            "group — never ran"
        )
        assert detected == "album"

    def test_the_release_group_is_actually_persisted(self, recorder):
        album_stage.ensure_album_type(album_row(album_type="album"))

        assert recorder.persisted, "_persist_album_type_to_tracks was never reached"
        artist, album, album_type, release_group_mbid = recorder.persisted[0]
        assert release_group_mbid == "rg-from-scan", (
            "the resolver produced a release group that was never persisted"
        )
        assert album_type == "album"

    def test_the_rows_own_type_survives_the_resolve(self, recorder):
        """The resolve existed for the id — it must not re-detect the type."""
        detected = album_stage.ensure_album_type(album_row(album_type="ep"))

        assert detected == "ep", "a cache hit turned into a re-detection"
        assert recorder.persisted[0][2] == "ep"

    def test_a_row_that_already_has_both_is_left_alone(self, recorder):
        """CONTROL — no wasted search when there is nothing left to find."""
        album_stage.ensure_album_type(
            album_row(album_type="album", release_group="rg-stored")
        )

        assert recorder.resolved == [], (
            "both values are already on the row — the search must not run"
        )
        assert recorder.persisted == []

    def test_a_row_with_no_type_at_all_still_resolves(self, recorder):
        """CONTROL — the original behaviour of the un-typed path."""
        album_stage.ensure_album_type(album_row(album_type=""))

        assert recorder.resolved
        assert recorder.persisted

    def test_force_still_bypasses_the_cache(self, recorder):
        """CONTROL — ``force`` means re-detect, cache or no cache."""
        album_stage.ensure_album_type(
            album_row(album_type="album", release_group="rg-stored"),
            {"force": True},
        )

        assert recorder.resolved, "force must reach the resolver"


# ===========================================================================
# 2. The album's own release MBID answers the release group
# ===========================================================================


class TestTheStoredReleaseMBIDAnswersTheReleaseGroup:
    def test_it_resolves_the_release_group_of_the_bound_release(self, monkeypatch):
        calls: list[str] = []

        def _fetch(mbid):
            calls.append(mbid)
            return {"release_group_mbid": "rg-from-release"}

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
            _fetch,
        )

        result, rg = album_stage._release_group_from_stored_release(
            [{"musicbrainz_album_mbid": "f3691bf0-5b59-4ecb-b910-8d4813952ce5"}],
            {"artist": "A", "album": "Al"},
        )

        assert rg == "rg-from-release"
        assert calls == ["f3691bf0-5b59-4ecb-b910-8d4813952ce5"]
        assert result is None, (
            "only the binding is known — the local detection must still "
            "decide the type"
        )

    def test_a_row_with_no_release_mbid_asks_nothing(self, monkeypatch):
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
            lambda *a, **k: pytest.fail("nothing to ask without a release MBID"),
        )

        assert album_stage._release_group_from_stored_release(
            [{"musicbrainz_album_mbid": ""}], {}
        ) == (None, None)

    def test_an_unreachable_service_degrades_to_none(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("MusicBrainz overloaded")

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.fetch_musicbrainz_release_metadata",
            _boom,
        )

        assert album_stage._release_group_from_stored_release(
            [{"musicbrainz_album_mbid": "f3691bf0-5b59-4ecb-b910-8d4813952ce5"}], {}
        ) == (None, None)

    def test_both_search_rejections_fall_back_to_it(self):
        """The two 'search produced nothing' exits must not discard the binding."""
        import inspect

        source = inspect.getsource(album_stage._lookup_musicbrainz_album_type)
        for anchor in (
            'MusicBrainz album type had no matches',
            'MusicBrainz album type match rejected',
        ):
            at = source.index(anchor)
            window = source[at: at + 400]
            assert "return _release_group_from_stored_release(tracks, context)" in window, (
                f"the {anchor!r} exit still returns (None, None), so a bound "
                "album whose text search failed comes back with no release group"
            )
            assert "return None, None" not in window.split(
                "return _release_group_from_stored_release"
            )[0], (
                f"the {anchor!r} exit discards the album's own binding"
            )


# ===========================================================================
# 3. What deliberately did not change
# ===========================================================================


class TestTheGatesAreStillIntact:
    def test_the_vaguard_still_rejects_unbound_compilations(self):
        """CONTROL — the fallback must not weaken the VA tracklist gate."""
        import inspect

        source = inspect.getsource(album_stage._lookup_musicbrainz_album_type)
        assert source.index("_tracklist_corroborates_release_group") < source.index(
            "release_group_mbid = str(best.get"
        ), "the VA gate still sits before the id is taken"

    def test_the_type_is_never_fabricated_from_the_binding(self):
        """CONTROL — a release id says what the album IS, not what it is called."""
        assert album_stage._release_group_from_stored_release.__doc__ is not None
        import inspect

        body = inspect.getsource(album_stage._release_group_from_stored_release)
        assert "return None, release_group_mbid" in body
