"""A "Soundtrack" album artist is a PLACEHOLDER, and must actually be repaired.

REPORT: albums and EPs "marked as Soundtrack seem to be getting changed to
Artist Soundtrack".  Clarified by the user as: **the artist name is
"Soundtrack"**, the stored type is ``album+Soundtrack``, and **no MBID is saved
on the releases**.

That combination is unrecoverable in the shipped code, for two independent
reasons:

1. ``_correct_soundtrack_album_artist`` bailed out at
   ``if not release_group_mbid: return``.  With no stored release-group MBID —
   exactly the reported state — the placeholder the legacy scan wrote was
   permanent.  Nothing else ever rewrites ``album_artist``.

2. The repair, when it DID run, updated only ``album_context["album_artist"]``
   behind an ``if album_context:`` truthiness guard.  The caller routinely
   passes an EMPTY dict, so the corrected value was written to the DB and then
   overwritten by the track stage's stale in-memory copy.

A third defect sits alongside them: ``_detect_album_type`` consulted the stored
type only AFTER the ``_COMPILATION_ARTISTS`` branch, which contains
``"soundtrack"``.  So any album whose artist/album_artist is the placeholder
came back as ``album+compilation`` regardless of what was stored — an
``ep+soundtrack`` EP lost its EP identity, and a manual "Album (Soundtrack)"
choice was silently overwritten on the next scan.  ``soundtrack`` was also
missing from the MusicBrainz secondary-type mapping, so a release-group MB
correctly flagged as a soundtrack resolved to a bare ``album``.

These tests pin the placeholder repair (including the no-MBID path and the
"never invent an artist" guards), the stored-type precedence, and the MB
secondary mapping.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from services.popularity.stages import album_stage as a

PLACEHOLDER_ARTIST = "Soundtrack"
REAL_ARTIST = "Hans Zimmer"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, rowcount: int = 3):
        self.rowcount = rowcount


class _Session:
    """Captures UPDATE parameters; everything else is a no-op."""

    def __init__(self, sink: list):
        self._sink = sink

    def execute(self, statement, params=None, *args, **kwargs):
        self._sink.append((str(statement), dict(params or {})))
        return _Result()

    def commit(self):
        return None


class _FakeMbService:
    def __init__(self, *, rg=None, matches=None):
        self._rg = rg or {}
        self._matches = matches or []

    def get_release_group_by_id(self, mbid, includes=None, **kwargs):
        return self._rg

    def search_releasegroup_matches(self, artist, album, limit=5, **kwargs):
        return self._matches


def _install(monkeypatch, service) -> list:
    """Wire the correction to fakes; return the captured DB parameters."""
    captured: list = []

    @contextmanager
    def _fake_session(*args, **kwargs):
        yield _Session(captured)

    monkeypatch.setattr(a, "db_session", _fake_session)
    monkeypatch.setattr(a, "get_shared_mb_service", lambda: service)
    monkeypatch.setattr(
        a, "_call_with_heartbeat", lambda name, fn, *args, **kwargs: fn(*args, **kwargs)
    )
    # File-tag writes are not under test and must never touch the filesystem.
    import services.metadata.tag_file_service as tfs

    monkeypatch.setattr(tfs, "update_file_tags", lambda *args, **kwargs: True)
    monkeypatch.setattr(a, "_log_section", lambda *args, **kwargs: _noop_section())
    return captured


@contextmanager
def _noop_section():
    yield


def _correct(monkeypatch, *, service, album_artist=PLACEHOLDER_ARTIST, rg_mbid=None):
    """Run the correction and return ``(db_params, album_context, tracks, captured)``."""
    captured = _install(monkeypatch, service)
    album_context: dict = {}
    tracks = [{
        "id": "t1",
        "title": "Dream Is Collapsing",
        "artist": REAL_ARTIST,
        "album_artist": album_artist,
        "album": "Inception",
        "file_path": "",   # skip the on-disk tag write
    }]
    a._correct_soundtrack_album_artist(
        artist=album_artist,
        album="Inception",
        album_artist=album_artist,
        release_group_mbid=rg_mbid,
        album_tracks=tracks,
        album_context=album_context,
    )
    writes = [params for sql, params in captured if "SET album_artist" in sql]
    return writes, album_context, tracks, captured


def _confident_match(**overrides):
    match = {
        "id": "rg-9",
        "match_score": 0.92,
        "artist_credit": [{"name": REAL_ARTIST, "joinphrase": ""}],
    }
    match.update(overrides)
    return match


# ---------------------------------------------------------------------------
# 1. The placeholder is repaired
# ---------------------------------------------------------------------------

class TestPlaceholderIsRepaired:
    def test_no_stored_mbid_is_repaired_via_a_title_search(self, monkeypatch):
        """⭐ The reported state: album_artist='Soundtrack', no MBID anywhere.

        The old code returned immediately at ``if not release_group_mbid``, so
        this case could never self-heal.
        """
        writes, ctx, tracks, _ = _correct(
            monkeypatch,
            service=_FakeMbService(matches=[_confident_match()]),
            rg_mbid=None,
        )
        assert writes and writes[0]["new_album_artist"] == REAL_ARTIST, (
            "a 'Soundtrack' placeholder with NO stored MBID was left unrepaired — "
            "the title search fallback must run"
        )

    def test_a_stored_mbid_still_uses_the_exact_credit(self, monkeypatch):
        writes, _, _, _ = _correct(
            monkeypatch,
            service=_FakeMbService(
                rg={"artist-credit": [{"name": REAL_ARTIST, "joinphrase": ""}]}
            ),
            rg_mbid="rg-1",
        )
        assert writes and writes[0]["new_album_artist"] == REAL_ARTIST

    def test_the_in_memory_context_is_updated_even_when_empty(self, monkeypatch):
        """⭐ The caller passes an EMPTY dict; a truthiness guard skipped it."""
        _, ctx, tracks, _ = _correct(
            monkeypatch,
            service=_FakeMbService(matches=[_confident_match()]),
        )
        assert ctx.get("album_artist") == REAL_ARTIST, (
            "album_context was not updated, so the track stage's stale in-memory "
            "copy would overwrite the corrected DB value"
        )
        assert tracks[0]["album_artist"] == REAL_ARTIST, (
            "the in-memory track dict was not mutated"
        )

    def test_the_update_is_scoped_to_the_placeholder(self, monkeypatch):
        """Only rows actually carrying the placeholder may be rewritten."""
        _, _, _, captured = _correct(
            monkeypatch,
            service=_FakeMbService(matches=[_confident_match()]),
        )
        sql = " ".join(s for s, _ in captured if "SET album_artist" in s).lower()
        assert "soundtrack" in sql, (
            "the UPDATE must constrain album_artist to the placeholder values, or "
            "it would rewrite real artists on the album"
        )


class TestNeverInventsAnArtist:
    """A wrong artist is worse than the placeholder — it must fail closed."""

    def test_a_weak_title_match_is_refused(self, monkeypatch):
        writes, _, _, _ = _correct(
            monkeypatch,
            service=_FakeMbService(matches=[_confident_match(match_score=0.4)]),
        )
        assert writes == [], "a low-confidence match must not be adopted"

    def test_no_matches_is_a_no_op(self, monkeypatch):
        writes, _, _, _ = _correct(monkeypatch, service=_FakeMbService(matches=[]))
        assert writes == []

    def test_a_credit_that_is_still_the_placeholder_is_refused(self, monkeypatch):
        writes, _, _, _ = _correct(
            monkeypatch,
            service=_FakeMbService(
                matches=[_confident_match(artist_credit=[{"name": "Soundtrack", "joinphrase": ""}])]
            ),
        )
        assert writes == [], "'Soundtrack' is not a usable artist credit"

    def test_a_real_artist_is_never_touched(self, monkeypatch):
        writes, _, tracks, _ = _correct(
            monkeypatch,
            service=_FakeMbService(matches=[_confident_match(artist_credit=[{"name": "Someone Else"}])]),
            album_artist=REAL_ARTIST,
        )
        assert writes == []
        assert tracks[0]["album_artist"] == REAL_ARTIST


# ---------------------------------------------------------------------------
# 2. A stored RICH type survives a 'Soundtrack' placeholder artist
# ---------------------------------------------------------------------------

class TestStoredTypeOutranksThePlaceholderName:
    """'Soundtrack' is a placeholder for the ARTIST, not for the TYPE."""

    @pytest.mark.parametrize(
        "stored,expected",
        [
            # ⭐ The reported symptom: an EP's identity must survive.
            ("ep+soundtrack", "ep+soundtrack"),
            ("album+soundtrack", "album+soundtrack"),
            ("single+soundtrack", "single+soundtrack"),
            ("album+live", "album+live"),
            ("ep+live", "ep+live"),
            ("album+compilation", "album+compilation"),
            ("ep", "ep"),
            ("single", "single"),
        ],
    )
    def test_a_stored_composite_type_is_returned_verbatim(self, stored, expected):
        assert a._detect_album_type(PLACEHOLDER_ARTIST, "Inception", PLACEHOLDER_ARTIST, stored) == expected, (
            f"the stored type {stored!r} was overwritten for an album whose "
            "album_artist is the 'Soundtrack' placeholder"
        )

    def test_a_bare_secondary_spelling_maps_to_its_composite(self):
        assert a._detect_album_type(PLACEHOLDER_ARTIST, "Inception", PLACEHOLDER_ARTIST, "soundtrack") == "album+soundtrack"

    def test_no_stored_type_still_treats_the_placeholder_as_a_compilation(self):
        """With no type to honour, the placeholder still means "compilation"."""
        assert a._detect_album_type(PLACEHOLDER_ARTIST, "Inception", PLACEHOLDER_ARTIST, None) == "album+compilation"
        assert a._detect_album_type(PLACEHOLDER_ARTIST, "Inception", PLACEHOLDER_ARTIST, "album") == "album+compilation"

    def test_various_artists_is_unchanged(self):
        assert a._detect_album_type("Various Artists", "Pure Rock", "Various Artists", None) == "album+compilation"
        assert a._detect_album_type("Various Artists", "Pure Rock", "Various Artists", "album+soundtrack") == "album+soundtrack"

    def test_an_ordinary_album_is_still_an_album(self):
        assert a._detect_album_type("Madball", "Not Your Kingdom", "Madball", None) == "album"
        assert a._detect_album_type("Madball", "Not Your Kingdom", "Madball", "album") == "album"

    def test_the_title_heuristic_still_applies_without_a_stored_type(self):
        assert a._detect_album_type("Hans Zimmer", "Inception (Original Soundtrack)", "Hans Zimmer", None) == "album+soundtrack"


# ---------------------------------------------------------------------------
# 3. MusicBrainz secondary types map through, soundtrack included
# ---------------------------------------------------------------------------

class TestMusicBrainzSecondaryTypes:
    def _resolve(self, monkeypatch, primary, secondary, tracks=4):
        service = _FakeMbService(matches=[{
            "id": "rg-1",
            "primary_type": primary,
            "secondary_types": secondary,
            "match_score": 0.95,
            "title": "Inception",
        }])
        captured = _install(monkeypatch, service)
        detected, mb_type, rg, raw = a._resolve_album_type(
            "Hans Zimmer", "Inception", "Hans Zimmer", None,
            [{"title": f"t{i}"} for i in range(tracks)],
        )
        return detected, mb_type

    def test_soundtrack_is_no_longer_dropped(self, monkeypatch):
        """⭐ ``soundtrack`` was absent from the mapping, so it became 'album'."""
        detected, mb_type = self._resolve(monkeypatch, "Album", ["soundtrack"])
        assert detected == "album+soundtrack", (
            "a MusicBrainz soundtrack secondary type must survive resolution"
        )
        assert mb_type == "album+soundtrack"

    def test_an_ep_keeps_its_primary(self, monkeypatch):
        detected, _ = self._resolve(monkeypatch, "EP", ["soundtrack"], tracks=4)
        assert detected == "ep+soundtrack", (
            "an EP soundtrack must remain an EP — the registry reads "
            "'ep+soundtrack' under EPs, but 'album+soundtrack' under Soundtracks"
        )

    def test_compilation_outranks_soundtrack(self, monkeypatch):
        detected, _ = self._resolve(monkeypatch, "Album", ["compilation", "soundtrack"])
        assert detected == "album+compilation"

    def test_no_secondary_is_a_plain_album(self, monkeypatch):
        detected, _ = self._resolve(monkeypatch, "Album", [])
        assert detected == "album"


class TestRegistryAgreement:
    """The composite this module writes must land in the expected section."""

    def test_the_composites_resolve_to_the_right_categories(self):
        from services.catalog.release_categories import category_for_album_type

        assert category_for_album_type("ep+soundtrack") == "ep"
        assert category_for_album_type("album+soundtrack") == "soundtrack"
        assert category_for_album_type("single+soundtrack") == "single"
