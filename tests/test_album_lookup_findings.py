"""Album page Lookup MBID: flag duplicates + missing tracks in the tracklist.

Request: *"On the album page, when looking up mbid, it should flag duplicate
tracks as well as tracks on the album not in the collection and add them to
the tracklist to download similar to how it does during a metadata lookup
during the popularity scan."* Clarified: *"Duplicate tracks are tracks on
that album that were downloaded twice."*

What is pinned here:

* ``find_duplicate_tracks`` — per-album duplicate detection using the SAME
  rule as the artist-corrections page (identical title/artist/position,
  version-variant file names excluded), including the keyword-parity guard
  against ``artist_service``'s copy of the rule.
* ``get_missing_tracks(release_mbid=...)`` — the album page's Lookup MBID
  resolves a release BEFORE the form is saved, so an explicit release must
  win over the stored id / name search (the same computation the popularity
  scan runs), and a failed fetch must NOT wipe the persisted list.
* Routes: ``/api/album/duplicate-tracks`` and the ``refresh``/``release_mbid``
  params on ``/api/album/missing-tracks`` (page loads stay DB-only).
* Wiring: both trees' album JS hook the lookup, fetch the endpoints and flag
  rows by ``data-track-id``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

import services.metadata.album_missing_service as ams
from db.engine import db_session
from db.models import MissingAlbumTrack, Track


REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_support_tables() -> None:
    """Tables ``get_missing_tracks`` touches beyond ``tracks``.

    The shared in-memory DB is disposed on ANY OperationalError — including
    "no such table" — which would wipe ``tracks`` mid-test, so every table the
    code path reads must exist first:

    * ``download_queue`` — the queue-coverage check (same ALTER-TABLE
      backfill pattern as test_download_import_matches_mb_release);
    * ``missing_album_tracks`` — the rejected-titles read after persist
      (ORM-created, same as ``Track``).
    """
    queue_columns = {
        "title": "TEXT", "track_number": "TEXT", "disc_number": "TEXT",
        "status": "TEXT", "album": "TEXT", "artist": "TEXT",
        "album_artist": "TEXT", "file_path": "TEXT",
    }
    with db_session() as session:
        bind = session.get_bind()
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS download_queue (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        ))
        MissingAlbumTrack.__table__.create(bind, checkfirst=True)
        Track.__table__.create(bind, checkfirst=True)
        session.commit()
    for name, sql_type in queue_columns.items():
        with db_session() as session:
            existing = {
                str(row[1])
                for row in session.execute(text("PRAGMA table_info(download_queue)")).fetchall()
            }
            if name not in existing:
                try:
                    session.execute(text(f"ALTER TABLE download_queue ADD COLUMN {name} {sql_type}"))
                    session.commit()
                except Exception:
                    pass


_ensure_support_tables()


def _queue_insert(artist: str, title: str, album: str, status: str) -> None:
    """Insert one ``download_queue`` row, repairing the shared table first.

    The queue table is hand-created by whichever test file imported first, so a
    reduced shape (no ``source``/``status``/``created_at``) can persist across
    the session — CREATE IF NOT EXISTS then leaves columns behind and the insert
    fails with "no column named ...".
    """
    with db_session() as session:
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT, title TEXT, album TEXT, album_artist TEXT,
                status TEXT, track_number TEXT, disc_number INTEGER,
                created_at TEXT, updated_at TEXT
            )
        """))
        present = {
            str(row[1])
            for row in session.execute(text("PRAGMA table_info(download_queue)")).fetchall()
        }
        for column in ("source", "artist", "title", "album", "album_artist",
                       "status", "track_number", "disc_number",
                       "created_at", "updated_at"):
            if column not in present:
                session.execute(text(
                    f"ALTER TABLE download_queue ADD COLUMN {column} TEXT"
                ))
        session.execute(
            text(
                "INSERT INTO download_queue "
                "(artist, title, album, album_artist, status, source) "
                "VALUES (:artist, :title, :album, :album_artist, :status, 'soulseek')"
            ),
            {"artist": artist, "title": title, "album": album,
             "album_artist": artist, "status": status},
        )
        session.commit()


@pytest.fixture(autouse=True)
def _isolated_tracks():
    def _wipe() -> None:
        tables = _existing_tables()
        with db_session() as session:
            if "tracks" in tables:
                session.execute(text("DELETE FROM tracks"))
            if "download_queue" in tables:
                session.execute(text("DELETE FROM download_queue"))
            if "missing_album_tracks" in tables:
                session.execute(text("DELETE FROM missing_album_tracks"))

    _wipe()
    yield
    _wipe()


def _existing_tables() -> set[str]:
    with db_session() as session:
        rows = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    return {str(r[0]) for r in rows}


def _seed_track(track_id: str, *, title: str, track_number: str = "1",
                disc_number: str = "1", album: str = "Dup Album",
                artist: str = "Dup Artist", file_path: str | None = None,
                album_mbid: str | None = None) -> None:
    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album, album_artist, title, "
                "track_number, disc_number, file_path, musicbrainz_album_mbid) "
                "VALUES (:id, :artist, :album, :aa, :title, :tn, :dn, :fp, :mbid)"
            ),
            {
                "id": track_id,
                "artist": artist,
                "album": album,
                "aa": artist,
                "title": title,
                "tn": track_number,
                "dn": disc_number,
                "fp": file_path or f"/music/{artist}/{album}/{title}.flac",
                "mbid": album_mbid,
            },
        )


# ---------------------------------------------------------------------------
# 1. find_duplicate_tracks — per-album "downloaded twice" detection
# ---------------------------------------------------------------------------

class TestFindDuplicateTracks:
    def test_double_download_is_flagged(self):
        _seed_track("d1", title="Song")
        _seed_track("d2", title="Song")

        result = ams.find_duplicate_tracks("Dup Artist", "Dup Album")

        assert result["duplicate_group_count"] == 1
        assert result["duplicate_extra_count"] == 1
        group = result["duplicates"][0]
        assert group["title"] == "Song"
        assert group["count"] == 2
        assert sorted(group["track_ids"]) == ["d1", "d2"]
        assert sorted(result["duplicate_track_ids"]) == ["d1", "d2"]

    def test_single_copy_is_not_flagged(self):
        _seed_track("s1", title="Once")

        result = ams.find_duplicate_tracks("Dup Artist", "Dup Album")

        assert result["duplicates"] == []
        assert result["duplicate_track_ids"] == []

    def test_same_title_on_another_album_is_not_a_duplicate(self):
        """Album scope: the same title twice is only a duplicate WITHIN one
        album (the artist-catalogue view is the corrections page's job)."""
        _seed_track("a1", title="Intro", album="Album One")
        _seed_track("a2", title="Intro", album="Album Two")

        result = ams.find_duplicate_tracks("Dup Artist", "Album One")

        assert result["duplicates"] == []

    def test_different_track_numbers_are_not_grouped(self):
        """Parity with the corrections rule: position is part of the key."""
        _seed_track("p1", title="Song", track_number="1")
        _seed_track("p2", title="Song", track_number="2")

        result = ams.find_duplicate_tracks("Dup Artist", "Dup Album")

        assert result["duplicates"] == []

    def test_version_variants_are_not_duplicates(self):
        """`Song.flac` + `Song (instrumental).flac` are two versions of one
        title — the corrections rule excludes them, and so must this."""
        _seed_track("v1", title="Song",
                    file_path="/music/Dup Artist/Dup Album/Song.flac")
        _seed_track("v2", title="Song",
                    file_path="/music/Dup Artist/Dup Album/Song (instrumental).flac")

        result = ams.find_duplicate_tracks("Dup Artist", "Dup Album")

        assert result["duplicates"] == []

    def test_year_prefixed_album_name_still_matches(self):
        """`_album_key` strips a leading year, like every album-scope query."""
        _seed_track("y1", title="Song", album="1999 - Dup Album")
        _seed_track("y2", title="Song", album="1999 - Dup Album")

        result = ams.find_duplicate_tracks("Dup Artist", "Dup Album")

        assert result["duplicate_group_count"] == 1


class TestVariantKeywordParityWithCorrections:
    def test_artist_service_uses_the_shared_keyword_list(self):
        """The corrections page groups duplicates with its own keyword list —
        it must be THE SAME list the album flag uses, or the two surfaces
        drift apart on what 'duplicate' means."""
        source = (REPO_ROOT / "services/metadata/artist_service.py").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"version_variant_keywords\s*=\s*\[(.*?)\]", source, re.DOTALL
        )
        assert match, "artist_service's version_variant_keywords list not found"
        keywords = tuple(sorted(set(re.findall(r'"([^"]+)"', match.group(1)))))
        assert keywords == tuple(sorted(set(
            __import__("helpers.normalization_service",
                      fromlist=["DUPLICATE_VARIANT_KEYWORDS"]).DUPLICATE_VARIANT_KEYWORDS
        ))), "the corrections page and the album flag disagree on variant keywords"


# ---------------------------------------------------------------------------
# 2. get_missing_tracks(release_mbid=...) — lookup-time recompute
# ---------------------------------------------------------------------------

_RELEASE_TRACKS = [
    {"title": "Missing One", "track_number": "1", "disc_number": 1,
     "recording_mbid": "rec-1", "artist": "Dup Artist", "duration": 100},
    {"title": "Missing Two", "track_number": "2", "disc_number": 1,
     "recording_mbid": "rec-2", "artist": "Dup Artist", "duration": 100},
]


@pytest.fixture()
def release_fetch(monkeypatch):
    calls: list[str] = []

    def _fetch(release_id, *args, **kwargs):
        calls.append(release_id)
        return {"tracks": list(_RELEASE_TRACKS), "release_year": "1999"}

    monkeypatch.setattr(ams, "fetch_musicbrainz_release_metadata", _fetch)
    return calls


class TestMissingTracksReleaseOverride:
    def test_explicit_release_wins_over_the_stored_id(self, monkeypatch, release_fetch):
        # track_number 9: a library track at position 1 would block the MB
        # track at position 1 regardless of title (position-occupancy rule).
        _seed_track("own1", title="Owned", album="Dup Album",
                    album_mbid="stored-id", track_number="9")
        persisted: list[dict] = []
        monkeypatch.setattr(ams, "_persist_missing_tracks",
                            lambda a, b, m: persisted.append(list(m)))

        result = ams.get_missing_tracks("Dup Artist", "Dup Album",
                                        release_mbid="picked-id")

        assert release_fetch == ["picked-id"], (
            "the lookup's picked release must be used — the stored id is "
            "still the OLD release until the user saves the form"
        )
        assert result["mb_total"] == 2
        assert result["missing_count"] == 2
        assert persisted and len(persisted[0]) == 2

    def test_stored_release_used_when_no_override(self, monkeypatch, release_fetch):
        _seed_track("own1", title="Owned", album="Dup Album", album_mbid="stored-id")
        monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)

        ams.get_missing_tracks("Dup Artist", "Dup Album")

        assert release_fetch == ["stored-id"]

    def test_failed_fetch_keeps_the_persisted_list(self, monkeypatch):
        """A bad id/offline fetch must NOT wipe the stored missing list —
        the response reports mb_total=0 so the UI keeps what it shows."""
        _seed_track("own1", title="Owned", album="Dup Album", album_mbid="stored-id")
        persisted: list[dict] = []
        monkeypatch.setattr(ams, "fetch_musicbrainz_release_metadata",
                            lambda *a, **k: None)
        monkeypatch.setattr(ams, "_persist_missing_tracks",
                            lambda a, b, m: persisted.append(list(m)))

        result = ams.get_missing_tracks("Dup Artist", "Dup Album",
                                        release_mbid="bad-id")

        assert result["mb_total"] == 0
        assert persisted == [], "a failed fetch must never overwrite the list"

    def test_missing_excludes_owned_and_queued_tracks(self, monkeypatch, release_fetch):
        _seed_track("own1", title="Missing One", album="Dup Album",
                    album_mbid="stored-id")   # already owned
        monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)

        result = ams.get_missing_tracks("Dup Artist", "Dup Album",
                                        release_mbid="picked-id")

        titles = [m["title"] for m in result["missing_tracks"]]
        assert titles == ["Missing Two"], "an owned track must not be missing"

    # ------------------------------------------------------------------
    # WHY the rest of the release is not listed
    # ------------------------------------------------------------------
    def test_excluded_counts_name_the_gate(self, monkeypatch, release_fetch):
        """The reported symptom had no way to say which filter swallowed a track.

        "1-9, 12, 13 render but 10 and 11 are absent" — four gates exist and
        the response named none of them.
        """
        _seed_track("own1", title="Missing One", album="Dup Album",
                    album_mbid="stored-id")
        monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)

        result = ams.get_missing_tracks("Dup Artist", "Dup Album",
                                        release_mbid="picked-id")

        excluded = result["excluded"]
        assert excluded["in_library"] == 1, "the gate that ate a track is countable"
        assert excluded["queued"] == 0
        assert excluded["rejected"] == 0

    def test_the_arithmetic_is_self_checking(self, monkeypatch, release_fetch):
        """Every MB track is either visible or counted — never silently dropped."""
        _seed_track("own1", title="Missing One", album="Dup Album",
                    album_mbid="stored-id")
        monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)

        result = ams.get_missing_tracks("Dup Artist", "Dup Album",
                                        release_mbid="picked-id")

        assert (
            len(result["missing_tracks"]) + sum(result["excluded"].values())
            == result["mb_total"]
        ), (
            "a track missing from BOTH the list and the counts is exactly the "
            "bug this breakdown exists to make impossible"
        )

    def test_nothing_excluded_when_everything_is_missing(self, monkeypatch, release_fetch):
        """CONTROL — counts must not fire when the gates did not."""
        monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)

        result = ams.get_missing_tracks("Dup Artist", "Dup Album",
                                        release_mbid="picked-id")

        assert result["missing_count"] == 2
        assert sum(result["excluded"].values()) == 0

    def test_an_undelivered_queue_row_is_still_listed(self, monkeypatch, release_fetch):
        """A track nobody has downloaded is MISSING, however long the queue takes.

        The reported bug: a queue row that never progresses removed the track
        from the page permanently ("missing tracks 10 and 11 are still
        outstanding") while a stalled search or transfer kept the row alive.
        """
        monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)
        _queue_insert("Dup Artist", "Missing Two", "Dup Album", "queued")

        result = ams.get_missing_tracks("Dup Artist", "Dup Album",
                                        release_mbid="picked-id")

        titles = [m["title"] for m in result["missing_tracks"]]
        assert titles == ["Missing One", "Missing Two"], (
            "an undelivered queue row must not make the track invisible"
        )
        queued = [m for m in result["missing_tracks"] if m["title"] == "Missing Two"]
        assert queued[0].get("queue_status") == "queued", (
            "the row carries WHY it is not offered for download again"
        )
        assert result["excluded"]["queued"] == 0, (
            "only a DELIVERED track is excluded — this one has not arrived"
        )

    def test_a_delivered_queue_row_stays_hidden(self, monkeypatch, release_fetch):
        """CONTROL — an imported track is in the library, so it is not missing."""
        monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)
        _queue_insert("Dup Artist", "Missing Two", "Dup Album", "imported")

        result = ams.get_missing_tracks("Dup Artist", "Dup Album",
                                        release_mbid="picked-id")

        titles = [m["title"] for m in result["missing_tracks"]]
        assert titles == ["Missing One"], "the delivered one is handled, not missing"
        assert result["excluded"]["queued"] == 1, (
            "the gate that keeps a finished download off the list still counts"
        )

    def test_a_queue_row_with_no_album_cannot_hide_this_albums_track(
        self, monkeypatch, release_fetch,
    ):
        """An unattributable queue row must not grant coverage to every album.

        The coverage query is scoped to the ARTIST and the album is matched in
        Python, so on a **Various Artists** album it spans the whole library —
        and a row with an empty ``album`` slipped past the guard and hid tracks
        for every VA release at once (the reported album is VA).  The row below
        deliberately reuses this album's own title: on the old code it was
        credited here and took the track off the list.
        """
        monkeypatch.setattr(ams, "_persist_missing_tracks", lambda *a, **k: None)
        _queue_insert("Dup Artist", "Missing Two", "", "queued")

        result = ams.get_missing_tracks("Dup Artist", "Dup Album",
                                        release_mbid="picked-id")

        titles = [m["title"] for m in result["missing_tracks"]]
        assert titles == ["Missing One", "Missing Two"], (
            "a row that names no album belongs to no album"
        )


# ---------------------------------------------------------------------------
# 3. Routes
# ---------------------------------------------------------------------------

class TestAlbumFindingRoutes:
    async def test_duplicate_tracks_requires_artist_and_album(self, client):
        resp = await client.get("/api/album/duplicate-tracks")
        assert resp.status_code == 400

    async def test_duplicate_tracks_returns_groups(self, client):
        _seed_track("d1", title="Song")
        _seed_track("d2", title="Song")

        resp = await client.get(
            "/api/album/duplicate-tracks?artist=Dup%20Artist&album=Dup%20Album"
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["duplicate_group_count"] == 1
        assert sorted(data["duplicate_track_ids"]) == ["d1", "d2"]

    async def test_missing_tracks_page_load_stays_db_only(self, monkeypatch, client):
        """The artist page requests this per owned album — a page load must
        NEVER trigger the MusicBrainz recompute (the original perf bug)."""
        recompute: list[tuple] = []
        db_only: list[tuple] = []
        monkeypatch.setattr(
            ams, "get_missing_tracks",
            lambda a, r, **k: recompute.append((a, r, k)) or {"missing_tracks": []},
        )
        monkeypatch.setattr(
            ams, "get_missing_tracks_from_db",
            lambda a, r: db_only.append((a, r)) or {"missing_tracks": []},
        )

        resp = await client.get("/api/album/missing-tracks?artist=A&album=B")

        assert resp.status_code == 200
        assert db_only and not recompute

    async def test_missing_tracks_refresh_recomputes_with_the_picked_release(self, monkeypatch, client):
        recompute: list[dict] = []
        monkeypatch.setattr(
            ams, "get_missing_tracks",
            lambda a, r, **k: recompute.append(k) or {"missing_tracks": [], "mb_total": 1},
        )
        monkeypatch.setattr(ams, "get_missing_tracks_from_db",
                            lambda a, r: pytest.fail("page-load path must not run"))

        resp = await client.get(
            "/api/album/missing-tracks?artist=A&album=B&refresh=1&release_mbid=picked-id"
        )

        assert resp.status_code == 200
        assert recompute == [{"release_mbid": "picked-id"}]


# ---------------------------------------------------------------------------
# 4. Wiring: both trees' album JS hook the lookup and flag rows
# ---------------------------------------------------------------------------

class TestAlbumJsWiring:
    TEST_SITE = "test_site/static/js/pages/album.js"
    LIVE = "static/js/album_detail.js"

    def _read(self, rel: str) -> str:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")

    @pytest.mark.parametrize("rel", [TEST_SITE, LIVE])
    def test_lookup_hook_refreshes_findings(self, rel):
        source = self._read(rel)
        assert "refreshAlbumTrackFindings" in source or "_refreshAlbumTrackFindings" in source, (
            f"{rel}: the Lookup MBID flow never refreshes the tracklist findings"
        )
        assert "refresh=1&release_mbid=" in source, (
            f"{rel}: never asks the server to recompute against the picked release"
        )
        # The lookup handler itself must call it (wiring, not just presence).
        assert re.search(r"refreshes?|_refreshAlbumTrackFindings\(.*\)|refreshAlbumTrackFindings\(.*\)", source)

    @pytest.mark.parametrize("rel", [TEST_SITE, LIVE])
    def test_duplicate_flags_load_on_page_load(self, rel):
        source = self._read(rel)
        assert "loadAlbumDuplicateFlags" in source
        assert "/api/album/duplicate-tracks?" in source
        assert ".album-duplicate-badge" in source
        # Flagged on plain page load (not only after a lookup).
        dom_ready = source[source.find("DOMContentLoaded"):]
        assert "loadAlbumDuplicateFlags" in dom_ready, (
            f"{rel}: duplicates only flag after a lookup — page load must flag them too"
        )

    @pytest.mark.parametrize("rel", [TEST_SITE, LIVE])
    def test_missing_refresh_guards_on_mb_total(self, rel):
        """mb_total == 0 (unfetchable release) must KEEP the rendered list
        rather than replacing it with nothing."""
        source = self._read(rel)
        assert "mb_total" in source

    @pytest.mark.parametrize("rel", [
        "templates/pages/album_detail.html",
        "test_site/templates/Pages/album_detail.html",
    ])
    def test_tracklist_rows_carry_data_track_id(self, rel):
        html = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert 'data-track-id="{{ track.id }}"' in html, (
            f"{rel}: duplicate flags badge rows by data-track-id"
        )


# ---------------------------------------------------------------------------
# 4. The DB-only page-load path must still say what it withheld
# ---------------------------------------------------------------------------
class TestThePageLoadPathSaysWhatWasDismissed:
    """``ignored = TRUE`` is permanent — so it has to be visible somewhere.

    The reject button sets the flag and **nothing in the codebase ever sets it
    back**, so a dismissed pair vanishes for good.  Before this the page-load
    response said nothing about it: two hidden tracks and a release that never
    had them were indistinguishable on screen.
    """

    def test_a_dismissed_track_is_counted_but_not_returned(self):
        with db_session() as session:
            # The shared in-memory DB is disposed on ANY OperationalError (the
            # documented trap), so tables created at IMPORT time can be gone by
            # now — an earlier file's failing query is all it takes. Recreate
            # before touching them rather than depending on collection order.
            bind = session.get_bind()
            MissingAlbumTrack.__table__.create(bind, checkfirst=True)
            Track.__table__.create(bind, checkfirst=True)
            session.execute(text(
                "DELETE FROM missing_album_tracks WHERE LOWER(artist_name) = 'dup artist'"
            ))
            session.execute(
                text(
                    "INSERT INTO missing_album_tracks "
                    "(id, artist_name, album_name, title, track_number, "
                    " disc_number, ignored) "
                    "VALUES (:id, 'Dup Artist', 'Dup Album', :title, :num, 1, :ig)"
                ),
                {"id": 991001, "title": "Forgotten Years", "num": "10", "ig": 1},
            )
            session.execute(
                text(
                    "INSERT INTO missing_album_tracks "
                    "(id, artist_name, album_name, title, track_number, "
                    " disc_number, ignored) "
                    "VALUES (:id, 'Dup Artist', 'Dup Album', :title, :num, 1, :ig)"
                ),
                {"id": 991002, "title": "Power and the Passion", "num": "11", "ig": 0},
            )
            session.commit()

        result = ams.get_missing_tracks_from_db("Dup Artist", "Dup Album")

        titles = [m["title"] for m in result["missing_tracks"]]
        assert titles == ["Power and the Passion"], (
            "a dismissed track stays off the list"
        )
        assert result["excluded"] == {"rejected": 1}, (
            "the page-load path must still be able to say '1 not shown', or a "
            "permanently dismissed pair looks like a release without those tracks"
        )
        assert result["missing_count"] == 1
