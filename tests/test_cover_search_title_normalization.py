"""MusicBrainz must not receive a raw library title in the recording search.

Reported on the *Spotify - Metal Covers* compilation: the tracks whose library
title carried a ``(X Cover)`` annotation or a ``- Version`` suffix never
resolved a recording MBID, while clean-titled tracks did — and only the tracks
with an MBID were detected as covers.

MusicBrainz is queried with a quoted Lucene *phrase*, so every token in the
supplied title must appear in the recording's title. "Doomed (Bring Me the
Horizon Cover)" therefore returns zero recordings even though "Doomed" by
Maphra exists; "Everytime We Touch - TEKKNO Version" likewise. The search now
queries a stripped title first and falls back to the raw title, and retries
without the artist clause when a file's ARTIST tag does not match MusicBrainz's
credit.
"""

from __future__ import annotations

import re

from services.enrichment import cover_detector_impl as impl
from services.enrichment.cover_detector_impl import CoverDetector

ALBUM_ARTIST = "Various Artists"


class _SearchStub:
    """MusicBrainz stub that honours the title AND artist phrases."""

    def __init__(self, recordings) -> None:
        self._recordings = list(recordings)
        self.queries: list[str] = []

    @staticmethod
    def _field(query: str, name: str) -> str:
        match = re.search(rf'{name}:"([^"]*)"', query)
        return match.group(1) if match else ""

    def search_recordings(self, query, limit=15):
        self.queries.append(query)
        title = self._field(query, "recording").casefold()
        artist = self._field(query, "artist").casefold()
        hits = []
        for rec in self._recordings:
            if str(rec.get("title") or "").casefold() != title:
                continue
            credit = str(
                (rec.get("artist-credit") or [{}])[0].get("artist", {}).get("name")
                or ""
            ).casefold()
            if artist and artist not in credit:
                continue
            hits.append(rec)
        return hits


def _detector(mb) -> CoverDetector:
    det = CoverDetector(db_connection=None)
    det.mb = mb
    return det


def _recording(mbid: str, title: str, artist: str) -> dict:
    return {
        "id": mbid,
        "title": title,
        "artist-credit": [{"artist": {"id": f"a-{mbid}", "name": artist}}],
    }


class TestQueryTitleVariants:
    def test_a_cover_annotation_is_stripped_first(self) -> None:
        assert impl._recording_query_titles(
            "Doomed (Bring Me the Horizon Cover)"
        ) == ["Doomed", "Doomed (Bring Me the Horizon Cover)"]

    def test_a_dash_version_suffix_is_stripped_first(self) -> None:
        assert impl._recording_query_titles(
            "Everytime We Touch - TEKKNO Version"
        ) == ["Everytime We Touch", "Everytime We Touch - TEKKNO Version"]

    def test_a_trailing_performer_is_stripped(self) -> None:
        """The UI renders "Title — Artist"; an em-dash suffix is not the title."""
        assert impl._recording_query_titles("It's A Sin — Ghost") == [
            "It's A Sin",
            "It's A Sin - Ghost",
        ]

    def test_a_clean_title_is_queried_once(self) -> None:
        assert impl._recording_query_titles("Tainted Love") == ["Tainted Love"]

    def test_an_empty_title_has_no_variant(self) -> None:
        assert impl._recording_query_titles("  ") == []


class TestTheSearchUsesTheCanonicalTitle:
    def test_the_stripped_title_is_queried_and_matches(self) -> None:
        mb = _SearchStub([_recording("rec-doomed", "Doomed", "Maphra")])
        det = _detector(mb)

        results = det._search_recordings_by_title(
            "Doomed (Bring Me the Horizon Cover)", artist="Maphra"
        )

        assert [r["id"] for r in results] == ["rec-doomed"]
        assert 'recording:"Doomed"' in mb.queries[0]

    def test_the_raw_title_is_retried_when_the_stripped_one_finds_nothing(self) -> None:
        # A parenthetical that is part of the real title must still be searched.
        mb = _SearchStub([_recording("rec-live", "Song (Live)", "Band")])
        det = _detector(mb)

        results = det._search_recordings_by_title("Song (Live)", artist="Band")

        assert [r["id"] for r in results] == ["rec-live"]
        assert len(mb.queries) == 2

    def test_the_artist_clause_is_dropped_before_giving_up(self) -> None:
        # The file's ARTIST tag need not match MusicBrainz's credit spelling.
        mb = _SearchStub([_recording("rec-doomed", "Doomed", "Maphra")])
        det = _detector(mb)

        results = det._search_recordings_by_title(
            "Doomed", artist="Maphra / Bring Me the Horizon"
        )

        assert [r["id"] for r in results] == ["rec-doomed"]
        assert any('artist:"' in query for query in mb.queries)
        assert any('artist:"' not in query for query in mb.queries)


class TestTheMetalCoversCompilationResolvesItsRecordings:
    def test_a_cover_annotated_title_resolves_the_recording_mbid(self) -> None:
        mb = _SearchStub([_recording("rec-doomed", "Doomed", "Maphra")])
        det = _detector(mb)

        mbid = det._resolve_recording_mbid(
            {
                "id": "t1",
                "title": "Doomed (Bring Me the Horizon Cover)",
                "artist": "Maphra",
                "album_artist": ALBUM_ARTIST,
            },
            ALBUM_ARTIST,
        )

        assert mbid == "rec-doomed"

    def test_a_version_suffixed_title_resolves_the_recording_mbid(self) -> None:
        mb = _SearchStub(
            [_recording("rec-tekkno", "Everytime We Touch", "Electric Callboy")]
        )
        det = _detector(mb)

        mbid = det._resolve_recording_mbid(
            {
                "id": "t2",
                "title": "Everytime We Touch - TEKKNO Version",
                "artist": "Electric Callboy",
                "album_artist": ALBUM_ARTIST,
            },
            ALBUM_ARTIST,
        )

        assert mbid == "rec-tekkno"

    def test_a_parenthesised_title_resolves_the_recording_mbid(self) -> None:
        mb = _SearchStub([_recording("rec-mea", "Mea Culpa", "Gojira")])
        det = _detector(mb)

        mbid = det._resolve_recording_mbid(
            {
                "id": "t3",
                "title": "Mea Culpa (Ah! Ça ira!)",
                "artist": "Gojira",
                "album_artist": ALBUM_ARTIST,
            },
            ALBUM_ARTIST,
        )

        assert mbid == "rec-mea"
