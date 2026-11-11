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


def test_report_lists_every_genre_source_and_the_final_genre():
    """The config-page export must show where a track's genre came from."""
    columns = set(svc.POPULARITY_REPORT_COLUMNS)
    assert {
        "listenbrainz_genres",
        "musicbrainz_genres",
        "lastfm_tags",
        "discogs_genres",
        "genres",
    } <= columns

    select_block = svc._POPULARITY_REPORT_SELECT.split("FROM tracks")[0]
    selected = {
        name.strip()
        for name in select_block.replace("SELECT", "").split(",")
        if name.strip()
    }
    assert {
        "listenbrainz_genres",
        "musicbrainz_genres",
        "lastfm_tags",
        "discogs_genres",
        "genres",
    } <= selected


def test_genre_lists_are_written_as_one_delimited_cell():
    """A JSONB list off psycopg must not land in the CSV as a Python repr."""
    assert svc._genre_cell(["Rock", "Pop"]) == "Rock; Pop"
    assert svc._genre_cell('["Rock", "Pop"]') == "Rock; Pop"
    assert svc._genre_cell("Rock, Pop") == "Rock; Pop"
    assert svc._genre_cell(None) == ""
