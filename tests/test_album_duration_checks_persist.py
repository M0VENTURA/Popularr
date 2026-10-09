"""The album page's "Length differs" notice must outlive Save Metadata.

Reported
--------
> when an album shows something like "incorrect track length" it shows until
> the metadata is saved. I want that to stay there always until the issue is
> resolved as a save doesn't fix the length.

and, choosing "never dismiss":

> it should always show while the mismatch exists, with no dismissal at all.

Why it vanished
---------------
The rows were rendered ONLY from a proposal — the "Lookup MBID" preview, or
the recommendations a scan stashed. Both are consumed by Save Metadata (and by
"Discard all"), so saving erased the notice while the length itself was
untouched: a metadata save cannot change a file's duration.

The fix
-------
* the findings get their OWN read path — ``album_duration_checks`` behind
  ``GET /api/album/duration-checks`` — with no stash behind it, fetched on
  every album page load;
* ``clearAll()`` no longer removes ``.mb-duration-row``, so Save and Discard
  leave the notices standing;
* ``renderDurationChecks`` REPLACES its own rows, so the independent fetch can
  never double them;
* a FAILED comparison answers ``success: False`` and clears nothing — silence
  is not evidence that the lengths now agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_JS = REPO_ROOT / "static" / "js" / "metadata-review.js"
REBUILT_JS = REPO_ROOT / "test_site" / "static" / "js" / "services" / "metadata-review.js"
ALBUM_ROUTES = REPO_ROOT / "routes" / "album_routes.py"

ARTIST = "Sirenia"


def _enable_regexp_replace() -> None:
    """Give the shared SQLite engine the album page's Postgres-only function.

    ``regexp_replace`` lives in the album page's ``ORDER BY``. It is stamped on
    the LIVE connection as well as registered for future ones, because
    ``StaticPool`` may already have opened that connection before this module
    was imported — an event listener alone would then never fire.
    """
    import re as _re

    from conftest import register_sqlite_regexp_replace
    from db.engine import get_engine

    def _regexp_replace(value, pattern, repl, flags=""):
        if value is None:
            return None
        try:
            return _re.sub(
                pattern, repl, str(value),
                flags=_re.IGNORECASE if "i" in flags else 0,
            )
        except Exception:
            return str(value)

    engine = get_engine()
    register_sqlite_regexp_replace(engine)
    try:
        with engine.connect() as conn:
            pooled = conn.connection
            dbapi = getattr(pooled, "dbapi_connection", None) or pooled.driver_connection
            dbapi.create_function("regexp_replace", -1, _regexp_replace)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _sqlite_regexp_replace():
    _enable_regexp_replace()
    yield


def _seed(db_session, album: str, *, mbid: str = "rel-1") -> None:
    from sqlalchemy import text

    db_session.execute(
        text("""
            INSERT INTO tracks (id, artist, album, title, file_path, duration,
                                musicbrainz_album_mbid)
            VALUES (:id, :artist, :album, 'The Last Call',
                    '/music/sirenia/album/01.mp3', 241, :mbid)
            ON CONFLICT DO NOTHING
        """),
        {"id": f"len-{album[:12]}", "artist": ARTIST, "album": album, "mbid": mbid},
    )
    db_session.commit()


def _comparison(diff: bool = True) -> dict:
    entry = {
        "matched": True,
        "library_track_id": "len-1",
        "library_title": "The Last Call",
        "library_track_number": "1",
        "library_duration_display": "4:01",
        "mb_duration_display": "3:52",
        "mb_disc_number": "1",
        "mb_recording_mbid": "rec-1",
        "mb_duration": 232000,
        "library_artist": ARTIST,
        "mb_artist": ARTIST,
    }
    entry["diff_fields"] = ["duration"] if diff else []
    return {"success": True, "comparison": [entry]}


class TestTheFindingIsReadWithoutAStash:
    def test_checks_come_back_with_nothing_stashed(self, monkeypatch, db_session):
        """THE POINT: no proposal, no scan stash, no save — just the finding."""
        _seed(db_session, "Length Notice Album", mbid="rel-1")
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.compare_musicbrainz_release",
            lambda *a, **k: _comparison(),
        )

        from services.metadata.metadata_proposal_service import album_duration_checks

        result = album_duration_checks(ARTIST, "Length Notice Album")

        assert result["success"] is True
        assert len(result["duration_checks"]) == 1, (
            "a length mismatch must be reported without a proposal to ride on"
        )
        assert result["counts"]["duration_mismatches"] == 1
        check = result["duration_checks"][0]
        assert check["track_id"] == "len-1"
        assert check["library_duration"] == "4:01"
        assert check["mb_duration"] == "3:52"

    def test_a_failed_comparison_never_claims_resolved(self, monkeypatch, db_session):
        """MusicBrainz being down must not read as "the lengths agree"."""
        _seed(db_session, "Failed Compare Album", mbid="rel-2")
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.compare_musicbrainz_release",
            lambda *a, **k: {"success": False, "error": "MusicBrainz overloaded"},
        )

        from services.metadata.metadata_proposal_service import album_duration_checks

        result = album_duration_checks(ARTIST, "Failed Compare Album")

        assert result["success"] is False, (
            "success + no checks is indistinguishable from 'resolved' and "
            "would clear notices the page could not verify"
        )
        assert result["duration_checks"] == []

    def test_an_unbound_album_says_nothing_rather_than_failing(
        self, monkeypatch, db_session
    ):
        _seed(db_session, "Unbound Length Album", mbid="")

        def _boom(*_a, **_k):
            raise AssertionError("an album with no binding must not call MusicBrainz")

        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.compare_musicbrainz_release",
            _boom,
        )

        from services.metadata.metadata_proposal_service import album_duration_checks

        result = album_duration_checks(ARTIST, "Unbound Length Album")

        assert result["success"] is True
        assert result["duration_checks"] == []


class TestTheAlbumPageAlwaysAsks:
    def test_the_route_is_registered(self):
        src = ALBUM_ROUTES.read_text(encoding="utf-8")
        assert '@album_bp.route("/duration-checks", methods=["GET"])' in src, (
            "the album page has no independent read path for the notices"
        )

    async def test_missing_names_are_rejected(self, client):
        resp = await client.get("/api/album/duration-checks")
        assert resp.status_code == 400
        payload = await resp.get_json()
        assert payload["success"] is False

    async def test_an_unbound_album_answers_successfully(self, client, db_session):
        """No binding → nothing to compare → 200 with an empty list, never a
        failure the page would have to swallow."""
        _seed(db_session, "Route Length Album", mbid="")
        resp = await client.get(
            f"/api/album/duration-checks?artist={ARTIST}&album=Route%20Length%20Album"
        )
        assert resp.status_code == 200
        payload = await resp.get_json()
        assert payload["success"] is True
        assert payload["duration_checks"] == []


class TestTheNoticesSurviveSaveAndDiscard:
    def test_clear_all_leaves_them_alone(self):
        for path in (LIVE_JS, REBUILT_JS):
            src = path.read_text(encoding="utf-8")
            start = src.index("function clearAll()")
            block = src[start:src.index("\n  }", start)]
            assert "querySelectorAll('.mb-duration-row')" not in block, (
                f"{path.name}: clearAll still removes the length notices, so "
                "Save Metadata or Discard all would hide an unfixed mismatch"
            )

    def test_the_stash_loader_does_not_own_them(self):
        for path in (LIVE_JS, REBUILT_JS):
            src = path.read_text(encoding="utf-8")
            start = src.index("async function loadPendingRecommendations(")
            # Ends where the INDEPENDENT loader begins — slicing to the next
            # comment would run past the loader I inserted there.
            end = src.index("async function loadDurationChecks(", start)
            loader = src[start:end]
            assert "renderDurationChecks(" not in loader, (
                f"{path.name}: the stash still renders the length notices, so "
                "they vanish the moment Save consumes the stash"
            )

    def test_they_are_fetched_on_every_page_load(self):
        for path in (LIVE_JS, REBUILT_JS):
            src = path.read_text(encoding="utf-8")
            assert "async function loadDurationChecks(" in src, (
                f"{path.name}: no independent fetch for the length notices"
            )
            at = src.index("DOMContentLoaded")
            init = src[at:at + 900]
            assert "loadDurationChecks()" in init, (
                f"{path.name}: the notices are not requested on page load, so "
                "an album with nothing staged shows no length warning at all"
            )

    def test_rendering_replaces_rather_than_appends(self):
        for path in (LIVE_JS, REBUILT_JS):
            src = path.read_text(encoding="utf-8")
            start = src.index("function renderDurationChecks(")
            at = src.index("(checks || []).forEach", start)
            block = src[start:src.index("\n", at)]
            assert "querySelectorAll('.mb-duration-row')" in block, (
                f"{path.name}: renderDurationChecks appends, so the independent "
                "fetch would double every notice"
            )
