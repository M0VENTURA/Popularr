"""Typography-only differences must never become corrections.

REPORTED

> "Files like this keep showing they need corrections:
>  MusicBrainz: Title: So Long, Good-Bye → So Long, Good‐Bye"

U+2010 (and its relatives) is the same hyphen as ASCII "-". The pending-update
and conflict gates compared raw strings, so the proposal was re-stashed and
re-rendered forever.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from services.metadata import pending_update_service as pending
from services.metadata.conflict_service import _same_value


ASCII = "So Long, Good-Bye"
MB = "So Long, Good\u2010Bye"  # U+2010 HYPHEN


class TestTheTypographyKey:
    def test_a_unicode_hyphen_is_not_a_different_title(self):
        assert pending._is_typography_only_change({
            "field": "title", "current": ASCII, "proposed": MB,
        }) is True

    def test_a_genuine_word_difference_is_still_a_change(self):
        assert pending._is_typography_only_change({
            "field": "title",
            "current": "So Long, Good-Bye",
            "proposed": "So Long, Farewell",
        }) is False

    def test_non_title_fields_fold_punctuation_but_keep_wording(self):
        assert _same_value("album", ASCII, MB) is True
        assert _same_value("album", ASCII, "So Long, Goodbye") is False

    def test_filter_drops_only_the_typography_change(self):
        changes = [
            {"field": "title", "current": ASCII, "proposed": MB},
            {"field": "title", "current": "Old", "proposed": "New"},
        ]
        kept = pending._filter_ignored(changes, None)
        assert kept == [{"field": "title", "current": "Old", "proposed": "New"}]


class TestImportAuthority:
    def test_a_unicode_hyphen_is_not_a_local_authority_conflict(self):
        from services.scanning.reconciliation import local_authority_conflicts

        assert local_authority_conflicts({"title": ASCII}, {"title": MB}) == []
        assert local_authority_conflicts({"title": "Old"}, {"title": "New"}) == ["title"]


@pytest.fixture()
def seeded(db_session):
    db_session.execute(text("DELETE FROM tracks"))
    db_session.execute(
        text(
            "INSERT INTO tracks (id, title, artist, album_artist, album, "
            "pending_mb_updates, mb_ignored_fields) VALUES "
            "('typ-1', :title, 'Artist', 'Artist', 'Album', :pending, '[]'), "
            "('typ-2', 'Clean Track', 'Artist', 'Artist', 'Album', :pending2, '[]')"
        ),
        {
            "title": ASCII,
            "pending": json.dumps({
                "version": 1,
                "changes": [
                    {"field": "title", "label": "Title",
                     "current": ASCII, "proposed": MB},
                ],
            }),
            "pending2": json.dumps({
                "version": 1,
                "changes": [
                    {"field": "title", "label": "Title",
                     "current": "Clean Track", "proposed": "Cleaner Track"},
                ],
            }),
        },
    )
    db_session.commit()
    yield


class TestExistingStashes:
    def test_a_typography_only_stash_is_filtered_from_the_album_review(self, seeded):
        result = pending.fetch_album_recommendations("Artist", "Album")
        tracks = {t["track_id"]: t for t in result["track_changes"]}
        assert "typ-1" not in tracks, "the hyphen-only proposal is still offered"
        assert "typ-2" in tracks
        assert result["has_any"] is True

    def test_it_is_not_counted_as_pending_work(self, seeded):
        result = pending.fetch_artist_recommendations("Artist")
        # Only the genuine change counts; the hyphen-only row does not.
        assert result["total"] == 1

    def test_stashing_a_typography_only_title_is_refused(self, seeded, db_session):
        wrote = pending.stash_track_title_recommendation(
            track_id="typ-1",
            artist="Artist",
            album="Album",
            current_title=ASCII,
            proposed_title=MB,
        )
        assert wrote is False
