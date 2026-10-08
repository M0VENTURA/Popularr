"""Scan windows, scheduled scans, and telling the operator what each mode does.

Three things this pins, one per section:

1. **The scan-mode key.** The honest answer to "does Finalise also run the
   prior scans?" is NO — ``finalise_only`` is dispatched alongside
   ``singles_only`` and every other stage is switched off — and before this
   nothing on any page said so.  ``components/_scan_mode_key.html`` now says
   it, and it must reach every selector, not just one.

2. **The scheduled scans are configurable from the Config page.**  The
   intervals lived only in ``config.yaml`` under ``scheduler.jobs``, which is
   where ``scheduler_service`` reads them from.

3. **A save must not delete what the page does not collect.**  ``save_config``
   REPLACES config.yaml wholesale, so the Config page's collector emitting no
   ``scheduler:`` key silently reset every scheduled-scan interval to its
   default on each save.  That is fixed twice over: the test_site collector now
   sends the section, and the route carries forward any top-level section the
   payload is missing (which is what keeps the LIVE tree safe too, since the
   live collector was deliberately left alone).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

TEST_SITE = REPO_ROOT / "test_site"
CONFIG_HTML = TEST_SITE / "templates" / "Pages" / "config.html"
CONFIG_JS = TEST_SITE / "static" / "js" / "pages" / "config.js"
MODE_KEY = TEST_SITE / "templates" / "components" / "_scan_mode_key.html"

#: Every place a scan type can be chosen.  A legend that reaches only one of
#: them is worse than none — the reader learns the rule exists somewhere and
#: cannot find it where they are.
SELECTOR_SITES = (
    "templates/components/_scan_selector.html",
    "templates/Pages/dashboard.html",
    "templates/Pages/artist_detail.html",
    "templates/components/_release_section.html",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The scan-mode key
# ---------------------------------------------------------------------------


def _strip_js_comments(source: str) -> str:
    """Remove ``//`` and ``/* */`` comments, keeping string literals intact.

    Needed because a guard's own WARNING comment quotes the pattern it is
    looking for — the comment beside this collector reads "NOT ``|| 360``",
    which a naive substring check finds.  ``//`` inside a URL is protected by
    both the quote check and the ``:`` lookbehind.
    """
    out: list[str] = []
    i, n = 0, len(source)
    quote: str | None = None
    while i < n:
        ch = source[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            if i == 0 or source[i - 1] != ":":
                while i < n and source[i] != "\n":
                    i += 1
                continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class TestTheScanModeKeySaysWhatEachModeDoes:
    def test_the_legend_exists(self):
        assert MODE_KEY.is_file(), (
            "components/_scan_mode_key.html must exist — it is the only place "
            "a reader can learn what Finalise does and does not run"
        )

    def test_it_is_reachable_from_every_selector(self):
        for rel in SELECTOR_SITES:
            path = TEST_SITE / rel
            assert path.is_file(), rel
            source = _read(path)
            assert "{% include 'components/_scan_mode_key.html' %}" in source, (
                f"{rel} offers a scan selector without the legend, so the "
                "reader has no way to know Finalise does not run the other scans"
            )

    def test_it_is_js_free(self):
        """A legend wired through JS can silently do nothing.

        It has to render correctly as plain markup, because it is included in
        templates that do not all load the same scripts.
        """
        source = _read(MODE_KEY)
        assert "<script" not in source.lower(), (
            "the legend must render without JavaScript"
        )
        # <details> is what makes it collapsible with zero JS.
        assert "<details" in source

    def test_finalise_does_not_claim_to_run_the_other_scans(self):
        """The reported question, pinned as text."""
        source = _read(MODE_KEY)
        assert "does <strong>not</strong> run metadata" in source, (
            "the legend must say Finalise does not run metadata"
        )
        assert "does <strong>not</strong> run Essentia" in source, (
            "the legend must say Finalise does not run Essentia"
        )
        assert "stale scan is <em>not</em> caught up by Finalise" in source, (
            "the legend must say a stale scan is not caught up by Finalise"
        )

    def test_finalise_is_documented_as_ignoring_the_windows(self):
        source = _read(MODE_KEY)
        assert "none — always every album" in source, (
            "Finalise bypasses every skip gate, so it has no rescan window"
        )

    def test_every_mode_is_listed(self):
        source = _read(MODE_KEY)
        for mode in (
            "All Modes",
            "Full",
            "Navidrome",
            "Metadata",
            "Popularity",
            "Singles",
            "Finalise",
            "Essentia",
        ):
            assert f"<strong>{mode}</strong>" in source, f"legend is missing {mode}"

    def test_it_points_at_the_page_that_sets_the_windows(self):
        """Otherwise the reader knows the windows exist but not where they are."""
        source = _read(MODE_KEY)
        assert "Scan Behaviour" in source


# ---------------------------------------------------------------------------
# 2. The scheduled scans are on the Config page
# ---------------------------------------------------------------------------


class TestScheduledScansAreOnTheConfigPage:
    def test_the_card_exists(self):
        source = _read(CONFIG_HTML)
        assert 'id="scheduled-scans"' in source, (
            "the scheduled-scan intervals must be editable from the Config page"
        )

    @pytest.mark.parametrize(
        "field_id,default",
        [
            ("scheduler_library_sync_interval_minutes", 360),
            ("scheduler_popularity_scan_interval_minutes", 1440),
        ],
    )
    def test_the_input_reads_its_config_key(self, field_id, default):
        """The input must render FROM config.yaml, not from a hardcoded value."""
        source = _read(CONFIG_HTML)
        assert f'id="{field_id}"' in source, f"missing input {field_id}"
        idx = source.index(f'id="{field_id}"')
        window = source[max(0, idx - 400): idx + 400]
        assert "scheduler" in window and "jobs" in window, (
            f"{field_id} must be prefilled from config scheduler.jobs, or a "
            "saved value would not round-trip"
        )
        assert str(default) in window, f"the documented default {default} must be visible"

    def test_the_card_says_the_job_is_not_a_scan_button(self):
        source = _read(CONFIG_HTML)
        idx = source.index('id="scheduled-scans"')
        window = source[idx: idx + 6000]
        assert "separate from every scan button" in window
        assert "scheduler.jobs" in window, (
            "the card must name the config key it writes, so a hand edit and "
            "the UI cannot drift apart"
        )

    def test_the_collector_emits_the_scheduler_section(self):
        """THE BUG: without this the whole section is deleted on every save."""
        script = _read(CONFIG_JS)
        assert "scheduler: Object.assign(" in script, (
            "config.js must emit `scheduler:` — save_config replaces the file "
            "wholesale, so an un-emitted top-level section is deleted on save"
        )
        assert "_schedJobs.library_sync || {}" in script, (
            "the collector must preserve scheduler.jobs entries it does not edit"
        )
        assert "_schedJobs.popularity_scan || {}" in script

    @pytest.mark.parametrize(
        "field_id",
        [
            "scheduler_library_sync_interval_minutes",
            "scheduler_popularity_scan_interval_minutes",
        ],
    )
    def test_the_collector_reads_both_ids(self, field_id, script_path=CONFIG_JS):
        script = _read(script_path)
        assert field_id in script, f"config.js never reads {field_id}"

    def test_a_zero_interval_is_not_clobbered_by_a_default(self):
        """0 means "never" — `|| 360` would silently turn it back on.

        Mirrors the ``Math.max(0, raw)`` rule the album-timeout input is held
        to for the same reason.  Comments are stripped first: the comment
        beside the collector QUOTES ``|| 360`` to say it is not used, and an
        unstripped check would then fail on its own explanation.
        """
        script = _strip_js_comments(_read(CONFIG_JS))
        for field_id, default in (
            ("scheduler_library_sync_interval_minutes", "360"),
            ("scheduler_popularity_scan_interval_minutes", "1440"),
        ):
            idx = script.index(field_id)
            window = script[idx - 200: idx + 200]
            assert "parseNumber(" in window, (
                f"{field_id} must use parseNumber, not parseInt(...) || {default}"
            )
            assert f"|| {default}" not in window, (
                f"`|| {default}` would turn an explicit 0 back into {default}"
            )


# ---------------------------------------------------------------------------
# 3. The rescan-windows summary
# ---------------------------------------------------------------------------


class TestTheRescanWindowSummary:
    def test_the_table_exists_and_is_read_only(self):
        source = _read(CONFIG_HTML)
        assert 'id="rescan-windows-summary"' in source
        idx = source.index('id="rescan-windows-summary"')
        window = source[idx: idx + 9000]
        assert "read-only" in window, (
            "a summary of the inputs must not offer a second place to edit them"
        )

    @pytest.mark.parametrize(
        "key,default",
        [
            ("album_skip_days", 7),
            ("popularity_skip_days", 7),
            ("singles_skip_days", 7),
            ("metadata_skip_days", 0),
        ],
    )
    def test_every_window_is_shown_from_the_live_config(self, key, default):
        """Rendered from config, so it can never disagree with the inputs."""
        source = _read(CONFIG_HTML)
        assert f"rw.get('{key}', {default})" in source, (
            f"the summary must read {key} from config with its real default"
        )

    def test_finalise_and_essentia_rows_explain_they_have_no_window(self):
        source = _read(CONFIG_HTML)
        idx = source.index('id="rescan-windows-summary"')
        window = source[idx: idx + 9000]
        assert "window ignored — always every album" in window
        assert "It never runs metadata, and it never runs Essentia." in window

    def test_the_old_help_text_no_longer_implies_finalise_is_covered(self):
        source = _read(CONFIG_HTML)
        assert "each scan option on the Scan page (Full, Popularity, Singles, Metadata)" not in source, (
            "the old wording listed four modes and omitted Finalise entirely"
        )
        assert "Finalise ignores every window" in source


# ---------------------------------------------------------------------------
# 4. The scheduler reads the same shape from both of its callers
# ---------------------------------------------------------------------------


class TestBothSchedulerEntryPointsReadTheSameConfig:
    SCHEDULER = REPO_ROOT / "services" / "scheduler" / "scheduler_service.py"

    @classmethod
    def _source(cls) -> str:
        return _read(cls.SCHEDULER)

    def test_a_config_save_reads_scheduler_jobs(self):
        """This was the actual defect: it read `config["jobs"]`, which never exists."""
        source = self._source()
        idx = source.index("def reschedule_jobs_from_config")
        window = source[idx: idx + 2500]
        assert '_register_default_jobs(scheduler, cfg.get("scheduler") or {}, root_cfg=cfg)' in window, (
            "a Config save must read scheduler.jobs — passing the root config "
            "reads config[\"jobs\"], a key that does not exist, which reset "
            "every custom interval to its default"
        )

    def test_boot_passes_the_root_config_for_the_watcher_fallback(self):
        source = self._source()
        idx = source.index("def get_scheduler")
        window = source[idx: idx + 2500]
        assert "_register_default_jobs(_SCHEDULER, scheduler_cfg, root_cfg=cfg)" in window, (
            "boot must hand over the root config too, or the watcher toggles "
            "are read from a scheduler.watcher path that does not exist"
        )

    def test_the_register_accepts_the_root_config_and_falls_back_to_watcher(self):
        source = self._source()
        assert 'root_cfg: dict[str, Any] | None = None' in source
        assert 'watcher = cfg.get("watcher") or root_cfg.get("watcher") or {}' in source, (
            "the top-level watcher: section is what the Config page edits"
        )

    def test_the_old_mismatched_shapes_are_gone(self):
        source = self._source()
        assert "_register_default_jobs(scheduler, cfg)" not in source, (
            "the root config still being passed directly would read config[\"jobs\"]"
        )


# ---------------------------------------------------------------------------
# 5. Behaviour: 0 disables a job, and the watcher toggle reaches it
# ---------------------------------------------------------------------------


def _fake_scheduler(existing_ids: tuple[str, ...] = ()):
    """A stopped scheduler whose store knows about ``existing_ids``.

    ``_remove_job`` only calls ``remove_job`` when it can FIND the job, so the
    cases that assert a removal have to declare the job as already persisted —
    otherwise "disabled" and "never registered" are indistinguishable.
    """
    scheduler = MagicMock()
    scheduler.get_job.return_value = None
    store = MagicMock()

    def _lookup(job_id):
        if job_id in existing_ids:
            job = MagicMock()
            job.func_ref = None
            job.trigger = None
            return job
        return None

    store.lookup_job.side_effect = _lookup
    scheduler._jobstores = {"default": store}
    return scheduler, store


def _added_job_ids(scheduler) -> list[str]:
    """Job ids passed to ``add_job``.

    The FIRST positional argument is the CALLABLE — ``_put`` pops the func out
    of ``kwargs`` — so reading ``call_args[0]`` yields ``_run_library_sync_job``
    rather than ``"library_sync"`` and every assertion silently looks at the
    wrong thing.  The id is the ``id=`` keyword.
    """
    return [c.kwargs.get("id") for c in scheduler.add_job.call_args_list]


@contextmanager
def _patched_config():
    """Neutralise the two jobs that read the live config directly."""
    with (
        patch(
            "helpers.config_helpers.get_config",
            return_value={"features": {"daily_musicbrainz_release_scan_enabled": False}},
        ),
        patch("helpers.config_helpers.get_feature", return_value=False),
    ):
        yield


class TestAZeroIntervalDisablesTheJob:
    """0 must mean "never" — it used to be mapped back onto the default."""

    @pytest.mark.parametrize("job_id", ["library_sync", "popularity_scan"])
    def test_zero_removes_the_job(self, monkeypatch, job_id):
        from services.scheduler.scheduler_service import _register_default_jobs

        scheduler, _store = _fake_scheduler(existing_ids=(job_id,))
        with _patched_config():
            _register_default_jobs(
                scheduler, {"jobs": {job_id: {"interval_minutes": 0}}}, root_cfg={}
            )

        removed = [c.args[0] for c in scheduler.remove_job.call_args_list]
        added = _added_job_ids(scheduler)
        assert job_id in removed, f"interval 0 must disable {job_id}"
        assert job_id not in added, f"interval 0 must not register {job_id}"

    @pytest.mark.parametrize("job_id,interval", [("library_sync", 90), ("popularity_scan", 90)])
    def test_a_positive_interval_is_registered(self, monkeypatch, job_id, interval):
        from services.scheduler.scheduler_service import _register_default_jobs

        scheduler, _store = _fake_scheduler()
        with _patched_config():
            _register_default_jobs(
                scheduler, {"jobs": {job_id: {"interval_minutes": interval}}}, root_cfg={}
            )

        added = _added_job_ids(scheduler)
        assert job_id in added, f"a {interval}-minute interval must register {job_id}"

    def test_a_garbage_interval_falls_back_to_the_default(self, monkeypatch):
        from services.scheduler.scheduler_service import _register_default_jobs

        scheduler, _store = _fake_scheduler()
        with _patched_config():
            _register_default_jobs(
                scheduler, {"jobs": {"library_sync": {"interval_minutes": "soon"}}},
                root_cfg={},
            )
        added = _added_job_ids(scheduler)
        assert "library_sync" in added, (
            "an unreadable value must fall back to the default, not disable the job"
        )


class TestTheWatcherToggleReachesTheScheduler:
    def test_a_root_watcher_toggle_gates_the_job(self, monkeypatch):
        """The Config page edits `watcher:`, which used to be read from a
        `scheduler.watcher` path that does not exist."""
        from services.scheduler.scheduler_service import _register_default_jobs

        scheduler, _store = _fake_scheduler(existing_ids=("popularity_scan",))
        with _patched_config():
            _register_default_jobs(
                scheduler,
                {},  # no scheduler.watcher of its own
                root_cfg={"watcher": {"auto_popularity_scan": False}},
            )

        removed = [c.args[0] for c in scheduler.remove_job.call_args_list]
        added = _added_job_ids(scheduler)
        assert "popularity_scan" in removed
        assert "popularity_scan" not in added
        # The other job has no toggle against it, so it still runs.
        assert "library_sync" in added

    def test_an_explicit_scheduler_watcher_still_wins(self, monkeypatch):
        """CONTROL — a `scheduler.watcher` copy must take precedence."""
        from services.scheduler.scheduler_service import _register_default_jobs

        scheduler, _store = _fake_scheduler()
        with _patched_config():
            _register_default_jobs(
                scheduler,
                {"watcher": {"auto_popularity_scan": True}},
                root_cfg={"watcher": {"auto_popularity_scan": False}},
            )
        added = _added_job_ids(scheduler)
        assert "popularity_scan" in added

    def test_the_existing_disabled_config_still_behaves(self, monkeypatch):
        """CONTROL — the shape the store-race suite already uses must keep working."""
        from services.scheduler.scheduler_service import _register_default_jobs

        scheduler, _store = _fake_scheduler(existing_ids=("popularity_scan",))
        cfg = {
            "jobs": {},
            "watcher": {
                "auto_import_enabled": True,
                "auto_popularity_scan": False,
                "downloads_watcher_enabled": False,
            },
        }
        with _patched_config():
            _register_default_jobs(scheduler, cfg)

        added = _added_job_ids(scheduler)
        assert "library_sync" in added
        assert "popularity_scan" not in added


# ---------------------------------------------------------------------------
# 6. A save must not delete a section the page does not collect
# ---------------------------------------------------------------------------


class TestASaveCannotDeleteUncollectedSections:
    def test_the_reader_helper_exists_and_bypasses_the_cache(self):
        from helpers.config_helpers import read_config_from_disk

        assert callable(read_config_from_disk)
        # CONFIG_PATH defaults to /dev/null in the test suite, so this is an
        # empty mapping rather than an error.
        assert isinstance(read_config_from_disk(), dict)

    def test_the_route_carries_sections_forward(self):
        source = _read(REPO_ROOT / "routes" / "ui_routes.py")
        idx = source.index("async def config_save_json")
        window = source[idx: idx + 2200]
        assert "read_config_from_disk()" in window, (
            "the save route must read what is on disk, or the live tree's "
            "collector keeps deleting scheduler: on every save"
        )
        assert "data.setdefault(_section_key, _section_value)" in window, (
            "an explicit payload value must win; only ABSENT keys are filled in"
        )

    async def test_a_payload_without_the_scheduler_section_keeps_it(
        self, client, monkeypatch, tmp_path
    ):
        """End-to-end: the live collector sends no `scheduler:` — it must survive."""
        import helpers.config_helpers as ch
        from routes import ui_routes

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {
                    "scheduler": {
                        "timezone": "UTC",
                        "jobs": {
                            "popularity_scan": {"interval_minutes": 90},
                            "library_sync": {"interval_minutes": 15},
                        },
                    },
                    "watcher": {"scan_interval": 30},
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(ch, "_CONFIG_PATH", str(cfg_file))
        ch.clear_config_cache()
        # Starting a real APScheduler against the test jobstore is not this
        # test's subject — the save path calls it, so stand it in.
        monkeypatch.setattr(ui_routes, "reschedule_jobs_from_config", lambda: {})

        response = await client.post(
            "/config/save-json", json={"watcher": {"scan_interval": 45}}
        )
        assert response.status_code == 200, await response.get_data(as_text=True)

        saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert saved["scheduler"]["jobs"]["popularity_scan"]["interval_minutes"] == 90, (
            "an uncollected section was deleted by an ordinary save"
        )
        assert saved["scheduler"]["jobs"]["library_sync"]["interval_minutes"] == 15
        assert saved["scheduler"]["timezone"] == "UTC"
        assert saved["watcher"]["scan_interval"] == 45, "the payload must still win"

    async def test_a_payload_that_carries_the_section_still_wins(
        self, client, monkeypatch, tmp_path
    ):
        """CONTROL — the test_site collector now sends it; that must not be overridden."""
        import helpers.config_helpers as ch
        from routes import ui_routes

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {"scheduler": {"jobs": {"popularity_scan": {"interval_minutes": 90}}}}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(ch, "_CONFIG_PATH", str(cfg_file))
        ch.clear_config_cache()
        monkeypatch.setattr(ui_routes, "reschedule_jobs_from_config", lambda: {})

        response = await client.post(
            "/config/save-json",
            json={"scheduler": {"jobs": {"popularity_scan": {"interval_minutes": 5}}}},
        )
        assert response.status_code == 200

        saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert saved["scheduler"]["jobs"]["popularity_scan"]["interval_minutes"] == 5
