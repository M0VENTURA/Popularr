"""Metadata comparison: genre side, and version/cover markers in titles.

REPORTED:
  1. "When comparing needed changes, Genres picked up when doing a lookup will
     only compare the musicbrains genre table, but the current genres attached
     to the tracks."
  2. "Comparisons will ignore any (artist cover) from the title comparison."
  3. "if the track is a live, acoustic, etc will ignore the (Live), etc from the
     title matching."

WHY BEHAVIOURAL. Every rule here is a suppression rule, and a suppression rule
fails SILENTLY — a wrong one makes the review quietly report less. A
source-text assertion cannot tell a correct suppression from an over-eager one,
so all three are EXECUTED: the proposal engine is driven directly (it is pure),
and the shared matcher is called with fixtures.

⚠️ EVERY suppression rule is paired with a CONTROL asserting that a genuine
difference is still reported. Without that pairing a rule that suppresses
EVERYTHING would pass.
"""
from __future__ import annotations

import pytest

from helpers.normalization_service import (
    has_version_marker,
    normalize_title_for_compare,
)
from services.enrichment.musicbrainz_service import _match_mb_tracks_to_library
from services.metadata.metadata_proposal_service import (
    _genre_sets_equal,
    _track_proposals,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _local(**overrides):
    """A library track row as ``_load_local_tracks`` (SELECT *) would return."""
    row = {
        "id": "t1",
        "title": "Song",
        "track_number": "1",
        "disc_number": "1",
        "mbid": "",
        "writer": "",
        "musicbrainz_genres": "",
        "genres": "",
        "mb_ignored_fields": None,
        "is_cover": 0,
        "is_live": 0,
        "is_acoustic": 0,
        "is_remix": 0,
    }
    row.update(overrides)
    return row


def _comparison(mb_title="Song", rec="rec-1"):
    return [{
        "library_track_id": "t1",
        "matched": True,
        "mb_title": mb_title,
        "mb_track_number": 1,
        "mb_disc_number": 1,
        "mb_recording_mbid": rec,
    }]


def _metadata(mb_genres="", mb_title="Song", rec="rec-1"):
    return {
        "tracks": [{
            "mb_title": mb_title,
            "mb_recording_mbid": rec,
            "mb_disc_number": 1,
            "mb_track_number": 1,
            "musicbrainz_genres": mb_genres,
            "writer": "",
        }],
    }


def _fields(local, comparison=None, metadata=None):
    """The set of `field` names the track proposal would report."""
    out = _track_proposals(
        [local], comparison or _comparison(), metadata or _metadata()
    )
    return {c["field"] for c in (out[0]["changes"] if out else [])}


def _mb_track(mb_title, disc=1, num=1, rec="rec-1"):
    return {
        "mb_title": mb_title,
        "mb_disc_number": disc,
        "mb_track_number": num,
        "mb_recording_mbid": rec,
        "mb_duration": 0,
    }


def _lib_track(title, disc="1", num="1", mbid="", tid="t1"):
    return {
        "id": tid,
        "title": title,
        "disc_number": disc,
        "track_number": num,
        "mbid": mbid,
        "duration": 0,
        "mb_ignored_fields": None,
    }


def _diff_fields(lib_title, mb_title):
    comparison, _extra = _match_mb_tracks_to_library(
        [_mb_track(mb_title)], [_lib_track(lib_title)]
    )
    return set(comparison[0].get("diff_fields") or [])


# ---------------------------------------------------------------------------
# 1. Genres must be compared against the TRACK's genres
# ---------------------------------------------------------------------------

class TestGenresCompareAgainstTheTracksOwnGenres:
    def test_the_current_side_is_the_tracks_genres_not_the_mb_column(self):
        """The reported defect: MB was compared against MB.

        `musicbrainz_genres` holds MusicBrainz's own genres, so using it as the
        "current" side compares a value with itself and can only ever differ
        when the STORED mb genres are stale — never when the track's real genres
        disagree with the release's.
        """
        fields_probe = _fields(
            _local(genres="Rock, Metal", musicbrainz_genres="Rock"),
            _comparison(),
            _metadata(mb_genres="Rock"),
        )
        assert "musicbrainz_genres" in fields_probe, (
            "the track's own genres ('Rock, Metal') differ from MusicBrainz's "
            "('Rock'), so a genre change MUST be reported — with the MB column "
            "as the 'current' side the two sides are identical and it vanishes"
        )

    def test_the_reported_current_value_is_the_tracks_genres(self):
        out = _track_proposals(
            [_local(genres="Rock, Metal", musicbrainz_genres="Rock")],
            _comparison(),
            _metadata(mb_genres="Rock"),
        )
        change = next(
            c for c in out[0]["changes"] if c["field"] == "musicbrainz_genres"
        )
        assert change["current"] == "Rock, Metal", (
            "the review must show the track's ACTUAL genres as the current value, "
            "or the user cannot tell what they are replacing"
        )
        assert change["proposed"] == "Rock"

    def test_identical_genres_are_not_reported(self):
        """CONTROL — the common case must stay quiet."""
        assert "musicbrainz_genres" not in _fields(
            _local(genres="Rock, Metal"), _comparison(),
            _metadata(mb_genres="Rock, Metal"),
        )

    @pytest.mark.parametrize("lib_genres,mb_genres", [
        # ⚠️ THESE must run through the PROPOSAL PATH, not just the helper.
        # Testing `_genre_sets_equal` on its own does not cover the wiring: the
        # oracle proved a mutation swapping the set comparison for a plain
        # string comparison SURVIVED, because the only end-to-end case used two
        # identical strings ("Rock, Metal" both sides), which a plain string
        # compare also accepts. These pairs differ as STRINGS but are the same
        # SET, so only the real rule accepts them.
        ("Rock, Metal", '["Metal", "Rock"]'),   # order (JSONB vs TEXT column)
        ("Metal, Rock", '["Rock", "Metal"]'),
        ("rock,metal", '["Rock", "Metal"]'),    # spacing/case/encoding
        ("Rock, Metal", "Metal, Rock"),
    ])
    def test_the_same_genres_in_a_different_order_are_not_reported(
        self, lib_genres, mb_genres
    ):
        """Genres are an unordered SET, and the two columns differ in TYPE.

        `tracks.genres` is TEXT (comma-joined) while `musicbrainz_genres` is
        JSONB, so a plain string comparison reports a change for the same genres
        written two ways.
        """
        assert "musicbrainz_genres" not in _fields(
            _local(genres=lib_genres), _comparison(),
            _metadata(mb_genres=mb_genres),
        ), (
            f"{lib_genres!r} and {mb_genres!r} are the same genres; a title-style "
            "string comparison would wrongly report a change"
        )

    def test_a_real_genre_difference_is_still_reported_end_to_end(self):
        """CONTROL through the proposal path — set-equality must not be a no-op."""
        assert "musicbrainz_genres" in _fields(
            _local(genres="Rock"), _comparison(), _metadata(mb_genres="Rock, Metal"),
        )

    def test_no_local_genres_at_all_is_not_reported_as_a_removal(self):
        """A track with no genres yet is "unset", not "differs by all of them"."""
        assert "musicbrainz_genres" not in _fields(
            _local(genres=""), _comparison(), _metadata(mb_genres=""),
        )

    @pytest.mark.parametrize("left,right", [
        ("Rock, Metal", '["Rock", "Metal"]'),   # TEXT column vs JSONB column
        ("Rock, Metal", '["Metal", "Rock"]'),   # order must not matter
        ("Metal, Rock", '["Rock", "Metal"]'),
        ("rock,metal", '["Rock","Metal"]'),     # spacing/case
        ('["rock"]', '["Rock"]'),
    ])
    def test_the_same_genres_in_a_different_order_or_encoding_are_equal(
        self, left, right
    ):
        """Genres are an unordered set, and the two columns differ in type.

        `tracks.genres` is TEXT (comma-joined) while `musicbrainz_genres` is
        JSONB, so comparing raw strings would report a change for the same
        genres written two ways.
        """
        assert _genre_sets_equal(left, right) is True

    @pytest.mark.parametrize("left,right", [
        ("Rock", '["Rock", "Metal"]'),   # an extra genre IS a change
        ("Rock, Metal", '["Rock"]'),     # a removed genre IS a change
        ("Rock", '["Pop"]'),             # a substitution IS a change
        ("Rock", ""),                    # losing genres entirely IS a change
    ])
    def test_a_real_genre_difference_is_still_reported(self, left, right):
        """CONTROL — set-equality must not degrade to 'always equal'."""
        assert _genre_sets_equal(left, right) is False


# ---------------------------------------------------------------------------
# 2 & 3. Version / cover markers in titles
# ---------------------------------------------------------------------------

class TestCoverAndVersionMarkersAreIgnored:
    @pytest.mark.parametrize("lib,mb", [
        ("Song (Live)", "Song"),
        ("Song", "Song (Live)"),
        ("Song (Acoustic)", "Song"),
        ("Song (Unplugged)", "Song"),
        ("Song (Remix)", "Song"),
        ("Song (Demo)", "Song"),
        ("Song (Instrumental)", "Song"),
        ("Song (Orchestral)", "Song"),
        # The annotations the cover detector writes onto a track.
        ("Song (Artist Cover)", "Song"),
        ("Song (Nirvana Cover)", "Song"),
        ("Song (Original Artist Cover)", "Song"),
        # Qualified and unparenthesised variants seen in the wild.
        ("Song (Recorded Live)", "Song"),
        ("Song (Live at Leeds)", "Song"),
        ("Song (Stripped Acoustic)", "Song"),
        ("Song - Live", "Song"),
        ("Song – Live", "Song"),
        ("Song [Live]", "Song"),
        ("SONG (LIVE)", "song"),
        # A marker on BOTH sides, differently worded.
        ("Song (Live)", "Song (Acoustic)"),
    ])
    def test_the_marker_alone_does_not_raise_a_title_change(self, lib, mb):
        """The reported symptom, on BOTH comparison paths.

        `(Live)`/`(Acoustic)`/`(Remix)` and `(Artist Cover)` describe a
        performance VARIANT, which the metadata model stores in dedicated
        columns — so the title must not be reported as changed.
        """
        assert "title" not in _fields(
            _local(title=lib), _comparison(mb_title=mb),
            _metadata(mb_title=mb),
        ), "the track-proposal path must ignore the marker"
        assert "title" not in _diff_fields(lib, mb), (
            "the shared matcher (the Compare button) must ignore it too"
        )

    @pytest.mark.parametrize("lib,mb", [
        ("Old Name", "New Name"),
        ("Song", "Song Two"),
        ("Song (Live)", "Other Song"),
        ("Song One (Live)", "Song Two (Live)"),
    ])
    def test_a_genuine_wording_change_is_still_reported(self, lib, mb):
        """CONTROL — the marker rule must not silence a real rename."""
        assert "title" in _fields(
            _local(title=lib), _comparison(mb_title=mb), _metadata(mb_title=mb),
        ), "a real wording difference must still be reported"
        assert "title" in _diff_fields(lib, mb)

    def test_both_comparison_paths_agree(self):
        """ANTI-DRIFT: the preview and the Compare must not disagree.

        They are separate code paths sharing one key by design. If either grew
        its own rule, the review and the Compare button would show different
        counts for the same album — the exact inconsistency the shared key
        exists to prevent.
        """
        for lib, mb in [
            ("Song (Live)", "Song"),
            ("Song (Artist Cover)", "Song"),
            ("Old Name", "New Name"),
            ("Song", "Song Two"),
        ]:
            proposal_says = "title" in _fields(
                _local(title=lib), _comparison(mb_title=mb), _metadata(mb_title=mb),
            )
            matcher_says = "title" in _diff_fields(lib, mb)
            assert proposal_says == matcher_says, (
                f"the two paths disagree for {lib!r} vs {mb!r}: "
                f"proposal={proposal_says} matcher={matcher_says}"
            )

    def test_an_ignored_title_is_still_never_proposed(self):
        """The marker rule must not bypass `mb_ignored_fields`."""
        local = _local(title="Old Name", mb_ignored_fields='["title"]')
        assert "title" not in _fields(
            local, _comparison(mb_title="New Name"), _metadata(mb_title="New Name"),
        )


class TestTheMarkerKeyItself:
    @pytest.mark.parametrize("title", [
        "Song (Live)", "Song [Live]", "Song - Live", "Song – Live",
        "Song (Acoustic)", "Song (Remix)", "Song (Demo)",
        "Song (Artist Cover)", "Song (Original Artist Cover)",
        "Song (Recorded Live)", "Song (Live at Leeds)",
    ])
    def test_markers_are_detected(self, title):
        assert has_version_marker(title) is True

    @pytest.mark.parametrize("title", [
        # These contain marker WORDS but not as markers. An over-eager pattern
        # here would silently swallow a real title difference.
        "Live and Let Die",
        "Undercover",
        "Demo Lition",
        "Instrumental",
        "Song",
        "",
    ])
    def test_ordinary_titles_are_not_treated_as_marked(self, title):
        assert has_version_marker(title) is False

    @pytest.mark.parametrize("title", [
        "Song (Live)", "Song [Live]", "Song - Live", "Song (Acoustic)",
        "Song (Remix)", "Song (Demo)", "Song (Artist Cover)",
        "Song (Original Artist Cover)", "Song (Recorded Live)",
    ])
    def test_removing_the_marker_leaves_the_base_title(self, title):
        assert normalize_title_for_compare(title) == "song"

    def test_the_key_preserves_a_real_wording_difference(self):
        assert (
            normalize_title_for_compare("Song One (Live)")
            != normalize_title_for_compare("Song Two (Live)")
        )
