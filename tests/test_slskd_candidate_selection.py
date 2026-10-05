"""Why a Soulseek candidate list came back empty-handed — and which file we keep.

Two related gaps behind the reported *"download queue doesn't seem to be
matching properly"*:

1. **The cap decided what the queue could ever see.**
   ``filter_results_by_quality`` sorted by **bitrate** and kept the first 50 —
   and because every poll re-filters the *whole* result set, that truncation
   happened on the full candidate list, not per batch. For a broad fallback
   query (a bare title, the artist's first word) the top 50 by bitrate are
   rarely the file being searched for, so the scorer was handed an arbitrary
   50 and answered ``no_qualifying_result (50 candidates)`` — for days, for
   the same track. Relevance to the query now ranks first; bitrate only
   breaks ties.

2. **Nobody could tell which gate fired.** Every rejection reason was logged
   at DEBUG, so the normal log showed only "50 candidates".
   ``_select_best_result`` now summarises the counts ONCE at WARNING, with the
   top score and the best filename it saw — one line that says *year gate*
   rather than *mystery*.
"""
from __future__ import annotations

import pytest

from services.downloads import download_pipeline_service as dps
from services.downloads.slskd_service import SearchResponse, SlskdService


def _response(*files: tuple[str, int, int]) -> SearchResponse:
    """(filename, bitrate, sample_rate) triples — quality always passes."""
    return SearchResponse(
        username="peer",
        files=[
            {"filename": name, "bitRate": br, "sampleRate": sr}
            for name, br, sr in files
        ],
    )


class _FilterHarness:
    """``filter_results_by_quality`` without a live slskd session."""

    def __call__(self, *responses: SearchResponse, query: str = "", max_results: int = 50):
        return SlskdService.filter_results_by_quality(
            self, list(responses), max_results=max_results, query=query,
        )


class _RecLogger:
    def __init__(self):
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, event, **kwargs):
        self.warnings.append((str(event), kwargs))

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


# ---------------------------------------------------------------------------
# 1. Relevance first, quality second
# ---------------------------------------------------------------------------
class TestTheQueryDecidesWhatSurvivesTheCap:
    # The matching file is deliberately the QUIETER of the two: if bitrate
    # still won, this test would fail and the old ordering would be back.
    MATCHING = ("Interpol - Wake Up.flac", 900, 44100)
    OTHER = ("ZZ Top - La Grange.flac", 1411, 96000)
    QUERY = "Interpol - Wake Up"

    def test_the_matching_file_survives_a_one_result_cap(self):
        kept = _FilterHarness()(
            _response(self.MATCHING, self.OTHER),
            query=self.QUERY,
            max_results=1,
        )

        assert [f["filename"] for f in kept] == [self.MATCHING[0]], (
            "the cap must drop the LOUDEST file first, not the least relevant "
            "— this is the truncation that made broad fallback queries return "
            "50 candidates none of which matched"
        )

    def test_without_a_query_quality_still_decides(self):
        """The ranking is query-gated: a caller with no query keeps old behaviour."""
        kept = _FilterHarness()(
            _response(self.MATCHING, self.OTHER),
            query="",
            max_results=1,
        )

        assert [f["filename"] for f in kept] == [self.OTHER[0]]

    def test_relevance_is_scored_highest_first_across_many(self):
        kept = _FilterHarness()(
            _response(
                ("Interpol - Wake Up (Live).flac", 800, 44100),
                self.OTHER,
                ("Interpol - NYC.flac", 1411, 96000),
                self.MATCHING,
            ),
            query=self.QUERY,
            max_results=2,
        )
        names = [f["filename"] for f in kept]

        assert names[0] == self.MATCHING[0], (
            "3 query tokens beat 1, whatever the bitrate"
        )
        assert len(names) == 2

    def test_the_quality_filter_still_applies(self):
        """Relevance must not smuggle in a file below the bitrate floor."""
        kept = _FilterHarness()(
            _response(("Interpol - Wake Up.mp3", 128, 44100), self.OTHER),
            query=self.QUERY,
            max_results=10,
        )

        assert [f["filename"] for f in kept] == [self.OTHER[0]]


# ---------------------------------------------------------------------------
# 2. The failure must say WHY
# ---------------------------------------------------------------------------
class TestTheFailureSaysWhichGateFired:
    ARTIST = "Interpol"
    TITLE = "Wake Up"

    def _select(self, filenames, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(dps, "logger", rec)
        results = [{"filename": f} for f in filenames]
        best = dps._select_best_result(
            results, expected_artist=self.ARTIST, expected_title=self.TITLE,
        )
        return best, rec

    def test_no_candidate_qualifies_and_the_reason_is_logged(self, monkeypatch):
        # No artist anywhere in these names, and titles that match nothing:
        # the artist-evidence gate rejects them before the title gate runs.
        best, rec = self._select(
            ["01 - Some Random Song.flac", "02 - Another One.flac"],
            monkeypatch,
        )

        assert best is None
        assert rec.warnings, "a rejected batch must produce ONE warning"
        event, fields = rec.warnings[-1]
        assert "no qualifying" in event.lower()
        assert fields["candidates"] == 2
        assert fields["rejected"].get("no_artist_evidence") == 2, (
            f"the warning must name the gate, got {fields['rejected']!r}"
        )
        assert fields["min_score"] == 45.0
        assert fields["top_candidate"], "show the best filename we saw"

    def test_a_candidate_below_the_floor_is_counted_separately(self, monkeypatch):
        """A candidate that scored — just not high enough — is NOT a gate reject.

        The two point at different fixes, so the summary has to separate them.
        Raising the floor is the only thing that makes this case deterministic:
        the candidate genuinely qualifies, nothing about it was rejected.
        """
        rec = _RecLogger()
        monkeypatch.setattr(dps, "logger", rec)

        best = dps._select_best_result(
            [{"filename": "Interpol - Wake Up.flac"}],
            expected_artist=self.ARTIST,
            expected_title=self.TITLE,
            min_score=999,
        )

        assert best is None
        fields = rec.warnings[-1][1]
        assert fields["rejected"].get("below_floor") == 1, (
            f"expected the below-floor count, got {fields['rejected']!r}"
        )
        assert not any(
            k for k in fields["rejected"] if k != "below_floor"
        ), "nothing was hard-rejected here"
        assert 0 < fields["top_score"] < 999

    def test_a_qualifying_candidate_returns_without_warning(self, monkeypatch):
        rec = _RecLogger()
        monkeypatch.setattr(dps, "logger", rec)

        best = dps._select_best_result(
            [{"filename": "Interpol - Wake Up.flac"}],
            expected_artist=self.ARTIST,
            expected_title=self.TITLE,
        )

        assert best is not None, "the plain happy path must still select"
        assert rec.warnings == [], "no warning when something qualified"

    def test_the_scorer_keeps_its_old_signature(self, monkeypatch):
        """`rejects` is optional — every existing caller passes five args."""
        monkeypatch.setattr(dps, "logger", _RecLogger())
        score = dps._score_result(
            {"filename": "01 - Nope.flac"}, self.ARTIST, self.TITLE,
        )
        assert score == 0.0
