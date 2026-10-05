"""Scans must fill in the Discogs album id, and the Edit tab needs a Last.fm link.

Reported
--------
> Discogs album ID doesn't seem to be populating correctly during scans, should
> it also have a last.fm field for each album that links to the last.fm release
> on the album page under the edit tab?

**Discogs.** Nothing in any scan path ever wrote ``tracks.discogs_album_id`` —
grepping every service turns up exactly two writers, both on the album page's
manual flow (``apply_discogs_id_to_album`` and ``update_album_ids``). The
column existed, the form existed, and a scan could only ever leave it blank.

Resolution is *only when empty* (confirmed with the reporter): the artist's
already-cached Discogs release list first — no API call — then one search, and
only an exact title match counts. A wrong release id is worse than a blank one,
because it is written to every track of the album.

**Last.fm.** Last.fm has no album id: a release page is addressed by
``/music/<artist>/<album>``. So there is nothing to resolve or store — the link
is derived, works for every album immediately, and needs no column. The artist
page already has the equivalent button.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import pytest

from services.popularity.stages import album_stage

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_TEMPLATE = REPO_ROOT / "templates" / "pages" / "album_detail.html"
REBUILT_TEMPLATE = REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"

TEMPLATES = [LIVE_TEMPLATE, REBUILT_TEMPLATE]


# ===========================================================================
# 1. Resolution
# ===========================================================================
def _patch_sources(monkeypatch, *, cache_rows, search_payload=None):
    """Install both Discogs sources and record whether the search was used."""
    import services.enrichment.discogs_service as discogs_service
    import services.popularity.release_cache_service as release_cache

    calls = {"search": 0}

    def _cache(artist, source="discogs"):
        return cache_rows

    def _search(artist, album):
        calls["search"] += 1
        return search_payload or {"success": True, "results": []}

    monkeypatch.setattr(release_cache, "get_cached_artist_release_rows", _cache)
    monkeypatch.setattr(discogs_service, "lookup_discogs_album", _search)
    return calls


class TestResolutionPrefersTheCache:
    def test_a_cached_title_match_wins_without_a_search(self, monkeypatch):
        calls = _patch_sources(
            monkeypatch,
            cache_rows=[{"title": "Bat Out Of Hell II", "release_id": "12345"}],
        )
        assert (
            album_stage._resolve_discogs_album_id("Meat Loaf", "Bat Out Of Hell II")
            == "12345"
        )
        assert calls["search"] == 0, "the cached release list must not cost an API call"

    def test_the_title_is_normalised_on_both_sides(self, monkeypatch):
        """Case, punctuation and the release-group separator must not defeat it."""
        _patch_sources(
            monkeypatch,
            cache_rows=[{"title": "Bat Out Of Hell II: Back into Hell", "release_id": "9"}],
        )
        assert (
            album_stage._resolve_discogs_album_id(
                "Meat Loaf", "bat out of hell ii: back into hell"
            )
            == "9"
        )

    def test_a_stale_cache_falls_back_to_one_search(self, monkeypatch):
        calls = _patch_sources(
            monkeypatch,
            cache_rows=None,  # None == absent/stale, [] == fresh but empty
            search_payload={
                "success": True,
                "results": [{"id": "777", "title": "Bat Out Of Hell II"}],
            },
        )
        assert (
            album_stage._resolve_discogs_album_id("Meat Loaf", "Bat Out Of Hell II")
            == "777"
        )
        assert calls["search"] == 1

    def test_no_exact_title_match_returns_nothing(self, monkeypatch):
        """A wrong release id is worse than a blank one."""
        _patch_sources(
            monkeypatch,
            cache_rows=[{"title": "Completely Different Record", "release_id": "1"}],
            search_payload={
                "success": True,
                "results": [{"id": "2", "title": "Also Not It"}],
            },
        )
        assert (
            album_stage._resolve_discogs_album_id("Meat Loaf", "Bat Out Of Hell II")
            is None
        )

    def test_a_failed_search_is_not_treated_as_a_match(self, monkeypatch):
        _patch_sources(
            monkeypatch,
            cache_rows=None,
            search_payload={"success": False, "error": "rate limited"},
        )
        assert (
            album_stage._resolve_discogs_album_id("Meat Loaf", "Bat Out Of Hell II")
            is None
        )

    def test_a_row_without_a_release_id_is_skipped(self, monkeypatch):
        calls = _patch_sources(
            monkeypatch,
            cache_rows=[{"title": "Bat Out Of Hell II", "release_id": ""}],
        )
        assert (
            album_stage._resolve_discogs_album_id("Meat Loaf", "Bat Out Of Hell II")
            is None
        )
        assert calls["search"] == 1, "an id-less row must not mask the search"


# ===========================================================================
# 2. Persistence — only where empty, and never without a token
# ===========================================================================
class TestTheAlbumIdIsFilledOnlyWhereMissing:
    def test_no_token_means_no_database_access_at_all(self, monkeypatch):
        import db.repositories.metadata as metadata_repo

        def _boom(*args, **kwargs):
            raise AssertionError("must not touch the database without a Discogs token")

        monkeypatch.setattr(metadata_repo, "album_missing_discogs_id", _boom)
        monkeypatch.setattr(metadata_repo, "fill_album_discogs_id", _boom)

        # Must not raise — a missing token is the normal, configured-off case.
        album_stage._record_discogs_album_id("Meat Loaf", "Bat Out Of Hell II", None)

    def test_an_album_already_filled_is_never_re_resolved(self, monkeypatch):
        import db.repositories.metadata as metadata_repo

        monkeypatch.setattr(
            metadata_repo, "album_missing_discogs_id", lambda **kw: False
        )

        def _boom(*args, **kwargs):
            raise AssertionError("a complete album must not spend a Discogs search")

        monkeypatch.setattr(metadata_repo, "fill_album_discogs_id", _boom)
        monkeypatch.setattr(
            album_stage, "_resolve_discogs_album_id", lambda *a, **k: _boom()
        )

        album_stage._record_discogs_album_id("Meat Loaf", "Bat Out Of Hell II", "tok")

    def test_a_missing_album_is_resolved_and_written(self, monkeypatch):
        import db.repositories.metadata as metadata_repo

        written = {}
        monkeypatch.setattr(
            metadata_repo, "album_missing_discogs_id", lambda **kw: True
        )
        monkeypatch.setattr(
            metadata_repo,
            "fill_album_discogs_id",
            lambda **kw: written.update(kw) or 1,
        )
        monkeypatch.setattr(
            album_stage, "_resolve_discogs_album_id", lambda *a, **k: "424242"
        )

        album_stage._record_discogs_album_id("Meat Loaf", "Bat Out Of Hell II", "tok")

        assert written == {
            "artist": "Meat Loaf",
            "album": "Bat Out Of Hell II",
            "discogs_id": "424242",
        }

    def test_a_resolution_that_finds_nothing_writes_nothing(self, monkeypatch):
        import db.repositories.metadata as metadata_repo

        monkeypatch.setattr(
            metadata_repo, "album_missing_discogs_id", lambda **kw: True
        )
        monkeypatch.setattr(
            album_stage, "_resolve_discogs_album_id", lambda *a, **k: None
        )

        def _boom(*args, **kwargs):
            raise AssertionError("nothing was resolved, so nothing may be written")

        monkeypatch.setattr(metadata_repo, "fill_album_discogs_id", _boom)

        album_stage._record_discogs_album_id("Meat Loaf", "Bat Out Of Hell II", "tok")

    def test_a_database_failure_does_not_escape(self, monkeypatch):
        """The caller is mid-enrichment; a Discogs hiccup must not abort it."""
        import db.repositories.metadata as metadata_repo

        def _explode(**kwargs):
            raise RuntimeError("database exploded")

        monkeypatch.setattr(metadata_repo, "album_missing_discogs_id", _explode)

        album_stage._record_discogs_album_id("Meat Loaf", "Bat Out Of Hell II", "tok")


# ===========================================================================
# 3. The SQL itself is guarded
# ===========================================================================
class _Result:
    def __init__(self, first, rowcount):
        self._first, self._rowcount = first, rowcount

    def first(self):
        return self._first

    @property
    def rowcount(self):
        return self._rowcount


class _RecordingSession:
    def __init__(self, first=None, rowcount=0):
        self._first, self._rowcount = first, rowcount
        self.sql = ""
        self.params = None

    def execute(self, statement, params=None):
        self.sql = str(statement)
        self.params = params
        return _Result(self._first, self._rowcount)


def _recorded_sql(monkeypatch, call, *, first=None, rowcount=0):
    import db.repositories.metadata as metadata_repo

    session = _RecordingSession(first=first, rowcount=rowcount)

    @contextmanager
    def _fake_db_session():
        yield session

    monkeypatch.setattr(metadata_repo, "db_session", _fake_db_session)
    call(metadata_repo)
    return session.sql


class TestThePersistenceSqlIsGuarded:
    def test_the_backfill_only_touches_empty_rows(self, monkeypatch):
        sql = _recorded_sql(
            monkeypatch,
            lambda repo: repo.fill_album_discogs_id(
                artist="A", album="B", discogs_id="123"
            ),
            rowcount=1,
        )
        assert "discogs_album_id = :did" in sql
        assert "COALESCE(discogs_album_id, '') = ''" in sql, (
            "the scan must never overwrite an id the user set by hand"
        )
        assert "UPDATE tracks" in sql

    def test_the_precheck_looks_for_an_empty_value(self, monkeypatch):
        sql = _recorded_sql(
            monkeypatch,
            lambda repo: repo.album_missing_discogs_id(artist="A", album="B"),
            first=(1,),
        )
        assert "COALESCE(discogs_album_id, '') = ''" in sql
        assert "LIMIT 1" in sql

    @pytest.mark.parametrize("call", [
        lambda repo: repo.fill_album_discogs_id(artist="", album="B", discogs_id="1"),
        lambda repo: repo.fill_album_discogs_id(artist="A", album="", discogs_id="1"),
        lambda repo: repo.fill_album_discogs_id(artist="A", album="B", discogs_id=""),
    ])
    def test_blank_arguments_write_nothing(self, monkeypatch, call):
        import db.repositories.metadata as metadata_repo

        session = _RecordingSession()

        @contextmanager
        def _fake_db_session():
            yield session

        monkeypatch.setattr(metadata_repo, "db_session", _fake_db_session)
        assert call(metadata_repo) == 0
        assert session.sql == "", "an incomplete call must not reach the database"


# ===========================================================================
# 4. The scan actually calls it
# ===========================================================================
def _function_source(name: str) -> str:
    """Just the named top-level function, not every function defined below it."""
    source = (REPO_ROOT / "services/popularity/stages/album_stage.py").read_text(
        encoding="utf-8"
    )
    start = source.index(f"def {name}(")
    rest = source[start:]
    nxt = re.search(r"\ndef ", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


class TestTheScanIsWiredToTheResolver:
    def test_enrichment_records_the_id(self):
        body = _function_source("enrich_album_extras")
        assert "_record_discogs_album_id(" in body, (
            "enrich_album_extras never asks for the Discogs id, so a scan still "
            "leaves discogs_album_id blank"
        )

    def test_the_enrichment_and_the_resolver_share_one_token(self):
        """Two config reads would double the 'token unavailable' log line."""
        body = _function_source("enrich_album_extras")
        assert "discogs_token = _get_discogs_token()" in body
        assert body.count("_get_discogs_token()") == 1
        assert "_record_discogs_album_id(artist, album, discogs_token)" in body


# ===========================================================================
# 5. The Last.fm release link
# ===========================================================================
class TestTheLastfmReleaseLink:
    @pytest.mark.parametrize("template", TEMPLATES)
    def test_the_edit_tab_links_the_release(self, template):
        html = template.read_text(encoding="utf-8")
        assert "https://www.last.fm/music/" in html, (
            f"{template.name} has no Last.fm release link on the Edit tab"
        )
        assert "{{ artist_name|urlencode }}/{{ album_name|urlencode }}" in html

    @pytest.mark.parametrize("template", TEMPLATES)
    def test_it_opens_in_a_new_tab_without_passing_the_referrer(self, template):
        html = template.read_text(encoding="utf-8")
        anchor = re.search(
            r'<a[^>]*href="https://www\.last\.fm/music/[^"]*"[^>]*>', html, re.S
        )
        assert anchor, "the Last.fm link anchor is missing"
        assert 'target="_blank"' in anchor.group(0)
        assert 'rel="noopener noreferrer"' in anchor.group(0)

    @pytest.mark.parametrize("template", TEMPLATES)
    def test_it_lives_beside_the_other_identifiers(self, template):
        html = template.read_text(encoding="utf-8")
        identifiers = html.find("Identifiers")
        discogs = html.find('id="album_discogs_id"')
        lastfm = html.find("last.fm/music/")
        assert -1 < identifiers < discogs < lastfm, (
            "the link belongs in the Identifiers & Linking block, after the "
            "Discogs Release ID"
        )

    @pytest.mark.parametrize("template", TEMPLATES)
    def test_no_new_column_was_introduced(self, template):
        """Last.fm has no album id — a stored field would always be derivable."""
        html = template.read_text(encoding="utf-8")
        assert 'name="album_lastfm' not in html
        assert "lastfm_album_id" not in html
