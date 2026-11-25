"""A release-id correction from the album page must reach EVERY row.

REPORT

> "This is showing in Popularr under Miroh, but I save the metadata and it still
>  keeps importing into Navidrome as SKZ2020. It seems that some of the fields
>  aren't saving from the album page save"

WHAT IS ACTUALLY HAPPENING

The file carries `musicbrainz_albumid = 5a0c3466…`, which is **SKZ2020**
(2 discs, 27 tracks, Japan, barcode 4547366444490) — verified against
MusicBrainz. Its album NAME, barcode and catalogue number all say MIROH, so the
identity is half-corrected: name fixed, release id not.

The album page cannot fix it as things stand:

1. ``album_detail`` scopes its rows to ONE release group, so a folder whose
   files disagree about their release group renders the majority's rows and
   silently omits the others. The odd file is never in the form.
2. The save loops only those scoped rows, so the corrected id was written to
   the rows on screen and nowhere else.
3. Navidrome re-reads the FILE, which still held the old id, and re-imported
   the album under SKZ2020.

A release id describes the ALBUM, so it must be fanned out album-wide.
"""

from __future__ import annotations

import pathlib

import pytest

from services.metadata import album_service

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"


def apply_album_wide_ids(**kwargs):
    """The writer under test, fetched lazily.

    A module-level import would make this file ERROR during collection against
    code that has not been fixed yet, hiding the behaviour it guards.
    """
    writer = getattr(album_service, "apply_album_wide_ids", None)
    assert writer is not None, "album_service has no album-wide id writer"
    return writer(**kwargs)


class TestTheAlbumWideFanOut:
    def test_it_writes_both_release_id_columns(self, monkeypatch) -> None:
        """The page displays ``_mbid``; other readers use ``albumid``."""
        captured: list[dict] = []
        monkeypatch.setattr(
            album_service,
            "update_album_ids",
            lambda payload, **k: (
                captured.append(payload)
                or ({"success": True, "rows_updated": 3, "files_updated": 3}, 200)
            ),
        )
        out = apply_album_wide_ids(
            artist="Stray Kids",
            album="Clé 1 : MIROH",
            album_mbid="2e00dae4-0000-0000-0000-000000000000",
        )
        assert out["rows_updated"] == 3
        assert captured[0]["musicbrainz_release_id"] == (
            "2e00dae4-0000-0000-0000-000000000000"
        )

    def test_it_is_album_scoped_not_page_scoped(self, monkeypatch) -> None:
        """It takes only artist+album, so it cannot inherit the page's row list."""
        monkeypatch.setattr(
            album_service,
            "update_album_ids",
            lambda payload, **k: ({"success": True, "rows_updated": 1, "files_updated": 1}, 200),
        )
        out = apply_album_wide_ids(artist="A", album="B", release_group_mbid="rg")
        assert out == {"rows_updated": 1, "files_updated": 1}

    def test_an_absent_field_delegates_nothing(self, monkeypatch) -> None:
        """None means "not part of this save" — never "clear it"."""
        called: list[dict] = []
        monkeypatch.setattr(
            album_service, "update_album_ids", lambda p, **k: (called.append(p), ({}, 200))[1]
        )
        assert apply_album_wide_ids(artist="A", album="B") == {
            "rows_updated": 0, "files_updated": 0
        }
        assert not called

    def test_an_empty_artist_or_album_is_a_noop(self, monkeypatch) -> None:
        monkeypatch.setattr(
            album_service, "update_album_ids", lambda p, **k: ({"rows_updated": 9}, 200)
        )
        assert apply_album_wide_ids(artist="", album="B", album_mbid="x")[
            "rows_updated"
        ] == 0

    def test_clearing_is_distinct_from_absent(self, monkeypatch) -> None:
        """``clear_album_mbid`` removes the id; ``album_mbid=None`` ignores it."""
        src = pathlib.Path(album_service.__file__).read_text(encoding="utf-8")
        start = src.index("def apply_album_wide_ids")
        body = src[start:src.index("\ndef _clear_album_id_file_tags")]
        assert "clear_album_mbid" in body
        assert "musicbrainz_album_mbid = ''" in body


class TestBothReleaseIdSpellings:
    """The page reads ``_mbid``; ``album_identity`` prefers ``albumid``.

    ⚠️ Writing only the page's spelling left the other stale on every row the
    page did not display. The FILE was corrected (so Navidrome merged the
    album) while Popularr kept grouping the row by its old
    ``musicbrainz_albumid`` — the reported "Korn - Untouchables is STILL
    split" after a save that appeared to work.
    """

    def _seed(self, *, mbid: str, albumid: str) -> None:
        from sqlalchemy import text

        from db.engine import db_session

        with db_session() as session:
            session.execute(text("DELETE FROM tracks"))
            session.execute(
                text(
                    "INSERT INTO tracks (id, title, artist, album_artist, album, "
                    "file_path, musicbrainz_album_mbid, musicbrainz_albumid) "
                    "VALUES ('t1', 'Here to Stay', 'Korn', 'Korn', 'Untouchables', "
                    "'/music/korn/t1.mp3', :mbid, :albumid)"
                ),
                {"mbid": mbid, "albumid": albumid},
            )
            session.commit()

    def _stored_ids(self) -> tuple[str, str]:
        from sqlalchemy import text

        from db.engine import db_session

        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT musicbrainz_album_mbid, musicbrainz_albumid "
                    "FROM tracks WHERE id = 't1'"
                )
            ).fetchone()
        return (str(row[0] or ""), str(row[1] or ""))

    def _stub_file_writer(self, monkeypatch) -> list[tuple[str, dict]]:
        import services.metadata.tag_file_service as tag_file_service

        writes: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            tag_file_service,
            "resolve_music_file_path",
            lambda value: "/music/korn/t1.mp3",
        )
        monkeypatch.setattr(
            tag_file_service,
            "update_file_tags",
            lambda path, tags, **kwargs: writes.append((path, tags)) or True,
        )
        return writes

    def test_the_db_update_writes_both_columns(self, monkeypatch) -> None:
        self._seed(mbid="old-release", albumid="old-release")
        writes = self._stub_file_writer(monkeypatch)

        album_service.update_album_ids(
            {
                "artist": "Korn",
                "album": "Untouchables",
                "musicbrainz_release_id": "new-release",
            }
        )

        assert self._stored_ids() == ("new-release", "new-release"), (
            "one release-id spelling was left stale; album_identity reads that "
            "one and re-splits the album after the save"
        )
        assert writes and writes[0][1]["musicbrainz_albumid"] == "new-release"

    def test_one_correct_spelling_does_not_skip_the_file_write(self, monkeypatch) -> None:
        """A half-corrected row must still be rewritten on the file.

        This is the state the old writer produced: ``_mbid`` already the target,
        ``albumid`` still the old release. Comparing only ``_mbid`` made the
        file-write guard say "already correct" and skip the one file that
        needed fixing.
        """
        self._seed(mbid="new-release", albumid="old-release")
        writes = self._stub_file_writer(monkeypatch)

        album_service.update_album_ids(
            {
                "artist": "Korn",
                "album": "Untouchables",
                "musicbrainz_release_id": "new-release",
            }
        )

        assert writes, (
            "the file was skipped because only the page's release-id spelling "
            "was compared"
        )
        assert self._stored_ids() == ("new-release", "new-release")


class TestThePageSaveFansOutAlbumWide:
    def test_the_save_calls_the_album_wide_writer(self) -> None:
        src = UI_ROUTES.read_text(encoding="utf-8")
        assert "apply_album_wide_ids" in src, (
            "the album-page save still only writes the rows it displayed, so a "
            "file carrying a stale release id keeps it"
        )

    def test_it_runs_after_the_page_rows_are_committed(self) -> None:
        """The on-screen rows must be saved before the wider fan-out."""
        src = UI_ROUTES.read_text(encoding="utf-8")
        persist_at = src.index("_persist_album_track_payloads(_db_payloads)")
        wide_at = src.index("apply_album_wide_ids(")
        assert persist_at < wide_at

    def test_it_is_gated_on_the_field_being_submitted(self) -> None:
        """A save that never carried the box must not rewrite the identity."""
        src = UI_ROUTES.read_text(encoding="utf-8")
        start = src.index("── Phase 2c: the RELEASE IDENTITY is album-wide")
        block = src[start:start + 2400]
        assert "_album_mbid_field" in block
        assert "_album_rg_field" in block

    def test_a_fan_out_failure_cannot_fail_the_save(self) -> None:
        src = UI_ROUTES.read_text(encoding="utf-8")
        start = src.index("── Phase 2c: the RELEASE IDENTITY is album-wide")
        # To the end of the fan-out block, wherever the except lands.
        end = src.index("── Do-not-search-album-release flag", start)
        block = src[start:end]
        assert "except Exception" in block, (
            "a fan-out failure must not fail a save whose page rows committed"
        )
