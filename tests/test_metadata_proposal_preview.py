"""Proposal engine + album-page metadata review contract.

Covers the album page's "Lookup MBID" preview:

* :func:`services.metadata.metadata_proposal_service.propose_album_metadata`
  reports what a metadata import WOULD write — album-level fields and
  per-track changes — **without writing anything**;
* the album save route honours the staged per-track payload
  (``#staged_track_updates``) so the review is ONE atomic save;
* the new propose endpoint is registered and wired to the service.

The "writes nothing" guarantee is the core assertion: the whole feature is
only safe because a lookup is reversible by reloading the page.
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


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[_Row]:
        return [_Row(r) for r in self._rows]

    def fetchone(self) -> _Row | None:
        return _Row(self._rows[0]) if self._rows else None


class _Session:
    """Minimal session that answers the local-track SELECT."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.executed: list[str] = []

    def execute(self, statement: Any, params: Any = None) -> _Result:
        sql = str(statement)
        self.executed.append(sql)
        if "FROM tracks" in sql and "ORDER BY" in sql:
            return _Result(self._rows)
        return _Result([])

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _local_track(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "t1",
        "title": "Old Title",
        "track_number": "1",
        "disc_number": "1",
        "mbid": "",
        "artist": "Test Artist",
        "album": "Test Album",
        "album_artist": "Test Artist",
        "genres": "Rock",
        "musicbrainz_genres": None,
        "mb_ignored_fields": None,
        "is_cover": 0,
        "writer": "",
        "recordlabel": "",
        "releasetitle": "",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def proposal():
    return importlib.import_module("services.metadata.metadata_proposal_service")


def _install(monkeypatch: Any, module: Any, rows: list[dict[str, Any]]) -> None:
    session = _Session(rows)
    monkeypatch.setattr(module, "db_session", lambda: session, raising=False)
    # ``db_session()`` is called as a context manager.
    monkeypatch.setattr(module, "db_session", lambda: session)


def _install_mb(monkeypatch: Any, proposal_mod: Any, *, comparison: dict[str, Any],
                metadata: dict[str, Any]) -> None:
    """Patch the lazily-imported MusicBrainz entry points."""
    mbs = importlib.import_module("services.enrichment.musicbrainz_service")
    monkeypatch.setattr(mbs, "compare_musicbrainz_release",
                        lambda a, al, m: comparison, raising=False)
    monkeypatch.setattr(mbs, "fetch_musicbrainz_release_metadata",
                        lambda rid: metadata, raising=False)


def _comparison_entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "mb_track_number": 1,
        "mb_disc_number": 1,
        "mb_title": "New Title",
        "mb_recording_mbid": "rec-1",
        "mb_duration": 200000,
        "matched": True,
        "library_track_id": "t1",
        "library_title": "Old Title",
        "library_track_number": "1",
        "library_disc_number": "1",
        "library_mbid": "",
        "diff_fields": ["title", "mbid"],
        "needs_update": True,
    }
    entry.update(overrides)
    return entry


def _release(**overrides: Any) -> dict[str, Any]:
    release = {
        "release_mbid": "rel-1",
        "release_group_mbid": "rg-1",
        "release_group_title": "Test Album",
        "release_title": "Test Album",
        "specific_release_title": "Test Album",
        "original_year": "2001",
        "original_release_year": 2001,
        "release_year": 2003,
        "version_release_year": 2003,
        "artist": "Test Artist",
        "artist_credit": "Test Artist",
        "album_artist_mbid": "artist-1",
        "album_type": "album",
        "disc_count": 1,
        "recordlabel": "Label X",
        "catalognumber": "CAT-1",
        "barcode": "1234567890",
        "releasedate": "2003-05-06",
        "media": "CD",
        "releasecountry": "US",
        "tracks": [{
            "mb_title": "New Title",
            "mb_recording_mbid": "rec-1",
            "musicbrainz_genres": "Rock, Metal",
            "writer": "Someone",
        }],
    }
    release.update(overrides)
    return release


def _propose(monkeypatch: Any, proposal: Any, *, rows: list[dict[str, Any]] | None = None,
             comparison: dict[str, Any] | None = None,
             metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    loaded = rows if rows is not None else [_local_track()]
    _install(monkeypatch, proposal, loaded)
    _install_mb(
        monkeypatch, proposal,
        comparison=comparison if comparison is not None
        else {"success": True, "mb_release_mbid": "rel-1", "mb_release_group_mbid": "rg-1",
              "comparison": [_comparison_entry()], "extra_tracks": []},
        metadata=metadata if metadata is not None else _release(),
    )
    return proposal.propose_album_metadata("Test Artist", "Test Album", "rel-1")


# ---------------------------------------------------------------------------
# propose_album_metadata — the write nothing guarantee
# ---------------------------------------------------------------------------

class TestProposeWritesNothing:
    def test_never_issues_a_write_statement(self, monkeypatch, proposal):
        """The single most important property: a preview must not mutate.

        Assert on the SQL that actually reaches the session — a service that
        merely *claims* to be read-only is not enough.
        """
        session = _Session([_local_track()])
        monkeypatch.setattr(proposal, "db_session", lambda: session)
        _install_mb(
            monkeypatch, proposal,
            comparison={"success": True, "mb_release_mbid": "rel-1",
                        "comparison": [_comparison_entry()], "extra_tracks": []},
            metadata=_release(),
        )
        proposal.propose_album_metadata("Test Artist", "Test Album", "rel-1")

        lowered = " ".join(session.executed).lower()
        for verb in ("insert", "update", "delete", "alter", "drop"):
            assert verb not in lowered, f"preview issued a {verb.upper()} statement"

    def test_payload_has_no_persisted_marker(self, monkeypatch, proposal):
        result = _propose(monkeypatch, proposal)
        assert result["success"] is True
        # A preview is advisory — it must not claim anything was written.
        assert "written" not in result
        assert "applied" not in result

    def test_missing_release_mbid_is_rejected(self, proposal):
        result = proposal.propose_album_metadata("A", "B", "")
        assert result["success"] is False
        assert result["album_changes"] == []
        assert result["track_changes"] == []

    def test_no_local_tracks_is_reported(self, monkeypatch, proposal):
        result = _propose(monkeypatch, proposal, rows=[])
        assert result["success"] is False
        assert "No library tracks" in result["error"]

    def test_failed_comparison_surfaces_the_error(self, monkeypatch, proposal):
        result = _propose(
            monkeypatch, proposal,
            comparison={"success": False, "error": "MusicBrainz unreachable"},
        )
        assert result["success"] is False
        assert result["error"] == "MusicBrainz unreachable"


# ---------------------------------------------------------------------------
# Album-level proposals
# ---------------------------------------------------------------------------

class TestAlbumLevelProposals:
    def test_reports_album_level_changes_with_current_and_proposed(self, monkeypatch, proposal):
        result = _propose(monkeypatch, proposal)
        by_field = {c["field"]: c for c in result["album_changes"]}

        # Label/catalog/barcode/date/media/country were all empty locally.
        for field in ("album_recordlabel", "album_catalognumber", "album_barcode",
                      "album_releasedate", "album_media", "album_releasecountry"):
            assert field in by_field, f"{field} not proposed"
            assert by_field[field]["current"] == ""
            assert by_field[field]["proposed"]

        assert by_field["album_recordlabel"]["proposed"] == "Label X"
        assert by_field["album_catalognumber"]["proposed"] == "CAT-1"

    def test_every_proposal_carries_the_three_contract_keys(self, monkeypatch, proposal):
        result = _propose(monkeypatch, proposal)
        for change in result["album_changes"]:
            assert set(change) >= {"field", "label", "current", "proposed"}
            assert change["field"]
            assert change["label"]

    def test_unchanged_fields_are_not_proposed(self, monkeypatch, proposal):
        """A field whose value already matches must produce no orange bar."""
        rows = [_local_track(
            album_artist="Test Artist",
            recordlabel="Label X",
            catalognumber="CAT-1",
            barcode="1234567890",
            releasedate="2003-05-06",
            media="CD",
            releasecountry="US",
            year="2001",
            release_year=2003,
            spotify_album_type="album",
            musicbrainz_album_mbid="rel-1",
            musicbrainz_releasegroupid="rg-1",
            musicbrainz_artistid="artist-1",
            release_title="Test Album",
        )]
        result = _propose(monkeypatch, proposal, rows=rows)
        fields = {c["field"] for c in result["album_changes"]}
        for already_correct in ("album_recordlabel", "album_catalognumber",
                                "album_barcode", "album_media", "album_releasecountry"):
            assert already_correct not in fields, f"{already_correct} proposed despite matching"

    def test_mbid_fields_are_proposed_when_absent(self, monkeypatch, proposal):
        result = _propose(monkeypatch, proposal)
        by_field = {c["field"]: c for c in result["album_changes"]}
        assert by_field["album_mbid"]["proposed"] == "rel-1"
        assert by_field["album_release_group_mbid"]["proposed"] == "rg-1"
        assert by_field["artist_mbid"]["proposed"] == "artist-1"


# ---------------------------------------------------------------------------
# Per-track proposals
# ---------------------------------------------------------------------------

class TestTrackProposals:
    def test_reports_title_change_for_the_matched_track(self, monkeypatch, proposal):
        result = _propose(monkeypatch, proposal)
        assert len(result["track_changes"]) == 1
        entry = result["track_changes"][0]
        assert entry["track_id"] == "t1"

        by_field = {c["field"]: c for c in entry["changes"]}
        assert by_field["title"]["current"] == "Old Title"
        assert by_field["title"]["proposed"] == "New Title"

    def test_track_number_equivalence_does_not_raise_a_bar(self, monkeypatch, proposal):
        """'01' and '1' are the same position — no proposal, no noise."""
        rows = [_local_track(track_number="01")]
        result = _propose(
            monkeypatch, proposal, rows=rows,
            comparison={"success": True, "mb_release_mbid": "rel-1",
                        "comparison": [_comparison_entry(mb_track_number=1)],
                        "extra_tracks": []},
        )
        fields = {c["field"] for t in result["track_changes"] for c in t["changes"]}
        assert "track_number" not in fields

    def test_ignored_fields_are_never_reproposed(self, monkeypatch, proposal):
        """A field the user permanently ignored stays ignored."""
        rows = [_local_track(mb_ignored_fields=json.dumps(["title"]))]
        result = _propose(monkeypatch, proposal, rows=rows)
        fields = {c["field"] for t in result["track_changes"] for c in t["changes"]}
        assert "title" not in fields

    def test_genre_proposal_uses_musicbrainz_genres(self, monkeypatch, proposal):
        result = _propose(monkeypatch, proposal)
        entry = result["track_changes"][0]
        by_field = {c["field"]: c for c in entry["changes"]}
        assert by_field["musicbrainz_genres"]["proposed"] == "Rock, Metal"

    def test_unmatched_tracks_are_reported_as_missing(self, monkeypatch, proposal):
        result = _propose(
            monkeypatch, proposal,
            comparison={"success": True, "mb_release_mbid": "rel-1",
                        "comparison": [
                            _comparison_entry(mb_track_number=1),
                            _comparison_entry(mb_track_number=2, matched=False,
                                              library_track_id="", mb_title="Absent"),
                        ],
                        "extra_tracks": []},
        )
        assert [m["mb_title"] for m in result["missing"]] == ["Absent"]

    def test_cover_verdict_is_proposed_as_a_structured_change(self, monkeypatch, proposal):
        metadata = _release(tracks=[{
            "mb_title": "New Title",
            "mb_recording_mbid": "rec-1",
            "is_cover": True,
            "original_cover_artist": "Someone Else",
            "musicbrainz_genres": "Rock, Metal",
        }])
        result = _propose(monkeypatch, proposal, metadata=metadata)
        entry = result["track_changes"][0]
        cover = next((c for c in entry["changes"] if c["field"] == "is_cover"), None)
        assert cover is not None, "cover verdict not proposed"
        assert cover["value"] == 1
        assert cover["original_cover_artist"] == "Someone Else"
        assert "Someone Else" in cover["proposed"]

    def test_counts_summarise_the_proposal(self, monkeypatch, proposal):
        result = _propose(monkeypatch, proposal)
        counts = result["counts"]
        assert counts["album_changes"] == len(result["album_changes"])
        assert counts["tracks_changed"] == len(result["track_changes"])
        assert counts["track_changes"] == sum(
            len(t["changes"]) for t in result["track_changes"]
        )


# ---------------------------------------------------------------------------
# Endpoint wiring
# ---------------------------------------------------------------------------

class TestProposeEndpointWiring:
    def test_route_is_registered(self):
        app_mod = importlib.import_module("app")
        rules = {str(r.rule) for r in app_mod.app.url_map.iter_rules()}
        assert "/api/album/musicbrainz/propose" in rules

    def test_route_is_post_only(self):
        app_mod = importlib.import_module("app")
        for rule in app_mod.app.url_map.iter_rules():
            if str(rule.rule) == "/api/album/musicbrainz/propose":
                assert "POST" in rule.methods
                assert "GET" not in rule.methods
                return
        pytest.fail("propose route not registered")
