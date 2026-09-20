"""Artist page track loading: which source serves which release, and does it exist?

The artist page's expandable tracklists were broken in TWO independent ways,
and neither is caught by an import check, a syntax check, or a template parse.
Both fail only at the moment a user clicks a release row.

── DEFECT 1 — owned albums read the DB with an EXACT match ──────────────────
``fetch_album_tracklist`` filtered on

    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
      AND album = :album

while the artist page had selected that very album with

    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)

So the album is only guaranteed to be the artist's up to CASE, and the
tracklist query then compared the URL's spelling exactly. Whenever the two
differed the album was listed as owned and simultaneously reported "Tracks not
found" — the reported "track loading on the artist page is failing".

── DEFECT 2 — missing releases asked for a route that does not exist ────────
The expander called

    GET /api/musicbrainz/release/tracks?mbid=…&release_id=…

No such rule exists. The only match is ``POST /api/album/musicbrainz/release/
tracks`` — a different blueprint, a different HTTP method, and its argument
arrives as ``release_mbid`` in a JSON body. Every missing release 404'd.

Worse, the same line read ``data-release-id`` off ``.release-summary`` while the
attribute was only ever emitted on the Import button, so the id was always
``undefined`` and the request carried an empty one — a second, independent
reason the call could not have worked.

── DEFECT 3 — the cache behind it could never fill ─────────────────────────
``missing_releases.release_id`` holds a release-GROUP id, so the previous
cache-filler's bare ``get_release(release_id)`` 404'd on every attempt, was
swallowed, and left the row NULL forever. And ``_persist_missing_releases``
DELETE+INSERTs every sweep without the tracklist, throwing away whatever had
been gathered.

── WHY SOME TESTS USE A RECORDING FAKE SESSION ─────────────────────────────
The artist-page surfaces require the real ``tracks`` table, which the shared
conftest fixtures provide, so those are exercised against real SQL. The
missing-releases cache is a SECOND table that no fixture creates, and
``db.engine._is_transient_db_error`` returns True for EVERY ``OperationalError``
— including "no such table" — which makes the engine dispose and erase the
in-memory SQLite database. Creating that table in-test therefore fights the
harness, and the failure mode is nasty: the functions under test swallow a
missing table and return an empty result, so the tests pass for the wrong
reason. The cache and backfill logic is pinned against a recording fake session
instead, which exercises the same statements without depending on an engine
whose lifetime the code under test can end.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent

LIVE_STATIC = REPO_ROOT / "static"
REBUILT_STATIC = REPO_ROOT / "test_site" / "static"
LIVE_TEMPLATES = REPO_ROOT / "templates"
REBUILT_TEMPLATES = REPO_ROOT / "test_site" / "templates"

ARTIST_ROUTES = REPO_ROOT / "routes" / "artist_routes.py"

#: The route the page's missing-release expander calls.
MISSING_TRACKLIST_ROUTE = "/api/artist/release/tracklist"

#: The route it used to call, which has never existed.
RETIRED_MISSING_TRACKLIST_ROUTE = "/api/musicbrainz/release/tracks"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _module_body(path: Path) -> str:
    """A static JS file with its leading block comment removed."""
    body = _read(path).lstrip()
    if body.startswith("/*"):
        end = body.find("*/")
        if end != -1:
            body = body[end + 2:].strip()
    return body


def _release_module_paths() -> list[Path]:
    """The shared release module, in whichever trees have it."""
    return [
        p
        for p in (
            LIVE_STATIC / "js/artist-releases.js",
            REBUILT_STATIC / "js/pages/artist-releases.js",
        )
        if p.is_file()
    ]


def _release_component_paths() -> list[Path]:
    return [
        p
        for p in (
            LIVE_TEMPLATES / "components/_release_section.html",
            REBUILT_TEMPLATES / "components/_release_section.html",
        )
        if p.is_file()
    ]


# ---------------------------------------------------------------------------
# Recording fake session, for the missing-releases cache queries
# ---------------------------------------------------------------------------

class _FakeResult:
    """Just enough of a SQLAlchemy Result for the code under test."""

    def __init__(self, rows=None) -> None:
        self._rows = list(rows or [])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Replays canned results and records every statement it was given."""

    def __init__(self, responses=None) -> None:
        self.statements: list[tuple[str, dict]] = []
        self._responses = list(responses or [])

    def execute(self, statement, params=None):
        self.statements.append((str(statement), dict(params or {})))
        return self._responses.pop(0) if self._responses else _FakeResult()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    # -- helpers ---------------------------------------------------------

    def inserts(self, table: str) -> list[dict]:
        return [
            params
            for sql, params in self.statements
            if "INSERT INTO" in sql and table in sql
        ]


def _patch_session(monkeypatch, module, responses=None) -> _FakeSession:
    """Make ``module``'s ``db_session`` yield a recording fake.

    Patched on the CONSUMING module because the code resolves ``db_session``
    from its own namespace.
    """
    session = _FakeSession(responses)
    monkeypatch.setattr(module, "db_session", lambda *a, **k: session)
    return session


def _tracks_columns() -> set[str]:
    return set(_TRACK_COLUMNS)


#: Columns this file inserts. Fixed rather than reflected off the live table:
#: ``inspect(engine).get_columns`` depends on inspector/engine state that the
#: shared in-memory engine does not guarantee across fixtures, and buys nothing —
#: ``db.models.Track`` defines every one of these.
_TRACK_COLUMNS = (
    "artist", "album_artist", "album", "title", "track_number",
)


def _insert_track(db_session, track_id: str, **fields) -> None:
    columns = [c for c in _TRACK_COLUMNS if c in fields]
    names = ", ".join(["id", *columns])
    placeholders = ", ".join([f":{n}" for n in ("id", *columns)])
    db_session.execute(
        text(f"INSERT INTO tracks ({names}) VALUES ({placeholders})"),
        {"id": track_id, **{n: fields[n] for n in columns}},
    )
    db_session.commit()


@pytest.fixture(autouse=True)
def _isolated_tracks(db_session):
    """Clear ``tracks`` around every test.

    The suite runs against ONE in-memory engine, so rows from an earlier test
    outlive it and a fixed id raises ``UNIQUE constraint failed``.
    """
    def _reset() -> None:
        db_session.rollback()
        try:
            db_session.execute(text("DELETE FROM tracks"))
            db_session.commit()
        except Exception:
            db_session.rollback()

    _reset()
    yield
    _reset()


# ===========================================================================
# 1. OWNED albums must be read from the local database — case-insensitively
# ===========================================================================

class TestOwnedAlbumTracklistReadsTheDatabase:
    """The album page keys on (artist, album); the artist page must agree."""

    def test_artist_case_difference_still_returns_tracks(self, db_session) -> None:
        """The exact reported failure: album listed, tracklist 404s.

        ``/artist/dartagnan`` (or any case variant the URL happens to carry)
        must resolve the rows the parser stored as ``dArtagnan``.
        """
        from services.metadata.album_service import get_album_tracklist_from_db

        _insert_track(
            db_session, "t1",
            artist="dArtagnan", album_artist="dArtagnan",
            album="Herzblut", title="Herzblut", track_number="1",
        )
        _insert_track(
            db_session, "t2",
            artist="dArtagnan", album_artist="dArtagnan",
            album="Herzblut", title="Königin", track_number="2",
        )

        tracks = get_album_tracklist_from_db("dartagnan", "herzblut")

        assert [t["title"] for t in tracks] == ["Herzblut", "Königin"], (
            "the artist page groups albums with LOWER() on the album artist, so "
            "the tracklist lookup must compare case-insensitively too — an exact "
            "match reports 'Tracks not found' for an album it just listed."
        )

    def test_album_rows_come_from_the_tracks_table_not_musicbrainz(
        self, db_session, monkeypatch
    ) -> None:
        """Owned albums are a pure DB read — no MusicBrainz request.

        Proven by making the shared client raise: if any code path reached the
        network this fails.
        """
        import services.enrichment.musicbrainz_service as mbs
        from services.metadata.album_service import get_album_tracklist_from_db

        def _explode(*a, **k):  # pragma: no cover - only runs on regression
            raise AssertionError("owned-album tracklist must never hit MusicBrainz")

        monkeypatch.setattr(mbs, "get_shared_mb_client", _explode)

        _insert_track(
            db_session, "t1",
            artist="Metallica", album_artist="Metallica",
            album="Master of Puppets", title="Battery", track_number="1",
        )

        tracks = get_album_tracklist_from_db("Metallica", "Master of Puppets")
        assert [t["title"] for t in tracks] == ["Battery"]
        assert tracks[0]["position"] == "1"

    def test_album_artist_column_wins_over_track_artist(self, db_session) -> None:
        """A featured guest on the track row must not hide the album.

        The page's own grouping uses ``COALESCE(NULLIF(album_artist, ''), artist)``,
        so the tracklist lookup has to use the same expression or a compilation
        album vanishes from the expander.
        """
        from services.metadata.album_service import get_album_tracklist_from_db

        _insert_track(
            db_session, "t1",
            artist="dArtagnan feat. Guest", album_artist="dArtagnan",
            album="Herzblut", title="Herzblut", track_number="1",
        )

        assert len(get_album_tracklist_from_db("dArtagnan", "Herzblut")) == 1

    def test_the_route_uses_this_helper(self) -> None:
        """``/api/album/tracklist`` must be the DB helper the artist page calls.

        Asserted structurally rather than over HTTP on purpose: the route is a
        SYNC handler, so Quart runs it in the event loop's executor, and the unit
        suite's engine is an in-memory SQLite connection bound to the thread that
        created it (``sqlite3.ProgrammingError: SQLite objects created in a
        thread can only be used in that same thread``). Production runs
        PostgreSQL, where no such restriction exists. Driving the handler
        through the test client would therefore test the engine, not the code —
        and it poisons the shared engine for every later test.
        """
        source = _read(REPO_ROOT / "routes" / "album_routes.py")
        tree = ast.parse(source)

        handler = next(
            (
                node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "api_album_tracklist"
            ),
            None,
        )
        assert handler is not None, "album_routes.py no longer defines api_album_tracklist"

        body = ast.get_source_segment(source, handler) or ""
        assert "get_album_tracklist_from_db" in body, (
            "the endpoint must read the LOCAL database for an owned album"
        )


# ===========================================================================
# 2. MISSING releases must call a route that EXISTS
# ===========================================================================

class TestMissingReleaseTracklistRouteExists:
    """A URL is not checked by anything until it is requested."""

    def test_the_page_calls_the_registered_route(self, app) -> None:
        rules = {str(rule) for rule in app.url_map.iter_rules()}
        assert MISSING_TRACKLIST_ROUTE in rules, (
            f"{MISSING_TRACKLIST_ROUTE} is not registered, so every missing "
            "release on the artist page 404s."
        )

    def test_the_retired_url_has_no_rule(self, app) -> None:
        """Pins WHY the old call was dead, so it cannot be readopted by mistake.

        The nearest real rule is a POST on ``/api/album/…``. If that ever
        changes, this test fails and the comment in the JS needs revisiting.
        """
        rules = {str(rule) for rule in app.url_map.iter_rules()}
        assert RETIRED_MISSING_TRACKLIST_ROUTE not in rules, (
            "the page used to call this; if it now exists, the JS comment "
            "explaining the 404 must be updated."
        )
        assert "/api/album/musicbrainz/release/tracks" in rules

    @pytest.mark.parametrize("path", _release_module_paths())
    def test_every_api_url_in_the_module_resolves(self, app, path: Path) -> None:
        """The general guard: no literal /api/… URL may point at nothing.

        This is the class of bug rather than the instance — a tracklist URL,
        a probe URL or an edit URL can each rot the same silent way.
        """
        rules = {str(rule) for rule in app.url_map.iter_rules()}
        code = _module_body(path)
        rel = path.relative_to(REPO_ROOT)

        urls = set(re.findall(r"'(/api/[A-Za-z0-9/_-]+)", code))
        assert urls, f"{rel} no longer contains any /api/ URL — check this guard"

        missing = sorted(u for u in urls if u not in rules)
        assert not missing, (
            f"{rel} calls {missing}, which no route serves. A dead URL here is "
            "invisible until a user clicks."
        )

    @pytest.mark.parametrize("path", _release_component_paths())
    def test_summary_carries_the_release_id(self, path: Path) -> None:
        """The expander reads data-release-id from the SUMMARY element.

        It used to exist only on the Import button inside ``.release-actions``,
        so ``summary.getAttribute('data-release-id')`` returned null and the
        request was built with an empty id — a dead call even once the URL was
        right.
        """
        body = _read(path)
        rel = path.relative_to(REPO_ROOT)

        marker = re.search(r'<div[^>]*class="release-summary"[^>]*>', body)
        assert marker, f"{rel} no longer renders .release-summary"
        assert "data-release-id=" in marker.group(0), (
            f"{rel} does not emit data-release-id on .release-summary; the "
            "missing-release tracklist request will send an empty id."
        )

    def test_route_is_declared_on_the_artist_blueprint(self) -> None:
        """Static check that does not need the app to boot."""
        source = _read(ARTIST_ROUTES)
        tree = ast.parse(source)

        decorated = set()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not (isinstance(func, ast.Attribute) and func.attr == "route"):
                    continue
                if decorator.args and isinstance(decorator.args[0], ast.Constant):
                    decorated.add(decorator.args[0].value)

        assert MISSING_TRACKLIST_ROUTE in decorated, (
            "routes/artist_routes.py no longer declares "
            f"{MISSING_TRACKLIST_ROUTE}."
        )


# ===========================================================================
# 3. The cache the route depends on
# ===========================================================================

class TestMissingReleaseTracklistCache:
    """The route serves the cached list first; only an empty row hits the API."""

    def test_cached_tracklist_is_served_without_musicbrainz(
        self, monkeypatch
    ) -> None:
        import services.metadata.artist_scan_service as ass

        session = _patch_session(monkeypatch, ass, responses=[
            _FakeResult(rows=[(json.dumps(["Herzblut", "Koenigin"]),)]),
        ])

        def _explode(*a, **k):  # pragma: no cover - only runs on regression
            raise AssertionError("a cached tracklist must not trigger a lookup")

        monkeypatch.setattr(ass, "get_shared_mb_client", _explode)

        assert ass.fetch_missing_release_tracklist("rg-1", "dArtagnan") == [
            "Herzblut", "Koenigin",
        ]
        assert len(session.statements) == 1, (
            "a cache hit must run exactly the one SELECT and nothing else"
        )

    def test_synthetic_release_id_is_not_sent_to_musicbrainz(
        self, monkeypatch
    ) -> None:
        """``_build_missing_release_items`` falls back to "{title}-{category}".

        That id can never resolve, so it must not cost a request (nor a 60s
        circuit-breaker retry) on every page load. A no-such-column/table error
        on the cache read must also be survivable — hence the raising fake.
        """
        import services.metadata.artist_scan_service as ass

        def _boom(*a, **k):
            raise RuntimeError("cache unavailable")

        monkeypatch.setattr(ass, "db_session", _boom)

        def _explode(*a, **k):  # pragma: no cover - only runs on regression
            raise AssertionError("a synthetic release id is not worth a request")

        monkeypatch.setattr(ass, "get_shared_mb_client", _explode)
        assert ass.fetch_missing_release_tracklist("herzblut-single", "dArtagnan") == []

    def test_release_group_id_is_resolved_through_the_group_fallback(
        self, monkeypatch
    ) -> None:
        """``release_id`` holds a release-GROUP id, not a release id.

        A bare ``get_release(<group-id>)`` 404s, which is exactly why the old
        cache-filler never populated anything: every fetch raised, was
        swallowed, and the row stayed NULL forever.
        """
        import services.metadata.artist_scan_service as ass

        calls: list[str] = []

        class _Client:
            def get_release(self, mbid, inc=None):
                calls.append(f"get_release:{mbid}")
                if mbid == RELEASE_GROUP_MBID:
                    # A release-GROUP id is not a release id: MusicBrainz 404s.
                    raise RuntimeError("404 Not Found")
                return {"id": mbid, "media": [
                    {"tracks": [{"title": "Herzblut"}, {"recording": {"title": "Koenigin"}}]}
                ]}

            def get(self, entity, params=None, timeout=None):
                calls.append(f"get:{entity}")
                return {"releases": [{"id": "rel-1"}]}

        # 1st response = cache miss, 2nd = the UPDATE that caches the result.
        _patch_session(monkeypatch, ass, responses=[_FakeResult(), _FakeResult()])
        monkeypatch.setattr(ass, "get_shared_mb_client", lambda: _Client())
        monkeypatch.setattr(ass, "_musicbrainz_available", lambda: True)

        titles = ass.fetch_missing_release_tracklist(RELEASE_GROUP_MBID, "dArtagnan")

        assert titles == ["Herzblut", "Koenigin"]
        assert calls[0] == f"get_release:{RELEASE_GROUP_MBID}"
        assert calls[1] == "get:release", (
            "the group id must be resolved via the release-group browse, not "
            "treated as a release id"
        )

    def test_a_resolved_tracklist_is_written_to_the_cache(self, monkeypatch) -> None:
        import services.metadata.artist_scan_service as ass

        session = _patch_session(monkeypatch, ass, responses=[_FakeResult(), _FakeResult()])

        class _Client:
            def get_release(self, mbid, inc=None):
                return {"id": mbid, "media": [{"tracks": [{"title": "Herzblut"}]}]}

        monkeypatch.setattr(ass, "get_shared_mb_client", lambda: _Client())
        monkeypatch.setattr(ass, "_musicbrainz_available", lambda: True)

        assert ass.fetch_missing_release_tracklist(RELEASE_GROUP_MBID, "dArtagnan") == [
            "Herzblut"
        ]

        updates = [
            params for sql, params in session.statements
            if "UPDATE missing_releases" in sql
        ]
        assert updates, "the fetched tracklist must be cached"
        assert json.loads(updates[0]["tracklist"]) == ["Herzblut"]


class TestPersistPreservesTheCache:
    def test_tracklists_survive_the_delete_and_insert(self, monkeypatch) -> None:
        """The sweep DELETE+INSERTs the same rows every run.

        Dropping the gathered tracklist there threw away up to three
        MusicBrainz calls per release on every scan, and left the cache
        permanently empty because the filler only selects NULL/empty rows.
        """
        import services.metadata.artist_scan_service as ass

        session = _patch_session(monkeypatch, ass, responses=[
            _FakeResult(rows=[("rg-1", json.dumps(["Herzblut"]))]),
            _FakeResult(),  # DELETE
            _FakeResult(),  # INSERT rg-1
            _FakeResult(),  # INSERT rg-2
        ])

        ass._persist_missing_releases("dArtagnan", [
            {"id": "rg-1", "title": "Herzblut", "primary_type": "Album",
             "first_release_date": "2019-01-01", "cover_art_url": "",
             "category": "Album"},
            {"id": "rg-2", "title": "Feuer & Flamme", "primary_type": "Album",
             "first_release_date": "2021-01-01", "cover_art_url": "",
             "category": "Album"},
        ])

        inserts = session.inserts("missing_releases")
        assert len(inserts) == 2
        by_id = {p["release_id"]: p["tracklist"] for p in inserts}
        assert by_id["rg-1"] == json.dumps(["Herzblut"]), (
            "an existing cached tracklist must be carried across the re-insert"
        )
        assert by_id["rg-2"] is None, (
            "a release with no cached tracklist must stay NULL, not inherit one"
        )

class TestTracklistBackfillIsBackgroundOnly:
    """The backfill must never compete with a scan for the MusicBrainz budget."""

    def _rows(self, count: int) -> list[tuple[str, ...]]:
        return [(f"1111111{i}-1111-1111-1111-111111111111",) for i in range(count)]

    def test_halts_the_moment_a_scan_starts(self, monkeypatch) -> None:
        """The backfill must stop outright, not merely slow down.

        ``backfill_missing_release_tracklists`` runs inside the missing-releases
        sweep, which already pauses for a popularity scan — but each tracklist
        costs up to three *shared* MusicBrainz requests, so leaving the work
        half-done per artist still competes with the scan.
        """
        import services.metadata.artist_scan_service as ass

        session = _patch_session(monkeypatch, ass, responses=[_FakeResult(rows=self._rows(3))])
        monkeypatch.setattr(ass, "get_tracklist_backfill_limit", lambda: 10)
        monkeypatch.setattr(ass, "_popularity_scan_active", lambda: True)
        monkeypatch.setattr(
            ass, "fetch_missing_release_tracklist",
            lambda *a, **k: pytest.fail("must not fetch while a scan is running"),
        )

        assert ass.backfill_missing_release_tracklists("dArtagnan") == 0
        assert len(session.statements) == 1, (
            "the halt must happen before any tracklist request"
        )

    def test_backfill_reads_the_pending_rows_and_fills_them(self, monkeypatch) -> None:
        """Anti-vacuity: a pending row must produce exactly one fetch.

        Two things are pinned together, because they are separate risks:
          * the per-artist cap must reach SQL as ``LIMIT :limit`` — the bound is
            enforced by the database, not by the Python loop, so a loop that
            trusted its caller would still drain the queue; and
          * every row the query RETURNS must be filled, so an ``== 0`` from a
            broken query cannot masquerade as the halt path working.
        """
        import services.metadata.artist_scan_service as ass

        session = _patch_session(monkeypatch, ass, responses=[_FakeResult(rows=self._rows(2))])

        seen: list[str] = []
        monkeypatch.setattr(ass, "get_tracklist_backfill_limit", lambda: 2)
        monkeypatch.setattr(ass, "_popularity_scan_active", lambda: False)
        monkeypatch.setattr(ass, "_musicbrainz_available", lambda: True)
        monkeypatch.setattr(
            ass, "fetch_missing_release_tracklist",
            lambda rid, artist="": seen.append(rid) or ["T"],
        )

        assert ass.backfill_missing_release_tracklists("dArtagnan") == 2
        assert len(seen) == 2

        selects = [
            params for sql, params in session.statements if "LIMIT :limit" in sql
        ]
        assert selects, "the pending-release query must bound its own result set"
        assert selects[0]["limit"] == 2, (
            "the configured per-artist cap must reach SQL; nothing else limits how "
            "much of the shared MusicBrainz budget one artist can consume"
        )

    def test_stops_when_the_scan_is_cancelled(self, monkeypatch) -> None:
        import services.metadata.artist_scan_service as ass

        _patch_session(monkeypatch, ass, responses=[_FakeResult(rows=self._rows(1))])
        monkeypatch.setattr(ass, "get_tracklist_backfill_limit", lambda: 10)
        monkeypatch.setattr(ass, "_popularity_scan_active", lambda: False)
        monkeypatch.setattr(
            ass, "fetch_missing_release_tracklist",
            lambda *a, **k: pytest.fail("must not fetch after a stop request"),
        )

        assert ass.backfill_missing_release_tracklists(
            "dArtagnan", should_stop=lambda: True,
        ) == 0

    def test_stops_when_musicbrainz_is_unavailable(self, monkeypatch) -> None:
        import services.metadata.artist_scan_service as ass

        _patch_session(monkeypatch, ass, responses=[_FakeResult(rows=self._rows(2))])
        monkeypatch.setattr(ass, "get_tracklist_backfill_limit", lambda: 10)
        monkeypatch.setattr(ass, "_popularity_scan_active", lambda: False)
        monkeypatch.setattr(ass, "_musicbrainz_available", lambda: False)
        monkeypatch.setattr(
            ass, "fetch_missing_release_tracklist",
            lambda *a, **k: pytest.fail("must not fetch with the breaker open"),
        )

        assert ass.backfill_missing_release_tracklists("dArtagnan") == 0

    def test_limit_zero_disables_the_backfill(self, monkeypatch) -> None:
        import services.metadata.artist_scan_service as ass

        monkeypatch.setattr(ass, "get_tracklist_backfill_limit", lambda: 0)
        monkeypatch.setattr(
            ass, "db_session",
            lambda *a, **k: pytest.fail("0 must disable the backfill before any query"),
        )
        assert ass.backfill_missing_release_tracklists("dArtagnan") == 0

    @pytest.mark.parametrize(
        "raw,expected",
        [(0, 0), (-5, 0), (5, 5), ("12", 12), (999, 200), (None, 10), ("junk", 10)],
    )
    def test_limit_is_clamped(self, monkeypatch, raw, expected) -> None:
        import services.metadata.artist_scan_service as ass

        monkeypatch.setattr(
            ass, "get_feature", lambda key, default=None: default if raw is None else raw,
        )
        assert ass.get_tracklist_backfill_limit() == expected


class TestReleaseTracklistFlattening:
    def test_titles_are_flattened_across_media(self) -> None:
        import services.metadata.artist_scan_service as ass

        titles = ass._tracklist_titles_from_release({
            "media": [
                {"tracks": [{"title": "A"}, {"recording": {"title": "B"}}]},
                {"tracks": [{"title": "C"}, {"title": ""}, "nonsense"]},
            ]
        })
        assert titles == ["A", "B", "C"]

    def test_a_release_with_no_media_yields_nothing(self) -> None:
        import services.metadata.artist_scan_service as ass

        assert ass._tracklist_titles_from_release({}) == []
        assert ass._tracklist_titles_from_release({"media": None}) == []


class TestTheOldCacheFillerDelegates:
    def test_populate_missing_release_tracklists_uses_the_resolving_helper(
        self, monkeypatch
    ) -> None:
        """The previous inline fetch called ``get_release`` on a GROUP id.

        It therefore 404'd on every release, and it built its own
        ``MusicBrainzHttpClient`` instead of the shared singleton. Both are fixed
        by delegating; this pins the delegation so the broken call cannot return.
        """
        from services.popularity import release_cache_service as rcs

        _patch_session(monkeypatch, rcs, responses=[_FakeResult(rows=[{
            "release_id": "rg-1", "title": "T", "primary_type": "Album", "category": "Album",
        }])])

        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(
            rcs.artist_scan_service, "fetch_missing_release_tracklist",
            lambda release_id, artist="": seen.append((release_id, artist)) or ["A"],
        )

        result = rcs.populate_missing_release_tracklists("dArtagnan", limit=5)

        assert result == {"fetched": 1}
        assert seen == [("rg-1", "dArtagnan")]


# ===========================================================================
# 4. Config page is the source of truth for the new knob
# ===========================================================================

class TestConfigPageExposesTheBackfillLimit:
    """Every configurable option must be settable from the Config page."""

    @pytest.mark.parametrize("path", [
        REPO_ROOT / "templates/pages/config.html",
        REPO_ROOT / "test_site/templates/Pages/config.html",
    ])
    def test_option_is_rendered_as_a_feature_field(self, path: Path) -> None:
        body = _read(path)
        rel = path.relative_to(REPO_ROOT)

        marker = re.search(
            r'<input[^>]*name="feature_missing_release_tracklist_limit"[^>]*>', body
        )
        assert marker, (
            f"{rel} does not render the missing-release tracklist limit, so the "
            "value cannot be set from the Config page and config.js will never "
            "collect it."
        )
        assert "feature-field" in marker.group(0), (
            f"{rel}'s input is missing the feature-field class, which is what "
            "config.js collects — the value would save as absent."
        )
        # The moved-key list hides the raw key from the generic Features card;
        # omitting it renders the option TWICE.
        assert "'missing_release_tracklist_limit'" in body, (
            f"{rel} must list missing_release_tracklist_limit in "
            "moved_feature_keys or the option appears in two cards."
        )
