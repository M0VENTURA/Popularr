"""A track number may only change when the TRACK NAME confirms the pairing.

REPORTED
--------

> When matching a release after using lookup mbid, can it also use the track
> name as a confirmation for track when matching the tracks? Sometimes it's
> falsely changing track numbers when the track name is different.

WHY IT HAPPENED
---------------

Steps 1 and 3 of ``_match_mb_tracks_to_library`` pair on the **name** — step 3
from a fuzzy similarity of only ``_TRACKLIST_TITLE_FLOOR`` (0.55). A name-driven
pairing legitimately sits at a different position (the library rip is ordered
differently, or the two titles are only *similar*), and ``diff_fields`` then
reported ``track_number`` alongside ``title``. The number rode along with the
title change unexamined: accepting rewrote a correct position for a pairing the
very same review reports as a **different name**.

Probed with the shipped matcher, three shapes produced a renumber while
``titles_match_for_review`` said the names differ:

    #4 -> #7   'Old Title'                 vs 'New Title'
    #4 -> #6   'Losing My Religon'         vs 'Losing My Religion'
    #2 -> #4   'Sunshine'                  vs 'Sunshine of Your Love'

THE RULE
--------

**A track number is only evidence about the SAME track, so the name must
confirm the pairing first.** The confirmation is ``titles_match_for_review`` —
the review's OWN title rule, already shared by the Lookup-MBID preview and the
Compare button — so "the names differ" and "don't renumber" can never
disagree. It is self-healing rather than a permanent block: accept the title
first and the next compare, now seeing matching names, proposes the number.

THREE SURFACES, ONE RULE
------------------------

1. ``musicbrainz_service._match_mb_tracks_to_library`` → ``diff_fields``. This
   is what renders the album page's per-row suggestions (``album.js`` walks
   ``trackComp.diff_fields``) and what ``align_album_tracklist`` writes.
2. ``metadata_proposal_service._track_proposals`` → the scan-time review. It
   deliberately does NOT read ``diff_fields`` — it joins ``local`` against the
   comparison entry — so gating only (1) would leave the review proposing a
   renumber the Compare button no longer shows.
3. ``alignTracklist()`` in BOTH trees (test_site ``pages/album.js`` and live
   ``album_detail.js``). It re-derived "needs a number" from the raw
   ``library_track_number !== mb_track_number`` and so ignored (1) entirely —
   and also walked straight past a per-row **Ignore**, which is exactly what
   ``diff_fields`` already encodes.

Nothing here makes matching stricter: the pairings are unchanged, and the
title change a user may want is still offered. Only the number waits for the
name.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.enrichment import musicbrainz_service as _mbs  # noqa: E402
from services.metadata.metadata_proposal_service import (  # noqa: E402
    _track_proposals,
)

_TRACKLIST_TITLE_FLOOR = getattr(_mbs, "_TRACKLIST_TITLE_FLOOR", 0.55)


def mb(title, number, duration_ms=None, disc=1, rec="rec-1"):
    return {
        "mb_title": title,
        "mb_track_number": number,
        "mb_disc_number": disc,
        "mb_duration": duration_ms,
        "mb_recording_mbid": rec,
    }


def library(title, number, duration=None, tid="t1", ignored=None):
    return {
        "id": tid,
        "title": title,
        "track_number": number,
        "disc_number": 1,
        "mbid": "",
        "duration": duration,
        "mb_ignored_fields": ignored,
        "file_path": f"/music/{tid}.mp3",
        "artist": "Band",
    }


def compare(mb_tracks, library_tracks):
    comparison, _extra = _mbs._match_mb_tracks_to_library(mb_tracks, library_tracks)
    return comparison


def diffs_of(entry):
    return set(entry.get("diff_fields") or [])


# ===========================================================================
# 1. The matcher no longer renumbers without the name
# ===========================================================================


class TestAFuzzyPairDoesNotRenumber:
    """The three shapes the probe found, each pairing across positions."""

    def test_a_renamed_track_at_a_different_position_is_not_renumbered(self):
        entry = compare(
            [mb("New Title", 7, 180000)],
            [library("Old Title", 4, 180)],
        )[0]

        assert entry["matched"] is True, "pairing itself must not get stricter"
        diffs = diffs_of(entry)
        assert "title" in diffs, "the rename is still offered for review"
        assert "track_number" not in diffs, (
            "a track number was changed on a pairing the review itself calls a "
            "different name"
        )

    def test_a_typo_pair_at_a_different_position_is_not_renumbered(self):
        entry = compare(
            [mb("Losing My Religion", 6, 250000)],
            [library("Losing My Religon", 4, 250)],
        )[0]

        assert entry["matched"] is True
        assert "track_number" not in diffs_of(entry)

    def test_a_weak_fuzzy_pair_at_a_different_position_is_not_renumbered(self):
        """0.55 is the floor — 'Sunshine' clears it against a different song."""
        entry = compare(
            [mb("Sunshine of Your Love", 4, 250000)],
            [library("Sunshine", 2, 250)],
        )[0]

        assert entry["matched"] is True
        diffs = diffs_of(entry)
        assert "track_number" not in diffs, (
            "a shared word is not enough to move a track"
        )
        assert "title" in diffs


class TestTheConfirmedRenumberStillWorks:
    """CONTROLS — the rule must remove a false proposal, not every proposal."""

    def test_an_exact_name_at_a_different_position_still_renumbers(self):
        """The library rip numbers this file 12; the release has it at 14."""
        entry = compare(
            [mb("Heaven Can Wait", 14, 300000)],
            [library("Heaven Can Wait (live)", "12", 300)],
        )[0]

        assert entry["matched"] is True
        diffs = diffs_of(entry)
        assert "track_number" in diffs, (
            "a name-confirmed renumber is exactly what Align exists for"
        )
        assert "title" not in diffs, "(live) is a performance marker"

    def test_a_performance_marker_does_not_block_the_renumber(self):
        entry = compare(
            [mb("Song", 9, 180000)],
            [library("Song (Acoustic)", 3, 180)],
        )[0]

        assert "track_number" in diffs_of(entry)
        assert "title" not in diffs_of(entry)

    def test_a_number_that_already_agrees_is_untouched(self):
        entry = compare(
            [mb("Song", 3, 180000)],
            [library("Song", 3, 180)],
        )[0]

        assert "track_number" not in diffs_of(entry)
        assert "title" not in diffs_of(entry)

    def test_an_ignored_track_number_stays_ignored(self):
        """``mb_ignored_fields`` already filtered the field; the rule adds to it.

        The column is TEXT holding a JSON array — a Python list raises inside
        ``json.loads`` and degrades to "nothing ignored", which is why the
        fixture spells it out.
        """
        entry = compare(
            [mb("Song", 9, 180000)],
            [library("Song", 3, 180, ignored='["track_number"]')],
        )[0]

        assert "track_number" not in diffs_of(entry)


# ===========================================================================
# 2. The scan-time review obeys the same rule
# ===========================================================================


def _local_row(**overrides):
    row = {
        "id": "t1",
        "title": "Song",
        "track_number": "3",
        "disc_number": "1",
        "mbid": "",
        "writer": "",
        "musicbrainz_genres": "",
        "artist": "Band",
        "album_artist": "Band",
        "duration": 180,
        "mb_ignored_fields": None,
    }
    row.update(overrides)
    return row


def _metadata(mb_title, rec="rec-1"):
    return {
        "tracks": [{
            "mb_title": mb_title,
            "mb_recording_mbid": rec,
            "mb_disc_number": 1,
            "mb_track_number": 7,
            "musicbrainz_genres": "",
            "writer": "",
            "artist": "Band",
        }],
    }


def _proposal_fields(local, mb_title):
    """Pair *local* with the release through the REAL matcher, then propose."""
    comparison = compare(
        [mb(mb_title, 7, 180000)],
        [{
            "id": local["id"],
            "title": local["title"],
            "track_number": local["track_number"],
            "disc_number": 1,
            "mbid": local["mbid"],
            "duration": local["duration"],
            "mb_ignored_fields": local["mb_ignored_fields"],
        }],
    )
    proposals = _track_proposals([local], comparison, _metadata(mb_title))
    if not proposals:
        return set()
    return {change["field"] for change in proposals[0]["changes"]}


class TestTheScanReviewObeysTheSameRule:
    def test_it_does_not_renumber_when_the_names_differ(self):
        """Gating only the matcher would make the two surfaces disagree."""
        fields = _proposal_fields(_local_row(title="Old Title"), "New Title")

        assert "title" in fields, "the rename is still shown"
        assert "track_number" not in fields, (
            "the scan-time review proposed a renumber the Compare button no "
            "longer shows"
        )

    def test_it_still_renumbers_when_the_names_agree(self):
        fields = _proposal_fields(_local_row(title="Song"), "Song")

        assert "track_number" in fields, (
            "the confirmed renumber must survive in the review too"
        )


# ===========================================================================
# 3. Align consumes the server's verdict instead of re-deriving it
# ===========================================================================


ALBUM_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
LIVE_ALBUM_JS = REPO_ROOT / "static" / "js" / "album_detail.js"


def _align_filter_window(source: str) -> str:
    """The ``needsNumber`` filter — everything between its binding and its use."""
    start = source.index("const needsNumber")
    end = source.index("if (!needsNumber.length)", start)
    return source[start:end]


@pytest.mark.parametrize("path", [ALBUM_JS, LIVE_ALBUM_JS], ids=["test_site", "live"])
class TestAlignReadsTheServerVerdict:
    def test_the_file_exists(self, path):
        assert path.is_file(), f"{path} is missing"

    def test_needs_number_is_decided_by_diff_fields(self, path):
        source = path.read_text(encoding="utf-8")
        window = _align_filter_window(source)

        assert "wantsNumber(c)" in window, (
            "Align re-derives 'needs a number' from the raw values, so it "
            "renumbers rows the server no longer proposes and undoes a "
            "per-row Ignore"
        )
        assert "!== String(c.mb_track_number)" not in window, (
            "the raw numeric comparison must not decide the filter any more"
        )

    def test_the_wants_number_helper_reads_diff_fields(self, path):
        source = path.read_text(encoding="utf-8")
        start = source.index("wantsNumber")
        end = source.index("needsNumber", start)
        definition = source[start:end]

        assert "diff_fields" in definition, (
            "the helper must consult the server's verdict, not the raw numbers"
        )
        assert "'track_number'" in definition

    def test_align_still_reports_honestly_when_nothing_can_be_confirmed(self, path):
        """A zero state that says 'already match' would be a lie here."""
        source = path.read_text(encoding="utf-8")
        after = source[source.index("if (!needsNumber.length)") :]

        assert "No track number can be confirmed by its track name" in after[:1200], (
            "Align declined every row because the names disagree, so it must "
            "not report that the numbers already match"
        )
