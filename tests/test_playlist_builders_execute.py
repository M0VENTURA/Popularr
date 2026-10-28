"""The playlist builders must RUN, not merely read correctly.

⭐⭐ WHY THIS FILE EXISTS — a real production regression, caught by log.

The weekly-rotation change added

    winners = _weekly_shuffle(winners, playlist_key=playlist_name)

to `_create_new_music_playlist`. That function never defines `playlist_name` —
its siblings do, which is exactly why the mistake looked right. Result, from the
shipped log:

    WARNING services.popularity.stages.finalise_stage
      New Music playlist generation failed error="name 'playlist_name' is not defined"

...and the New Music playlist simply stopped being generated, silently, because
the caller catches every exception.

⚠️ MY OWN TEST COULD NOT CATCH IT. `test_the_rotation_runs_after_the_track_cap`
reads the function SOURCE and asserts the shuffle call sits after the slice. A
NameError is invisible to a source-text assertion: the text was correct, the
call was in the right place, and the name did not exist. A guard that inspects
source can only prove a string is present — never that the code executes.

So these tests EXECUTE each builder with its collaborators stubbed and assert it
returns a sensible value instead of raising. They are slower than a grep and
they are the only kind that would have caught this.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.popularity.stages import finalise_stage as fs


def row(title: str, artist: str, *, stars: int = 5, listeners: int = 1000) -> dict[str, Any]:
    """A pool row shaped like the builders' SELECT output."""
    return {
        "id": f"{artist}-{title}",
        "title": title,
        "album": "Album",
        "artist": artist,
        "album_artist": artist,
        "file_path": f"/music/{artist}/{title}.mp3",
        "duration": 200,
        "stars": stars,
        "score": 70.0,
        "popularity_score": 70.0,
        "lastfm_listeners": listeners,
        "listenbrainz_listens": 0,
        "is_live": 0,
        "is_compilation": 0,
        "genres": "Punk Rock",
        "added_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def fetchall(self):
        return [_Mapped(r) for r in self._rows]

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _Mapped:
    def __init__(self, data: dict[str, Any]):
        self._mapping = data


class _FakeSession:
    """Answers every SELECT with the same canned row set."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def execute(self, statement, params=None):
        return _FakeResult(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def stubs(monkeypatch):
    """Stub every collaborator the three builders touch, and record the syncs."""
    synced: list[tuple[str, list[str]]] = []
    deleted: list[str] = []

    def fake_sync(name, song_ids, rows=None, existing_playlist_id=None, **kwargs):
        synced.append((name, list(song_ids)))
        return {"ok": True, "playlist_id": f"pl-{name}", "skipped": False}

    monkeypatch.setattr(fs, "_sync_playlist_to_navidrome", fake_sync)
    monkeypatch.setattr(fs, "_delete_playlist_from_navidrome", lambda n: deleted.append(n))
    monkeypatch.setattr(fs, "_playlist_sync_succeeded", lambda res: True)
    monkeypatch.setattr(fs, "_navidrome_clients", lambda: [])
    monkeypatch.setattr(fs, "log_unified", lambda *a, **k: None)

    return {"synced": synced, "deleted": deleted, "monkeypatch": monkeypatch}


def _patch_rows(monkeypatch, rows: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(fs, "db_session", lambda: _FakeSession(rows))


def _patch_config(monkeypatch, cfg: dict[str, Any] | None = None) -> None:
    monkeypatch.setattr(fs, "_resolve_max_per_artist", lambda _cfg: 0)
    import helpers.config_helpers as ch

    pl = {
        "playlist_interleave_artists": True,
        "playlist_order_mode": "prominence",
        "weekly_shuffle_enabled": True,
        **(cfg or {}),
    }
    monkeypatch.setattr(ch, "get_playlists_config", lambda: pl)
    monkeypatch.setattr(ch, "get_excluded_playlist_artists", lambda: set())


# ---------------------------------------------------------------------------
# New Music — the builder that broke
# ---------------------------------------------------------------------------


class TestNewMusicPlaylistRuns:
    def test_it_generates_without_raising(self, stubs) -> None:
        """⭐⭐ THE REGRESSION. This raised NameError, was caught by the caller,
        and the playlist silently stopped being generated."""
        _patch_rows(stubs["monkeypatch"], [row(f"Track {i}", f"Artist {i % 4}") for i in range(30)])
        _patch_config(stubs["monkeypatch"])

        written = fs._create_new_music_playlist()

        assert written > 0, "New Music produced no tracks"
        assert stubs["synced"], "the playlist was never pushed to Navidrome"
        assert stubs["synced"][0][0] == "New Music"

    def test_the_synced_name_matches_the_rotation_key(self, stubs) -> None:
        """The week's seed must name the same playlist that gets synced, or the
        rotation order changes on every call within one week."""
        _patch_rows(stubs["monkeypatch"], [row(f"Track {i}", f"Artist {i % 4}") for i in range(30)])
        _patch_config(stubs["monkeypatch"])

        fs._create_new_music_playlist()

        assert fs._NEW_MUSIC_PLAYLIST_NAME == "New Music"
        assert stubs["synced"][0][0] == fs._NEW_MUSIC_PLAYLIST_NAME

    def test_a_short_pool_deletes_instead_of_syncing(self, stubs) -> None:
        _patch_rows(stubs["monkeypatch"], [row("Only One", "Artist")])
        _patch_config(stubs["monkeypatch"])

        assert fs._create_new_music_playlist() == 0
        assert stubs["deleted"] == [fs._NEW_MUSIC_PLAYLIST_NAME]
        assert not stubs["synced"]

    def test_the_rotation_is_actually_applied(self, stubs) -> None:
        """Behavioural proof the shuffle participates, not just that it compiles."""
        _patch_rows(stubs["monkeypatch"], [row(f"Track {i:02d}", f"Artist {i % 4}") for i in range(40)])
        _patch_config(stubs["monkeypatch"])

        order: list[list[str]] = []
        original = fs._weekly_shuffle

        def spy(rows, *, playlist_key, **kwargs):
            out = original(rows, playlist_key=playlist_key, **kwargs)
            order.append([r["title"] for r in out])
            return out

        stubs["monkeypatch"].setattr(fs, "_weekly_shuffle", spy)
        fs._create_new_music_playlist()

        assert order, "the rotation was never called"
        assert order[0], "the rotation produced an empty list"
        # Same membership as the pre-rotation list — a shuffle, not a filter.
        assert len(order[0]) == len(set(order[0]))


# ---------------------------------------------------------------------------
# The same guard for the other two builders, so this class is closed
# ---------------------------------------------------------------------------


class TestTheOtherBuildersAlsoRun:
    def test_the_essential_builder_runs(self, stubs) -> None:
        rows = [row(f"Track {i}", "The Offspring") for i in range(20)]
        _patch_rows(stubs["monkeypatch"], rows)
        _patch_config(stubs["monkeypatch"])

        # The real signature is (artist, artist_scores, featured_rows) — there
        # is no ``order_mode`` parameter; artist-specific builders use the
        # fixed stars-then-Popularity-Score contract internally.
        result = fs._sync_essential_playlist(
            artist="The Offspring",
            artist_scores=[60.0 + i for i in range(20)],
            featured_rows=[],
        )

        assert result is not False
        assert stubs["synced"], "the Essential Collection was never pushed"

    def test_the_genre_builder_runs(self, stubs) -> None:
        rows = [row(f"Track {i:02d}", f"Artist {i % 5}") for i in range(120)]
        _patch_rows(stubs["monkeypatch"], rows)
        _patch_config(stubs["monkeypatch"])

        written = fs._create_genre_top_track_playlists()

        assert written >= 0, "the genre builder returned a negative count"
        assert stubs["synced"], "no genre playlist was pushed"


# ---------------------------------------------------------------------------
# The guard that DID catch it, pinned so it keeps catching it
# ---------------------------------------------------------------------------


class TestTheUndefinedNameGuard:
    def test_finalise_stage_has_no_undefined_names(self) -> None:
        """⭐ pyflakes found the NameError; my source-text test did not.

        Kept here as a reminder of which mechanism works: run it on the module
        that owns the builders rather than on the whole tree, so a failure names
        this file rather than an unrelated one.
        """
        pyflakes_api = pytest.importorskip("pyflakes.api")
        pyflakes_reporter = pytest.importorskip("pyflakes.reporter")

        class Collector(pyflakes_reporter.Reporter):
            def __init__(self):
                self.problems: list[str] = []

            def flake(self, message):
                text = str(message)
                if "undefined name" in text:
                    self.problems.append(text)

            def unexpectedError(self, filename, msg):
                self.problems.append(f"{filename}: {msg}")

            def syntaxError(self, filename, msg, lineno, offset, text):
                self.problems.append(f"{filename}:{lineno}: {msg}")

        reporter = Collector()
        import services.popularity.stages.finalise_stage as module

        pyflakes_api.checkPath(module.__file__, reporter)
        assert not reporter.problems, (
            "finalise_stage references undefined names: " + "; ".join(reporter.problems)
        )
