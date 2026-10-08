"""The release picker must not offer a choice that isn't one.

REPORTED
--------
> When selecting a release to add to soulseek, it pops up and asks for the
> specific release to download. For all releases that have the same amount of
> tracks, can they be merged into a single release to pick, or if all releases
> have the same amount of tracks, then auto select the best release so the pop
> up doesn't come up.

Two halves, both pinned here:

1. **Merge** — the flyout lists every pressing of a release group, so a group
   whose candidates all carry the same track count showed rows differing only
   by country/date/format. ``_picker_group_editions`` folds those into ONE
   choice, labelled with how many editions it stands for.
2. **Auto-select** — when the WHOLE group is indistinguishable that way,
   ``openReleasePicker`` queues the best candidate directly instead of opening
   the flyout at all. It already did this for a genuine single-release group;
   this extends it to "one track count across every candidate".

Both halves refuse to guess about *unknown* counts: a track count of 0 means
the media did not load, which is unidentified rather than identical.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

MAIN_JS = REPO_ROOT / "test_site" / "static" / "js" / "main.js"
ROUTES = REPO_ROOT / "routes" / "musicbrainz_routes.py"


def _group(processed):
    from routes.musicbrainz_routes import _picker_group_editions

    return _picker_group_editions(processed)


def _rel(track_count, *, rid="r", status="Official"):
    return {"id": rid, "title": "Album", "track_count": track_count, "status": status}


# ---------------------------------------------------------------------------
# 1. The merge
# ---------------------------------------------------------------------------


class TestEditionsWithTheSameTrackCountMerge:
    def test_equal_counts_collapse_to_one_row(self):
        grouped = _group([
            _rel(12, rid="us"),
            _rel(12, rid="jp"),
            _rel(12, rid="eu"),
        ])
        assert len(grouped) == 1, f"3 identical candidates stayed as {len(grouped)} rows"
        assert grouped[0]["id"] == "us", "the FIRST (already-ranked) release must represent the group"
        assert grouped[0]["edition_count"] == 3

    def test_the_representative_is_the_ranked_best_not_an_arbitrary_member(self):
        """The endpoint sorts Official first, then by track count — grouping
        must not throw that ranking away."""
        grouped = _group([_rel(12, rid="official"), _rel(12, rid="bootleg", status="Bootleg")])
        assert grouped[0]["id"] == "official"

    def test_different_counts_stay_separate(self):
        grouped = _group([_rel(12, rid="a"), _rel(10, rid="b"), _rel(12, rid="c")])
        assert [r["id"] for r in grouped] == ["a", "b"]
        assert grouped[0]["edition_count"] == 2
        assert grouped[1]["edition_count"] == 1

    def test_unknown_counts_are_never_merged(self):
        """0 means the media did not load — unidentified, not identical.

        Folding two uncountable releases together would hide a real choice
        behind a false equivalence, which is worse than the noise it removes.
        """
        grouped = _group([_rel(0, rid="x"), _rel(0, rid="y"), _rel(12, rid="z")])
        assert [r["id"] for r in grouped] == ["x", "y", "z"], (
            "releases with no track count must each stay pickable"
        )
        assert all(r["edition_count"] == 1 for r in grouped)

    def test_the_input_is_not_mutated(self):
        """The JSON response uses the same list — grouping must copy."""
        processed = [_rel(12, rid="a"), _rel(12, rid="b")]
        _group(processed)
        assert len(processed) == 2, "grouping mutated the caller's release list"
        assert "edition_count" not in processed[0], (
            "edition_count must live on the group row, not the source release"
        )

    def test_an_empty_group_is_harmless(self):
        assert _group([]) == []

    def test_the_renderer_actually_uses_it(self):
        """Guards against grouping computed and then discarded."""
        source = ROUTES.read_text(encoding="utf-8")
        assert "releases=_picker_group_editions(processed)" in source, (
            "the flyout must render the GROUPED list, not the raw one"
        )
        assert "releases=processed,\n        album=album," not in source

    def test_the_grouped_card_says_how_many_it_folds(self):
        source = ROUTES.read_text(encoding="utf-8")
        assert "rel.edition_count > 1" in source
        assert "editions" in source, (
            "a folded row must say it stands for several editions, or the user "
            "cannot tell their choice was narrowed for them"
        )


# ---------------------------------------------------------------------------
# 2. The auto-select
# ---------------------------------------------------------------------------


def _picker_source() -> str:
    return MAIN_JS.read_text(encoding="utf-8")


class TestOneTrackCountQueuesWithoutAsking:
    def test_the_auto_select_block_exists(self):
        source = _picker_source()
        assert "counts.size === 1" in source, (
            "openReleasePicker must detect an all-identical track count"
        )
        assert "queued the best match" in source, (
            "the silent skip must be announced, or the missing flyout looks "
            "like a broken button"
        )

    def test_it_only_fires_when_every_count_matches(self):
        source = _picker_source()
        idx = source.index("counts.size === 1")
        assert "!counts.has(0)" in source[idx: idx + 200], (
            "a group whose counts are all 0 is UNIDENTIFIED, not identical — "
            "auto-selecting it would queue an arbitrary release"
        )

    def test_it_queues_the_ranked_first_release(self):
        source = _picker_source()
        idx = source.index("counts.size === 1")
        window = source[idx: idx + 600]
        assert "const best = releases[0]" in window, (
            "the endpoint already ranks Official-then-track-count, so [0] is "
            "the best candidate"
        )
        assert "queueSpecificRelease(best.id" in window

    def test_the_flyout_still_opens_when_counts_differ(self):
        """CONTROL — a real choice must still be offered."""
        source = _picker_source()
        # Scope to this function: the file calls openSlideOver from elsewhere
        # too, so a whole-file index finds an unrelated call and reports the
        # flyout as unreachable.
        fn = source[source.index("async function openReleasePicker"):]
        fn = fn[: fn.index("\n  }")]
        assert fn.count("openSlideOver(url, 'Select Version: '") == 1
        idx = fn.index("counts.size === 1")
        assert fn.index("openSlideOver(url,") > idx, (
            "the slide-over must remain reachable after the auto-select block"
        )
        assert "return queueSpecificRelease(best.id" in fn[:fn.index("openSlideOver(url,")], (
            "the auto-select must RETURN, or every group would also open the flyout"
        )

    def test_a_single_release_still_queues_directly(self):
        """CONTROL — the pre-existing one-release path is untouched."""
        source = _picker_source()
        assert "if (releases && releases.length === 1) {" in source

    def test_the_reason_is_announced_only_after_a_successful_queue(self):
        """Announcing first would claim a queue the API then refused.

        ``queueSpecificRelease`` returns early on a refused/empty response, so
        the note has to sit after ``toast.queued``.
        """
        source = _picker_source()
        fn = source[source.index("async function queueSpecificRelease"):]
        fn = fn[: fn.index("\n  }")]
        queued_at = fn.index("global.toast.queued(releaseTitle)")
        note_at = fn.index("if (note && global.toast")
        assert note_at > queued_at, (
            "the note must come after the queue is confirmed"
        )
        assert "return;" in fn[:note_at] or "nothingQueued" in fn[:note_at], (
            "early returns for a refused queue must precede the note"
        )

    def test_the_extra_argument_is_optional(self):
        """The server-rendered onclick passes three args — it must keep working."""
        source = _picker_source()
        assert "async function queueSpecificRelease(releaseId, releaseTitle, artist, note)" in source
        assert "queueSpecificRelease(\"{{ rel.id }}\"" not in source, (
            "sanity: that string lives in the Python template, not here"
        )


# ---------------------------------------------------------------------------
# 3. The two halves must agree
# ---------------------------------------------------------------------------


class TestTheHalvesAgree:
    def test_both_sides_use_track_count(self):
        server = ROUTES.read_text(encoding="utf-8")
        client = _picker_source()
        assert '"track_count"' in server
        assert "track_count" in client, (
            "the client keys its decision on the field the endpoint supplies"
        )

    def test_the_json_payload_is_not_grouped(self):
        """The client needs the RAW list to judge whether every count matches;
        only the rendered flyout is folded."""
        source = ROUTES.read_text(encoding="utf-8")
        json_block = source[source.index('if request.args.get("format"'):]
        json_block = json_block[: json_block.index("return await _render")]
        assert '"releases": processed' in json_block, (
            "grouping the JSON too would make every group look like one "
            "release and silently lose the candidate list"
        )
        assert "_picker_group_editions" not in json_block
