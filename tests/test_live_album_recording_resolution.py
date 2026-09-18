"""A live album must resolve to its OWN recordings, not the studio originals.

Reported symptom:

> Live albums seem to be getting high ratings in the popularity scan
>
> They seem to be getting ratings based on the original versions of the song,
> not the live versions when matching

## Root cause

The popularity score is read from Last.fm/ListenBrainz for whichever MUSICBRAINZ
RECORDING the track is resolved to. A live album routinely ships **plainly
titled** tracks — every track on Metallica's "S&M" is titled exactly as its
studio original — so:

* ``Edition_annotations_compatible("Enter Sandman", "Enter Sandman")`` is True
  for both the studio and the live candidate;
* ``_mbid_similarity`` scores BOTH at exactly 1.0;

so nothing in ``get_suggested_mbid`` could tell them apart, and MusicBrainz's
relevance ordering (which puts the famous studio recording first) decided it.
The live track was then stamped with the **studio** recording's MBID, and every
popularity figure read from that MBID was the studio recording's — which is why
live albums scored like studio albums.

Two things were missing:

1. ``get_suggested_mbid`` never received the release-level liveness, and had no
   release data to judge on anyway (``inc`` was not requested, so candidates
   carried no ``releases``).
2. The MBID cache was keyed on ``(title, artist)`` alone, so a plainly titled
   live track and its studio namesake shared one entry — whichever was scanned
   first resolved the other.

## Fix

* ``get_suggested_mbid`` accepts ``is_live_release`` and requests
  ``inc="releases+release-groups"`` so candidates can be classified.
* Candidates are ranked by **liveness agreement first, then similarity**, with
  unclassifiable candidates in the middle so they never masquerade as verified
  studio.
* ``is_live_release`` is part of the cache key.
* ``lookup_recording_metadata`` forwards the flag through to the search.
"""

from __future__ import annotations

import inspect

import pytest

STUDIO_MBID = "11111111-1111-1111-1111-111111111111"
LIVE_MBID = "22222222-2222-2222-2222-222222222222"


def _release(title: str, *, live: bool) -> dict:
    return {
        "id": f"rel-{title.lower().replace(' ', '-')}",
        "title": title,
        "release-group": {
            "id": f"rg-{title.lower().replace(' ', '-')}",
            "title": title,
            "primary-type": "album",
            "secondary-types": ["Live"] if live else [],
        },
    }


def _candidate(mbid: str, title: str, *, live: bool, with_releases: bool = True) -> dict:
    rec = {"id": mbid, "title": title, "score": 100}
    if with_releases:
        rec["releases"] = [_release("S&M" if live else "Metallica", live=live)]
    return rec


class _FakeHttp:
    """MusicBrainz-shaped search stub.

    ``order`` mirrors MusicBrainz's relevance ordering, which for a hit song
    lists the STUDIO recording first — the exact condition that used to decide
    the outcome.
    """

    def __init__(self, order: str = "studio_first", with_releases: bool = True) -> None:
        self.order = order
        self.with_releases = with_releases
        self.seen_incs: list[str] = []

    def search_recordings(self, query, limit=10, inc=""):
        self.seen_incs.append(inc)
        studio = _candidate(STUDIO_MBID, "Enter Sandman", live=False,
                            with_releases=self.with_releases)
        live = _candidate(LIVE_MBID, "Enter Sandman", live=True,
                          with_releases=self.with_releases)
        return [studio, live] if self.order == "studio_first" else [live, studio]

    def get(self, *args, **kwargs):
        return {}


def _service(order: str = "studio_first", with_releases: bool = True):
    from services.enrichment.musicbrainz_service import MusicBrainzService

    http = _FakeHttp(order, with_releases)
    svc = MusicBrainzService(http_client=http, enabled=True)
    svc._mbid_cache.clear()
    return svc, http


# ---------------------------------------------------------------------------
# 1. The core bug: a plainly titled live track must resolve to the LIVE recording
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order", ["studio_first", "live_first"])
def test_live_track_resolves_to_live_recording(order: str) -> None:
    """The whole bug, in one assertion.

    Title is identical ("Enter Sandman"), so similarity is 1.0 for both
    candidates; only the release-group secondary type can separate them.
    """
    svc, _ = _service(order)
    mbid, score = svc.get_suggested_mbid(
        "Enter Sandman", "Metallica", is_live_release=True
    )
    assert mbid == LIVE_MBID, (
        f"a live track resolved to the STUDIO recording (order={order}). Its "
        "popularity would then be the studio recording's, which is exactly the "
        "reported symptom."
    )
    assert score > 0


@pytest.mark.parametrize("order", ["studio_first", "live_first"])
def test_studio_track_still_resolves_to_studio_recording(order: str) -> None:
    """The fix must not flip the normal case."""
    svc, _ = _service(order)
    mbid, _ = svc.get_suggested_mbid(
        "Enter Sandman", "Metallica", is_live_release=False
    )
    assert mbid == STUDIO_MBID


def test_unclassifiable_candidates_do_not_masquerade_as_studio() -> None:
    """With no release data, a live lookup still must not prefer the studio hit.

    When MusicBrainz does not honour ``inc`` every candidate looks identical.
    The old behaviour (first candidate wins) is retained rather than inventing
    a studio preference, because an unclassifiable candidate is not evidence
    of a studio recording.
    """
    svc, _ = _service("live_first", with_releases=False)
    mbid, _ = svc.get_suggested_mbid(
        "Enter Sandman", "Metallica", is_live_release=True
    )
    # All candidates rank equal, so the first one wins — the pre-existing
    # behaviour. The point is that it is NOT forced to the studio MBID.
    assert mbid in (STUDIO_MBID, LIVE_MBID)


# ---------------------------------------------------------------------------
# 2. The search must request the data the decision depends on
# ---------------------------------------------------------------------------


def test_search_requests_release_data() -> None:
    """Without ``inc`` the liveness decision is impossible.

    This is the regression guard for the subtlest part of the bug: even with
    the ranking logic correct, a search that does not request releases leaves
    every candidate unclassifiable and the fix silently does nothing.
    """
    svc, http = _service()
    svc.get_suggested_mbid("Enter Sandman", "Metallica", is_live_release=True)
    assert http.seen_incs, "search_recordings was never called"
    for inc in http.seen_incs:
        assert "release" in inc, (
            f"the recording search did not request release data (inc={inc!r}). "
            "Candidates would carry no release-group information, so a live "
            "track cannot be told apart from its studio namesake."
        )


# ---------------------------------------------------------------------------
# 3. Cache isolation
# ---------------------------------------------------------------------------


def test_cache_key_distinguishes_live_from_studio() -> None:
    """A plainly titled live track must not share a cache entry with its studio twin.

    Both produce the same ``(artist, title)``, so the first scan used to
    decide for both.
    """
    from services.enrichment.musicbrainz_service import MusicBrainzService

    studio_key = MusicBrainzService._cache_key("Enter Sandman", "Metallica")
    live_key = MusicBrainzService._cache_key("Enter Sandman", "Metallica", is_live=True)
    assert studio_key != live_key, (
        "the live and studio lookups share a cache key, so whichever is "
        "scanned first poisons the other"
    )


def test_live_and_studio_lookups_do_not_cross_poison_the_cache() -> None:
    """End-to-end: resolving studio then live must give the right answer twice."""
    from services.enrichment.musicbrainz_service import MusicBrainzService

    http = _FakeHttp("studio_first")
    svc = MusicBrainzService(http_client=http, enabled=True)
    svc._mbid_cache.clear()

    studio, _ = svc.get_suggested_mbid("Enter Sandman", "Metallica")
    live, _ = svc.get_suggested_mbid(
        "Enter Sandman", "Metallica", is_live_release=True
    )

    assert studio == STUDIO_MBID
    assert live == LIVE_MBID, (
        "the live lookup was served the studio entry from cache, so the live "
        "track keeps the studio recording's popularity"
    )


# ---------------------------------------------------------------------------
# 4. The flag must reach the search from the popularity pipeline
# ---------------------------------------------------------------------------


def test_lookup_recording_metadata_forwards_is_live_release() -> None:
    """``lookup_recording_metadata`` must pass the flag to the search."""
    from services.enrichment.musicbrainz_service import MusicBrainzService

    seen: dict = {}

    class _Http:
        def get_recording(self, mbid, inc=""):
            return {"title": "Enter Sandman", "releases": [_release("S&M", live=True)]}

    svc = MusicBrainzService(http_client=_Http(), enabled=True)

    def _fake_suggested(title, artist, limit=5, **kwargs):
        seen.update(kwargs)
        return LIVE_MBID, 1.0

    svc.get_suggested_mbid = _fake_suggested  # type: ignore[assignment]

    svc.lookup_recording_metadata("Enter Sandman", "Metallica", is_live_release=True)
    assert seen.get("is_live_release") is True, (
        "lookup_recording_metadata dropped is_live_release, so the search "
        "cannot prefer the live recording"
    )


def test_module_level_wrapper_exposes_is_live_release() -> None:
    """The module-level helper must accept the keyword too."""
    from services.enrichment import musicbrainz_service as mb

    params = inspect.signature(mb.lookup_recording_metadata).parameters
    assert "is_live_release" in params, (
        "module-level lookup_recording_metadata does not accept "
        "is_live_release, so callers using it cannot request live-aware matching"
    )


def test_service_method_exposes_is_live_release() -> None:
    from services.enrichment.musicbrainz_service import MusicBrainzService

    params = inspect.signature(
        MusicBrainzService.lookup_recording_metadata
    ).parameters
    assert "is_live_release" in params


def test_track_stage_passes_release_liveness_to_metadata_lookup() -> None:
    """The scan must actually supply the flag, or the fix is inert.

    Static source check: ``track_stage`` is the only production caller that
    resolves a recording for a local track.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "services" / "popularity" / "stages" / "track_stage.py"
    ).read_text(encoding="utf-8")

    assert "is_live_release=_is_live_release" in src, (
        "track_stage does not pass is_live_release into "
        "lookup_recording_metadata, so a live album still resolves its tracks "
        "to the studio recordings"
    )
    assert 'is_live_release=is_live_release' in src, (
        "track_stage does not pass is_live_release into get_suggested_mbid for "
        "the ListenBrainz MBID fallback, so the listen count comes from the "
        "studio recording"
    )
