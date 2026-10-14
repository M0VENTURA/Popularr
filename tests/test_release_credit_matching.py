"""Same-titled release rows are matched by ARTIST CREDIT, not just position.

Reported
--------
> It looks like track artists are being ignored when matching mbid's via
> listenbrainz and only using album artists

Measured on Electric Callboy's "MMXX - Hypa Hypa Edition" (MusicBrainz release
``5a449751-55e6-41d9-a1e6-93d8934f33e4``): the release lists eight "Hypa Hypa"
tracks — seven sharing the plain title (the original at position 1, plus
Saltatio Mortis, The BossHoss, 257ers, Sasha, We Butter the Bread With Butter
and Axel One) and the Gestört aber GeiL remix. Only the ARTIST CREDIT
separates the seven, and the library carries that credit in each track's own
``artist`` field.

Two defects made every version read as one track:

1. The album-tracklist consumer read the TITLE key, so all eight rows took
   whichever version the loop reached LAST — the reported log line repeated
   ``186 listens`` for six different rows, all sharing one recording.
2. Nothing compared artist credits — the release tracklist was not even
   INDEXED with them, and the MusicBrainz tracklist was fetched without
   ``artist-credits`` — so a row that position/duration could not decide had
   no second discriminator at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: One recording MBID per version, in release order.
HYPA = "3b548ac2-17cf-41af-8bd6-5a5248c7e8c"          # pos 1 — the original
HATE_LOVE = "c767e375-7688-4ba0-adb7-a6f5c806cb47"    # pos 2
MC_THUNDER = "46739381-dc3b-4c04-9499-e704beb8c059"   # pos 3
MOUSTACHE = "7b2d1ff8-7b1f-4177-aed4-4ccf67ee8321"    # pos 4
DRAMAQUEEN = "59f9d2aa-9fbb-4173-8612-d0cab0d16c1c"   # pos 5
PRISM = "ca40e95b-9819-4cb2-8430-c013506ce62f"        # pos 6
HYPA_SALTATIO = "01b92c32-a1bc-4f74-ba73-b13cb96c3272"     # pos 7
HYPA_BOSSHOSS = "1ee40ae2-88e-48f1-a280-e5518eb2a522"      # pos 8
HYPA_257ERS = "80c0a2b3-39b1-4207-ad2b-b0faa0afb63"        # pos 9
HYPA_SASHA = "04e5e70c-b48a-472f-b120-8c9903938ac9"        # pos 10
HYPA_REMIX = "c6199dd2-1446-4517-a5a2-e3efee16ffe4"        # pos 11
HYPA_WBTBWB = "e561976a-8b8a-469a-9499-5ee7161a7d31"       # pos 12
HYPA_AXEL = "4f26b31-b2ca-4b43-9ff5-bdd5d82bde76"          # pos 13

#: (position, title, credit parts, recording mbid, length ms) — the real
#: tracklist of "MMXX - Hypa Hypa Edition".
RELEASE_ROWS: list[tuple[int, str, list[str], str, int]] = [
    (1, "Hypa Hypa", ["Electric Callboy"], HYPA, 213000),
    (2, "Hate/Love", ["Electric Callboy"], HATE_LOVE, 188000),
    (3, "MC Thunder II (Dancing Like a Ninja)", ["Electric Callboy"], MC_THUNDER, 248000),
    (4, "Monsieur Moustache", ["Electric Callboy"], MOUSTACHE, 175000),
    (5, "Dramaqueen", ["Electric Callboy"], DRAMAQUEEN, 214000),
    (6, "Prism", ["Electric Callboy", "Tobias Rauscher"], PRISM, 202000),
    (7, "Hypa Hypa", ["Electric Callboy", "Saltatio Mortis"], HYPA_SALTATIO, 184533),
    (8, "Hypa Hypa", ["Electric Callboy", "The BossHoss"], HYPA_BOSSHOSS, 178000),
    (9, "Hypa Hypa", ["Electric Callboy", "257ers"], HYPA_257ERS, 120000),
    (10, "Hypa Hypa", ["Electric Callboy", "Sasha"], HYPA_SASHA, 176906),
    (11, "Hypa Hypa (Gestört aber Geil remix)", ["Electric Callboy", "Gestört aber GeiL"], HYPA_REMIX, 149560),
    (12, "Hypa Hypa", ["Electric Callboy", "We Butter the Bread With Butter"], HYPA_WBTBWB, 241546),
    (13, "Hypa Hypa", ["Electric Callboy", "Axel One"], HYPA_AXEL, 210960),
]


def _mb_track(position: int, title: str, credit: list[str], mbid: str, length: int) -> dict:
    """One MusicBrainz-shaped track: names joined by ``joinphrase``."""
    return {
        "position": position,
        "number": str(position),
        "title": title,
        "length": length,
        "recording": {"id": mbid},
        "artist-credit": [
            {
                "name": part,
                "joinphrase": ", " if index < len(credit) - 1 else "",
                "artist": {"name": part},
            }
            for index, part in enumerate(credit)
        ],
    }


def _media_payload() -> list[dict]:
    return [{
        "position": 1,
        "tracks": [
            _mb_track(pos, title, credit, mbid, length)
            for pos, title, credit, mbid, length in RELEASE_ROWS
        ],
    }]


#: The library as reported: every "Hypa Hypa" version is titled plainly and
#: carries its version's credit in the track artist field.
LOCAL_TRACKS: list[dict] = [
    {"title": "Hypa Hypa", "artist": "Electric Callboy",
     "disc_number": 1, "track_number": 1, "duration": 213},
    {"title": "Hate/Love", "artist": "Electric Callboy",
     "disc_number": 1, "track_number": 2, "duration": 188},
    {"title": "MC Thunder II (Dancing Like a Ninja)", "artist": "Electric Callboy",
     "disc_number": 1, "track_number": 3, "duration": 248},
    {"title": "Monsieur Moustache", "artist": "Electric Callboy",
     "disc_number": 1, "track_number": 4, "duration": 175},
    {"title": "Dramaqueen", "artist": "Electric Callboy",
     "disc_number": 1, "track_number": 5, "duration": 214},
    {"title": "Prism", "artist": "Electric Callboy feat. Tobias Rauscher",
     "disc_number": 1, "track_number": 6, "duration": 202},
    {"title": "Hypa Hypa", "artist": "Electric Callboy feat. Saltatio Mortis",
     "disc_number": 1, "track_number": 7, "duration": 185},
    {"title": "Hypa Hypa", "artist": "Electric Callboy feat. The BossHoss",
     "disc_number": 1, "track_number": 8, "duration": 178},
    {"title": "Hypa Hypa", "artist": "Electric Callboy, 257ers",
     "disc_number": 1, "track_number": 9, "duration": 120},
    {"title": "Hypa Hypa", "artist": "Electric Callboy feat. Sasha",
     "disc_number": 1, "track_number": 10, "duration": 177},
    {"title": "Hypa Hypa (Gestört aber Geil remix)", "artist": "Electric Callboy, Gestört aber GeiL",
     "disc_number": 1, "track_number": 11, "duration": 150},
    {"title": "Hypa Hypa", "artist": "Electric Callboy, We Butter the Bread With Butter",
     "disc_number": 1, "track_number": 12, "duration": 242},
    {"title": "Hypa Hypa", "artist": "Electric Callboy, Axel One",
     "disc_number": 1, "track_number": 13, "duration": 211},
]


def _index():
    from services.popularity.popularity_sources import _index_release_tracklist

    titles_to_mbids: dict = {}
    position_index: dict = {}
    recording_mbids: list = []
    _index_release_tracklist(
        _media_payload(), titles_to_mbids, position_index, recording_mbids
    )
    return titles_to_mbids, position_index, recording_mbids


def _producer(monkeypatch, tracks: list[dict], counts: dict | None = None):
    """Run the album-tracklist pass against the controlled release."""
    from services.popularity import popularity_sources as ps

    monkeypatch.setattr(ps, "_resolve_release_mbid", lambda artist, album, t: "rel-1")
    monkeypatch.setattr(
        ps, "lb_get_release_metadata_batch",
        lambda mbids, **kwargs: {"rel-1": {"media": _media_payload()}},
    )
    monkeypatch.setattr(
        ps, "lb_get_recording_popularity_batch", lambda mbids: dict(counts or {})
    )
    out, release_mbid = ps.get_listenbrainz_album_tracklist_with_release(
        "Electric Callboy", "MMXX - Hypa Hypa Edition", tracks
    )
    return out, release_mbid


def _row(out: dict, title: str, track_number: int) -> dict:
    from services.popularity.popularity_sources import (
        normalize_for_aggregation,
        track_identity_key,
    )

    return out.get(track_identity_key(normalize_for_aggregation(title), 1, track_number)) or {}


# ===========================================================================
# 1. The credit helpers
# ===========================================================================


class TestTheCreditIsReadFromTheRelease:
    def test_a_multi_artist_credit_is_joined(self):
        from services.popularity.popularity_sources import _release_credit_text

        track = _mb_track(7, "Hypa Hypa", ["Electric Callboy", "Saltatio Mortis"], HYPA_SALTATIO, 184533)

        assert _release_credit_text(track) == "Electric Callboy, Saltatio Mortis"

    def test_the_artist_name_is_the_fallback(self):
        from services.popularity.popularity_sources import _release_credit_text

        track = {"artist-credit": [{"artist": {"name": "Electric Callboy"}}]}

        assert _release_credit_text(track) == "Electric Callboy"

    def test_a_missing_credit_is_empty(self):
        from services.popularity.popularity_sources import _release_credit_text

        assert _release_credit_text({}) == ""


class TestCreditTokensCompareExactly:
    """The comparison the artist pass runs: EXACT token equality."""

    def _tokens(self, value: str):
        from services.popularity.popularity_sources import _credit_identity_tokens

        return _credit_identity_tokens(value)

    def test_join_spellings_are_equivalent(self):
        """", " and " feat. " say the same thing about WHO is on the track."""
        assert self._tokens("Electric Callboy, Saltatio Mortis") == self._tokens(
            "Electric Callboy feat. Saltatio Mortis"
        )
        assert self._tokens("Electric Callboy & The BossHoss") == self._tokens(
            "Electric Callboy, The BossHoss"
        )

    def test_a_plain_credit_is_not_equal_to_a_featured_one(self):
        """THE ambiguity the pass must not resolve wrongly: a plainly tagged
        track could be ANY of the versions, so the tokens must not be equal."""
        assert self._tokens("Electric Callboy") != self._tokens(
            "Electric Callboy, Saltatio Mortis"
        )

    def test_two_different_features_are_not_equal(self):
        assert self._tokens("Electric Callboy, 257ers") != self._tokens(
            "Electric Callboy, Sasha"
        )

    def test_an_alias_resolves_both_sides(self):
        """A credit under the band's FORMER name is the same artist — the
        alias map the artist page manages is what makes that true."""
        from services.metadata.artist_alias_service import (
            add_alias,
            invalidate_alias_cache,
        )

        try:
            add_alias("Electric Callboy", "Eskimo Callboy")

            assert self._tokens("Eskimo Callboy, Saltatio Mortis") == self._tokens(
                "Electric Callboy feat. Saltatio Mortis"
            )
        finally:
            invalidate_alias_cache()

    def test_an_empty_credit_yields_nothing(self):
        assert self._tokens("") == frozenset()


class TestTheIndexCarriesCredits:
    def test_every_row_keeps_its_own_credit(self):
        titles_to_mbids, _positions, _mbids = _index()

        rows = titles_to_mbids["hypa hypa"]["rows"]
        by_mbid = {row["mbid"]: row["credit"] for row in rows}

        assert by_mbid[HYPA] == "Electric Callboy"
        assert by_mbid[HYPA_SALTATIO] == "Electric Callboy, Saltatio Mortis"
        assert by_mbid[HYPA_WBTBWB] == "Electric Callboy, We Butter the Bread With Butter"
        assert len(rows) == 7, "seven of the release's eight 'Hypa Hypa' tracks share the plain title"

    def test_the_position_entry_carries_its_credit_too(self):
        _titles, position_index, _mbids = _index()

        assert position_index[(1, 7)]["credit"] == "Electric Callboy, Saltatio Mortis"
        assert position_index[(1, 12)]["credit"] == "Electric Callboy, We Butter the Bread With Butter"


# ===========================================================================
# 2. The reported case: eight versions, eight identities
# ===========================================================================


class TestEveryVersionGetsItsOwnRecording:
    def test_each_row_keeps_its_own_recording(self, monkeypatch):
        """THE REPORTED DEFECT. Every row must resolve to ITS OWN version."""
        out, _release = _producer(monkeypatch, LOCAL_TRACKS)

        assert _row(out, "Hypa Hypa", 1)["recording_mbid"] == HYPA
        assert _row(out, "Hypa Hypa", 7)["recording_mbid"] == HYPA_SALTATIO
        assert _row(out, "Hypa Hypa", 8)["recording_mbid"] == HYPA_BOSSHOSS
        assert _row(out, "Hypa Hypa", 9)["recording_mbid"] == HYPA_257ERS
        assert _row(out, "Hypa Hypa", 10)["recording_mbid"] == HYPA_SASHA
        assert _row(out, "Hypa Hypa", 12)["recording_mbid"] == HYPA_WBTBWB
        assert _row(out, "Hypa Hypa", 13)["recording_mbid"] == HYPA_AXEL

    def test_the_recordings_are_all_different(self, monkeypatch):
        """The symptom was eight rows sharing ONE recording."""
        out, _release = _producer(monkeypatch, LOCAL_TRACKS)

        mbids = {
            _row(out, "Hypa Hypa", pos)["recording_mbid"]
            for pos in (1, 7, 8, 9, 10, 12, 13)
        }

        assert len(mbids) == 7, f"each version must have its own recording, got {mbids}"

    def test_the_remix_keeps_its_own_title_and_recording(self, monkeypatch):
        """Its title is UNIQUE (locally and on the release), so the title pass
        answers it directly — no alias needed."""
        from services.popularity.popularity_sources import normalize_for_aggregation

        out, _release = _producer(monkeypatch, LOCAL_TRACKS)

        remix = out[normalize_for_aggregation("Hypa Hypa (Gestört aber Geil remix)")]
        assert remix["recording_mbid"] == HYPA_REMIX

    def test_the_artist_match_is_recorded_as_such(self, monkeypatch):
        """The entry says HOW it was identified, so the scan log can show it."""
        out, _release = _producer(monkeypatch, LOCAL_TRACKS)

    #: The listens the reported scan logged, one per version.
    COUNTS = {
        HYPA: {"total_listen_count": 7568, "total_user_count": 100},
        HYPA_SALTATIO: {"total_listen_count": 260, "total_user_count": 10},
        HYPA_BOSSHOSS: {"total_listen_count": 36, "total_user_count": 2},
        HYPA_257ERS: {"total_listen_count": 186, "total_user_count": 5},
        HYPA_SASHA: {"total_listen_count": 41, "total_user_count": 3},
        HYPA_WBTBWB: {"total_listen_count": 216, "total_user_count": 6},
        HYPA_AXEL: {"total_listen_count": 85, "total_user_count": 4},
    }

    def test_the_title_key_keeps_the_title_pass_answer(self, monkeypatch):
        """⚠️ The reported symptom: eight rows wrote the SAME title key, so the
        last one processed (position 13's Axel One version) spoke for all of
        them. The title key must stay the deterministic answer the TITLE pass
        resolved — the release's FIRST row with that title."""
        out, _release = _producer(monkeypatch, LOCAL_TRACKS, self.COUNTS)

        assert out["hypa hypa"]["recording_mbid"] == HYPA, (
            "the title key must not be overwritten by whichever row was "
            "processed last"
        )
        # The count on a SHARED title key is the pre-existing sum over every
        # candidate for that title (7568 + 260 + 36 + 186 + 41 + 216 + 85).
        # It is a fallback value only — the per-row aliases below carry each
        # version's own number, and the consumer reads those first.
        assert out["hypa hypa"]["listenbrainz_listens"] == 8392

    def test_each_row_still_reads_its_own_count(self, monkeypatch):
        """The per-row aliases carry the version's OWN listens — the value the
        reported log repeated as 186 for six different rows."""
        out, _release = _producer(monkeypatch, LOCAL_TRACKS, self.COUNTS)

        assert _row(out, "Hypa Hypa", 1)["listenbrainz_listens"] == 7568
        assert _row(out, "Hypa Hypa", 8)["listenbrainz_listens"] == 36
        assert _row(out, "Hypa Hypa", 9)["listenbrainz_listens"] == 186
        assert _row(out, "Hypa Hypa", 13)["listenbrainz_listens"] == 85
        assert out["hypa hypa"]["recording_mbid"] == HYPA, (
            "the title key must not be overwritten by whichever row was "
            "processed last"
        )

    def test_a_unique_title_is_still_written_by_the_position_pass(self, monkeypatch):
        """CONTROL — the guard only applies to a SHARED local title."""
        tracks = [
            {"title": "Monsieur Moustache", "artist": "Electric Callboy",
             "disc_number": 1, "track_number": 4, "duration": 175},
        ]

        out, _release = _producer(monkeypatch, tracks)

        assert out["monsieur moustache"]["recording_mbid"] == MOUSTACHE


# ===========================================================================
# 3. The artist pass stays conservative
# ===========================================================================


class TestTheArtistPassRefusesToGuess:
    def test_a_plain_credit_does_not_claim_a_featured_version(self, monkeypatch):
        """A track tagged with just the album artist could be ANY version, so
        the artist pass must not pick one — it falls to position."""
        tracks = [
            {"title": "Hypa Hypa", "artist": "Electric Callboy",
             "disc_number": 1, "track_number": 9, "duration": 120},
            {"title": "Hypa Hypa", "artist": "Electric Callboy",
             "disc_number": 1, "track_number": 10, "duration": 177},
        ]

        out, _release = _producer(monkeypatch, tracks)

        assert _row(out, "Hypa Hypa", 9).get("matched_by") != "artist"
        assert _row(out, "Hypa Hypa", 10).get("matched_by") != "artist"

    def test_an_unknown_artist_gets_no_artist_match(self, monkeypatch):
        tracks = [
            {"title": "Hypa Hypa", "artist": "Somebody Else",
             "disc_number": 1, "track_number": 9, "duration": 120},
            {"title": "Hypa Hypa", "artist": "Somebody Else",
             "disc_number": 1, "track_number": 10, "duration": 177},
        ]

        out, _release = _producer(monkeypatch, tracks)

        assert _row(out, "Hypa Hypa", 9).get("matched_by") != "artist"

    def test_a_misnumbered_row_is_rescued_by_its_credit(self, monkeypatch):
        """The credit is a SECOND discriminator, not a decoration: a folder
        whose numbering does not line up with the release (missing tracks,
        a different edition) is identified by the credit alone."""
        tracks = [
            # The BossHoss version, sitting at local position 7 — where the
            # release has the Saltatio Mortis version (and the durations
            # disagree, so the position guard refuses it).
            {"title": "Hypa Hypa", "artist": "Electric Callboy, The BossHoss",
             "disc_number": 1, "track_number": 7, "duration": 178},
            {"title": "Hypa Hypa", "artist": "Electric Callboy, Sasha",
             "disc_number": 1, "track_number": 8, "duration": 177},
        ]

        out, _release = _producer(monkeypatch, tracks)

        assert _row(out, "Hypa Hypa", 7)["recording_mbid"] == HYPA_BOSSHOSS
        assert _row(out, "Hypa Hypa", 8)["recording_mbid"] == HYPA_SASHA

    def test_a_missing_position_is_left_alone(self, monkeypatch):
        """No position means no row-qualified key — writing the plain title
        key would hand this row's answer to every sibling."""
        tracks = [
            {"title": "Hypa Hypa", "artist": "Electric Callboy, 257ers",
             "disc_number": 1, "track_number": 0, "duration": 120},
            {"title": "Hypa Hypa", "artist": "Electric Callboy, Sasha",
             "disc_number": 1, "track_number": 0, "duration": 177},
        ]

        out, _release = _producer(monkeypatch, tracks)

        assert out.get("hypa hypa", {}).get("matched_by") != "artist"


class TestTheLengthGuardStillDecidesTheTitle:
    def test_an_agreeing_length_confirms_the_release_title(self, monkeypatch):
        out, _release = _producer(monkeypatch, LOCAL_TRACKS)

        assert _row(out, "Hypa Hypa", 7)["length_verified"] is True

    def test_a_disagreeing_length_does_not(self, monkeypatch):
        """The credit names the RECORDING; the length still has to bless the
        TITLE it carries before the batch may auto-apply it."""
        tracks = [
            {"title": "Hypa Hypa", "artist": "Electric Callboy, Saltatio Mortis",
             "disc_number": 1, "track_number": 7, "duration": 400},
            {"title": "Hypa Hypa", "artist": "Electric Callboy, Sasha",
             "disc_number": 1, "track_number": 10, "duration": 400},
        ]

        out, _release = _producer(monkeypatch, tracks)

        row = _row(out, "Hypa Hypa", 7)
        assert row["recording_mbid"] == HYPA_SALTATIO, "the credit still identifies it"
        assert row["length_verified"] is False


# ===========================================================================
# 4. The identity reaches the track (the consumer contract)
# ===========================================================================


class _FakeMbService:
    def __init__(self, entries: dict):
        self.entries = entries

    def lookup_recordings_by_mbid_bulk(self, mbids, album_name=None):
        return {m: dict(self.entries.get(m) or {}) for m in mbids if m in self.entries}


class TestTheBatchGetsEachRowsOwnRecording:
    def test_the_alias_entry_carries_the_row_identity(self, monkeypatch):
        """``_build_album_recording_batch`` reads the per-row alias FIRST, so
        the identity the album-tracklist pass resolved must be reachable there
        — that is how it becomes ``tracks.recording_mbid``."""
        from services.popularity import scan_stage_runner as runner
        from services.popularity.popularity_sources import (
            album_recording_batch_key,
            normalize_for_aggregation,
            track_identity_key,
        )

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service",
            lambda: _FakeMbService({
                HYPA_SALTATIO: {"recording_mbid": HYPA_SALTATIO, "title": "Hypa Hypa"},
                HYPA_BOSSHOSS: {"recording_mbid": HYPA_BOSSHOSS, "title": "Hypa Hypa"},
            }),
        )

        key = normalize_for_aggregation("Hypa Hypa")
        prefetched = {
            key: {"recording_mbid": HYPA, "release_track_title": "Hypa Hypa"},
            track_identity_key(key, 1, 7): {
                "recording_mbid": HYPA_SALTATIO,
                "release_track_title": "Hypa Hypa",
                "length_verified": True,
            },
            track_identity_key(key, 1, 8): {
                "recording_mbid": HYPA_BOSSHOSS,
                "release_track_title": "Hypa Hypa",
                "length_verified": True,
            },
        }
        tracks = [
            {"title": "Hypa Hypa", "artist": "Electric Callboy feat. Saltatio Mortis",
             "disc_number": 1, "track_number": 7},
            {"title": "Hypa Hypa", "artist": "Electric Callboy feat. The BossHoss",
             "disc_number": 1, "track_number": 8},
        ]

        batch = runner._build_album_recording_batch(
            album="MMXX - Hypa Hypa Edition",
            track_dicts=tracks,
            prefetched_popularity=prefetched,
        )

        saltatio = batch.get(album_recording_batch_key(
            "Electric Callboy feat. Saltatio Mortis", "Hypa Hypa", 1, 7
        ))
        bosshoss = batch.get(album_recording_batch_key(
            "Electric Callboy feat. The BossHoss", "Hypa Hypa", 1, 8
        ))

        assert saltatio and saltatio["recording_mbid"] == HYPA_SALTATIO
        assert bosshoss and bosshoss["recording_mbid"] == HYPA_BOSSHOSS

    def test_a_same_titled_row_without_an_alias_is_not_guessed(self, monkeypatch):
        """CONTROL — with no per-row identity, a duplicated local title must
        stay unresolved rather than inherit a sibling's recording."""
        from services.popularity import scan_stage_runner as runner
        from services.popularity.popularity_sources import (
            album_recording_batch_key,
            normalize_for_aggregation,
            track_identity_key,
        )

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service",
            lambda: _FakeMbService({HYPA_SALTATIO: {"recording_mbid": HYPA_SALTATIO}}),
        )

        key = normalize_for_aggregation("Hypa Hypa")
        prefetched = {
            key: {"recording_mbid": HYPA, "release_track_title": "Hypa Hypa"},
            track_identity_key(key, 1, 7): {
                "recording_mbid": HYPA_SALTATIO,
                "release_track_title": "Hypa Hypa",
                "length_verified": True,
            },
        }
        tracks = [
            {"title": "Hypa Hypa", "artist": "Electric Callboy feat. Saltatio Mortis",
             "disc_number": 1, "track_number": 7},
            {"title": "Hypa Hypa", "artist": "Electric Callboy",
             "disc_number": 1, "track_number": 8},
        ]

        batch = runner._build_album_recording_batch(
            album="MMXX - Hypa Hypa Edition",
            track_dicts=tracks,
            prefetched_popularity=prefetched,
        )

        assert album_recording_batch_key(
            "Electric Callboy feat. Saltatio Mortis", "Hypa Hypa", 1, 7
        ) in batch
        assert album_recording_batch_key(
            "Electric Callboy", "Hypa Hypa", 1, 8
        ) not in batch, "the row with no alias must not inherit its sibling's recording"


class TestTheConsumerReadsTheRowAlias:
    """The album-tracklist cache loop lives inside the scan's per-album body,
    so its wiring is pinned here rather than driven end to end."""

    @staticmethod
    def _source() -> str:
        return (
            Path(__file__).resolve().parents[1]
            / "services" / "popularity" / "scan_stage_runner.py"
        ).read_text(encoding="utf-8")

    def test_the_row_alias_is_read_before_the_title_key(self):
        source = self._source()

        assert "_row_entry = _album_lb_by_title.get(_alias_key)" in source
        assert "_entry = _row_entry or _album_lb_by_title.get(_key)" in source, (
            "reading the title key alone gave every same-titled row one "
            "version's identity — the reported '186 listens' for six rows"
        )

    def test_the_row_identity_is_mirrored_into_its_own_slot(self):
        source = self._source()

        assert '_row = prefetched_popularity.setdefault(_alias_key, {})' in source, (
            "the batch builder reads the alias first, so the per-row identity "
            "must be written there or it never reaches the track"
        )

    def test_the_log_shows_which_path_answered(self):
        source = self._source()

        assert "via={'row-alias' if _row_entry else 'title'}" in source, (
            "the log line is how the fix is verified on a real scan"
        )


class TestTheMusicBrainzTracklistCarriesCredits:
    def test_the_release_fetch_requests_artist_credits(self):
        """Without ``artist-credits`` the index has no credits to compare and
        the artist pass can never fire — this is the enabling change."""
        source = (
            Path(__file__).resolve().parents[1]
            / "services" / "popularity" / "popularity_sources.py"
        ).read_text(encoding="utf-8")

        assert 'inc="recordings+artist-credits"' in source
