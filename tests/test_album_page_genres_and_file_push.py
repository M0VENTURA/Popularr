"""Two album-page fixes.

1. **Genres were proposed after every save.** ``current`` for the album-genres
   field is ``tracks.genres`` — the FINAL genres the scanner chose, capped at
   ``get_genre_track_limit()`` — while the proposed side was the raw union of
   every release-group, release and recording tag. On Untouchables that is 3
   stored against 14 offered, so set equality could never hold and NO save could
   clear the bar.

   Trimming the offered list to N is not the fix: the union is ordered
   group-first, so "first 3" picks a genre the scan did not keep. The question
   that has an answer is "does the album still support what is STORED?" — a
   subset test.

2. **A saved genre edit never reached the file.** ``_apply_album_track_genres``
   writes the database only, by design; the FILES were the popularity scan's job
   and only catch up on the next scan. Navidrome reads the files, so it kept
   showing the old genres. The save now pushes the stored value with
   ``apply_genres_to_album`` — and deliberately NOT with
   ``sync_album_file_tags``, whose "active genre cleanup" re-derives every
   track's genres and silently reverts the user's choice.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from services.metadata import metadata_proposal_service as mps

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

STORED = "Nu Metal, Alternative Metal, Industrial Metal"
RELEASE_GROUP = ["Nu Metal", "Alternative Metal", "Industrial Metal"]
RECORDINGS = RELEASE_GROUP + [
    "Alternative Rock", "Heavy Metal", "Industrial Rock", "Rock", "Metal",
    "Sludge Metal", "Rapcore", "Black Metal", "Post-Grunge", "Death Metal",
    "Gothic Metal",
]


class TestTheAlbumGenresBarCanBeCleared:
    def test_more_offered_than_stored_is_not_a_change(self) -> None:
        """The reported case: 3 stored, 14 offered, re-proposed forever."""
        assert mps._album_genre_sets_equal(STORED, ", ".join(RECORDINGS)) is True

    def test_a_genre_that_left_the_release_still_reports(self) -> None:
        assert mps._album_genre_sets_equal(
            "Nu Metal, Alternative Metal, Dubstep", ", ".join(RECORDINGS)
        ) is False

    def test_order_alone_is_not_a_change(self) -> None:
        assert mps._album_genre_sets_equal(
            "Industrial Metal, Nu Metal", ", ".join(RECORDINGS)
        ) is True

    def test_nothing_stored_is_only_equal_to_nothing_offered(self) -> None:
        assert mps._album_genre_sets_equal("", ", ".join(RECORDINGS)) is False
        assert mps._album_genre_sets_equal("", "") is True

    def test_the_album_proposal_is_not_capped_by_order(self) -> None:
        """Trimming to N would pick Alternative Rock over the stored Industrial
        Metal, so the proposal must keep offering the full union."""
        proposed = mps._album_genres({
            "release_group_genres": RELEASE_GROUP,
            "release_genres": [],
            "tracks": [{"musicbrainz_genres": RECORDINGS}],
        })
        assert "Industrial Metal" in proposed
        assert len(proposed) > len(RELEASE_GROUP)

    def test_the_track_level_rule_is_untouched(self) -> None:
        """Track genres keep strict set equality — nothing there is capped."""
        assert mps._genre_sets_equal("Nu Metal", "nu metal") is True
        assert mps._genre_sets_equal("Nu Metal, Cover", "nu metal") is False

    def test_the_album_branch_uses_the_subset_rule(self) -> None:
        source = inspect.getsource(mps)
        assert "_album_genre_sets_equal(\n            current.get(form_field), proposed" in source or (
            "_album_genre_sets_equal(" in source
        ), "the album field must not fall back to set equality"
        assert "if form_field == \"album_genres\" and _album_genre_sets_equal(" in source


class TestTheSavePushesGenresToTheFiles:
    def _save_source(self) -> str:
        return (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")

    def test_the_save_calls_the_file_writer(self) -> None:
        assert "apply_genres_to_album(" in self._save_source(), (
            "a saved genre edit stays in the database until the next scan, so "
            "Navidrome keeps showing the old genres"
        )

    def test_it_does_not_call_the_scans_blend(self) -> None:
        """``sync_album_file_tags`` re-derives genres and undoes the save."""
        src = self._save_source()
        assert "sync_album_file_tags(" not in src, (
            "the scan's helper rewrites tracks.genres from each track's own "
            "sources, which reverts the album fill the save just wrote"
        )

    def test_it_only_fires_when_a_genre_changed(self) -> None:
        src = self._save_source()
        start = src.index("push the FINAL GENRES to the audio files")
        # The comment block between the marker and the condition is long; bound
        # the window by the next section marker rather than a character count.
        end = src.index("Do-not-search-album-release flag", start)
        block = src[start:end]
        assert "if genre_only_writes:" in block, (
            "keying off the presence of the album_genres field would rewrite "
            "every file on every unrelated save"
        )
