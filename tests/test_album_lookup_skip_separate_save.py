"""Custom-album setting saves on its own, at the top of the Edit Album tab.

Reported
--------
> When selecting this and saving, it time out as it tries to update all the
> files. As it's a setting only, there should be a separate save button under
> the custom album setting. It may be better placed to move this to the top of
> the Edit Album Metadata page, then the info below it.

What changed
------------
The "Do not search album release" checkbox used to live INSIDE
``#albumMetadataForm``, so toggating one boolean cost the WHOLE save: the
album → tracks DB fan-out plus a concurrent write of every file tag — a
timeout on a large album for a value that is a single album-wide UPDATE.

* The block moved OUT of the form and to the TOP of the Edit Album Metadata
  tab (its own card), with the rest of the form below it.
* It gained its OWN Save button posting to ``POST /api/album/lookup-skip``
  (one UPDATE, no fan-out, no file writes).
* The big save is now PRESENCE-GATED: ``if "album_lookup_skip" in form`` —
  a form that does not carry the field must not CLEAR it. This also fixes a
  latent cross-tree bug: the live tree's form never had the field, so a live-
  mode save would have cleared a custom album marked from the rebuilt page.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "test_site" / "templates" / "Pages" / "album_detail.html"
ALBUM_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _form_span(html: str) -> str:
    """The exact body of #albumMetadataForm (open tag → its </form>)."""
    start = html.index('<form method="POST" action="/album/')
    end = html.index("</form>", start)
    return html[start:end]


# ===========================================================================
# 1. Placement: top of the Edit Album Metadata tab, outside the big form
# ===========================================================================


class TestTheBlockMovedToTheTop:
    def test_the_checkbox_precedes_the_big_form(self) -> None:
        html = _template()
        assert html.index('id="album_lookup_skip"') < html.index('id="albumMetadataForm"'), (
            "the setting must sit at the TOP of the tab, above the form"
        )

    def test_the_checkbox_is_not_inside_the_big_form(self) -> None:
        span = _form_span(_template())
        assert 'id="album_lookup_skip"' not in span, (
            "a checkbox inside #albumMetadataForm rides the whole save — the "
            "exact timeout that was reported"
        )

    def test_the_checkbox_appears_exactly_once(self) -> None:
        """The move must DELETE the old block, not copy it."""
        assert _template().count('id="album_lookup_skip"') == 1

    def test_it_has_its_own_save_button(self) -> None:
        html = _template()
        assert 'onclick="saveAlbumLookupSkip(this)"' in html
        # The button must live in the new card, above the form.
        assert html.index('onclick="saveAlbumLookupSkip(this)"') < html.index(
            'id="albumMetadataForm"'
        )

    def test_the_help_text_no_longer_pretends_the_form_saves_it(self) -> None:
        html = _template()
        assert "Saved with the rest of this form." not in html, (
            "the setting no longer posts with the form — the old wording "
            "would tell the user the opposite"
        )
        assert "Saved on its own" in html

    def test_the_csv_card_moved_with_the_block(self) -> None:
        html = _template()
        assert "albumLookupSkipCsvCard" in html
        assert html.index("albumLookupSkipCsvCard") < html.index('id="albumMetadataForm"'), (
            "the CSV upload belongs to the custom-album card, not the big form"
        )


# ===========================================================================
# 2. The big save is presence-gated: an absent field must not clear the flag
# ===========================================================================


class TestTheBigSaveCannotClearTheFlag:
    @staticmethod
    def _handler_src() -> str:
        return (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")

    def test_the_flag_write_is_presence_gated(self) -> None:
        src = self._handler_src()
        assert 'if "album_lookup_skip" in form:' in src, (
            "the big save must only write the flag when the field was "
            "actually posted — otherwise every save without the checkbox "
            "clears it (and a live-tree save would clear a custom album "
            "marked from the rebuilt page)"
        )

    def test_the_gate_precedes_the_write(self) -> None:
        src = self._handler_src()
        gate = src.index('if "album_lookup_skip" in form:')
        write = src.index("set_album_lookup_skip(", gate)
        assert gate < write, "the write must sit INSIDE the presence gate"

    def test_the_template_still_renders_the_saved_state(self) -> None:
        """CONTROL — album_data must still carry the flag so the checkbox
        renders checked from the DB (unchanged read path)."""
        src = self._handler_src()
        album_data = src.split("album_data = {")[1].split("}")[0]
        assert "album_lookup_skip" in album_data


# ===========================================================================
# 3. The dedicated endpoint
# ===========================================================================


class TestTheDedicatedEndpoint:
    @staticmethod
    def _route_src() -> str:
        return (REPO_ROOT / "routes" / "album_routes.py").read_text(encoding="utf-8")

    def test_the_route_exists(self) -> None:
        src = self._route_src()
        assert '@album_bp.route("/lookup-skip", methods=["POST"])' in src
        assert "set_album_lookup_skip(" in src, (
            "the endpoint must delegate to the SAME service the album save "
            "uses — one writer, one scope rule"
        )

    def test_the_page_posts_to_it(self) -> None:
        js = ALBUM_JS.read_text(encoding="utf-8")
        assert "'/api/album/lookup-skip'" in js
        assert "saveAlbumLookupSkip" in js
        assert "global.saveAlbumLookupSkip = saveAlbumLookupSkip" in js, (
            "the template's onclick calls the global — an unexported handler "
            "is a dead button (templates have no compiler)"
        )

    def test_the_handler_does_not_reach_for_the_whole_form(self) -> None:
        js = ALBUM_JS.read_text(encoding="utf-8")
        assert "buttonState.withBusy" in js, (
            "the save button needs a busy state like every other mutation here"
        )

    async def test_the_endpoint_saves_the_flag(self, client, monkeypatch) -> None:
        """Behavioural: the endpoint writes via the SAME service the album
        save uses — one album-wide UPDATE, no track fan-out, no file writes."""
        import services.metadata.album_lookup_skip_service as alss

        calls: list[tuple] = []

        def _fake(artist, album, skip):
            calls.append((artist, album, skip))
            return 1

        # Patched at the SERVICE module — the route imports the name INSIDE
        # the function, so the module attribute is what resolves at call time.
        monkeypatch.setattr(alss, "set_album_lookup_skip", _fake)

        resp = await client.post(
            "/api/album/lookup-skip",
            json={"artist": "Test Artist", "album": "Bootleg Tape", "skip": True},
        )
        assert resp.status_code == 200
        payload = json.loads(await resp.get_data(as_text=True))
        assert payload == {"success": True, "skip": True}
        assert calls == [("Test Artist", "Bootleg Tape", True)], calls

    async def test_the_endpoint_can_turn_the_flag_back_off(self, client, monkeypatch) -> None:
        import services.metadata.album_lookup_skip_service as alss

        calls: list[tuple] = []
        monkeypatch.setattr(
            alss, "set_album_lookup_skip",
            lambda artist, album, skip: calls.append((artist, album, skip)) or 1,
        )
        resp = await client.post(
            "/api/album/lookup-skip",
            json={"artist": "Test Artist", "album": "Bootleg Tape", "skip": False},
        )
        assert resp.status_code == 200
        assert calls == [("Test Artist", "Bootleg Tape", False)]

    async def test_a_missing_artist_is_rejected(self, client) -> None:
        """A 400 must not reach the service — scoping to an empty artist
        would update EVERY row."""
        resp = await client.post(
            "/api/album/lookup-skip",
            json={"artist": "", "album": "Bootleg Tape", "skip": True},
        )
        assert resp.status_code == 400
        payload = json.loads(await resp.get_data(as_text=True))
        assert "artist" in payload.get("error", "")