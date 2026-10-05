"""Regression tests for scan-pipeline bugs.

Covers:
- ``track_stage`` must not clobber album-type columns with the stale
  in-memory value loaded before album enrichment ran (the album page showed
  "Unknown" after a full scan).
- ``ensure_album_type`` returns the detected/reused verdict so the runner's
  singles-only pass can feed it to per-track singles detection.
"""

from __future__ import annotations

import pytest


class TestAlbumTypeNotClobbered:
    """track_stage persistence must preserve album-type columns."""

    def test_strip_album_type_columns_removes_unset_columns(self):
        from services.popularity.stages.track_stage import _strip_album_type_columns

        track = {
            "id": "t1",
            "title": "Song",
            "musicbrainz_albumtype": "",  # stale value loaded before enrich_album
            "spotify_album_type": "album",
            "releasetype": "album",
            "final_score": 50.0,
        }
        payload = {"final_score": 55.0}  # track stage only updates scoring

        stripped = _strip_album_type_columns(track, payload)

        # Album-type columns the track stage didn't update are dropped so the
        # DB keeps whatever the album stage persisted.
        assert "musicbrainz_albumtype" not in stripped
        assert "spotify_album_type" not in stripped
        assert "releasetype" not in stripped
        assert stripped["id"] == "t1"
        # Title is dropped too when the track stage didn't rename it: the
        # album stage renames covers ("Song (Artist Cover)") and live tracks
        # AFTER track contexts are prepared, so the loaded title is stale and
        # the upsert would clobber the rename.
        assert "title" not in stripped
        # Fresh scoring updates ARE applied — the original implementation only
        # copied album-type columns from the payload, silently dropping every
        # freshly-computed score/listener/single-detection value the track
        # stage produced (regression introduced alongside the album-type fix).
        assert stripped["final_score"] == 55.0

    def test_strip_album_type_columns_drops_stale_album_mbids(self):
        from services.popularity.stages.track_stage import _strip_album_type_columns

        # Track loaded from DB BEFORE album enrichment ran — the album MBID
        # columns are stale (empty here) and the album stage will have
        # persisted freshly-resolved values for them.  The track upsert must
        # NOT write these stale values back (that would clobber the release
        # MBID / release-group MBID the album stage just stored).
        track = {
            "id": "t1",
            "title": "Song",
            "musicbrainz_album_mbid": "",
            "musicbrainz_albumid": "",
            "musicbrainz_releasegroupid": "",
            "final_score": 50.0,
        }
        payload = {"final_score": 55.0}

        stripped = _strip_album_type_columns(track, payload)

        assert "musicbrainz_album_mbid" not in stripped
        assert "musicbrainz_albumid" not in stripped
        assert "musicbrainz_releasegroupid" not in stripped
        assert stripped["final_score"] == 55.0

    def test_strip_album_type_columns_keeps_explicit_updates(self):
        from services.popularity.stages.track_stage import _strip_album_type_columns

        track = {"id": "t1", "title": "Old Name", "musicbrainz_albumtype": ""}
        payload = {"musicbrainz_albumtype": "single", "title": "New Name"}

        stripped = _strip_album_type_columns(track, payload)

        # If the track stage explicitly produced an album type, it is kept.
        assert stripped["musicbrainz_albumtype"] == "single"
        # An explicit title rename from the track stage is kept as well.
        assert stripped["title"] == "New Name"

    def test_strip_album_type_columns_applies_fresh_scan_fields(self):
        from services.popularity.stages.track_stage import _strip_album_type_columns

        # Track loaded from DB carries stale scan data; the track stage's
        # update_payload holds the freshly-computed values which must win.
        track = {
            "id": "t1", "title": "Song",
            "final_score": 0.0, "lastfm_listeners": 0,
            "listenbrainz_listens": 0, "is_single": False,
            "single_confidence": "", "recording_mbid": "",
            "musicbrainz_albumtype": "",
        }
        payload = {
            "final_score": 67.2, "lastfm_listeners": 2653,
            "listenbrainz_listens": 386, "is_single": True,
            "single_confidence": "high", "recording_mbid": "rec-mbid",
        }

        stripped = _strip_album_type_columns(track, payload)

        assert stripped["final_score"] == 67.2
        assert stripped["lastfm_listeners"] == 2653
        assert stripped["listenbrainz_listens"] == 386
        assert stripped["is_single"] is True
        assert stripped["single_confidence"] == "high"
        assert stripped["recording_mbid"] == "rec-mbid"
        # Stale album-type column (not in payload) is dropped, not clobbered.
        assert "musicbrainz_albumtype" not in stripped


class TestEnsureAlbumTypeReturnsVerdict:
    """ensure_album_type should return the reused/detected album type."""

    def test_returns_stored_consistent_verdict(self):
        from services.popularity.stages.album_stage import ensure_album_type

        album_row = {
            "artist": "Muse",
            "album": "Absolution",
            "tracks": [
                {"id": "t1", "musicbrainz_albumtype": "album"},
                {"id": "t2", "musicbrainz_albumtype": "album"},
            ],
        }
        assert ensure_album_type(album_row) == "album"

    def test_returns_none_when_no_artist(self):
        from services.popularity.stages.album_stage import ensure_album_type

        assert ensure_album_type({"artist": "", "album": "X"}) is None

    def test_detects_when_missing(self, monkeypatch):
        from services.popularity.stages.album_stage import ensure_album_type
        import services.popularity.stages.album_stage as album_stage

        # No stored verdict + no network: name-based detection returns "album".
        monkeypatch.setattr(album_stage, "_lookup_musicbrainz_album_type", lambda artist, album: (None, None))

        # Avoid opening a real DB connection — the persist helper is patched
        # away so only the detection path is exercised.
        persisted = {}

        def fake_persist(conn, cursor, artist, album, tracks, album_type, rg_mbid):
            persisted["album_type"] = album_type

        monkeypatch.setattr(album_stage, "_persist_album_type_to_tracks", fake_persist)

        album_row = {
            "artist": "Muse",
            "album": "Absolution",
            "album_artist": "Muse",
            "tracks": [{"id": "t1", "musicbrainz_albumtype": None}],
        }
        assert ensure_album_type(album_row) == "album"
        assert persisted.get("album_type") == "album"


class TestEnrichAlbumUnpacking:
    """enrich_album must not raise when _run_full_enrichment returns a 2-tuple.

    Regression: metadata-only scans run enrichment inline (defer_full_enrichment
    is False), and enrich_album used to unpack 3 values from
    ``_run_full_enrichment`` — which returns ``(meta, similar)`` — raising
    "not enough values to unpack (expected 3, got 2)" and logging
    "[album_stage] Enrichment failed for '<artist> - <album>'".
    """

    def test_metadata_pass_unpacks_two_values(self, monkeypatch):
        from services.popularity.stages.album_stage import enrich_album
        import services.popularity.stages.album_stage as album_stage

        # No network / DB: MB album-type lookup and the persist are patched
        # away, and _run_full_enrichment is stubbed to return its real
        # 2-tuple contract (meta, similar).
        monkeypatch.setattr(album_stage, "_lookup_musicbrainz_album_type", lambda artist, album: (None, None))
        monkeypatch.setattr(album_stage, "_persist_album_type_to_tracks", lambda *a, **k: None)
        monkeypatch.setattr(
            album_stage,
            "_run_full_enrichment",
            lambda *a, **k: (
                {"country": None, "bio": None, "image_url": None},
                {"lastfm": [], "listenbrainz": []},
            ),
        )

        album_row = {
            "artist": "dArtagnan",
            "album": "Feuer & Flamme",
            "album_artist": "dArtagnan",
            "tracks": [{"id": "t1", "title": "Feuer & Flamme"}],
        }
        result = enrich_album(
            album_row=album_row,
            album_context={},
            stat_eligible_tracks=[],
            options={"defer_full_enrichment": False},  # metadata-only scan
        )

        # The unpacking bug raised before returning; now it must succeed and
        # surface the enrichment results.
        assert result["detected_album_type"] == "album"
        assert result["is_heterogeneous"] is False
        assert result["artist_metadata"]["country"] is None
        assert result["similar_artists"] == {"lastfm": [], "listenbrainz": []}

    def test_full_scan_deferred_path_still_works(self, monkeypatch):
        from services.popularity.stages.album_stage import enrich_album
        import services.popularity.stages.album_stage as album_stage

        # Full scans defer the heavy enrichment (defer_full_enrichment=True):
        # only the MB artist ID is fetched now, so no 3-tuple unpack happens.
        monkeypatch.setattr(album_stage, "_lookup_musicbrainz_album_type", lambda artist, album: (None, None))
        monkeypatch.setattr(album_stage, "_persist_album_type_to_tracks", lambda *a, **k: None)
        monkeypatch.setattr(album_stage, "_fetch_musicbrainz_artist_id", lambda *a, **k: None)

        album_row = {
            "artist": "dArtagnan",
            "album": "Herzblut",
            "album_artist": "dArtagnan",
            "tracks": [{"id": "t1", "title": "Herzblut"}],
        }
        result = enrich_album(
            album_row=album_row,
            album_context={},
            stat_eligible_tracks=[],
            options={"defer_full_enrichment": True},  # full scan
        )

        assert result["detected_album_type"] == "album"
        assert result["artist_metadata"]["country"] is None


class TestSinglesOnlyPass:
    """process_track must run singles detection in singles-detection-only mode
    while skipping the metadata/popularity/cover/genre sections."""

    def _run_process_track(self, monkeypatch, track, options=None, track_context=None):
        import services.popularity.stages.track_stage as ts

        calls = {"metadata": 0, "popularity": 0, "cover": 0, "genre": 0, "singles": 0, "persist": 0}
        results = []

        def _track_phase(*a, **k):
            if options and options.get("metadata_only"):
                return "Metadata"
            if options and (options.get("singles_only") or options.get("singles_with_missing_popularity")):
                return "Singles"
            if options and options.get("popularity_only"):
                return "Popularity"
            return "Full"

        original = ts.process_track

        # Patch the expensive API clients so no network/DB is touched, and
        # record which sections actually ran by wrapping detection entry points.
        monkeypatch.setattr(ts, "LastFmClient", lambda *a, **k: object())
        monkeypatch.setattr(ts, "ListenBrainzClient", lambda *a, **k: object())
        monkeypatch.setattr(ts, "get_shared_mb_service", lambda *a, **k: object())
        monkeypatch.setattr(ts, "get_shared_mb_client", lambda *a, **k: object())
        monkeypatch.setattr(ts, "get_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_aggregated_listenbrainz_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_search_aggregated_lastfm_popularity", lambda *a, **k: {})

        def fake_detect(title=None, artist=None, album_track_count=1, popularity=0,
                        album_type=None, album=None, isrc=None, duration=None,
                        use_advanced_detection=True, persist_result=False,
                        mb_cached_singles=None, discogs_cached_singles=None,
                        discogs_cached_promos=None,
                        artist_mbid=None, recording_mbid=None,
                        listenbrainz_listens=0,
                        lastfm_listeners=0, album_lf_listeners=None,
                        album_lb_listens=None,
                        discogs_token=None, lastfm_client=None, mb_client=None,
                        is_va_compilation=False, **kwargs):
            calls["singles"] += 1
            return {"is_single": True, "confidence": "high", "confidence_score": 0.9,
                    "single_status": "high", "sources": [{"source": "musicbrainz", "matched": True}],
                    "reasons": ["mb"]}

        monkeypatch.setattr(ts, "detect_single_for_track", fake_detect)

        def fake_insert(track_id, data):
            calls["persist"] += 1

        monkeypatch.setattr(ts, "insert_or_update_track", fake_insert)

        result = original(
            track=track,
            track_context=track_context or {"artist": track.get("artist"), "album": track.get("album")},
            album_context={"album": track.get("album"), "artist": track.get("artist"), "tracks": [track]},
            album_result={"detected_album_type": "album", "is_heterogeneous": False},
            options=options or {},
        )
        return result, calls

    def test_singles_only_runs_detection_without_popularity(self, monkeypatch):
        track = {
            "id": "t1",
            "artist": "Muse",
            "album": "Absolution",
            "title": "Hysteria",
            "final_score": 50.0,
            "lastfm_listeners": 1000,
            "listenbrainz_listens": 2000,
            "lb_percentile": 0.9,
            "single_detection_last_updated": None,
        }
        result, calls = self._run_process_track(
            monkeypatch,
            track,
            options={"singles_detection_only": True},
        )
        assert result is not None
        assert calls["singles"] == 1
        # Stored popularity is carried through, not zeroed.
        assert result["popularity_score"] == 50.0
        assert result["is_single"] is True
        assert result["single_confidence"] == "high"

    def test_singles_only_carries_stored_single_state_when_fresh(self, monkeypatch):
        # When singles detection is fresh, stored is_single/confidence/sources
        # must be carried into the result so per-album output still shows them.
        import datetime as _dt

        track = {
            "id": "t1",
            "artist": "Muse",
            "album": "Absolution",
            "title": "Hysteria",
            "final_score": 50.0,
            "is_single": True,
            "single_confidence": "high",
            "single_sources": '[{"source":"musicbrainz","matched":true}]',
            "single_detection_last_updated": _dt.datetime.now(_dt.timezone.utc),
            "lastfm_listeners": 1000,
            "listenbrainz_listens": 2000,
        }
        result, calls = self._run_process_track(
            monkeypatch,
            track,
            options={"singles_detection_only": True},
        )
        assert result is not None
        # Fresh gate means detection itself is skipped, but the stored verdict
        # is preserved in the returned result.
        assert result["is_single"] is True
        assert result["single_confidence"] == "high"
        assert result["single_sources"]
        assert result["popularity_score"] == 50.0


class TestSinglesPassPopularityGating:
    """A standalone singles scan only fetches popularity for tracks WITHOUT
    stored popularity data.

    ``singles_only`` / ``singles_with_missing_popularity`` run singles
    detection as the whole point; a track that already carries popularity data
    reuses it (singles detection only needs SOME score signal for its
    z-score / top-50% gates), while a track with NO stored data gets a
    popularity fetch because detection cannot work without one.
    """

    def _run(self, monkeypatch, track, options):
        import services.popularity.stages.track_stage as ts

        calls = {"fetch_popularity": 0, "singles": 0}
        monkeypatch.setattr(ts, "LastFmClient", lambda *a, **k: object())
        monkeypatch.setattr(ts, "ListenBrainzClient", lambda *a, **k: object())
        monkeypatch.setattr(ts, "get_shared_mb_service", lambda *a, **k: object())
        monkeypatch.setattr(ts, "get_shared_mb_client", lambda *a, **k: object())
        monkeypatch.setattr(ts, "get_search_aggregated_lastfm_popularity", lambda *a, **k: {})

        def fake_agg(*a, **k):
            calls["fetch_popularity"] += 1
            return {"listeners": 5000, "track_play": 9000, "matched_tracks": []}

        monkeypatch.setattr(ts, "get_aggregated_lastfm_popularity", fake_agg)
        monkeypatch.setattr(ts, "get_aggregated_listenbrainz_popularity", lambda *a, **k: {})

        # Give the popularity section a Last.fm key so a run actually fetches
        # (the section's config read goes through helpers.config_helpers).
        monkeypatch.setattr(
            "helpers.config_helpers.get_config",
            lambda: {"api_integrations": {"lastfm": {"api_key": "test-key"}}},
        )

        def fake_detect(**kwargs):
            calls["singles"] += 1
            return {"is_single": True, "confidence": "high", "confidence_score": 0.9,
                    "single_status": "high", "sources": [{"source": "musicbrainz", "matched": True}],
                    "reasons": ["mb"]}

        monkeypatch.setattr(ts, "detect_single_for_track", fake_detect)
        monkeypatch.setattr(ts, "insert_or_update_track", lambda *a, **k: None)

        result = ts.process_track(
            track=track,
            track_context={"artist": track.get("artist"), "album": track.get("album")},
            album_context={"album": track.get("album"), "artist": track.get("artist"), "tracks": [track]},
            album_result={"detected_album_type": "album", "is_heterogeneous": False},
            options=options,
        )
        return result, calls

    def test_singles_only_reuses_stored_popularity(self, monkeypatch):
        track = {
            "id": "t1", "artist": "Muse", "album": "Absolution", "title": "Hysteria",
            "final_score": 72.0, "popularity": 72.0,
            "lastfm_listeners": 1000, "listenbrainz_listens": 2000,
            "lb_percentile": 0.9, "single_detection_last_updated": None,
        }
        result, calls = self._run(monkeypatch, track, options={"singles_only": True})
        assert result is not None
        # Stored popularity reused, NO popularity fetch.
        assert calls["fetch_popularity"] == 0
        assert result["popularity_score"] == 72.0
        assert result["lastfm_listeners"] == 1000
        assert result["listenbrainz_listens"] == 2000
        # Singles detection still ran.
        assert calls["singles"] == 1
        assert result["is_single"] is True

    def test_singles_only_fetches_when_popularity_missing(self, monkeypatch):
        track = {
            "id": "t1", "artist": "Muse", "album": "Absolution", "title": "Hysteria",
            "final_score": 0.0, "popularity": 0.0,
            "lastfm_listeners": 0, "listenbrainz_listens": 0,
            "single_detection_last_updated": None,
        }
        result, calls = self._run(monkeypatch, track, options={"singles_only": True})
        assert result is not None
        # No stored popularity → fetched (required for singles detection).
        assert calls["fetch_popularity"] == 1
        assert calls["singles"] == 1
        # Freshly-fetched count flowed through to the result.
        assert result["lastfm_listeners"] == 5000

    def test_singles_with_missing_popularity_also_gates(self, monkeypatch):
        track = {
            "id": "t1", "artist": "Muse", "album": "Absolution", "title": "Hysteria",
            "final_score": 55.0, "popularity": 55.0,
            "lastfm_listeners": 800, "listenbrainz_listens": 900,
            "single_detection_last_updated": None,
        }
        result, calls = self._run(
            monkeypatch, track, options={"singles_with_missing_popularity": True}
        )
        assert result is not None
        assert calls["fetch_popularity"] == 0
        assert result["popularity_score"] == 55.0
        assert calls["singles"] == 1


class TestAlbumTypeProtectedFromNavidromeSync:
    """A Navidrome metadata sync must never wipe album-type columns.

    The album type is owned by the album stage's enrichment pass (detected via
    MusicBrainz release-group + name heuristics).  A Navidrome import/sync
    reads ``releasetype``/``albumtype`` from file tags, which are usually
    empty, so writing them back would clobber the type the scan just persisted
    and the album page would fall back to "Unknown" after the next library
    sync / boot import / navidrome scan.
    """

    def test_album_type_columns_are_navidrome_protected(self):
        from db.repositories.popularity_repository import _POPULARITY_PROTECTED_COLUMNS

        for col in ("musicbrainz_albumtype", "spotify_album_type", "releasetype"):
            assert col in _POPULARITY_PROTECTED_COLUMNS, (
                f"{col} must be protected from _navidrome_sync overwrites"
            )

    def test_save_to_db_builds_navidrome_update_set_without_album_type(self):
        # ``_execute_save`` builds the UPDATE SET clause by skipping protected
        # columns when ``_navidrome_sync`` is True.  Verify the album-type
        # columns are now excluded so the stored value survives.
        import db.repositories.popularity_repository as repo

        payload = {
            "_navidrome_sync": True,
            "id": "t1",
            "title": "Song",
            "musicbrainz_albumtype": "",   # empty tag value
            "releasetype": "",
        }

        seen = {}

        class _FakeResult:
            def fetchall(self):
                return []

        class _FakeCursor:
            pass

        class _FakeSession:
            def execute(self, statement, params):
                seen["statement"] = str(statement)
                seen["params"] = params
                return _FakeResult()

        # get_tracks_table_columns/types are cached and read via the real
        # session; monkeypatch them so the test is self-contained.
        repo._TRACKS_COLUMN_CACHE = {"id", "title", "musicbrainz_albumtype", "releasetype"}
        repo._TRACKS_COLUMN_TYPES_CACHE = {
            "id": "text", "title": "text",
            "musicbrainz_albumtype": "text", "releasetype": "text",
        }

        try:
            repo._execute_save(_FakeSession(), dict(payload))
        finally:
            repo._TRACKS_COLUMN_CACHE = None
            repo._TRACKS_COLUMN_TYPES_CACHE = None

        statement = seen.get("statement", "")
        # The UPDATE SET clause must not touch the album-type columns.
        assert "musicbrainz_albumtype=EXCLUDED.musicbrainz_albumtype" not in statement
        assert "releasetype=EXCLUDED.releasetype" not in statement


# ---------------------------------------------------------------------------
# The scan's own album-type write must FILL, never overwrite
# ---------------------------------------------------------------------------
class TestAlbumTypePersistenceIsFillOnly:
    """The popularity scan must not undo the Album Type the user just chose.

    Reported: *"I'm updating the metadata for albums, but something is
    bringing back the old versions ... Everything I set"* — with **Album Type**
    named explicitly, and noticed "a few hours later, seems to be from a
    Navidrome scan".

    The mechanism: the album page's select READS ``musicbrainz_albumtype``
    first (``album_data`` prefers it) and its Save WRITES it — while this scan
    used to overwrite every track whose stored value merely *differed* from
    MusicBrainz's. So a type chosen on the page was reverted to MusicBrainz's
    value by the next popularity scan.
    """

    _COLUMNS = ("musicbrainz_albumtype", "spotify_album_type", "releasetype")

    def _seed(self, track_id: str, albumtype: str) -> None:
        from sqlalchemy import text

        from db.engine import db_session
        from db.models import Track

        with db_session() as session:
            Track.__table__.create(session.get_bind(), checkfirst=True)
            # Another test file may have created a reduced `tracks`; repair the
            # columns this test needs rather than trusting collection order.
            present = {
                str(row[1])
                for row in session.execute(text("PRAGMA table_info(tracks)")).fetchall()
            }
            for column in (*self._COLUMNS,):
                if column not in present:
                    session.execute(text(
                        f"ALTER TABLE tracks ADD COLUMN {column} TEXT"
                    ))
            session.execute(text("DELETE FROM tracks WHERE id = :id"), {"id": track_id})
            session.execute(
                text(
                    "INSERT INTO tracks (id, artist, album_artist, album, title, "
                    " musicbrainz_albumtype, spotify_album_type, releasetype) "
                    "VALUES (:id, 'Muse', 'Muse', 'Absolution', 'Song', "
                    "        :t, :t, :t)"
                ),
                {"id": track_id, "t": albumtype},
            )
            session.commit()

    def _read(self, track_id: str) -> str:
        from sqlalchemy import text

        from db.engine import db_session

        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT musicbrainz_albumtype, spotify_album_type, releasetype "
                    "FROM tracks WHERE id = :id"
                ),
                {"id": track_id},
            ).first()
        return "" if row is None else str(row[0] or "")

    def _persist(self, track_id: str, detected: str) -> None:
        import services.popularity.stages.album_stage as album_stage

        album_stage._persist_album_type_to_tracks(
            "Muse",
            "Absolution",
            tracks=[{"id": track_id}],
            album_type=detected,
            release_group_mbid=None,
        )

    def test_a_set_type_survives_a_disagreeing_musicbrainz_value(self):
        """The reported revert: the page says Soundtrack, the scan says Album."""
        self._seed("type-manual", "album+soundtrack")

        self._persist("type-manual", "album")

        assert self._read("type-manual") == "album+soundtrack", (
            "a type the user chose was overwritten by MusicBrainz's value on "
            "the next scan — the select read it back as the old version"
        )

    def test_a_track_with_no_type_is_still_filled(self):
        """CONTROL — fill-only must not stop the detection populating the field."""
        self._seed("type-empty", "")

        self._persist("type-empty", "album")

        assert self._read("type-empty") == "album"

