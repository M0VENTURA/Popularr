"""The popularity report must expose the diagnostics behind its scores."""

from __future__ import annotations

from services.popularity import popularity_report_service as svc


def test_report_exposes_raw_score_duration_and_exclusion_diagnostics():
    columns = set(svc.POPULARITY_REPORT_COLUMNS)
    assert {
        "raw_score",
        "duration",
        "exclude_from_stats",
        "lastfm_playcount_available",
    } <= columns


def test_every_new_report_column_is_actually_selected():
    select_block = svc._POPULARITY_REPORT_SELECT.split("FROM tracks")[0]
    selected = {
        name.strip()
        for name in select_block.replace("SELECT", "").split(",")
        if name.strip()
    }
    assert {
        "raw_score",
        "duration",
        "exclude_from_stats",
        "lastfm_playcount_available",
    } <= selected
    assert selected <= set(svc.POPULARITY_REPORT_COLUMNS)
