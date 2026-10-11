"""A known recording MBID must not be re-searched.

Reported: *"I noticed repeated patterns like 'recording suggestion
completed' for every track, even after a recording MBID is known."*

The scan path that enriches a track with an MBID but no genres calls
``lookup_recording_metadata(..., mbid=<stored>)``. The method **ignored** the
``mbid`` kwarg and always ran ``get_suggested_mbid`` — a full
``recording.search`` against the shared 1 req/s budget — for every such
track, on every scan. Besides the wasted request, the re-ranking can churn
the id: a tie-break may pick a DIFFERENT recording than the one already
stored.

The fix: when a known MBID is supplied, skip the search and fetch the
metadata directly. The search is only used when no MBID is known, or when a
stored MBID no longer resolves (deleted recording) — a single recovery
search, not a per-scan one.
"""

from __future__ import annotations

import inspect


KNOWN_MBID = "33333333-3333-3333-3333-333333333333"
SEARCHED_MBID = "44444444-4444-4444-4444-444444444444"


class _FakeHttp:
    """Counts searches and metadata fetches; serves a recording by MBID."""

    def __init__(self, resolve_known: bool = True) -> None:
        self.search_calls: list[str] = []
        self.get_calls: list[str] = []
        self.resolve_known = resolve_known

    def search_recordings(self, query, limit=10, inc=""):
        self.search_calls.append(query)
        return [
            {
                "id": SEARCHED_MBID,
                "title": "Enter Sandman",
                "score": 100,
                "artist-credit": [{"name": "Metallica", "artist": {"id": "a-1"}}],
                "releases": [],
            }
        ]

    def get_recording(self, mbid, inc=""):
        self.get_calls.append(mbid)
        if mbid == KNOWN_MBID and not self.resolve_known:
            return {}
        return {
            "id": mbid,
            "title": "Enter Sandman",
            "artist-credit": [{"name": "Metallica", "artist": {"id": "a-1"}}],
            "releases": [],
        }


def _service(resolve_known: bool = True):
    from services.enrichment.musicbrainz_service import MusicBrainzService

    http = _FakeHttp(resolve_known=resolve_known)
    svc = MusicBrainzService(http_client=http, enabled=True)
    svc._mbid_cache.clear()
    return svc, http


# ---------------------------------------------------------------------------
# 1. The core bug: a known MBID must not trigger a search
# ---------------------------------------------------------------------------

def test_known_mbid_skips_the_recording_search() -> None:
    svc, http = _service()
    result = svc.lookup_recording_metadata(
        "Enter Sandman", "Metallica", album="Metallica", mbid=KNOWN_MBID,
    )

    assert http.search_calls == [], (
        "a recording.search ran even though the MBID was known — that is the "
        "reported 'recording suggestion completed for every track' spam"
    )
    assert http.get_calls == [KNOWN_MBID]
    assert result.get("recording_mbid") == KNOWN_MBID


def test_known_mbid_metadata_is_returned() -> None:
    svc, http = _service()
    result = svc.lookup_recording_metadata(
        "Enter Sandman", "Metallica", mbid=KNOWN_MBID,
    )
    assert result.get("recording_mbid") == KNOWN_MBID
    assert result.get("title")


# ---------------------------------------------------------------------------
# 2. No MBID -> the search still runs (unchanged behaviour)
# ---------------------------------------------------------------------------

def test_unknown_track_still_searches() -> None:
    svc, http = _service()
    result = svc.lookup_recording_metadata("Enter Sandman", "Metallica")

    assert len(http.search_calls) == 1
    assert result.get("recording_mbid") == SEARCHED_MBID


# ---------------------------------------------------------------------------
# 3. A stored MBID that no longer resolves recovers with ONE search
# ---------------------------------------------------------------------------

def test_stale_mbid_falls_back_to_search_once() -> None:
    svc, http = _service(resolve_known=False)
    result = svc.lookup_recording_metadata(
        "Enter Sandman", "Metallica", mbid=KNOWN_MBID,
    )

    # The known MBID was tried first, then one recovery search.
    assert http.get_calls[0] == KNOWN_MBID
    assert len(http.search_calls) == 1
    assert result.get("recording_mbid") == SEARCHED_MBID


def test_stale_mbid_that_search_also_misses_returns_empty() -> None:
    svc, http = _service(resolve_known=False)

    class _NoResults(_FakeHttp):
        def search_recordings(self, query, limit=10, inc=""):
            self.search_calls.append(query)
            return []

    http2 = _NoResults(resolve_known=False)
    from services.enrichment.musicbrainz_service import MusicBrainzService
    svc2 = MusicBrainzService(http_client=http2, enabled=True)
    svc2._mbid_cache.clear()

    result = svc2.lookup_recording_metadata(
        "Enter Sandman", "Metallica", mbid=KNOWN_MBID,
    )
    assert result == {}
    # One get (the known) + one search; no second get.
    assert len(http2.search_calls) == 1


# ---------------------------------------------------------------------------
# 4. The scan path forwards the stored MBID (source-level contract)
# ---------------------------------------------------------------------------

def test_track_stage_forwards_the_stored_mbid() -> None:
    """The caller must pass ``mbid=`` or the optimization never engages."""
    from services.popularity.stages import track_stage

    src = inspect.getsource(track_stage)
    # The lookup call inside the enrichment block must carry mbid=.
    idx = src.index("lookup_recording_metadata(")
    call = src[idx: idx + 600]
    assert "mbid=" in call, (
        "track_stage no longer forwards the stored mbid to "
        "lookup_recording_metadata — the redundant search would return"
    )


def test_module_wrapper_forwards_mbid() -> None:
    from services.enrichment import musicbrainz_service as mbs

    sig = inspect.signature(mbs.lookup_recording_metadata)
    assert "mbid" in sig.parameters, (
        "the module-level wrapper drops the mbid kwarg"
    )


# ---------------------------------------------------------------------------
# 5. Empty/whitespace mbid is treated as unknown (search still runs)
# ---------------------------------------------------------------------------

def test_blank_mbid_still_searches() -> None:
    svc, http = _service()
    svc.lookup_recording_metadata("Enter Sandman", "Metallica", mbid="   ")
    assert len(http.search_calls) == 1
