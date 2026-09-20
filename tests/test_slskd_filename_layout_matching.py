"""Soulseek filename LAYOUTS must not decide a match.

The old ``queue_processor`` scored candidates with a single function whose only
requirement was that the artist and title appeared in the basename, so every
common peer layout matched:

    Artist/Album/Song.flac          (artist + album in FOLDERS, bare title)
    Artist/Album/01. Song.flac      (dot-separated track number)
    Artist \u2013 Song.mp3            (en dash / em dash separator)
    Artist - Song.mp3

``download_pipeline_service`` replaced that with a per-field scorer
(``_parse_filename_parts`` + ``_score_result``).  The parser only understood a
" - " separator, so for those layouts it returned ``title = None`` \u2014 and the
HARD TITLE GATE reads exactly that field.  Every such candidate scored 0.0 and
was rejected, while the identical file matched fine before the rewrite.  That is
the reported "it matched really well in the old system but not in the new one".

Two invariants are pinned here:

1. ``_parse_filename_parts`` ALWAYS yields a title (the basename is the title
   when no structured layout is recognised), and never truncates a title that
   merely starts with digits.
2. ``_score_result`` accepts the common layouts, and still rejects a candidate
   whose title or artist disagrees \u2014 the strictness the gates exist for.
"""

from __future__ import annotations

import pytest

from services.downloads.download_pipeline_service import (
    _parse_filename_parts,
    _score_result,
    _select_best_result,
)

#: ``_select_best_result``'s acceptance floor.
ACCEPT = 45.0


def _score(filename: str, item: dict) -> float:
    return _score_result(
        {"filename": filename},
        item.get("artist"),
        item.get("title"),
        item.get("album"),
        item.get("duration"),
        item.get("year"),
    )


METALLICA = {"artist": "Metallica", "title": "Enter Sandman", "album": "Metallica", "duration": 331}


# ---------------------------------------------------------------------------
# 1. The parser must always produce a title
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename,expected_title",
    [
        # bare title basename \u2014 artist/album live in the folders
        ("Metallica/Enter Sandman.mp3", "Enter Sandman"),
        ("Music/Metallica/Metallica/Enter Sandman.flac", "Enter Sandman"),
        # dot-separated track numbers (and the no-space form)
        ("Metallica/01. Enter Sandman.mp3", "Enter Sandman"),
        ("Metallica/01.Enter Sandman.mp3", "Enter Sandman"),
        ("Metallica/1. Enter Sandman.mp3", "Enter Sandman"),
        # space-separated track number
        ("Metallica/01 Enter Sandman.mp3", "Enter Sandman"),
        # the long-standing " - " layout keeps working
        ("Metallica/01 - Enter Sandman.mp3", "Enter Sandman"),
        ("Metallica - Metallica - 01 - Enter Sandman.flac", "Enter Sandman"),
        # dash variants used as the separator
        ("Queen \u2013 Bohemian Rhapsody.mp3", "Bohemian Rhapsody"),
        ("Queen \u2014 Bohemian Rhapsody.mp3", "Bohemian Rhapsody"),
    ],
)
def test_parser_always_yields_the_track_title(filename: str, expected_title: str) -> None:
    assert _parse_filename_parts(filename)["title"] == expected_title


@pytest.mark.parametrize(
    "filename",
    [
        # A title that merely STARTS with digits must not be truncated: the
        # track-number prefix needs a separator or whitespace after the digits.
        "Prince/1999.mp3",
        "Prince/1999.flac",
        "Bowling For Soup/1985.mp3",
    ],
)
def test_parser_does_not_truncate_numeric_titles(filename: str) -> None:
    title = _parse_filename_parts(filename)["title"]
    assert title, f"{filename} parsed to no title"
    assert title[0].isdigit(), f"{filename} lost its leading digits: {title!r}"


def test_parser_normalises_dash_separators_only_when_flanked_by_spaces() -> None:
    """A dash INSIDE a title is not a separator."""
    parts = _parse_filename_parts("Prince/1999\u20132000.mp3")
    assert parts["title"] == "1999\u20132000"


# ---------------------------------------------------------------------------
# 2. The scorer must accept the layouts the old pipeline matched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename",
    [
        "Metallica/Enter Sandman.mp3",
        "Music/Metallica/Metallica/Enter Sandman.flac",
        "Metallica/01. Enter Sandman.mp3",
        "Metallica/01.Enter Sandman.mp3",
        "Metallica/1. Enter Sandman.mp3",
        "Metallica/01 Enter Sandman.mp3",
        "Metallica/01 - Enter Sandman.mp3",
        "Metallica - Metallica - 01 - Enter Sandman.flac",
    ],
)
def test_bare_and_numbered_basename_layouts_are_accepted(filename: str) -> None:
    score = _score(filename, METALLICA)
    assert score >= ACCEPT, (
        f"{filename!r} scored {score} \u2014 a plain correct file must clear the "
        f"{ACCEPT} floor. The parser/title gate has regressed again."
    )


@pytest.mark.parametrize(
    "filename",
    [
        "Queen \u2013 Bohemian Rhapsody.mp3",
        "Queen \u2014 Bohemian Rhapsody.mp3",
        "Queen - Bohemian Rhapsody.mp3",
    ],
)
def test_dash_variant_separators_are_accepted(filename: str) -> None:
    item = {"artist": "Queen", "title": "Bohemian Rhapsody", "album": "A Night At The Opera"}
    assert _score(filename, item) >= ACCEPT


def test_featured_artist_credit_mismatch_still_matches() -> None:
    """The queue credit may carry a guest the filename never mentions.

    The evidence gate strips "feat. \u2026" before accepting a candidate, so the
    SCORE must be computed from the same stripped credit.  Scoring the raw
    credit credited the artist with 0 points, and a perfect title match then
    landed at 25.0 \u2014 under the floor \u2014 for a file the gate had just approved.
    """
    item = {"artist": "KNEECAP feat. Fawzi", "title": "Better Way To Live", "album": "Fine Art"}
    for filename in (
        "KNEECAP - Better Way To Live.mp3",
        "KNEECAP - Better Way To Live (feat. Fawzi).mp3",
        "KNEECAP/Fine Art/01 Better Way To Live.mp3",
    ):
        assert _score(filename, item) >= ACCEPT, f"{filename!r} should match {item['artist']!r}"


# ---------------------------------------------------------------------------
# 3. Leniency must not become wrong-file acceptance
# ---------------------------------------------------------------------------

def test_wrong_title_is_still_rejected() -> None:
    assert _score("Metallica/Metallica/Enter Sandman.mp3", {**METALLICA, "title": "Master Of Puppets"}) == 0.0


def test_wrong_artist_in_folder_is_still_rejected() -> None:
    item = {"artist": "The Pretty Reckless", "title": "Heaven Knows", "album": "Going To Hell"}
    assert _score("The Jesus and Mary Chain/Darklands/Heaven Knows.mp3", item) == 0.0


def test_year_mismatch_is_still_rejected() -> None:
    item = {"artist": "Metallica", "title": "Enter Sandman", "album": "Metallica", "year": 1991}
    assert _score("Metallica/Metallica [2019]/01. Enter Sandman.mp3", item) == 0.0


def test_select_best_result_picks_the_correct_file_from_a_mixed_pool() -> None:
    """End-to-end: a realistic result pool must resolve to the right file."""
    item = {"artist": "Aephanemer", "title": "Utopie", "album": "Utopie", "duration": 280}
    pool = [
        {"filename": "Aephanemer/Utopie/01 - Prologue.flac"},
        {"filename": "Aephanemer/Utopie/15 - Utopie.flac"},
        {"filename": "Some Other Band/Utopie/01 - Utopie.mp3"},
        {"filename": "Aephanemer/Utopie/09 - Le Cimetiere Marin.flac"},
    ]
    best = _select_best_result(
        pool, item["artist"], item["title"], item["album"], item["duration"], None,
    )
    assert best is not None, "no candidate was accepted from a pool containing the correct file"
    assert best["filename"] == "Aephanemer/Utopie/15 - Utopie.flac"
