"""Regression tests for album downloads being added as all tracks.

Covers the fix for albums collapsing to a single queue row:

- ``resolve_release_id`` converts a release-group MBID handed over by the
  MusicBrainz search UI into a concrete release MBID (legacy parity with
  old_system's ``_fetch_release_payload`` browse fallback), so
  ``start_release_download`` can fetch per-track data instead of failing and
  letting the route fall back to a one-row "album as one track" add.
- ``start_release_download`` uses the resolved release MBID for the fetch,
  the ``musicbrainz_releases`` upsert and the per-track queue insert.
"""

from __future__ import annotations

import pytest

from services.enrichment.musicbrainz_service import resolve_release_id
from services.downloads import download_pipeline_service as dps


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeHttp:
    """Stands in for MusicBrainzHttpClient to simulate 404 / browse."""

    def __init__(self):
        self.calls: list = []
        self._release_404 = True
        self._browse_releases: list = []

    def set_release_data(self, data: dict):
        self._release_404 = not bool(data and data.get("id"))
        self._release_data = data or {}

    def set_browse(self, releases: list):
        self._browse_releases = releases or []

    def get_release(self, release_mbid: str, inc: str = "", timeout: float = 10.0):
        self.calls.append(("release", release_mbid))
        if self._release_404:
            class _Exc(Exception):
                pass
            raise _Exc("404")
        return self._release_data

    def browse_releases_for_group(self, release_group_mbid: str, inc: str = "media", limit: int = 50):
        self.calls.append(("browse", "release", {"release-group": release_group_mbid, "inc": inc, "limit": limit}))
        return self._browse_releases

    def get(self, endpoint: str, *, params=None, timeout: float = 10.0):
        self.calls.append(("browse", endpoint, params))
        return {"releases": self._browse_releases}


class _FakeService:
    def __init__(self):
        self.http = _FakeHttp()


def _install_fake_service(monkeypatch, service=None):
    svc = service or _FakeService()
    monkeypatch.setattr("services.enrichment.musicbrainz_service._get_service", lambda: svc)
    return svc


# ---------------------------------------------------------------------------
# resolve_release_id
# ---------------------------------------------------------------------------

def test_release_id_passes_through_when_release_lookup_succeeds(monkeypatch):
    svc = _FakeService()
    svc.http.set_release_data({"id": "rel-123", "title": "Abyss"})
    _install_fake_service(monkeypatch, svc)

    assert resolve_release_id("rel-123") == "rel-123"


def test_release_group_mbid_resolved_to_concrete_release(monkeypatch):
    svc = _FakeService()
    svc.http.set_browse([{"id": "rel-456", "title": "Abyss"}])
    _install_fake_service(monkeypatch, svc)

    assert resolve_release_id("rg-abc") == "rel-456"


def test_unknown_id_returns_input_when_browse_empty(monkeypatch):
    svc = _FakeService()
    svc.http.set_browse([])
    _install_fake_service(monkeypatch, svc)

    assert resolve_release_id("rg-abc") == "rg-abc"


def test_empty_input_returns_input(monkeypatch):
    _install_fake_service(monkeypatch)
    assert resolve_release_id("") == ""


# ---------------------------------------------------------------------------
# start_release_download uses the resolved release MBID end to end
# ---------------------------------------------------------------------------

def test_start_release_download_resolves_group_and_adds_per_track(monkeypatch):
    from services.downloads import download_pipeline_service as _dps

    calls = {"upsert": [], "add_tracks": [], "fetch_raw": [], "fetch_svc": []}

    def fake_resolve(rid):
        return "rel-456" if rid == "rg-abc" else rid

    def fake_raw_fetch(rid):
        calls["fetch_raw"].append(rid)
        return None

    def fake_svc_fetch(rid):
        calls["fetch_svc"].append(rid)
        return {
            "release_title": "Abyss",
            "release_year": 2024,
            "artist": "Ad Infinitum",
            "tracks": [
                {"title": "My Halo", "track_number": 1, "recording_mbid": "r1"},
                {"title": "Dead End", "track_number": 2, "recording_mbid": "r2"},
            ],
        }

    def fake_upsert(rid, *a, **k):
        calls["upsert"].append(rid)
        return 99

    def fake_add(rid, tracks, artist, album, **k):
        calls["add_tracks"].append((rid, len(tracks)))
        return {
            "queue_ids": [1, 2],
            "queued": True,
            "reason": None,
            "message": "",
            "total_tracks": len(tracks),
            "in_library": 0,
            "already_queued": 0,
            "already_active": 0,
            "duplicate": 0,
        }

    def fake_mkdir(*a, **k):
        return "/tmp/monitoring/abyss"

    monkeypatch.setattr(_dps, "resolve_release_id", fake_resolve)
    monkeypatch.setattr(_dps, "fetch_musicbrainz_release_metadata", fake_raw_fetch)
    monkeypatch.setattr(_dps, "fetch_release_metadata", fake_svc_fetch)
    monkeypatch.setattr(_dps, "upsert_musicbrainz_release", fake_upsert)
    monkeypatch.setattr(_dps, "add_release_tracks_to_queue_detailed", fake_add)
    monkeypatch.setattr(_dps, "create_monitoring_folder", fake_mkdir)

    result = _dps.start_release_download("rg-abc", "Abyss", "Ad Infinitum", method="slskd")

    assert result["success"] is True
    assert result["queue_items_created"] == 2
    # The resolved release MBID (not the release-group id) is used everywhere.
    assert calls["fetch_svc"] == ["rel-456"]
    assert calls["upsert"] == ["rel-456"]
    assert calls["add_tracks"] == [("rel-456", 2)]
