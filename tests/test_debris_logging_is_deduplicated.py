"""A debris value must be reported ONCE, not once per row per scan.

Reported (log): the same line repeated hundreds of times —

    Ignoring a malformed local value instead of protecting it field='composer'
    local="Joacim Cans, Oscar Dronjak, ['Joacim Cans', 'Oscar Dronjak']"

The repair is correct and unchanged; the repetition buried everything else.

⚠️ The module logs through STRUCTLOG, so pytest's ``caplog`` (which hooks the
stdlib root logger) does not capture it. These tests therefore assert on the
dedupe state and on the repair behaviour, and one test patches the logger to
count actual emissions.
"""

from __future__ import annotations

import pytest


DEBRIS = "Joacim Cans, Oscar Dronjak, ['Joacim Cans', 'Oscar Dronjak']"


def _reset() -> None:
    from services.scanning import reconciliation as rec

    with rec._debris_logged_lock:
        rec._debris_logged.clear()


@pytest.fixture
def counted(monkeypatch):
    """Count real log emissions, whatever the logging backend."""
    from services.scanning import reconciliation as rec

    calls: list[tuple[str, str]] = []

    class _Spy:
        def info(self, event, **kwargs):
            calls.append((str(event), str(kwargs.get("field") or "")))

        def warning(self, event, **kwargs):
            calls.append((str(event), str(kwargs.get("field") or "")))

        def debug(self, event, **kwargs):
            pass

        def error(self, event, **kwargs):
            calls.append((str(event), str(kwargs.get("field") or "")))

    monkeypatch.setattr(rec, "logger", _Spy())
    return calls


def test_the_same_value_logs_once(counted):
    from services.scanning.reconciliation import _note_debris_logged

    _reset()
    for _ in range(50):
        _note_debris_logged("composer", DEBRIS)

    hits = [c for c in counted if "malformed local value" in c[0]]
    assert len(hits) == 1, f"expected one line, got {len(hits)}"


def test_a_different_value_still_logs(counted):
    from services.scanning.reconciliation import _note_debris_logged

    _reset()
    _note_debris_logged("composer", DEBRIS)
    _note_debris_logged("composer", "['Steve Harris']")

    hits = [c for c in counted if "malformed local value" in c[0]]
    assert len(hits) == 2, "a genuinely different corruption must still be reported"


def test_a_different_field_still_logs(counted):
    from services.scanning.reconciliation import _note_debris_logged

    _reset()
    _note_debris_logged("composer", DEBRIS)
    _note_debris_logged("lyricist", DEBRIS)

    hits = [c for c in counted if "malformed local value" in c[0]]
    assert len(hits) == 2


def test_the_memory_is_bounded():
    from services.scanning import reconciliation as rec

    _reset()
    for index in range(rec._DEBRIS_LOGGED_MAX + 10):
        rec._note_debris_logged("composer", f"['name {index}']")

    assert len(rec._debris_logged) <= rec._DEBRIS_LOGGED_MAX, (
        "a library polluted across many values must not grow the set forever"
    )


def test_the_repair_is_still_skipped_for_debris(counted):
    """The point: a malformed local value must NOT be protected."""
    from services.scanning.reconciliation import local_authority_conflicts

    _reset()
    existing = {"composer": DEBRIS, "title": "Hysteria"}
    payload = {"composer": "Joacim Cans, Oscar Dronjak", "title": "Hysteria"}
    conflicts = local_authority_conflicts(existing, payload)
    assert "composer" not in conflicts, (
        "corruption must be repaired by the provider's clean value"
    )


def test_a_real_local_value_is_still_protected():
    from services.scanning.reconciliation import local_authority_conflicts

    existing = {"composer": "Joacim Cans", "title": "Hysteria"}
    payload = {"composer": "Someone Else", "title": "Hysteria"}
    assert "composer" in local_authority_conflicts(existing, payload), (
        "a genuine local correction must still win over the provider"
    )

