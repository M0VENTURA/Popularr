"""A matched download folder must SHOW what it was matched to.

Reported:

> When on the downloads page, I select the manual match to an artist, it
> shows matched but doesn't list the details of the album it's matched with.
> **The End of Heartache (2004) [Album]** Matched ✓ /
> Killswitch Engage — The End of Heartache.
> This was manually matched with Various Artists - MTV2 Headbangers Ball V2.

The row's ``artist — album`` line comes from the FILES' tags, not from the
match. After a manual association the row flipped to ``Matched ✓`` but kept
showing whatever the folder itself said, so nothing on the page named the
release it had been matched to — and a wrong match (here: a Killswitch Engage
folder associated with an MTV2 Headbangers Ball compilation) was invisible.

The stored ``folder_matches`` row already carries release title, artist, year
and MBID; the payload already ships it as ``match``. Only the rendering and a
tracklist reader were missing:

* both monitor rows now render the matched release (``Matched to …``) with an
  expander for its tracklist;
* ``GET /api/downloads/folder/match-tracklist`` supplies that tracklist,
  cache-first through ``missing_releases.tracklist`` so repeated opens cost
  one indexed SELECT and only an uncached release reaches MusicBrainz;
* a fresh match opens ITS OWN tracklist immediately — the point of matching
  is seeing what you matched.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.downloads import download_folder_service as dfs  # noqa: E402

_RELEASE_MBID = "99999999-8888-7777-6666-555555555555"
LIVE_MONITOR = REPO_ROOT / "static" / "js" / "monitor.js"
TEST_SITE_MONITOR = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "monitor.js"


# ── Endpoint ───────────────────────────────────────────────────────────────

class TestMatchTracklistEndpoint:
    async def test_a_release_mbid_is_required(self, client):
        resp = await client.get("/api/downloads/folder/match-tracklist")
        assert resp.status_code == 400
        body = await resp.get_json()
        assert body["success"] is False

    async def test_it_returns_the_tracklist_for_the_release(self, client, monkeypatch):
        seen = {}

        def fake_fetch(release_id: str, artist: str = "") -> list[str]:
            seen["release_id"] = release_id
            seen["artist"] = artist
            return ["Open Hands, Closed Fists", "Take Me Alive"]

        monkeypatch.setattr("routes.downloads.fetch_missing_release_tracklist", fake_fetch)

        resp = await client.get(
            f"/api/downloads/folder/match-tracklist?release_mbid={_RELEASE_MBID}"
        )
        assert resp.status_code == 200
        body = await resp.get_json()

        assert body["success"] is True
        assert body["release_mbid"] == _RELEASE_MBID
        assert body["tracklist"] == ["Open Hands, Closed Fists", "Take Me Alive"]
        # The lookup must not be scoped to an artist: the folder's match has
        # no artist of its own, and any cached row for this release is valid.
        assert seen == {"release_id": _RELEASE_MBID, "artist": ""}

    async def test_a_lookup_failure_is_not_a_500(self, client, monkeypatch):
        """The page stays usable when MusicBrainz/cache is unreachable."""
        def boom(release_id: str, artist: str = "") -> list[str]:
            raise RuntimeError("musicbrainz is down")

        monkeypatch.setattr("routes.downloads.fetch_missing_release_tracklist", boom)

        resp = await client.get(
            f"/api/downloads/folder/match-tracklist?release_mbid={_RELEASE_MBID}"
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["success"] is True
        assert body["tracklist"] == []


# ── Payload: the match travels to the row ──────────────────────────────────

def _make_folder_matches_engine():
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE folder_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_path TEXT NOT NULL,
                release_mbid TEXT NOT NULL,
                release_title TEXT,
                artist TEXT,
                release_year INTEGER,
                status TEXT DEFAULT 'matched',
                created_at TEXT,
                updated_at TEXT,
                UNIQUE (folder_path)
            )
        """))
    return engine


@pytest.fixture()
def match_repo_env(monkeypatch):
    """Point the folder-match repository at a fresh SQLite DB."""
    engine = _make_folder_matches_engine()
    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    class _Session:
        def __init__(self, session):
            self._session = session

        def execute(self, *args, **kwargs):
            return self._session.execute(*args, **kwargs)

        def commit(self):
            self._session.commit()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, *exc):
            if exc_type is None:
                self._session.commit()
            self._session.close()
            return False

    session = sess_factory()
    monkeypatch.setattr(
        "db.repositories.folder_match_repository.db_session",
        lambda *a, **kw: _Session(session),
    )
    return engine


@pytest.fixture()
def downloads_env(tmp_path, monkeypatch):
    """One album folder under the downloads root."""
    root = tmp_path / "downloads"
    root.mkdir()
    album = root / "Killswitch Engage - The End of Heartache (2004) [Album]"
    album.mkdir()
    (album / "01 - Open Hands, Closed Fists.flac").write_bytes(b"x")

    monkeypatch.setattr(dfs, "resolve_downloads_dir", lambda *a, **kw: str(root))
    monkeypatch.setattr(dfs, "resolve_original_archive_dir", lambda: str(root / "Original"))
    monkeypatch.setattr(dfs, "_tracked_monitoring_folders", lambda: set())
    monkeypatch.setattr(dfs, "_imported_source_paths", lambda: set())
    return root


class TestThePayloadCarriesTheMatchedRelease:
    def test_the_row_ships_the_stored_match(self, downloads_env, match_repo_env):
        """The UI cannot show what the payload does not contain."""
        from db.repositories.folder_match_repository import upsert_folder_match

        album = os.path.normpath(
            os.path.join(str(downloads_env), "Killswitch Engage - The End of Heartache (2004) [Album]")
        )
        upsert_folder_match(
            folder_path=album,
            release_mbid=_RELEASE_MBID,
            release_title="MTV2 Headbangers Ball, Volume 2",
            artist="Various Artists",
            release_year=2004,
            status="matched",
        )

        result = dfs.get_unmatched_folders()
        assert result["success"] is True
        entry = next(f for f in result["folders"] if f["name"] == album)

        assert entry["status"] == "matched"
        match = entry["match"]
        assert match is not None, "the match row is what the details block renders"
        assert match["release_mbid"] == _RELEASE_MBID
        assert match["release_title"] == "MTV2 Headbangers Ball, Volume 2"
        assert match["artist"] == "Various Artists"
        assert match["release_year"] == 2004
        # …and the folder's OWN tags are still the (different) subtitle line,
        # which is why the match needed its own block.
        assert entry["album"] != match["release_title"]


# ── Rendering ──────────────────────────────────────────────────────────────

class TestBothMonitorPagesRenderTheMatchedRelease:
    @pytest.mark.parametrize("path", [LIVE_MONITOR, TEST_SITE_MONITOR])
    def test_the_row_has_a_matched_release_block(self, path):
        source = path.read_text(encoding="utf-8")

        assert 'badge bg-success me-1">Matched to<' in source, (
            "the row must NAME the release it was matched to"
        )
        assert 'class="mt-1 matched-tracklist"' in source, (
            "and offer the matched album's tracklist"
        )
        assert 'data-release-id="' in source

    @pytest.mark.parametrize("path", [LIVE_MONITOR, TEST_SITE_MONITOR])
    def test_the_block_is_appended_to_the_folder_row(self, path):
        source = path.read_text(encoding="utf-8")

        assert "matchedReleaseHtml(" in source, "helper missing"
        # The row builder must actually call it (not just define it).
        assert "+ matchedReleaseHtml(folder)" in source or "${matchedReleaseHtml(f)}" in source

    @pytest.mark.parametrize("path", [LIVE_MONITOR, TEST_SITE_MONITOR])
    def test_the_tracklist_comes_from_the_new_endpoint(self, path):
        source = path.read_text(encoding="utf-8")

        assert "/api/downloads/folder/match-tracklist?release_mbid=" in source
        assert "bindMatchedTracklistToggles" in source, (
            "the expander must be wired after every render"
        )

    @pytest.mark.parametrize("path", [LIVE_MONITOR, TEST_SITE_MONITOR])
    def test_a_folder_without_a_match_renders_no_block(self, path):
        """CONTROL — unassociated folders keep their plain row."""
        source = path.read_text(encoding="utf-8")

        assert "if (!releaseId) return '';" in source

    @pytest.mark.parametrize("path", [LIVE_MONITOR, TEST_SITE_MONITOR])
    def test_a_fresh_match_opens_its_own_tracklist(self, path):
        source = path.read_text(encoding="utf-8")

        assert "folder/associate" in source
        assert "details.open = true" in source, (
            "after matching, the tracklist must show without a second click — "
            "that is the detail the user matched for"
        )


class TestTheTracklistIsReadOnce:
    @pytest.mark.parametrize("path", [LIVE_MONITOR, TEST_SITE_MONITOR])
    def test_results_are_cached_per_release(self, path):
        source = path.read_text(encoding="utf-8")

        # Re-renders (confirm, delete, queue refresh) must not re-hit
        # MusicBrainz for a tracklist already fetched this session.
        assert "TracklistCache[releaseId] !== undefined" in source

    @pytest.mark.parametrize("path", [LIVE_MONITOR, TEST_SITE_MONITOR])
    def test_a_failed_load_can_be_retried(self, path):
        source = path.read_text(encoding="utf-8")

        assert "click to retry" in source, (
            "a transient failure must not be cached as 'no tracklist'"
        )
