"""Album-level inheritance must be scoped to the release the queue row names.

The reported problem
--------------------
> When metadata is added to a track that is downloaded from the queue, it
> doesn't always match the metadata of the release that was added to the queue.
> Sometimes the album year or the release mbid is different.

``_resolve_album_level_metadata`` fills album-scoped fields (release group,
barcode, catalogue number, original year, release date …) from, in order: the
queue row's own columns, its stored ``album_metadata`` payload, a library
SIBLING, then MusicBrainz.

The sibling step matched on artist+album NAME with no release constraint, and
preferred a sibling that carried a release group:

    WHERE LOWER(...artist...) = LOWER(:artist)
      AND LOWER(...album...)  = LOWER(:album)
    ORDER BY CASE WHEN COALESCE(musicbrainz_releasegroupid,'') <> ''
                  THEN 0 ELSE 1 END, id

so a library holding the SAME album name from a DIFFERENT release — an original
and a remaster, or an album previously imported from another edition — donated
its release group, barcode, catalogue number and year to the incoming track.
Because inheritance runs BEFORE the MusicBrainz refresh and fills every column
it can, the release actually queued was then consulted only for the leftovers:
the wrong identity was locked in and the right one could never win.

The scoping now allows a sibling only when it is the SAME release, or shares the
SAME release group. A row naming NO release keeps the name-only behaviour —
that is the case the fallback exists for, and the only case where a name match
is the best evidence available.

Why these tests cannot both pass vacuously
------------------------------------------
``test_same_release_sibling_is_inherited`` proves the sibling query FINDS a
sibling when it should — so a broken query (a mistyped column, a predicate that
never matches) fails there. ``test_wrong_release_sibling_is_not_inherited``
proves it REFUSES one when it should. A query that silently raised and fell
through to MusicBrainz would pass the second and fail the first, which is what
makes the pair load-bearing rather than decorative.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from services.downloads import download_completion_service as dcs

RELEASE_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"  # what the queue row names
GROUP_A = "aaaa1111-1111-4111-8111-aaaa11111111"
RELEASE_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"  # a DIFFERENT release in the library
GROUP_B = "bbbb2222-2222-4222-8222-bbbb22222222"

MB_RELEASE_A = {
    "release_year": 1994,
    "releasedate": "1994-08-22",
    "original_year": 1994,
    "original_date": "1994-08-22",
    "release_group_mbid": GROUP_A,
    "barcode": "A-BARCODE",
    "recordlabel": "Go! Beat",
    "catalognumber": "A-CAT",
    "media": "CD",
    "disctotal": 1,
    "album_type": "Album",
    "status": "Official",
    "releasecountry": "GB",
    "album_artist_mbid": "AAAA1111-1111-4111-8111-AAAA11111111",
}

#: A same-named album from a DIFFERENT release, already in the library.
SIBLING_B = {
    "id": "t-b",
    "artist": "Portishead",
    "album_artist": "Portishead",
    "album": "Dummy",
    "file_path": "/music/Portishead/Dummy/01 Mysterons.mp3",
    "musicbrainz_album_mbid": RELEASE_B,
    "musicbrainz_releasegroupid": GROUP_B,
    "releasedate": "1997-08-25",
    "originalyear": "1997",
    "originaldate": "1997-08-25",
    "barcode": "B-BARCODE",
    "recordlabel": "London",
    "catalognumber": "B-CAT",
    "media": "CD",
    "disctotal": "2",
    "release_title": "Dummy (Remastered)",
    "release_year": "1997",
    "musicbrainz_albumtype": "Album",
    "musicbrainz_albumstatus": "Official",
    "releasecountry": "GB",
    "musicbrainz_albumartistid": "BBBB2222-2222-4222-8222-BBBB22222222",
}

#: A sibling that IS the release the row names.
SIBLING_A = dict(
    SIBLING_B,
    id="t-a",
    file_path="/music/Portishead/Dummy/05 Glory Box.mp3",
    musicbrainz_album_mbid=RELEASE_A,
    musicbrainz_releasegroupid=GROUP_A,
    releasedate="1994-08-22",
    originalyear="1994",
    originaldate="1994-08-22",
    barcode="A-BARCODE",
    catalognumber="A-CAT",
    disctotal="1",
    release_title="Dummy",
    release_year="1994",
)


@pytest.fixture
def library(tmp_path: Path, monkeypatch):
    """A real SQLite ``tracks`` table standing in for the library, plus a recorder.

    A FILE-BACKED database is used deliberately: ``db_session`` disposes the
    engine on any transient error, which erases an in-memory database, and this
    fixture must survive the code under test failing.
    """
    engine = create_engine(f"sqlite:///{(tmp_path / 'library.db').as_posix()}")

    columns = ["id", "artist", "album_artist", "album", "file_path", "musicbrainz_album_mbid"]
    columns += list(dcs._ALBUM_LEVEL_COLUMNS)
    ddl_cols = ", ".join(f"{c} TEXT" for c in columns)

    state: dict[str, object] = {"errors": [], "mb_calls": []}

    def build(siblings: list[dict]) -> None:
        with engine.begin() as conn:
            conn.execute(text(f"CREATE TABLE tracks ({ddl_cols}, PRIMARY KEY (id))"))
            for row in siblings:
                names = [c for c in columns if c in row]
                conn.execute(
                    text(
                        f"INSERT INTO tracks ({', '.join(names)}) "
                        f"VALUES ({', '.join(':' + n for n in names)})"
                    ),
                    {n: row[n] for n in names},
                )

    class _Session:
        def __init__(self) -> None:
            self._conn = engine.connect()

        def execute(self, stmt, params=None):
            return self._conn.execute(stmt, params or {})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._conn.close()
            return False

    class _FakeDbSession:
        def __call__(self):
            return _Session()

    monkeypatch.setattr(dcs, "db_session", _FakeDbSession())

    def _fake_fetch(release_mbid: str):
        state["mb_calls"].append(release_mbid)
        return dict(MB_RELEASE_A) if release_mbid == RELEASE_A else None

    import services.enrichment.musicbrainz_service as mbs

    monkeypatch.setattr(mbs, "fetch_musicbrainz_release_metadata", _fake_fetch)

    # Record any "inheritance failed" debug line. A scenario that only passes
    # through that except-branch has NOT exercised the scoping, and must not be
    # mistaken for a pass.
    class _RecLogger:
        def __getattr__(self, name):
            def _capture(event, **kw):
                if "inheritance" in str(event):
                    state["errors"].append(str(kw.get("error")))
            return _capture

    monkeypatch.setattr(dcs, "logger", _RecLogger())

    def resolve(item_extra: dict) -> dict:
        item = {
            "id": 1,
            "artist": "Portishead",
            "album_artist": "Portishead",
            "album": "Dummy",
            "title": "Mysterons",
            "metadata": "{}",
        }
        item.update(item_extra)
        return dcs._resolve_album_level_metadata(dict(item))

    # A namespace, not a class: a class body does not close over this fixture's
    # locals, so `staticmethod(build)` there would raise NameError.
    return SimpleNamespace(build=build, resolve=resolve, state=state)


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------

def test_wrong_release_sibling_is_not_inherited(library) -> None:
    """The load-bearing case: a different release's identity must never be donated."""
    library.build([SIBLING_B])

    resolved = library.resolve({"release_mbid": RELEASE_A, "release_id": RELEASE_A})

    assert resolved["musicbrainz_releasegroupid"] == GROUP_A, (
        "the queued release's group must win, not the same-named sibling's"
    )
    assert resolved["barcode"] == "A-BARCODE"
    assert resolved["catalognumber"] == "A-CAT"
    assert str(resolved["originalyear"]) == "1994"
    assert str(resolved["releasedate"]) == "1994-08-22"
    # And the refusal must be the SCOPING, not a swallowed query failure.
    assert library.state["errors"] == []


def test_same_release_sibling_is_still_inherited(library) -> None:
    """The local optimisation must survive — this is what proves the query matches.

    If the scoping were broken (a mistyped column, a predicate that never
    matched), the sibling would not be found, this would fall through to
    MusicBrainz, and ``mb_calls`` would be non-empty.
    """
    library.build([SIBLING_A])

    resolved = library.resolve({"release_mbid": RELEASE_A, "release_id": RELEASE_A})

    assert resolved["musicbrainz_releasegroupid"] == GROUP_A
    assert resolved["barcode"] == "A-BARCODE"
    assert library.state["mb_calls"] == [], (
        "a same-release sibling is a LOCAL source; MusicBrainz must not be spent"
    )


def test_identityless_row_keeps_name_only_inheritance(library) -> None:
    """CONTROL: a row naming no release must still inherit by name.

    That is the case the fallback exists for, and the only case where an
    artist+album match is the best evidence available.
    """
    library.build([SIBLING_B])

    resolved = library.resolve({})

    assert resolved["musicbrainz_releasegroupid"] == GROUP_B
    assert resolved["barcode"] == "B-BARCODE"
    assert library.state["errors"] == []


def test_same_release_group_wins_over_an_unrelated_release(library) -> None:
    """A sibling sharing the release GROUP is in scope even without the release MBID.

    Real libraries hold rows written before ``musicbrainz_album_mbid`` existed,
    so the group is the only link available. Scoping that allowed only an exact
    release MBID would refuse those and regress the "don't come in as separate
    albums" guarantee.
    """
    same_group_no_release_mbid = dict(
        SIBLING_B,
        id="t-g",
        musicbrainz_album_mbid="",
        musicbrainz_releasegroupid=GROUP_A,
        barcode="A-BARCODE",
        catalognumber="A-CAT",
        originalyear="1994",
    )
    library.build([same_group_no_release_mbid, SIBLING_B])

    resolved = library.resolve(
        {"release_mbid": RELEASE_A, "release_id": RELEASE_A}
    )

    assert resolved["musicbrainz_releasegroupid"] == GROUP_A
    assert resolved["barcode"] == "A-BARCODE"
    assert str(resolved["originalyear"]) == "1994"


def test_a_row_with_a_group_but_no_release_is_still_scoped(library) -> None:
    """A row that knows only its release GROUP must not take an unrelated release.

    There is no release MBID, so MusicBrainz has nothing to be asked about and
    the fields the row does not carry stay ABSENT — the contract is "never
    invent", so refusing the unrelated sibling must leave a hole rather than
    fill it with the wrong release's values.
    """
    library.build([SIBLING_B])

    resolved = library.resolve({"musicbrainz_releasegroupid": GROUP_A})

    assert resolved["musicbrainz_releasegroupid"] == GROUP_A, "the row's own group is kept"
    assert "barcode" not in resolved, (
        "nothing can supply A's barcode without a release MBID, and B's must not be used"
    )
    assert resolved.get("barcode") != "B-BARCODE"
    assert library.state["mb_calls"] == []


def test_stored_row_values_are_never_overwritten(library) -> None:
    """Inheritance fills ONLY what is missing — a stored value always wins."""
    library.build([SIBLING_A])

    resolved = library.resolve(
        {
            "release_mbid": RELEASE_A,
            "release_id": RELEASE_A,
            "barcode": "ROW-BARCODE",
            "catalognumber": "ROW-CAT",
        }
    )

    assert resolved["barcode"] == "ROW-BARCODE"
    assert resolved["catalognumber"] == "ROW-CAT"


def test_stored_album_metadata_is_honoured(library) -> None:
    """The queue row's own ``album_metadata`` payload outranks any sibling."""
    library.build([SIBLING_B])

    resolved = library.resolve(
        {
            "release_mbid": RELEASE_A,
            "release_id": RELEASE_A,
            "metadata": json.dumps(
                {"album_metadata": {"barcode": "STORED-BARCODE", "catalognumber": "STORED-CAT"}}
            ),
        }
    )

    assert resolved["barcode"] == "STORED-BARCODE"
    assert resolved["catalognumber"] == "STORED-CAT"


def test_a_failing_sibling_query_still_resolves_from_musicbrainz(library) -> None:
    """A broken local lookup must degrade to MusicBrainz, never to a wrong release."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dcs, "db_session", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        resolved = dcs._resolve_album_level_metadata(
            {
                "id": 1,
                "artist": "Portishead",
                "album_artist": "Portishead",
                "album": "Dummy",
                "title": "Mysterons",
                "release_mbid": RELEASE_A,
                "release_id": RELEASE_A,
                "metadata": "{}",
            }
        )
    finally:
        monkeypatch.undo()

    assert resolved["musicbrainz_releasegroupid"] == GROUP_A
    assert resolved["barcode"] == "A-BARCODE"


def test_ordering_prefers_the_exact_release_over_a_group_match(library) -> None:
    """When both a release-match and a group-match sibling exist, the exact one wins.

    ⚠️ The ids are chosen so that a plain ``ORDER BY id`` picks the WRONG row:
    ``a-*`` sorts before ``z-*``, so the exact-release sibling is deliberately
    the LAST one. With an insertion order or an alphabetic id order doing the
    deciding, this test would pass against the unscoped query too and prove
    nothing — the release-match preference is what has to choose.
    """
    wrong_release = dict(SIBLING_B, id="a-wrong", musicbrainz_album_mbid=RELEASE_B)
    group_only = dict(
        SIBLING_B,
        id="b-group",
        musicbrainz_album_mbid="",
        musicbrainz_releasegroupid=GROUP_A,
        barcode="GROUP-BARCODE",
        catalognumber="GROUP-CAT",
    )
    exact = dict(SIBLING_A, id="z-exact")
    library.build([wrong_release, group_only, exact])

    resolved = library.resolve({"release_mbid": RELEASE_A, "release_id": RELEASE_A})

    assert resolved["barcode"] == "A-BARCODE"
    assert resolved["catalognumber"] == "A-CAT"


def test_no_sibling_leaves_the_field_for_musicbrainz(library) -> None:
    """An empty library must not invent anything — MusicBrainz answers."""
    library.build([])

    resolved = library.resolve({"release_mbid": RELEASE_A, "release_id": RELEASE_A})

    assert resolved["musicbrainz_releasegroupid"] == GROUP_A
    assert resolved["barcode"] == "A-BARCODE"
    assert library.state["errors"] == []
