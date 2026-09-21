"""Every log must be bounded by the SAME cap — access.log wasn't.

Reported: "I'm not sure if some of the log files are cleaning up, they are blowing
up fairly large", with ``access.log 26831.3 KB`` while every other file sat at
1-2 MB.

The app's own logs rotate at ``helpers.logging_config._LOG_MAX_BYTES`` (5 MB), and
``tests/test_log_rotation.py`` guards that. The files the APP does not own are
rotated by ``entrypoint.sh`` — and its default cap was **50 MB**
(``SPTNR_ACCESS_LOG_MAX_SIZE:-52428800``), ten times the app's limit. That is why
access.log alone looked unbounded: it was simply allowed to reach ten times the
size of everything else before rotating.

These guards pin the alignment and the two mechanism details that make the
rotation correct rather than merely present.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _entrypoint() -> str:
    path = REPO_ROOT / "entrypoint.sh"
    assert path.exists(), "entrypoint.sh is missing"
    return path.read_text(encoding="utf-8")


def _caps(source: str) -> list[int]:
    return [int(value) for value in re.findall(r"SPTNR_ACCESS_LOG_MAX_SIZE:-(\d+)", source)]


class TestCapsAgree:
    def test_the_access_log_cap_matches_the_app_log_cap(self):
        from helpers.logging_config import _LOG_MAX_BYTES

        caps = _caps(_entrypoint())
        assert caps, "entrypoint.sh must set a default for SPTNR_ACCESS_LOG_MAX_SIZE"
        assert all(cap == _LOG_MAX_BYTES for cap in caps), (
            f"access/error/client logs must cap at the app's own {_LOG_MAX_BYTES} bytes, got {caps}"
        )

    def test_the_cap_is_still_overridable(self):
        """Allowing an override is fine; silently ignoring it would not be."""
        source = _entrypoint()
        assert "${SPTNR_ACCESS_LOG_MAX_SIZE:-" in source
        assert "local max_bytes=" in source


class TestWhatIsRotated:
    @pytest.mark.parametrize("name", ["access.log", "error.log", "client.log"])
    def test_the_unowned_logs_are_rotated(self, name: str):
        source = _entrypoint()
        # Each of these is written by something outside our logging config:
        # hypercorn's file handlers and append_client_log().
        assert name in source, f"{name} must be covered by the rotator"

    def test_queue_processor_log_is_not_copytruncated(self):
        """A ``> file`` redirect cannot be truncated in place.

        Its writer keeps its byte offset across a truncation, so the file would
        go sparse and be padded with NUL bytes. It must stay out of the rotator
        (it is near-empty anyway: the queue worker uses setup_logging()).
        """
        source = _entrypoint()
        rotator_calls = re.findall(r"_rotate_log_if_large\s+[^\n]*", source)
        assert rotator_calls, "the rotator must actually be called"
        assert not any("queue_processor.log" in call for call in rotator_calls), rotator_calls


class TestRotationActuallyRuns:
    def test_it_rotates_at_boot_not_only_after_the_first_sleep(self):
        """A file left oversized by a previous run must shrink at startup."""
        source = _entrypoint()
        # The housekeeping call must appear BEFORE the sleep loop, inside the
        # background subshell.
        boot = source.find("            _rotate_all_logs\n            while true; do")
        assert boot != -1, "the rotator must run once before entering the sleep loop"

    def test_it_keeps_numbered_backups(self):
        source = _entrypoint()
        assert "SPTNR_ACCESS_LOG_BACKUPS:-" in source
        assert "cp -f \"$file\" \"$file.1\"" in source
        assert ": > \"$file\"" in source, "copytruncate must truncate in place"
