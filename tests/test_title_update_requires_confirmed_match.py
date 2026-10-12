"""A track title is auto-updated ONLY when the release match is confirmed.

Required by the user:

> When updating a track title, it should only update if the track number and
> track length are matched. Anything that is flagged as potentially wrong with
> the match should leave it only as a recommendation and post to "metadata
> requiring approval" on the dashboard.

Three levels of match confidence flow through the batch identity:

- CONFIRMED = the identity came from the position-qualified alias (the local
  track NUMBER matched a release position) AND the +/-5s length guard VERIFIED
  both durations (both sides were known).  Only these titles are auto-applied.
- POTENTIALLY WRONG = a position match whose length was never verified (either
  side's duration missing), OR the title-keyed fallback (no track-number
  guarantee at all).  Their release titles are deferred to
  ``tracks.pending_mb_updates`` — the dashboard's "metadata requiring
  approval" panel.
- SEARCH = no batch identity at all (``_from_batch`` False).  Not promoted
  (unchanged), and not recommended either — a title-search result is too
  unstable to offer as a change.

The stash helper merges into an existing envelope, never clears the album,
and respects ``mb_ignored_fields``.
"""

from __future__ import annotations

import json

import pytest

import services.popularity.stages.track_stage as track_stage


def _mbid(seed: int) -> str:
    return f"{seed:08d}-0000-0000-0000-{seed:012d}"


def _batch_key(artist: str, title: str, disc=1, track=1):
    from services.popularity.popularity_sources import album_recording_batch_key

    return album_recording_batch_key(artist, title, disc, track)


def _resolve(monkeypatch, *, batch_entry, gate=None, local_title="My First and Last"):
    monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
    monkeypatch.setattr(track_stage, "get_shared_mb_service",
                        lambda: pytest.fail("batch hit must not search"))
    if gate == "recommend_only":
        monkeypatch.setattr(track_stage, "_metadata_update_allowed", lambda: False)
    else:
        monkeypatch.setattr(track_stage, "_metadata_update_allowed", lambda: True)

    return track_stage._resolve_track_mb_metadata(
        track_id="t1",
        track={
            "title": local_title,
            "artist": "NCT DREAM",
            "recording_mbid": "",
            "musicbrainz_genres": '["K-pop"]',
            "disc_number": 1,
            "track_number": 3,
        },
        track_title=local_title,
        track_artist="NCT DREAM",
        frozen_track=(gate == "frozen"),
        force_meta=False,
        options={
            "mb_batch_metadata": {
                _batch_key("nct dream", local_title, 1, 3): batch_entry
            }
        },
    )


class TestConfirmedMatchPromotes:
    def test_position_and_length_verified_promotes(self, monkeypatch):
        result = _resolve(monkeypatch, batch_entry={
            "recording_mbid": _mbid(3),
            "title": "My First and Last (Chinese version)",
            "_title_match_confirmed": True,
        })
        assert result["payload"]["title"] == "My First and Last (Chinese version)"
        assert result.get("title_recommendation") is None


class TestUnconfirmedMatchDeferredToApproval:
    def test_title_keyed_fallback_is_not_promoted(self, monkeypatch):
        """The batch entry has NO _title_match_confirmed (title-keyed fallback
        or length not verified) — the title must go to approval, not apply."""
        result = _resolve(monkeypatch, batch_entry={
            "recording_mbid": _mbid(3),
            "title": "My First and Last (Chinese version)",
            # no _title_match_confirmed key
        })

        assert "title" not in (result["payload"] or {}), (
            "an unconfirmed match must NEVER rewrite the title"
        )
        rec = result.get("title_recommendation")
        assert rec is not None
        assert rec["current"] == "My First and Last"
        assert rec["proposed"] == "My First and Last (Chinese version)"

    def test_position_without_verified_length_is_deferred(self, monkeypatch):
        result = _resolve(monkeypatch, batch_entry={
            "recording_mbid": _mbid(3),
            "title": "My First and Last (Chinese version)",
            "_title_match_confirmed": False,  # length never verified
        })
        assert "title" not in (result["payload"] or {})
        assert result.get("title_recommendation") is not None

    def test_unconfirmed_ignored_title_gets_nothing(self, monkeypatch):
        """The Compare UI's Ignore button suppresses the recommendation too."""
        track = {
            "title": "My First and Last",
            "artist": "NCT DREAM",
            "recording_mbid": "",
            "musicbrainz_genres": '["K-pop"]',
            "disc_number": 1,
            "track_number": 3,
            "mb_ignored_fields": '["title"]',
        }
        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(track_stage, "get_shared_mb_service",
                            lambda: pytest.fail("batch hit must not search"))
        monkeypatch.setattr(track_stage, "_metadata_update_allowed", lambda: True)
        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track=track,
            track_title="My First and Last",
            track_artist="NCT DREAM",
            frozen_track=False,
            force_meta=False,
            options={
                "mb_batch_metadata": {
                    _batch_key("nct dream", "My First and Last", 1, 3): {
                        "recording_mbid": _mbid(3),
                        "title": "My First and Last (Chinese version)",
                        "_title_match_confirmed": False,
                    }
                }
            },
        )
        assert "title" not in (result["payload"] or {})
        assert result.get("title_recommendation") is None

    def test_recommend_only_mode_defers_nothing(self, monkeypatch):
        """In recommend-only mode the album-level stash owns recommendations;
        the per-track branch stays silent (no recommendation, no promote)."""
        result = _resolve(monkeypatch, gate="recommend_only", batch_entry={
            "recording_mbid": _mbid(3),
            "title": "My First and Last (Chinese version)",
            "_title_match_confirmed": False,
        })
        assert "title" not in (result["payload"] or {})
        assert result.get("title_recommendation") is None

    def test_search_result_never_recommends(self, monkeypatch):
        """A search result has no batch identity — no promote AND no review."""
        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(track_stage, "_metadata_update_allowed", lambda: True)

        class _Svc:
            def lookup_recording_metadata(self, *a, **k):
                return {
                    "recording_mbid": _mbid(9),
                    "title": "Found (Version)",
                    "genres": ["x"],
                }

        monkeypatch.setattr(track_stage, "get_shared_mb_service", lambda: _Svc())
        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={"title": "Found", "artist": "Artist", "recording_mbid": "",
                   "musicbrainz_genres": '["x"]', "disc_number": 1, "track_number": 1},
            track_title="Found",
            track_artist="Artist",
            frozen_track=False,
            force_meta=False,
            options={},
        )
        assert result["payload"].get("title") is None
        assert result.get("title_recommendation") is None


class TestBatchConfidenceFlag:
    def test_position_alias_with_verified_length_is_confirmed(self):
        from services.popularity import scan_stage_runner as runner
        from services.popularity.popularity_sources import track_identity_key

        key = track_identity_key("plain", 1, 3)
        prefetched = {
            key: {
                "recording_mbid": _mbid(3),
                "release_track_title": "Plain",
                "length_verified": True,
            },
        }

        class _Bulk:
            def lookup_recordings_by_mbid_bulk(self, mbids, *, album_name=None, **k):
                return {}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service",
            lambda: _Bulk(),
        )
        try:
            batch = runner._build_album_recording_batch(
                album="Album",
                track_dicts=[
                    {"artist": "Artist", "title": "Plain",
                     "disc_number": 1, "track_number": 3},
                ],
                prefetched_popularity=prefetched,
            )
        finally:
            monkeypatch.undo()

        assert batch[_batch_key("Artist", "Plain", 1, 3)]["_title_match_confirmed"] is True

    def test_title_keyed_fallback_is_not_confirmed(self):
        """A title-only identity (no track-number guarantee) must never be
        auto-applied — its release title goes to approval instead."""
        from services.popularity import scan_stage_runner as runner
        from services.popularity.popularity_sources import track_identity_key

        # The position alias is absent; only the title-keyed entry exists.
        prefetched = {
            "plain": {"recording_mbid": _mbid(1), "release_track_title": "Plain"},
        }

        class _Bulk:
            def lookup_recordings_by_mbid_bulk(self, mbids, *, album_name=None, **k):
                return {}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service",
            lambda: _Bulk(),
        )
        try:
            batch = runner._build_album_recording_batch(
                album="Album",
                track_dicts=[
                    {"artist": "Artist", "title": "Plain",
                     "disc_number": 1, "track_number": 3},
                ],
                prefetched_popularity=prefetched,
            )
        finally:
            monkeypatch.undo()

        entry = batch[_batch_key("Artist", "Plain", 1, 3)]
        assert entry["_title_match_confirmed"] is False
        assert entry["title"] == "Plain"  # release title still carried for review

    def test_position_without_verified_length_is_not_confirmed(self):
        from services.popularity import scan_stage_runner as runner
        from services.popularity.popularity_sources import track_identity_key

        key = track_identity_key("plain", 1, 3)
        prefetched = {
            key: {
                "recording_mbid": _mbid(3),
                "release_track_title": "Plain",
                # length_verified absent -> the guard never compared durations
            },
        }

        class _Bulk:
            def lookup_recordings_by_mbid_bulk(self, mbids, *, album_name=None, **k):
                return {}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_service",
            lambda: _Bulk(),
        )
        try:
            batch = runner._build_album_recording_batch(
                album="Album",
                track_dicts=[
                    {"artist": "Artist", "title": "Plain",
                     "disc_number": 1, "track_number": 3},
                ],
                prefetched_popularity=prefetched,
            )
        finally:
            monkeypatch.undo()

        assert batch[_batch_key("Artist", "Plain", 1, 3)]["_title_match_confirmed"] is False


class TestStashTrackTitleRecommendation:
    def _db(self, tmp_path, monkeypatch):
        import os
        from contextlib import contextmanager

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(f"sqlite:///{os.path.join(tmp_path, 'rec.db')}")
        with engine.begin() as conn:
            conn.execute(pytest.importorskip("sqlalchemy").text("""
                CREATE TABLE tracks (
                    id TEXT PRIMARY KEY,
                    artist TEXT,
                    album TEXT,
                    title TEXT,
                    pending_mb_updates TEXT,
                    mb_ignored_fields TEXT,
                    updated_at TEXT
                )
            """))
            conn.execute(pytest.importorskip("sqlalchemy").text(
                "INSERT INTO tracks (id, artist, album, title) "
                "VALUES ('t1', 'Artist', 'Album', 'Local')"
            ))
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        @contextmanager
        def _session(*a, **k):
            s = Session()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        from services.metadata import pending_update_service as pus
        monkeypatch.setattr(pus, "db_session", _session)

        def _read():
            with _session() as s:
                row = s.execute(
                    pytest.importorskip("sqlalchemy").text(
                        "SELECT pending_mb_updates FROM tracks WHERE id='t1'"
                    )
                ).fetchone()
            return json.loads(row[0]) if row and row[0] else None

        return _read

    def test_stash_writes_a_title_change(self, tmp_path, monkeypatch):
        from services.metadata.pending_update_service import (
            stash_track_title_recommendation,
        )

        read = self._db(tmp_path, monkeypatch)
        ok = stash_track_title_recommendation(
            track_id="t1", artist="Artist", album="Album",
            current_title="Local", proposed_title="Proposed (Version)",
        )
        assert ok is True

        env = read()
        assert env["changes"] == [{
            "field": "title", "label": "Title",
            "current": "Local", "proposed": "Proposed (Version)",
        }]

    def test_stash_merges_into_existing_envelope(self, tmp_path, monkeypatch):
        from services.metadata.pending_update_service import (
            stash_track_title_recommendation,
        )
        import os

        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        # Seed an existing envelope with an unrelated change.
        engine = create_engine(f"sqlite:///{os.path.join(tmp_path, 'rec2.db')}")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE tracks (
                    id TEXT PRIMARY KEY, pending_mb_updates TEXT, mb_ignored_fields TEXT,
                    updated_at TEXT
                )
            """))
            conn.execute(text(
                "INSERT INTO tracks (id, pending_mb_updates, mb_ignored_fields) VALUES "
                "('t1', :env, NULL)"
            ), {"env": json.dumps({
                "version": 1, "changes": [
                    {"field": "mbid", "label": "MusicBrainz Recording ID",
                     "current": "", "proposed": "abc"}
                ],
            })})

        from services.metadata import pending_update_service as pus
        from contextlib import contextmanager

        Session = sessionmaker(bind=engine, expire_on_commit=False)

        @contextmanager
        def _session(*a, **k):
            s = Session()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        monkeypatch.setattr(pus, "db_session", _session)
        ok = stash_track_title_recommendation(
            track_id="t1", artist="Artist", album="Album",
            current_title="Local", proposed_title="New Title",
        )
        assert ok is True

        env = None

        def _read_row():
            with _session() as s:
                row = s.execute(text(
                    "SELECT pending_mb_updates FROM tracks WHERE id='t1'"
                )).fetchone()
            return row

        row = _read_row()
        env = json.loads(row[0])
        fields = [c["field"] for c in env["changes"]]
        assert "title" in fields and "mbid" in fields, (
            "the title change must MERGE with an existing envelope, not wipe it"
        )

    def test_stash_respects_ignored_fields(self, tmp_path, monkeypatch):
        from services.metadata.pending_update_service import (
            stash_track_title_recommendation,
        )
        import os

        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(f"sqlite:///{os.path.join(tmp_path, 'rec3.db')}")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE tracks (
                    id TEXT PRIMARY KEY, pending_mb_updates TEXT, mb_ignored_fields TEXT,
                    updated_at TEXT
                )
            """))
            conn.execute(text(
                "INSERT INTO tracks (id, pending_mb_updates, mb_ignored_fields) "
                "VALUES ('t1', NULL, '[\"title\"]')"
            ))

        from services.metadata import pending_update_service as pus
        from contextlib import contextmanager

        Session = sessionmaker(bind=engine, expire_on_commit=False)

        @contextmanager
        def _session(*a, **k):
            s = Session()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        monkeypatch.setattr(pus, "db_session", _session)
        ok = stash_track_title_recommendation(
            track_id="t1", artist="Artist", album="Album",
            current_title="Local", proposed_title="New Title",
        )
        assert ok is False
        with _session() as s:
            row = s.execute(text(
                "SELECT pending_mb_updates FROM tracks WHERE id='t1'"
            )).fetchone()
        assert row[0] is None, "a permanently-ignored field must never be stashed"