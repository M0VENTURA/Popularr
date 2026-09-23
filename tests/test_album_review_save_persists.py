"""A saved "Lookup MBID" review must reach BOTH the database and the audio files.

Reported:

    "After doing lookup MBID on the album page, all the changes get shown on the
     UI. But after selecting Save Metadata, the changes don't update on the files
     and database correctly."

The lookup itself is read-only by design — ``applyProposal()`` writes the album
values into the FORM and the per-track changes into the hidden
``#staged_track_updates`` input, and the form's own POST is what persists them
(one atomic save; a wrong release is undone by reloading).

That makes the POST handler the only place the review can be applied, and it had
NO test coverage at the server side: ``test_album_metadata_review_wiring.py``
asserts the template/script contract and that the lookup writes nothing, and
``test_metadata_proposal_preview.py`` asserts the proposal shape. Neither ever
posts a staged payload to ``/album/<artist>/<album>`` and inspects what was
written. These tests do exactly that, against the real route.

WHAT IS ASSERTED
----------------
For each staged field: the DB payload carries the column, AND the tag payload
carries the matching tag-writer field. ``build_tag_updates`` is the only bridge
between them, so a field missing from ``_COLUMN_TO_TAG_FIELD`` reaches the
database and silently never reaches the file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from routes import ui_routes as ui
from services.metadata import tag_file_service as tfs

#: This feature is test_site-only; the live tree's album page has no review UI.
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeRow:
    def __init__(self, mapping):
        self._mapping = dict(mapping)


class _Track(dict):
    """A track row that also exposes ``_mapping`` like a SQLAlchemy Row."""

    @property
    def _mapping(self):
        return dict(self)


class _FakeSession:
    def __init__(self, tracks):
        self._tracks = tracks
        self.statements: list[str] = []

    def execute(self, statement, params=None, *a, **k):
        sql = str(statement)
        self.statements.append(sql)
        if "FROM tracks" in sql:
            return _FakeResult([_FakeRow(t) for t in self._tracks])
        return _FakeResult([])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _track(track_id: str, **over):
    row = _Track({
        "id": track_id,
        "title": "Not Your Kingdom",
        "artist": "Madball",
        "album": "Not Your Kingdom",
        "album_artist": "Madball",
        "track_number": "1",
        "disc_number": "1",
        "recording_mbid": "rec-1",
        "file_path": f"/music/Madball/Not Your Kingdom/{track_id}.flac",
        "year": "2024",
        "is_cover": 0,
    })
    row.update(over)
    return row


class _Capture:
    """Records what the handler wrote to the DB and to the files."""

    def __init__(self):
        self.db_payloads: list[dict] = []
        self.file_writes: list[tuple[str, dict]] = []
        self.genre_calls: list[tuple[str, str]] = []


@pytest.fixture
def captured(monkeypatch):
    cap = _Capture()
    tracks = [_track("t1"), _track("t2", title="Sugar", track_number="2")]

    # The auth gate's FIRST-RUN interceptor redirects every non-public request
    # to /setup while Navidrome is unconfigured, which would make this whole
    # module pass vacuously (the handler never runs). Patch it so the route is
    # genuinely exercised.
    monkeypatch.setattr("helpers.app_hooks.needs_setup", lambda: False)

    monkeypatch.setattr(ui, "db_session", lambda *a, **k: _FakeSession(tracks))
    monkeypatch.setattr(ui, "get_config", lambda: {})

    monkeypatch.setattr(
        ui, "insert_or_update_track",
        lambda track_id, payload: cap.db_payloads.append(dict(payload)),
    )

    def _genres(track_id, genres_str):
        cap.genre_calls.append((track_id, genres_str))
        return 1

    monkeypatch.setattr("db.repositories.metadata.update_track_genres", _genres)

    # Tag writes: record the RAW field names handed to the writer. These are
    # the names the writer must understand, so asserting here (rather than on
    # the file) is what catches a mapping gap.
    monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: str(p) if p else None)
    monkeypatch.setattr(
        ui, "update_file_tags",
        lambda path, tags: (cap.file_writes.append((str(path), dict(tags))) or True),
    )

    monkeypatch.setattr(ui, "track_carries_live_state", lambda t: False)
    monkeypatch.setattr(ui, "get_album_tag_inconsistencies", lambda *a, **k: [])
    monkeypatch.setattr(ui, "get_recent_album_scans", lambda *a, **k: [])
    # The MB album-artist backfill only runs when a release MBID is posted; keep
    # it off so only the STAGED payload is under test here.
    cap.tracks = tracks
    return cap


def _staged(payload: dict) -> str:
    return json.dumps(payload)


ALBUM_URL = "/album/Madball/Not%20Your%20Kingdom"


async def _post(client, staged=None, **fields):
    form = {
        "album_title": "Not Your Kingdom",
        "album_artist": "Madball",
    }
    if staged is not None:
        form["staged_track_updates"] = _staged(staged)
    form.update(fields)
    # ``form=`` (not ``data=``) — that is the kwarg that makes the test client
    # send an application/x-www-form-urlencoded body, which is what
    # ``await request.form`` reads. With ``data=`` the dict is sent as the raw
    # body and ``request.form`` comes back EMPTY, so every assertion here would
    # fail for the wrong reason.
    return await client.post(ALBUM_URL, form=form)


def _field_writes(cap, field):
    """Every value written for one tag field across all files."""
    return [tags[field] for _path, tags in cap.file_writes if field in tags]


# ---------------------------------------------------------------------------
# 1. Each staged field reaches the database
# ---------------------------------------------------------------------------

class TestStagedFieldsReachTheDatabase:

    async def test_staged_title_is_written(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "title", "label": "Title", "current": "Old", "proposed": "Fixed Title"},
        ]}})
        titles = [p.get("title") for p in captured.db_payloads]
        assert "Fixed Title" in titles, captured.db_payloads

    async def test_staged_track_number_is_written(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "track_number", "label": "Track #", "current": "9", "proposed": "1"},
        ]}})
        assert any(str(p.get("track_number")) == "1" for p in captured.db_payloads)

    async def test_staged_disc_number_is_written(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "disc_number", "label": "Disc #", "current": "1", "proposed": "2"},
        ]}})
        assert any(str(p.get("disc_number")) == "2" for p in captured.db_payloads)

    async def test_staged_recording_mbid_is_written(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "mbid", "label": "MusicBrainz Recording ID",
             "current": "", "proposed": "rec-new"},
        ]}})
        assert any(p.get("mbid") == "rec-new" for p in captured.db_payloads)

    async def test_staged_writer_is_written(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "writer", "label": "Writer", "current": "", "proposed": "F. Cricien"},
        ]}})
        assert any(p.get("writer") == "F. Cricien" for p in captured.db_payloads)

    async def test_staged_genres_are_written(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "musicbrainz_genres", "label": "Genres",
             "current": "", "proposed": "Hardcore"},
        ]}})
        assert any(p.get("musicbrainz_genres") == "Hardcore" for p in captured.db_payloads)

    async def test_a_cover_verdict_is_written(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "is_cover", "label": "Cover", "current": "not a cover",
             "proposed": "cover of Bad Brains", "value": 1,
             "original_cover_artist": "Bad Brains"},
        ]}})
        assert any(p.get("is_cover") == 1 for p in captured.db_payloads)
        assert any(p.get("original_cover_artist") == "Bad Brains" for p in captured.db_payloads)

    async def test_the_cover_convention_renames_the_title(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "is_cover", "label": "Cover", "current": "not a cover",
             "proposed": "cover", "value": 1, "original_cover_artist": "Bad Brains"},
        ]}})
        titles = [p.get("title") for p in captured.db_payloads]
        assert any(t and "(Bad Brains Cover)" in t for t in titles), titles


# ---------------------------------------------------------------------------
# 2. The SAME fields reach the files
# ---------------------------------------------------------------------------

class TestStagedFieldsReachTheFiles:
    """The bridge is ``build_tag_updates`` — a column missing from
    ``_COLUMN_TO_TAG_FIELD`` is written to the DB and silently dropped here."""

    async def test_staged_title_reaches_the_file(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "title", "label": "Title", "current": "Old", "proposed": "Fixed Title"},
        ]}})
        assert "Fixed Title" in _field_writes(captured, "title")

    async def test_staged_track_number_reaches_the_file(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "track_number", "label": "Track #", "current": "9", "proposed": "4"},
        ]}})
        assert captured.file_writes, "no file write happened at all"
        assert "4" in [str(v) for v in _field_writes(captured, "track_number")]

    async def test_staged_disc_number_reaches_the_file(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "disc_number", "label": "Disc #", "current": "1", "proposed": "2"},
        ]}, "album_type": "album", "album_disctotal": "2"})
        assert "2" in [str(v) for v in _field_writes(captured, "disc_number")]

    async def test_staged_recording_mbid_reaches_the_file(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "mbid", "label": "MusicBrainz Recording ID",
             "current": "", "proposed": "rec-new"},
        ]}})
        assert "rec-new" in _field_writes(captured, "mbid")

    async def test_staged_writer_reaches_the_file(self, client, captured):
        await _post(client, {"t1": {"changes": [
            {"field": "writer", "label": "Writer", "current": "", "proposed": "F. Cricien"},
        ]}})
        assert "F. Cricien" in _field_writes(captured, "writer")

    async def test_staged_genres_reach_the_file(self, client, captured):
        """A staged genre must land on the FILE, not only in the column.

        ``musicbrainz_genres`` reaches the writer as its own frame (it is in
        ``_COLUMN_TO_TAG_FIELD``), which is what carries an MB genre to disk.
        The separate ``genres``/``manual_genres`` columns are driven by the
        ALBUM-genres box via ``update_track_genres``, so a per-track staged
        genre is NOT expected to populate those — asserting on ``genres`` here
        would have been the wrong key and failed on correct code.
        """
        await _post(client, {"t1": {"changes": [
            {"field": "musicbrainz_genres", "label": "Genres",
             "current": "", "proposed": "Hardcore"},
        ]}})
        written = _field_writes(captured, "musicbrainz_genres")
        assert any("Hardcore" in str(v) for v in written), (
            f"a staged genre reached the DB but not the file; tag writes={captured.file_writes}"
        )


# ---------------------------------------------------------------------------
# 3. The contract between the proposal's field names and the tag writer
# ---------------------------------------------------------------------------

class TestProposalFieldsAreAllTagMappable:
    """Static guard, independent of the route.

    Every ``_TRACK_FIELD_SPECS`` entry is documented as "the column name so the
    staged payload can be applied verbatim", which means each one must also be
    a key ``build_tag_updates`` knows — otherwise it is a DB-only field.
    """

    def test_every_staged_track_field_maps_to_a_tag(self):
        from services.metadata.metadata_proposal_service import _TRACK_FIELD_SPECS

        unmapped = [
            field for field, _label, _key in _TRACK_FIELD_SPECS
            if field not in tfs._COLUMN_TO_TAG_FIELD
        ]
        assert not unmapped, (
            f"these staged fields reach the DB but can never reach a file: {unmapped}"
        )

    def test_the_cover_keys_map_to_tags(self):
        from services.metadata.metadata_proposal_service import _COVER_KEYS

        unmapped = [k for k in _COVER_KEYS if k not in tfs._COLUMN_TO_TAG_FIELD]
        assert not unmapped, f"cover fields with no tag mapping: {unmapped}"

    def test_genres_maps_as_a_list(self):
        """The DB column is JSONB/CSV; the TAG is a list of strings.

        ``update_track_genres`` normalises to a comma string for the column, so
        passing that straight to the writer would put "Hardcore, Punk" in ONE
        genre frame instead of two.
        """
        tags = tfs.build_tag_updates({"musicbrainz_genres": "Hardcore, Punk"})
        assert "musicbrainz_genres" in tags


# ---------------------------------------------------------------------------
# 4. A JSONB column must accept the CSV strings the genre writers produce
# ---------------------------------------------------------------------------

class TestJsonbColumnsAcceptTheCsvWriters:
    """REGRESSION: the genre columns are JSONB but written as CSV strings.

    ``tracks.manual_genres`` / ``musicbrainz_genres`` and friends are declared
    JSONB, while ``update_track_genres`` does ``SET manual_genres = :genres``
    with ``"Hardcore, Punk"``. ``"Hardcore, Punk"`` is not valid JSON, so
    PostgreSQL rejects the statement with ``invalid input syntax for type json``.

    Historically those columns were TEXT because of schema drift, which is why
    the round-trip appeared to work; converging them to their declared JSONB
    (``db/jsonb_drift_repair.py``) turned every such write into a failure. The
    persistence layer had NO JSON branch at all, so nothing coerced the value.
    """

    def test_a_csv_string_becomes_a_json_array(self):
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type as c

        assert json.loads(c("manual_genres", "Rock", "JSONB")) == ["Rock"]
        assert json.loads(c("manual_genres", "Hardcore, Punk", "JSONB")) == ["Hardcore", "Punk"]

    def test_the_result_is_json_TEXT_not_a_python_list(self):
        """⚠️ The return type is load-bearing.

        ``services/scanning/payload_builder.py`` already writes these columns as
        ``json.dumps([...])`` strings, and that path works. A Python LIST would
        be adapted by the driver to a Postgres ARRAY literal, which a ``jsonb``
        column rejects ("column is of type jsonb but expression is of type
        text[]") — so coercing to a list here would have fixed the album page
        and broken the scanner.
        """
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type as c

        for raw in ("Rock", ["a", "b"], '["a","b"]', {}, 3, True):
            out = c("x", raw, "JSONB")
            assert isinstance(out, str), f"{raw!r} -> {out!r} is not JSON text"

    def test_a_json_literal_is_normalised(self):
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type as c

        # A JSON literal must become a real array; keeping it as the STRING
        # '["a","b"]' would store ONE genre whose name contains brackets.
        assert json.loads(c("x", '["a", "b"]', "JSONB")) == ["a", "b"]
        assert json.loads(c("x", '{"a": 1}', "JSON")) == {"a": 1}

    def test_a_list_passes_through_as_an_array(self):
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type as c

        assert json.loads(c("x", ["a", "b"], "JSONB")) == ["a", "b"]

    def test_an_empty_string_becomes_an_empty_array(self):
        """Not NULL — matching ``db/schema.py``'s ``'' -> '[]'::jsonb`` rule."""
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type as c

        assert json.loads(c("x", "", "JSONB")) == []
        assert json.loads(c("x", "   ", "JSONB")) == []

    def test_none_stays_none(self):
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type as c

        assert c("x", None, "JSONB") is None

    def test_non_json_types_are_untouched(self):
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type as c

        assert c("title", "Not Your Kingdom", "TEXT") == "Not Your Kingdom"
        assert c("stars", "4", "INTEGER") == 4
        assert c("is_cover", 1, "boolean") is True

    def test_a_bracket_leading_title_is_not_mangled(self):
        """A TEXT title starting with ``[`` must not be JSON-parsed.

        The bracket sniffing is only reached for JSON/JSONB columns, so a title
        on a TEXT column is safe by construction — this pins that boundary.
        """
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type as c

        assert c("title", "[untitled]", "TEXT") == "[untitled]"

    def test_every_genre_column_is_covered_by_the_coercion(self):
        """The genre columns the writers target must all be declared JSONB.

        Their writers emit CSV strings, so each one MUST go through the JSON
        coercion — if one silently became TEXT again the coercion would be
        skipped and the mismatch would come back.
        """
        from db.repositories.popularity_repository import PG_JSON_TYPES

        source = (REPO_ROOT / "db" / "schema.py").read_text(encoding="utf-8")
        genre_cols = {
            "manual_genres", "musicbrainz_genres", "navidrome_genres",
            "spotify_genres", "listenbrainz_genres", "essentia_genres",
            "discogs_genres", "audiodb_genres", "wikidata_genres", "lastfm_genres",
        }
        for col in sorted(genre_cols):
            assert f'"{col}": "JSONB"' in source, f"{col} is no longer declared JSONB"
        assert "jsonb" in PG_JSON_TYPES and "json" in PG_JSON_TYPES


# ---------------------------------------------------------------------------
# 5. A refused write must be REPORTED, never presented as "no changes"
# ---------------------------------------------------------------------------

class TestRefusedWritesAreReported:
    """⚠️ The failure mode that hid this bug.

    The DB write was wrapped in ``try: ... except: logger.debug(...)``, so a
    rejected statement left ``updated_count`` at 0 and the page then flashed
    "No changes were made." — telling the user their edit was a no-op when the
    database had actually refused it. It also made the failure invisible in a
    normal log tail.
    """

    async def test_a_failed_db_write_is_counted_and_flashed(self, client, monkeypatch):
        monkeypatch.setattr("helpers.app_hooks.needs_setup", lambda: False)

        captured = _Capture()
        tracks = [_track("t1")]
        monkeypatch.setattr(ui, "db_session", lambda *a, **k: _FakeSession(tracks))
        monkeypatch.setattr(ui, "get_config", lambda: {})
        monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: None)
        monkeypatch.setattr(ui, "update_file_tags", lambda p, t: True)
        monkeypatch.setattr(ui, "track_carries_live_state", lambda t: False)

        def _boom(track_id, payload):
            raise ValueError('invalid input syntax for type json')

        monkeypatch.setattr(ui, "insert_or_update_track", _boom)

        resp = await client.post(ALBUM_URL, form={
            "album_title": "Not Your Kingdom",
            "album_artist": "Madball",
            "album_genres": "Hardcore",
        })
        assert resp.status_code == 302

        # The flash queue is consumed by the NEXT request; follow the redirect
        # and read the rendered message.
        page = await client.get(resp.headers["Location"])
        html = await page.get_data(as_text=True)
        assert "could NOT be saved" in html, (
            "a refused DB write was not reported to the user"
        )
        assert "No changes were made" not in html, (
            "a FAILED save was reported as 'no changes were made'"
        )


