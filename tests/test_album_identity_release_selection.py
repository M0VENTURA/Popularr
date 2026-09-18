"""Regression tests: a recording must adopt the album being scanned, not an
arbitrary release it also appears on.

Reproduces the "72 Seasons" split: a Metallica track from the studio album was
also played live on the M72 tour, so its recording lists BOTH releases.
MusicBrainz returns a recording's releases in no meaningful order, and the
resolver took ``releases[0]``. When that was the live release, the track:

- adopted the tour release-GROUP's name as its album, so one folder shattered
  into many one-track "albums" named
  "2023-06-16: M72 World Tour: Gothenburg, Sweden"; and
- was titled with the SPECIFIC release name instead of the release-group name.

Guards:
- ``_select_primary_release`` prefers the release matching the scanned album,
  then a canonical studio release, and is deterministic.
- ``_recording_to_metadata`` takes the album identity from the release GROUP,
  never from the specific release/edition title.
"""

from __future__ import annotations

ARTIST = "Metallica"
ALBUM = "72 Seasons"
TITLE = "Lux Aeterna"


def _live_tour_release():
    return {
        "id": "rel-live",
        "title": "2023-06-16: M72 World Tour: Gothenburg, Sweden",
        "date": "2023-06-16",
        "release-group": {
            "id": "rg-live",
            "title": "M72 World Tour: Gothenburg, Sweden",
            "primary-type": "Album",
            "secondary-types": ["Live"],
        },
    }


def _studio_release():
    return {
        "id": "rel-studio",
        "title": ALBUM,
        "date": "2023-04-14",
        "release-group": {
            "id": "rg-studio",
            "title": ALBUM,
            "primary-type": "Album",
        },
    }


def _recording(releases):
    return {
        "id": "rec-lux",
        "title": TITLE,
        "artist-credit": [
            {"name": ARTIST, "artist": {"id": "artist-mb", "name": ARTIST}}
        ],
        "releases": releases,
    }


class TestSelectPrimaryRelease:
    """The release that describes the scanned album wins."""

    def _select(self, releases, album=None):
        from services.enrichment.musicbrainz_service import _select_primary_release

        return _select_primary_release(releases, album)

    def test_album_anchor_beats_an_earlier_live_release(self):
        # releases[0] is the live tour release — the reported defect.
        chosen = self._select([_live_tour_release(), _studio_release()], ALBUM)
        assert chosen["id"] == "rel-studio"

    def test_studio_release_beats_live_even_with_no_anchor(self):
        # No album to anchor on: the canonical studio release must still win
        # over a live tour album, otherwise the folder still splits.
        chosen = self._select([_live_tour_release(), _studio_release()])
        assert chosen["id"] == "rel-studio"

    def test_compilation_is_not_preferred_over_a_studio_album(self):
        compilation = {
            "id": "rel-comp",
            "title": "The Best Of Metallica",
            "date": "2019-01-01",
            "release-group": {
                "id": "rg-comp",
                "title": "The Best Of Metallica",
                "primary-type": "Album",
                "secondary-types": ["Compilation"],
            },
        }
        chosen = self._select([compilation, _studio_release()])
        assert chosen["id"] == "rel-studio"

    def test_single_release_list_returns_that_release(self):
        chosen = self._select([_live_tour_release()], ALBUM)
        assert chosen["id"] == "rel-live"

    def test_empty_list_returns_empty_dict(self):
        assert self._select([], ALBUM) == {}

    def test_non_dict_entries_are_ignored(self):
        chosen = self._select(["nonsense", _studio_release()], ALBUM)
        assert chosen["id"] == "rel-studio"

    def test_result_is_deterministic_regardless_of_input_order(self):
        # MusicBrainz ordering must not change which release is chosen.
        releases = [_live_tour_release(), _studio_release(), {
            "id": "rel-other",
            "title": "Some Other Album",
            "date": "2001-01-01",
            "release-group": {"id": "rg-o", "title": "Some Other Album"},
        }]
        first = self._select(list(releases), ALBUM)
        second = self._select(list(reversed(releases)), ALBUM)
        third = self._select([releases[1], releases[2], releases[0]], ALBUM)
        assert first["id"] == second["id"] == third["id"] == "rel-studio"

    def test_no_studio_release_falls_back_deterministically(self):
        # Only non-studio releases exist: pick the earliest, then by id, so
        # the outcome cannot depend on ordering.
        late = {
            "id": "rel-b",
            "title": "Live B",
            "date": "2024-05-05",
            "release-group": {"id": "rg-b", "title": "Live B", "secondary-types": ["Live"]},
        }
        early = {
            "id": "rel-a",
            "title": "Live A",
            "date": "2023-06-16",
            "release-group": {"id": "rg-a", "title": "Live A", "secondary-types": ["Live"]},
        }
        assert self._select([late, early])["id"] == "rel-a"
        assert self._select([early, late])["id"] == "rel-a"

    def test_missing_release_group_does_not_crash(self):
        chosen = self._select([{"id": "rel-bare", "title": "Bare"}], ALBUM)
        assert chosen["id"] == "rel-bare"


class TestRecordingMetadataUsesReleaseGroupName:
    """The album name comes from the release GROUP, never the edition title."""

    def _convert(self, releases, album_name=None):
        from services.enrichment.musicbrainz_service import MusicBrainzService

        svc = MusicBrainzService(enabled=True)
        return svc._recording_to_metadata(
            _recording(releases), "rec-lux", 0.9, album_name=album_name,
        )

    def test_album_is_the_release_group_not_the_live_release_title(self):
        # The reported symptom: album titled
        # "2023-06-16: M72 World Tour: Gothenburg, Sweden".
        meta = self._convert([_live_tour_release(), _studio_release()], ALBUM)
        assert meta["album"] == ALBUM
        assert meta["album"] != "2023-06-16: M72 World Tour: Gothenburg, Sweden"

    def test_album_uses_release_group_even_without_an_anchor(self):
        meta = self._convert([_live_tour_release(), _studio_release()])
        assert meta["album"] == ALBUM

    def test_specific_release_title_is_kept_as_the_edition_only(self):
        # release_title is the EDITION tagline and must not leak into `album`.
        edition = {
            "id": "rel-ed",
            "title": "72 Seasons (Deluxe Edition)",
            "date": "2023-04-14",
            "release-group": {"id": "rg-studio", "title": ALBUM, "primary-type": "Album"},
        }
        meta = self._convert([edition], ALBUM)
        assert meta["album"] == ALBUM
        assert meta["release_title"] == "72 Seasons (Deluxe Edition)"

    def test_album_anchor_overrides_a_different_matching_release(self):
        # A caller that knows the album pins the identity regardless of what
        # else the recording appears on.
        meta = self._convert([_live_tour_release()], ALBUM)
        assert meta["album"] == ALBUM


class TestLookupRecordingMetadataAlbumAnchor:
    """``lookup_recording_metadata`` forwards the album anchor."""

    def _service(self):
        from services.enrichment.musicbrainz_service import MusicBrainzService

        svc = MusicBrainzService(enabled=True)
        svc.get_suggested_mbid = lambda title, artist, limit=5: ("rec-lux", 0.9)
        svc.http.get_recording = lambda mbid, inc="": _recording(
            [_live_tour_release(), _studio_release()]
        )
        return svc

    def test_album_argument_pins_the_album_identity(self):
        meta = self._service().lookup_recording_metadata(TITLE, ARTIST, album=ALBUM)
        assert meta["album"] == ALBUM

    def test_without_album_the_studio_release_still_wins(self):
        meta = self._service().lookup_recording_metadata(TITLE, ARTIST)
        assert meta["album"] == ALBUM

    def test_incomplete_input_returns_empty(self):
        assert self._service().lookup_recording_metadata("", ARTIST) == {}
        assert self._service().lookup_recording_metadata(TITLE, "") == {}
