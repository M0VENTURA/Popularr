"""Final genre strings are canonically spelled.

REPORTED (collection popularity report review, 65,135 tracks)
------------------------------------------------------------
* 883 distinct genre strings — too many for a controlled vocabulary.
* The duplication was capitalization and separators, not classification:
  ``Rock``/``rock``, ``Alternative Rock``/``alternative rock``,
  ``Pop``/``pop``, ``Drum And Bass``/``Drum and Bass``/``drum and bass``,
  ``Post Rock``/``post rock``, ``Trip-Hop``/``trip-hop``.

THE TWO CAUSES
--------------
1. ``_resolve_display_name`` picked the highest-weighted raw SPELLING a source
   sent, so ``Rock`` and ``rock`` were both "correct".
2. ``clean_conflicting_genres`` then returned its lowercase, separator-stripped
   COMPARISON key as the VALUE — so even a correctly resolved "Death Metal"
   left as "death metal". That alone guarantees the split.

The fix asks the vocabulary for its one display spelling
(``rock`` -> ``Rock``, ``drum_and_bass`` -> ``Drum and Bass``,
``post_rock`` -> ``Post-Rock``). Lookup is separator- AND case-insensitive
because the vote key is squashed (``deathmetal``) while the vocabulary id keeps
its separators (``death_metal``).

⚠️ A genre the vocabulary does NOT know keeps its own spelling. Renaming it
would be inventing a vocabulary entry, and dropping it would be data loss.
"""

from __future__ import annotations

import re

import pytest

from services.enrichment.genre_aggregation_service import (
    _canonical_display_name,
    _squash,
    clean_conflicting_genres,
)


# ---------------------------------------------------------------------------
# The reported duplicate pairs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variants, expected",
    [
        (["Rock", "rock"], "Rock"),
        (["Alternative Rock", "alternative rock"], "Alternative Rock"),
        (["Pop", "pop"], "Pop"),
        (["Drum And Bass", "Drum and Bass", "drum and bass"], "Drum and Bass"),
        (["Post Rock", "post rock"], "Post-Rock"),
        (["Trip-Hop", "trip-hop"], "Trip Hop"),
    ],
)
def test_the_reported_variants_collapse_to_one_string(variants, expected):
    out = clean_conflicting_genres(variants)
    assert out == [expected], (
        f"{variants} resolved to {out} — the report's duplicate pairs must "
        "produce exactly ONE canonical string"
    )


# ---------------------------------------------------------------------------
# The lookup itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "deathmetal",    # separator-stripped vote key
        "death_metal",   # vocabulary id
        "Death Metal",   # display name
        "DEATH METAL",   # shouted
    ],
)
def test_every_form_of_one_genre_finds_the_same_display(key):
    """The vote key, the id and the display name must all answer alike."""
    assert _canonical_display_name(key) == "Death Metal", (
        f"{key!r} did not resolve to the vocabulary's display name"
    )


def test_an_unknown_genre_is_left_alone():
    """CONTROL: renaming it would invent a vocabulary entry; dropping it would
    be data loss."""
    assert _canonical_display_name("Zydeco Speedcore") is None
    assert clean_conflicting_genres(["Zydeco Speedcore"]) == ["Zydeco Speedcore"]


def test_the_lookup_is_cached_but_cache_misses_too():
    """An unknown genre is scanned ONCE; a miss must not re-scan every call."""
    from services.enrichment.genre_aggregation_service import (
        _CANONICAL_DISPLAY_CACHE,
    )

    _canonical_display_name("Definitely Not A Genre 12345")
    assert _squash("Definitely Not A Genre 12345") in _CANONICAL_DISPLAY_CACHE


def test_no_two_vocabulary_ids_squash_together():
    """⚠️ The lookup squashes separators on BOTH sides, so two ids that differ
    only by punctuation would silently resolve to whichever is found first.
    At 319 ids there are no collisions; this fails if the vocabulary grows one.
    """
    from services.enrichment.genre_vocabulary import get_vocabulary

    seen: dict[str, list[str]] = {}
    for gid in get_vocabulary().genres:
        seen.setdefault(_squash(gid), []).append(gid)
    collisions = {k: v for k, v in seen.items() if len(v) > 1}
    assert not collisions, (
        f"vocabulary ids collide after separator-squashing: {collisions} — the "
        "canonical lookup can no longer tell them apart"
    )


# ---------------------------------------------------------------------------
# Behaviour through the public entry point
# ---------------------------------------------------------------------------


class TestThroughAggregateGenres:
    """The end-to-end path, on the legacy engine and the canonical one."""

    def test_the_legacy_engine_now_writes_a_canonical_spelling(self, monkeypatch):
        """⚠️ THE REGRESSION THIS FILE EXISTS FOR.

        The legacy engine returned its lowercase comparison key, so
        ``['death metal','rock']`` came out as ``['deathmetal']`` — not merely
        a casing difference, but a genre name with the space removed.
        """
        from services.enrichment import genre_aggregation_service as gas

        monkeypatch.setattr(gas, "_selection_engine_v2_enabled", lambda: False)
        out = gas.aggregate_genres(
            {"lastfm": ["death metal", "rock"], "musicbrainz": ["death metal"]},
            max_genres=3,
        )
        assert out == ["Death Metal"], out

    def test_the_canonical_engine_still_writes_its_display_name(self, monkeypatch):
        from services.enrichment import genre_aggregation_service as gas

        monkeypatch.setattr(gas, "_selection_engine_v2_enabled", lambda: True)
        out = gas.aggregate_genres(
            {"lastfm": ["death metal"], "musicbrainz": ["death metal"]},
            max_genres=3,
        )
        assert out == ["Death Metal"], out

    def test_both_engines_agree_on_the_spelling(self, monkeypatch):
        """CONTROL: the whole point is that the two paths write the same string,
        so a rebuild does not introduce a second spelling of an existing genre."""
        from services.enrichment import genre_aggregation_service as gas

        source_map = {"lastfm": ["death metal"], "musicbrainz": ["death metal"]}

        monkeypatch.setattr(gas, "_selection_engine_v2_enabled", lambda: True)
        v2 = gas.aggregate_genres(dict(source_map), max_genres=3)
        monkeypatch.setattr(gas, "_selection_engine_v2_enabled", lambda: False)
        legacy = gas.aggregate_genres(dict(source_map), max_genres=3)

        assert v2 == legacy, f"engines disagree: v2={v2} legacy={legacy}"
