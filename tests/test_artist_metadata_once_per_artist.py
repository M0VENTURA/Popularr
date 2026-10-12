"""Artist-level metadata (country/bio/image/line-up) is refreshed once per
artist pass, not checked on every album.

Reported (album-level caching review): release-country, artist image, artist
bio and similar artists are already cached but still "checked every album"
during a scan.  The similar-artists half was already moved to the once-per-
artist prefetch (2026-10-11, ``9bb2ae5a``); this closes the same gap for the
artist metadata block:

- ``album_stage._fetch_artist_metadata`` stays the NETWORK-refresh function
  (biography / MusicBrainz country / AudioDB image / MusicBrainz members),
  now invoked ONCE PER ARTIST by the scan runner's prefetch thread;
- the per-album enrichment path reads the persisted values with the new
  DB-only ``_read_artist_metadata`` — no network, and no repeated "already
  cached" skip-checks for every album of the artist.
"""

from __future__ import annotations

import datetime
import inspect
import io
import json
import tokenize

import pytest


class _FakeResult:
    def __init__(self, rows):
        if isinstance(rows, dict):
            rows = [rows]
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return _FakeResult(self._results.pop(0) if self._results else [])


class _FakeCM:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *args):
        return False


def _patch_session(monkeypatch, module, session):
    monkeypatch.setattr(module, "db_session", lambda: _FakeCM(session))
    return session


def _strip_comments(source: str) -> str:
    """``source`` with every comment removed (structure otherwise kept).

    Used by the wiring guards: prose that NAMES a retired call (docstrings,
    explanatory comments) is documentation, not a call site — a "this call is
    gone" assertion must not pass on prose, and a "this call is present"
    assertion must not be satisfied by a comment either.
    """
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    except (tokenize.TokenError, IndentationError):
        return source
    return "".join(tok.string for tok in tokens if tok.type != tokenize.COMMENT)


# ---------------------------------------------------------------------------
# 1. The per-album enrichment path is DB-read-only
# ---------------------------------------------------------------------------

class TestAlbumPathIsReadOnly:

    def _run_full_enrichment_source(self) -> str:
        import services.popularity.stages.album_stage as album_stage

        source = inspect.getsource(album_stage)
        start = source.index("def _run_full_enrichment")
        return _strip_comments(source[start:])

    def test_the_album_path_reads_the_cache(self):
        assert "_read_artist_metadata(artist)" in self._run_full_enrichment_source()

    def test_the_album_path_no_longer_runs_the_refresh(self):
        # The network refresh must live in the prefetch thread, NOT here. The
        # ``(`` guards against the function being merely imported/mentioned.
        assert "_fetch_artist_metadata(" not in self._run_full_enrichment_source()

    def test_the_country_backfill_still_reads_the_read_value(self):
        # The release-country backfill consumes the DB-read result, so the
        # artist-level cache still reaches the per-album write.
        src = self._run_full_enrichment_source()
        assert "metadata[\"country\"]" in src
        assert "releasecountry" in src


# ---------------------------------------------------------------------------
# 2. The once-per-artist prefetch now owns the refresh
# ---------------------------------------------------------------------------

class TestPrefetchRefreshOncePerArtist:

    def _prefetch_window(self) -> str:
        import services.popularity.scan_stage_runner as runner

        source = inspect.getsource(runner)
        start = source.index("def _prefetch_artist_work")
        # Raw window: every assertion here is a POSITIVE presence check whose
        # exact call form appears only in CODE (the explanatory comments name
        # the functions without the ``(artist)`` call), and tokenizing a slice
        # taken from inside a nested block trips an IndentationError.
        return source[start:]

    def test_the_prefetch_refreshes_artist_metadata(self):
        assert "_fetch_artist_metadata(artist)" in self._prefetch_window()

    def test_similar_artists_refresh_is_still_there(self):
        assert "refresh_similar_artists(artist, options)" in self._prefetch_window()

    def test_both_refreshes_sit_inside_the_not_singles_gate(self):
        window = self._prefetch_window()
        gate = window.index("if not _singles_pass:")
        assert gate < window.index("_fetch_artist_metadata(artist)")
        assert gate < window.index("refresh_similar_artists(artist, options)")


# ---------------------------------------------------------------------------
# 3. _read_artist_metadata — DB-only behaviour
# ---------------------------------------------------------------------------

class TestReadArtistMetadataBehaviour:

    def test_cached_values_are_returned(self, monkeypatch):
        from services.popularity.stages import album_stage

        row = {
            "country": "DE",
            "bio": "A band.",
            "image_url": "https://cdn.example/a.jpg",
            "members": json.dumps([{"name": "X"}]),
            "members_last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _patch_session(monkeypatch, album_stage, _FakeSession([row]))
        result = album_stage._read_artist_metadata("Band")
        assert result["country"] == "DE"
        assert result["bio"] == "A band."
        assert result["image_url"] == "https://cdn.example/a.jpg"
        assert result["members"] == row["members"]

    def test_no_row_returns_empties(self, monkeypatch):
        from services.popularity.stages import album_stage

        _patch_session(monkeypatch, album_stage, _FakeSession([None]))
        result = album_stage._read_artist_metadata("Unscanned Band")
        assert result == {
            "country": None, "bio": None, "image_url": None,
            "members": None, "members_last_updated": None,
        }

    def test_a_failing_session_returns_empties(self, monkeypatch):
        from services.popularity.stages import album_stage

        def _boom():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(album_stage, "db_session", _boom)
        result = album_stage._read_artist_metadata("Band")
        assert result["country"] is None
        assert result["bio"] is None

    def test_a_fresh_row_cost_no_network(self, monkeypatch):
        """The read is pure SQL — with a full row, NO network helper may run."""
        from services.popularity.stages import album_stage

        row = {
            "country": "US",
            "bio": "Bio.",
            "image_url": "https://cdn.example/a.jpg",
            "members": json.dumps([{"name": "Y"}]),
            "members_last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _patch_session(monkeypatch, album_stage, _FakeSession([row]))
        monkeypatch.setattr(
            album_stage,
            "_call_with_heartbeat",
            lambda *a, **k: pytest.fail("the DB-only read must not call the network"),
        )
        result = album_stage._read_artist_metadata("Band")
        assert result["country"] == "US"


# ---------------------------------------------------------------------------
# 4. _fetch_artist_metadata is still the refresh function
# ---------------------------------------------------------------------------

class TestFetchArtistMetadataStillRefreshes:

    def _fresh_row(self):
        return {
            "country": "GB",
            "bio": "Bio.",
            "image_url": "https://cdn.example/b.jpg",
            "members": json.dumps([{"name": "Z"}]),
            "members_last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    def test_fully_cached_artist_is_a_true_noop(self, monkeypatch):
        """A complete fresh cache row means ZERO network heartbeats."""
        from services.popularity.stages import album_stage

        _patch_session(monkeypatch, album_stage, _FakeSession([self._fresh_row()]))
        monkeypatch.setattr(
            album_stage,
            "_call_with_heartbeat",
            lambda *a, **k: pytest.fail("a cached artist must not trigger lookups"),
        )
        result = album_stage._fetch_artist_metadata("Band")
        assert result["country"] == "GB"

    def test_an_empty_row_still_runs_the_lookups(self, monkeypatch):
        """Control: with NOTHING cached, the refresh performs the network work."""
        from services.popularity.stages import album_stage

        _patch_session(monkeypatch, album_stage, _FakeSession([None]))
        calls: list[str] = []

        def _fake_heartbeat(section, fn, *args, **kwargs):
            calls.append(section)

        monkeypatch.setattr(album_stage, "_call_with_heartbeat", _fake_heartbeat)
        # Persist writes go through db_session too — never fail them.
        monkeypatch.setattr(album_stage, "_lookup_artist_members", lambda *a, **k: [])
        result = album_stage._fetch_artist_metadata("Band")
        assert any(s.startswith("artist_metadata.") for s in calls), calls
        assert result["country"] is None  # nothing resolved by the fakes