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


def _comparison(mb_title="Song", rec="rec-1", mb_track_number=1):
    return [{
        "library_track_id": "t1",
        "matched": True,
        "mb_title": mb_title,
        "mb_track_number": mb_track_number,
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

class TestGenresCompareTheMusicBrainzColumn:
    """The Genres bar is about ``musicbrainz_genres``, not the main field.

    Reported:

        "The changes show Genres, but the Genres should only be comparing and
         adjusting the MusicBrainz genres field not the main genres field."

    with a bar reading *arena rock, … symphonic rock, heavy metal, In Love,
    Other, rock opera* → *arena rock, … symphonic rock* — i.e. the track's own
    aggregated genres against MusicBrainz's.

    The save path has ALWAYS written ``musicbrainz_genres`` only
    (``_STAGED_WRITABLE``), so showing ``genres`` as the current side implied a
    write that never happens while hiding whether the MB column itself is
    stale. This deliberately REVERSES the earlier fix that moved the comparison
    the other way; see the comment in metadata_proposal_service.
    """

    def test_the_main_genres_are_never_the_current_side(self):
        """The reported example: own genres differ, MB column already matches."""
        fields_probe = _fields(
            _local(
                genres="arena rock, ballad, classic rock, hard rock, piano rock, "
                        "pop, pop rock, rock, soft rock, symphonic rock, "
                        "heavy metal, In Love, Other, rock opera",
                musicbrainz_genres="arena rock, ballad, classic rock, hard rock, "
                                   "piano rock, pop, pop rock, rock, soft rock, "
                                   "symphonic rock",
            ),
            _comparison(),
            _metadata(mb_genres="arena rock, ballad, classic rock, hard rock, "
                                "piano rock, pop, pop rock, rock, soft rock, "
                                "symphonic rock"),
        )
        assert "musicbrainz_genres" not in fields_probe, (
            "the main genres are an aggregate of several sources and must not "
            "raise a MusicBrainz bar — only a stale MB column may"
        )

    def test_the_reported_current_value_is_the_stored_mb_column(self):
        out = _track_proposals(
            [_local(musicbrainz_genres="Rock", genres="Rock, Metal, In Love")],
            _comparison(),
            _metadata(mb_genres="Rock, Metal"),
        )
        change = next(
            c for c in out[0]["changes"] if c["field"] == "musicbrainz_genres"
        )
        assert change["current"] == "Rock", (
            "the bar must show the stored MusicBrainz genres, not the track's "
            "own genres — applying writes the MB column"
        )
        assert change["proposed"] == "Rock, Metal"

    def test_a_stale_mb_column_is_still_reported(self):
        """CONTROL — the bar must still appear when the MB column is out of date."""
        assert "musicbrainz_genres" in _fields(
            _local(musicbrainz_genres="Rock", genres="Rock, Metal"),
            _comparison(),
            _metadata(mb_genres="Rock, Metal"),
        )

    def test_an_unpopulated_mb_column_is_filled_in(self):
        """The common first case: the column was never written at all."""
        out = _track_proposals(
            [_local(musicbrainz_genres="", genres="Rock, Metal")],
            _comparison(),
            _metadata(mb_genres="Rock"),
        )
        change = next(
            c for c in out[0]["changes"] if c["field"] == "musicbrainz_genres"
        )
        assert change["current"] == ""
        assert change["proposed"] == "Rock"

    def test_identical_genres_are_not_reported(self):
        """CONTROL — the common case must stay quiet."""
        assert "musicbrainz_genres" not in _fields(
            _local(musicbrainz_genres="Rock, Metal"), _comparison(),
            _metadata(mb_genres="Rock, Metal"),
        )

    @pytest.mark.parametrize("stored,mb_genres", [
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
        self, stored, mb_genres
    ):
        """Genres are an unordered SET, and the two sides differ in TYPE.

        ``musicbrainz_genres`` is JSONB while the freshly fetched value is a
        plain string, so a plain string comparison reports a change for the
        same genres written two ways.
        """
        assert "musicbrainz_genres" not in _fields(
            _local(musicbrainz_genres=stored), _comparison(),
            _metadata(mb_genres=mb_genres),
        ), (
            f"{stored!r} and {mb_genres!r} are the same genres; a title-style "
            "string comparison would wrongly report a change"
        )

    def test_a_real_genre_difference_is_still_reported_end_to_end(self):
        """CONTROL through the proposal path — set-equality must not be a no-op."""
        assert "musicbrainz_genres" in _fields(
            _local(musicbrainz_genres="Rock"), _comparison(),
            _metadata(mb_genres="Rock, Metal"),
        )

    def test_no_local_genres_at_all_is_not_reported_as_a_removal(self):
        """A track with no genres yet is "unset", not "differs by all of them"."""
        assert "musicbrainz_genres" not in _fields(
            _local(musicbrainz_genres="", genres=""), _comparison(),
            _metadata(mb_genres=""),
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


class TestAJsonbListIsNotARepr:
    """Reported: *"the genre matching … needs to be corrected as it's finding
    it needs to be updated but it's matching with the same information"*.

    ``musicbrainz_genres`` is JSONB, so the driver hands back a Python
    ``list`` — and ``str(list)`` is a REPR (``['Rock', 'Metal']``), which is
    NOT JSON. The tolerant parser rejected it, comma-split the fallback, and
    produced the names ``['Rock'`` and ``'Metal']``: the same genres spelled
    differently, so EVERY genre row proposed changing a value into itself
    (``["children's music", 'classical'] → children's music, classical``).

    The other cases in this module feed STRINGS, which is exactly why none of
    them caught it: this class feeds what Postgres actually returns.
    """

    LIST_STORED = [
        "children's music", "classical", "electronic",
        "musical", "pop", "punk",
    ]
    COMMA = "children's music, classical, electronic, musical, pop, punk"

    def test_the_exact_reported_row_proposes_nothing(self):
        assert "musicbrainz_genres" not in _fields(
            _local(musicbrainz_genres=self.LIST_STORED), _comparison(),
            _metadata(mb_genres=self.COMMA),
        ), (
            "the same genres as a list and as a string must not ask to be "
            "changed — that is the reported false update"
        )

    def test_a_repr_is_read_as_the_list_it_is(self):
        # Imported here, not at module level: with the fix stashed the name
        # does not exist, and a module-level import would collapse the whole
        # file into one collection error — no test could then say WHICH rule
        # it was checking.
        from services.metadata.metadata_proposal_service import _genres_text

        assert _genre_sets_equal(str(["Rock", "Metal"]), "Metal, Rock") is True
        assert _genres_text(str(["Rock", "Metal"])) == "Rock, Metal"

    def test_the_displayed_value_is_never_a_python_repr(self):
        """The bar must show genres a human can line up, not ``str(list)``."""
        out = _track_proposals(
            [_local(musicbrainz_genres=self.LIST_STORED)],
            _comparison(),
            _metadata(mb_genres=self.COMMA + ", rock"),
        )
        change = next(
            c for c in out[0]["changes"] if c["field"] == "musicbrainz_genres"
        )
        assert change["current"] == self.COMMA
        assert not change["current"].startswith("[")
        assert not change["proposed"].startswith("[")

    def test_a_real_difference_with_a_list_is_still_reported(self):
        """CONTROL — suppressing the repr must not suppress the signal."""
        assert "musicbrainz_genres" in _fields(
            _local(musicbrainz_genres=["Rock"]), _comparison(),
            _metadata(mb_genres="Rock, Metal"),
        )

    def test_an_extra_genre_from_a_list_is_still_reported(self):
        """CONTROL at the helper — the set rule still sees a real delta."""
        assert _genre_sets_equal(self.LIST_STORED, self.COMMA + ", rock") is False


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
        """The original report, still true for PERFORMANCE markers.

        `(Live)`/`(Acoustic)`/`(Remix)` describe a performance VARIANT, which
        the metadata model stores in dedicated columns — so the title must not
        be reported as changed.

        COVER markers deliberately moved to
        ``TestCoverMarkersAreReportedAndJudged``: see that class for why.
        """
        assert "title" not in _fields(
            _local(title=lib), _comparison(mb_title=mb),
            _metadata(mb_title=mb),
        ), "the track-proposal path must ignore the performance marker"
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


class TestCoverMarkersAreReportedAndJudged:
    """A COVER claim must surface so the user can review it.

    Reported:

        "I want this to pick up if the track name is different even if it
         shows (Cover Version) as this can be selected to skip if this is a
         false cover. Though if the work relationship is downloaded during
         this lookup then it should know whether a cover is likely based on
         the relationship status."

    A cover marker is not a cosmetic variant like ``(Live)``: it asserts WHO
    performed the recording, and only the work relationship fetched with the
    lookup can confirm that.  Swallowing it left the user with no way to spot
    a false cover at all.
    """

    @pytest.mark.parametrize("lib,mb", [
        ("Song (Artist Cover)", "Song"),
        ("Song (Nirvana Cover)", "Song"),
        ("Song (Original Artist Cover)", "Song"),
        # The spelling from the report itself.
        ("Song (Cover Version)", "Song"),
        ("Song", "Song (Cover Version)"),
        # Marked on both sides, differently worded.
        ("Song (Artist Cover)", "Song (Cover Version)"),
    ])
    def test_a_cover_marker_raises_a_title_change_on_both_paths(self, lib, mb):
        assert "title" in _fields(
            _local(title=lib), _comparison(mb_title=mb), _metadata(mb_title=mb),
        ), "the Lookup-MBID preview must show a cover-marker difference"
        assert "title" in _diff_fields(lib, mb), (
            "the Compare button must agree with the preview — a second, "
            "independently-written title rule is what lets them drift"
        )

    @pytest.mark.parametrize("lib,mb", [
        ("Song (Live)", "Song"),
        ("Song (Acoustic)", "Song"),
        ("Song (Remix)", "Song"),
        ("Song", "Song (Live)"),
    ])
    def test_a_performance_marker_is_still_ignored(self, lib, mb):
        """CONTROL — the new rule must not report every marker again."""
        assert "title" not in _fields(
            _local(title=lib), _comparison(mb_title=mb), _metadata(mb_title=mb),
        ), f"{lib!r} vs {mb!r} is a performance variant, not a rename"

    def test_an_identical_title_is_still_quiet(self):
        """CONTROL — no bar when nothing differs at all."""
        assert "title" not in _fields(
            _local(title="Song"), _comparison(mb_title="Song"),
            _metadata(mb_title="Song"),
        )


def _metadata_with(mb_track_extra, mb_title="Song"):
    """``_metadata`` with extra keys merged onto its single track entry."""
    meta = _metadata(mb_title=mb_title)
    meta["tracks"][0].update(mb_track_extra)
    return meta


def _title_note(local_title, mb_title, mb_track_extra):
    """The ``note`` carried by the reported title change (None = no change)."""
    out = _track_proposals(
        [_local(title=local_title)],
        _comparison(mb_title=mb_title),
        _metadata_with(mb_track_extra, mb_title=mb_title),
    )
    if not out:
        return None
    change = next(
        (c for c in out[0]["changes"] if c["field"] == "title"), None
    )
    assert change is not None, "the title change must be reported"
    return change.get("note")


class TestTheWorkRelationshipCoverVerdict:
    """The verdict rides ON the title bar, so a false cover can be judged."""

    def test_a_work_credited_to_another_artist_is_a_cover(self):
        note = _title_note(
            "Song (Cover Version)", "Song",
            {"is_cover": True, "original_cover_artist": "Nirvana",
             "work_mbid": "work-1"},
        )
        assert note == "cover of Nirvana (work relationship)", (
            "the work relationship says another artist wrote the work, so the "
            "bar must say it is a cover"
        )

    def test_a_cover_without_a_named_original_artist_still_says_cover(self):
        note = _title_note(
            "Song (Cover Version)", "Song",
            {"is_cover": True, "work_mbid": "work-1"},
        )
        assert note and note.startswith("cover")

    def test_a_work_by_the_same_artist_disproves_a_cover_marker(self):
        """The false-cover case from the report.

        The work HAS an artist-credit (``work_artist``) and ``is_cover`` was
        therefore computed and came back False — that is what makes the
        negative verdict legitimate.
        """
        note = _title_note(
            "Song (Cover Version)", "Song",
            {"work_mbid": "work-1", "work_artist": "Nirvana"},
        )
        assert note == "not a cover (work relationship)", (
            "a work relationship exists and credits the same artist, so the "
            "(Cover Version) marker is unsupported and the user must be told"
        )

    def test_a_work_with_no_artist_credit_says_nothing(self):
        """No credit → the comparison never ran → silence, not a verdict.

        REPORTED: covers on Various-Artists compilations were labelled "not a
        cover (work relationship)". ``_flatten_release`` only sets ``is_cover``
        when ``work['artist-credit']`` exists; a work without one leaves it
        ABSENT, and absent is not False.
        """
        assert _title_note(
            "Song (Cover Version)", "Song", {"work_mbid": "work-1"},
        ) is None, (
            "a work we could not compare must not produce a negative verdict — "
            "that is the reported mislabel"
        )

    def test_no_artist_credit_is_not_the_same_as_a_missing_work(self):
        """CONTROL — both return nothing, for two DIFFERENT reasons."""
        assert _title_note("Song (Cover Version)", "Song", {}) is None
        assert _title_note(
            "Song (Cover Version)", "Song", {"work_mbid": "work-1"},
        ) is None

    def test_no_work_relationship_means_no_verdict(self):
        """Only claim something when the lookup actually fetched the work."""
        assert _title_note("Song (Cover Version)", "Song", {}) is None, (
            "without a work relationship there is nothing to base a verdict on"
        )

    def test_no_marker_and_no_cover_means_no_note(self):
        """CONTROL — an ordinary wording difference must not grow a note."""
        assert _title_note("Old Name", "New Name", {"work_mbid": "work-1"}) is None

    def test_the_note_is_attached_only_to_the_title_change(self):
        out = _track_proposals(
            [_local(title="Song (Cover Version)", track_number="1")],
            _comparison(mb_title="Song", mb_track_number=2),
            _metadata_with({"work_mbid": "work-1"}, mb_title="Song"),
        )
        assert out, "the track must be proposed"
        for change in out[0]["changes"]:
            if change["field"] != "title":
                assert "note" not in change, (
                    "the cover verdict belongs on the title bar only"
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


class TestWriterUsesTheNameListRule:
    """Reported: **Writer:** *["Marianne Faithfull", …]* → **Marianne Faithfull,
    Joe Mavety, …** — the writer lookup, "a similar issue to the genre
    matching".

    ``tracks.writer`` is a TEXT column that stores a **JSON array**, while
    ``_flatten_release`` hands back a comma string. The field had no set rule,
    so it was compared as a plain string: the SAME writers spelled two ways
    always read as a change — the exact defect the genre rows had, on a field
    that never got the rule. Both sides are now rendered through the shared
    name-list parser, so the bar also shows two lines a human can line up
    instead of a raw JSON array opposite a comma string.
    """

    STORED = '["Marianne Faithfull", "Joe Mavety", "Barry Reynolds"]'
    PROPOSED = "Marianne Faithfull, Joe Mavety, Barry Reynolds"
    # The pair from the report: the stored list carries one more name.
    STORED_EXTRA = (
        '["Marianne Faithfull", "Joe Mavety", "Barry Reynolds", "Ralph Sall"]'
    )

    @staticmethod
    def _writer_fields(stored: str, proposed: str) -> set[str]:
        out = _track_proposals(
            [_local(writer=stored)],
            _comparison(),
            _metadata_with({"writer": proposed}),
        )
        return {c["field"] for c in (out[0]["changes"] if out else [])}

    @staticmethod
    def _writer_change(stored: str, proposed: str) -> dict | None:
        out = _track_proposals(
            [_local(writer=stored)],
            _comparison(),
            _metadata_with({"writer": proposed}),
        )
        for change in out[0]["changes"] if out else []:
            if change["field"] == "writer":
                return change
        return None

    def test_the_same_writers_in_a_different_encoding_are_not_reported(self):
        assert "writer" not in self._writer_fields(self.STORED, self.PROPOSED), (
            "a JSON array and a comma string list the same writers — comparing "
            "them as strings is what made every lookup propose a no-op change"
        )

    @pytest.mark.parametrize("stored,proposed", [
        ('["Rock Band", "Other Writer"]', "Other Writer, Rock Band"),  # order
        ('["  Rock Band  "]', "Rock Band"),                            # spacing
        ('["rock band"]', "Rock Band"),                                # case
    ])
    def test_order_spacing_and_case_do_not_matter(self, stored, proposed):
        assert "writer" not in self._writer_fields(stored, proposed)

    def test_the_reported_pair_still_reports(self):
        """CONTROL — the bar in the report lists a name MusicBrainz drops.

        ``Ralph Sall`` is in the stored array and not in the proposal, so this
        is a genuine difference; suppressing it would hide a real edit.
        """
        assert "writer" in self._writer_fields(self.STORED_EXTRA, self.PROPOSED)

    def test_the_displayed_writer_is_a_clean_line_on_both_sides(self):
        """The report showed a raw JSON array against a comma string."""
        change = self._writer_change(self.STORED_EXTRA, self.PROPOSED)

        assert change is not None
        assert change["current"].startswith("Marianne Faithfull")
        assert "Ralph Sall" in change["current"], (
            "the dropped name must be visible — that is what the bar is for"
        )
        assert not change["current"].startswith("[")
        assert not change["proposed"].startswith("[")

    def test_an_empty_stored_writer_is_still_filled(self):
        """CONTROL — set-equality must not swallow a real addition."""
        assert "writer" in self._writer_fields("", self.PROPOSED)
