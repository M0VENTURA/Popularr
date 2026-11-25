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
    # ⚠️ ``id`` is a JOIN KEY, not a report column: the playlist-membership map
    # is keyed by track id, and the value is written under
    # ``playlists_included_in``. Every OTHER selected name must be a column the
    # report actually writes, or the query is pulling data nothing reads.
    non_report_keys = {"id"}
    assert (selected - non_report_keys) <= set(svc.POPULARITY_REPORT_COLUMNS)


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


class TestTheMusicBrainzIdDiagnostic:
    def test_an_album_with_multiple_release_ids_is_flagged(self):
        assert svc._mb_id_diagnostic(
            {"musicbrainz_albumid": "a"},
            album_has_multiple_releases=True,
        ) == "album_has_multiple_releases"

    def test_a_release_id_in_the_group_column_is_flagged(self):
        assert svc._mb_id_diagnostic({
            "musicbrainz_albumid": "c1fd5078-c940-3bc1-8102-fe83a56c4ef0",
            "musicbrainz_releasegroupid": "c1fd5078-c940-3bc1-8102-fe83a56c4ef0",
        }) == "release_equals_group"

    def test_a_compilation_track_using_the_album_artist_id_is_flagged(self):
        assert svc._mb_id_diagnostic({
            "album_artist": "Various Artists",
            "musicbrainz_albumartistid": "89ad4ac3-39f7-470e-963a-56509c546377",
            "musicbrainz_artistid": "89ad4ac3-39f7-470e-963a-56509c546377",
        }) == "artist_is_album_artist"

    def test_a_lead_artist_matching_the_album_artist_is_fine(self):
        assert svc._mb_id_diagnostic({
            "album_artist": "Korn",
            "musicbrainz_albumartistid": "a1",
            "musicbrainz_artistid": "a1",
        }) == ""

    def test_a_healthy_compilation_is_fine(self):
        assert svc._mb_id_diagnostic({
            "album_artist": "Various Artists",
            "musicbrainz_albumartistid": "va-id",
            "musicbrainz_artistid": "vitro-id",
            "musicbrainz_albumid": "release-1",
            "musicbrainz_releasegroupid": "group-1",
        }) == ""


class TestTheScoreDiagnostic:
    """The audit's highest-value score/popularity disagreements."""

    def test_a_famous_track_with_a_low_score_is_flagged(self):
        assert svc._score_diagnostic({
            "final_score": 9.19,
            "lastfm_listeners": 835_000,
            "listenbrainz_listens": 2_000,
            "is_live": 0,
            "exclude_from_stats": False,
        }) == "popular_but_low_score"

    def test_a_high_score_with_no_evidence_is_flagged(self):
        assert svc._score_diagnostic({
            "final_score": 95.0,
            "lastfm_listeners": 0,
            "listenbrainz_listens": 0,
            "is_live": 0,
            "exclude_from_stats": False,
        }) == "high_score_low_evidence"

    def test_live_and_excluded_rows_are_not_flagged(self):
        row = {
            "final_score": 0,
            "lastfm_listeners": 500_000,
            "listenbrainz_listens": 0,
            "is_live": 1,
            "exclude_from_stats": False,
        }
        assert svc._score_diagnostic(row) == ""
        assert svc._score_diagnostic({**row, "is_live": 0, "exclude_from_stats": True}) == ""

    def test_an_ordinary_row_is_blank(self):
        assert svc._score_diagnostic({
            "final_score": 55.0,
            "lastfm_listeners": 50_000,
            "listenbrainz_listens": 500,
            "is_live": 0,
            "exclude_from_stats": False,
        }) == ""

    def test_the_column_is_reported(self):
        assert "score_diagnostic" in svc.POPULARITY_REPORT_COLUMNS

