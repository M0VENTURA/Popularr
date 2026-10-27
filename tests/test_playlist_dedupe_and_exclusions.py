"""Dedupe and artist-exclusion identity for generated playlists.

Reported
--------
> Both playlists should have deduplication so the same track isnt displayed
> twice, making sure to check track artist as well as album artist for the
> dedup.
>
> Also make sure that if an artist is listed in the config to ignore from
> playlist generation, that it also takes into account the track artist. I've
> seen some tracks from compilations making it through.

Why the album artist alone is not enough
----------------------------------------
A compilation row carries the PERFORMER in ``tracks.artist`` and
"Various Artists" (or the album's artist) in ``tracks.album_artist``. A
dedup key or an exclusion check built from only ``album_artist`` therefore
sees "Various Artists" and neither recognises the duplicate nor applies the
exclusion the operator configured.

The fix checks BOTH fields: rows are duplicates when their titles match AND
either artist field matches (or they share a track id), and a row is excluded
when EITHER artist field matches the configured list.
"""

from __future__ import annotations

import pytest

from services.popularity.stages import finalise_stage as fs


def song(
    title: str,
    artist: str,
    album_artist: str | None = None,
    *,
    track_id: str | None = None,
    stars: int = 5,
    final_score: float = 50.0,
) -> dict:
    return {
        "id": track_id or f"{artist}-{title}",
        "title": title,
        "artist": artist,
        "album_artist": album_artist if album_artist is not None else artist,
        "stars": stars,
        "popularity_score": final_score,
        "final_score": final_score,
    }


def _titles(groups: list[list[dict]]) -> list[str]:
    return sorted(group[0]["title"] for group in groups)


class TestTheDedupeKeyChecksBothArtistFields:
    def test_the_compilation_copy_is_the_same_track(self):
        """Performer matches, album artist differs → ONE group."""
        rows = [
            song("Y'all Want a Single", "Korn", "Korn"),
            song("Y'all Want a Single", "Korn", "Various Artists"),
        ]
        groups = fs._group_duplicate_tracks(rows)
        assert len(groups) == 1, (
            "the compilation copy must not survive as a second playlist entry"
        )

    def test_the_album_artist_alone_can_identify_a_duplicate(self):
        """Same album artist, different artist credit → ONE group."""
        rows = [
            song("Song", "Band", "Shared Album"),
            song("Song", "Other Band", "Shared Album"),
        ]
        assert len(fs._group_duplicate_tracks(rows)) == 1

    def test_a_guest_credit_does_not_split_one_recording(self):
        rows = [
            song("Song", "Band", "Band"),
            song("Song", "Band feat. Guest", "Band"),
        ]
        assert len(fs._group_duplicate_tracks(rows)) == 1

    def test_the_same_track_id_is_one_track_even_if_metadata_differs(self):
        rows = [
            song("Song", "Band", track_id="42"),
            song("Song (Remastered)", "Band", track_id="42"),
        ]
        assert len(fs._group_duplicate_tracks(rows)) == 1

    def test_different_titles_are_not_merged(self):
        rows = [
            song("Song", "Band"),
            song("Another Song", "Band"),
        ]
        groups = fs._group_duplicate_tracks(rows)
        assert len(groups) == 2
        assert _titles(groups) == ["Another Song", "Song"]

    def test_different_artists_on_different_albums_are_not_merged(self):
        rows = [
            song("Intro", "Artist A", "Album A"),
            song("Intro", "Artist B", "Album B"),
        ]
        assert len(fs._group_duplicate_tracks(rows)) == 2, (
            "two genuinely different songs that share a title must stay apart"
        )

    def test_a_placeholder_album_artist_does_not_merge_two_songs(self):
        """Every compilation row shares "Various Artists" — it is not identity."""
        rows = [
            song("Intro", "Artist A", "Various Artists"),
            song("Intro", "Artist B", "Various Artists"),
        ]
        assert len(fs._group_duplicate_tracks(rows)) == 2

    def test_the_genre_builder_pool_carries_the_album_artist(self):
        """The shared helper needs the album artist to exist on pool dicts."""
        import inspect

        source = inspect.getsource(fs._create_genre_top_track_playlists)
        assert '"album_artist": str(row.get("album_artist") or "")' in source, (
            "the genre pool dict must carry album_artist, or the dedupe helper "
            "can only see the track artist"
        )

    def test_grouping_is_transitive(self):
        """A↔B by album artist, B↔C by track artist → one group."""
        rows = [
            song("Song", "Artist A", "Shared Album"),
            song("Song", "Artist B", "Shared Album"),
            song("Song", "Artist B", "Other Album"),
        ]
        assert len(fs._group_duplicate_tracks(rows)) == 1

    @pytest.mark.parametrize(
        "fields",
        [
            ("Y'all Want a Single", "Y\u2019all Want a Single"),
            ("Song - Live", "Song \u2013 Live"),
        ],
    )
    def test_the_title_key_still_folds_unicode_punctuation(self, fields):
        left, right = fields
        rows = [song(left, "Band"), song(right, "Band")]
        assert len(fs._group_duplicate_tracks(rows)) == 1


class TestTheExclusionCheckChecksTheTrackArtist:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("artist", "Candlemass"),
            ("album_artist", "Candlemass"),
            ("track_artist", "Candlemass"),
        ],
    )
    def test_either_artist_field_can_exclude_a_row(self, field, value):
        """Only the field under test names the excluded act."""
        row = song("Bewitched", "Someone Else", "Some Album")
        row[field] = value
        assert fs._row_artist_is_excluded(row, {"candlemass"}) is True

    def test_a_compilation_row_is_excluded_via_its_track_artist(self):
        """The reported case: album artist is a placeholder, artist is excluded."""
        row = song("Bewitched", "Candlemass", "Various Artists")
        assert fs._row_artist_is_excluded(row, {"candlemass"}) is True

    def test_a_guest_credit_is_excluded_via_its_primary_artist(self):
        row = song("Bewitched", "Candlemass feat. Someone", "Various Artists")
        assert fs._row_artist_is_excluded(row, {"candlemass"}) is True

    def test_an_excluded_featured_guest_is_also_caught(self):
        """A featured credit still plays the excluded act."""
        row = song("Song", "Band feat. Candlemass", "Band")
        assert fs._row_artist_is_excluded(row, {"candlemass"}) is True

    def test_an_unrelated_artist_is_kept(self):
        row = song("Bewitched", "Candlemass", "Various Artists")
        assert fs._row_artist_is_excluded(row, {"muse"}) is False

    def test_no_exclusions_keeps_everything(self):
        row = song("Bewitched", "Candlemass")
        assert fs._row_artist_is_excluded(row, set()) is False


class TestTheEssentialBuilderUsesBothHelpers:
    def test_the_essential_builder_groups_through_the_shared_helper(self):
        import inspect

        source = inspect.getsource(fs._sync_essential_playlist)
        assert "_group_duplicate_tracks(" in source, (
            "the Essential Collection must de-duplicate through the shared "
            "artist-aware helper"
        )
        assert "_row_artist_is_excluded(" in source, (
            "the Essential Collection must apply the exclusion to the track "
            "artist as well as the album artist"
        )
