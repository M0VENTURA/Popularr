"""``noise_values`` must be honoured, whole values included.

⭐ The block was parsed by nobody: ``Vocabulary`` had no ``noise_values`` field,
so a hand-written list had no effect and only the junk HEURISTIC applied. Four of
the user's 38 entries still reached the final genres, and every one of them
contains a separator:

    "alternative & punk"      -> Alternative Rock + Punk
    "folk, world, & country"  -> Country
    "& country"              -> Country
    "folk, world"            -> Folk + World

That is why the check runs on the WHOLE raw value before tokenisation: by the
time "alternative & punk" has become ["alternative", "punk"] the phrase that was
listed is gone, so a per-token check can never match it.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from services.enrichment import genre_vocabulary as gv
from services.enrichment.genre_selection import select_album_or_track_genres

#: Exactly the values the reported vocabulary lists that survived.
SEPARATOR_NOISE = ["alternative & punk", "folk, world, & country", "& country", "folk, world"]


def _vocabulary(noise: list[str]) -> gv.Vocabulary:
    data = json.loads(
        pathlib.Path(gv.DEFAULT_VOCABULARY_PATH).read_text(encoding="utf-8")
    )
    data["noise_values"] = {"values": noise}
    return gv._build(data, path="<test>", is_override=False, errors=())


class TestTheBlockIsParsed:
    def test_noise_values_reach_the_vocabulary(self) -> None:
        assert _vocabulary(SEPARATOR_NOISE).noise_values, (
            "the block is discarded on load if this is empty"
        )

    def test_matching_is_on_the_normalised_token(self) -> None:
        """An en dash in the list must match a hyphen from a provider."""
        vocab = _vocabulary(["1–4 wochen"])
        assert vocab.is_noise_value("1-4 wochen")

    def test_a_substring_is_not_a_match(self) -> None:
        """Listing "folk, world" must not drop plain "folk"."""
        vocab = _vocabulary(["folk, world"])
        assert not vocab.is_noise_value("folk")
        assert vocab.is_noise_value("folk, world")


class TestTheReportedValuesAreDropped:
    @pytest.mark.parametrize("value", SEPARATOR_NOISE)
    def test_a_listed_value_never_reaches_the_final_genres(self, value: str) -> None:
        vocab = _vocabulary(SEPARATOR_NOISE)
        result = select_album_or_track_genres(
            {"lastfm@track": [value], "musicbrainz@track": [value]},
            max_genres=4, vocab=vocab,
        )
        assert result["genres"] == [], f"{value!r} still produced {result['genres']}"

    def test_an_unlisted_value_still_works(self) -> None:
        """The filter must not be a blanket block on the same sources."""
        vocab = _vocabulary(SEPARATOR_NOISE)
        result = select_album_or_track_genres(
            {"lastfm@track": ["Rock"], "musicbrainz@track": ["Rock"]},
            max_genres=4, vocab=vocab,
        )
        assert "Rock" in result["genres"]

    def test_it_is_reported_as_noise_not_as_unknown(self) -> None:
        vocab = _vocabulary(SEPARATOR_NOISE)
        result = select_album_or_track_genres(
            {"lastfm@track": ["alternative & punk"]}, max_genres=4, vocab=vocab,
        )
        assert result["moved"].get("noise"), (
            "the reason must say why it was dropped, not leave it unexplained"
        )


class TestItAlsoWorksPerToken:
    def test_a_delimited_list_still_drops_the_listed_token(self) -> None:
        """`"Rock; vocal"` splits into tokens that were never whole values."""
        vocab = _vocabulary(["vocal"])
        result = select_album_or_track_genres(
            {"lastfm@track": ["Rock; vocal"], "musicbrainz@track": ["Rock"]},
            max_genres=4, vocab=vocab,
        )
        assert not any("vocal" in g.casefold() for g in result["genres"])
