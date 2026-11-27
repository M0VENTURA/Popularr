"""Artist identity must survive a collaborative / joined / Unicode-spelled credit.

REPORTED (user's diagnosis, confirmed against the code): "RATATATA" by BABYMETAL &
Electric Callboy is re-imported as new on every scan.

CAUSE

The Navidrome prefetch — the query that decides which existing rows the import can
see — matched ONE artist identity:

    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)

Navidrome stores the album artist of a collaboration as the JOINED credit
``"BABYMETAL & Electric Callboy"``, which never equals the scanned view
``"BABYMETAL"``. So the prefetch returned 0 rows, ``existing_album_tracks`` was
empty, and EVERY skip gate (album diff, fast-skip, fingerprint) was bypassed: the
album was rewritten in full on every scan, with ``is_new_track=True``.

The row is NOT duplicated (``tracks.id`` is the stable Navidrome song id and the
write is ``ON CONFLICT (id)``) — it is the same row treated as new forever. The
fix is to make the prefetch find the row at all.
"""

from __future__ import annotations

import pytest

from services.scanning import navidrome_import as ni


class TestCreditContainment:
    @pytest.mark.parametrize("credit, name", [
        ("BABYMETAL & Electric Callboy", "BABYMETAL"),
        ("Electric Callboy & BABYMETAL", "BABYMETAL"),
        ("BABYMETAL; Electric Callboy", "Electric Callboy"),
        ("BABYMETAL • Electric Callboy", "BABYMETAL"),
        ("BABYMETAL feat. Electric Callboy", "BABYMETAL"),
        ("BABYMETAL", "BABYMETAL"),
    ])
    def test_a_joined_credit_contains_its_names(self, credit, name):
        assert ni._credit_contains_name(credit, name) is True

    @pytest.mark.parametrize("credit, name", [
        ("VAMPIRE", "VA"),              # substring is NOT a name match
        ("Bad Omens", "Bad"),
        ("Bad Omens", "Bad Omens"),     # exact still works
    ])
    def test_a_substring_is_not_a_name_match(self, credit, name):
        expected = credit == name
        assert ni._credit_contains_name(credit, name) is expected

    def test_an_empty_credit_never_matches(self):
        assert ni._credit_contains_name("", "BABYMETAL") is False

    def test_a_unicode_hyphen_credit_matches_its_ascii_spelling(self):
        """MusicBrainz hyphenates with U+2010; the view uses ASCII."""
        assert ni._credit_contains_name("blink\u2010182", "blink-182") is True
        assert ni._credit_contains_name("blink-182", "blink\u2010182") is True


class TestThePrefetchSeesTheCollaboration:
    """Drives the REAL prefetch SQL against a seeded tracks table."""

    @pytest.fixture()
    def seeded(self, monkeypatch):
        from sqlalchemy import text
        from db.engine import db_session

        import uuid

        tag = uuid.uuid4().hex[:8]
        ids = {
            "collab": f"collab-1-{tag}",
            "solo": f"solo-1-{tag}",
            "va": f"va-1-{tag}",
            "hyphen": f"hyphen-1-{tag}",
            "unrelated": f"unrelated-1-{tag}",
        }
        self.ids = ids
        with db_session() as session:
            # Scoped cleanup: never DELETE the whole shared table (other tests
            # in this suite share the in-memory DB).
            session.execute(
                text("DELETE FROM tracks WHERE id LIKE :p"), {"p": f"%-{tag}"}
            )
            session.execute(text(
                "INSERT INTO tracks (id, artist, album_artist, album, title) VALUES "
                "(:collab, 'BABYMETAL & Electric Callboy', "
                "'BABYMETAL & Electric Callboy', 'RATATATA', 'RATATATA'),"
                "(:solo, 'BABYMETAL', 'BABYMETAL', 'Metal Galaxy', 'Karate'),"
                "(:va, 'The Black Dahlia Murder', 'Various Artists', "
                "'MTV2 Headbangers Ball, Volume 2', 'Funeral Thirst'),"
                "(:hyphen, 'blink\u2010182', 'blink\u2010182', 'Enema', 'Dammit'),"
                "(:unrelated, 'VAMPIRE', 'VAMPIRE', 'X', 'Y')"
            ), ids)
            session.commit()
        yield

    def _prefetch(self, artist):
        state = ni.prefetch_artist_state(canonical_artist_name=artist)
        return state["existing_track_ids"]

    def test_a_collaboration_row_is_found_under_one_member(self, seeded):
        assert self.ids["collab"] in self._prefetch("BABYMETAL")

    def test_a_compilation_row_is_found_under_its_track_artist(self, seeded):
        assert self.ids["va"] in self._prefetch("The Black Dahlia Murder")

    def test_a_unicode_hyphen_row_is_found_by_its_ascii_name(self, seeded):
        assert self.ids["hyphen"] in self._prefetch("blink-182")

    def test_unrelated_rows_are_not_pulled_in(self, seeded):
        """CONTROL: a broad LIKE must not widen the artist's scope."""
        ids = self._prefetch("BABYMETAL")
        assert self.ids["unrelated"] not in ids
        assert self.ids["va"] not in ids

    def test_an_ordinary_artist_still_sees_its_own_rows(self, seeded):
        assert self.ids["solo"] in self._prefetch("BABYMETAL")


class TestTheChangeDiffSeesItToo:
    """``compute_artist_album_diff`` shares the clause; it must not say 'changed'."""

    def test_a_collaboration_album_is_not_reported_as_changed(self, monkeypatch):
        from sqlalchemy import text
        from db.engine import db_session
        import uuid

        tag = uuid.uuid4().hex[:8]
        artist = f"Collab Artist {tag}"
        album = f"RATATATA {tag}"
        with db_session() as session:
            session.execute(
                text("DELETE FROM tracks WHERE id LIKE :p"), {"p": f"%-{tag}"}
            )
            session.execute(text(
                "INSERT INTO tracks (id, artist, album_artist, album, title) "
                "VALUES (:id, :credit, :credit, :album, 'RATATATA')"
            ), {"id": f"c1-{tag}", "credit": f"{artist} & Electric Callboy", "album": album})
            session.commit()

        nav_albums = [{"name": album, "songCount": 1}]
        skip, changed, _removed = ni.compute_artist_album_diff(artist, nav_albums)
        assert changed == set(), (
            "the album exists in the DB under the joined credit, so the diff "
            f"must not call it changed; got {changed}"
        )
        assert skip is True
