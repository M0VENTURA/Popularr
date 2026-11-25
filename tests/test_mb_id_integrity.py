"""MusicBrainz entity IDs must stay internally consistent.

Reported on a Various Artists compilation:

    artist: Vitro
    musicbrainz_artistid: 89ad4ac3-…   (the Various Artists id)
    musicbrainz_albumid: c1fd5078-…
    musicbrainz_releasegroupid: c1fd5078-…  (same UUID as the release)

A Release and a Release Group are different entities, and a track's artist id
is not the album artist's id. These are the import-time guards; the album save
has its own (``test_album_review_save_persists``).
"""

from __future__ import annotations

from services.scanning.payload_builder import (
    VARIOUS_ARTISTS_MBID,
    build_track_payload,
)

RELEASE = "c1fd5078-c940-3bc1-8102-fe83a56c4ef0"
GROUP = "2d8d4eda-72a4-4490-b4a1-cc3b5f89e0bc"
VITRO = "b1e9c3a4-1111-4222-8333-944455556666"
KORN = "ac865b2e-bba8-4f5a-8756-dd40d5e39f46"
OTHER_ARTIST = "d2f9c3a4-2222-4333-8444-955566667777"


def _payload(track_artist: str, album_artist: str, *, album_mbid=None, **extracted):
    return build_track_payload(
        track={"id": "t1", "title": "Song", "artist": track_artist},
        album_name="Compilation",
        album_artist_value=album_artist,
        canonical_artist_name=track_artist,
        album_mbid=album_mbid,
        extracted={
            "musicbrainz_albumid": RELEASE,
            "musicbrainz_albumartistid": (
                VARIOUS_ARTISTS_MBID if album_artist == "Various Artists" else OTHER_ARTIST
            ),
            **extracted,
        },
    )


def test_the_release_id_is_not_kept_as_the_release_group():
    payload = _payload(
        "Vitro",
        "Various Artists",
        musicbrainz_artistid=VITRO,
        musicbrainz_releasegroupid=RELEASE,
    )
    assert payload["musicbrainz_releasegroupid"] is None, (
        "the release UUID was stored as the release-group id"
    )


def test_the_album_artist_id_is_not_stored_as_the_track_artist_id():
    payload = _payload(
        "Vitro",
        "Various Artists",
        musicbrainz_artistid=VARIOUS_ARTISTS_MBID,
        musicbrainz_releasegroupid=GROUP,
    )
    assert payload["musicbrainz_artistid"] is None
    assert payload.get("musicbrainz_artist_id") is None


def test_a_normal_album_keeps_its_track_artist_id():
    """CONTROL — a lead artist's id is legitimate, even when it equals the
    album artist's id."""
    payload = _payload(
        "Korn",
        "Korn",
        musicbrainz_artistid=KORN,
        musicbrainz_releasegroupid=GROUP,
    )
    assert payload["musicbrainz_artistid"] == KORN
    assert payload["musicbrainz_releasegroupid"] == GROUP


def test_a_compilation_keeps_a_real_performer_id():
    payload = _payload(
        "Vitro",
        "Various Artists",
        musicbrainz_artistid=VITRO,
        musicbrainz_releasegroupid=GROUP,
    )
    assert payload["musicbrainz_artistid"] == VITRO
    assert payload["musicbrainz_releasegroupid"] == GROUP

