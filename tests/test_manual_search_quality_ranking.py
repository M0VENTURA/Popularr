"""Manual Soulseek search: ranked like the automated search, config quality.

THE REPORT
----------
"Doing a manual search for soulseek returned a lot of results. Just checking
that an automated search works in a similar way to searching and matching —
selecting the 320 peer with open slots."

The manual list was rendered in **slskd's raw response order**: no relevance
ranking, no quality ordering, no slots column. The automated path ranks
``relevance → free upload slot → bitrate → sample rate → queue length →
upload speed`` and gates on a quality minimum, so the two views could disagree
about which peer is best — and the 320 kbps open-slot peer could sit anywhere
in 50 rows.

These tests pin the fix:

* the route reads the quality minimum from ``downloads.quality`` (config), not a
  hardcoded 192/44100;
* it ranks with the SAME ``rank_search_results`` the automated path uses
  (``SlskdService._rank_qualified`` is a thin delegate to it), so the two
  cannot drift;
* files meeting the minimum are listed FIRST, and the rest are KEPT below (a
  manual search exists to find what the automated one could not);
* the quality counts are reported so the UI can explain the order;
* all three renderers show a Slots column.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTE = REPO_ROOT / "routes" / "download_search_routes.py"
SERVICE = REPO_ROOT / "services" / "downloads" / "slskd_service.py"

RENDERERS = (
    ("live search tab", REPO_ROOT / "static" / "js" / "downloads.js"),
    ("test_site", REPO_ROOT / "test_site" / "static" / "js" / "services" / "slskd.js"),
)


def _route() -> str:
    return ROUTE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The quality minimum comes from CONFIG, not a hardcoded constant
# ---------------------------------------------------------------------------

class TestQualityMinimumComesFromConfig:

    def test_the_route_reads_the_quality_config(self) -> None:
        src = _route()
        assert "get_search_quality_config" in src, (
            "the manual search must use the configured quality minimum, not a "
            "hardcoded 192/44100 that drifts from the automated path"
        )
        assert "from helpers.config_helpers import get_config, get_search_quality_config" in src

    def test_the_three_configured_thresholds_are_used(self) -> None:
        src = _route()
        for key in ("min_bitrate", "min_sample_rate", "min_length_seconds"):
            assert f'_quality_cfg.get("{key}")' in src, f"{key} must come from config"

    def test_it_reuses_matches_quality(self) -> None:
        """One quality rule, not a second hand-written copy."""
        src = _route()
        assert "_file_meets_quality(" in src
        helper = src[src.index("def _file_meets_quality("):]
        helper = helper[: helper.index("# ====")]
        assert "file.matches_quality(min_bitrate, min_sample_rate)" in helper, (
            "the manual path must apply the SAME quality rule as the automated "
            "one (which treats a missing attribute as UNKNOWN, not a failure)"
        )


# ---------------------------------------------------------------------------
# 2. It ranks with the automated ranking
# ---------------------------------------------------------------------------

class TestRankedLikeTheAutomatedSearch:

    def test_it_calls_the_shared_ranker(self) -> None:
        src = _route()
        assert "rank_search_results(results, _query)" in src, (
            "the manual list must use the automated ranking, or the two views "
            "disagree about which peer is best"
        )
        assert "from services.downloads.slskd_service import" in src

    def test_the_ranker_is_ordered_relevance_then_slots_then_bitrate(self) -> None:
        src = SERVICE.read_text(encoding="utf-8")
        idx = src.index("def rank_search_results(")
        block = src[idx: idx + 3600]
        # The tuple order IS the contract.
        assert "-relevance" in block
        assert "has_free_upload_slot" in block
        assert '-int(item.get("bitrate")' in block
        assert block.index("-relevance") < block.index("has_free_upload_slot"), (
            "relevance must lead, or a well-named wrong file outranks the track"
        )
        assert block.index("has_free_upload_slot") < block.index('"bitrate"'), (
            "an open-slot peer must outrank a marginally higher bitrate"
        )

    def test_the_staticmethod_delegates_to_the_module_ranker(self) -> None:
        """One implementation; callers may come through the class or the module."""
        src = SERVICE.read_text(encoding="utf-8")
        assert "return rank_search_results(items, query)" in src, (
            "the service's own staticmethod must delegate, or two rankers drift"
        )

    def test_the_query_used_for_ranking_is_the_searches_own(self) -> None:
        src = _route()
        assert '(_manual_search_state.get(search_id) or {}).get("query")' in src, (
            "relevance needs the query the user actually searched for"
        )


# ---------------------------------------------------------------------------
# 3. Qualifying files lead; nothing is hidden
# ---------------------------------------------------------------------------

class TestQualifyingFirstButNothingHidden:

    def test_qualifying_files_are_partitioned_first(self) -> None:
        src = _route()
        assert 'qualifying = [r for r in ranked if r.get("meets_quality")]' in src
        assert 'below_quality = [r for r in ranked if not r.get("meets_quality")]' in src
        assert "ordered = qualifying + below_quality" in src

    def test_below_quality_files_are_kept_not_dropped(self) -> None:
        """A manual search is what you reach for when the automated one failed —
        hiding the only 128 kbps copy of a rare track would defeat the point."""
        src = _route()
        assert "ordered = qualifying + below_quality" in src
        assert '"results": ordered' in src, "the response must carry the full list"

    def test_it_marks_each_result(self) -> None:
        src = _route()
        assert '"meets_quality": bool(meets)' in src, (
            "the flag is what drives the partition; without it the ordering "
            "silently reverts to raw response order"
        )

    def test_the_length_gate_only_fires_when_length_is_known(self) -> None:
        """An unknown length is UNKNOWN, matching matches_quality's convention."""
        src = _route()
        helper = src[src.index("def _file_meets_quality("):]
        helper = helper[: helper.index("# ====")]
        assert "if meets and min_length:" in helper
        assert "if length and length < min_length:" in helper, (
            "a missing length must not fail the gate"
        )


class TestOneOddFileCannotEmptyTheList:
    """The boundary rule: an unknown file is KEPT, never fatal.

    Found by running the EXISTING ``test_soulseek_free_slot_filter.py``: the
    first version called ``file.matches_quality(...)`` directly, so a file
    object without that method raised and the whole route fell into its
    ``except`` — the user's manual search returned ZERO results when slskd had
    returned two.  A file of uncertain quality last beats an empty list.
    """

    def test_a_raising_quality_check_keeps_the_file(self) -> None:
        from routes.download_search_routes import _file_meets_quality

        class _NoHelper:
            filename = "a.mp3"

        assert _file_meets_quality(_NoHelper(), 192, 44100, 30) is True, (
            "a file whose quality cannot be determined must be KEPT, not "
            "allowed to empty the entire result list"
        )

    def test_a_missing_length_is_not_read_as_zero(self) -> None:
        from routes.download_search_routes import _file_meets_quality

        class _NoLength:
            def matches_quality(self, *_a, **_k):
                return True

        assert _file_meets_quality(_NoLength(), 192, 44100, 30) is True

    def test_the_helper_defers_to_matches_quality(self) -> None:
        from routes.download_search_routes import _file_meets_quality

        class _File:
            def __init__(self, ok, length=200):
                self._ok = ok
                self.length = length
                self.calls = []

            def matches_quality(self, bitrate, sample_rate):
                self.calls.append((bitrate, sample_rate))
                return self._ok

        # The CONFIGURED thresholds must be the ones passed through.
        f = _File(True)
        assert _file_meets_quality(f, 320, 48000, 0) is True
        assert f.calls == [(320, 48000)]

    def test_a_short_file_fails_the_length_gate(self) -> None:
        from routes.download_search_routes import _file_meets_quality

        class _File:
            length = 10

            def matches_quality(self, *_a, **_k):
                return True

        assert _file_meets_quality(_File(), 192, 44100, 30) is False

    def test_the_length_gate_is_off_at_zero(self) -> None:
        from routes.download_search_routes import _file_meets_quality

        class _File:
            length = 10

            def matches_quality(self, *_a, **_k):
                return True

        assert _file_meets_quality(_File(), 192, 44100, 0) is True


# ---------------------------------------------------------------------------
# 4. The response reports what the list was judged against
# ---------------------------------------------------------------------------

class TestQualityMetadataIsReported:

    def test_the_payload_carries_the_thresholds_and_counts(self) -> None:
        src = _route()
        assert '"quality": {' in src
        for key in ('"min_bitrate"', '"min_sample_rate"', '"min_length_seconds"',
                    '"qualifying"', '"total"'):
            assert key in src, f"{key} must be reported so the UI can explain the order"

    def test_a_fallback_flag_is_reported(self) -> None:
        src = _route()
        assert '"qualityFallback": bool(count) and not qualifying' in src, (
            "when nothing meets the minimum the UI must say so rather than "
            "silently showing a list of low-bitrate files"
        )


# ---------------------------------------------------------------------------
# 5. The renderers show slots and the quality note
# ---------------------------------------------------------------------------

class TestRenderers:

    @pytest.mark.parametrize("label,path", RENDERERS)
    def test_a_slots_column_exists(self, label, path) -> None:
        js = path.read_text(encoding="utf-8")
        # ⚠️ `>Slots<` not `"Slots"` — the latter also matches the
        # `freeUploadSlots` field name, so it passed with the column removed.
        assert ">Slots<" in js, (
            f"{label}: a slots column is what makes the ranking visible — "
            "1 vs 8 slots matters when picking a peer by hand"
        )

    def test_both_live_renderers_have_the_column(self) -> None:
        js = (REPO_ROOT / "static" / "js" / "downloads.js").read_text(encoding="utf-8")
        # The search tab and the manual modal are separate renderers.
        assert js.count('data-label="Slots"') >= 2, (
            "both live renderers (search tab + manual modal) need the column"
        )

    def test_a_missing_slot_count_is_unknown_not_zero(self) -> None:
        for label, path in RENDERERS:
            js = path.read_text(encoding="utf-8")
            assert "has_free_upload_slot === false ? 0 : null" in js, (
                f"{label}: an unknown slot count must not render as 0 (which "
                "reads as 'peer is full')"
            )

    def test_the_session_captures_the_quality_payload(self) -> None:
        js = (REPO_ROOT / "test_site" / "static" / "js" / "services" / "slskd.js").read_text(
            encoding="utf-8"
        )
        assert "session.quality = data.quality" in js
        assert "session.qualityFallback = !!data.qualityFallback" in js

    def test_the_quality_note_explains_the_order(self) -> None:
        js = (REPO_ROOT / "test_site" / "static" / "js" / "services" / "slskd.js").read_text(
            encoding="utf-8"
        )
        assert "function qualityNote(" in js
        idx = js.index("function qualityNote(")
        block = js[idx: idx + 900]
        assert "quality minimum" in block
        assert "listed first" in block, (
            "the note must say qualifying files lead, or the order looks random"
        )
        assert "showing all" in block, (
            "and must be explicit when NOTHING met the minimum"
        )
