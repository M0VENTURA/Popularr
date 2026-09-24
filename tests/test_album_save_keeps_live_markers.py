"""Saving an album must NOT strip live/unplugged/acoustic from a track title.

Reported
--------
> Saving on an album page removes live, unplugged or acoustic from a track even
> if the metadata had updated to include them.

Root cause
----------
The album save (``routes/ui_routes.py::album_detail``) called
``revert_track_live_state`` for every track whenever the SAVED album type was
not live:

    if album_type and "+live" not in album_type.lower() and "(live)" not in ...:
        if track_carries_live_state(track):
            revert_track_live_state(track_id)

That condition is true for **every ordinary album** — ``album_type`` is read
straight from the Album Type ``<select>``, whose normal value is ``album`` — so
the revert fired essentially always.

``revert_track_live_state`` does two things, and the second is the destructive
one: it clears the ``is_live`` / ``is_acoustic`` / ``album_context_live`` flags
**and it rewrites the title** through ``strip_live_acoustic_suffix``. So a track
whose metadata had just been updated to ``"Song (Unplugged)"`` was silently
reduced back to ``"Song"``.

Proven end to end before the fix::

    before save   title='Song (Unplugged)'  is_acoustic=1
    after save    title='Song'              is_acoustic=0

The guard also ignored ``+acoustic``, so an ``album+acoustic`` release was
treated as ordinary and lost its "(Acoustic)" titles the same way.

Why the condition was wrong
---------------------------
The revert exists to undo tagging the pipeline applied because it *believed the
album was live*. That is only meaningful when the belief has just CHANGED. The
old guard asked "is the saved type non-live?", which is true for every normal
album rather than for a reclassification.

This save path cannot express that change either: it never writes ``is_live`` /
``is_acoustic`` / ``album_context_live`` (none appears in the payload it builds,
and none is in ``_STAGED_WRITABLE``). Nothing about the live state is cleared
here, so there is nothing for it to undo.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from routes.ui_routes import _album_type_is_live_state

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROUTES = REPO_ROOT / "routes" / "ui_routes.py"


def _album_save_source() -> str:
    """The album-detail handler's own source."""
    from routes import ui_routes

    return inspect.getsource(ui_routes.album_detail)


def _code_only(source: str) -> str:
    """Strip comments so an assertion cannot be satisfied by the prose."""
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"''' (?:.|\n)*?'''", "", source)
    source = re.sub(r"#[^\n]*", "", source)
    return source


# ===========================================================================
# 1. The helper recognises every live-state spelling
# ===========================================================================

class TestLiveStateTypeDetection:

    @pytest.mark.parametrize(
        "value",
        ["album+live", "Album + Live", "(live)", "Live", "album+acoustic",
         "Album + Acoustic", "(acoustic)"],
    )
    def test_live_state_types_are_recognised(self, value):
        assert _album_type_is_live_state(value) is True, (
            f"{value!r} describes a live/acoustic release but was not recognised, "
            "so its titles would be reverted as if it were a studio album"
        )

    @pytest.mark.parametrize(
        "value",
        ["album", "Album", "ep", "single", "album+remix", "album+compilation",
         "album+soundtrack", "", None],
    )
    def test_ordinary_types_are_not_live_state(self, value):
        assert _album_type_is_live_state(value) is False

    def test_acoustic_counts_as_live_state(self):
        """The old guard checked only '+live'/'(live)'.

        `_apply_live_remix_album_tagging` applies the Acoustic label for
        `+acoustic`, so the revert must treat it as live state or it strips the
        label it applied.
        """
        assert _album_type_is_live_state("album+acoustic") is True

    @pytest.mark.parametrize(
        "value",
        [
            # ⚠️ MUST use word boundaries, not a substring test. Each of these
            # CONTAINS "live" but is an ordinary release type; a substring check
            # classified them as live albums, which would route every ordinary
            # save of such an album through the revert.
            "Delivery",
            "Deliverance",
            "Alive",
            "Outlive",
            "Unlived",
        ],
    )
    def test_a_word_merely_containing_live_is_not_live_state(self, value):
        assert _album_type_is_live_state(value) is False, (
            f"{value!r} merely CONTAINS 'live' — a substring match would treat "
            "it as a live album"
        )

    def test_the_matcher_uses_word_boundaries(self):
        """Pin the mechanism, because a substring version passes most cases."""
        src = _code_only(inspect.getsource(_album_type_is_live_state))
        assert "\\b" in src, (
            "_album_type_is_live_state stopped using word boundaries, so "
            "'Delivery' / 'Alive' would now match as live releases"
        )


# ===========================================================================
# 2. The revert fires ONLY on a reclassification
# ===========================================================================

class TestRevertOnlyOnReclassification:

    @staticmethod
    def _reverts(stored: str, saved: str) -> bool:
        """The exact condition the album save now uses."""
        return _album_type_is_live_state(stored) and not _album_type_is_live_state(saved)

    @pytest.mark.parametrize(
        "stored,saved",
        [
            # ⛔ THE REPORTED BUG: an ordinary save must never strip.
            ("album", "album"),
            ("", "album"),
            ("album", ""),
            ("album", "album+remix"),
            ("ep", "ep"),
            ("single", "album"),
            # Already live and staying live — nothing changed.
            ("album+live", "album+live"),
            ("album+acoustic", "album+acoustic"),
            ("(Live)", "(live)"),
        ],
    )
    def test_no_revert_without_a_reclassification(self, stored, saved):
        assert self._reverts(stored, saved) is False, (
            f"stored={stored!r} saved={saved!r} would revert, so saving an "
            "ordinary album still strips live/acoustic markers from titles"
        )

    @pytest.mark.parametrize(
        "stored,saved",
        [
            # ✅ A genuine reclassification: the pipeline had applied live
            # tagging because it believed the album was live, and the user has
            # now saved it as a studio album. Undoing the tagging is correct.
            ("album+live", "album"),
            ("(live)", "album"),
            ("album+acoustic", "album"),
            ("(acoustic)", "ep"),
            ("album+live", ""),
        ],
    )
    def test_revert_happens_on_a_real_reclassification(self, stored, saved):
        assert self._reverts(stored, saved) is True, (
            f"stored={stored!r} saved={saved!r} should revert — the album was "
            "reclassified away from live, so pipeline-applied live tagging is "
            "now stale"
        )


# ===========================================================================
# 3. The handler itself uses the new condition
# ===========================================================================

class TestTheAlbumSaveUsesTheNewGuard:

    def test_it_no_longer_keys_on_the_saved_type_alone(self):
        """The old condition made the revert fire for every ordinary album."""
        code = _code_only(_album_save_source())
        assert 'album_type and "+live" not in album_type.lower()' not in code, (
            "the album save is back to reverting whenever the SAVED type is "
            "non-live, which is true for every ordinary album — that is the "
            "reported title-stripping bug"
        )

    def test_it_compares_the_stored_type_with_the_saved_type(self):
        code = _code_only(_album_save_source())
        assert "_stored_type_was_live" in code, (
            "the save no longer reads the STORED album type, so it cannot tell a "
            "reclassification from an ordinary save"
        )
        assert "_saved_type_is_live" in code
        assert "_stored_type_was_live and not _saved_type_is_live" in code, (
            "the revert is not gated on an actual live -> non-live transition"
        )

    def test_it_still_guards_with_the_cheap_predicate(self):
        """The per-track guard is what avoids a SELECT per track."""
        code = _code_only(_album_save_source())
        assert "track_carries_live_state(track)" in code

    def test_the_save_never_writes_the_live_flags(self):
        """Which is WHY reverting on an ordinary save can only destroy.

        If this ever changes, the revert gate must be revisited — so the
        assumption it rests on is pinned here rather than left implicit.
        """
        code = _code_only(_album_save_source())
        for field in ("is_live", "is_acoustic", "album_context_live"):
            assert f'payload["{field}"]' not in code, (
                f"the album save now writes {field}, so 'nothing was cleared "
                "here' is no longer true and the revert gate needs rethinking"
            )

    def test_the_whitelist_still_excludes_the_live_flags(self):
        src = UI_ROUTES.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"_STAGED_WRITABLE = frozenset\(\{(.*?)\}\)", src, re.S)
        assert m, "_STAGED_WRITABLE not found"
        body = m.group(1)
        for field in ("is_live", "is_acoustic", "album_context_live"):
            assert field not in body, (
                f"{field} became staged-writable, so a review CAN clear the live "
                "state and the revert gate must account for it"
            )


# ===========================================================================
# 4. `strip_live_acoustic_suffix` itself is not the defect
# ===========================================================================

class TestTheStripperIsFineInIsolation:
    """It is a legitimate helper; the bug was calling it on every save."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Song (Live)", "Song"),
            ("Song (Acoustic)", "Song"),
            ("Song (Unplugged)", "Song"),
            ("Song (Recorded Live)", "Song"),
            ("Song (Stripped Acoustic)", "Song"),
        ],
    )
    def test_it_strips_a_trailing_marker(self, raw, expected):
        from helpers.normalization_service import strip_live_acoustic_suffix

        assert strip_live_acoustic_suffix(raw) == expected

    @pytest.mark.parametrize("raw", ["Song", "Live And Let Die", "Acoustic Sunrise"])
    def test_it_leaves_a_marker_free_title_alone(self, raw):
        from helpers.normalization_service import strip_live_acoustic_suffix

        assert strip_live_acoustic_suffix(raw) == raw

    def test_the_documented_revert_uses_it(self):
        """Confirms the destructive half of the revert is real, not assumed."""
        from services.popularity.stages import album_stage

        src = _code_only(inspect.getsource(album_stage.revert_track_live_state))
        assert "strip_live_acoustic_suffix" in src, (
            "revert_track_live_state no longer rewrites the title, so this "
            "guard's premise changed — re-check the album-save gate"
        )
