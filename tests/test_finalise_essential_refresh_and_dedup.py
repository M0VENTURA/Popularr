"""Two behaviours of the finalise stage that nothing pinned.

1. **The dedup winner.** ``compute_track_artist_scores`` merges the in-memory
   scan results with the artist's existing database history and its docstring
   promises it excludes "any (album, title) pair already covered by the
   in-memory scores so nothing is double-counted". So the **scan result wins**
   and the database copy is dropped — if that ever flipped, the same song
   would be counted twice and would drag the artist's whole distribution
   (and therefore every star rating on a compilation) with it.

2. **An empty scan still refreshes the collections.** ``finalise_scan`` is
   given ``results=[]`` whenever a scan found nothing to rate, and it refreshes
   the Essential collections anyway (unless the scan already did) — otherwise a
   library that scans clean would keep last season's playlists forever.
"""
from __future__ import annotations

from typing import Any

from services.popularity.stages import finalise_stage as fs

ARTIST = "Feuerschwanz"


def _row(title: str, album: str, score: float) -> dict[str, Any]:
    return {"title": title, "album": album, "final_score": score}


class _FakeDb:
    """Stands in for ``db_session`` and replays one canned query result."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.queries = 0

    def __call__(self) -> "_FakeDb":
        return self

    def __enter__(self) -> "_FakeDb":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, *_args: Any, **_kwargs: Any) -> "_FakeDb":
        self.queries += 1
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def _scan_result(title: str, album: str, score: float = 50.0) -> dict[str, Any]:
    return {
        "artist": ARTIST,
        "album": album,
        "title": title,
        "popularity_score": score,
    }


# ---------------------------------------------------------------------------
# 1. The dedup winner: scan beats database
# ---------------------------------------------------------------------------
class TestTheScanResultWinsTheDedup:
    def test_a_database_copy_of_a_scanned_track_contributes_nothing(
        self, monkeypatch
    ):
        """Same (album, title) in both places -> counted ONCE, via the scan."""
        both = _FakeDb([
            _row("Shared Song", "Album One", 90.0),
            _row("Unique Song", "Album Two", 80.0),
        ])
        monkeypatch.setattr(fs, "db_session", both)
        with_duplicate = fs.compute_track_artist_scores(
            ARTIST, [_scan_result("Shared Song", "Album One")]
        )

        unique_only = _FakeDb([_row("Unique Song", "Album Two", 80.0)])
        monkeypatch.setattr(fs, "db_session", unique_only)
        without_duplicate = fs.compute_track_artist_scores(
            ARTIST, [_scan_result("Shared Song", "Album One")]
        )

        assert with_duplicate == without_duplicate, (
            "the database's copy of an already-scanned (album, title) was "
            "counted a second time"
        )
        assert 50.0 in with_duplicate, "the scan's own score must still be there"

    def test_the_control_the_database_half_really_does_contribute(self, monkeypatch):
        """Non-vacuous: with no scan results BOTH rows must be counted.

        Without this, the test above would pass even if the database half had
        stopped working entirely.
        """
        db = _FakeDb([
            _row("Shared Song", "Album One", 90.0),
            _row("Unique Song", "Album Two", 80.0),
        ])
        monkeypatch.setattr(fs, "db_session", db)

        scores = fs.compute_track_artist_scores(ARTIST, [])

        assert db.queries == 1, "the catalogue query never ran"
        assert len(scores) == 2, f"expected both rows, got {scores}"

    def test_only_the_matching_artist_is_deduped(self, monkeypatch):
        """The key is scoped to THIS artist — another artist's row survives."""
        db = _FakeDb([
            _row("Shared Song", "Album One", 90.0),
            _row("Shared Song", "Album One", 70.0),
        ])
        monkeypatch.setattr(fs, "db_session", db)

        scores = fs.compute_track_artist_scores(
            ARTIST, [_scan_result("Shared Song", "Album One")]
        )

        # One scan score only; the query is still made with this artist's name.
        assert scores == [50.0]


# ---------------------------------------------------------------------------
# 2. An empty scan still refreshes the collections
# ---------------------------------------------------------------------------
class TestAnEmptyScanRefreshesTheCollections:
    @staticmethod
    def _instrument(monkeypatch) -> list[str]:
        calls: list[str] = []
        monkeypatch.setattr(
            fs, "_refresh_all_essential_collections", lambda: calls.append("refresh") or 1
        )
        monkeypatch.setattr(
            fs, "_run_global_playlist_rebuild", lambda options: calls.append("rebuild")
        )
        return calls

    def test_a_scan_with_no_results_still_refreshes(self, monkeypatch):
        calls = self._instrument(monkeypatch)

        fs.finalise_scan(results=[], options={"create_playlists": True})

        assert calls == ["refresh", "rebuild"], (
            "a library that scans clean must not keep last season's "
            "Essential collections"
        )

    def test_a_scan_that_already_refreshed_does_not_refresh_again(self, monkeypatch):
        calls = self._instrument(monkeypatch)

        fs.finalise_scan(
            results=[],
            options={
                "create_playlists": True,
                "_essential_playlists_done": ["Feuerschwanz", "Korn"],
            },
        )

        assert calls == ["rebuild"], "the per-artist pass already did the work"

    def test_playlists_disabled_means_no_refresh(self, monkeypatch):
        calls = self._instrument(monkeypatch)

        fs.finalise_scan(results=[], options={"create_playlists": False})

        assert calls == ["rebuild"], "the config gate must still be honoured"
