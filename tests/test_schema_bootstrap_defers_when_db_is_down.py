"""The schema bootstrap must not crash the container when PostgreSQL is not up.

Reported
--------
``python -m db.bootstrap`` died at start with a full traceback:

```
File "/app/db/bootstrap.py", line 456, in <module>
    result = verify_all_tables_exist()
File "/app/db/schema_helpers.py", line 23, in table_exists
    ...
File ".../psycopg2/__init__.py", line 122, in connect
    conn = _connect(dsn, ...)
```

Why it is the COMMON case, not an edge case:

* ``entrypoint.sh::wait_for_db`` **returns immediately** when no ``PG_HOST`` is
  configured — which is the ``DATABASE_URL``-driven setup — so nothing waits
  for the server before the bootstrap runs;
* ``init_database_and_schema()`` handles this correctly (four retries, and it
  swallows transient errors via ``is_transient_pg_startup_error``), but the
  **second** connection it triggers — ``verify_all_tables_exist()`` at line
  ~456 of the ``__main__`` block — was unguarded;
* the entrypoint's fallback branch re-runs the module **without** ``>/dev/null``
  (``python3 -m db.bootstrap || true``), which is exactly where the traceback
  surfaced.

The exit code must stay non-zero: "All 9 table groups verified" would be a lie
when the tables were never looked at. What changes is that the failure is a
one-line explanation instead of a wall of SQLAlchemy frames — and a genuinely
broken (non-transient) error still raises, so real faults are not masked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO_ROOT / "db" / "bootstrap.py"
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"


@pytest.fixture(scope="module")
def main_block() -> str:
    """The ``__main__`` section — the part that ran in the traceback."""
    src = BOOTSTRAP.read_text(encoding="utf-8")
    return src[src.index('if __name__ == "__main__":'):]


class TestTheVerificationIsGuarded:
    def test_the_verify_runs_inside_a_transient_check(self, main_block: str):
        assert "is_transient_pg_startup_error" in main_block, (
            "verify_all_tables_exist() is called with no guard — when "
            "PostgreSQL is not reachable yet the bootstrap dies with a full "
            "psycopg2 traceback instead of saying so"
        )

    def test_a_deferred_verification_exits_non_zero(self, main_block: str):
        assert "raise SystemExit(1) from None" in main_block, (
            "deferring must still fail: exiting 0 would make entrypoint.sh "
            "print 'All 9 table groups verified' when nothing was verified"
        )

    def test_a_real_fault_is_still_raised(self, main_block: str):
        assert "raise" in main_block.split("is_transient_pg_startup_error", 1)[1], (
            "a non-transient error must not be swallowed — that would hide a "
            "genuinely broken database behind a friendly message"
        )

    def test_the_deferred_message_names_the_next_actor(self, main_block: str):
        """The operator must be told what happens next, not left guessing."""
        assert "runtime bootstrap" in main_block


class TestTheEntrypointToleratesIt:
    def test_the_bootstrap_step_is_non_fatal(self):
        src = ENTRYPOINT.read_text(encoding="utf-8")
        assert "run_schema_bootstrap || true" in src, (
            "a not-yet-ready PostgreSQL must not abort the container: the "
            "runtime bootstrap retries once the server is up"
        )
