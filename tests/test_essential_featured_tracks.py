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
    """⚠️ ASSERTS THE INTENT, NOT THE OLD SPELLING.

    The query used to carry eighteen leading-wildcard ``ILIKE``s. No index serves
    ``%x%``, so it was a sequential scan with eight comparisons per column. It is
    now six ``LIKE`` terms (broader, and portable to SQLite) — but the property
    that matters is unchanged: guest credits are searched in ALL THREE columns,
    since Navidrome/metadata correction can leave the credit in any of them.

    Anything the pre-filter lets through is re-validated in Python by
    ``_track_has_featured_artist``, which is the authority.
    """
    source = inspect.getsource(fs._fetch_essential_featured_rows)
    for column in ("artist", "album_artist", "title"):
        assert f"LOWER({column}) LIKE" in source, (
            f"the featured-rows query no longer searches `{column}`, so a guest "
            f"credit stored there would never reach that artist's collection"
        )
    assert "feat_term" in source and "ft_term" in source


def test_the_pre_filter_is_broader_than_it_needs_to_be():
    """CONTROL: a false negative drops a guest track; a false positive is free.

    ``_track_has_featured_artist`` rejects anything the SQL lets through
    wrongly, so the pre-filter must never be NARROWER than the separators a real
    credit can use.
    """
    assert "%feat%" in fs._ESSENTIAL_FEAT_SQL_TERMS  # feat, feat., featuring
    assert "%ft%" in fs._ESSENTIAL_FEAT_SQL_TERMS  # " ft ", "ft."

