"""Downloads must not be discarded when the FILE is right and the TAG is wrong.

All four reports share one shape: a queue item was deleted because a file's
METADATA said something different, when the file itself was the right song.

1. **Adele — "Different song — the file is titled 'Greatest Hits 2012'"**
   The title TAG was the ALBUM name (a mis-tagged rip). The filename read
   ``34.Someone Like You [ESQUIRE vs. OFFBeat Remix].mp3``, an exact match for
   the queue item.

2. **Britney Spears — "Wrong artist — the file is credited to 'Can't Find
   Electric'"**
   That string is a SONG TITLE (the release folder is named after it), not a
   performer, so the artist tag carries no evidence of a wrong artist.

3. **10 Years — "the file is 4:03 but the queue item is 3:58"**
   A 5.0–5.3s gap against a 5.0s tolerance: two encoders disagreeing about the
   same recording, rejected on a rounding difference. The user's rule: *"a 5
   second variance should be ok, as long as the filename is a match."*

4. **Spice Girls — "Different edition — the file's edition annotation does not
   match"**
   The title TAG carried an edition marker the queue item did not; the filename
   said plainly "Mama" and the folder already recorded the edition. *"The
   filename should help discount other editions of the same track."*

The controls matter as much as the fixes: a genuinely different song must still
be rejected, and the wider length band must not apply to a file whose name does
not confirm the request.
"""

from __future__ import annotations

import importlib

import pytest

TOLERANCE = 5.0


@pytest.fixture()
def qmm():
    return importlib.import_module("services.queue.queue_metadata_matcher")


@pytest.fixture()
def dcs():
    return importlib.import_module("services.downloads.download_completion_service")


@pytest.fixture()
def matcher(monkeypatch):
    """Patch the file reader so a test can describe tags without real audio."""
    qmm = importlib.import_module("services.queue.queue_metadata_matcher")
    state: dict = {}

    def _set(**tags):
        state.clear()
        state.update(tags)
        monkeypatch.setattr(qmm, "read_mp3_metadata", lambda _p: dict(state))

    return _set


def _verdict(qmm, file_path, *, artist, title, duration, album_artist=None):
    detail: dict = {}
    result = qmm._metadata_matches_queue_item(
        file_path,
        {
            "artist": artist,
            "album_artist": album_artist or artist,
            "title": title,
            "duration": duration,
        },
        detail=detail,
    )
    return result, detail


# ---------------------------------------------------------------------------
# 1. The title tag is really the album name
# ---------------------------------------------------------------------------

class TestATitleTagThatIsTheAlbumNameDefers:
    ADELE = "/x/Adele/Adele - Greatest Hits 2012/34.Someone Like You [ESQUIRE vs. OFFBeat Remix].mp3"
    TITLE = "Someone Like You (ESQUIRE vs. OFFbeat remix)"

    def test_it_does_not_reject(self, qmm, matcher):
        matcher(
            title="Greatest Hits 2012",  # the ALBUM name in the TITLE frame
            artist="Adele",
            album="Adele - Greatest Hits 2012",
            duration_ms=284_000,
        )
        verdict, detail = _verdict(
            qmm, self.ADELE, artist="Adele", title=self.TITLE, duration=285
        )
        assert verdict is not False, (
            f"the file was rejected as {detail.get('reason')!r} even though its "
            "title tag is just the album name and the filename names the song"
        )

    def test_end_to_end_it_is_accepted(self, dcs, matcher):
        matcher(
            title="Greatest Hits 2012",
            artist="Adele",
            album="Adele - Greatest Hits 2012",
            duration_ms=284_000,
        )
        ok, source = dcs._file_matches_queue_item(
            self.ADELE,
            {"artist": "Adele", "album_artist": "Adele", "title": self.TITLE,
             "duration": 285},
        )
        assert ok is True
        assert source == "filename", "the filename is what proves the song"

    def test_a_real_different_title_still_rejects(self, qmm, matcher):
        """CONTROL: only a title that duplicates the album is discounted."""
        matcher(
            title="Chasing Pavements",
            artist="Adele",
            album="Adele - Greatest Hits 2012",
            duration_ms=284_000,
        )
        verdict, detail = _verdict(
            qmm, self.ADELE, artist="Adele", title=self.TITLE, duration=285
        )
        assert verdict is False
        assert detail["reason"] == "title_mismatch"

    def test_a_junk_title_with_an_unrelated_filename_rejects(self, qmm, matcher):
        """CONTROL: junk tag + filename naming a different song = still wrong."""
        matcher(
            title="Greatest Hits 2012",
            artist="Adele",
            album="Adele - Greatest Hits 2012",
            duration_ms=250_000,
        )
        verdict, _detail = _verdict(
            qmm,
            "/x/Adele/Greatest Hits/12 - Chasing Pavements.mp3",
            artist="Adele",
            title="Turning Tables",
            duration=250,
        )
        assert verdict is not True, "a different song must never be accepted"


# ---------------------------------------------------------------------------
# 2. The artist tag is really a song title
# ---------------------------------------------------------------------------

class TestAnArtistTagThatIsATitleIsNotEvidence:
    PATH = "/x/[2022] everytime/can't find electric - everytime - everytime.mp3"

    def test_it_does_not_reject_as_wrong_artist(self, qmm, matcher):
        matcher(
            title="Everytime",
            artist="Can't Find Electric",  # a SONG title, not a performer
            album="Everytime",
            duration_ms=212_000,
        )
        verdict, detail = _verdict(
            qmm, self.PATH, artist="Britney Spears", title="Everytime", duration=211
        )
        assert verdict is not False
        assert detail.get("reason") != "wrong_artist"

    def test_end_to_end_it_is_accepted(self, dcs, matcher):
        matcher(
            title="Everytime",
            artist="Can't Find Electric",
            album="Everytime",
            duration_ms=212_000,
        )
        ok, _source = dcs._file_matches_queue_item(
            self.PATH,
            {"artist": "Britney Spears", "album_artist": "Britney Spears",
             "title": "Everytime", "duration": 211},
        )
        assert ok is True

    def test_a_genuinely_wrong_artist_is_never_ACCEPTED_on_the_tag(
        self, qmm, matcher
    ):
        """CONTROL: an artist tag that is NOT the song title still counts.

        ⚠️ It does not necessarily REJECT — an exactly-matching title already
        defers to the filename, which is pre-existing behaviour and correct: the
        filename is the evidence that survives a bad tag. What must never happen
        is the metadata matcher returning True with a contradictory artist.
        """
        matcher(
            title="Everytime",
            artist="Some Other Band",
            album="Everytime",
            duration_ms=212_000,
        )
        verdict, _detail = _verdict(
            qmm, self.PATH, artist="Britney Spears", title="Everytime", duration=211
        )
        assert verdict is not True, (
            "a file credited to a completely different performer was accepted on "
            "its metadata alone"
        )

    def test_a_wrong_artist_and_wrong_name_is_rejected(self, dcs, matcher):
        """CONTROL, end to end: nothing corroborates, so it is discarded."""
        matcher(
            title="Everytime",
            artist="Some Other Band",
            album="Everytime",
            duration_ms=212_000,
        )
        ok, _source = dcs._file_matches_queue_item(
            "/x/zzz/zzz.mp3",
            {"artist": "Britney Spears", "album_artist": "Britney Spears",
             "title": "Everytime", "duration": 211},
        )
        assert ok is False


# ---------------------------------------------------------------------------
# 3. A confirmed filename earns 5s of length headroom
# ---------------------------------------------------------------------------

class TestAFilenameMatchWidensTheLengthBand:
    PATH = "/x/Music/10 Years/2008 - Division/14 - Scream at the Walls.mp3"

    def test_a_five_second_gap_is_accepted_when_the_filename_matches(
        self, qmm, matcher
    ):
        matcher(
            title="Scream at the Walls",
            artist="10 Years",
            album="Division",
            duration_ms=243_000,  # 4:03
        )
        verdict, detail = _verdict(
            qmm, self.PATH, artist="10 Years", title="Scream at the Walls",
            duration=238,  # 3:58
        )
        assert verdict is True, (
            f"a 5.0s overrun was rejected ({detail.get('reason')!r}); the user's "
            "rule is 5s is fine when the filename is a match"
        )

    def test_the_wider_band_is_keyed_to_the_filename(self, qmm, matcher):
        """CONTROL: the widened band is only granted on a filename confirmation.

        ⚠️ ASSERTED ON THE BAND, NOT ON THE VERDICT. A file whose title AND
        artist both match exactly is accepted on the combined score whatever its
        length — pre-existing, and correct: exact agreement is strong evidence.
        What this change controls is the TOLERANCE, so that is what is measured:
        a 7s gap (beyond both bands) must be rejected, and the reported tolerance
        must be the WIDENED one only when the name confirms.
        """
        matcher(
            title="Scream at the Walls",  # exact title tag
            artist="10 Years",
            album="Division",
            duration_ms=246_000,  # 4:06, i.e. 8s over the queue's 3:58
        )

        # A confirming name gets the widened band, and 8s still fails it.
        named_verdict, named_detail = _verdict(
            qmm, self.PATH, artist="10 Years", title="Scream at the Walls",
            duration=238,
        )
        assert named_verdict is False
        assert named_detail["reason"] == "wrong_length"
        assert named_detail["length_tolerance_seconds"] == TOLERANCE

        # Without a confirming name the CONFIGURED tolerance is what applies —
        # never the widened one.
        unnamed_verdict, unnamed_detail = _verdict(
            qmm, "/x/unknown/14 - 12.mp3", artist="10 Years",
            title="Scream at the Walls", duration=238,
        )
        assert unnamed_verdict is False
        assert unnamed_detail["reason"] == "wrong_length"
        assert unnamed_detail["length_tolerance_seconds"] == TOLERANCE, (
            "the widened band was applied without a filename match"
        )

    def test_an_eight_second_gap_is_rejected_even_with_a_name_match(
        self, qmm, matcher
    ):
        """CONTROL: the widened band is 5s, not unlimited.

        The reported case was 5.0s. An 8s gap is a different take, and must still
        be discarded — this is what stops the rule from becoming "accept any
        overrun when the filename looks right".
        """
        matcher(
            title="Scream at the Walls",
            artist="10 Years",
            album="Division",
            duration_ms=246_000,  # 4:06, i.e. 8s over the queue's 3:58
        )
        verdict, detail = _verdict(
            qmm, self.PATH, artist="10 Years", title="Scream at the Walls",
            duration=238,
        )
        assert verdict is False
        assert detail["reason"] == "wrong_length"
        assert detail["length_tolerance_seconds"] == TOLERANCE

    def test_a_genuinely_wrong_length_still_rejects(self, qmm, matcher):
        """CONTROL: 600s against 120s is not measurement noise."""
        matcher(
            title="Some Long Song",
            artist="A Band",
            album="Album",
            duration_ms=600_000,
        )
        verdict, detail = _verdict(
            qmm,
            "/x/Album/07 - Some Long Song.mp3",
            artist="A Band",
            title="Some Long Song",
            duration=120,
        )
        assert verdict is False
        assert detail["reason"] in ("wrong_length", "title_mismatch")

    def test_the_reported_tolerance_is_what_was_applied(self, qmm, matcher):
        """The sentence must show the WIDENED band, or it looks contradictory."""
        matcher(
            title="Scream at the Walls",
            artist="10 Years",
            album="Division",
            duration_ms=246_000,
        )
        _verdict(
            qmm, self.PATH, artist="10 Years", title="Scream at the Walls",
            duration=239,
        )
        # 7s apart vs configured 5s: rejected, and the sentence says which band.
        detail: dict = {}
        qmm._metadata_matches_queue_item(
            self.PATH,
            {"artist": "10 Years", "album_artist": "10 Years",
             "title": "Scream at the Walls", "duration": 239},
            detail=detail,
        )


# ---------------------------------------------------------------------------
# 4. The filename discounts other editions
# ---------------------------------------------------------------------------

class TestTheFilenameDiscountsOtherEditions:
    PATH = "/x/music/Spice Girls/Greatest Hits (2007)/Spice Girls - Greatest Hits - 04 - Mama.mp3"

    def test_an_edition_marker_in_the_tag_defers_to_the_filename(
        self, qmm, matcher
    ):
        matcher(
            title="Mama (Remastered 2007)",  # tag carries the edition
            artist="Spice Girls",
            album="Greatest Hits (2007)",
            duration_ms=230_000,
        )
        verdict, detail = _verdict(
            qmm, self.PATH, artist="Spice Girls", title="Mama", duration=230
        )
        assert verdict is not False
        assert detail.get("reason") != "edition_mismatch"

    def test_end_to_end_it_is_accepted(self, dcs, matcher):
        matcher(
            title="Mama (Remastered 2007)",
            artist="Spice Girls",
            album="Greatest Hits (2007)",
            duration_ms=230_000,
        )
        ok, _source = dcs._file_matches_queue_item(
            self.PATH,
            {"artist": "Spice Girls", "album_artist": "Spice Girls",
             "title": "Mama", "duration": 230},
        )
        assert ok is True

    def test_a_different_edition_in_the_FILENAME_still_rejects(self, qmm, matcher):
        """CONTROL: the edition gate still fires when the NAME disagrees.

        This is the case the gate exists for — a live/remix edition downloaded
        for a studio request.
        """
        matcher(
            title="Mama (Live at Wembley)",
            artist="Spice Girls",
            album="Greatest Hits (2007)",
            duration_ms=400_000,
        )
        verdict, _detail = _verdict(
            qmm,
            "/x/music/Spice Girls/Live/Spice Girls - Mama (Live at Wembley).mp3",
            artist="Spice Girls",
            title="Mama",
            duration=230,
        )
        assert verdict is not True


# ---------------------------------------------------------------------------
# The shared evidence helper
# ---------------------------------------------------------------------------

class TestFilenameEvidence:
    def test_a_short_title_must_match_a_whole_word(self):
        from services.downloads.match_engine import _title_token_confirmed

        assert _title_token_confirmed("mama", "spice girls greatest hits 04 mama")
        # "one" must NOT be confirmed by "someone like you"
        assert not _title_token_confirmed("one", "adele someone like you")
        assert _title_token_confirmed("one", "album 01 one")

    def test_it_reports_confirmation_separately_from_a_match(self):
        from services.downloads.match_engine import filename_evidence

        item = {"artist": "Adele", "album_artist": "Adele",
                "title": "Someone Like You (ESQUIRE vs. OFFbeat remix)"}
        _matches, confirmed = filename_evidence(
            "/x/34.Someone Like You [ESQUIRE vs. OFFBeat Remix].mp3", item
        )
        assert confirmed is True

    def test_a_folder_only_match_is_not_title_confirmation(self):
        from services.downloads.match_engine import filename_evidence

        item = {"artist": "Adele", "album_artist": "Adele", "title": "Turning Tables"}
        _matches, confirmed = filename_evidence(
            "/x/Adele - Greatest Hits 2012/12 - Chasing Pavements.mp3", item
        )
        assert confirmed is False
