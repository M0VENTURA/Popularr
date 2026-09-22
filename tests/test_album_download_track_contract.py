"""Regression tests: album download queues per-track rows, never "Unknown Track".

Reported symptom (artist page → add album to the download queue):

    Unknown Track
    Madball - Not Your Kingdom
    Backed off

    Unknown Track
    Mayday Parade - Sugar
    Backed off

Two independent defects produced that:

1. **The track key contract had split into two incompatible shapes.**
   ``fetch_musicbrainz_release_metadata`` emitted ONLY ``mb_*`` keys
   (``mb_title``, ``mb_track_number``, ``mb_disc_number``, ``mb_recording_mbid``,
   ``mb_duration``) because those are what ``_match_mb_tracks_to_library`` and
   ``release_group_tracklist_similarity`` read. But the queue adder
   (``add_release_tracks_to_queue``) — reached from
   ``/api/musicbrainz/download`` (artist page + release picker) and
   ``/api/downloads/queue-upcoming`` (dashboard/upcoming Queue) — reads the PLAIN
   keys. Every field missed, so ``track.get("title") or "Unknown Track"`` fired
   for every track and, with no ``recording_mbid``, the per-track dedupe key
   collapsed to ``"unknown track"`` and discarded all but the first row.

2. The album artist / per-track artist split and the recording work-relation
   enrichment (writer / cover / genres) were lost in the same rewrite.

The fix emits BOTH key sets from one fetch and keeps the defensive readers on
the queue side, so the two halves of the pipeline cannot drift apart again.
"""

from __future__ import annotations

import pytest

from services.enrichment import musicbrainz_service as mbs
from services.queue import queue_processing_service as qps


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

def _release() -> dict:
    """A MusicBrainz release: two tracks, each with its own recording credit."""
    return {
        "id": "rel-not-your-kingdom",
        "title": "Not Your Kingdom",
        "date": "2024-05-01",
        "artist-credit": [{"name": "Madball", "joinphrase": "", "artist": {"id": "art-mb", "name": "Madball"}}],
        "release-group": {
            "id": "rg-1",
            "title": "Not Your Kingdom",
            "first-release-date": "2024-05-01",
            "primary-type": "Album",
            "secondary-types": [],
        },
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "position": 1,
                        "title": "Not Your Kingdom",
                        "length": 180000,
                        "recording": {
                            "id": "rec-1",
                            "artist-credit": [{"name": "Madball"}],
                            "relations": [
                                {
                                    "type": "performance",
                                    "work": {
                                        "id": "work-1",
                                        "title": "Not Your Kingdom",
                                        "iswc": "T-111.222.333-4",
                                        "artist-credit": [{"name": "Madball"}],
                                        "relations": [
                                            {"type": "composer", "artist": {"name": "F. Cricien"}},
                                        ],
                                    },
                                }
                            ],
                            "genres": [{"name": "Hardcore"}],
                        },
                    },
                    {
                        "position": 2,
                        "title": "Sugar",
                        "length": 190000,
                        "recording": {
                            "id": "rec-2",
                            # No genres / relations — the thin-track case.
                        },
                    },
                ],
            }
        ],
    }


class _FakeMbClient:
    def __init__(self, release: dict | None = None):
        self.release = release if release is not None else _release()
        self.requested_inc: str | None = None

    def get_release(self, release_id: str, inc: str = "", **kwargs):
        self.requested_inc = inc
        return self.release


@pytest.fixture
def client(monkeypatch):
    fake = _FakeMbClient()
    monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# 1. The producer must satisfy the queue contract
# ---------------------------------------------------------------------------

QUEUE_READ_KEYS = (
    "title",
    "artist",
    "track_number",
    "disc_number",
    "recording_mbid",
    "duration",
)


class TestProducerSatisfiesQueueContract:
    """Every field ``add_release_tracks_to_queue`` reads must be populated."""

    def test_plain_keys_are_present_and_non_empty(self, client):
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        assert payload is not None
        tracks = payload["tracks"]
        assert len(tracks) == 2

        for track in tracks:
            for key in QUEUE_READ_KEYS:
                assert track.get(key) not in (None, ""), (
                    f"queue reader {key!r} is empty on {track!r} — the download "
                    "would fall back to 'Unknown Track'"
                )

    def test_titles_are_the_real_track_titles(self, client):
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        assert [t["title"] for t in payload["tracks"]] == [
            "Not Your Kingdom",
            "Sugar",
        ]

    def test_the_fetch_requests_work_rels_and_labels(self, client):
        mbs.fetch_musicbrainz_release_metadata("rel-1")
        inc = client.requested_inc or ""
        for needed in ("work-rels", "labels", "genres", "artist-credits"):
            assert needed in inc, (
                f"the release fetch must request {needed!r} or the writer/cover "
                "and Extended Metadata enrichment is never returned"
            )

    def test_track_numbers_and_disc_numbers_are_ints(self, client):
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        first, second = payload["tracks"]
        assert first["track_number"] == 1
        assert second["track_number"] == 2
        assert first["disc_number"] == 1
        # Absolute numbering survives across discs.
        assert first["absolute_track_number"] == 1
        assert second["absolute_track_number"] == 2

    def test_duration_stays_in_musicbrainz_milliseconds(self, client):
        """``duration`` is normalised on consumption by ``queue_duration_seconds``."""
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        assert payload["tracks"][0]["duration"] == 180000
        assert qps.queue_duration_seconds(180000) == 180.0


class TestProducerKeepsTheMbKeys:
    """The ``mb_*`` keys are what the compare/similarity helpers predicate on."""

    def test_mb_keys_are_still_emitted(self, client):
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        track = payload["tracks"][0]
        assert track["mb_title"] == "Not Your Kingdom"
        assert track["mb_track_number"] == 1
        assert track["mb_disc_number"] == 1
        assert track["mb_recording_mbid"] == "rec-1"
        assert track["mb_duration"] == 180000

    def test_mb_and_plain_keys_agree(self, client):
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        for track in payload["tracks"]:
            assert track["mb_title"] == track["title"]
            assert track["mb_recording_mbid"] == track["recording_mbid"]
            assert track["mb_disc_number"] == track["disc_number"]
            assert track["mb_duration"] == track["duration"]

    def test_tracklist_similarity_still_scores_a_real_match(self, client, monkeypatch):
        monkeypatch.setattr(
            mbs,
            "fetch_musicbrainz_release_metadata",
            lambda _rid: mbs._flatten_release(_release(), _rid),
        )
        library = [
            {"title": "Not Your Kingdom", "track_number": "1", "disc_number": 1},
            {"title": "Sugar", "track_number": "2", "disc_number": 1},
        ]
        similarity = mbs.release_group_tracklist_similarity(
            "rg-1", library, prefer_release_id="rel-1"
        )
        assert similarity == 1.0, (
            "the album compare now reads blanks, so a fully-owned album scores 0"
        )


class TestProducerEnrichment:
    """Work-relation enrichment must survive the dual-shape emit."""

    def test_writer_composer_work_and_genres(self, client):
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        track = payload["tracks"][0]
        assert track["composer"] == "F. Cricien"
        assert track["writer"] == "F. Cricien"
        assert track["work_mbid"] == "work-1"
        assert track["iswc"] == "T-111.222.333-4"
        # Comma-joined STRING on the tag surface, list on the mb_ surface.
        assert track["musicbrainz_genres"] == "Hardcore"
        assert track["mb_genres"] == ["Hardcore"]

    def test_a_track_without_relations_omits_the_enrichment(self, client):
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        second = payload["tracks"][1]
        assert "composer" not in second
        assert "work_mbid" not in second
        assert "musicbrainz_genres" not in second

    def test_album_artist_is_primary_and_credit_keeps_the_join(self, monkeypatch):
        release = _release()
        release["artist-credit"] = [
            {"name": "Weezer", "joinphrase": " & ", "artist": {"id": "a1"}},
            {"name": "Rivers Cuomo", "joinphrase": "", "artist": {"id": "a2"}},
        ]
        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeMbClient(release))

        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        # Primary only — a joined album artist made Navidrome split the album.
        assert payload["artist"] == "Weezer"
        assert payload["artist_credit"] == "Weezer & Rivers Cuomo"
        # The joined credit still reaches the track.
        assert payload["tracks"][0]["artist"] == "Madball"


class TestFetchReleaseMetadataEntryPointsAgree:
    """Both public fetchers must return the same dual-shape payload."""

    def test_fetch_release_metadata_honours_the_registered_service(self, monkeypatch):
        fake = _FakeMbClient()

        class _Service:
            http = fake

        monkeypatch.setattr(mbs, "_get_service", lambda: _Service())
        payload = mbs.fetch_release_metadata("rel-1")
        assert payload is not None
        assert [t["title"] for t in payload["tracks"]] == [
            "Not Your Kingdom",
            "Sugar",
        ]
        assert payload["tracks"][0]["mb_title"] == "Not Your Kingdom"


# ---------------------------------------------------------------------------
# 2. The queue adder must store real per-track rows
# ---------------------------------------------------------------------------

class _FakeResult:
    """SQLAlchemy-Result stand-in: ``INSERT ... RETURNING id`` + empty SELECTs."""

    def __init__(self, value=None, rows=()):
        self._value = value
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._value

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeSession:
    """Captures INSERTs and answers the pre-flight SELECTs with "no match"."""

    def __init__(self):
        self.inserted: list[dict] = []

    def execute(self, statement, params=None, *args, **kwargs):
        sql = str(statement)
        if "INSERT INTO download_queue" in sql:
            self.inserted.append(dict(params or {}))
            return _FakeResult(len(self.inserted))
        # The stale-row pre-flight / duplicate lookups: nothing found.
        return _FakeResult(None, rows=[])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_session(monkeypatch) -> _FakeSession:
    session = _FakeSession()
    monkeypatch.setattr(qps, "db_session", lambda *a, **k: session)
    monkeypatch.setattr(qps, "find_library_track", lambda **k: None)
    monkeypatch.setattr(qps, "signal_new_item", lambda: None)
    return session


class TestQueueAdderStoresRealRows:
    def test_musicbrainz_payload_never_produces_unknown_track(self, client, monkeypatch):
        """End-to-end: the artist-page download path must not queue 'Unknown Track'."""
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")

        session = _install_fake_session(monkeypatch)
        queue_ids = qps.add_release_tracks_to_queue(
            "rel-1",
            payload["tracks"],
            "Madball",
            "Not Your Kingdom",
            album_artist="Madball",
            year=2024,
        )

        assert len(queue_ids) == 2, (
            "a two-track album must create two rows; a shared 'Unknown Track' "
            "dedupe key collapsed them into one"
        )
        titles = [row["title"] for row in session.inserted]
        assert titles == ["Not Your Kingdom", "Sugar"]
        assert "Unknown Track" not in titles

    def test_mbid_and_positions_are_persisted(self, client, monkeypatch):
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        session = _install_fake_session(monkeypatch)
        qps.add_release_tracks_to_queue(
            "rel-1", payload["tracks"], "Madball", "Not Your Kingdom"
        )

        by_title = {row["title"]: row for row in session.inserted}
        assert by_title["Not Your Kingdom"]["recording_mbid"] == "rec-1"
        assert by_title["Sugar"]["recording_mbid"] == "rec-2"
        assert by_title["Not Your Kingdom"]["track_number"] == 1
        assert by_title["Sugar"]["track_number"] == 2
        # Duration is normalised to SECONDS on the way into the queue.
        assert by_title["Not Your Kingdom"]["duration"] == 180.0

    def test_thin_mb_shape_is_still_readable(self, monkeypatch):
        """The defensive readers: an mb_*-only track must still queue correctly.

        This is the guard that would have caught the original split — the adder
        accepts either shape rather than silently storing 'Unknown Track'.
        """
        session = _install_fake_session(monkeypatch)
        qps.add_release_tracks_to_queue(
            "rel-1",
            [
                {
                    "mb_title": "Not Your Kingdom",
                    "mb_track_number": 1,
                    "mb_disc_number": 1,
                    "mb_recording_mbid": "rec-1",
                    "mb_duration": 180000,
                }
            ],
            "Madball",
            "Not Your Kingdom",
        )

        assert len(session.inserted) == 1
        row = session.inserted[0]
        assert row["title"] == "Not Your Kingdom"
        assert row["track_number"] == 1
        assert row["recording_mbid"] == "rec-1"
        assert row["duration"] == 180.0

    def test_mb_genres_list_reaches_the_metadata_jsonb(self, client, monkeypatch):
        payload = mbs.fetch_musicbrainz_release_metadata("rel-1")
        session = _install_fake_session(monkeypatch)
        qps.add_release_tracks_to_queue(
            "rel-1", payload["tracks"], "Madball", "Not Your Kingdom"
        )
        first = session.inserted[0]
        assert "Hardcore" in first["metadata"]
        assert "F. Cricien" in first["metadata"]
