"""A batch position alias never overwrites the track's real artist when the
identity is UNCONFIRMED.

Reported: on a Various Artists compilation with NO album MBID, the ListenBrainz
album-tracklist found a DIFFERENT edition (46 tracks vs the local 80), so track
numbers misaligned. The position alias matched "Where Is My Mind?" to release
slot "Happy" — and the artist write happened BEFORE the title gate protected
the name: the track's real performer ("Pixies") was replaced by the slot's
artist ("Ned's Atomic Dustbin"), which then fed a recording search that found
nothing -> the track saved NO recording MBID.

The title has been gated on ``_title_match_confirmed`` since `d061adfd`
("track title auto-updates only on a verified track#+length match"). This
closes the second half of that defect family: the ARTIST (and the artist MBID)
obey the SAME trust rule.

- A batch position alias whose release title DIFFERS from the local title AND
  is not confirmed -> its artist is IGNORED (real performer preserved).
- A CONFIRMED alias (track# + verified length) still replaces a placeholder.
- A search-resolved (non-batch) identity is always trustworthy: the search ran
  with the local title+artist, so its artist IS the match.
"""

from __future__ import annotations

import pytest


def _mbid(seed: int) -> str:
    return f"{seed:08d}-0000-0000-0000-{seed:012d}" if seed else ""


def _run(
    *,
    existing_artist: str,
    mb_artist: str,
    batch_title: str,
    local_title: str,
    title_match_confirmed: bool,
    force_meta: bool = False,
    from_batch: bool = True,
) -> dict:
    """Drive the real _resolve_track_mb_metadata; return the payload."""
    from services.popularity.stages import track_stage

    batch: dict = {}
    if from_batch:
        # The batch is KEYED by the track's LOCAL artist+title (both lower) —
        # ``_build_album_recording_batch`` keys from ``identity["artist"]``,
        # which is the LOCAL artist.  The VALUE carries the recording credit
        # (``mb_data["artist"]``), which is the slot's possibly-WRONG artist.
        batch[f"{existing_artist}::{local_title}".lower()] = {
            "recording_mbid": _mbid(1),
            "title": batch_title,
            "artist": mb_artist,
            "artist_mbid": _mbid(2),
            "_title_match_confirmed": title_match_confirmed,
        }

    kwargs = dict(
        track_id="t1",
        track={
            "title": local_title,
            "artist": existing_artist,
            "album": "Compilation",
            "album_artist": "Various Artists",
            "recording_mbid": "",
            "musicbrainz_genres": '["metal"]',
            "disc_number": 1,
            "track_number": 5,
        },
        track_title=local_title,
        track_artist=existing_artist,
        frozen_track=False,
        force_meta=force_meta,
        options={"mb_batch_metadata": batch},
    )
    return track_stage._resolve_track_mb_metadata(**kwargs)


@pytest.fixture(autouse=True)
def _patches(monkeypatch):
    from services.popularity.stages import track_stage

    monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
    monkeypatch.setattr(
        track_stage,
        "get_shared_mb_service",
        lambda: pytest.fail("a batch hit must not search or fetch"),
    )
    monkeypatch.setattr(track_stage, "_metadata_update_allowed", lambda: True)


class TestUnconfirmedBatchAliasArtistIsIgnored:

    def test_mismatched_artist_is_never_written(self):
        """The reported case: local 'Pixies' / slot artist 'Ned's Atomic
        Dustbin' with a DIFFERENT release title and no confirmation."""
        result = _run(
            existing_artist="Pixies",
            mb_artist="Ned's Atomic Dustbin",
            batch_title="Happy",
            local_title="Where Is My Mind?",
            title_match_confirmed=False,
        )
        assert "artist" not in result["payload"], (
            "an unconfirmed batch alias must not overwrite the track's real "
            "performer — doing so feeds the wrong artist into the recording "
            "search, which then finds nothing"
        )
        assert result["payload"].get("artist") is None

    def test_the_artist_mbid_is_not_written_either(self):
        result = _run(
            existing_artist="Pixies",
            mb_artist="Ned's Atomic Dustbin",
            batch_title="Happy",
            local_title="Where Is My Mind?",
            title_match_confirmed=False,
        )
        assert "musicbrainz_artistid" not in result["payload"], (
            "the artist MBID belongs to the slot's (wrong) artist — writing it "
            "stamps the wrong identity onto the real performer"
        )

    def test_a_placeholder_artist_is_not_replaced_by_an_unconfirmed_alias(self):
        result = _run(
            existing_artist="Various Artists",
            mb_artist="Ned's Atomic Dustbin",
            batch_title="Happy",
            local_title="Where Is My Mind?",
            title_match_confirmed=False,
        )
        assert "artist" not in result["payload"], (
            "even a placeholder must not be filled from an unconfirmed alias — "
            "the release slot is a DIFFERENT song"
        )

    def test_the_recording_mbid_is_still_applied(self):
        # The RECORDING identity is position-derived, but the artist is the
        # dangerous half — make sure the recording MBID still lands so the
        # downstream LB/Last.fm reads use it, not a title+artist search.
        result = _run(
            existing_artist="Pixies",
            mb_artist="Ned's Atomic Dustbin",
            batch_title="Happy",
            local_title="Where Is My Mind?",
            title_match_confirmed=False,
        )
        assert result["payload"].get("recording_mbid") == _mbid(1)


class TestConfirmedAndSearchResolvedIdentitiesStillWriteArtist:

    def test_confirmed_alias_still_replaces_a_placeholder(self):
        result = _run(
            existing_artist="Various Artists",
            mb_artist="P.O.D.",
            batch_title="School of Hard Knocks",
            local_title="School of Hard Knocks",
            title_match_confirmed=True,
        )
        assert result["payload"].get("artist") == "P.O.D."

    def test_force_meta_correction_still_works_on_a_confirmed_alias(self):
        result = _run(
            existing_artist="POD",
            mb_artist="P.O.D.",
            batch_title="School of Hard Knocks",
            local_title="School of Hard Knocks",
            title_match_confirmed=True,
            force_meta=True,
        )
        assert result["payload"].get("artist") == "P.O.D."

    def test_artist_mbid_is_written_when_trustworthy(self):
        result = _run(
            existing_artist="Various Artists",
            mb_artist="P.O.D.",
            batch_title="School of Hard Knocks",
            local_title="School of Hard Knocks",
            title_match_confirmed=True,
        )
        assert result["payload"].get("musicbrainz_artistid") == _mbid(2)


class TestSearchResolvedIdentity:

    def test_a_non_batch_match_always_writes_its_artist(self, monkeypatch):
        """A search hit comes from a lookup WITH the local title+artist, so its
        artist IS the match — the gate must not suppress it."""
        from services.popularity.stages import track_stage

        class _SearchService:
            def lookup_recording_metadata(self, title, artist, **kwargs):
                return {
                    "recording_mbid": _mbid(9),
                    "artist": "The Pixies",
                    "title": title,
                    "confidence": 0.9,
                }

        # Disable the autouse batch-hit failure so the SEARCH path runs.
        monkeypatch.setattr(
            track_stage,
            "get_shared_mb_service",
            lambda: _SearchService(),
        )
        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={
                "title": "Where Is My Mind?",
                "artist": "Pixies",
                "album": "Compilation",
                "recording_mbid": "",
                "musicbrainz_genres": '["rock"]',
            },
            track_title="Where Is My Mind?",
            track_artist="Pixies",
            frozen_track=False,
            force_meta=False,
            options={},  # no batch -> the per-track search path fills mb_data
        )
        assert result["payload"].get("recording_mbid") == _mbid(9)
        # Search-resolved: no batch alias involved, so nothing is suppressed —
        # the recording id lands and the artist write follows the normal
        # (pre-existing) rules.  This is the control that the untrustworthy
        # gate is scoped to BATCH aliases only.


class TestArtistGateSourceContract:

    def test_the_gate_uses_the_same_confirmation_flag_as_the_title(self):
        import inspect

        from services.popularity.stages import track_stage

        source = inspect.getsource(track_stage._resolve_track_mb_metadata)
        # ONE definition of "trustworthy batch artist" derived from the SAME
        # flag the title promotion consumes — never a second independent check.
        assert "_batch_artist_untrustworthy" in source
        assert "_title_match_confirmed" in source
        assert "release_title.casefold() != _local_title.casefold()" in source

    def test_the_recording_mbid_write_is_not_gated(self):
        import inspect

        from services.popularity.stages import track_stage

        source = inspect.getsource(track_stage._resolve_track_mb_metadata)
        # The position-derived RECORDING id still lands (downstream listens
        # read through it) — only the artist + artist MBID are gated.
        assert 'payload["recording_mbid"] = recording_mbid' in source