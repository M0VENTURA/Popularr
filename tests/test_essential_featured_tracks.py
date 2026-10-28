"""Guest credits can live in artist, album_artist or title."""

from __future__ import annotations

import inspect

from services.popularity.stages import finalise_stage as fs


def test_featured_guest_in_artist_field_matches():
    assert fs._track_has_featured_artist("Poppy feat. Grimes", "Grimes") is True


def test_featured_guest_in_title_parentheses_matches():
    assert fs._track_has_featured_artist(
        "Poppy", "Grimes", title_field="Play Destroy (feat. Grimes)"
    ) is True


def test_featured_guest_in_album_artist_matches():
    assert fs._track_has_featured_artist(
        "Poppy", "Grimes", album_artist_field="Poppy featuring Grimes"
    ) is True


def test_multiple_guests_and_parenthesis_are_handled():
    assert fs._track_has_featured_artist(
        "Lead", "Halsey", title_field="Song (feat. Grimes & Halsey)"
    ) is True


def test_the_lead_artist_is_not_treated_as_a_guest():
    assert fs._track_has_featured_artist("Poppy feat. Grimes", "Poppy") is False


def test_guest_credit_strips_a_parenthesised_feature():
    assert fs._essential_strip_guest_credit("Poppy (feat. Grimes)") == "Poppy"


def test_the_featured_query_searches_all_credit_fields():
    source = inspect.getsource(fs._fetch_essential_featured_rows)
    assert "album_artist ILIKE" in source
    assert "title ILIKE" in source

