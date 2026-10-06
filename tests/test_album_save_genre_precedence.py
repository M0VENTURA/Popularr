"""Album genres must never overwrite a track's own genres.

Reported:

> Album genres seem to be overwriting track genres when an album is saved.
> Genres attached to tracks for Musicbrainz, Last.fm, etc should have a
> greater preference than genres saved to albums.

Root cause (two compounding writes in the album-save loop):

1. ``album_genres`` is the hidden input behind the album's genre chips, and
   it is prefilled with ``collect_top_genres(tracks)`` — an AGGREGATE of the
   tracks' own source columns. Saving the album wrote that aggregate back to
   EVERY track's ``genres``, flattening each track's precise list to the
   album blend.
2. The same call wrote it into ``manual_genres`` — a per-track SOURCE that
   ``collect_top_genres`` and the aggregators read back. Every track then
   claimed the album blend as its own manual genres, so the overwrite
   disguised itself as track-level evidence and survived every later save.

The rule implemented here: a track with ANY genre evidence of its own keeps
it; the album's list only FILLS a track that has no genres at all, and it
lands in ``genres`` alone — never in ``manual_genres``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from routes import ui_routes as ui  # noqa: E402


# ---------------------------------------------------------------------------
# 1. The precedence rule (pure)
# ---------------------------------------------------------------------------

class TestTrackEvidenceOutranksTheAlbumList:
    @pytest.mark.parametrize(
        ("column", "value"),
        [
            # The reported sources first: MusicBrainz and Last.fm.
            ("musicbrainz_genres", '["metalcore"]'),
            ("lastfm_tags", '["melodic hardcore"]'),
            ("genres", "Hardcore"),
            # …and the rest of the evidence set.
            ("manual_genres", '["punk"]'),
            ("navidrome_genres", '["metal"]'),
            ("listenbrainz_genres", '["post-hardcore"]'),
            ("discogs_genres", '["metalcore"]'),
            ("essentia_genres", '["rock"]'),
            ("spotify_genres", '["metal"]'),
            ("audiodb_genres", '["metal"]'),
            ("wikidata_genres", '["metal"]'),
        ],
    )
    def test_any_track_source_wins(self, column: str, value):
        track = {column: value}
        assert ui._track_keeps_its_own_genres(track) is True, column

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("musicbrainz_genres", None),
            ("musicbrainz_genres", ""),
            ("musicbrainz_genres", []),
            ("musicbrainz_genres", "[]"),
            ("genres", None),
            ("genres", ""),
            ("lastfm_tags", "{}"),
            ("manual_genres", "null"),
        ],
    )
    def test_empty_markers_are_not_evidence(self, column: str, value):
        """Only a track with NOTHING at all may receive the album's list."""
        assert ui._track_keeps_its_own_genres({column: value}) is False

    def test_a_bare_track_loses_to_the_album_list(self):
        assert ui._track_keeps_its_own_genres({"id": "t1", "title": "x"}) is False

    def test_a_staged_mb_genre_counts_as_evidence(self):
        """The Lookup-MBID review lands later in the SAME save — the album
        list must not win the race and contradict it."""
        staged = {"musicbrainz_genres": "Hardcore"}
        assert ui._track_keeps_its_own_genres({"id": "t1"}, staged) is True

    def test_an_empty_staged_value_does_not(self):
        assert ui._track_keeps_its_own_genres({"id": "t1"}, {"musicbrainz_genres": ""}) is False


# ---------------------------------------------------------------------------
# 2. The write is a FILL, and manual_genres stays per-track
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _empty_tracks(db_session):
    """Fixed ids + ON CONFLICT DO NOTHING = silently seeing another test's rows."""
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()


def _seed(db_session, track_id: str, **cols):
    db_session.execute(
        text("""
            INSERT INTO tracks (id, artist, album, title, file_path, genres, manual_genres)
            VALUES (:id, :artist, :album, :title, :file_path, :genres, :manual_genres)
            ON CONFLICT DO NOTHING
        """),
        {
            "id": track_id,
            "artist": "Madball",
            "album": "Not Your Kingdom",
            "title": track_id,
            "file_path": f"/music/{track_id}.flac",
            "genres": None,
            "manual_genres": None,
            **cols,
        },
    )
    db_session.commit()


def _row(db_session, track_id: str):
    row = db_session.execute(
        text("SELECT genres, manual_genres FROM tracks WHERE id = :id"),
        {"id": track_id},
    ).fetchone()
    return {"genres": row[0], "manual_genres": row[1]}


class TestTheAlbumFillNeverTouchesManualGenres:
    def test_the_fill_writes_genres_only(self, db_session):
        """The album's list may fill a bare track — but ``manual_genres`` is a
        per-track SOURCE the aggregators read back; stamping it with an
        album-wide value is how the old overwrite made itself permanent."""
        _seed(db_session, "gprec-fill")

        rows, failed = ui._apply_album_track_genres("gprec-fill", "Rock, Pop")

        assert (rows, failed) == (1, 0)
        after = _row(db_session, "gprec-fill")
        assert after["genres"] == "Rock, Pop"
        assert after["manual_genres"] in (None, ""), after

    def test_the_default_write_still_sets_manual_genres(self, db_session):
        """CONTROL — the other callers (apply_genres_to_album, the genre
        update flows) still write the pair; only the album save opts out."""
        from db.repositories.metadata import update_track_genres

        _seed(db_session, "gprec-manual")

        assert update_track_genres(track_id="gprec-manual", genres_str="Rock") == 1
        after = _row(db_session, "gprec-manual")
        assert after["genres"] == "Rock"
        assert json.loads(after["manual_genres"]) == ["Rock"]


# ---------------------------------------------------------------------------
# 3. End to end through the album save
# ---------------------------------------------------------------------------

ALBUM_URL = "/album/Madball/Not%20Your%20Kingdom"
ARTIST = "Madball"
ALBUM = "Not Your Kingdom"


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
    @property
    def _mapping(self):
        return dict(self)


class _FakeSession:
    def __init__(self, tracks):
        self._tracks = tracks

    def execute(self, statement, params=None, *a, **k):
        if "FROM tracks" in str(statement):
            return _FakeResult([_FakeRow(t) for t in self._tracks])
        return _FakeResult([])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _route_track(track_id: str, **over):
    """The route's view of a row (SELECT * — every column travels)."""
    row = _Track({
        "id": track_id,
        "title": track_id,
        "artist": ARTIST,
        "album": ALBUM,
        "album_artist": ARTIST,
        "track_number": "1",
        "disc_number": "1",
        "file_path": f"/music/{track_id}.flac",
        "year": "2024",
    })
    row.update(over)
    return row


@pytest.fixture
def save_env(monkeypatch, db_session):
    """A two-track album: one with its own genres, one bare — through the
    REAL route and the REAL repository write (only file I/O is faked)."""
    _seed(db_session, "gprec-with", genres="metalcore")
    db_session.execute(
        text("UPDATE tracks SET musicbrainz_genres = :g WHERE id = 'gprec-with'"),
        {"g": '["metalcore"]'},
    )
    _seed(db_session, "gprec-bare")

    monkeypatch.setattr("helpers.app_hooks.needs_setup", lambda: False)

    # Route reads (SELECT *) come from here; the GENRE WRITE deliberately goes
    # to the real repository against the real rows, so the assertions below
    # observe what a user's database would actually contain.
    tracks = [
        _route_track("gprec-with", genres="metalcore", musicbrainz_genres=["metalcore"]),
        _route_track("gprec-bare"),
    ]
    monkeypatch.setattr(ui, "db_session", lambda *a, **kw: _FakeSession(tracks))
    monkeypatch.setattr(ui, "get_config", lambda: {})
    monkeypatch.setattr(ui, "insert_or_update_track", lambda track_id, payload: True)
    monkeypatch.setattr(ui, "resolve_music_file_path", lambda p: None)
    monkeypatch.setattr(ui, "update_file_tags", lambda path, tags: True)
    monkeypatch.setattr(ui, "track_carries_live_state", lambda t: False)
    monkeypatch.setattr(ui, "get_album_tag_inconsistencies", lambda *a, **k: [])
    monkeypatch.setattr(ui, "get_recent_album_scans", lambda *a, **k: [])
    return db_session


async def _post(client, **fields):
    form = {
        "album_title": ALBUM,
        "album_artist": ARTIST,
        "album_genres": "Rock, Pop",
    }
    form.update(fields)
    # ``form=`` is what makes ``await request.form`` read the body.
    return await client.post(ALBUM_URL, form=form)


class TestTheAlbumSaveKeepsTrackGenres:
    async def test_a_track_with_musicbrainz_genres_keeps_them(self, client, save_env):
        await _post(client)

        after = _row(save_env, "gprec-with")
        assert after["genres"] == "metalcore", (
            "the album's aggregate replaced the track's own MusicBrainz genres"
        )

    async def test_a_bare_track_receives_the_album_list(self, client, save_env):
        """CONTROL — the album genres still reach a track that has none."""
        await _post(client)

        after = _row(save_env, "gprec-bare")
        assert after["genres"] == "Rock, Pop"

    async def test_no_manual_genres_are_fabricated(self, client, save_env):
        """The album-wide list must never become a track's manual genres."""
        await _post(client)

        for track_id in ("gprec-with", "gprec-bare"):
            after = _row(save_env, track_id)
            assert after["manual_genres"] in (None, ""), (
                f"{track_id}: manual_genres={after['manual_genres']!r} — the album "
                "list was stamped into a per-track SOURCE"
            )


class TestTheSaveSaysWhatItDid:
    def test_the_route_has_a_kept_genres_branch(self):
        """A genres-only save that kept everything must not claim failure."""
        source = (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")
        assert "_track_keeps_its_own_genres(track, _staged_for_track)" in source
        assert "kept their own track-level genres" in source

    def test_the_fill_passes_write_manual_off(self):
        source = (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")
        assert "write_manual=False" in source, (
            "the album save must opt out of writing manual_genres"
        )
