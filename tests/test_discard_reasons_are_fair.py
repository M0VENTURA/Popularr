"""Discard reasons must not reject files that are actually correct.

Reported from the discard list (25 entries, all "file did not match the queue
item"). Several were the queue matcher rejecting a file for reasons that were
either a normalisation bug or a message that could not be verified.

1. `(Dust)` and `(I Died)` — SIX of the 25.
   Both queue item and file carry the same title, yet each was discarded as
   "Different song". `clean_title(remove_brackets=True)` deleted the WHOLE
   title, so `normalize_core_title("(Dust)") == ""`, and
   `_simple_similarity("", "")` is 0.0 — a file could not match ITSELF.

2. `KoЯn` — "Wrong artist".
   `normalize_string` lowercases and strips diacritics, but `Я` is a LETTER, not
   an accent, so "KoЯn" normalised to "koяn" against "korn" and scored 0.0.

3. "Wrong length — the file is 4:03 but the queue item is 3:58".
   3:58 vs 4:03 is 5s, exactly the default tolerance, and m:ss rounding hides
   the real gap. The sentence now states the delta and the tolerance.

4. "Different edition — the file's edition annotation does not match".
   Named no annotation, so the verdict could not be checked. It now names both.
"""

from __future__ import annotations

import pytest

from helpers.normalization_service import (
    edition_annotations_compatible,
    extract_edition_annotation,
    fold_confusables,
    normalize_core_title,
    normalize_string,
)
from services.queue.queue_metadata_matcher import _simple_similarity


# ---------------------------------------------------------------------------
# 1. A title that is entirely parenthesised
# ---------------------------------------------------------------------------

class TestBracketedTitlesKeepTheirTitle:
    @pytest.mark.parametrize(
        "title, expected",
        [
            ("(Dust)", "dust"),
            ("(I Died)", "i died"),
            ("(sic)", "sic"),
        ],
    )
    def test_the_bracketed_text_becomes_the_title(self, title, expected):
        assert normalize_core_title(title) == expected

    @pytest.mark.parametrize("title", ["(Dust)", "(I Died)"])
    def test_a_file_can_match_itself(self, title):
        """The regression: 0.0 against itself, so every copy was discarded."""
        core = normalize_core_title(title)
        assert core, f"{title!r} normalised to nothing"
        assert _simple_similarity(core, core) == 1.0

    def test_a_normal_parenthetical_still_strips(self):
        """CONTROL: the fix must not stop ordinary annotations being removed.

        "(We Are) The Road Crew" keeps its leading parenthetical stripped, and
        "Song (Remastered 2011)" must still collapse onto "Song".
        """
        assert normalize_core_title("(We Are) The Road Crew") == "the road crew"
        assert normalize_core_title("Song (Remastered 2011)") == "song"

    def test_a_title_with_real_brackets_and_text_around_them(self):
        assert normalize_core_title("Song (Live)") == "song"


# ---------------------------------------------------------------------------
# 2. Latin look-alike letters
# ---------------------------------------------------------------------------

class TestConfusableLettersFold:
    @pytest.mark.parametrize(
        "stylised, plain",
        [
            ("KoЯn", "Korn"),
            ("Koяn", "Korn"),
        ],
    )
    def test_the_artist_reaches_the_same_key(self, stylised, plain):
        a, b = normalize_string(stylised), normalize_string(plain)
        assert a == b
        assert _simple_similarity(a, b) == 1.0

    @pytest.mark.parametrize("name", ["Кино", "Σαββόπουλος", "Мельница"])
    def test_real_cyrillic_and_greek_are_left_alone(self, name):
        """⚠️ THE DANGEROUS DIRECTION.

        Folding a genuinely non-Latin name would collide it with unrelated Latin
        names, which is worse than a missed match. Only strings that already
        contain Latin letters are folded.
        """
        assert fold_confusables(name) == name

    def test_diacritics_still_fold_as_before(self):
        assert normalize_string("Motörhead") == normalize_string("Motorhead")


# ---------------------------------------------------------------------------
# 3 & 4. The discard sentences carry the evidence
# ---------------------------------------------------------------------------

def _sentence(**detail) -> str:
    from services.downloads.download_completion_service import (
        _discard_detail_sentence,
    )

    return _discard_detail_sentence(detail, "metadata")


class TestTheDiscardSentencesAreVerifiable:
    def test_wrong_length_states_the_delta_and_the_tolerance(self):
        """The reported "4:03 vs 3:58" — 5s apart, exactly the default limit."""
        sentence = _sentence(
            reason="wrong_length",
            found_length_seconds=242.6,
            expected_length_seconds=238.4,
            length_difference_seconds=4.2,
            length_tolerance_seconds=5.0,
        )
        assert "4:03" in sentence and "3:58" in sentence
        assert "4.2s apart" in sentence, "the size of the gap must be shown"
        assert "tolerance 5s" in sentence, "the verdict must be auditable"

    def test_wrong_length_without_a_recorded_delta_still_reports_both(self):
        sentence = _sentence(
            reason="wrong_length",
            found_length_seconds=242.0,
            expected_length_seconds=120.0,
        )
        assert "Wrong length" in sentence
        assert "4:02" in sentence and "2:00" in sentence

    def test_edition_mismatch_names_both_annotations(self):
        sentence = _sentence(
            reason="edition_mismatch",
            file_edition_annotation="deluxe edition",
            queue_edition_annotation="",
        )
        assert "deluxe edition" in sentence, (
            "the message must name the annotation it rejected, or the user "
            "cannot tell a real mismatch from a false positive"
        )

    def test_edition_mismatch_with_both_named(self):
        sentence = _sentence(
            reason="edition_mismatch",
            file_edition_annotation="japanese ver",
            queue_edition_annotation="kr ver",
        )
        assert "japanese ver" in sentence and "kr ver" in sentence

    def test_edition_mismatch_falls_back_when_nothing_is_recorded(self):
        """CONTROL: the old generic wording is still the last resort."""
        sentence = _sentence(reason="edition_mismatch")
        assert sentence.startswith("Different edition")


class TestTheMatcherRecordsWhyItRejectedTheEdition:
    def test_an_edition_reject_carries_both_annotations(self, tmp_path, monkeypatch):
        """The sentence can only name them if the matcher recorded them."""
        from services.queue import queue_metadata_matcher as qmm

        # An audio file whose TITLE carries a conflicting annotation.
        import json

        monkeypatch.setattr(
            qmm,
            "read_mp3_metadata",
            lambda _p: {
                "title": "Mama (Deluxe Edition)",
                "artist": "Spice Girls",
                "album_artist": "Spice Girls",
                "duration_ms": 240_000,
            },
        )

        detail: dict = {}
        verdict = qmm._metadata_matches_queue_item(
            str(tmp_path / "f.mp3"),
            {
                "artist": "Spice Girls",
                "album_artist": "Spice Girls",
                "title": "Mama",
                "duration": 240,
            },
            detail=detail,
        )

        assert verdict is False
        assert detail["reason"] == "edition_mismatch"
        assert detail["file_edition_annotation"] == "deluxe edition"
        assert detail["queue_edition_annotation"] == ""


# ---------------------------------------------------------------------------
# Edition detection remains sound
# ---------------------------------------------------------------------------

class TestEditionDetectionIsUnchanged:
    def test_plain_titles_have_no_annotation(self):
        for title in ("Mama", "Architecture of Aggression", "The Real"):
            assert extract_edition_annotation(title) is None
            assert edition_annotations_compatible(title, title)

    def test_a_dash_inside_a_filename_is_not_an_annotation(self):
        """"Artist - Album - 03 - Title" must not look like an edition."""
        for name in (
            "Megadeth - Countdown to Extinction - 03 - Architecture of Aggression",
            "Spice Girls - Greatest Hits - 04 - Mama",
            "Song - Part 2",
        ):
            assert extract_edition_annotation(name) is None
