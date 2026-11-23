"""A vocabulary entry's ``scope`` must actually be enforced.

⭐ THE REPORTED BUG

The user's vocabulary (schema v4) declares, per genre:

    soundtrack / film_score / video_game_music
        require_confirmation: true, confirm_release_types: ["Soundtrack"]
    christmas / comedy / soundtrack
        slot: "marker"       (never consumes a Maximum Genres slot)

None of it happened. ``GenreEntry`` had no ``scope`` field, so the block was
discarded on load — and ``select_album_or_track_genres`` had no album or
release-type parameter either, so the confirm rules could not have fired from any
caller even if they had been parsed. Measured before the fix: three track-level
"Soundtrack" tags on a plain studio album produced ``Soundtrack`` as a final
genre, and a lone "Christmas" tag was removed as a family outlier.

These tests pin the four rules in the order the selector applies them.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from services.enrichment import genre_vocabulary as gv
from services.enrichment.genre_scope import (
    REASON_ARTIST_MIN_SOURCES,
    REASON_LEVEL_NOT_ALLOWED,
    REASON_UNCONFIRMED,
    evidence_levels_allowed,
    reject_reason,
    split_markers,
)
from services.enrichment.genre_vocabulary import GenreScope, validate_vocabulary
from services.enrichment.genre_selection import select_album_or_track_genres


def _vocabulary(**scopes: dict) -> gv.Vocabulary:
    """The shipped vocabulary with the given entries' scope replaced."""
    data = json.loads(
        pathlib.Path(gv.DEFAULT_VOCABULARY_PATH).read_text(encoding="utf-8")
    )
    for entry in data["genres"]:
        if entry["id"] in scopes:
            entry["scope"] = scopes[entry["id"]]
    return gv._build(data, path="<test>", is_override=False, errors=())


SOUNDTRACK_SCOPE = {
    "levels": ["track", "album"],
    "slot": "marker",
    "confirm_release_types": ["Soundtrack"],
    "confirm_album_title_patterns": [r"(?i)\bmotion picture soundtrack\b"],
    "require_confirmation": True,
}
CHRISTMAS_SCOPE = {
    "levels": ["track", "album"],
    "slot": "marker",
    "confirm_title_patterns": [r"(?i)\b(christmas|xmas)\b"],
}

TRACK_ONLY_SOUNDTRACK = {
    "lastfm@track": ["Soundtrack"],
    "musicbrainz@track": ["Soundtrack"],
    "listenbrainz@track": ["Soundtrack"],
}


class TestScopeIsParsed:
    def test_the_loader_keeps_scope(self) -> None:
        vocab = _vocabulary(soundtrack=SOUNDTRACK_SCOPE)
        scope = vocab.genres["soundtrack"].scope
        assert scope.require_confirmation is True
        assert scope.confirm_release_types == ("Soundtrack",)
        assert scope.is_marker is True

    def test_an_entry_without_scope_is_the_default(self) -> None:
        vocab = _vocabulary()
        assert vocab.genres["rock"].scope.is_default

    def test_the_scope_survives_the_family_backfill(self) -> None:
        """``_build`` rebuilds every entry to back-fill the inherited family.

        Dropping ``scope`` in that rebuild re-discarded it a second time: the
        file parsed and the value still arrived empty at the selector.
        """
        vocab = _vocabulary(soundtrack=SOUNDTRACK_SCOPE)
        assert vocab.genres["soundtrack"].scope.require_confirmation is True


class TestRequireConfirmation:
    def test_a_track_tag_alone_is_rejected(self) -> None:
        """The exact reported case: no soundtrack release, no title to match."""
        vocab = _vocabulary(soundtrack=SOUNDTRACK_SCOPE)
        result = select_album_or_track_genres(
            TRACK_ONLY_SOUNDTRACK, max_genres=4, vocab=vocab,
            album_title="Greatest Hits",
        )
        assert "Soundtrack" not in result["genres"]

    def test_the_album_title_confirms_it(self) -> None:
        vocab = _vocabulary(soundtrack=SOUNDTRACK_SCOPE)
        result = select_album_or_track_genres(
            TRACK_ONLY_SOUNDTRACK, max_genres=4, vocab=vocab,
            album_title="The Original Motion Picture Soundtrack",
        )
        assert "Soundtrack" in result["genres"]

    def test_the_release_type_confirms_it(self) -> None:
        vocab = _vocabulary(soundtrack=SOUNDTRACK_SCOPE)
        result = select_album_or_track_genres(
            TRACK_ONLY_SOUNDTRACK, max_genres=4, vocab=vocab,
            album_title="Greatest Hits", release_types=("Album", "Soundtrack"),
        )
        assert "Soundtrack" in result["genres"]

    def test_a_pattern_that_does_not_match_does_not_confirm(self) -> None:
        vocab = _vocabulary(soundtrack=SOUNDTRACK_SCOPE)
        result = select_album_or_track_genres(
            TRACK_ONLY_SOUNDTRACK, max_genres=4, vocab=vocab,
            album_title="Sounds of the Studio", track_title="Track One",
            release_types=("Album", "Live"),
        )
        assert "Soundtrack" not in result["genres"]


class TestLevels:
    def test_artist_evidence_cannot_create_a_track_only_genre(self) -> None:
        scope = GenreScope(levels=("track", "album"))
        assert reject_reason(
            scope, entities={"artist"}, source_groups=9,
        ) == REASON_LEVEL_NOT_ALLOWED

    def test_a_track_tag_is_allowed(self) -> None:
        scope = GenreScope(levels=("track", "album"))
        assert reject_reason(scope, entities={"track"}, source_groups=1) == ""

    def test_empty_levels_means_the_policy_default(self) -> None:
        assert evidence_levels_allowed(GenreScope()) == {"track", "album", "artist"}


class TestArtistMinSources:
    def test_lone_artist_evidence_is_disabled(self) -> None:
        scope = GenreScope(levels=("track", "album", "artist"),
                            artist_requires_min_sources=2)
        assert reject_reason(
            scope, entities={"artist"}, source_groups=1,
        ) == REASON_ARTIST_MIN_SOURCES

    def test_enough_groups_satisfy_it(self) -> None:
        scope = GenreScope(levels=("track", "album", "artist"),
                            artist_requires_min_sources=2)
        assert reject_reason(scope, entities={"artist"}, source_groups=2) == ""

    def test_track_evidence_is_not_gated(self) -> None:
        """The requirement is about ARTIST evidence; a track tag needs no help."""
        scope = GenreScope(levels=("track", "album", "artist"),
                            artist_requires_min_sources=3)
        assert reject_reason(scope, entities={"track"}, source_groups=1) == ""


class TestMarkers:
    def test_a_marker_does_not_consume_a_slot(self) -> None:
        """``max_genres=3`` must still yield THREE musical genres."""
        vocab = _vocabulary(christmas=CHRISTMAS_SCOPE)
        result = select_album_or_track_genres(
            {
                "lastfm@track": ["Nu Metal", "Alternative Metal",
                                 "Industrial Metal", "Grunge", "Christmas"],
                "musicbrainz@track": ["Nu Metal", "Alternative Metal",
                                      "Industrial Metal", "Grunge", "Christmas"],
            },
            max_genres=3, vocab=vocab, album_title="Holiday",
        )
        musical = [g for g in result["genres"] if g != "Christmas"]
        assert len(musical) == 3, result["genres"]
        assert "Christmas" in result["genres"], "the marker must survive"
        assert "Christmas" in (result.get("markers") or [])

    def test_a_marker_is_not_dropped_as_a_lone_family(self) -> None:
        """One Christmas tag beside a large metal cluster is a marker, not noise."""
        vocab = _vocabulary(christmas=CHRISTMAS_SCOPE)
        result = select_album_or_track_genres(
            {
                "lastfm@track": ["Nu Metal", "Alternative Metal",
                                 "Industrial Metal", "Christmas"],
                "musicbrainz@track": ["Nu Metal", "Alternative Metal",
                                      "Industrial Metal", "Christmas"],
            },
            max_genres=3, vocab=vocab, album_title="Holiday",
        )
        assert "Christmas" in result["genres"]

    def test_split_markers_is_a_no_op_without_declared_markers(self) -> None:
        vocab = _vocabulary()
        musical, markers = split_markers(["Rock", "Nu Metal"], vocab)
        assert musical == ["Rock", "Nu Metal"]
        assert markers == []


class TestValidation:
    def test_an_unknown_level_is_reported(self) -> None:
        errors = validate_vocabulary({
            "genres": [{"id": "x", "display": "X",
                        "scope": {"levels": ["song"]}}],
        })
        assert any("scope.levels" in e for e in errors)

    def test_an_unknown_slot_is_reported(self) -> None:
        errors = validate_vocabulary({
            "genres": [{"id": "x", "display": "X", "scope": {"slot": "badge"}}],
        })
        assert any("scope.slot" in e for e in errors)

    def test_a_bad_regex_is_reported(self) -> None:
        """An unrunnable pattern never matches, so the genre is never confirmed."""
        errors = validate_vocabulary({
            "genres": [{"id": "x", "display": "X",
                        "scope": {"confirm_title_patterns": ["(unclosed"]}}],
        })
        assert any("not a valid regex" in e for e in errors)
