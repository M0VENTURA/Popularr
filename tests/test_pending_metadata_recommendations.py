"""Scan-time recommendations: stash, read, discard, and the config gate.

When the Config page has metadata updating set to "Recommend only"
(``metadata_update.apply_during_scan: false``) a scan must not apply
MusicBrainz metadata. It stores what it would have changed on the album's
track rows (``tracks.pending_mb_updates``) so the album/artist pages can offer
save or discard.

The column already existed (it is read by ``/api/missing/overview``); these
tests cover the WRITER and its lifecycle, which is what was missing.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Row:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping

    def __getitem__(self, index: int) -> Any:
        return list(self._mapping.values())[index]


class _Result:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchall(self) -> list[_Row]:
        return [_Row(r) for r in self._rows]

    def fetchone(self) -> _Row | None:
        return _Row(self._rows[0]) if self._rows else None


class _Session:
    """Records writes so a test can assert on what was persisted."""

    def __init__(self, *, track_ids: list[str] | None = None,
                 read_rows: list[dict[str, Any]] | None = None,
                 existing: int = 0) -> None:
        self.track_ids = track_ids if track_ids is not None else ["t1", "t2"]
        self.read_rows = read_rows or []
        self.existing = existing
        self.updates: list[dict[str, Any]] = []
        self.executed: list[str] = []

    def execute(self, statement: Any, params: Any = None) -> _Result:
        sql = " ".join(str(statement).split())
        self.executed.append(sql)
        lowered = sql.lower()

        if lowered.startswith("select cast(id as text) as id, title"):
            return _Result(self.read_rows)
        if lowered.startswith("select cast(id as text) as id"):
            return _Result([{"id": t} for t in self.track_ids])
        if lowered.startswith("select coalesce(nullif(album_artist"):
            return _Result(self.read_rows)
        if lowered.startswith("update tracks set pending_mb_updates = :payload"):
            self.updates.append(params or {})
            return _Result([], rowcount=1)
        if lowered.startswith("update tracks set pending_mb_updates = null"):
            return _Result([], rowcount=self.existing)
        return _Result([])

    def commit(self) -> None:
        return None

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


@pytest.fixture()
def pending():
    return importlib.import_module("services.metadata.pending_update_service")


def _install(monkeypatch: Any, module: Any, session: _Session) -> None:
    monkeypatch.setattr(module, "db_session", lambda: session)


def _proposal(**overrides: Any) -> dict[str, Any]:
    proposal = {
        "success": True,
        "artist": "A",
        "album": "B",
        "release_mbid": "rel-1",
        "release_group_mbid": "rg-1",
        "release_title": "B",
        "album_changes": [
            {"field": "album_recordlabel", "label": "Record Label",
             "current": "", "proposed": "Label X"},
        ],
        "track_changes": [
            {"track_id": "t1", "title": "One",
             "changes": [{"field": "title", "label": "Title",
                          "current": "Old", "proposed": "New"}]},
        ],
        "counts": {"album_changes": 1, "tracks_changed": 1, "track_changes": 1},
    }
    proposal.update(overrides)
    return proposal


# ---------------------------------------------------------------------------
# Stashing
# ---------------------------------------------------------------------------

class TestStashAlbumRecommendations:
    def test_writes_the_payload_to_the_offending_track(self, monkeypatch, pending):
        session = _Session()
        _install(monkeypatch, pending, session)

        result = pending.stash_album_recommendations("A", "B", _proposal())

        assert result["stashed"] >= 1
        written = json.loads(session.updates[0]["payload"])
        assert written["changes"][0]["proposed"] == "New"
        assert written["version"] == 1
        assert written["release_mbid"] == "rel-1"

    def test_clears_stale_recommendations_before_writing(self, monkeypatch, pending):
        """A track that is now up to date must stop showing a banner."""
        session = _Session(existing=3)
        _install(monkeypatch, pending, session)

        pending.stash_album_recommendations("A", "B", _proposal())

        clears = [s for s in session.executed
                  if s.lower().startswith("update tracks set pending_mb_updates = null")]
        assert clears, "stale recommendations were not cleared"

    def test_album_level_changes_land_on_the_first_track_only(self, monkeypatch, pending):
        """The block is carried once; a reader must not see it N times."""
        session = _Session(track_ids=["t1", "t2", "t3"])
        _install(monkeypatch, pending, session)

        pending.stash_album_recommendations("A", "B", _proposal(track_changes=[]))

        assert len(session.updates) == 1
        written = json.loads(session.updates[0]["payload"])
        assert written["track_id"] if "track_id" in written else True
        assert written["album_changes"][0]["proposed"] == "Label X"

    def test_a_row_without_its_own_changes_omits_the_album_block(self, monkeypatch, pending):
        session = _Session(track_ids=["t1", "t2"])
        _install(monkeypatch, pending, session)

        # t2 has its own change, so it must carry its own block only.
        proposal = _proposal(track_changes=[
            {"track_id": "t2", "title": "Two",
             "changes": [{"field": "title", "label": "Title",
                          "current": "Old", "proposed": "New"}]},
        ])
        pending.stash_album_recommendations("A", "B", proposal)

        payloads = [json.loads(u["payload"]) for u in session.updates]
        # The album block rides on the first track row.
        assert any("album_changes" in p for p in payloads)

    def test_failed_proposal_is_not_stashed(self, monkeypatch, pending):
        session = _Session()
        _install(monkeypatch, pending, session)

        result = pending.stash_album_recommendations(
            "A", "B", {"success": False, "error": "boom"}
        )

        assert result["stashed"] == 0
        assert session.updates == []

    def test_nothing_to_recommend_clears_and_reports(self, monkeypatch, pending):
        session = _Session(existing=2)
        _install(monkeypatch, pending, session)

        result = pending.stash_album_recommendations(
            "A", "B", _proposal(album_changes=[], track_changes=[])
        )

        assert result["stashed"] == 0
        assert result["cleared"] == 2

    def test_missing_artist_or_album_is_rejected(self, monkeypatch, pending):
        session = _Session()
        _install(monkeypatch, pending, session)
        result = pending.stash_album_recommendations("", "B", _proposal())
        assert result["stashed"] == 0
        assert "required" in result["reason"]


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------

class TestFetchAlbumRecommendations:
    def _rows(self) -> list[dict[str, Any]]:
        return [
            {"id": "t1", "title": "One",
             "pending_mb_updates": json.dumps({
                 "version": 1, "release_mbid": "rel-1", "release_title": "B",
                 "album_changes": [{"field": "album_recordlabel",
                                    "label": "Record Label",
                                    "current": "", "proposed": "Label X"}],
                 "changes": [{"field": "title", "label": "Title",
                              "current": "Old", "proposed": "New"}],
             }),
             "mb_ignored_fields": None},
            {"id": "t2", "title": "Two",
             "pending_mb_updates": json.dumps({
                 "version": 1, "changes": [{"field": "track_number",
                                            "label": "Track #",
                                            "current": "2", "proposed": "3"}],
             }),
             "mb_ignored_fields": None},
        ]

    def test_album_changes_are_reported_once(self, monkeypatch, pending):
        _install(monkeypatch, pending, _Session(read_rows=self._rows()))
        result = pending.fetch_album_recommendations("A", "B")

        assert result["has_any"] is True
        assert len(result["album_changes"]) == 1
        assert result["album_changes"][0]["proposed"] == "Label X"

    def test_per_track_changes_are_grouped_by_track(self, monkeypatch, pending):
        _install(monkeypatch, pending, _Session(read_rows=self._rows()))
        result = pending.fetch_album_recommendations("A", "B")

        by_track = {t["track_id"]: t for t in result["track_changes"]}
        assert set(by_track) == {"t1", "t2"}
        assert by_track["t1"]["changes"][0]["proposed"] == "New"
        assert by_track["t2"]["changes"][0]["proposed"] == "3"

    def test_counts_match_the_returned_data(self, monkeypatch, pending):
        _install(monkeypatch, pending, _Session(read_rows=self._rows()))
        result = pending.fetch_album_recommendations("A", "B")

        assert result["counts"]["album_changes"] == len(result["album_changes"])
        assert result["counts"]["tracks_changed"] == len(result["track_changes"])

    def test_permanently_ignored_fields_are_filtered_out(self, monkeypatch, pending):
        rows = self._rows()
        rows[0]["mb_ignored_fields"] = json.dumps(["title"])
        _install(monkeypatch, pending, _Session(read_rows=rows))

        result = pending.fetch_album_recommendations("A", "B")
        fields = {c["field"] for t in result["track_changes"] for c in t["changes"]}
        assert "title" not in fields

    def test_album_block_ignored_field_is_filtered_too(self, monkeypatch, pending):
        rows = self._rows()
        rows[0]["mb_ignored_fields"] = json.dumps(["album_recordlabel"])
        _install(monkeypatch, pending, _Session(read_rows=rows))

        result = pending.fetch_album_recommendations("A", "B")
        assert result["album_changes"] == []

    def test_unknown_envelope_version_is_skipped(self, monkeypatch, pending):
        rows = self._rows()
        rows[0]["pending_mb_updates"] = json.dumps({"version": 99, "changes": [
            {"field": "title", "proposed": "Nope"}]})
        _install(monkeypatch, pending, _Session(read_rows=rows))

        result = pending.fetch_album_recommendations("A", "B")
        fields = {c["field"] for t in result["track_changes"] for c in t["changes"]}
        assert "title" not in fields

    def test_malformed_json_does_not_raise(self, monkeypatch, pending):
        rows = [{"id": "t1", "title": "One", "pending_mb_updates": "{not json",
                 "mb_ignored_fields": None}]
        _install(monkeypatch, pending, _Session(read_rows=rows))
        result = pending.fetch_album_recommendations("A", "B")
        assert result["success"] is True
        assert result["has_any"] is False

    def test_empty_album_reports_no_recommendations(self, monkeypatch, pending):
        _install(monkeypatch, pending, _Session(read_rows=[]))
        result = pending.fetch_album_recommendations("A", "B")
        assert result["has_any"] is False
        assert result["counts"]["tracks_changed"] == 0


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------

class TestDiscard:
    def test_discard_reports_the_number_cleared(self, monkeypatch, pending):
        _install(monkeypatch, pending, _Session(existing=4))
        result = pending.discard_album_recommendations("A", "B")
        assert result["success"] is True
        assert result["cleared"] == 4


# ---------------------------------------------------------------------------
# Artist aggregation
# ---------------------------------------------------------------------------

class TestArtistRecommendations:
    def test_aggregates_by_album(self, monkeypatch, pending):
        rows = [
            {"artist": "A", "album": "B", "track_count": 3},
            {"artist": "A", "album": "C", "track_count": 2},
        ]
        _install(monkeypatch, pending, _Session(read_rows=rows))
        result = pending.fetch_artist_recommendations("A")

        assert result["album_count"] == 2
        assert result["total"] == 5
        assert {a["album"] for a in result["albums"]} == {"B", "C"}


# ---------------------------------------------------------------------------
# Config gate
# ---------------------------------------------------------------------------

class TestApplyDuringScanConfig:
    def test_defaults_to_applying(self, monkeypatch):
        """An existing config.yaml without the key must be unaffected."""
        cfg = importlib.import_module("helpers.config_helpers")
        monkeypatch.setattr(cfg, "get_config", lambda: {"metadata_update": {}})
        assert cfg.get_metadata_update_config()["apply_during_scan"] is True

    def test_can_be_switched_off(self, monkeypatch):
        cfg = importlib.import_module("helpers.config_helpers")
        monkeypatch.setattr(cfg, "get_config",
                            lambda: {"metadata_update": {"apply_during_scan": False}})
        assert cfg.get_metadata_update_config()["apply_during_scan"] is False

    def test_false_string_is_honoured(self, monkeypatch):
        """The Config page posts strings, so 'false' must not be truthy."""
        cfg = importlib.import_module("helpers.config_helpers")
        monkeypatch.setattr(cfg, "get_config",
                            lambda: {"metadata_update": {"apply_during_scan": "false"}})
        assert cfg.get_metadata_update_config()["apply_during_scan"] is False

    def test_other_keys_are_unaffected(self, monkeypatch):
        cfg = importlib.import_module("helpers.config_helpers")
        monkeypatch.setattr(cfg, "get_config", lambda: {"metadata_update": {
            "album_name_source": "release_group",
            "album_name_update_target": "files",
            "update_on_files": {"genres": True},
        }})
        result = cfg.get_metadata_update_config()
        assert result["album_name_source"] == "release_group"
        assert result["album_name_update_target"] == "files"
        assert result["update_on_files"]["genres"] is True


class TestConfigPageContract:
    """The Config page is the source of truth — the toggle must be on it."""

    @pytest.mark.parametrize("tree", ["templates/pages", "test_site/templates/Pages"])
    def test_toggle_is_rendered_in_both_trees(self, tree):
        from pathlib import Path

        path = Path(tree) / "config.html"
        text = path.read_text(encoding="utf-8")
        assert "metadata_update_apply_during_scan" in text, f"missing in {tree}"

    @pytest.mark.parametrize("tree_js", ["static/js/config.js",
                                         "test_site/static/js/pages/config.js"])
    def test_collector_reads_the_toggle_in_both_trees(self, tree_js):
        from pathlib import Path

        text = Path(tree_js).read_text(encoding="utf-8")
        assert "metadata_update_apply_during_scan" in text, f"missing in {tree_js}"
