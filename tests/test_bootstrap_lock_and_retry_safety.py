"""Data-safety and request-correctness fixes from the outstanding-issue register.

Three reported classes, each pinned by the failure it prevents:

1. **`db/bootstrap.py`** — the schema advisory lock was released on a DIFFERENT
   connection from the one that took it (a silent no-op), DDL failures were
   swallowed with a bare ``except`` that left the transaction aborted, and the
   duplicate reconciliation could delete a manually-confirmed or MBID-resolved
   row — on every boot.
2. **`wikipedia_scraper_service._sanitize_stored_rows`** — ANY exception from
   the rename was treated as "a duplicate exists" and the row was deleted, and
   the pass ran over MusicBrainz-sourced rows whose names it does not own.
3. **`api_clients/http_utils`** — a negative/NaN ``Retry-After`` reached
   ``time.sleep``, and private-network detection was a string prefix that both
   missed loopback hostnames and matched PUBLIC addresses (``172.217.x.x``).
"""

from __future__ import annotations

import inspect
import math
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Retry-After parsing
# ---------------------------------------------------------------------------


def http_utils():
    return pytest.importorskip("api_clients.http_utils")


class TestParseRetryAfter:
    def test_seconds_are_clamped_to_the_maximum(self) -> None:
        hu = http_utils()
        assert hu.parse_retry_after("120") == hu._MAX_RETRY_AFTER

    def test_a_normal_value_passes_through(self) -> None:
        hu = http_utils()
        assert hu.parse_retry_after("5") == pytest.approx(5.0)

    @pytest.mark.parametrize("value", ["-5", "-0.001", "nan", "-nan", "inf", "-inf"])
    def test_negative_and_non_finite_never_reach_sleep(self, value: str) -> None:
        """⭐⭐ ``time.sleep(-5)`` and ``time.sleep(nan)`` both RAISE.

        The old code was ``min(float(retry_after), 60)``, which passes every one
        of these through — inside the retry policy, so the exception replaced a
        recoverable request failure with a hard error.
        """
        hu = http_utils()
        wait = hu.parse_retry_after(value, default=1.25)
        assert wait == pytest.approx(1.25)
        assert math.isfinite(wait) and wait >= 0

    def test_a_negative_value_without_a_default_is_none_not_zero(self) -> None:
        """⚠️ "Wait 0" would retry IMMEDIATELY — the opposite of the header's
        purpose. A malformed value carries no information at all."""
        hu = http_utils()
        assert hu.parse_retry_after("-5") is None

    def test_zero_is_a_real_value(self) -> None:
        hu = http_utils()
        assert hu.parse_retry_after("0") == 0.0

    def test_http_date_form_is_understood(self) -> None:
        hu = http_utils()
        # A past date → 0 (never negative).
        assert hu.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0

    def test_an_unparseable_value_falls_back(self) -> None:
        hu = http_utils()
        assert hu.parse_retry_after("not-a-date", default=2.5) == pytest.approx(2.5)

    def test_none_and_blank_fall_back(self) -> None:
        hu = http_utils()
        assert hu.parse_retry_after(None, default=3.0) == pytest.approx(3.0)
        assert hu.parse_retry_after("   ", default=3.0) == pytest.approx(3.0)

    def test_the_wait_function_uses_the_validated_parser(self) -> None:
        """Source guard: the raw ``float(retry_after)`` must be gone.

        ⚠️ Scanned on ``handle_request`` (which owns the ``_wait`` closure), NOT
        the module: ``parse_retry_after``'s own docstring QUOTES the retired
        ``min(float(retry_after), 60)`` to explain it, so a module-wide "must
        not appear" check fails against the fixed code.
        """
        source = inspect.getsource(http_utils()._RetryTransport.handle_request)
        assert "parse_retry_after(retry_after" in source
        assert "min(float(retry_after)" not in source


# ---------------------------------------------------------------------------
# 2. Private / loopback host detection
# ---------------------------------------------------------------------------


class TestPrivateHostDetection:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.1.2.3",
            "localhost",
            "LOCALHOST",
            "::1",
            "10.0.0.5",
            "10.255.255.254",
            "192.168.1.20",
            "172.16.0.1",
            "172.31.255.255",
            "169.254.10.10",   # link-local
            "fe80::1%eth0",
            "0.0.0.0",
        ],
    )
    def test_private_and_loopback_are_detected(self, host: str) -> None:
        assert http_utils().is_private_host(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            # ⭐⭐ THE BUG: every one of these starts with "172." or "10." and
            # the old prefix check called it internal, so it skipped the rate
            # limiter entirely.
            "172.217.16.142",   # Google
            "172.64.0.1",       # Cloudflare
            "172.32.0.1",       # first address AFTER 172.16.0.0/12
            "10.example.com",   # hostname starting with the digits "10"
            "8.8.8.8",
            "1.1.1.1",
            "musicbrainz.org",
            "api.listenbrainz.org",
        ],
    )
    def test_public_hosts_are_not_internal(self, host: str) -> None:
        assert http_utils().is_private_host(host) is False

    def test_an_empty_host_is_not_internal(self) -> None:
        hu = http_utils()
        assert hu.is_private_host("") is False
        assert hu.is_private_host(None) is False

    def test_the_transport_uses_the_helper(self) -> None:
        source = inspect.getsource(http_utils()._RetryTransport.handle_request)
        assert "is_private_host(" in source
        assert 'startswith("172.")' not in source
        assert 'startswith("10.")' not in source


# ---------------------------------------------------------------------------
# 3. Per-attempt MusicBrainz throttle
# ---------------------------------------------------------------------------


class TestRetryAttemptsAreThrottled:
    def test_retries_reserve_a_shared_slot(self) -> None:
        """A retry is a REQUEST and counts against the 1 req/s MusicBrainz budget."""
        source = inspect.getsource(http_utils()._RetryTransport.handle_request)
        assert "_throttle_shared(" in source
        assert '== "musicbrainz.org"' in source

    def test_the_first_attempt_is_not_charged_twice(self) -> None:
        """⭐ ``musicbrainz_http.get()`` already reserves a slot per CALL.

        Charging attempt 1 here as well would double the effective interval and
        halve scan throughput — a fix that looks right and makes every scan
        twice as slow.
        """
        source = inspect.getsource(http_utils()._RetryTransport.handle_request)
        assert "attempt_no > 1" in source

    def test_the_throttle_helper_never_raises(self) -> None:
        """Inside a retry transport an exception would replace a recoverable
        network failure with a limiter failure."""
        hu = http_utils()
        source = inspect.getsource(hu._throttle_shared)
        assert "except Exception" in source


# ---------------------------------------------------------------------------
# 4. Bootstrap: lock ownership, savepoints, non-destructive dedupe
# ---------------------------------------------------------------------------


def bootstrap():
    return pytest.importorskip("db.bootstrap")


class TestTheSchemaLockIsReleasedWhereItWasTaken:
    def test_no_second_session_in_the_except_finally(self) -> None:
        """⭐⭐ ``pg_advisory_unlock`` from a DIFFERENT connection is a no-op.

        The old ``finally`` opened a second ``db_session()`` to unlock, so when
        the pool returned a different connection the lock stayed held until that
        connection died — a crashed bootstrap could wedge every later one.
        """
        source = inspect.getsource(bootstrap().ensure_full_schema)
        assert "with db_session() as session: _release_advisory_lock" not in source
        assert "_release_advisory_lock(session, _SCHEMA_BOOTSTRAP_LOCK_NAME)" in source

    def test_the_release_sits_inside_the_acquiring_block(self) -> None:
        source = inspect.getsource(bootstrap().ensure_full_schema)
        acquire = source.index("_try_advisory_lock(session, _SCHEMA_BOOTSTRAP_LOCK_NAME)")
        release = source.index("_release_advisory_lock(session, _SCHEMA_BOOTSTRAP_LOCK_NAME)")
        # The unlock must come after the acquisition, inside the same `with`.
        assert release > acquire
        assert "finally:" in source[acquire:release]

    def test_the_commit_precedes_the_unlock(self) -> None:
        """Another process must never take the lock and see uncommitted DDL."""
        source = inspect.getsource(bootstrap().ensure_full_schema)
        assert source.index("session.commit()") < source.index(
            "_release_advisory_lock(session, _SCHEMA_BOOTSTRAP_LOCK_NAME)"
        )


class TestDdlFailuresAreContained:
    def test_the_savepoint_helper_exists(self) -> None:
        assert callable(bootstrap()._ddl_savepoint)

    def test_every_upcoming_ddl_statement_uses_it(self) -> None:
        """⭐ A bare ``try/except`` leaves the transaction ABORTED on PostgreSQL,
        so every statement after the tolerated failure also fails."""
        source = inspect.getsource(bootstrap().ensure_upcoming_releases_schema)
        assert "_ddl_savepoint(" in source
        assert "try: session.execute" not in source

    def test_a_failing_ddl_does_not_raise(self, monkeypatch) -> None:
        import db.bootstrap as bs

        calls: list[str] = []

        class FakeNested:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeSession:
            def begin_nested(self):
                return FakeNested()

            def execute(self, statement, params=None):
                calls.append(str(statement))
                raise RuntimeError("boom")

        assert bs._ddl_savepoint(FakeSession(), "ALTER TABLE x DROP CONSTRAINT y") is False
        assert calls, "the statement must actually have been attempted"

    def test_the_unique_index_failure_is_reported_not_fatal(self) -> None:
        source = inspect.getsource(bootstrap().ensure_upcoming_releases_schema)
        assert "index_ok" in source
        assert "Nothing was " in source or "Nothing was deleted" in source


class TestTheDedupeCannotLoseData:
    def test_a_manually_overridden_row_is_never_deleted(self) -> None:
        """⭐⭐ The DELETE ran on EVERY BOOT with no override check."""
        source = inspect.getsource(bootstrap()._dedupe_upcoming_releases)
        assert "mbid_manual_override" in source
        assert "COALESCE(a.mbid_manual_override, FALSE) = FALSE" in source

    def test_a_resolved_mbid_outranks_a_scraped_row(self) -> None:
        source = inspect.getsource(bootstrap()._dedupe_upcoming_releases)
        assert "release_group_mbid" in source

    def test_primary_type_is_part_of_the_identity(self) -> None:
        """⭐ The first DELETE had no ``primary_type`` term, so a distinct single
        could be removed because an ALBUM with the same artist/title existed."""
        assert "primary_type" in bootstrap()._SAME_RELEASE_SQL

    def test_the_rank_prefers_override_then_mbid(self) -> None:
        rank = bootstrap()._DEDUPE_RANK_SQL
        override = rank.index("mbid_manual_override")
        mbid = rank.index("release_group_mbid")
        assert override < mbid, "the override term must lead the MBID term"
        # The override term must outweigh the MBID term on its own.
        assert "THEN 2 ELSE 0" in rank and "THEN 1 ELSE 0" in rank


class TestOtherLocksReleaseOnFailure:
    def test_album_artist_backfill_unlocks_in_a_finally(self) -> None:
        source = inspect.getsource(bootstrap().ensure_album_artist_column_data)
        assert "finally:" in source
        assert "_release_advisory_lock(session, _ALBUM_ART_DATA_LOCK_KEY)" in source

    def test_album_art_schema_unlocks_in_a_finally(self) -> None:
        source = inspect.getsource(bootstrap().ensure_album_art_schema)
        assert "finally:" in source
        assert "_release_advisory_lock(session, _ALBUM_ART_SCHEMA_LOCK_KEY)" in source


# ---------------------------------------------------------------------------
# 5. Scraper: delete only on a duplicate, protect foreign identity
# ---------------------------------------------------------------------------


def scraper():
    return pytest.importorskip("services.upcoming_releases.wikipedia_scraper_service")


class TestOnlyADuplicateMayDeleteARow:
    def test_the_unique_violation_detector_accepts_both_dialects(self) -> None:
        s = scraper()
        assert s._is_unique_violation(Exception("duplicate key value violates unique constraint")) is True
        assert s._is_unique_violation(Exception("UNIQUE constraint failed: upcoming_releases.artist_name")) is True

    def test_the_detector_rejects_unrelated_failures(self) -> None:
        s = scraper()
        for message in (
            "could not serialize access due to concurrent update",
            "canceling statement due to lock timeout",
            "server closed the connection unexpectedly",
            "current transaction is aborted, commands ignored",
        ):
            assert s._is_unique_violation(Exception(message)) is False, message

    def test_a_lock_timeout_does_not_delete_the_row(self, monkeypatch) -> None:
        """⭐⭐ THE DATA-LOSS BUG: every exception was read as "a duplicate
        exists", so a lock timeout destroyed a perfectly valid row."""
        s = scraper()
        executed: list[str] = []

        class Nested:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Result:
            def fetchall(self):
                return [(1, "Ann Wilsonand Tripsitter", "Album", "wikipedia", None, False)]

        class FakeSession:
            def execute(self, statement, params=None):
                sql = str(statement)
                executed.append(sql)
                if sql.strip().upper().startswith("SELECT"):
                    return Result()
                raise RuntimeError("canceling statement due to lock timeout")

            def begin_nested(self):
                return Nested()

        class Ctx:
            def __enter__(self):
                return FakeSession()

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(s, "db_session", lambda: Ctx())
        monkeypatch.setattr(
            "services.upcoming_releases.matching_service.sanitize_wiki_entry",
            lambda a, b: ("Ann Wilson and Tripsitter", b),
        )

        s._sanitize_stored_rows()

        assert not any(sql.strip().upper().startswith("DELETE") for sql in executed), (
            "a non-unique failure must never delete the row"
        )

    def test_a_unique_violation_does_delete_the_duplicate(self, monkeypatch) -> None:
        s = scraper()
        executed: list[str] = []

        class Nested:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Result:
            def fetchall(self):
                return [(1, "Ann Wilsonand Tripsitter", "Album", "wikipedia", None, False)]

        class FakeSession:
            def execute(self, statement, params=None):
                sql = str(statement)
                executed.append(sql)
                if sql.strip().upper().startswith("SELECT"):
                    return Result()
                if sql.strip().upper().startswith("UPDATE"):
                    raise RuntimeError(
                        "duplicate key value violates unique constraint "
                        '"uq_upcoming_artist_album_type"'
                    )
                return Result()

            def begin_nested(self):
                return Nested()

        class Ctx:
            def __enter__(self):
                return FakeSession()

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(s, "db_session", lambda: Ctx())
        monkeypatch.setattr(
            "services.upcoming_releases.matching_service.sanitize_wiki_entry",
            lambda a, b: ("Ann Wilson and Tripsitter", b),
        )

        s._sanitize_stored_rows()

        assert any(sql.strip().upper().startswith("DELETE") for sql in executed)


class TestTheSanitizerOnlyTouchesWhatItOwns:
    def test_the_select_reads_the_protection_columns(self) -> None:
        source = inspect.getsource(scraper()._sanitize_stored_rows)
        assert "release_group_mbid" in source
        assert "mbid_manual_override" in source

    @pytest.mark.parametrize(
        "row",
        [
            # manually confirmed
            (7, "Ann Wilsonand Tripsitter", "Album", "wikipedia", None, True),
            # resolved MBID
            (8, "Ann Wilsonand Tripsitter", "Album", "wikipedia", "abc-123", False),
            # MusicBrainz provenance
            (9, "Ann Wilsonand Tripsitter", "Album", "MusicBrainz Daily Collection", None, False),
        ],
    )
    def test_a_protected_row_is_left_exactly_as_stored(self, monkeypatch, row) -> None:
        s = scraper()
        executed: list[str] = []

        class Nested:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Result:
            def fetchall(self):
                return [row]

        class FakeSession:
            def execute(self, statement, params=None):
                executed.append(str(statement))
                return Result()

            def begin_nested(self):
                return Nested()

        class Ctx:
            def __enter__(self):
                return FakeSession()

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(s, "db_session", lambda: Ctx())
        monkeypatch.setattr(
            "services.upcoming_releases.matching_service.sanitize_wiki_entry",
            lambda a, b: ("CHANGED", b),
        )

        s._sanitize_stored_rows()

        assert not any("UPDATE" in sql.upper() for sql in executed), (
            f"row {row[0]} was renamed despite being protected"
        )
