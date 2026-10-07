"""The MBIDs Subsonic never sends must be savable when enrichment finds them.

Reported (two linked defects):

> 1. **The Subsonic API Blindspot:** Navidrome reads every raw MusicBrainz
>    tag, but the Subsonic API only transmits the Release ID
>    (`musicBrainzId`) — it omits the Release Group ID and Artist ID, so
>    `navidrome_import.py` saves those fields blank.
> 2. **The Case-Sensitive Save Bug:** the background Enrichment stage finds
>    the missing IDs on MusicBrainz, but the `UPDATE` fails on a casing
>    mismatch (e.g. `WHERE artist = 'Afi'` against a row imported as `AFI`)
>    → `rows_updated=0` → the fields stay blank for ever.

Half 1 is an API limitation the import already respects: the empty MBID keys
are in ``MBID_IDENTITY_FIELDS``, so an import OMITS them instead of wiping
stored values — a *new* track simply starts blank and enrichment is the only
thing that can fill it. That makes half 2 the operative bug: every
artist/album-scoped read and write in the backfill chain now matches
case-insensitively, the way the rest of the codebase already scopes these
queries.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

RG_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RELEASE_UUID = "11111111-2222-3333-4444-555555555555"
ARTIST_UUID = "99999999-8888-7777-6666-555555555555"


@pytest.fixture(autouse=True)
def _empty_tracks(db_session):
    """Fixed ids + ON CONFLICT DO NOTHING = silently seeing another test's rows."""
    db_session.execute(text("DELETE FROM tracks"))
    db_session.commit()


def _seed(db_session, track_id: str, *, artist: str = "AFI", album: str = "Sing The Sorrow", **cols):
    db_session.execute(
        text("""
            INSERT INTO tracks (id, artist, album, album_artist, file_path)
            VALUES (:id, :artist, :album, :album_artist, :file_path)
            ON CONFLICT DO NOTHING
        """),
        {
            "id": track_id,
            "artist": artist,
            "album": album,
            "album_artist": artist,
            "file_path": f"/music/{artist}/{album}/{track_id}.flac",
        },
    )
    if cols:
        assignments = ", ".join(f"{col} = :{col}" for col in cols)
        db_session.execute(
            text(f"UPDATE tracks SET {assignments} WHERE id = :id"),
            {"id": track_id, **cols},
        )
    db_session.commit()


def _cols(db_session, track_id: str, *cols: str) -> dict:
    row = db_session.execute(
        text(f"SELECT {', '.join(cols)} FROM tracks WHERE id = :id"),
        {"id": track_id},
    ).fetchone()
    return {col: row[i] for i, col in enumerate(cols)}


# ---------------------------------------------------------------------------
# 1. The import must never BLANK what the API cannot resend
# ---------------------------------------------------------------------------

class TestTheImportPreservesIdsTheApiNeverSends:
    def test_the_mbids_are_in_the_preserve_set(self):
        from services.scanning.payload_builder import (
            MBID_IDENTITY_FIELDS,
            PRESERVE_WHEN_EMPTY_FIELDS,
        )

        for field in (
            "musicbrainz_releasegroupid",
            "musicbrainz_albumartistid",
            "musicbrainz_artistid",
            "musicbrainz_albumid",
        ):
            assert field in MBID_IDENTITY_FIELDS, field
            assert field in PRESERVE_WHEN_EMPTY_FIELDS, (
                f"{field} would be wiped by the next import's empty value"
            )


# ---------------------------------------------------------------------------
# 2. The enrichment backfill writes despite casing
# ---------------------------------------------------------------------------

class TestTheReleaseGroupBackfillSurvivesCasing:
    def test_the_release_group_id_reaches_the_rows(self, db_session):
        """THE reported failure: enrichment found the RG id and saved 0 rows."""
        from services.popularity.stages import album_stage

        # Seeded WITH a release id so the later release-resolution block
        # short-circuits (``_needs_release_mbid`` → False) — no MusicBrainz
        # call is wanted here, only the release-group persist under test.
        _seed(db_session, "mbid-rg-1", musicbrainz_album_mbid=RELEASE_UUID)
        _seed(db_session, "mbid-rg-2", musicbrainz_album_mbid=RELEASE_UUID)

        # Rows say AFI / Sing The Sorrow; the scan context says Afi / …
        album_stage._persist_album_type_to_tracks(
            artist="Afi",
            album="sing the sorrow",
            tracks=[],
            album_type="album",
            release_group_mbid=RG_UUID,
        )

        for tid in ("mbid-rg-1", "mbid-rg-2"):
            got = _cols(db_session, tid, "musicbrainz_releasegroupid")
            assert got["musicbrainz_releasegroupid"] == RG_UUID, (
                f"{tid}: the release-group id was not saved despite matching case-insensitively"
            )

    def test_the_release_id_backfill_survives_casing(self, db_session, monkeypatch):
        """Same block, one step later: the resolved RELEASE id must land too."""
        from services.popularity.stages import album_stage

        _seed(db_session, "mbid-rel-1")
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.resolve_release_id",
            lambda *a, **k: RELEASE_UUID,
        )

        album_stage._persist_album_type_to_tracks(
            artist="Afi",
            album="sing the sorrow",
            tracks=[],
            album_type="album",
            release_group_mbid=RG_UUID,
        )

        got = _cols(db_session, "mbid-rel-1", "musicbrainz_album_mbid", "musicbrainz_albumid")
        assert got["musicbrainz_album_mbid"] == RELEASE_UUID
        assert got["musicbrainz_albumid"] == RELEASE_UUID

    def test_the_release_gate_reads_despite_casing(self, db_session):
        """A miss here made the stage believe every track already HAD a
        release id, so resolution was skipped entirely."""
        from services.popularity.stages import album_stage

        _seed(db_session, "mbid-gate-1")

        assert album_stage._needs_release_mbid("Afi", "sing the sorrow") is True
        # CONTROL — exact casing still answers.
        assert album_stage._needs_release_mbid("AFI", "Sing The Sorrow") is True

        db_session.execute(
            text("UPDATE tracks SET musicbrainz_album_mbid = :m WHERE id = 'mbid-gate-1'"),
            {"m": RELEASE_UUID},
        )
        db_session.commit()
        assert album_stage._needs_release_mbid("Afi", "sing the sorrow") is False


class TestTheArtistMbIdBackfillSurvivesCasing:
    def test_the_found_artist_mb_id_is_written(self, db_session, monkeypatch):
        """THE reported Artist ID half: the lookup found the id, then its own
        SELECT matched nothing and no row was updated."""
        from services.enrichment import musicbrainz_persistence_service as mps

        class _FakeMB:
            def search_artists(self, query, limit=10):
                return [{"id": ARTIST_UUID, "name": "AFI", "type": "group"}]

        monkeypatch.setattr(mps, "get_shared_mb_client", lambda: _FakeMB())

        _seed(db_session, "mbid-art-1")                                  # exact-mismatch
        _seed(db_session, "mbid-art-2", artist="AFI feat. Someone")      # LIKE branch
        _seed(db_session, "mbid-art-3", musicbrainz_artistid=ARTIST_UUID)  # already valid

        found = mps.lookup_and_save_artist_mbid("Afi")

        assert found == ARTIST_UUID
        for tid in ("mbid-art-1", "mbid-art-2"):
            got = _cols(db_session, tid, "musicbrainz_artistid")
            assert got["musicbrainz_artistid"] == ARTIST_UUID, f"{tid} not updated"
        kept = _cols(db_session, "mbid-art-3", "musicbrainz_artistid")
        assert kept["musicbrainz_artistid"] == ARTIST_UUID


class TestTheManualMbIdLinksSurviveCasing:
    def test_update_album_mbid_fields_returns_a_real_rowcount(self, db_session):
        from db.repositories.metadata import update_album_mbid_fields

        _seed(db_session, "mbid-link-1")

        rows = update_album_mbid_fields(
            None, "Afi", "sing the sorrow", RELEASE_UUID, RG_UUID, None,
        )
        assert rows == 1, "rows_updated=0 — the reported symptom"
        got = _cols(db_session, "mbid-link-1", "musicbrainz_album_mbid", "musicbrainz_releasegroupid")
        assert got["musicbrainz_album_mbid"] == RELEASE_UUID
        assert got["musicbrainz_releasegroupid"] == RG_UUID

    def test_exact_casing_still_works(self, db_session):
        """CONTROL."""
        from db.repositories.metadata import update_album_mbid_fields

        _seed(db_session, "mbid-link-2")
        rows = update_album_mbid_fields(
            None, "AFI", "Sing The Sorrow", RELEASE_UUID, None, None,
        )
        assert rows == 1

    def test_update_album_ids_writes_despite_casing(self, db_session):
        from services.metadata.album_service import update_album_ids

        _seed(db_session, "mbid-ids-1")

        body, status = update_album_ids({
            "artist": "Afi",
            "album": "sing the sorrow",
            "musicbrainz_release_group_id": RG_UUID,
        })
        assert status == 200
        assert body["rows_updated"] == 1, body
        got = _cols(db_session, "mbid-ids-1", "musicbrainz_releasegroupid")
        assert got["musicbrainz_releasegroupid"] == RG_UUID

    def test_the_companion_file_path_read_matches_too(self, db_session):
        """The DB write and the file-tag pass must see the SAME rows, or the
        database updates while the files stay blank."""
        from services.metadata.album_service import _album_file_paths

        _seed(db_session, "mbid-files-1")

        paths = _album_file_paths("Afi", "sing the sorrow")
        assert any(p.endswith("mbid-files-1.flac") for p in paths), paths


# ---------------------------------------------------------------------------
# 3. Source guards — the pattern must not regress back to an exact match
# ---------------------------------------------------------------------------

class TestTheBackfillQueriesScopeCaseInsensitively:
    @pytest.mark.parametrize(
        ("rel", "must_contain"),
        [
            (
                "services/enrichment/musicbrainz_persistence_service.py",
                ["WHERE LOWER(artist) = LOWER(:artist)", "LOWER(artist) LIKE LOWER(:pattern)"],
            ),
            (
                "services/popularity/stages/album_stage.py",
                [
                    "SET musicbrainz_releasegroupid = :release_group_mbid\n"
                    "                        WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)",
                ],
            ),
            (
                "services/metadata/artist_service.py",
                ["WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"],
            ),
        ],
    )
    def test_the_where_clause_is_lowercased(self, rel: str, must_contain: list[str]):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for snippet in must_contain:
            assert snippet in source, f"{rel}: expected `{snippet.splitlines()[0]}` to scope case-insensitively"

    def test_update_album_mbid_fields_lowercases_its_own_scope(self):
        """Scoped to the FUNCTION — other helpers in metadata.py already
        used the lowercase pattern, so a whole-file check passed at HEAD."""
        source = (REPO_ROOT / "db" / "repositories" / "metadata.py").read_text(encoding="utf-8")
        fn = source[source.index("def update_album_mbid_fields"):]
        fn = fn[: fn.index("def update_album_discogs_fields")]
        assert "WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)" in fn
        assert "AND LOWER(album) = LOWER(:album)" in fn

    def test_artist_service_lowercases_both_of_its_reads(self):
        source = (REPO_ROOT / "services" / "metadata" / "artist_service.py").read_text(encoding="utf-8")
        assert source.count("WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)") == 2, (
            "the guard read and the file-tag read must BOTH match case-insensitively"
        )
