"""Clearing the Release Name must actually clear it.

Reported: "Deleting the release name from a release and selecting save doesn't
remove it. It puts the same one back."

The album page's ``#album_release_title`` box is documented as
*"Leave blank when the collection holds the original release"* — so blank is a
MEANINGFUL input, not a missing one. The handler nonetheless wrote it with a
truthiness test::

    if release_title:
        payload["release_title"] = release_title

An emptied box therefore contributed NO key to the payload, ``_execute_save``
never touched the column, and the value the user had just deleted was still
there on the next render. The box was write-only in the one direction that
matters — removing an edition name the collection no longer holds.

Two halves had to be fixed, and the second is what made it survive a rescan:

1. **The database.** The write is now PRESENCE-gated (`"album_release_title" in
   form`) — the same convention ``album_lookup_skip`` already uses — so a caller
   that omits the field leaves the column alone while a submitted blank clears
   it. ``release_year``, ``album_type`` and the release-detail fields had the
   identical defect and are fixed with it.
2. **The file.** ``release_title`` is not in ``build_tag_updates``' column map
   (the FILE's name for it is ``albumversion``) and that function deliberately
   drops empty values, so the old frame could never be removed. Navidrome reads
   ``albumversion`` and APPENDS it to the album name as a subtitle, so a stale
   frame re-clips the edition the album page no longer shows.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"


@pytest.fixture(scope="module")
def ui_source() -> str:
    return UI_ROUTES.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def album_field_block(ui_source: str) -> str:
    """The album-scalar section of ``album_detail``'s POST branch.

    Anchored on the per-track ``payload`` construction, NOT on the form reads:
    the reads happen far above the writes (the staged-review parsing sits
    between them), so a window starting at ``release_title = (form.get(…)``
    ends before the first assignment it is meant to inspect.
    """
    start = ui_source.index('payload: dict[str, Any] = {"id": track_id}')
    end = ui_source.index("Single-disc albums: strip the per-track", start)
    block = ui_source[start:end]
    assert "release_title" in block, "the album-scalar window lost the release title"
    assert "release_values.items()" in block, "the window ends before the release-detail loop"
    return block


# ---------------------------------------------------------------------------
# 1. The file half — REAL behaviour, driven through the writer
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_tags(monkeypatch) -> list[dict[str, Any]]:
    """Run ``_write_album_track_file_tags`` with the disk write intercepted."""
    from routes import ui_routes

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(ui_routes, "resolve_music_file_path", lambda _p: "/music/x.flac")
    monkeypatch.setattr(
        ui_routes,
        "update_file_tags",
        lambda _path, tags: (captured.append(dict(tags)), True)[1],
    )
    return captured


def _write(payload: dict[str, Any], *, strip_disc_numbers: bool = False) -> None:
    from routes import ui_routes

    ui_routes._write_album_track_file_tags(
        "t1", {"file_path": "x.flac"}, payload,
        strip_disc_numbers=strip_disc_numbers, disc_staged=False,
    )


class TestTheReleaseNameReachesTheFile:
    def test_a_set_name_is_written_as_albumversion(self, captured_tags) -> None:
        """``release_title`` is the DB's name for what the FILE calls
        ``albumversion`` — without this mapping the box never reached the file."""
        _write({"id": "t1", "release_title": "Experience: Expanded"})
        assert captured_tags[0]["albumversion"] == "Experience: Expanded"

    def test_a_cleared_name_clears_the_frame(self, captured_tags) -> None:
        """⭐⭐ THE REPORTED CASE. ``build_tag_updates`` drops empty values, so an
        explicit clear has to be pushed here or the old frame survives — and
        Navidrome re-glues it onto the album name."""
        _write({"id": "t1", "release_title": ""})
        assert captured_tags[0] == {"albumversion": ""}

    def test_a_whitespace_only_name_is_a_clear(self, captured_tags) -> None:
        _write({"id": "t1", "release_title": "   "})
        assert captured_tags[0]["albumversion"] == ""

    def test_an_absent_field_writes_nothing(self, captured_tags) -> None:
        """CONTROL: a payload that never carried the field must not blank the
        file's albumversion frame — absence is not a clear."""
        _write({"id": "t1", "title": "Nookie"})
        assert "albumversion" not in captured_tags[0]

    def test_an_explicit_albumversion_wins(self, captured_tags) -> None:
        """The artist page's Edit Release modal posts ``album_albumversion``
        directly; that value must not be overwritten by the other box."""
        _write({"id": "t1", "albumversion": "Direct", "release_title": "Other"})
        assert captured_tags[0]["albumversion"] == "Direct"

    def test_the_disc_number_clear_still_works(self, captured_tags) -> None:
        """CONTROL: the clear this one was modelled on must not regress."""
        _write({"id": "t1"}, strip_disc_numbers=True)
        assert captured_tags[0]["disc_number"] == ""


# ---------------------------------------------------------------------------
# 2. The database half — the gate, and why it is a gate and not a truthiness test
# ---------------------------------------------------------------------------


class TestTheWriteIsPresenceGated:
    def test_the_release_name_is_written_without_a_truthiness_test(
        self, album_field_block: str
    ) -> None:
        """⭐ ``if release_title:`` WAS the bug."""
        assert 'if "album_release_title" in form:' in album_field_block
        assert not re.search(r"^\s*if release_title:\s*$", album_field_block, re.M), (
            "the truthiness gate is back — an emptied Release Name box will "
            "stop clearing the column"
        )

    def test_an_absent_field_is_not_a_clear(self, album_field_block: str) -> None:
        """A partial caller (the artist page's modal, a script) that omits the
        input must leave the stored value alone — which is exactly why the test
        is on the FORM KEY and not on the value."""
        assert '"album_release_title" in form' in album_field_block

    @pytest.mark.parametrize(
        "gate",
        [
            '"release_year" in form',
            'f"album_{field}" in form',
        ],
    )
    def test_the_same_defect_is_fixed_for_its_neighbours(
        self, album_field_block: str, gate: str
    ) -> None:
        """Each of these boxes had the identical write-only behaviour: emptying
        it was silently ignored, so the old value came straight back."""
        assert gate in album_field_block, f"still truthiness-gated: {gate}"

    def test_the_album_type_select_is_deliberately_not_gated(
        self, album_field_block: str
    ) -> None:
        """⚠️ THE ONE BOX THAT MUST STAY TRUTHINESS-GATED, and why.

        Its select cannot render every type the scan stores (``album+acoustic``,
        ``album+dj-mix``, a mixtape…). When no option matches, the browser falls
        back to the FIRST one — the blank "Unknown" option — so a blank
        submission does NOT reliably mean "the user cleared it". Treating it as
        a clear would wipe a type the page never showed.
        """
        assert 'elif "album_type" in form:' not in album_field_block
        assert "if album_type:" in album_field_block

    def test_the_release_detail_fields_are_gated_per_field(self, album_field_block: str) -> None:
        """``release_values`` covers fields the album page has NO box for
        (``asin``, ``script``, ``discsubtitle``…). Writing those unconditionally
        would blank metadata the page cannot show, so the gate is per FIELD."""
        assert "if value or f\"album_{field}\" in form:" in album_field_block


# ---------------------------------------------------------------------------
# 3. Why a blank release year is safe to hand to the repository
# ---------------------------------------------------------------------------


class TestTheClearsSurviveTheColumnTypes:
    def test_an_empty_release_year_becomes_null_not_a_cast_error(self) -> None:
        """``tracks.release_year`` is INTEGER; sending ``""`` would raise
        ``invalid input syntax for type integer`` and (before the savepoint)
        take the rest of the album's writes with it."""
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type

        assert coerce_track_value_for_pg_type("release_year", "", "INTEGER") is None

    def test_an_empty_text_column_stays_an_empty_string(self) -> None:
        """``release_title`` is TEXT: ``""`` is the stored "no edition name"."""
        from db.repositories.popularity_repository import coerce_track_value_for_pg_type

        assert coerce_track_value_for_pg_type("release_title", "", "TEXT") == ""

    def test_the_save_keeps_the_key_so_the_upsert_can_apply_it(self) -> None:
        """``_execute_save`` filters by COLUMN NAME, not by truthiness — so a
        key whose value is blank still reaches the ``UPDATE SET`` clause. Had it
        filtered on truthiness, this whole fix would be a no-op that looked
        like one."""
        repo = (REPO_ROOT / "db" / "repositories" / "popularity_repository.py").read_text(
            encoding="utf-8"
        )
        block = repo[repo.index("def _execute_save"):]
        assert 'if k in columns and k != "_navidrome_sync"' in block
        assert "release_title=EXCLUDED.release_title" not in block  # built dynamically
        assert 'update_parts.append(f"{k}=EXCLUDED.{k}")' in block


# ---------------------------------------------------------------------------
# 4. The page reads the cleared value back
# ---------------------------------------------------------------------------


class TestThePageShowsWhatIsStored:
    def test_the_box_is_filled_from_the_stored_release_title(self) -> None:
        """The field would re-show the old name if the template fell back to
        anything else, so the display source is pinned to the column."""
        template = (REPO_ROOT / "templates" / "pages" / "album_detail.html").read_text(
            encoding="utf-8"
        )
        assert 'value="{{ album_data.release_title or \'\' }}"' in template

    def test_the_album_view_reads_the_column_not_a_guess(self, ui_source: str) -> None:
        """``album_data["release_title"]`` is ``first_value("release_title")``.
        A fallback to e.g. ``albumversion`` would re-display the very frame the
        file write just cleared."""
        start = ui_source.index("album_data = {")
        block = ui_source[start: start + 3000]
        assert '"release_title": first_value("release_title"),' in block
