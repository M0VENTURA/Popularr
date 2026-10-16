"""A track must not be paired with a DIFFERENT song that merely starts with its title.

Reported: *"Something keeps trying to move tracks where the file name is nothing
like the song it's trying to move it to."*

The screenshot: a compilation row whose file is ``19. Various Artists - The
Look.mp3`` (3:57) matched to the MusicBrainz recording **"The Power of Love -
Love Shack"** (4:54) — a medley. The review then offered the title change, and
the rename moves the file to a name derived from it.

ROOT CAUSE — the same ``token_set_ratio`` trap this codebase has hit before.

``_similarity`` is ``popularity_math.fuzzy_match_score``, which is RapidFuzz's
``token_set_ratio``: a metric that returns **1.0 whenever one token set is a
strict SUBSET of the other**, by design, because "all the important words are
present" is exactly what it measures. So

    "the power of love"  vs  "the power of love love shack"   ->  1.0

``_titles_agree_for_pairing`` fell back to ``_similarity >= 0.55``, saw 1.0 and
reported the names as AGREEING — which made ``_track_number_pairing_allowed``
return True, which meant **the length check never ran**. The 57-second
difference that exists precisely to catch a different recording was never
consulted, and the number step paired the two rows.

Fixed by ``_title_adds_a_second_song``: a title that is the other's words PLUS a
word naming something else is a different song; one that adds only markers and
credits is the same song spelled differently. Erring toward "different" is the
safe direction here — an unmatched MusicBrainz entry comes back
``matched=False`` and the row is left alone, whereas a wrong pairing writes a
stranger's recording MBID and title into the file's tags and then renames the
file to match.
"""

from __future__ import annotations

import pytest

from services.enrichment.musicbrainz_service import (
    _match_mb_tracks_to_library,
    _similarity,
    _title_adds_a_second_song,
    _titles_agree_for_pairing,
    _track_number_pairing_allowed,
)
from helpers.normalization_service import normalize_title_for_lookup

LOCAL = "The Power of Love"
MASHUP = "The Power of Love - Love Shack"


def _library_row(**overrides) -> dict:
    row = {
        "id": "lib-1",
        "title": LOCAL,
        "track_number": "13",
        "disc_number": "1",
        "duration": 237,
        "mbid": "",
        "mb_ignored_fields": "[]",
    }
    row.update(overrides)
    return row


def _mb_track(**overrides) -> dict:
    track = {
        "mb_disc_number": 1,
        "mb_track_number": "13",
        "mb_title": LOCAL,
        "mb_recording_mbid": "rec-original",
        "mb_duration": 234000,
        "artist": "Huey Lewis and the News",
    }
    track.update(overrides)
    return track


# ---------------------------------------------------------------------------
# The reported defect
# ---------------------------------------------------------------------------


class TestTheReportedPair:
    def test_the_metric_scores_the_mashup_a_perfect_match(self):
        """⭐ WHY THE OLD GATE COULD NOT FIRE.

        Not a threshold problem: ``token_set_ratio`` returns exactly 1.0 for a
        subset, so no floor at or below 1.0 can reject this pair. The guard was
        structurally incapable of catching the case it existed for.
        """
        assert (
            _similarity(
                normalize_title_for_lookup(LOCAL),
                normalize_title_for_lookup(MASHUP),
            )
            == 1.0
        )

    def test_the_titles_do_not_agree(self):
        assert _titles_agree_for_pairing(LOCAL, MASHUP) is False

    def test_the_number_step_is_vetoed_so_the_length_check_runs(self):
        """⭐ The veto that matters.

        The number is only ever a tie-breaker, so with the names disagreeing and
        the lengths 57s apart the pairing must be refused — not waved through by
        a title gate that called them the same song.
        """
        assert _track_number_pairing_allowed(LOCAL, MASHUP, 237, 294000) is False


class TestTheMatcherNoLongerPairsIt:
    def test_the_mashup_is_left_unmatched(self):
        """⭐ The symptom, end to end.

        Printed as ``matched=True`` with the mashup's MBID before the fix; the
        review then proposed the title change the user is seeing.
        """
        comparison, _extra = _match_mb_tracks_to_library(
            [_mb_track(), _mb_track(mb_track_number="14", mb_title=MASHUP,
                                    mb_recording_mbid="rec-mashup", mb_duration=294000)],
            [_library_row()],
        )
        by_title = {entry["mb_title"]: entry for entry in comparison}
        assert by_title[LOCAL]["matched"] is True
        assert by_title[MASHUP]["matched"] is False, (
            "the medley was paired to a row named after only one of its two songs"
        )

    def test_the_row_is_left_unclaimed_rather_than_handed_to_a_stranger(self):
        """The file's own name — "The Look" — against the medley.

        This is the reported shape: the row is named after ONE song and the
        MusicBrainz entry is a different one. The number matches and the titles
        are not a subset of each other, so the number step's name/length veto is
        what refuses it (57s apart), and the fuzzy arm scores it far too low to
        claim it. The row must come back UNMATCHED and reported as extra, never
        paired — a wrong pairing writes a stranger's recording MBID into the
        file's tags and then renames the file to match.
        """
        comparison, extra = _match_mb_tracks_to_library(
            [_mb_track(mb_title=MASHUP, mb_recording_mbid="rec-mashup", mb_duration=294000)],
            [_library_row(title="The Look", track_number="13", duration=237)],
        )
        assert comparison[0]["matched"] is False
        assert [row["library_title"] for row in extra] == ["The Look"], (
            "the unmatched row must be surfaced, not silently dropped"
        )

    def test_the_fuzzy_arm_cannot_pair_it_either(self):
        """Step 3 has NO length check, so it needs the veto independently.

        Here the mashup sits at a different number, so the number step cannot
        fire and only the fuzzy arm is left — which scored the pair 1.0.
        """
        comparison, _extra = _match_mb_tracks_to_library(
            [_mb_track(mb_track_number="7", mb_title=MASHUP, mb_recording_mbid="rec-mashup",
                       mb_duration=294000)],
            [_library_row(track_number="13")],
        )
        assert comparison[0]["matched"] is False


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


class TestTitleAddsASecondSong:
    @pytest.mark.parametrize(
        "base, extended",
        [
            (LOCAL, MASHUP),
            ("Hold the Line", "Hold the Line - Rosanna"),
            ("Song", "Song Part 2"),
            ("Song", "Song of the Damned"),
            ("Rebel Yell", "Rebel Yell Reprise"),
        ],
    )
    def test_a_new_word_means_a_different_song(self, base, extended):
        assert _title_adds_a_second_song(
            normalize_title_for_lookup(base), normalize_title_for_lookup(extended)
        )

    @pytest.mark.parametrize(
        "base, extended",
        [
            ("Song", "Song - Live"),                 # unbracketed marker
            ("Song", "Song - Remastered 2011"),
            ("Song", "Song feat. Guest"),
            ("Song", "Song featuring Someone and Other"),
            ("Song", "Song ft. Guest"),
        ],
    )
    def test_markers_and_credits_are_the_same_song(self, base, extended):
        assert not _title_adds_a_second_song(
            normalize_title_for_lookup(base), normalize_title_for_lookup(extended)
        )

    def test_unrelated_titles_are_not_a_superset(self):
        """The fuzzy arm's own territory — a typo must not be mislabelled."""
        assert not _title_adds_a_second_song(
            normalize_title_for_lookup("Hoppipola"),
            normalize_title_for_lookup("Hoppipolla"),
        )

    def test_an_empty_side_cannot_disagree(self):
        assert not _title_adds_a_second_song("", "song")
        assert not _title_adds_a_second_song("song", "")


# ---------------------------------------------------------------------------
# CONTROLS — the pairings that must keep working
# ---------------------------------------------------------------------------


class TestLegitimatePairingsStillWork:
    @pytest.mark.parametrize(
        "library, mb",
        [
            (LOCAL, LOCAL),
            # The dArtagnan version-marker case the alias machinery exists for:
            # the local title lost its "(Unplugged Version)" marker.
            ("Farewell (feat. Patty Gurdy)", "Farewell [Unplugged Version]"),
            ("Song", "Song - Live"),
            ("Song", "Song (Live)"),
            ("Song", "Song feat. Guest"),
            ("Hoppipola", "Hoppipolla"),
            ("Für immer Dein", "Für immer Dein (Unplugged Version)"),
        ],
    )
    def test_the_names_still_agree(self, library, mb):
        assert _titles_agree_for_pairing(library, mb) is True

    def test_a_marker_difference_still_pairs_through_the_matcher(self):
        comparison, _extra = _match_mb_tracks_to_library(
            [_mb_track(mb_title="Farewell [Unplugged Version]", mb_recording_mbid="rec-abu")],
            [_library_row(title="Farewell (feat. Patty Gurdy)")],
        )
        assert comparison[0]["matched"] is True
        assert comparison[0]["mb_recording_mbid"] == "rec-abu"

    def test_a_typo_still_pairs_through_the_matcher(self):
        comparison, _extra = _match_mb_tracks_to_library(
            [_mb_track(mb_title="Hoppipolla", mb_recording_mbid="rec-hp")],
            [_library_row(title="Hoppipola")],
        )
        assert comparison[0]["matched"] is True

    def test_an_identical_title_still_pairs(self):
        comparison, _extra = _match_mb_tracks_to_library([_mb_track()], [_library_row()])
        assert comparison[0]["matched"] is True

    def test_a_different_number_still_pairs_on_an_identical_name(self):
        """Step 1 asks the NAME first, so a mis-numbered file is not abandoned."""
        comparison, _extra = _match_mb_tracks_to_library(
            [_mb_track(mb_track_number="3")], [_library_row(track_number="13")]
        )
        assert comparison[0]["matched"] is True
