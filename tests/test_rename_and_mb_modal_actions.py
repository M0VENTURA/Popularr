"""Three reported faults: apostrophe renames, the modal's download button, and
the missing pre-fill.

REPORT

> "I'm sometimes getting track rename recommendations like the following.
>  MusicBrainz: Title: Through a Child's Eyes → Through a Child's Eyes"
>  (the two differ only in the apostrophe: U+0027 vs U+2019)

> "...clicking on the Soulseek button, it doesn't look like anything is
>  happening. Can the button be changed to 'Add to queue' and work in a
>  similar way to the unified search."

> "...when pressing the search icon next to the release on the releases page,
>  it pops up with a search, but doesn't automatically populate the artist name
>  and album name and has to be added in manually."
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PICKER = REPO_ROOT / "test_site/static/js/services/musicbrainz-picker.js"
UPCOMING = REPO_ROOT / "test_site/static/js/services/upcoming-releases.js"

STRAIGHT = "Through a Child's Eyes"
CURLY = "Through a Child\u2019s Eyes"


# ---------------------------------------------------------------------------
# 1. The apostrophe rename
# ---------------------------------------------------------------------------

class TestTypographyOnlyRenamesAreNotApplied:
    @staticmethod
    def _confidence():
        from services.popularity.popularity_sources import (
            _title_rename_confidence,
        )

        return _title_rename_confidence

    _COMMON = dict(
        local_duration="3:45", release_length_ms=225000,
        local_disc=1, local_position=3,
        release_disc=1, release_position=3,
        file_path="/music/x/03.flac",
    )

    def test_a_curly_apostrophe_is_not_a_rename(self) -> None:
        """⭐ THE REPORTED CASE: the only difference is U+0027 vs U+2019."""
        verdict = self._confidence()(STRAIGHT, CURLY, **self._COMMON)
        assert verdict["ok"] is False, (
            "a typography-only difference is being applied as a file rename"
        )
        assert verdict["reason"] == "identical"

    def test_it_works_in_either_direction(self) -> None:
        """The library may hold either spelling; neither may cause churn."""
        assert self._confidence()(CURLY, STRAIGHT, **self._COMMON)["ok"] is False

    def test_a_true_typo_still_renames(self) -> None:
        """The guard must not swallow a genuine correction."""
        verdict = self._confidence()("Through a Childs Eye", STRAIGHT, **self._COMMON)
        assert verdict["ok"] is True

    def test_a_different_title_still_renames(self) -> None:
        verdict = self._confidence()(
            "Through the Eyes", "Through a Child's Eyes", **self._COMMON
        )
        assert verdict["ok"] is True

    def test_case_and_spacing_alone_are_not_a_rename(self) -> None:
        verdict = self._confidence()("through a child's eyes", STRAIGHT, **self._COMMON)
        assert verdict["ok"] is False

    def test_the_review_agreees_with_the_scan(self) -> None:
        """The Lookup-MBID review must not offer what the scan now refuses."""
        from helpers.normalization_service import titles_match_for_review

        assert titles_match_for_review(STRAIGHT, CURLY) is True


# ---------------------------------------------------------------------------
# 2. The result button
# ---------------------------------------------------------------------------

class TestTheResultButtonQueuesTheRelease:
    def test_the_label_reads_add_to_queue(self) -> None:
        src = PICKER.read_text(encoding="utf-8")
        assert "Add to queue" in src

    def test_it_no_longer_renders_a_soulseek_label(self) -> None:
        src = PICKER.read_text(encoding="utf-8")
        assert "Soulseek</button>" not in src

    def test_it_posts_the_release_to_the_download_endpoint(self) -> None:
        """⭐ The unified search's Queue endpoint — not a page-local helper."""
        src = PICKER.read_text(encoding="utf-8")
        assert "/api/musicbrainz/download" in src
        assert "queue_items_only" in src

    def test_it_no_longer_depends_on_a_page_local_helper(self) -> None:
        """``downloadMbRelease`` lives in download-queue.js, absent on the
        upcoming page — which is why the old button silently did nothing."""
        src = PICKER.read_text(encoding="utf-8")
        start = src.index("mb-download-release').forEach")
        block = src[start:start + 400]
        assert "downloadMbRelease" not in block, (
            "the click still calls a helper the upcoming page never loads"
        )
        assert "queueReleaseForDownload" in block

    def test_every_outcome_is_reported(self) -> None:
        """A silent no-op is the reported symptom, so silence is the bug."""
        src = PICKER.read_text(encoding="utf-8")
        start = src.index("async function queueReleaseForDownload")
        block = src[start:start + 2600]
        assert "toastError" in block
        assert "toastSuccess" in block


# ---------------------------------------------------------------------------
# 3. The pre-fill
# ---------------------------------------------------------------------------

class TestTheUpcomingSearchIsPreFilled:
    def test_it_opens_the_shared_modal_with_both_fields(self) -> None:
        src = UPCOMING.read_text(encoding="utf-8")
        # Anchor on the CALL, not the first mention inside the docstring.
        assert "global.openGlobalMbSearch(artist || '', album || '');" in src, (
            "the search still opens the legacy modal, which ignores the "
            "artist/album it was given"
        )
        assert "typeof global.openGlobalMbSearch === 'function'" in src

    def test_it_passes_no_callback(self) -> None:
        """A callback flips the modal to MATCH, which is not what this page wants.

        The upcoming page adds releases to the queue, so the modal must stay a
        download browser — ``matchIntent`` is ``callback !== null``.
        """
        src = UPCOMING.read_text(encoding="utf-8")
        # The CALL, not the prose that describes it: the docstring above the
        # call mentions openGlobalMbSearch too, which makes a naive index() land
        # in a comment.
        call = "global.openGlobalMbSearch(artist || '', album || '');"
        assert call in src, "the pre-filled call is missing or gained an argument"

    def test_the_shared_modal_prefills_from_its_arguments(self) -> None:
        """Pins the behaviour the fix relies on."""
        src = PICKER.read_text(encoding="utf-8")
        start = src.index("function openGlobalMbSearch(")
        # Up to the NEXT top-level function, so the window cannot silently
        # truncate before the assertions below (a fixed-width slice did exactly
        # that once the docstring above the call grew).
        end = src.index("\n  /**", start)
        block = src[start:end]
        assert "values[name] || ''" in block, (
            "openGlobalMbSearch no longer seeds the form fields from its arguments"
        )
        assert "performSearch()" in block, (
            "openGlobalMbSearch no longer runs the search on open"
        )


class TestTheScanStageUsesTheSameRule:
    """⭐ THE SURFACE THE REPORT ACTUALLY CAME FROM.

    ``track_stage`` builds the "metadata requiring approval" recommendation (and
    the auto-apply) with a title comparison. It used bare ``casefold()``, which
    folds CASE ONLY — so a curly apostrophe against a straight one read as a
    "different title" on every scan.
    """

    STAGE = REPO_ROOT / "services/popularity/stages/track_stage.py"

    def test_no_bare_casefold_title_comparison_remains(self) -> None:
        src = self.STAGE.read_text(encoding="utf-8")
        assert "_release_title.casefold() != _local_title.casefold()" not in src, (
            "a title gate still compares casefold() only, so typography-only "
            "differences are still offered as renames"
        )

    def test_it_uses_the_shared_review_rule(self) -> None:
        src = self.STAGE.read_text(encoding="utf-8")
        assert "titles_match_for_review(" in src
        assert "titles_match_for_review," in src, "the helper is not imported"

    def test_the_helper_it_calls_folds_typography(self) -> None:
        from helpers.normalization_service import titles_match_for_review

        assert titles_match_for_review(
            "Through a Child's Eyes", "Through a Child\u2019s Eyes"
        ) is True

    def test_it_still_reports_a_real_title_change(self) -> None:
        from helpers.normalization_service import titles_match_for_review

        assert titles_match_for_review(
            "Through a Child's Eyes", "Through the Eyes"
        ) is False


# ---------------------------------------------------------------------------
# Structural: the files must still be valid JS
# ---------------------------------------------------------------------------

class TestMyEditsDoNotUnbalanceTheSources:
    """A delta check against git HEAD, not an absolute one.

    There is no JS runtime here, so a structural assertion is what stands
    between an unbalanced edit and a page that fails to load.

    ⚠️ ABSOLUTE BALANCE IS NOT ASSERTED ON PURPOSE. A character scanner cannot
    perfectly separate JS regex literals and nested template interpolation from
    code, and these two files already read as slightly "unbalanced" at HEAD
    under such a scanner. Asserting an absolute count would fail on code nobody
    touched. What matters for an edit is the DELTA: equal numbers of opening and
    closing delimiters added or removed.
    """

    @staticmethod
    def _counts(src: str) -> dict[str, int]:
        """Count delimiters OUTSIDE strings, template literals and comments."""
        counts = {"{": 0, "}": 0, "(": 0, ")": 0, "[": 0, "]": 0}
        i, n = 0, len(src)
        while i < n:
            if src.startswith("//", i):
                j = src.find("\n", i)
                i = n if j < 0 else j
                continue
            if src.startswith("/*", i):
                j = src.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
            c = src[i]
            if c in "'\"":
                quote = c
                i += 1
                while i < n:
                    if src[i] == "\\":
                        i += 2
                        continue
                    if src[i] == quote:
                        i += 1
                        break
                    i += 1
                continue
            if c == "`":
                i += 1
                while i < n:
                    if src[i] == "\\":
                        i += 2
                        continue
                    if src[i] == "`":
                        i += 1
                        break
                    # ${...} inside a literal IS code — count its braces, then
                    # skip past it so the literal body cannot leak out.
                    if src.startswith("${", i):
                        j = src.find("}", i)
                        seg = src[i:j + 1] if j > 0 else ""
                        counts["{"] += seg.count("{")
                        counts["}"] += seg.count("}")
                        i = (j + 1) if j > 0 else n
                        continue
                    i += 1
                continue
            if c in counts:
                counts[c] += 1
            i += 1
        return counts

    @pytest.mark.parametrize(
        "rel",
        [
            "test_site/static/js/services/musicbrainz-picker.js",
            "test_site/static/js/services/upcoming-releases.js",
        ],
    )
    def test_the_edit_added_balanced_delimiters(self, rel: str) -> None:
        import subprocess

        head = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        ).stdout
        assert head, f"could not read HEAD:{rel}"

        before = self._counts(head)
        after = self._counts((REPO_ROOT / rel).read_text(encoding="utf-8"))
        delta = {k: after[k] - before[k] for k in before}
        assert delta["{"] == delta["}"], f"{rel}: braces unbalanced by edit {delta}"
        assert delta["("] == delta[")"], f"{rel}: parens unbalanced by edit {delta}"
        assert delta["["] == delta["]"], f"{rel}: brackets unbalanced by edit {delta}"
