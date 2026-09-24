"""The artist page's Edit Release modal must be able to edit a release.

REPORTED:

    Clicking the edit album modal on the artist page brings up Edit Release
    with Title / Year / MBID. When save is selected, it doesn't seem to update
    those fields on the tracks contained in the album or the album in the
    database. The edit album modal should also contain all the edit album
    fields that show when on the album page.

TWO DEFECTS, and they are independent:

1. THE MBID WAS NEVER POSTED AT ALL. The save handler hand-picked three values
   into a FormData and never read ``#editReleaseMbid``. The route reads
   ``album_mbid``, so even if it had been posted under the element's id the
   name would not have matched. The user typed an id, pressed Save, and it was
   discarded with no error — ``r.ok`` was true, so "Release updated." appeared.
   ⚠️ That is the worst combination: a silent no-op reporting success.

2. THE MODAL EXPOSED A STRICT SUBSET OF THE ALBUM PAGE'S FIELDS. Both forms POST
   to the SAME endpoint (``/album/<artist>/<album>``), so the artist page could
   do strictly less than the album page for no reason.

⚠️ AND THE VALUES WERE NEVER LOADED. The modal was only ever seeded from the
release row's ``data-*`` attributes (artist/album/mbid/rgid/year), so every
other field rendered EMPTY — and saving an empty field posts a blank over real
data. Title/Year/MBID appeared empty or stale rather than showing the album's
actual metadata.

THE DRIFT RISK is the thing to guard: a field the album page posts but the
modal omits is a field a user cannot edit from an artist page, and a field named
differently is silently discarded. So the tests below derive the expected field
set FROM the album page's own markup and the route's own reader, rather than
hard-coding a list here that would rot the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODAL_PARTIAL = REPO_ROOT / "test_site" / "templates" / "components" / "modals" / "_artist_modals.html"
ALBUM_PAGE = REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"
ARTIST_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "artist-releases.js"
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"

ALBUM_URL = "/album/Madball/Not%20Your%20Kingdom"


def _modal_block() -> str:
    """Just the ``#editReleaseModal`` div from the partial."""
    src = MODAL_PARTIAL.read_text(encoding="utf-8")
    start = src.index('id="editReleaseModal"')
    # The modal ends at the next top-level closing of its own div; take a wide
    # window and cut at the next modal's id, which is unambiguous.
    rest = src[start:]
    nxt = rest.find('id="editTrackModal"')
    return rest[:nxt] if nxt != -1 else rest


def _album_page_field_names() -> set[str]:
    """``name="album_..."`` inputs the album page posts (excluding hidden)."""
    src = ALBUM_PAGE.read_text(encoding="utf-8")
    names = set()
    for m in re.finditer(r"<(input|select|textarea)[^>]*>", src):
        tag = m.group(0)
        if 'type="hidden"' in tag:
            continue
        nm = re.search(r'name="([^"]+)"', tag)
        if nm:
            names.add(nm.group(1))
    return names


def _modal_field_names() -> set[str]:
    names = set()
    for m in re.finditer(r"<(input|select|textarea)[^>]*>", _modal_block()):
        tag = m.group(0)
        if 'type="hidden"' in tag:
            continue
        nm = re.search(r'name="([^"]+)"', tag)
        if nm:
            names.add(nm.group(1))
    return names


def _route_reads() -> str:
    return UI_ROUTES.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The modal covers the album page's field set
# ---------------------------------------------------------------------------

class TestTheModalMirrorsTheAlbumPage:
    def test_the_modal_has_proper_edit_fields_not_three(self):
        names = _modal_field_names()
        assert len(names) >= 15, (
            "the modal is back to a handful of fields; the report is that it "
            f"must offer the album page's field set (found {sorted(names)})"
        )

    def test_it_covers_every_non_hidden_field_on_the_album_page(self):
        """The report, stated as an assertion.

        A field the album page posts but the modal omits is one a user cannot
        edit from an artist page — and the other direction is harmless.

        ⚠️ ``force`` is excluded deliberately: it is the album page's "Force
        full rescan" CHECKBOX, a scan control, not album metadata. Offering it
        in a metadata modal would let a save trigger a rescan by accident.
        """
        NON_METADATA = {"force"}
        album = _album_page_field_names() - NON_METADATA
        modal = _modal_field_names()
        missing = sorted(album - modal)
        assert not missing, (
            "these album-page fields are missing from the Edit Release modal, "
            f"so they cannot be edited from an artist page: {missing}"
        )

    def test_it_does_not_invent_field_names_the_route_cannot_read(self):
        """Every posted name must be one the album route actually reads.

        ⚠️ THIS IS THE EXACT BUG. The modal posted nothing for the MBID, and a
        field named anything other than ``album_mbid`` / ``album_release_group_mbid``
        is dropped by the route without complaint — the save reports success
        while the value is discarded.
        """
        route = _route_reads()
        handled = set(re.findall(r'form\.get\("([^"]+)"\)', route))
        # ``release_fields`` are read as f"album_{f}".
        release_fields = re.search(
            r"release_fields\s*=\s*\[([^\]]+)\]", route, re.S
        )
        if release_fields:
            for f in re.findall(r'"([^"]+)"', release_fields.group(1)):
                handled.add(f"album_{f}")
        # Staged JSON + the flags the handler consults by name.
        handled |= {
            "staged_track_updates", "pending_recommendations",
            "track_artist", "track_composer", "track_comment",
        }

        unreadable = sorted(
            n for n in _modal_field_names()
            if n not in handled and not n.endswith("_hidden")
        )
        assert not unreadable, (
            "the modal posts fields the route never reads, so they are silently "
            f"discarded on save: {unreadable}"
        )


# ---------------------------------------------------------------------------
# 2. The MBID is actually sent
# ---------------------------------------------------------------------------

class TestTheMbidIsSent:
    def test_the_mbid_input_has_the_name_the_route_reads(self):
        block = _modal_block()
        m = re.search(r'<input[^>]*id="editReleaseMbid"[^>]*>', block)
        assert m, "the MBID input disappeared from the modal"
        assert 'name="album_mbid"' in m.group(0), (
            "the MBID input must be named album_mbid — the route reads that, and "
            "under any other name the value is discarded without error"
        )

    def test_the_release_group_mbid_is_editable_too(self):
        block = _modal_block()
        assert 'name="album_release_group_mbid"' in block

    def test_the_save_handler_posts_fields_generically(self):
        """Not a hand-picked trio — that is what dropped the MBID."""
        src = ARTIST_JS.read_text(encoding="utf-8")
        assert "editReleaseModal [name]" in src, (
            "the save handler must collect every named field in the modal; "
            "hand-picking values is how the MBID came to be ignored"
        )

    def test_the_save_handler_no_longer_references_only_three_values(self):
        src = ARTIST_JS.read_text(encoding="utf-8")
        assert "form.set('album_originalyear', year)" not in src, (
            "the old hand-picked mapping (which sent the YEAR as "
            "album_originalyear and never sent the MBID) is back"
        )


# ---------------------------------------------------------------------------
# 3. Values are loaded, not left blank
# ---------------------------------------------------------------------------

class TestTheModalLoadsRealValues:
    def test_it_fetches_the_album_metadata_endpoint(self):
        src = ARTIST_JS.read_text(encoding="utf-8")
        assert "/api/album/metadata" in src, (
            "the modal must load the album's real field values; seeding only "
            "from data-* attributes leaves every other field blank, and saving "
            "a blank posts it over real data"
        )

    def test_the_endpoint_exists(self):
        src = (REPO_ROOT / "routes" / "album_routes.py").read_text(encoding="utf-8")
        assert '@album_bp.route("/metadata"' in src

    def test_it_still_seeds_from_the_row_so_it_never_shows_stale_values(self):
        """A failed metadata fetch must not leave the PREVIOUS album's values."""
        src = ARTIST_JS.read_text(encoding="utf-8")
        open_fn = src[src.index("function openEditReleaseModal"):]
        open_fn = open_fn[: open_fn.index("function importMissingRelease")]
        assert "set('editReleaseTitle'" in open_fn, (
            "the modal must seed from the row before (and in case of) the fetch, "
            "or reopening it shows the previously-edited album's values"
        )

    def test_the_album_name_is_read_from_the_row_not_invented(self):
        """The merge lives in one place: the endpoint, keyed by artist+album."""
        src = (REPO_ROOT / "routes" / "album_routes.py").read_text(encoding="utf-8")
        assert "@album_bp.route(\"/metadata\"" in src
        assert "musicbrainz_album_mbid" in src, (
            "the endpoint must return the same column precedence the album page "
            "uses, or the two forms disagree about what the current value is"
        )


# ---------------------------------------------------------------------------
# 4. The endpoint returns real, correct values
# ---------------------------------------------------------------------------

class TestTheMetadataEndpoint:
    """Drives the endpoint against a stubbed session.

    ⚠️ The route imports ``db_session`` INSIDE the function body, so patching
    ``db.engine.db_session`` is what takes effect. Patching the route module's
    own namespace does nothing there — nothing is bound to it.
    """

    @staticmethod
    def _patch(monkeypatch, tracks):
        import db.engine as engine

        class _Row:
            def __init__(self, mapping):
                self._mapping = dict(mapping)

        class _Result:
            def fetchall(self):
                return [_Row(t) for t in tracks]

        class _Session:
            def execute(self, *a, **k):
                return _Result()

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

        monkeypatch.setattr(engine, "db_session", lambda *a, **k: _Session())

    @pytest.mark.asyncio
    async def test_it_returns_the_albums_current_values(self, monkeypatch, client):
        self._patch(monkeypatch, [{
            "album": "B-Sides & Rarities",
            "album_artist": "Deftones",
            "artist": "Deftones",
            "release_title": "B-Sides & Rarities",
            "originalyear": "2005",
            "musicbrainz_album_mbid": "rel-1",
            "musicbrainz_releasegroupid": "rg-1",
            "recordlabel": "Rhino",
            "barcode": "075678329125",
        }])
        resp = await client.get(
            "/api/album/metadata?artist=Deftones&album=B-Sides%20%26%20Rarities"
        )
        data = await resp.get_json()

        assert data["success"] is True, data
        meta = data["metadata"]
        assert meta["album_title"] == "B-Sides & Rarities"
        assert meta["album_mbid"] == "rel-1"
        assert meta["album_release_group_mbid"] == "rg-1"
        assert meta["album_originalyear"] == "2005"
        assert meta["album_recordlabel"] == "Rhino"

    @pytest.mark.asyncio
    async def test_a_value_on_a_later_track_is_still_found(self, monkeypatch, client):
        """Column priority is honoured, and any track can supply the value."""
        self._patch(monkeypatch, [
            {"album": "A", "album_artist": "X", "artist": "X"},
            {"album": "A", "album_artist": "X", "artist": "X",
             "musicbrainz_album_mbid": "only-on-track-2"},
        ])
        resp = await client.get("/api/album/metadata?artist=X&album=A")
        data = await resp.get_json()
        assert data["metadata"]["album_mbid"] == "only-on-track-2", (
            "a value present only on a later track must still be found, or the "
            "modal shows blank and saving posts blank over real data"
        )

    @pytest.mark.asyncio
    async def test_a_higher_priority_column_wins(self, monkeypatch, client):
        """``musicbrainz_album_mbid`` outranks the legacy ``musicbrainz_albumid``."""
        self._patch(monkeypatch, [{
            "album": "A", "album_artist": "X", "artist": "X",
            "musicbrainz_albumid": "legacy-value",
            "musicbrainz_album_mbid": "canonical-value",
        }])
        resp = await client.get("/api/album/metadata?artist=X&album=A")
        data = await resp.get_json()
        assert data["metadata"]["album_mbid"] == "canonical-value"

    @pytest.mark.asyncio
    async def test_it_requires_both_keys(self, client):
        resp = await client.get("/api/album/metadata?artist=Only")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_an_unknown_album_is_a_clean_404(self, monkeypatch, client):
        self._patch(monkeypatch, [])
        resp = await client.get("/api/album/metadata?artist=X&album=Missing")
        assert resp.status_code == 404
        data = await resp.get_json()
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_track_artist_is_not_prefilled(self, monkeypatch, client):
        """It is a PER-TRACK field; prefilling it would overwrite every track.

        Saving the album form writes ``track_artist`` to all tracks, so a
        prefilled majority value would silently homogenise a compilation.
        """
        self._patch(monkeypatch, [{"album": "A", "album_artist": "X", "artist": "X"}])
        resp = await client.get("/api/album/metadata?artist=X&album=A")
        data = await resp.get_json()
        assert "track_artist" not in data["metadata"], (
            "track_artist must not be prefilled — it applies to EVERY track"
        )

    @pytest.mark.asyncio
    async def test_a_db_failure_does_not_500_the_modal(self, monkeypatch, client):
        """The modal falls back to the row's values, so this must be soft."""
        import db.engine as engine

        def _boom(*a, **k):
            raise RuntimeError("table missing")

        monkeypatch.setattr(engine, "db_session", _boom)
        resp = await client.get("/api/album/metadata?artist=X&album=A")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["success"] is False and data.get("error")


# ---------------------------------------------------------------------------
# 5. The route accepts the modal's MBID names
# ---------------------------------------------------------------------------

class TestTheRouteAcceptsTheModalPayload:
    def test_the_route_reads_the_release_mbid_under_the_modal_names(self):
        """Guards the silent-discard class, not just this one field."""
        route = _route_reads()
        # The MBID line must consult at least the canonical name; the aliases are
        # there because two different callers used two different names.
        assert 'form.get("album_mbid")' in route
        assert 'form.get("musicbrainz_release_id")' in route, (
            "the route should accept the alias the /api/album/update-ids endpoint "
            "uses, so either caller's payload works"
        )

    def test_no_duplicate_dom_ids_inside_the_modal(self):
        """``getElementById`` returns the FIRST match, so a duplicate id means
        the modal writes into the other element."""
        block = _modal_block()
        ids = re.findall(r'id="([^"]+)"', block)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        assert not dupes, f"duplicate ids in the modal: {dupes}"

    def test_the_modal_ids_do_not_collide_with_the_album_pages(self):
        """Both can render on one page (the album page includes the partials)."""
        album_ids = set(re.findall(r'id="([^"]+)"', ALBUM_PAGE.read_text(encoding="utf-8")))
        modal_ids = set(re.findall(r'id="([^"]+)"', _modal_block()))
        clashes = sorted(album_ids & modal_ids)
        assert not clashes, (
            "these ids exist in BOTH the album page and the modal; whichever "
            f"renders first wins for getElementById: {clashes}"
        )
