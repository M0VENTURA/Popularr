"""A VA track's artist fallback must not borrow from sibling compilation tracks.

Reported: *"On these compilations the genres are being matched correctly for the
tracks and using genres that are matched to other tracks on these
compilations, while the popularity data for the tracks are matching correctly."*

``sync_various_artists_track_genres`` gives a track its own source genres first.
When it has none, ``fallback_genres_for_artist`` reads the PERFORMER's genres
from the database — but that query only excluded the CURRENT track, so other
tracks of the same compilation (the same artist credited on several slots, or a
shared folder credit) were legitimate evidence. The album being synced is now
excluded from that read.
"""

from __future__ import annotations

from sqlalchemy import text


def _insert(session, track_id: str, artist: str, album: str, genres: str) -> None:
    session.execute(
        text(
            "INSERT INTO tracks (id, artist, album, title, genres) "
            "VALUES (:id, :artist, :album, :title, :genres)"
        ),
        {
            "id": track_id,
            "artist": artist,
            "album": album,
            "title": f"Track {track_id}",
            "genres": genres,
        },
    )
    session.commit()


class TestTheArtistFallbackExcludesTheCompilation:
    def test_sibling_tracks_on_the_same_album_are_not_evidence(self, db_session):
        _insert(db_session, "comp-1", "Band", "Compilation", "Pop, Rock")
        _insert(db_session, "comp-2", "Band", "Compilation", "Pop, Rock")
        _insert(db_session, "other-1", "Band", "Other Album", "Metal")

        from services.enrichment.genre_aggregation_service import (
            _artist_genres_from_library,
        )

        got = _artist_genres_from_library(
            "Band",
            exclude_track_id="comp-1",
            exclude_album="Compilation",
        )

        assert "Metal" in got
        assert "Pop" not in got
        assert "Rock" not in got

    def test_without_the_exclusion_the_old_behaviour_is_visible(self, db_session):
        """CONTROL: the filter is what changes the answer, not the query shape."""
        _insert(db_session, "comp-1", "Band", "Compilation", "Pop, Rock")
        _insert(db_session, "comp-2", "Band", "Compilation", "Pop, Rock")
        _insert(db_session, "other-1", "Band", "Other Album", "Metal")

        from services.enrichment.genre_aggregation_service import (
            _artist_genres_from_library,
        )

        got = _artist_genres_from_library("Band", exclude_track_id="comp-1")

        assert "Pop" in got or "Rock" in got


class TestTheVaSyncPassesTheAlbumToTheFallback:
    def test_the_sync_excludes_its_own_album(self):
        import inspect

        from services.enrichment import genre_aggregation_service as svc

        source = inspect.getsource(svc.sync_various_artists_track_genres)
        assert "exclude_album=str(album or \"\")" in source, (
            "the VA sync must tell the artist fallback which album it is "
            "syncing, or sibling tracks leak into each other's genres"
        )


class TestAVaTrackPrefersItsRecordingGenresOverTheAlbumsReleaseGroup:
    """The release group is the ALBUM; on a compilation it is shared by every
    track, so using it first painted each track with the same album genres."""

    def test_the_scan_branches_on_the_va_flag(self):
        import inspect

        from services.popularity.stages import track_stage

        source = inspect.getsource(track_stage.process_track)
        marker = 'if bool((album_context or {}).get("is_va_compilation")):'
        assert marker in source
        window = source[source.index(marker): source.index(marker) + 500]
        assert "_recording_genres() or _release_group_genres()" in window, (
            "a VA track must take its own recording's genres first"
        )
        assert "_release_group_genres() or _recording_genres()" in window, (
            "single-artist albums keep the release-group-first order"
        )

