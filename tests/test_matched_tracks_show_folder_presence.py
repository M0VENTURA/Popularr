"""The matched-folder tracklist must show WHICH tracks the folder has.

REPORTED
--------
> When matching an album on the downloads queue, it doesn't show which tracks
> on the release have been matched from files, it just shows the tracklist of
> all tracks.

ROOT CAUSE
----------
The ``<details>`` expander built by ``matchedReleaseHtml`` renders the
**release's** tracklist from
``GET /api/downloads/folder/match-tracklist?release_mbid=…`` and labels it
``Matched album tracks (N)`` where ``N`` is the release's own size. Nothing
marked a row as present or absent, so a folder holding 3 of 11 tracks claimed
11 indistinguishable matches — and the count read as a claim about the folder
when it was a fact about the release.

The folder's file list was already on the row (``folder.files``, from
``_get_files_in_folder`` → ``{"name": <relative path>, "size": …}``); it was
simply never consulted.

WHAT CHANGED
------------
* ``trackListHtml(titles, keys)`` is now the single renderer for both the
  initial (cached) render and the async one, marking each row present/absent.
* The summary reads ``Matched album tracks (X/Y in this folder)`` — X counted
  against the FOLDER's files, Y being the release size.
* ``_normaliseTrackKey`` reduces a file to a comparable key: basename → strip
  extension → strip a leading track number → strip a trailing bitrate →
  collapse whitespace → lowercase.

Matching is **normalised EXACT**, never substring. ``"01 - Enemies"`` reduces
to ``enemies`` which equals the release title ``Enemies``; a substring test
would report ``Die`` as present because ``Dieter`` contains it, which is worse
than showing nothing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

MONITOR = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "monitor.js"


def _source() -> str:
    return MONITOR.read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """The body of a top-level ``function <name>`` in the module."""
    source = _source()
    start = source.index(f"function {name}(")
    end = source.index("\n  }", start)
    return source[start:end]


class TestTheTracklistMarksWhatTheFolderHas:
    def test_a_single_renderer_is_used_by_both_paths(self):
        """Cached-first render and the async render must not drift apart."""
        source = _source()
        assert source.count("trackListHtml(") >= 3, (
            "both render sites must go through trackListHtml, or one of them "
            "keeps showing the unmarked list"
        )
        # The two inline lists this replaced are gone.
        assert ".map((t) => `<li>${esc(t)}</li>`)" not in source, (
            "an unmarked inline list is still rendered somewhere"
        )

    def test_present_and_absent_rows_are_visually_distinct(self):
        body = _fn("trackListHtml")
        assert "text-success" in body, "a track the folder has must be marked"
        assert "text-muted" in body, "a track the folder lacks must be dimmed"
        assert "bi-check2" in body
        assert "bi-dash" in body, (
            "an absent track needs its own affordance, not just a colour"
        )

    def test_the_summary_counts_the_folder_not_the_release(self):
        source = _source()
        assert "in this folder" in source, (
            "the label must say whose tracks are being counted — the old "
            "number was the RELEASE size and read as a claim about the folder"
        )
        assert "${list.matched}/${list.total}" in source
        assert "`Matched album tracks (${tracks.length})`" not in source, (
            "the bare release size is the reported bug"
        )

    def test_the_async_path_updates_the_summary_too(self):
        """The tracklist loads lazily — the label must update when it lands."""
        fn = _fn("loadMatchedTracklist")
        assert "list.matched" in fn and "list.total" in fn
        assert "Matched album tracks" in fn

    def test_the_async_path_reads_the_owning_folder(self):
        """A release can be matched by several folders; each keeps its answer."""
        fn = _fn("loadMatchedTracklist")
        assert "data-folder-key" in fn, (
            "the async render must recover WHICH folder it belongs to"
        )

    def test_the_row_supplies_that_key(self):
        source = _source()
        assert 'data-folder-key="${esc(folder.name || \'\')}"' in source

    def test_keys_are_keyed_by_folder_path(self):
        source = _source()
        assert "folderTrackKeys[folder.name] = matchedTrackKeys(folder)" in source


class TestMatchingIsExactNotSubstring:
    def test_the_lookup_is_an_exact_membership_test(self):
        for name in ("_normaliseTrackKey", "countMatched", "trackListHtml"):
            body = _fn(name)
            assert ".includes(" not in body, (
                f"{name} matched by substring — 'Die' would count as present "
                "for 'Dieter'"
            )
            assert ".indexOf(" not in body

    def test_the_key_normalisation_strips_the_file_shape(self):
        body = _fn("_normaliseTrackKey")
        # basename — ``files[].name`` is a relative PATH, so it must be reduced
        # before anything else compares. Raw string: the value IS ``^.*[\\/]``.
        assert r"^.*[\\/]" in body, "the basename must be stripped"
        # extension
        assert r"\.[a-z0-9]{2,5}$" in body
        # leading track number — the part that makes "01 - Enemies" == "Enemies"
        assert r"^\s*\d{1,3}\s*[-–.—_]\s*" in body
        # case
        assert "toLowerCase()" in body

    def test_each_side_is_normalised_exactly_once(self):
        """The FILE side is normalised when the key set is built (once per
        folder, not once per title); only the release title is normalised
        inside the comparison. Asserting both inside one function would be
        wrong about where the work happens."""
        build = _fn("matchedTrackKeys")
        assert "_normaliseTrackKey(file && file.name)" in build, (
            "the folder's files must be reduced to comparable keys"
        )
        compare = _fn("countMatched")
        assert "_normaliseTrackKey(title)" in compare, (
            "the release title must be reduced with the SAME normaliser"
        )


class TestTheControlsStillHold:
    def test_a_folder_without_a_match_renders_no_block(self):
        source = _source()
        idx = source.index("function matchedReleaseHtml")
        window = source[idx: idx + 700]
        assert "if (!releaseId) return '';" in window, (
            "a folder with no association must still render exactly as before"
        )

    def test_an_empty_folder_renders_the_fallback(self):
        source = _source()
        assert "Show matched album tracks" in source, (
            "the collapsed summary before the tracklist loads must survive"
        )
        assert "No tracklist found for this release." in source

    def test_the_release_identity_block_is_untouched(self):
        source = _source()
        assert "Matched to" in source
        assert "releaseId" in source

    def test_the_tracklist_endpoint_is_still_called(self):
        source = _source()
        assert "/api/downloads/folder/match-tracklist?release_mbid=" in source

    def test_failures_are_still_retryable(self):
        fn = _fn("loadMatchedTracklist")
        assert "click to retry" in fn, (
            "a failed load must not be cached as a dead end"
        )
