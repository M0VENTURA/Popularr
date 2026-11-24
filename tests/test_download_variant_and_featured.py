"""Download matching: requested variants and featured artists are mandatory.

REPORTED (queue activity log):

* "Adele – Someone Like You (Funk3d radio edit)" downloaded the plain album
  track, because ``radio``/``edit`` were not in the hard-variant set.
* "Adele – Make You Feel My Love (Luka Bloom Cover)" accepted an ordinary
  greatest-hits copy with no cover evidence.
* "Mortemia feat. Marita Hellem – Shout" accepted ``01-01. Shout (Cover).flac``,
  which names neither the guest nor a feature.

A plain title match must not compensate for a missing recording variant.
"""

from __future__ import annotations

from services.downloads.download_pipeline_service import _score_result


def _score(filename, *, artist, title, rejects=None, metadata=None):
    result = {"filename": filename}
    if metadata is not None:
        result["metadata"] = metadata
    return _score_result(
        result, artist, title, rejects=rejects,
    )


class TestRequestedVariantIsMandatory:
    def test_a_radio_edit_request_rejects_the_plain_album_track(self):
        rejects: dict[str, int] = {}
        score = _score(
            "Adele - 21 - 11 - Someone Like You.mp3",
            artist="Adele",
            title="Someone Like You (Funk3d radio edit)",
            rejects=rejects,
        )
        assert score == 0.0
        assert rejects.get("requested_variant_missing") == 1

    def test_a_candidate_carrying_the_edit_is_accepted(self):
        rejects: dict[str, int] = {}
        score = _score(
            "Adele - Someone Like You (Funk3d Radio Edit).mp3",
            artist="Adele",
            title="Someone Like You (Funk3d radio edit)",
            rejects=rejects,
        )
        assert score > 0
        assert "requested_variant_missing" not in rejects

    def test_a_plain_request_accepts_the_plain_track(self):
        rejects: dict[str, int] = {}
        score = _score(
            "Adele - 21 - 11 - Someone Like You.mp3",
            artist="Adele",
            title="Someone Like You",
            rejects=rejects,
        )
        assert score > 0
        assert "requested_variant_missing" not in rejects

    def test_a_neutral_album_version_does_not_demand_a_marker(self):
        """“Album Version” names the ordinary recording, not a variant."""
        rejects: dict[str, int] = {}
        score = _score(
            "Adele - 21 - 11 - Someone Like You.mp3",
            artist="Adele",
            title="Someone Like You (Album Version)",
            rejects=rejects,
        )
        assert score > 0
        assert "requested_variant_missing" not in rejects

    def test_an_unrequested_live_track_is_still_rejected(self):
        rejects: dict[str, int] = {}
        score = _score(
            "Adele - Someone Like You (Live).mp3",
            artist="Adele",
            title="Someone Like You",
            rejects=rejects,
        )
        assert score == 0.0
        assert rejects.get("variant_mismatch") == 1


class TestRequestedCoverIsMandatory:
    def test_a_plain_copy_does_not_satisfy_a_named_cover(self):
        rejects: dict[str, int] = {}
        score = _score(
            "2012 - Greatest Hits/13 - Make You Feel My Love.mp3",
            artist="Adele",
            title="Make You Feel My Love (Luka Bloom Cover)",
            rejects=rejects,
        )
        assert score == 0.0
        assert rejects.get("requested_variant_missing") == 1

    def test_the_named_cover_performer_counts_as_evidence(self):
        rejects: dict[str, int] = {}
        score = _score(
            "Adele - Make You Feel My Love (Luka Bloom Cover).mp3",
            artist="Adele",
            title="Make You Feel My Love (Luka Bloom Cover)",
            rejects=rejects,
        )
        assert score > 0
        assert "requested_variant_missing" not in rejects


class TestFeaturedArtistConfirmation:
    def test_a_candidate_without_the_guest_is_rejected(self):
        rejects: dict[str, int] = {}
        score = _score(
            "Mortemia/01-01. Shout (Cover).flac",
            artist="Mortemia feat. Marita Hellem",
            title="Shout",
            rejects=rejects,
        )
        assert score == 0.0
        assert rejects.get("featured_artist_missing") == 1

    def test_the_guest_in_the_filename_confirms_the_match(self):
        rejects: dict[str, int] = {}
        score = _score(
            "Mortemia feat. Marita Hellem - Shout.flac",
            artist="Mortemia feat. Marita Hellem",
            title="Shout",
            rejects=rejects,
        )
        assert score > 0
        assert "featured_artist_missing" not in rejects

    def test_embedded_metadata_can_confirm_the_guest(self):
        rejects: dict[str, int] = {}
        score = _score(
            "01-01. Shout.flac",
            artist="Mortemia feat. Marita Hellem",
            title="Shout",
            rejects=rejects,
            metadata={"artist": "Mortemia feat. Marita Hellem", "album_artist": "Mortemia"},
        )
        assert score > 0
        assert "featured_artist_missing" not in rejects

    def test_a_plain_artist_request_is_unaffected(self):
        rejects: dict[str, int] = {}
        score = _score(
            "Adele - 21 - 11 - Someone Like You.mp3",
            artist="Adele",
            title="Someone Like You",
            rejects=rejects,
        )
        assert score > 0
        assert "featured_artist_missing" not in rejects
