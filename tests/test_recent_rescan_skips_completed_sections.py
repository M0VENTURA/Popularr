"""Re-scan of a recently-scanned artist skips completed sections.

Reported
--------
> This is the popularity scan running for a recently scanned artist, can you
> recommend a faster way to skip through the artist skipping sections that
> were already completed?

The pasted log showed a re-scan of "Various Artists" (182 albums) running the
FULL pipeline for every album: enrich → track phase (with ~3-8s MusicBrainz
recording searches per unresolvable track) → tag sync (~90-120s re-reading
every audio file). Not one "Skipping album" line appeared.

THREE root causes, all now fixed:

1. **A recording search that found NOTHING was forgotten.** The in-memory
   negative ``_mbid_cache`` lives in ONE process, but the queue worker and
   web worker are separate processes — so a ``candidate_count=0`` track
   re-searched on every scan. Worse, ``is_album_incomplete`` read "no
   recording_mbid" as "a re-run could fill it" and FORCED the whole album
   through the pipeline. Fix: track_stage persists ``recording_mbid_checked_at``
   via an ``on_miss`` callback fired ONLY when the search confirms a miss (not
   on a transient error), and the completeness gate treats a fresh stamp as
   terminal.

2. **A stored 0.0 score was read as "not scored".** The log's
   "1996 #011 - Quad City Dj's" row scored 0.0 (LF 0, LB 0 — genuinely
   unknown). ``skip_unchanged_albums`` required ``final_score > 0`` and
   ``is_album_incomplete`` required ``final_score`` non-zero, so that one row
   blocked the album's skip forever. Fix: "assessed" now means the column is
   non-NULL; a stored 0.0 IS an assessment.

3. **File tags were re-read on every scan.** ``sync_album_file_tags`` opened
   every audio file with mutagen per album per scan. Fix: the runner passes
   ``changed_track_ids`` (tracks whose file-tag-relevant DB values actually
   changed this pass — ``process_track``'s new ``tag_changed`` flag) and the
   sync skips tracks not in that set that were already reconciled
   (``file_tags_synced_at``). Migration ``022`` adds both timestamp columns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _route() -> str:
    return (REPO_ROOT / "routes" / "download_search_routes.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The durable search-miss stamp is wired through the MB service
# ---------------------------------------------------------------------------

class TestTheMissCallbackIsThreaded:

    def test_get_suggested_mbid_accepts_on_miss(self) -> None:
        src = (REPO_ROOT / "services" / "enrichment" / "musicbrainz_service.py").read_text(
            encoding="utf-8"
        )
        idx = src.index("def get_suggested_mbid(")
        # The method continues past 4000 chars to the _record_mbid call; slice
        # to the last get_suggested_mbid occurrence (the miss-record sits after
        # the search loop).
        end = src.find("    def lookup_recording_metadata(", idx)
        window = src[idx: end if end > idx else idx + 9000]
        assert "on_miss" in window, (
            "the search must expose a callback so the CALLER can persist the "
            "durable miss stamp at the exact moment the negative is confirmed"
        )
        # Fired in the miss branch AND the negative-cache-hit branch — never
        # on an exception (the except arm must not reference it).
        miss_idx = window.index("self._record_mbid(cache_key, \"\", 0.0)")
        after_miss = window[miss_idx: miss_idx + 260]
        assert "on_miss()" in after_miss
        except_idx = window.index("except Exception as exc:")
        except_window = window[except_idx: except_idx + 250]
        assert "on_miss" not in except_window, (
            "a transient error must NOT stamp a terminal miss, or the album "
            "skip would swallow a search that might succeed next time"
        )

    def test_lookup_recording_metadata_forwards_on_miss(self) -> None:
        src = (REPO_ROOT / "services" / "enrichment" / "musicbrainz_service.py").read_text(
            encoding="utf-8"
        )
        idx = src.index("def lookup_recording_metadata(")
        window = src[idx: idx + 3500]
        assert "on_miss = kwargs.get(\"on_miss\")" in window
        # Both internal get_suggested_mbid calls forward it.
        assert window.count("on_miss=on_miss") == 2

    def test_track_stage_stamps_via_the_callback(self) -> None:
        src = (REPO_ROOT / "services" / "popularity" / "stages" / "track_stage.py").read_text(
            encoding="utf-8"
        )
        # The metadata resolution path.
        idx = src.index("def _resolve_track_mb_metadata(")
        window = src[idx: idx + 5200]
        assert "on_miss=_stamp_recording_mbid_miss" in window
        assert 'payload["recording_mbid_checked_at"]' in window
        # The ListenBrainz-search path.
        lb_idx = src.index("_stamp_lb_mbid_miss")
        assert lb_idx > idx
        assert 'update_payload["recording_mbid_checked_at"]' in src[lb_idx: lb_idx + 700]
        assert "on_miss=_stamp_lb_mbid_miss" in src[lb_idx: lb_idx + 1500]


class TestTheMissStampHelper:
    @staticmethod
    def _helper():
        from services.popularity.scan_stage_runner import _recording_mbid_checked_recently

        return _recording_mbid_checked_recently

    def test_a_fresh_stamp_is_recent(self) -> None:
        fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert self._helper()({"recording_mbid_checked_at": fresh}) is True

    def test_an_old_stamp_is_stale(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        assert self._helper()({"recording_mbid_checked_at": old}) is False

    def test_missing_and_malformed_are_false(self) -> None:
        helper = self._helper()
        assert helper({}) is False
        assert helper({"recording_mbid_checked_at": "not-a-date"}) is False


# ---------------------------------------------------------------------------
# 2. Completeness: terminal misses + zero-score = assessed (behavioural)
# ---------------------------------------------------------------------------

class TestCompletedSectionsAreSkippable:
    def test_album_with_one_unresolvable_track_can_skip(self) -> None:
        from services.popularity.scan_stage_runner import is_album_incomplete

        stale_miss = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        tracks = [
            {
                "id": "ok", "title": "Resolved Track",
                "genres": "Rock", "lastfm_last_updated": "2026-10-13T00:00:00",
                "final_score": 55.0, "musicbrainz_albumtype": "album",
                "recording_mbid": "aaaa-aaaa",
            },
            {
                "id": "miss", "title": "Papa Won't Leave You, Henry",
                "genres": "Rock", "lastfm_last_updated": "2026-10-13T00:00:00",
                "final_score": 0.0, "musicbrainz_albumtype": "album",
                # MB searched this title and found nothing (candidate_count=0).
                "recording_mbid": "", "recording_mbid_checked_at": stale_miss,
            },
        ]
        incomplete, reason = is_album_incomplete(tracks)
        assert incomplete is False, (
            "a confirmed miss + a stored zero score are both terminal; the "
            f"album must be skippable — {reason}"
        )

    def test_the_completeness_relief_is_scoped(self) -> None:
        from services.popularity.scan_stage_runner import is_album_incomplete

        fresh_miss = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        # A fresh miss covers the MBID, but a NULL final_score is still a gap.
        tracks = [
            {
                "id": "t", "title": "T",
                "genres": "Rock", "lastfm_last_updated": "2026-10-13T00:00:00",
                "final_score": None, "musicbrainz_albumtype": "album",
                "recording_mbid": "", "recording_mbid_checked_at": fresh_miss,
            },
        ]
        incomplete, reason = is_album_incomplete(tracks)
        assert incomplete is True and "final score" in reason

    def test_the_skip_gate_treats_zero_as_scored(self) -> None:
        """skip_unchanged_albums used ``> 0``; a genuinely-zero track blocked
        "no changes detected" forever."""
        src = (REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py").read_text(
            encoding="utf-8"
        )
        idx = src.index("def _scored(t)")
        block = src[idx: idx + 260]
        assert 't.get("final_score") is not None' in block
        assert "> 0" not in block


# ---------------------------------------------------------------------------
# 3. Tag sync is scoped to changed tracks (behavioural)
# ---------------------------------------------------------------------------

class TestTagSyncScopesToChangedTracks:
    def _run(self, monkeypatch, tracks, changed_ids):
        from services.metadata import album_tag_sync_service as svc

        monkeypatch.setattr(
            "helpers.config_helpers.get_tagging_config",
            lambda: {"sync_album_tags_on_scan": True, "embed_lyrics": False},
        )
        monkeypatch.setattr(svc, "_load_fresh_tracks", lambda artist, album: tracks)
        # No MB release → MBID fields never written; keeps the fake simple.
        monkeypatch.setattr(svc, "_resolve_mb_release", lambda tracks: ("", {}, 0))
        reads: list[str] = []
        monkeypatch.setattr(svc, "_read_file_values", lambda p: reads.append(p) or {})
        written: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "services.metadata.tag_file_service.write_tags_to_file",
            lambda path, tags: written.append((path, tags)) or True,
        )
        monkeypatch.setattr(
            "services.metadata.tag_file_service.resolve_music_file_path",
            lambda p: str(p) if p else None,
        )
        monkeypatch.setattr("os.path.exists", lambda p: True)
        monkeypatch.setattr(
            "services.metadata.conflict_service.detect_and_record_conflicts",
            lambda **kw: {"conflicts_recorded": 0, "safe_updates": [], "blocked_fields": []},
        )

        def _noop_stamp(artist, album, ids):
            return None

        monkeypatch.setattr(svc, "_stamp_tag_sync", _noop_stamp)
        result = svc.sync_album_file_tags("Artist", "Album", changed_track_ids=changed_ids)
        return result, reads, written

    def test_only_changed_tracks_are_read(self, monkeypatch) -> None:
        tracks = [
            {
                "id": "changed", "title": "A", "artist": "Artist", "album": "Album",
                "album_artist": "Artist", "year": "2020", "track_number": "1",
                "disc_number": "1", "genres": "Rock", "file_path": "/tmp/a.mp3",
            },
            {
                "id": "untouched", "title": "B", "artist": "Artist", "album": "Album",
                "album_artist": "Artist", "year": "2020", "track_number": "2",
                "disc_number": "1", "genres": "Rock", "file_path": "/tmp/b.mp3",
                "file_tags_synced_at": "2026-10-13T00:00:00",
            },
        ]
        result, reads, _written = self._run(
            monkeypatch, tracks, changed_ids={"changed"}
        )
        assert reads == ["/tmp/a.mp3"], (
            "only the track the scan changed this pass may be re-read; the "
            f"untouched one was already reconciled — reads were {reads}"
        )
        assert result["tracks"] == 1

    def test_never_synced_tracks_are_always_read(self, monkeypatch) -> None:
        """A track with NO file_tags_synced_at stamp must be visited even if
        the scan didn't touch it — its tags were never reconciled."""
        tracks = [
            {
                "id": "new", "title": "A", "artist": "Artist", "album": "Album",
                "album_artist": "Artist", "year": "2020", "track_number": "1",
                "disc_number": "1", "genres": "Rock", "file_path": "/tmp/a.mp3",
            },
        ]
        result, reads, _written = self._run(monkeypatch, tracks, changed_ids=set())
        assert reads == ["/tmp/a.mp3"], (
            "never-synced tracks are read even with an empty changed set"
        )
        assert result["tracks"] == 1

    def test_all_changed_is_a_full_sync(self, monkeypatch) -> None:
        """CONTROL — delivering every id reproduces the old behaviour."""
        tracks = [
            {
                "id": "a", "title": "A", "artist": "Artist", "album": "Album",
                "album_artist": "Artist", "year": "2020", "track_number": "1",
                "disc_number": "1", "genres": "Rock", "file_path": "/tmp/a.mp3",
                "file_tags_synced_at": "2026-10-13T00:00:00",
            },
            {
                "id": "b", "title": "B", "artist": "Artist", "album": "Album",
                "album_artist": "Artist", "year": "2020", "track_number": "2",
                "disc_number": "1", "genres": "Rock", "file_path": "/tmp/b.mp3",
                "file_tags_synced_at": "2026-10-13T00:00:00",
            },
        ]
        result, reads, _written = self._run(
            monkeypatch, tracks, changed_ids={"a", "b"}
        )
        assert reads == ["/tmp/a.mp3", "/tmp/b.mp3"]
        assert result["tracks"] == 2

    def test_stamp_covers_only_read_files(self, monkeypatch) -> None:
        """The stamp is scoped to tracks whose FILE existed (was read)."""
        from services.metadata import album_tag_sync_service as svc

        stamped: list[list[str]] = []

        def _fake_stamp(artist, album, ids):
            stamped.append(list(ids))

        tracks = [
            {
                "id": "missing-file", "title": "A", "artist": "Artist",
                "album": "Album", "album_artist": "Artist", "year": "2020",
                "track_number": "1", "disc_number": "1", "genres": "Rock",
                "file_path": "/tmp/does-not-exist.mp3",
            },
            {
                "id": "has-file", "title": "B", "artist": "Artist",
                "album": "Album", "album_artist": "Artist", "year": "2020",
                "track_number": "2", "disc_number": "1", "genres": "Rock",
                "file_path": "/tmp/b.mp3",
            },
        ]
        monkeypatch.setattr(
            "helpers.config_helpers.get_tagging_config",
            lambda: {"sync_album_tags_on_scan": True, "embed_lyrics": False},
        )
        monkeypatch.setattr(svc, "_load_fresh_tracks", lambda artist, album: tracks)
        monkeypatch.setattr(svc, "_resolve_mb_release", lambda tracks: ("", {}, 0))
        monkeypatch.setattr(
            "services.metadata.tag_file_service.resolve_music_file_path",
            lambda p: str(p) if p else None,
        )
        # Only b.mp3 exists.
        monkeypatch.setattr("os.path.exists", lambda p: str(p).endswith("b.mp3"))
        monkeypatch.setattr(
            "services.metadata.conflict_service.detect_and_record_conflicts",
            lambda **kw: {"conflicts_recorded": 0, "safe_updates": [], "blocked_fields": []},
        )
        monkeypatch.setattr(svc, "_stamp_tag_sync", _fake_stamp)

        svc.sync_album_file_tags("Artist", "Album", changed_track_ids={"missing-file", "has-file"})
        assert stamped == [["has-file"]], (
            "a track with no file must not read as 'synced' — the missing "
            "file would never be filled when it later appears"
        )


# ---------------------------------------------------------------------------
# 4. The runner collects tag_changed ids and passes them down
# ---------------------------------------------------------------------------

class TestTheRunnerScopesTheSync:

    def test_tag_changed_flag_is_computed_per_track(self) -> None:
        src = (REPO_ROOT / "services" / "popularity" / "stages" / "track_stage.py").read_text(
            encoding="utf-8"
        )
        idx = src.index("_TAG_FILE_COLUMNS = (")
        block = src[idx: idx + 300]
        # The file-tag columns mirror what _db_tag_candidates reads.
        for col in ("title", "artist", "genres", "recording_mbid", "isrc"):
            assert col in block, f"{col} must be a tag-relevant column"
        assert '"tag_changed": _tag_changed' in src

    def test_the_runner_passes_changed_ids_to_the_sync(self) -> None:
        src = (REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py").read_text(
            encoding="utf-8"
        )
        idx = src.index("_album_changed_track_ids: set[str] = set(_corrected_title_ids)")
        block = src[idx: idx + 700]
        assert 'if not _res.get("tag_changed"):' in block
        assert "_album_changed_track_ids.add(_rid)" in block
        sync_call = src[src.index("changed_track_ids=_album_changed_track_ids") - 200:
                        src.index("changed_track_ids=_album_changed_track_ids") + 80]
        assert "changed_track_ids=_album_changed_track_ids" in sync_call


# ---------------------------------------------------------------------------
# 5. Migration 022 + registry + ORM
# ---------------------------------------------------------------------------

class TestMigration022Columns:

    def test_migration_adds_both_columns(self) -> None:
        src = (REPO_ROOT / "migrations" / "versions" /
               "022_tracks_recording_mbid_checked_and_file_tags_synced.py").read_text(
            encoding="utf-8"
        )
        for col in ("recording_mbid_checked_at", "file_tags_synced_at"):
            assert col in src
        assert "down_revision" in src
        assert '"021_album_lookup_skip"' in src, "must chain from the head"

    def test_the_registry_carries_both_columns(self) -> None:
        src = (REPO_ROOT / "db" / "schema.py").read_text(encoding="utf-8")
        assert '"recording_mbid_checked_at": "TIMESTAMP"' in src, (
            "the registry is what _ensure_columns reads to add columns to "
            "EXISTING databases on boot — a migration alone is not enough"
        )
        assert '"file_tags_synced_at": "TIMESTAMP"' in src

    def test_the_orm_declares_both_columns(self) -> None:
        src = (REPO_ROOT / "db" / "models.py").read_text(encoding="utf-8")
        assert "recording_mbid_checked_at: Mapped[datetime | None]" in src
        assert "file_tags_synced_at: Mapped[datetime | None]" in src

    def test_the_upsert_only_writes_real_columns(self) -> None:
        """The payload key survives only if the column exists — the runner's
        tag_changed machinery relies on the persist filtering by column."""
        src = (REPO_ROOT / "db" / "repositories" / "popularity_repository.py").read_text(
            encoding="utf-8"
        )
        assert "get_tracks_table_columns(session)" in src
        assert "if k in columns" in src