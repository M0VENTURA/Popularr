"""An unchanged rejected file must not be re-probed every discovery cycle.

REPORTED
--------
``/downloads/SKZ2020/1-01 風 (Levanter) -Japanese ver.-.m4a`` was logged as
``m4a @ ?kbps doesn't match quality priorities`` on every scan, and each scan
read its metadata again. The discovery hook runs inside the queue's 120 s
maintenance budget.
"""

from __future__ import annotations

from services.downloads import download_scan_service as dss

_POLICY = {
    "enabled": True,
    "reject_others": True,
    "bitrate_tolerance": 1,
    "priorities": [{"format": "flac", "bitrate_kbps": 1000}],
}


def test_unchanged_rejected_file_is_probed_once(monkeypatch, tmp_path):
    path = tmp_path / "song.m4a"
    path.write_bytes(b"x")

    probes: list[str] = []

    def _read(file_path):
        probes.append(file_path)
        return {"bitrate": 128000}

    monkeypatch.setattr(dss, "_quality_filter_config", lambda: dict(_POLICY))
    monkeypatch.setattr(dss, "read_mp3_metadata", _read)
    dss._QUALITY_FILTER_CACHE.clear()
    dss._QUALITY_FILTER_LOGGED.clear()

    first_ok, first_reason = dss._matches_quality_filter(str(path))
    second_ok, second_reason = dss._matches_quality_filter(str(path))

    assert first_ok is False
    assert second_ok is False
    assert first_reason == second_reason
    assert len(probes) == 1, "the unchanged file was re-read on the second scan"

    # A changed file must be reconsidered.
    path.write_bytes(b"xy")
    dss._matches_quality_filter(str(path))
    assert len(probes) == 2, "a changed file (new size) must be re-probed"


def test_a_changed_policy_reconsiders_the_file(monkeypatch, tmp_path):
    path = tmp_path / "song.m4a"
    path.write_bytes(b"x")

    probes: list[str] = []
    monkeypatch.setattr(
        dss,
        "read_mp3_metadata",
        lambda file_path: probes.append(file_path) or {"bitrate": 128000},
    )
    dss._QUALITY_FILTER_CACHE.clear()

    monkeypatch.setattr(dss, "_quality_filter_config", lambda: dict(_POLICY))
    dss._matches_quality_filter(str(path))

    changed = dict(_POLICY)
    changed["bitrate_tolerance"] = 5
    monkeypatch.setattr(dss, "_quality_filter_config", lambda: changed)
    dss._matches_quality_filter(str(path))

    assert len(probes) == 2, "a policy change must re-evaluate the file"
