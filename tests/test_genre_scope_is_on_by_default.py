"""The v4 scope rules must be in force on a FRESH install.

⭐ Two separate gaps, both fixed:

1. **The code.** Enforcement was already unconditional (no flag), but the scan
   never handed ``aggregate_genres`` the album's release type — so
   ``confirm_release_types`` could not fire during a real SCAN. Only the resync
   passed it. A rule that can never fire is not "on by default".

2. **The data.** Only ``soundtrack`` and ``christmas`` declared a scope in the
   shipped default. A fresh install, or anyone who RESETS the vocabulary from
   the Config page, therefore did NOT get the v4 rules for
   ``film_score`` / ``video_game_music`` / ``musical_theatre`` / ``comedy``.

Measured on a real 1,739-track export, shipping both changes touches 3.4% of
tracks and no track loses a genre it should keep.
"""

from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from services.enrichment import genre_vocabulary as gv
from services.enrichment.genre_scope import release_types
from services.enrichment.genre_vocabulary import validate_vocabulary


class TestTheShippedDefaultDeclaresTheV4Scopes:
    """A reset must not silently disable a rule."""

    #: id -> (slot, require_confirmation)
    EXPECTED = {
        "soundtrack": ("marker", True),
        "christmas": ("marker", False),
        "comedy": ("marker", False),
        "film_score": ("genre", True),
        "video_game_music": ("genre", True),
        "musical_theatre": ("genre", False),
    }

    @pytest.mark.parametrize(("genre_id", "expected"), sorted(EXPECTED.items()))
    def test_the_entry_declares_its_scope(self, genre_id: str, expected: tuple) -> None:
        data = json.loads(
            pathlib.Path(gv.DEFAULT_VOCABULARY_PATH).read_text(encoding="utf-8")
        )
        entry = next(e for e in data["genres"] if e["id"] == genre_id)
        assert "scope" in entry, (
            f"{genre_id} declares no scope in the shipped default, so a fresh "
            "install (and a vocabulary reset) does not get the v4 rule"
        )
        slot, require_confirmation = expected
        assert entry["scope"].get("slot", "genre") == slot
        assert bool(entry["scope"].get("require_confirmation", False)) is require_confirmation

    def test_the_default_still_validates(self) -> None:
        data = json.loads(
            pathlib.Path(gv.DEFAULT_VOCABULARY_PATH).read_text(encoding="utf-8")
        )
        assert validate_vocabulary(data) == []


class TestTheScanPassesTheReleaseType:
    """The rule can only fire if the value reaches the selector."""

    def test_the_scan_call_site_passes_release_types(self) -> None:
        from services.popularity.stages import track_stage

        source = inspect.getsource(track_stage)
        assert "release_types=_release_types_for_scope(" in source, (
            "the scan never passes the release type, so confirm_release_types "
            "cannot fire during a scan"
        )

    @pytest.mark.parametrize(
        ("album_context", "album_result", "expected"),
        [
            ({"musicbrainz_albumtype": "Album + Soundtrack"}, None,
             ("Album", "Soundtrack")),
            ({"detected_album_type": "Soundtrack"}, None, ("Soundtrack",)),
            (None, {"album_type": "Album/Soundtrack"}, ("Album", "Soundtrack")),
            ({"musicbrainz_albumtype": "Album"}, None, ("Album",)),
            ({}, {}, ()),
        ],
    )
    def test_the_helper_reads_every_spelling(
        self, album_context, album_result, expected
    ) -> None:
        from services.popularity.stages.track_stage import _release_types_for_scope

        assert _release_types_for_scope(album_context, album_result) == expected


class TestReleaseTypesSplitsEverySeparator:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Album + Soundtrack", ("Album", "Soundtrack")),
            ("Album/Soundtrack", ("Album", "Soundtrack")),
            ("Album, Soundtrack", ("Album", "Soundtrack")),
            ("Album & Soundtrack", ("Album", "Soundtrack")),
            ("Album and Soundtrack", ("Album", "Soundtrack")),
            ("Soundtrack", ("Soundtrack",)),
            ("", ()),
            (None, ()),
        ],
    )
    def test_splits(self, value, expected) -> None:
        assert release_types(value) == expected

    def test_the_resync_delegates_to_the_shared_splitter(self) -> None:
        """One separator list, not two that can drift apart."""
        from services.enrichment import genre_resync_service as svc

        source = inspect.getsource(svc._row_release_types)
        assert "from services.enrichment.genre_scope import release_types" in source


class TestASoundtrackIsConfirmedByItsReleaseType:
    EVIDENCE = {"lastfm@track": ["Soundtrack"], "musicbrainz@track": ["Soundtrack"]}

    def test_a_plain_album_does_not_get_it(self) -> None:
        from services.enrichment.genre_aggregation_service import aggregate_genres

        assert aggregate_genres(
            self.EVIDENCE, max_genres=4, context_title="T",
            context_album="Greatest Hits", release_types=("Album",),
        ) == []

    def test_the_release_type_confirms_it(self) -> None:
        from services.enrichment.genre_aggregation_service import aggregate_genres

        got = aggregate_genres(
            self.EVIDENCE, max_genres=4, context_title="T",
            context_album="Greatest Hits", release_types=("Album", "Soundtrack"),
        )
        assert "Soundtrack" in got

    def test_film_score_needs_the_same_confirmation(self) -> None:
        """It declares require_confirmation in the shipped default now."""
        from services.enrichment.genre_aggregation_service import aggregate_genres

        evidence = {"lastfm@track": ["Film Score"], "musicbrainz@track": ["Score"]}
        assert aggregate_genres(evidence, max_genres=4, context_title="T",
                                 context_album="Greatest Hits") == []
        assert "Film Score" in aggregate_genres(
            evidence, max_genres=4, context_title="T",
            context_album="Greatest Hits", release_types=("Soundtrack",),
        )
