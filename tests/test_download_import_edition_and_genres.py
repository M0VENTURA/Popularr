"""A downloaded album must import with the Release Name, Release Year and
MusicBrainz genres the release provides.

Reported: *"When downloading an album, it updates all the metadata from
Musicbrainz, but currently is missing adding Catalog Number, Release Date,
Release Name and Release year and musicbrainz genres."*

Measured against the real import path, the download dropped THREE things an
album-page Lookup MBID writes for the same release:

* **Release Name** (``release_title``) — the specific EDITION, e.g.
  "Experience: Expanded (Remixes and B-Sides)". It is the tagline the album
  page shows under the album name and the field that distinguishes two
  pressings of one album. ``_flatten_release`` has always emitted it, but
  ``_ALBUM_LEVEL_COLUMNS`` did not name it, so nothing read it.
* **Release Year** (``release_year``) — THIS edition's year, as opposed to
  ``year``, which is the release GROUP's original year.
* **MusicBrainz genres** — the release GROUP's and the RELEASE's curated
  genres. Only the per-RECORDING genres travelled, so an album whose release
  group is fully tagged but whose recordings are not came back with no genre
  at all.

Catalog Number and Release Date were already carried, but the release date
was written to the DATABASE and not to the FILE (missing from
``_ALBUM_MB_TAG_COLUMNS``), so the two disagreed about the same release.
"""

from __future__ import annotations

import json

import pytest

from services.downloads import download_completion_service as dcs

RELEASE = {
    "id": "rel-1",
    "title": "The Specific Edition Name",
    "specific_release_title": "The Specific Edition Name",
    "release_title": "The Specific Edition Name",
    "release_group_title": "The Album Name",
    "status": "Official",
    "date": "2024-05-17",
    "releasedate": "2024-05-17",
    "country": "GB",
    "releasecountry": "GB",
    "barcode": "50601175009999",
    "recordlabel": "Big Label",
    "catalognumber": "CAT-12345",
    "media": "CD",
    "album_type": "album",
    "album_artist_mbid": "art-1",
    "release_group_mbid": "rg-1",
    "original_year": "1994",
    "original_date": "1994-06-01",
    "version_release_year": 2024,
    "release_year": 2024,
    "disctotal": "1",
    # The release GROUP's curated genres, then the RELEASE's own.
    "release_group_genres": "Rock, Metal",
    "release_genres": "Metal, Thrash Metal",
    "tracks": [
        {
            "recording_mbid": "rec-a",
            "title": "Song One",
            "musicbrainz_genres": "Metal",
        }
    ],
}


class TestAlbumLevelFieldsCarryTheEdition:
    """``_album_level_mb_fields`` must expose the edition name and year."""

    def test_release_name_is_carried(self):
        fields = dcs._album_level_mb_fields(RELEASE)
        assert fields.get("release_title") == "The Specific Edition Name", (
            "the Release Name (the specific edition) has to reach the queue "
            "row, or the download has nothing to write"
        )

    def test_release_year_is_the_edition_year_not_the_original(self):
        fields = dcs._album_level_mb_fields(RELEASE)
        assert str(fields.get("release_year")) == "2024", (
            "release_year is THIS edition's year; the original year (1994) "
            "belongs in ``originalyear``"
        )
        assert str(fields.get("originalyear")) == "1994"

    def test_the_specific_release_title_wins_over_the_generic_one(self):
        """``specific_release_title`` is the edition; ``release_title`` is its alias."""
        release = dict(RELEASE)
        release["specific_release_title"] = "The Edition"
        release["release_title"] = "Something Else"
        assert dcs._album_level_mb_fields(release)["release_title"] == "The Edition"

    def test_catalog_number_and_release_date_are_still_carried(self):
        fields = dcs._album_level_mb_fields(RELEASE)
        assert fields.get("catalognumber") == "CAT-12345"
        assert fields.get("releasedate") == "2024-05-17"

    def test_new_columns_are_real_tracks_columns(self):
        """They build a SELECT, so a non-column would break the sibling query."""
        import re
        from pathlib import Path

        schema = Path("db/schema.py").read_text(encoding="utf-8")
        for column in ("release_title", "release_year"):
            assert f'"{column}"' in schema, f"{column} is not a tracks column"
        assert "release_title" in dcs._ALBUM_LEVEL_COLUMNS
        assert "release_year" in dcs._ALBUM_LEVEL_COLUMNS


class TestAlbumLevelGenres:
    """The release GROUP's and RELEASE's genres must be offered to the album."""

    def test_release_group_and_release_genres_are_combined(self):
        genres = dcs._album_level_genres(RELEASE)
        assert "Rock" in genres and "Metal" in genres and "Thrash Metal" in genres

    def test_release_group_genres_lead(self):
        genres = dcs._album_level_genres(RELEASE)
        assert genres.split(", ")[0] == "Rock"

    def test_per_track_affiliation_markers_are_stripped(self):
        """An album-wide "Cover" would be stamped on every track of the album."""
        release = dict(RELEASE, release_group_genres="Rock, Cover", release_genres="Live")
        genres = dcs._album_level_genres(release)
        assert "Rock" in genres
        assert "Cover" not in genres
        assert "Live" not in genres

    def test_genres_are_deduplicated_case_insensitively(self):
        release = dict(RELEASE, release_group_genres="Metal", release_genres="metal, Thrash")
        names = [g for g in dcs._album_level_genres(release).split(", ")]
        assert [g.casefold() for g in names] == ["metal", "thrash"]

    def test_no_genres_gives_an_empty_string(self):
        release = dict(RELEASE, release_group_genres="", release_genres="")
        assert dcs._album_level_genres(release) == ""

    def test_album_genres_travel_with_the_album_fields(self):
        fields = dcs._album_level_mb_fields(RELEASE)
        assert fields.get("musicbrainz_album_genres")

    def test_the_album_genres_key_is_not_a_tracks_column(self):
        """It must NOT be in ``_ALBUM_LEVEL_COLUMNS`` — that list drives a SELECT."""
        assert "musicbrainz_album_genres" not in dcs._ALBUM_LEVEL_COLUMNS


class TestImportAppliesThem:
    """``_apply_stored_metadata`` must reach album-page parity."""

    @pytest.fixture(autouse=True)
    def _capture(self, monkeypatch):
        from services.metadata import tag_file_service as tfs
        from db.repositories import tracks as tracks_repo

        self.tag_writes: list[dict] = []
        self.db_writes: list[dict] = []

        # Both collaborators are imported INSIDE the function body, so the
        # SOURCE modules must be patched.
        monkeypatch.setattr(
            tfs, "update_file_metadata",
            lambda path, meta: (self.tag_writes.append(dict(meta)), True)[1],
        )
        monkeypatch.setattr(
            tracks_repo, "insert_or_update_track",
            lambda tid, payload, *a, **k: self.db_writes.append(dict(payload)),
        )
        monkeypatch.setattr(
            dcs, "_resolve_track_id_for_import", lambda **kw: "t-1"
        )
        monkeypatch.setattr(dcs, "_fetch_import_art", lambda item: None)

    def _run(self, **overrides):
        item = {
            "id": 42,
            "artist": "Band Alpha",
            "album_artist": "Band Alpha",
            "album": "The Album Name",
            "title": "Song One",
            "track_number": "1",
            "disc_number": 1,
            "year": "2024",
            "release_year": 2024,
            "recording_mbid": "rec-a",
            "release_mbid": "rel-1",
            "metadata": json.dumps(
                {
                    "musicbrainz_genres": "Metal",
                    "album_metadata": dcs._album_level_mb_fields(RELEASE),
                }
            ),
        }
        item.update(overrides)
        dcs._apply_stored_metadata(dict(item), "/music/x.flac")
        return self.db_writes[-1] if self.db_writes else {}

    def test_release_name_reaches_the_database(self):
        payload = self._run()
        assert payload.get("release_title") == "The Specific Edition Name"

    def test_release_year_reaches_the_database(self):
        payload = self._run()
        assert str(payload.get("release_year")) == "2024"

    def test_release_date_reaches_the_file(self):
        self._run()
        assert self.tag_writes, "no tag write happened"
        assert self.tag_writes[-1].get("releasedate") == "2024-05-17"

    def test_album_genres_fill_a_track_with_no_genres_of_its_own(self):
        """No per-track genre evidence → the album's list fills ``genres``."""
        payload = self._run(
            metadata=json.dumps(
                {"album_metadata": dcs._album_level_mb_fields(RELEASE)}
            )
        )
        assert payload.get("genres"), "the album genres did not fill genres"

    def test_a_track_with_its_own_genres_keeps_them(self):
        payload = self._run()
        assert "genres" not in payload, (
            "a track with its own MusicBrainz genres must not be overwritten "
            "by the album's list"
        )


class TestTheQueueRowCarriesThem:
    """The release fields must survive queueing — the import reads them there."""

    @pytest.fixture(autouse=True)
    def _queue_env(self, monkeypatch):
        from sqlalchemy import event, text

        from db.engine import db_session, get_engine
        from db.models import Track

        with db_session() as session:
            Track.__table__.create(session.get_bind(), checkfirst=True)
            session.execute(text("CREATE TABLE IF NOT EXISTS download_queue "
                                 "(id INTEGER PRIMARY KEY AUTOINCREMENT)"))
            for name, sql_type in (
                ("artist", "TEXT"), ("album", "TEXT"), ("title", "TEXT"),
                ("search_query", "TEXT"), ("source", "TEXT"), ("status", "TEXT"),
                ("release_id", "TEXT"), ("import_group", "TEXT"),
                ("album_artist", "TEXT"), ("recording_mbid", "TEXT"),
                ("musicbrainz_releasegroupid", "TEXT"), ("duration", "REAL"),
                ("year", "TEXT"), ("release_year", "INTEGER"), ("metadata", "TEXT"),
                ("track_number", "TEXT"), ("disc_number", "TEXT"),
                ("created_at", "TIMESTAMP"), ("updated_at", "TIMESTAMP"),
            ):
                existing = {
                    str(r[1]) for r in session.execute(
                        text("PRAGMA table_info(download_queue)")
                    ).fetchall()
                }
                if name in existing:
                    continue
                try:
                    session.execute(
                        text(f"ALTER TABLE download_queue ADD COLUMN {name} {sql_type}")
                    )
                    session.commit()
                except Exception:
                    pass
            session.execute(text("DELETE FROM download_queue"))
            session.commit()

        from services.queue import queue_processing_service as qps
        monkeypatch.setattr(qps, "find_library_track", lambda **kw: None)

        self.inserted: list[dict] = []

        def _before(conn, cursor, statement, params, context, executemany):
            if "INSERT INTO download_queue" in " ".join(statement.split()):
                self.inserted.append(params)

        engine = get_engine()
        event.listen(engine, "before_cursor_execute", _before)
        yield
        event.remove(engine, "before_cursor_execute", _before)

    def _queue(self, **kwargs):
        from services.queue import queue_processing_service as qps
        return qps.add_release_tracks_to_queue_detailed(
            "rel-1",
            [{"title": "Song One", "artist": "Band Alpha", "track_number": 1,
              "disc_number": 1, "recording_mbid": "rec-a"}],
            "Band Alpha", "The Album Name",
            album_artist="Band Alpha", year=2024, **kwargs,
        )

    def _stored(self) -> dict:
        params = self.inserted[0]
        raw = params.get("metadata") if isinstance(params, dict) else next(
            (v for v in (params or ()) if isinstance(v, str) and v.strip().startswith("{")),
            None,
        )
        if not raw:
            return {}
        return json.loads(raw) if isinstance(raw, str) else dict(raw)

    def test_queueing_persists_the_release_name_and_year(self):
        result = self._queue(album_metadata=dcs._album_level_mb_fields(RELEASE))
        assert result.get("queued") is True, result
        stored = self._stored().get("album_metadata") or {}
        assert stored.get("release_title") == "The Specific Edition Name"
        assert str(stored.get("release_year")) == "2024"

    def test_queueing_persists_the_album_genres(self):
        self._queue(album_metadata=dcs._album_level_mb_fields(RELEASE))
        stored = self._stored().get("album_metadata") or {}
        assert stored.get("musicbrainz_album_genres"), (
            "the album genres must travel to the queue row or the import has "
            "nothing to fill the tracks with"
        )

