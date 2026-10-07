"""Album genres must come from the release GROUP and the release itself.

Reported: *"MusicBrainz has both Genres and other tags … The album genres
should be pulled from the release group and the specific release."*

Probed against the live API: one ``/ws/2/release/{mbid}?inc=…+genres``
lookup already carries three genre layers — the embedded **release-group**'s
curated genres, the **release**'s own genres, and each **recording**'s
genres. ``_flatten_release`` surfaced only the recordings, so the album
genre proposal unioned per-track values and could propose NOTHING for an
whose recordings are untagged even when the release group is fully
tagged (metal, symphonic metal …).

The raw "other tags" (``english``, ``offizielle charts``, ``1–4 wochen`` …)
are deliberately NOT genres: MusicBrainz's curated ``genres`` list is the
genre subset derived from them, and that is what this pipeline consumes.
"""
from __future__ import annotations

from services.enrichment import musicbrainz_service as _mbs
from services.metadata import metadata_proposal_service as _proposal

# ``getattr`` with a ``None`` default rather than a direct import: a missing
# helper is exactly the regression these tests exist for, so the BEHAVIOURAL
# tests must still run and fail loudly instead of the whole module erroring
# out of collection and reporting a broken build as "1 error".
_flatten_release = _mbs._flatten_release
_join_genre_names = getattr(_mbs, "_join_genre_names", None)
_album_genres = getattr(_proposal, "_album_genres", None)
_album_level_proposals = getattr(_proposal, "_album_level_proposals", None)


def _require(function, name):
    assert function is not None, (
        f"{name} is missing — the album genres are no longer pulled from the "
        "release group and the release"
    )
    return function


def _release_payload():
    """A minimal release shaped like the live probe's response."""
    return {
        "id": "rel-1",
        "title": "Album Title",
        "status": "Official",
        "artist-credit": [{"name": "Band", "artist": {"id": "artist-1"}}],
        "release-group": {
            "id": "rg-1",
            "title": "Album Title",
            "first-release-date": "2007-04-02",
            "primary-type": "Album",
            "secondary-types": [],
            "genres": [{"name": "metal"}, {"name": "symphonic metal"}],
        },
        "genres": [{"name": "heavy metal"}],
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "position": 1,
                        "title": "Song",
                        "length": 200000,
                        "recording": {
                            "id": "rec-1",
                            "genres": [{"name": "gothic metal"}],
                        },
                    }
                ],
            }
        ],
    }


class TestFlattenSurfacesAlbumLevelGenres:
    """The same fetch already carries them — they were simply dropped."""

    def test_release_group_genres_are_surfaced(self):
        flat = _flatten_release(_release_payload(), "rel-1")
        assert flat["release_group_genres"] == "metal, symphonic metal"

    def test_release_genres_are_surfaced(self):
        flat = _flatten_release(_release_payload(), "rel-1")
        assert flat["release_genres"] == "heavy metal"

    def test_recordings_keep_their_genres(self):
        flat = _flatten_release(_release_payload(), "rel-1")
        assert flat["tracks"][0]["musicbrainz_genres"] == "gothic metal"

    def test_absent_genres_surface_as_empty_strings(self):
        payload = _release_payload()
        payload.pop("genres")
        payload["release-group"].pop("genres")
        flat = _flatten_release(payload, "rel-1")
        assert flat["release_group_genres"] == ""
        assert flat["release_genres"] == ""

    def test_join_genre_names_dedupes_case_insensitively(self):
        join = _require(_join_genre_names, "_join_genre_names")
        assert join(None) == ""
        assert (
            join([{"name": "Metal"}, {"name": "metal"}, {"name": " rock "}])
            == "Metal, rock"
        )


class TestAlbumGenresPrecedence:
    """Group → release → recordings, deduplicated in first-seen order."""

    @staticmethod
    def _genres(metadata):
        return _require(_album_genres, "_album_genres")(metadata)

    def test_group_and_release_genres_lead_the_list(self):
        genres = self._genres({
            "release_group_genres": "metal, symphonic metal",
            "release_genres": "heavy metal, metal",
            "tracks": [{"musicbrainz_genres": "gothic metal"}],
        })
        assert genres == "metal, symphonic metal, heavy metal, gothic metal"

    def test_recordings_alone_still_work(self):
        """The legacy path: an album with no surfaced group/release genres."""
        genres = self._genres({
            "tracks": [
                {"musicbrainz_genres": "rock, pop"},
                {"musicbrainz_genres": "Rock"},
            ],
        })
        assert genres == "rock, pop"

    def test_uncharted_everything_is_empty(self):
        assert self._genres({"tracks": [{"musicbrainz_genres": ""}]}) == ""
        assert self._genres({}) == ""


class TestTheProposalUsesGroupAndReleaseGenres:
    def test_group_genres_propose_even_when_recordings_have_none(self):
        """The reported casualty: untagged recordings, fully tagged group."""
        metadata = {
            "release_title": "Album Title",
            "release_group_genres": "metal, symphonic metal",
            "release_genres": "",
            "tracks": [{"musicbrainz_genres": ""}],
        }

        proposals = _require(_album_level_proposals, "_album_level_proposals")(
            [{"title": "Song"}], metadata
        )
        genre_proposal = next(
            (p for p in proposals if p.get("field") == "album_genres"), None
        )

        assert genre_proposal is not None, (
            "a fully tagged release group must propose album genres even "
            "when every recording is untagged"
        )
        assert genre_proposal["proposed"] == "metal, symphonic metal"

    def test_an_identical_current_value_is_not_reproposed(self):
        metadata = {
            "release_group_genres": "metal, symphonic metal",
            "tracks": [],
        }
        local = [{"genres": "symphonic metal, metal"}]

        proposals = _require(_album_level_proposals, "_album_level_proposals")(
            local, metadata
        )

        assert [p for p in proposals if p.get("field") == "album_genres"] == [], (
            "the same genre set in a different order is not a change"
        )
