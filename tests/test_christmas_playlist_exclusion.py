"""Christmas music must be excluded from every playlist except "Christmas" ones.

Reported symptom:

> Christmas songs are being added to playlists, but they were meant to be
> skipped from all playlists other than ones with "Christmas"

There was already a Christmas intercept in the genre-playlist builder, but FOUR
independent gaps let tracks through it.

## Gap 1 — the check ran on a TRUNCATED genre list

``_genre_playlist_track_genres(row, max_genres=3)`` returns at most three
genres, and those are the genres read from the front of the stored string. The
genre aggregator APPENDS filter tags (Christmas / Live / Cover / Remaster)
*after* the voted genres, so a Christmas track with three real genres stores:

    "pop, rock, metal, Christmas"

and the builder's window saw only ``['pop', 'rock', 'metal']``. The check
``"christmas" in norm_track_genres`` was therefore False and the track pooled
straight into the ordinary Pop / Rock / Metal playlists.

## Gap 2 — only the literal word "christmas" was recognised

The aggregator intercepts ``_FILTER_TAGS`` and stores whichever spelling the
source used, so a track can carry ``Holiday``, ``Xmas``, ``Noel``, ``Yule`` or
``Hanukkah`` and be Christmas music. Testing only for "christmas" missed all of
them.

## Gap 3 — Essential Collection had no filter at all

``_sync_essential_playlist`` selected every 4★+ track for the artist. A
Christmas album by that artist put its tracks into their Essential Collection.

## Gap 4 — New Music had no filter at all

``_create_new_music_playlist`` is a rolling "recently added" list. Anything
imported in December — i.e. all of it — surfaced there.

The fix routes every playlist generator through one canonical
``is_christmas_track`` test, and only a playlist whose NAME contains the
configured marker (default "christmas") is allowed to hold Christmas music.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. The canonical detector
# ---------------------------------------------------------------------------


class TestIsChristmasGenre:
    def _check(self):
        from services.catalog.album_classification_service import is_christmas_genre
        return is_christmas_genre

    def test_literal_christmas(self):
        assert self._check()("Pop, Christmas") is True

    def test_every_stored_spelling(self):
        """The aggregator stores whichever spelling the source used."""
        for value in ["Holiday", "Xmas", "X-Mas", "Noel", "Yule", "Yuletide",
                      "Advent", "Hanukkah", "Chanukah"]:
            assert self._check()(value) is True, value

    def test_json_list_form(self):
        assert self._check()('["pop", "christmas"]') is True

    def test_python_list_form(self):
        assert self._check()(["pop", "christmas"]) is True

    def test_non_christmas_genres(self):
        for value in ["Pop", "Rock, Metal", "Hip-Hop", "Folk Rock"]:
            assert self._check()(value) is False, value

    def test_holiday_rock_is_not_matched_as_a_whole_token(self):
        """"Holiday Rock" is a genre; only the bare token means Christmas."""
        assert self._check()("Holiday Rock") is False

    def test_empty_values(self):
        for value in (None, "", [], "null", "[]"):
            assert self._check()(value) is False, value


class TestIsChristmasTrack:
    def _check(self):
        from services.catalog.album_classification_service import is_christmas_track
        return is_christmas_track

    def test_title_signal(self):
        assert self._check()(title="All I Want for Christmas Is You") is True

    def test_album_signal(self):
        assert self._check()(album="A Very Special Christmas") is True

    def test_genre_signal_only(self):
        """A track whose ONLY signal is a "Holiday" tag is still Christmas."""
        assert self._check()(title="Winter Song", album="Seasonal", genres="holiday") is True

    def test_genre_list_signal(self):
        assert self._check()(title="Song", genres=["pop", "christmas"]) is True

    def test_ordinary_track_is_not_christmas(self):
        assert self._check()(title="Enter Sandman", album="Metallica",
                             genres="Metal, Thrash") is False

    def test_no_signals(self):
        assert self._check()() is False


# ---------------------------------------------------------------------------
# 2. The detector must NOT be defeated by truncation (the actual bug)
# ---------------------------------------------------------------------------


def test_christmas_survives_genre_truncation() -> None:
    """The regression that caused the report.

    Stored as "pop, rock, metal, Christmas" — the builder's 3-genre window hides
    the Christmas tag, so the check must run against the FULL stored value.
    """
    from services.catalog.album_classification_service import is_christmas_track

    stored = "pop, rock, metal, Christmas"
    window = [g.strip() for g in re.split(r"[,;/\\]+", stored) if g.strip()][:3]
    assert "christmas" not in [g.lower() for g in window], (
        "this test is only meaningful while Christmas falls outside the window"
    )

    assert is_christmas_track(title="Song", album="Now That's What I Call Christmas",
                              genres=stored) is True
    assert is_christmas_track(title="Song", genres=stored) is True


def test_genre_playlist_builder_uses_the_full_genre_list() -> None:
    """Static guard: the builder must not re-introduce the truncated check."""
    src = (
        REPO_ROOT / "services" / "popularity" / "stages" / "finalise_stage.py"
    ).read_text(encoding="utf-8")

    assert '"christmas" in norm_track_genres' not in src, (
        "the builder is testing the TRUNCATED genre window for 'christmas' "
        "again. That window excludes filter tags appended by the aggregator, so "
        "a Christmas track with 3 real genres is never detected and leaks into "
        "the ordinary genre playlists."
    )
    assert "is_christmas_track(" in src, (
        "the genre-playlist builder no longer calls is_christmas_track"
    )


# ---------------------------------------------------------------------------
# 3. Playlist-name policy: only "Christmas" playlists may hold Christmas music
# ---------------------------------------------------------------------------


class TestPlaylistAllowsChristmas:
    def _allows(self, name):
        from services.popularity.stages.finalise_stage import _playlist_allows_christmas
        return _playlist_allows_christmas(name)

    def test_christmas_playlists_are_allowed(self):
        for name in ["Christmas", "Christmas - Top Tracks",
                     "Christmas Pop - Top Tracks", "CHRISTMAS ROCK"]:
            assert self._allows(name) is True, name

    def test_ordinary_playlists_are_not(self):
        for name in ["Pop - Top Tracks", "Metal - Top Tracks",
                     "Artist - Essential Collection", "New Music", "Loved Tracks"]:
            assert self._allows(name) is False, name


class TestFilterChristmasRows:
    def _filter(self, rows, name):
        from services.popularity.stages.finalise_stage import _filter_christmas_rows
        return _filter_christmas_rows(rows, name)

    ROWS = [
        {"id": "1", "title": "Enter Sandman", "album": "Metallica", "genres": "Metal"},
        {"id": "2", "title": "All I Want for Christmas Is You",
         "album": "Merry Christmas", "genres": "Pop, Christmas"},
        {"id": "3", "title": "Jingle Bell Rock", "album": "Xmas Hits", "genres": "Rock"},
        # Christmas ONLY via a "Holiday" tag outside the 3-genre window
        {"id": "4", "title": "Winter Song", "album": "Seasonal",
         "genres": "pop, rock, metal, Holiday"},
        {"id": "5", "title": "Nothing Else Matters", "album": "Metallica", "genres": "Metal"},
    ]

    def test_ordinary_playlist_excludes_all_christmas(self):
        kept = self._filter(self.ROWS, "Metal - Top Tracks")
        ids = {r["id"] for r in kept}
        assert ids == {"1", "5"}, (
            f"Christmas tracks leaked into an ordinary playlist: {sorted(ids)}"
        )

    def test_christmas_playlist_keeps_them(self):
        kept = self._filter(self.ROWS, "Christmas - Top Tracks")
        assert len(kept) == len(self.ROWS)

    def test_exclusion_can_be_disabled(self, monkeypatch):
        import services.popularity.stages.finalise_stage as fs

        monkeypatch.setattr(fs, "_christmas_exclusion_enabled", lambda: False)
        kept = self._filter(self.ROWS, "Metal - Top Tracks")
        assert len(kept) == len(self.ROWS), (
            "with the exclusion disabled nothing should be removed"
        )

    def test_empty_input(self):
        assert self._filter([], "Metal - Top Tracks") == []


# ---------------------------------------------------------------------------
# 4. Every generator must apply the filter
# ---------------------------------------------------------------------------


def _source(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "label, marker",
    [
        ("genre playlists", "def _create_genre_top_track_playlists"),
        ("Essential Collection", "def _sync_essential_playlist"),
        ("New Music", "def _create_new_music_playlist"),
    ],
)
def test_generator_applies_the_christmas_filter(label: str, marker: str) -> None:
    """Each playlist generator must consult the exclusion policy.

    Covers the two generators that had NO filter at all (Essential, New Music)
    and the genre builder whose check ran on truncated data.
    """
    src = _source("services/popularity/stages/finalise_stage.py")
    start = src.find(marker)
    assert start != -1, f"{label}: {marker} not found"

    nxt = src.find("\ndef ", start + 1)
    body = src[start: nxt if nxt != -1 else len(src)]

    assert "is_christmas_track(" in body or "_filter_christmas_rows(" in body, (
        f"{label} does not apply any Christmas exclusion, so Christmas tracks "
        "will be added to it."
    )


def test_essential_and_new_music_select_the_columns_the_filter_needs() -> None:
    """The filter is blind without `genres` (and `album`) in the row.

    Both functions contain MORE THAN ONE ``FROM tracks`` query (e.g. the
    artist-score lookup), so this inspects the query that actually produces the
    rows handed to the filter — the one selecting ``id, title, ...``.
    """
    src = _source("services/popularity/stages/finalise_stage.py")

    for marker in ("def _sync_essential_playlist", "def _create_new_music_playlist"):
        start = src.find(marker)
        assert start != -1, f"{marker} not found"
        nxt = src.find("\ndef ", start + 1)
        body = src[start: nxt if nxt != -1 else len(src)]

        # The row-producing query is the one that selects the playlist columns.
        queries = re.findall(r'text\("""(.*?)"""\)', body, re.DOTALL)
        row_query = next(
            (q for q in queries if "COALESCE(stars, star_rating) AS stars" in q),
            None,
        )
        assert row_query is not None, (
            f"{marker}: could not locate the row-producing query"
        )
        assert "genres" in row_query, (
            f"{marker} does not SELECT `genres`, so the Christmas filter cannot "
            "see the genre signal and only title/album matches are caught"
        )


def test_genre_rows_query_selects_album_for_the_filter() -> None:
    """`album` is part of the canonical test, so it must be selected."""
    src = _source("services/popularity/stages/finalise_stage.py")
    start = src.find("_GENRE_ROWS_SQL = ")
    end = src.find('"""', src.find('"""', start) + 3)
    sql = src[start:end]
    assert "album" in sql, (
        "_GENRE_ROWS_SQL does not select `album`, so a Christmas track whose "
        "only signal is its album name is not detected"
    )
    assert "genres" in sql


# ---------------------------------------------------------------------------
# 5. Config surface
# ---------------------------------------------------------------------------


def test_config_defaults_expose_the_exclusion() -> None:
    from helpers.config_helpers import get_playlists_config

    cfg = get_playlists_config()
    assert cfg.get("exclude_christmas_from_playlists") is True, (
        "the exclusion must default to ON — that is the intended behaviour"
    )
    assert cfg.get("christmas_playlist_marker") == "christmas"


def test_config_page_exposes_both_settings() -> None:
    """Every configurable option must appear on the Config page."""
    for rel in ("templates/pages/config.html",
                "test_site/templates/Pages/config.html"):
        html = _source(rel)
        assert "playlists_exclude_christmas" in html, (
            f"{rel} has no control for exclude_christmas_from_playlists"
        )
        assert "playlists_christmas_marker" in html, (
            f"{rel} has no control for christmas_playlist_marker"
        )


def test_config_js_persists_both_settings() -> None:
    """A control that is not in the save payload silently never persists."""
    for rel, field in (
        ("static/js/config.js", "exclude_christmas_from_playlists"),
        ("test_site/static/js/pages/config.js", "exclude_christmas_from_playlists"),
    ):
        js = _source(rel)
        assert field in js, (
            f"{rel} does not send `{field}` when the Config page is saved, so "
            "toggling the switch would have no effect"
        )
        assert "christmas_playlist_marker" in js, (
            f"{rel} does not send `christmas_playlist_marker`"
        )
