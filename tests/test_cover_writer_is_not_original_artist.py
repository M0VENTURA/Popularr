"""A work's WRITER is not the original ARTIST.

Reported: Metallica's "So What" was detected as a cover of **Animal**, when the
original is Anti-Nowhere League.

MusicBrainz facts (verified against the live API):

* the recording (Metallica, Garage Inc.) is a ``performance`` of the work
  "So What?" with the ``cover`` attribute;
* the work's writers are Clive Blake, Nick Culmer and Chris Exall — **no**
  artist called Animal;
* Nick Culmer has an ``is person`` relation to the performing alias "Animal"
  (member of Anti-Nowhere League) and a ``member of band`` relation to
  Anti-Nowhere League;
* several **Metallica live** recordings of the work are credited
  ``Metallica feat. Animal`` / ``Metallica & vocals by Animal``.

Two paths therefore produced "Animal":

1. ``_original_by_writer`` scanned EVERY artist credit of each recording, so the
   guest credit on a Metallica live performance was eligible as "the original
   artist". Only the FIRST credit is the performing artist.
2. ``_writer_from_cover_work`` (the last-resort writer fallback) returned the
   writer's personal name. A writer's BAND is the performer; the personal name
   is only a fallback when no performing identity is linked.
"""

from __future__ import annotations

from services.enrichment.cover_detector_impl import CoverDetector

WORK_ID = "work-so-what"
PERFORMER = "Metallica"


def _writer(artist_id: str, name: str) -> dict:
    return {"type": "writer", "artist": {"id": artist_id, "name": name}}


CLIVE = _writer("clive", "Clive Blake")
NICK = _writer("nick", "Nick Culmer")
CHRIS = _writer("chris", "Chris Exall")


NICK_RELATIONS = [
    {"type": "is person", "artist": {"id": "animal", "name": "Animal"}},
    {
        "type": "member of band",
        "artist": {"id": "anl", "name": "Anti-Nowhere League"},
    },
]


class _StubMB:
    """A work whose writers include a person with a performing alias + band."""

    def __init__(self, work_relations, recordings=(), artist_relations=None):
        self._work_relations = list(work_relations)
        self._recordings = list(recordings)
        self._artist_relations = artist_relations or {}
        self.search_queries: list[str] = []

    def get_work(self, work_id, inc=None):
        return {
            "id": work_id,
            "title": "So What?",
            "artist-relation-list": list(self._work_relations),
        }

    def get_artist(self, artist_id, inc=None):
        return {
            "id": artist_id,
            "artist-relation-list": list(self._artist_relations.get(artist_id, [])),
        }

    def browse_work_recordings(self, work_id, inc=None, limit=100, offset=0):
        return list(self._recordings) if offset == 0 else []

    def search_recordings(self, query, limit=15):
        self.search_queries.append(query)
        return []


def _detector(mb) -> CoverDetector:
    det = CoverDetector(db_connection=None)
    det.mb = mb
    return det


GUEST_LIVE_RECORDING = {
    "id": "rec-feat-animal",
    "title": "So What",
    "first-release-date": "2011-12-09",
    "artist-credit": [
        {"artist": {"id": "metallica", "name": "Metallica"}},
        {"artist": {"id": "animal", "name": "Animal"}},
    ],
}

ORIGINAL_RECORDING = {
    "id": "rec-original",
    "title": "So What",
    "first-release-date": "1981",
    "artist-credit": [
        {"artist": {"id": "anl", "name": "Anti-Nowhere League"}},
    ],
}


class TestTheWriterFallbackNamesTheBandNotTheWriter:
    def test_the_band_is_preferred_over_the_writers_alias(self) -> None:
        mb = _StubMB(
            [CLIVE, NICK, CHRIS],
            artist_relations={"nick": NICK_RELATIONS},
        )
        result = _detector(mb)._writer_from_cover_work({WORK_ID}, performer=PERFORMER)

        assert result == {
            "artist": "Anti-Nowhere League",
            "year": None,
            "confidence": "medium",
        }
        assert result["artist"] != "Animal"

    def test_the_writers_name_is_used_when_no_performing_identity_is_linked(
        self,
    ) -> None:
        mb = _StubMB([CLIVE])
        result = _detector(mb)._writer_from_cover_work({WORK_ID}, performer=PERFORMER)

        assert result == {
            "artist": "Clive Blake",
            "year": None,
            "confidence": "medium",
        }


class TestAGuestCreditIsNeverTheOriginalArtist:
    def test_a_featured_credit_is_ignored(self) -> None:
        """⭐ THE REPORTED CASE. Only ``Metallica feat. Animal`` is on the page;
        the verdict must not become "(Animal Cover)"."""
        mb = _StubMB(
            [CLIVE, NICK, CHRIS],
            recordings=[GUEST_LIVE_RECORDING],
            artist_relations={"nick": NICK_RELATIONS},
        )

        result = _detector(mb)._original_by_writer(
            {WORK_ID},
            performer=PERFORMER,
            exclude_mbid="rec-local",
            title_hint="So What",
        )

        assert result is None, result

    def test_the_earliest_original_still_wins_when_present(self) -> None:
        mb = _StubMB(
            [CLIVE, NICK, CHRIS],
            recordings=[GUEST_LIVE_RECORDING, ORIGINAL_RECORDING],
            artist_relations={"nick": NICK_RELATIONS},
        )

        result = _detector(mb)._original_by_writer(
            {WORK_ID},
            performer=PERFORMER,
            exclude_mbid="rec-local",
            title_hint="So What",
        )

        assert result is not None
        assert result["artist"] == "Anti-Nowhere League"
