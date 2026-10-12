"""A sparse library must not position-match a unique-titled track to a WRONG
release slot.

REPORTED
--------
> It's detecting wrong information for The Power of Love, but Love Shack is
> track 5 and still in the queue to be downloaded so it's incorrectly matching
> and trying to change the wrong track.

The library folder for "Take 40 Australia: The Number Ones" is missing "Love
Shack" (release track 5, still queued), so the files are numbered 1..M while
those recordings sit at release positions that no longer align. The local file
"The Power of Love" (release position 13) sits at LOCAL position 5.

The position-alias loop emitted a position-qualified identity for EVERY row,
even when the title-keyed pass had ALREADY resolved the track by its unique
title. So local#5 got aliased to release#5 = "Love Shack" — the wrong
recording and the wrong release title — and the album batch (which prefers
the position alias) proposed changing "The Power of Love" to "Love Shack".

The right principle is the one the title-fill loop already used: a title that
identifies exactly ONE release row has been answered — a position alias must
not contradict it. Same-titled pairs still get their position aliases
(``title_is_unique`` False), and version-marker tracks whose title is absent
from the release keep theirs too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Release identity: Love Shack is track 5, The Power of Love is track 13.
LOVE_SHACK_MBID = "aaaaaaaa-aaa1-4a01-8ae1-000000000001"
POWER_OF_LOVE_MBID = "bbbbbbbb-bbb2-4b02-8be2-000000000002"

RELEASE_TRACKS = [
    (1, 1, "Funky Town", "aaaaaaa1-0000-0000-0000-000000000001", 240000),
    (1, 2, "U Can't Touch This", "aaaaaaa2-0000-0000-0000-000000000002", 257000),
    (1, 3, "Simply Irresistible", "aaaaaaa3-0000-0000-0000-000000000003", 250000),
    (1, 4, "Groove Is in the Heart", "aaaaaaa4-0000-0000-0000-000000000004", 292000),
    (1, 5, "Love Shack", LOVE_SHACK_MBID, 317000),
    (1, 6, "Venus", "aaaaaaa6-0000-0000-0000-000000000006", 212000),
    (1, 7, "Walk Like an Egyptian", "aaaaaaa7-0000-0000-0000-000000000007", 200000),
    (1, 13, "The Power of Love", POWER_OF_LOVE_MBID, 238000),
]


def _media_payload() -> list[dict]:
    return [{
        "position": 1,
        "tracks": [
            {
                "position": pos,
                "number": str(pos),
                "title": title,
                "length": length,
                "recording": {"id": mbid},
            }
            for _d, pos, title, mbid, length in RELEASE_TRACKS
        ],
    }]


# The local library: Love Shack is MISSING (queued), so The Power of Love is
# the local file at number 5 (the folder was scanned before the download).
#
# The Power of Love's DURATION is intentionally OMITTED here — that is the
# production trigger for the reported wrong match. The position pass SKIPS its
# length guard when either side's duration is missing (a 0/None value reads
# as "can't compare" and the alias is emitted WITHOUT length verification →
# the recommendation branch). With both lengths present and differing >5s the
# alias would be skipped harmlessly even at baseline, so a fixture with a
# present duration would not reproduce the report.
LOCAL_TRACKS = [
    {"title": "Funky Town", "track_number": 1, "disc_number": 1, "duration": 240},
    {"title": "U Can't Touch This", "track_number": 2, "disc_number": 1, "duration": 257},
    {"title": "Simply Irresistible", "track_number": 3, "disc_number": 1, "duration": 250},
    {"title": "Groove Is in the Heart", "track_number": 4, "disc_number": 1, "duration": 292},
    {"title": "The Power of Love", "track_number": 5, "disc_number": 1, "duration": 0},
    {"title": "Venus", "track_number": 6, "disc_number": 1, "duration": 212},
    {"title": "Walk Like an Egyptian", "track_number": 7, "disc_number": 1, "duration": 200},
]


def _identity_key(local_title_key: str, disc=None, position=None) -> str:
    from services.popularity.popularity_sources import track_identity_key
    return track_identity_key(local_title_key, disc, position)


def _norm_key(value: str) -> str:
    from services.popularity.popularity_sources import normalize_for_aggregation
    return normalize_for_aggregation(value)


@pytest.fixture()
def ps(monkeypatch):
    """Point the release-tracklist source at the fixture release."""
    from services.popularity import popularity_sources as _ps

    monkeypatch.setattr(_ps, "_resolve_release_mbid", lambda artist, album, tracks: "rel-1")
    monkeypatch.setattr(
        _ps,
        "lb_get_release_metadata_batch",
        lambda mbids: {"rel-1": {"media": _media_payload()}},
    )
    monkeypatch.setattr(
        _ps,
        "lb_get_recording_popularity_batch",
        lambda mbids: {m: {"total_listen_count": 100, "total_user_count": 10} for m in mbids},
    )
    return _ps


def test_unique_title_gets_no_wrong_position_alias(ps):
    """The Power of Love (unique on the release) must keep its OWN identity —
    NOT the release track 5 (Love Shack) that the sparse local numbering
    collides with."""
    out, _release = ps.get_listenbrainz_album_tracklist_with_release(
        "Various Artists", "Take 40 Australia", LOCAL_TRACKS
    )

    power = out.get(_norm_key("The Power of Love")) or {}
    assert power.get("recording_mbid") == POWER_OF_LOVE_MBID, (
        "the unique title-keyed identity must carry the track's OWN recording"
    )

    # The wrong alias must NOT exist: local#5 pointing at release#5 (Love Shack).
    alias_key = _identity_key(_norm_key("The Power of Love"), 1, 5)
    alias = out.get(alias_key)
    if alias:
        assert alias.get("recording_mbid") != LOVE_SHACK_MBID, (
            "reported: a sparse library position-matched 'The Power of Love' "
            "to Love Shack and tried to change the wrong track"
        )
    else:
        # No alias at all is the preferred outcome — the title answered it.
        assert power.get("recording_mbid") == POWER_OF_LOVE_MBID


def test_the_wrong_release_title_is_never_published(ps):
    out, _release = ps.get_listenbrainz_album_tracklist_with_release(
        "Various Artists", "Take 40 Australia", LOCAL_TRACKS
    )

    for key, entry in out.items():
        title = str(entry.get("release_track_title") or "")
        if entry.get("recording_mbid") != POWER_OF_LOVE_MBID:
            continue
        assert "Love Shack" not in title, (
            "a track whose own recording was resolved must not carry the "
            "release title of a missing neighbour slot"
        )


def test_a_missing_track_does_not_resolve_anything(ps):
    """Love Shack is NOT in the library — no LIBRARY track should claim it.

    ``out`` is the release's full identity catalog, so it legitimately carries
    a ``love shack`` key (consumers read keys matching LOCAL titles only). The
    guard this pins: none of the library's own rows may resolve to Love
    Shack's recording.
    """
    out, _release = ps.get_listenbrainz_album_tracklist_with_release(
        "Various Artists", "Take 40 Australia", LOCAL_TRACKS
    )

    local_keys = {_norm_key(t["title"]) for t in LOCAL_TRACKS}
    for key in local_keys:
        entry = out.get(key) or {}
        assert entry.get("recording_mbid") != LOVE_SHACK_MBID, (
            f"'{key}' is a LIBRARY track but was aliased to Love Shack, which "
            "is not in the library — the sparse numbering fabricated it"
        )