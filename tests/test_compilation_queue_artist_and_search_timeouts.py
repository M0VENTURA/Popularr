"""Compilation tracks must queue with the SONG's artist, and searches must not hang.

Two reported bugs:

1. "Various artist collections are adding to the download queue with the album
   artist, not track artist." A page-load missing-track row carries
   ``track_artist`` from the DB, but ``missingRowToTrackComp`` (album.js) dropped
   it, so the queue payload fell through to ``pageArtist()`` — the album header.

2. "Manual soulseek search for a queue item keeps failing and timing out."
   Every slskd call went through a 4-attempt retry client, so one Poll could
   block ~80-120 s against a 30 s browser cap; every poll was abandoned, the
   loop treated each abort as transient and continued for the full 120-attempt
   budget, and a failed slot check reported ``slotFree: True``.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ALBUM_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"
SLSKD_JS = REPO_ROOT / "test_site" / "static" / "js" / "services" / "slskd.js"


def _src(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _func_body(source: str, name: str) -> str:
    """The body of a top-level JS function, by brace matching.

    Starts at the brace AFTER the parameter list, so a default parameter like
    ``opts = {}`` is not mistaken for the body opening.
    """
    start = source.index(f"function {name}(")
    # Walk to the closing paren of the parameter list, then to the body brace.
    paren = source.index("(", start)
    depth = 0
    for index in range(paren, len(source)):
        ch = source[index]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = source.index("{", index)
                break
    else:  # pragma: no cover
        raise AssertionError(f"no parameter list for {name}")

    depth = 0
    for index in range(brace, len(source)):
        ch = source[index]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[brace:index + 1]
    raise AssertionError(f"unbalanced braces for {name}")


def _py_func_body(source: str, name: str) -> str:
    """The source of a Python function, via AST (exact line range)."""
    import ast

    tree = ast.parse(source)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1: node.end_lineno])
    raise AssertionError(f"no function {name}")


# ---------------------------------------------------------------------------
# 1. The compilation artist is carried from the DB row to the queue payload
# ---------------------------------------------------------------------------

class TestTheMissingRowKeepsTheTrackArtist:
    def test_the_mapper_copies_track_artist(self):
        body = _func_body(_src(ALBUM_JS), "missingRowToTrackComp")
        assert "track_artist:" in body, (
            "missingRowToTrackComp dropped track_artist. The server selects it "
            "from missing_album_tracks; without it the queue payload falls "
            "through to pageArtist() and a compilation track queues as "
            "'Various Artists'."
        )

    def test_it_also_supplies_mb_artist(self):
        """The queue payload's chain is track_artist || mb_artist || pageArtist."""
        body = _func_body(_src(ALBUM_JS), "missingRowToTrackComp")
        assert "mb_artist:" in body

    def test_the_queue_payload_still_prefers_the_track_artist(self):
        body = _func_body(_src(ALBUM_JS), "buildMissingRow")
        assert "trackArtist || pageArtist()" in body, (
            "the payload's artist must come from the track performer with the "
            "album artist only as a last resort"
        )

    def test_the_performer_is_also_stashed_in_metadata(self):
        """The worker recovers a real artist from metadata.track_artist."""
        body = _func_body(_src(ALBUM_JS), "buildMissingRow")
        assert "track_artist: trackArtist" in body, (
            "the queue worker's _generic_artists recovery reads "
            "metadata.track_artist; without it a placeholder row searches on a "
            "bare title and the artist gate is skipped"
        )


class TestTheWorkerRecoveryExists:
    def test_the_pipeline_reads_metadata_track_artist(self):
        src = (REPO_ROOT / "services" / "downloads" / "download_pipeline_service.py").read_text(
            encoding="utf-8"
        )
        assert '"track_artist"' in src, (
            "the queue worker must be able to recover the performer from the "
            "queue row's metadata"
        )


# ---------------------------------------------------------------------------
# 2. The manual search fails fast instead of hanging
# ---------------------------------------------------------------------------

class TestTheManualSearchClientDoesNotRetry:
    def test_a_fast_client_kind_exists(self):
        from api_clients.slskd_http import SlskdHttpClient

        client = SlskdHttpClient("http://slskd:5030", fast=True)
        # The fast session must not be the shared retrying one.
        from api_clients import session as shared

        assert client.session is not shared

    def test_the_default_is_unchanged_for_background_work(self):
        from api_clients import session as shared
        from api_clients.slskd_http import SlskdHttpClient

        assert SlskdHttpClient("http://slskd:5030").session is shared

    def test_the_search_routes_ask_for_the_fast_client(self):
        src = (REPO_ROOT / "routes" / "download_search_routes.py").read_text(encoding="utf-8")
        assert "fast=True" in src
        # The three interactive entry points must use the helper.
        for name in ("slskd_search", "slskd_search_slot", "slskd_search_results"):
            body = _py_func_body(src, name)
            assert "_manual_search_client(" in body, f"{name} must use the fast client"


class TestASlotCheckFailureIsNotAFreeSlot:
    def test_the_slot_route_no_longer_reports_free_on_error(self):
        src = (REPO_ROOT / "routes" / "download_search_routes.py").read_text(encoding="utf-8")
        body = _py_func_body(src, "slskd_search_slot")
        tail = body[body.index("except Exception"):]
        assert '"slotFree": True' not in tail, (
            "returning slotFree=True on an error makes a broken slot check "
            "indistinguishable from a free one"
        )
        assert '"slotFree": False' in tail

    def test_the_failure_is_logged_at_error(self):
        src = (REPO_ROOT / "routes" / "download_search_routes.py").read_text(encoding="utf-8")
        body = _py_func_body(src, "slskd_search_slot")
        assert 'logger.error("Failed to check search slot"' in body


class TestTheStartFailureIsLogged:
    def test_the_none_search_id_branch_logs(self):
        src = (REPO_ROOT / "routes" / "download_search_routes.py").read_text(encoding="utf-8")
        body = _py_func_body(src, "slskd_search")
        # After the success return, the failure branch must log before returning.
        tail = body[body.index('return jsonify({"searchId"'):]
        assert "logger.warning(" in tail, (
            "a search that slskd refused used to return 500 with no server log"
        )


class TestThePollLoopFailsFast:
    def test_a_consecutive_error_cap_exists(self):
        src = _src(SLSKD_JS)
        assert "MAX_CONSECUTIVE_POLL_ERRORS" in src

    def test_repeated_poll_errors_stop_the_loop(self):
        body = _func_body(_src(SLSKD_JS), "pollUntilComplete")
        assert "consecutiveErrors" in body
        assert "session.poll.stop()" in body, (
            "the loop must stop itself; it used to continue for all 120 attempts"
        )

    def test_a_successful_poll_resets_the_streak(self):
        body = _func_body(_src(SLSKD_JS), "pollUntilComplete")
        assert "consecutiveErrors = 0" in body

    def test_the_poll_passes_a_timeout(self):
        body = _func_body(_src(SLSKD_JS), "pollUntilComplete")
        assert "timeoutMs: POLL_TIMEOUT_MS" in body


class TestTheEmptyStateExplainsItself:
    def test_the_renderer_mentions_filtered_results(self):
        body = _func_body(_src(SLSKD_JS), "renderTable")
        assert "formatFiltered" in body, (
            "a search whose results were all filtered out rendered exactly "
            "'No results found', identical to a genuinely empty search"
        )

    def test_the_server_reports_the_dropped_count(self):
        src = (REPO_ROOT / "routes" / "download_search_routes.py").read_text(encoding="utf-8")
        assert '"formatFiltered": _format_filtered' in src


# ---------------------------------------------------------------------------
# 3. Compose service names are not rate-limited as public hosts
# ---------------------------------------------------------------------------

class TestSingleLabelHostsAreLocal:
    @pytest.mark.parametrize("host", ["slskd", "navidrome", "beets", "postgres", "SLSKD"])
    def test_a_compose_service_name_is_private(self, host):
        from api_clients.http_utils import is_private_host

        assert is_private_host(host) is True, (
            "a single-label hostname cannot be a public FQDN; treating it as "
            "public serialised every slskd call through the 1 req/s throttle"
        )

    @pytest.mark.parametrize("host", ["musicbrainz.org", "api.discogs.com", "8.8.8.8"])
    def test_a_public_host_is_still_public(self, host):
        from api_clients.http_utils import is_private_host

        assert is_private_host(host) is False

    def test_loopback_and_private_ranges_still_work(self):
        from api_clients.http_utils import is_private_host

        assert is_private_host("127.0.0.1") is True
        assert is_private_host("192.168.1.5") is True
        assert is_private_host("localhost") is True
