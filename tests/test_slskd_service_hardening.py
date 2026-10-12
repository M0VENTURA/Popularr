"""slskd service hardening — quality gate, defensive parsing, search polling
semantics and the server-state gate (P0/P1 from the slskd review).

Each class pins one review finding:

- 2.1  missing sampleRate/bitRate is UNKNOWN, not a rejection; per-reason counts
- 2.5  the empty-search abort uses RAW results, never qualified ones
- 2.6  a final drain always runs (late arrivals are not lost)
- 2.7  polls accumulate uncapped; the cap is applied once at the end
- 2.9/2.10 download_file funnels through one enqueue; size 0 is refused;
       remote paths pass through UNCHANGED
- 2.11 queue position 0 is a real answer; queueLength is not a fallback
- 9    a disconnected client means search_and_filter returns None (distinct
       from [] — the queue must not record no_results/backoff)
- 10   transfer parsing dedupes nested + top-level entries; `path` is not
       localFilePath
- P1-4 tokeniser preserves non-ASCII; basename matches outrank folder matches
- defensive parsing: one bad file never drops the whole response
"""

from __future__ import annotations

import pytest

from services.downloads.slskd_service import (
    SearchFile,
    SearchResponse,
    SlskdService,
    _extension_from_filename,
)


@pytest.fixture(autouse=True)
def _reset_owned():
    SlskdService.reset_owned_searches()
    yield
    SlskdService.reset_owned_searches()


# ---------------------------------------------------------------------------
# 2.1 Quality gate: unknown ≠ reject
# ---------------------------------------------------------------------------

class TestQualityGateUnknownAttributes:

    def test_lossy_file_without_sample_rate_passes(self):
        f = SearchFile(filename="Band - Song.mp3", size=1, bitrate=320, sample_rate=0, length=300)
        assert f.matches_quality(min_bitrate=192) is True

    def test_lossless_file_without_sample_rate_passes(self):
        f = SearchFile(filename="Band - Song.flac", size=1, bitrate=900, sample_rate=0, length=300)
        assert f.matches_quality() is True

    def test_lossy_file_without_bitrate_passes(self):
        f = SearchFile(filename="Band - Song.mp3", size=1, bitrate=0, sample_rate=44100, length=300)
        assert f.matches_quality() is True

    def test_clearly_below_min_bitrate_still_rejected(self):
        f = SearchFile(filename="Band - Song.mp3", size=1, bitrate=96, sample_rate=44100, length=300)
        assert f.matches_quality(min_bitrate=192) is False

    def test_locked_files_are_rejected(self):
        f = SearchFile(filename="Band - Song.mp3", size=1, bitrate=320, sample_rate=44100, length=300, is_locked=True)
        assert f.matches_quality() is False

    def test_rejections_are_counted_by_reason(self):
        f = SearchFile(filename="Band - Song.mp3", size=1, bitrate=96, sample_rate=0, length=300)
        reasons: dict[str, int] = {}
        assert f.matches_quality(min_bitrate=192, reject_reasons=reasons) is False
        assert reasons.get("bitrate_below_min") == 1

    def test_locked_counting(self):
        f = SearchFile(filename="Band - Song.mp3", size=1, bitrate=320, sample_rate=44100, length=300, is_locked=True)
        reasons: dict[str, int] = {}
        assert f.matches_quality(reject_reasons=reasons) is False
        assert reasons.get("locked") == 1


# ---------------------------------------------------------------------------
# 7 + defensive parsing (one bad file must not drop the whole response)
# ---------------------------------------------------------------------------

class TestExtensionAndDefensiveParsing:

    def test_extension_derived_from_filename(self):
        f = SearchFile(filename="Band - Song.FLAC", size=1, bitrate=0, sample_rate=0, length=300)
        assert f.extension == "flac"
        assert f.is_lossless is True

    def test_ape_and_wv_are_lossless(self):
        for ext in ("ape", "wv"):
            f = SearchFile(filename=f"Band - Song.{ext}", size=1, bitrate=0, sample_rate=0, length=300)
            assert f.is_lossless is True, ext
            assert f.matches_quality() is True, ext

    def test_m4a_is_not_treated_as_lossless(self):
        f = SearchFile(filename="Band - Song.m4a", size=1, bitrate=128, sample_rate=44100, length=300)
        assert f.is_lossless is False

    def test_a_non_numeric_sample_rate_does_not_crash(self):
        f = SearchFile(filename="Band - Song.mp3", size="x", bitrate="320", sample_rate="lots", length="300")
        assert f.size == 0 and f.bitrate == 320 and f.sample_rate == 0 and f.length == 300

    def test_a_mixed_files_list_keeps_the_valid_entries(self):
        resp = SearchResponse(username="peer", files=[
            {"filename": "ok.flac", "size": 1, "bitRate": 900, "sampleRate": 96000, "length": 100},
            "garbage",
            None,
            {"filename": "ok2.mp3", "size": 2, "bitRate": 320, "sampleRate": "nope", "length": 200},
        ])
        assert len(resp.files) == 2
        assert resp.files[0].filename == "ok.flac"
        assert resp.files[1].sample_rate == 0

    def test_extension_helper(self):
        assert _extension_from_filename("Music and More/Band - Song.flac") == "flac"
        assert _extension_from_filename("no-extension") == ""


# ---------------------------------------------------------------------------
# 2.7 Cap once, never per snapshot
# ---------------------------------------------------------------------------

class TestCapOnce:

    def test_uncapped_mode_returns_everything(self):
        svc = SlskdService(http_client=_NoHttp())
        files = [{"filename": f"f{i}.mp3", "bitRate": 200, "sampleRate": 44100, "size": i, "length": i} for i in range(70)]
        resp = SearchResponse(username="peer", files=files)
        out = svc.filter_results_by_quality([resp], query="f", max_results=None)
        assert len(out) == 70

    def test_default_cap_still_applies_for_direct_callers(self):
        svc = SlskdService(http_client=_NoHttp())
        files = [{"filename": f"f{i}.mp3", "bitRate": 200, "sampleRate": 44100, "size": i, "length": i} for i in range(70)]
        resp = SearchResponse(username="peer", files=files)
        assert len(svc.filter_results_by_quality([resp], query="f")) == 50


# ---------------------------------------------------------------------------
# 2.5 / 2.6 / 9 — search_and_filter polling semantics
# ---------------------------------------------------------------------------

class _NoHttp:
    enabled = True


class _FeedHttp(_NoHttp):
    """Canned responses per get_search_results call."""

    def __init__(self):
        self.enabled = True
        self.entries: list[tuple[list, str, bool]] = []
        self.calls = 0

    def get_search_results(self, search_id, timeout=None):
        self.calls += 1
        if self.entries:
            return self.entries.pop(0)
        return ([], "InProgress", False)


class _SearchableSvc:
    """SlskdService wired to the feed with the fake collaborators pre-patched."""

    def __init__(self, http):
        self.svc = SlskdService(http_client=http)
        self.svc.maybe_clear_stale_searches = lambda budget_seconds=6: False
        self.svc.start_search = lambda query, timeout=None: "s1"
        self.svc.is_connected = lambda: True
        self.cancelled: list[str] = []
        self.svc.cancel_search = lambda search_id, timeout=None: self.cancelled.append(str(search_id)) or True
        self.svc.get_search_results = http.get_search_results


def _qualify_file(name: str) -> dict:
    return {"username": "peer", "filename": name, "size": 1, "size_mb": 0.0,
            "bitrate": 320, "sample_rate": 44100, "bit_depth": None, "extension": "flac",
            "is_lossless": True, "duration": "3:00", "length_seconds": 180,
            "has_free_upload_slot": True, "upload_speed": None, "queue_length": None}


def _response(*names: str) -> SearchResponse:
    """A real SearchResponse with normal-quality files (feed entries)."""
    return SearchResponse(username="peer", files=[
        {"filename": n, "size": 1, "bitRate": 320, "sampleRate": 44100, "length": 180}
        for n in names
    ])


class TestSearchAndFilterPolling:

    def test_disconnected_client_returns_none(self):
        svc = SlskdService(http_client=_NoHttp())
        svc.is_connected = lambda: False
        assert svc.search_and_filter("anything", wait_seconds=0) is None

    def test_raw_results_prevent_the_early_abort(self):
        """A search streaming files that ALL fail quality is NOT empty."""
        http = _FeedHttp()
        # One response with a LOCKED file (fails quality) and nothing else.
        locked = SearchResponse(username="peer", files=[
            {"filename": "Band - Song.mp3", "size": 1, "bitRate": 320, "sampleRate": 44100, "length": 180, "isLocked": True},
        ])
        http.entries = [
            # raw files present -> the abort must NOT fire, despite zero qualified
            ([locked], "InProgress", True),
            ([], "InProgress", False),
        ]
        holder = _SearchableSvc(http)
        out = holder.svc.search_and_filter("Band - Song", wait_seconds=60, empty_abort_seconds=1, poll_interval=0.01)
        assert out == []

    def test_the_final_drain_always_runs(self):
        """Even when the loop already completed, one more drain happens so late
        arrivals are not lost."""
        http = _FeedHttp()
        http.entries = [
            ([_response("Band - Song.flac")], "Completed, Succeeded", True),
        ]
        holder = _SearchableSvc(http)
        out = holder.svc.search_and_filter("Band - Song", wait_seconds=10, poll_interval=0.01)
        assert [i["filename"] for i in out] == ["Band - Song.flac"]
        # poll during the loop + ALWAYS a final drain
        assert http.calls >= 2


# ---------------------------------------------------------------------------
# 2.9 / 2.10 — single download funnel + passthrough of remote paths
# ---------------------------------------------------------------------------

class _RecordingEnqueue(_NoHttp):
    def __init__(self):
        self.enabled = True
        self.enqueued: list[tuple[str, list[dict]]] = []

    def enqueue_downloads(self, username, files, timeout=15, raise_on_error=False):
        self.enqueued.append((username, [dict(f) for f in files]))
        return ["t1"]


class TestDownloadFunnel:

    def test_download_file_goes_through_the_shared_enqueue(self):
        http = _RecordingEnqueue()
        svc = SlskdService(http_client=http)
        assert svc.download_file("peer", "Music/x.flac", size=1000) is True
        assert http.enqueued and http.enqueued[0][0] == "peer"
        assert http.enqueued[0][1] == [{"filename": "Music/x.flac", "size": 1000}]

    def test_zero_size_is_refused_before_any_request(self):
        http = _RecordingEnqueue()
        svc = SlskdService(http_client=http)
        assert svc.download_file("peer", "x.flac", size=0) is False
        assert http.enqueued == []

    def test_batch_passes_paths_through_unchanged(self):
        import services.downloads.slskd_service as mod

        http = _RecordingEnqueue()
        svc = SlskdService(http_client=http)
        results = svc.download_files([
            {"username": "peer", "filename": " Some/Path file.flac ", "size": 1},
        ])
        assert results[0]["success"] is True
        # the trailing space is PART of the remote path — Soulseek matches exactly
        assert http.enqueued[0][1][0]["filename"] == " Some/Path file.flac "


# ---------------------------------------------------------------------------
# 2.11 queue position
# ---------------------------------------------------------------------------

class TestQueuePosition:

    def test_position_zero_is_a_real_answer(self):
        assert SlskdService.extract_queue_position({"queuePosition": 0}) == 0
        assert SlskdService.extract_queue_position({"queue_position": "0"}) == 0

    def test_queue_length_is_never_used_as_a_fallback(self):
        # queueLength is the peer's TOTAL queue, not this file's position.
        assert SlskdService.extract_queue_position({"queueLength": 7}) is None

    def test_order_and_string_tolerance(self):
        assert SlskdService.extract_queue_position({"queueIndex": "3"}) == 3
        assert SlskdService.extract_queue_position({"queuePosition": 2, "queueLength": 7}) == 2


# ---------------------------------------------------------------------------
# 10 transfer parsing — dedupe + localFilePath
# ---------------------------------------------------------------------------

class TestTransferParsing:

    def test_nested_and_top_level_duplicates_collapse(self):
        svc = SlskdService(http_client=_NoHttp())
        raw = {
            "downloads": [
                {
                    "username": "peer",
                    "directories": [
                        {"directory": "Music", "files": [
                            {"id": "t-1", "filename": "Music/x.flac", "size": 100,
                             "bytesTransferred": 50, "state": "InProgress"}
                        ]},
                    ],
                    "files": [
                        {"id": "t-1", "filename": "Music/x.flac", "size": 100,
                         "bytesTransferred": 50, "state": "InProgress"},
                    ],
                }
            ]
        }
        flat = svc.parse_transfers_response(raw)
        assert len(flat) == 1

    def test_local_file_path_never_comes_from_the_remote_path_key(self):
        svc = SlskdService(http_client=_NoHttp())
        flat = svc.parse_transfers_response([{
            "username": "peer",
            "filename": "remote/path/x.flac",
            "path": "remote/path/anywhere",  # remote, not local
            "files": [],
        }])
        assert flat[0]["localFilePath"] == ""


# ---------------------------------------------------------------------------
# P1-4 tokeniser: non-ASCII preserved, basename outranks folder
# ---------------------------------------------------------------------------

class TestTokeniserAndRanking:

    def test_non_ascii_and_apostrophes_are_folded_well(self):
        tokens = SlskdService._rank_query_tokens("Mötley Crüe - Ça ira, don't")
        assert "motley" in tokens and "crue" in tokens and "ca" in tokens and "ira" in tokens
        assert "dont" in tokens  # apostrophe removed, not split into don+t

    def test_basename_matches_outrank_folder_matches(self):
        items = [
            # Query title only in the FOLDER path — basename is a different song.
            _qualify_file("Band - Song/01 - Some Other Song.flac"),
            # Query title in the BASENAME — the real target.
            _qualify_file("Random Folder/Band - Song.flac"),
        ]
        ranked = SlskdService._rank_qualified(items, "Band - Song")
        assert ranked[0]["filename"] == "Random Folder/Band - Song.flac"
        assert ranked[1]["filename"] == "Band - Song/01 - Some Other Song.flac"

    def test_a_folder_match_still_beats_no_match_at_all(self):
        # Query words appear ONLY in the first file's FOLDER path; the second
        # file matches nowhere.  The genuine folder match must still rank
        # first — the previous implementation credited every non-basename
        # token as a "folder hit", so both scored identically and bitrate
        # decided the order.
        folder_match = _qualify_file("Arctic Monkeys - AM/01 Whatever.flac")
        folder_match["bitrate"] = 192
        no_match = _qualify_file("Other/03 Unrelated.flac")
        no_match["bitrate"] = 320
        ranked = SlskdService._rank_qualified([no_match, folder_match], "Arctic Monkeys")
        assert ranked[0]["filename"] == "Arctic Monkeys - AM/01 Whatever.flac"

    def test_a_basename_match_beats_a_folder_only_match(self):
        basename_match = _qualify_file("Some Folder/Arctic Monkeys - Do I Wanna Know.flac")
        folder_only = _qualify_file("Arctic Monkeys/01 Do I Wanna Know.flac")
        ranked = SlskdService._rank_qualified([folder_only, basename_match], "Arctic Monkeys")
        assert ranked[0]["filename"] == "Some Folder/Arctic Monkeys - Do I Wanna Know.flac"


# ---------------------------------------------------------------------------
# Refinements: 404-complete, slot-above-bitrate, casefold dedup, 429 ceiling,
# null-safe transfer progress
# ---------------------------------------------------------------------------

class _NotFound(Exception):
    """Minimal stand-in for httpx.HTTPStatusError on a 404."""
    status_code = 404


class _NotFoundClient(_NoHttp):
    """get_json raises a 404 for the state half, or the responses half."""

    def __init__(self, fail_on="state"):
        self.enabled = True
        self.fail_on = fail_on

    def get_json(self, endpoint, *args, **kwargs):
        if self.fail_on == "state":
            raise _NotFound("404 Not Found")
        if "responses" in endpoint:  # responses half
            raise _NotFound("404 Not Found")
        return {"state": "InProgress"}


class TestGetSearchResults404IsComplete:

    def test_a_404_state_lookup_treats_the_search_as_complete(self):
        svc = SlskdService(http_client=_NotFoundClient(fail_on="state"))
        responses, state, is_complete = svc.get_search_results("s1")
        assert responses == []
        assert is_complete is True
        assert "Deleted" == state

    def test_a_404_on_responses_treats_the_search_as_complete(self):
        svc = SlskdService(http_client=_NotFoundClient(fail_on="responses"))
        responses, state, is_complete = svc.get_search_results("s1")
        assert responses == []
        assert is_complete is True

    def test_a_404_via_the_response_object_is_also_complete(self):
        # Real httpx raises HTTPStatusError carrying the original response, so
        # the status may be reachable only through ``exc.response``.
        class _Resp:
            status_code = 404

        class _Err(Exception):
            response = _Resp()

        class _Client(_NoHttp):
            enabled = True

            def get_json(self, endpoint, *args, **kwargs):
                raise _Err("404 Not Found")

        svc = SlskdService(http_client=_Client())
        responses, state, is_complete = svc.get_search_results("s1")
        assert responses == []
        assert is_complete is True


class TestRankSlotAboveBitrate:

    def test_an_open_slot_low_bitrate_outranks_a_full_slot_high_bitrate(self):
        full_hq = _qualify_file("Band - Song.flac")
        full_hq["has_free_upload_slot"] = False   # full peer, great file
        open_lq = _qualify_file("Band - Song.mp3")
        open_lq["bitrate"] = 128
        open_lq["has_free_upload_slot"] = True    # open peer, ok file
        ranked = SlskdService._rank_qualified([full_hq, open_lq], "Band - Song")
        assert ranked[0]["filename"] == "Band - Song.mp3", (
            "an open-slot peer must rank ABOVE bitrate — a full-slot perfect "
            "file can sit queued behind 4000 other downloads"
        )

    def test_unknown_slot_is_not_treated_as_full(self):
        unknown = _qualify_file("a.flac")
        unknown["has_free_upload_slot"] = None     # unknown, must not rank last
        full = _qualify_file("b.flac")
        full["has_free_upload_slot"] = False
        ranked = SlskdService._rank_qualified([full, unknown], "a b")
        assert ranked[0]["filename"] == "a.flac"
        assert ranked[1]["filename"] == "b.flac"


class TestSearchCasefoldDedup:
    """Windows peers make path casing unpredictable — identical files differing
    only in case must not be counted/queued twice."""

    def test_same_file_different_case_is_seen_once(self):
        http = _FeedHttp()
        http.entries = [
            ([_response("Band - Song.FLAC"), _response("band - song.flac")], "Completed", True),
        ]
        holder = _SearchableSvc(http)
        out = holder.svc.search_and_filter("band - song", wait_seconds=10, poll_interval=0.01)
        assert len(out) == 1, "case-only differences are the same Soulseek file"


class TestRetryAfter429Ceiling:

    def test_429_backoff_can_reach_ten_seconds(self):
        from services.downloads.slskd_service import SlskdService as _S

        resp = _HdrResp()
        assert _S._retry_after_or_backoff(resp, 100) == 10.0
        assert _S._retry_after_or_backoff(resp, 5) == 5.0

    def test_retry_after_header_still_wins(self):
        from services.downloads.slskd_service import SlskdService as _S

        resp = _HdrResp(retry_after="2")
        assert _S._retry_after_or_backoff(resp, 5) == 2.0


class _HdrResp:
    def __init__(self, retry_after=""):
        self.headers = {"Retry-After": retry_after} if retry_after else {}


class TestTransferNullSize:

    def test_null_and_garbage_sizes_do_not_crash(self):
        svc = SlskdService(http_client=_NoHttp())
        flat = svc.parse_transfers_response([{
            "username": "peer",
            "directories": [{"directory": "x", "files": [
                {"id": "t1", "filename": "a.flac", "size": None,
                 "bytesTransferred": "lots", "state": "InProgress"},
                {"id": "t2", "filename": "b.flac", "size": 0,
                 "percentComplete": None, "state": "InProgress"},
            ]}],
        }])
        assert flat[0]["size"] == 0
        assert flat[0]["progress"] == 0
        assert flat[1]["progress"] == 0

    def test_percent_complete_fallback_is_used_when_size_is_zero(self):
        svc = SlskdService(http_client=_NoHttp())
        flat = svc.parse_transfers_response([{
            "username": "peer",
            "directories": [{"directory": "x", "files": [
                {"id": "t1", "filename": "a.flac", "size": 0, "percentComplete": "57", "state": "InProgress"},
            ]}],
        }])
        assert flat[0]["progress"] == 57