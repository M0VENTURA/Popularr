"""Provider genre vote counts must survive into the genre ranking.

MusicBrainz release-group pages show the vote counts (``pop 14``,
``dance-pop 4``, ``alternative rock 2``). Discarding ``count`` and storing the
raw array order made an alphabetical tag list look like a ranking.
"""

from __future__ import annotations


class TestGenreEntriesByVote:
    def test_musicbrainz_counts_order_the_names(self):
        from services.popularity.stages.track_stage import _genre_entries_by_vote

        entries = [
            {"name": "alternative rock", "count": 2},
            {"name": "pop", "count": 14},
            {"name": "dance-pop", "count": 4},
            {"name": "teen pop", "count": 3},
            {"name": "bubblegum pop", "count": 2},
        ]

        assert _genre_entries_by_vote(entries) == [
            "pop", "dance-pop", "teen pop", "alternative rock", "bubblegum pop",
        ]

    def test_listenbrainz_tag_key_is_supported(self):
        from services.popularity.stages.track_stage import _genre_entries_by_vote

        entries = [
            {"tag": "alternative rock", "count": 2},
            {"tag": "pop", "count": 14},
        ]

        assert _genre_entries_by_vote(entries) == ["pop", "alternative rock"]

    def test_missing_counts_keep_provider_order_and_duplicates_are_removed(self):
        from services.popularity.stages.track_stage import _genre_entries_by_vote

        entries = [{"name": "ambient"}, {"name": "pop"}, {"name": "ambient"}]

        assert _genre_entries_by_vote(entries) == ["ambient", "pop"]
