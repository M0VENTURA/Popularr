"""The Upcoming Releases window must centre on TODAY, not on July.

Reported:

    "Upcoming released on the dashboard is meant to be based around the current
     date showing the last 4 weeks and the upcoming two weeks. But it's still
     showing July"

Root cause: ``get_release_window()``
(``services/upcoming_releases/wikipedia_scraper_service.py``) defaulted to a
**2-month lookback / 6-month lookahead**,

    return now - timedelta(days=30 * lookback), now + timedelta(days=30 * lookahead)

so on 2026-09-24 the window opened at **2026-07-26** — "still showing July".
The stated intent is 4 weeks back / 2 weeks forward ([-28, +14] days).

Two further defects made it uncorrectable from the UI:

* the tuning keys were ``upcoming_releases_lookback_months`` /
  ``upcoming_releases_lookahead_months``, which appear on **neither** Config page
  and in **neither** ``config.js`` collector, so they could only be set by
  hand-editing ``config.yaml``;
* they were absent from ``_DEFAULT_FEATURE_FLAGS``, so a fresh install had no
  documented default at all.

⚠️ The window is what the DASHBOARD table is filtered by: its
``loadUpcomingReleasesTable()`` does not send a ``window`` parameter, so the
separate "tight" clause in the route (``window_days > 0``) never applies. This
function is the only thing governing what the dashboard shows.
"""

from __future__ import annotations

import inspect
from datetime import datetime as _real_datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

WINDOW_DEFAULTS_DAYS = {"lookback": 28, "lookahead": 14}


def _module():
    from services.upcoming_releases import wikipedia_scraper_service as ws
    return ws


class _FrozenDatetime(_real_datetime):
    """A datetime pinned to a known 'now' so the window is deterministic."""

    FROZEN = _real_datetime(2026, 9, 24, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return cls.FROZEN


@pytest.fixture
def frozen(monkeypatch):
    """Freeze 'now' at 2026-09-24 and return the scraper module."""
    ws = _module()
    monkeypatch.setattr(ws, "datetime", _FrozenDatetime)
    return ws


def _window_with_config(monkeypatch, ws, features: dict):
    """Run get_release_window() with a stubbed features config."""
    import helpers.config_helpers as ch

    def _get_feature(key, default=None):
        return features.get(key, default)

    monkeypatch.setattr(ch, "get_feature", _get_feature)
    return ws.get_release_window()


# ---------------------------------------------------------------------------
# 1. The default window matches the stated intent
# ---------------------------------------------------------------------------

class TestDefaultWindow:
    def test_it_does_not_reach_back_into_july(self, frozen, monkeypatch):
        """The reported symptom: on 2026-09-24 the window opened 2026-07-26."""
        start, _end = _window_with_config(monkeypatch, frozen, {})
        assert start.date().isoformat() == "2026-08-27", (
            f"window starts {start.date()} — expected 4 weeks before 2026-09-24"
        )
        assert start.month != 7, "the window still reaches back into July"

    def test_it_reaches_exactly_two_weeks_forward(self, frozen, monkeypatch):
        _start, end = _window_with_config(monkeypatch, frozen, {})
        assert end.date().isoformat() == "2026-10-08", (
            f"window ends {end.date()} — expected 2 weeks after 2026-09-24"
        )

    def test_the_lookback_is_four_weeks(self, frozen, monkeypatch):
        start, _end = _window_with_config(monkeypatch, frozen, {})
        assert (_FrozenDatetime.FROZEN.date() - start.date()).days == 28

    def test_the_lookahead_is_two_weeks(self, frozen, monkeypatch):
        _start, end = _window_with_config(monkeypatch, frozen, {})
        assert (end.date() - _FrozenDatetime.FROZEN.date()).days == 14

    def test_it_is_centred_on_today(self, frozen, monkeypatch):
        start, end = _window_with_config(monkeypatch, frozen, {})
        assert start < _FrozenDatetime.FROZEN < end

    def test_the_defaults_are_days_not_months(self):
        """Days, because the intent is week-based and the sibling window already
        uses days (`daily_musicbrainz_release_*_days`)."""
        ws = _module()
        src = inspect.getsource(ws.get_release_window)
        assert "lookback_days" in src
        assert "lookahead_days" in src

    def test_the_window_keys_are_deliberately_not_in_default_feature_flags(self):
        """⚠️ Do NOT "tidy" these into ``_DEFAULT_FEATURE_FLAGS``.

        ``get_features_config()`` merges that registry over the file config, so
        a key present there is NEVER absent — ``get_feature(key, None)`` would
        always return the registry value and the legacy-months fallback in
        ``_window_days`` would become dead code. An existing install whose
        ``config.yaml`` still says ``upcoming_releases_lookback_months: 3``
        would silently snap back to the default instead of honouring 3 months.

        The sibling ``upcoming_releases_purge_days`` key is absent from the
        registry for the same reason: the reader supplies its own fallback.
        This test exists so the symmetry is intentional rather than accidental.
        """
        import helpers.config_helpers as ch

        registry = ch._DEFAULT_FEATURE_FLAGS
        assert "upcoming_releases_lookback_days" not in registry
        assert "upcoming_releases_lookahead_days" not in registry
        # The registry must stay small and explicit; guard against a bulk add.
        assert "upcoming_releases_purge_days" not in registry


# ---------------------------------------------------------------------------
# 2. The window is configurable from the Config page
# ---------------------------------------------------------------------------

class TestWindowIsConfigurable:
    def test_days_keys_drive_the_window(self, frozen, monkeypatch):
        start, end = _window_with_config(
            monkeypatch, frozen,
            {"upcoming_releases_lookback_days": 7, "upcoming_releases_lookahead_days": 3},
        )
        assert start.date().isoformat() == "2026-09-17"
        assert end.date().isoformat() == "2026-09-27"

    def test_zero_lookahead_is_honoured_not_treated_as_unset(self, frozen, monkeypatch):
        """`0` is a legitimate value (show nothing future-dated).

        A truthiness test would swallow it and silently fall back to the
        default — the trap this codebase has hit before with `or`.
        """
        _start, end = _window_with_config(
            monkeypatch, frozen,
            {"upcoming_releases_lookback_days": 28, "upcoming_releases_lookahead_days": 0},
        )
        assert end.date().isoformat() == "2026-09-24"

    def test_legacy_months_keys_still_work(self, frozen, monkeypatch):
        """A hand-edited config.yaml using the old keys must not break."""
        lookback, _end = _window_with_config(monkeypatch, frozen, {
            "upcoming_releases_lookback_months": 2,
        })
        assert (lookback and (_FrozenDatetime.FROZEN.date() - lookback.date()).days == 60)

    def test_the_days_key_wins_over_the_legacy_months_key(self, frozen, monkeypatch):
        start, _end = _window_with_config(monkeypatch, frozen, {
            "upcoming_releases_lookback_days": 7,
            "upcoming_releases_lookback_months": 6,
        })
        assert (_FrozenDatetime.FROZEN.date() - start.date()).days == 7

    def test_a_broken_config_falls_back_to_the_defaults(self, frozen, monkeypatch):
        """A config read that raises must never break the page."""
        import helpers.config_helpers as ch

        def _boom(key, default=None):
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(ch, "get_feature", _boom)
        start, end = frozen.get_release_window()
        assert (end.date() - start.date()).days == 42  # 28 + 14

    def test_a_non_numeric_config_falls_back(self, frozen, monkeypatch):
        start, end = _window_with_config(
            monkeypatch, frozen,
            {"upcoming_releases_lookback_days": "lots", "upcoming_releases_lookahead_days": None},
        )
        assert (end.date() - start.date()).days == 42

    def test_a_negative_value_cannot_invert_the_window(self, frozen, monkeypatch):
        start, end = _window_with_config(
            monkeypatch, frozen,
            {"upcoming_releases_lookback_days": -10, "upcoming_releases_lookahead_days": -10},
        )
        assert start <= end, "a negative setting inverted the window"


# ---------------------------------------------------------------------------
# 3. The Config page exposes the window (the project's Config UX contract)
# ---------------------------------------------------------------------------

class TestConfigPageExposesTheWindow:
    """`templates/pages/config.html` is the source of truth for settings.

    The old keys were on NEITHER tree's Config page and in NEITHER collector,
    so the user had no way to correct the window from the UI — which is why the
    bug could not be worked around.
    """

    TEMPLATES = [
        "templates/pages/config.html",
        "test_site/templates/Pages/config.html",
    ]
    COLLECTORS = [
        "static/js/config.js",
        "test_site/static/js/pages/config.js",
    ]

    @pytest.mark.parametrize("rel", TEMPLATES)
    @pytest.mark.parametrize("element_id", [
        "upcoming_releases_lookback_days",
        "upcoming_releases_lookahead_days",
    ])
    def test_the_input_exists(self, rel: str, element_id: str):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert f'id="{element_id}"' in source, f"{rel}: missing #{element_id}"

    @pytest.mark.parametrize("rel", COLLECTORS)
    @pytest.mark.parametrize("key", [
        "upcoming_releases_lookback_days",
        "upcoming_releases_lookahead_days",
    ])
    def test_the_collector_reads_it(self, rel: str, key: str):
        """An input without a collector silently discards the user's edit."""
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert f"parseNumber('{key}'" in source, f"{rel}: does not collect {key}"

    @pytest.mark.parametrize("rel", TEMPLATES)
    def test_the_defaults_match_the_shipped_behaviour(self, rel: str):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "upcoming_releases_lookback_days', 28" in source
        assert "upcoming_releases_lookahead_days', 14" in source

    @pytest.mark.parametrize("rel", COLLECTORS)
    def test_the_collector_defaults_match(self, rel: str):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "parseNumber('upcoming_releases_lookback_days', 28)" in source
        assert "parseNumber('upcoming_releases_lookahead_days', 14)" in source

    @pytest.mark.parametrize("rel", COLLECTORS)
    @pytest.mark.parametrize("key", [
        "upcoming_releases_lookback_days",
        "upcoming_releases_lookahead_days",
    ])
    def test_the_collector_does_not_swallow_a_zero(self, rel: str, key: str):
        """A literal 0 means "show nothing older" / "show nothing future-dated".

        `parseInt(...) || 28` and `getValue(id, ...) || 28` both treat 0 as
        falsy and silently replace it with the default, so the user could never
        actually disable one side of the window. parseNumber only falls back on
        NaN, which is why the collector (and the backend reader, which was
        tested above) must use it.
        """
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert f"parseNumber('{key}'" in source, f"{rel}: {key} is not read via parseNumber"
        for bad in (
            f"parseInt(getValue('{key}'",
            f"getValue('{key}'",
        ):
            assert bad not in source, (
                f"{rel}: {key} is read with `{bad}`; a literal 0 would be "
                "replaced by the fallback default"
            )

    @pytest.mark.parametrize("rel", TEMPLATES)
    def test_the_help_text_states_the_intent(self, rel: str):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        block = source[source.index('id="upcoming_releases_lookback_days"'):]
        block = block[:block.index("</div>") + 6]
        assert "weeks" in block.lower() or "days" in block.lower()


# ---------------------------------------------------------------------------
# 4. Coherence with the purge window
# ---------------------------------------------------------------------------

class TestPurgeWindowCoherence:
    def test_the_default_purge_does_not_delete_inside_the_display_window(self):
        """Purge older than N days, display back M days.

        If N < M the purge silently deletes rows the display window still wants
        to show. The defaults must not fight each other.
        """
        source = (REPO_ROOT / "templates" / "pages" / "config.html").read_text(encoding="utf-8")
        purge_block = source[source.index('id="upcoming_releases_purge_days"'):]
        purge_default = int(purge_block.split('placeholder="')[1].split('"')[0])

        display_source = source[source.index('id="upcoming_releases_lookback_days"'):]
        display_default = int(display_source.split('placeholder="')[1].split('"')[0])

        assert purge_default >= display_default, (
            f"purge default ({purge_default}d) is shorter than the display "
            f"lookback ({display_default}d), so the purge deletes rows the "
            "dashboard would still show"
        )

    def test_a_short_purge_is_raised_to_the_display_lookback(self, frozen, monkeypatch):
        """BEHAVIOURAL: the floor must actually be applied at runtime.

        Defaults (30 vs 28) happen not to overlap, but the user can configure
        ``upcoming_releases_purge_days: 7`` next to a 90-day lookback. The purge
        would then delete rows the dashboard is still showing, and the table
        would empty from the left as the scheduler ran.

        ⚠️ This is a runtime assertion on purpose. A source-level check ("the
        floor exists") would NOT have caught the logging defect this test was
        written alongside: the floor's log call used structlog kwargs style in a
        module that uses stdlib logging, which raises TypeError inside a bare
        ``except Exception: pass`` that sits *outside* the floor assignment —
        silently disabling the whole correction.
        """
        ws = frozen
        import helpers.config_helpers as ch

        def _get_feature(key, default=None):
            if key == "upcoming_releases_purge_days":
                return 7  # shorter than the 28-day default lookback
            return default

        monkeypatch.setattr(ch, "get_feature", _get_feature)

        captured: dict = {}

        class _FakeResult:
            rowcount = 0

        class _FakeSession:
            def execute(self, statement, params):
                captured["params"] = params
                captured["sql"] = str(statement)
                return _FakeResult()

        from contextlib import contextmanager

        @contextmanager
        def _fake_db_session(*_a, **_k):
            yield _FakeSession()

        monkeypatch.setattr(ws, "db_session", _fake_db_session)

        result = ws.purge_stale_upcoming_releases()

        assert "error" not in result, (
            "the purge raised instead of applying the floor: "
            f"{result.get('error')!r}"
        )
        assert result["days"] == 28, (
            f"purge stayed at {result['days']}d; it must be raised to the "
            "28-day display lookback or it deletes rows the dashboard shows"
        )
        # And the raised value must be the one actually used in the query.
        expected_cutoff = (_FrozenDatetime.now().date() - timedelta(days=28)).isoformat()
        assert captured["params"]["cutoff"] == expected_cutoff, (
            "the floor was computed but the DELETE still used the short window"
        )

    def test_a_long_purge_is_left_alone(self, frozen, monkeypatch):
        """The floor raises short values; it must not LOWER long ones.

        A user who wants a 365-day purge should keep it — the floor only
        prevents deleting inside the display window.
        """
        ws = frozen
        import helpers.config_helpers as ch

        def _get_feature(key, default=None):
            if key == "upcoming_releases_purge_days":
                return 365
            return default

        monkeypatch.setattr(ch, "get_feature", _get_feature)

        class _FakeResult:
            rowcount = 0

        class _FakeSession:
            def execute(self, statement, params):
                return _FakeResult()

        from contextlib import contextmanager

        @contextmanager
        def _fake_db_session(*_a, **_k):
            yield _FakeSession()

        monkeypatch.setattr(ws, "db_session", _fake_db_session)

        result = ws.purge_stale_upcoming_releases()
        assert result["days"] == 365, (
            f"purge was changed to {result['days']}d; the floor must only raise "
            "values that fall inside the display window"
        )

    def test_a_zero_day_purge_cannot_delete_inside_the_display_window(self, frozen, monkeypatch):
        """``purge_days: 0`` must not collapse the cutoff onto today.

        ``days`` is clamped by ``max(1, days)`` when the cutoff is computed, so a
        ``0`` reaching that line would mean "cutoff = yesterday" — deleting
        essentially the whole table, which is far worse than the bug the floor
        guards. Two independent things prevent it: the ``or 30`` fallback in the
        purge default, and the display floor. This asserts the OBSERVABLE
        invariant rather than which mechanism fires, so it stays valid if the
        ``or 30`` is ever replaced with a proper None check.
        """
        ws = frozen
        import helpers.config_helpers as ch

        def _get_feature(key, default=None):
            if key == "upcoming_releases_purge_days":
                return 0
            return default

        monkeypatch.setattr(ch, "get_feature", _get_feature)

        captured: dict = {}

        class _FakeResult:
            rowcount = 0

        class _FakeSession:
            def execute(self, statement, params):
                captured["params"] = params
                return _FakeResult()

        from contextlib import contextmanager

        @contextmanager
        def _fake_db_session(*_a, **_k):
            yield _FakeSession()

        monkeypatch.setattr(ws, "db_session", _fake_db_session)

        result = ws.purge_stale_upcoming_releases()
        assert "error" not in result, f"the purge raised: {result.get('error')!r}"

        cutoff = captured["params"]["cutoff"]
        display_start = (ws.get_release_window()[0]).date()

        # The invariant that matters: never delete inside the display window.
        assert cutoff <= display_start.isoformat(), (
            f"cutoff {cutoff} is more recent than the display window start "
            f"{display_start} — the purge would delete rows the dashboard shows"
        )
        # And never collapse onto today.
        assert cutoff <= (_FrozenDatetime.now().date() - timedelta(days=1)).isoformat(), (
            f"cutoff {cutoff} is today/yesterday, which would wipe the table"
        )

