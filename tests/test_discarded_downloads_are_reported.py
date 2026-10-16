"""A discarded download must say WHAT was discarded and WHY.

Reported: *"Could the download queue page have a section at the top for errors
that list queue items discarded and the reasons for being discarded? Such as
wrong artist (noting found artist name), wrong length (noting the length), etc"*

WHY THE INFORMATION DID NOT EXIST ANYWHERE

A downloaded file that fails the completion match is **deleted from disk** and the
queue item is rescheduled. The reason was announced once on a log line and then
existed nowhere:

* ``mark_failed`` writes ``download_queue.failure_reason`` and returns the row to
  ``queued`` — so the next attempt OVERWRITES it, and the reason is gone within
  minutes;
* ``get_failed_queue()`` reads ``WHERE status = 'failed'``, a status
  ``mark_failed`` never sets, so it never sees those rows at all;
* ``_queue_reject_failure_reason`` returned one generic sentence for every cause.

So the page could not have shown it however it was queried. This module pins the
three pieces that make it possible: the detail the MATCHER now hands back, the
sentence built from it, and the store that outlives the queue row.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Audio fixture
# ---------------------------------------------------------------------------


def _flac(path: Path, tags: dict, seconds: float = 30.0) -> str:
    """Silence of a KNOWN length carrying the given tags.

    ⚠️ ``seconds`` must be the REAL audio length. ``read_mp3_metadata`` reports
    the file's actual duration — it does not read a ``duration`` TAG — so a
    30-second file tagged as 234s still measures as 30s, which silently turns
    every "length agrees" case into a wrong-length rejection.

    ⚠️ ``tmp_path``, never ``/tmp``. The repo's older download tests write to
    ``/tmp/``, which does not exist on Windows, and four of them fail for exactly
    that reason on this machine.
    """
    import numpy as np
    import soundfile as sf
    from mutagen.flac import FLAC as MFLAC

    target = str(path)
    sr = 8000  # low rate keeps a multi-minute file cheap
    sf.write(target, np.zeros(int(sr * seconds), dtype=np.float32), sr, format="FLAC")
    audio = MFLAC(target)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return target


QUEUE_ITEM = {
    "artist": "Huey Lewis and the News",
    "title": "The Power of Love",
    "album": "Back to the Future",
    "album_artist": "Huey Lewis and the News",
    "track_number": "1",
    "duration": 234,
}


def _match(tmp_path: Path, name: str, tags: dict, seconds: float = 30.0):
    """Drive the REAL matcher and return (verdict, detail)."""
    from services.queue.queue_metadata_matcher import _metadata_matches_queue_item

    path = _flac(tmp_path / f"{name}.flac", tags, seconds)
    detail: dict = {}
    verdict = _metadata_matches_queue_item(path, dict(QUEUE_ITEM), detail=detail)
    return verdict, detail


# ---------------------------------------------------------------------------
# 1. The matcher hands back WHY
# ---------------------------------------------------------------------------


class TestTheMatcherReportsWhy:
    def test_a_wrong_length_names_both_lengths(self, tmp_path):
        """⭐ "wrong length (noting the length)" — the report's own example."""
        verdict, detail = _match(
            tmp_path, "wrong_len",
            {"title": "The Power of Love", "artist": "Huey Lewis and the News"},
            seconds=294,
        )

        assert verdict is False
        assert detail["reason"] == "wrong_length"
        assert detail["found_length_seconds"] == 294.0
        assert detail["expected_length_seconds"] == 234.0
        assert detail["length_difference_seconds"] == 60.0

    def test_a_wrong_artist_names_the_found_artist(self, tmp_path):
        """⭐ "wrong artist (noting found artist name)" — the other example."""
        verdict, detail = _match(
            tmp_path, "wrong_artist",
            {"title": "The Power of Love", "artist": "Somebody Else"},
            seconds=234,
        )

        assert verdict is False
        assert detail["reason"] == "wrong_artist"
        assert detail["found_artist"] == "Somebody Else"
        assert detail["expected_artist"] == "Huey Lewis and the News"
        # The length AGREED, so the reason must not be attributed to it.
        assert detail.get("length_matched") is True

    def test_a_title_mismatch_names_the_found_title(self, tmp_path):
        verdict, detail = _match(
            tmp_path, "wrong_title",
            {"title": "Completely Different Song", "artist": "Huey Lewis and the News"},
            seconds=234,
        )

        assert verdict is False
        assert detail["reason"] == "title_mismatch"
        assert detail["found_title"] == "Completely Different Song"

    def test_the_fields_the_report_asks_for_are_always_present(self, tmp_path):
        """Even on a rejection, so the UI never renders a hole."""
        _, detail = _match(
            tmp_path, "always",
            {"title": "The Power of Love", "artist": "Huey Lewis and the News"},
            seconds=294,
        )
        for key in ("found_artist", "found_title", "found_length_seconds",
                    "expected_length_seconds", "expected_artist"):
            assert key in detail, f"{key} missing from the detail: {detail}"

    def test_an_artist_disagreement_with_a_loose_title_is_rejected_early(self, tmp_path):
        """⚠️ The OTHER artist-rejection branch, which is easy to leave untested.

        The matcher rejects a wrong artist from two places: early, when the
        artist disagrees and the title is not an exact match; and later, inside
        the strict-duration block, when the artist disagrees but the title IS
        exact. A mutation removing only the later one was invisible to a test
        that only ever drove that one, so both are exercised deliberately here.

        The two are told apart by ``length_matched``: reaching the later branch
        means the matcher had already compared durations, which the early one
        never does.
        """
        verdict, detail = _match(
            tmp_path, "wrong_artist_loose_title",
            {"title": "Power of Love", "artist": "Somebody Else"},
            seconds=234,
        )

        assert verdict is False
        assert detail["reason"] == "wrong_artist"
        assert detail["found_artist"] == "Somebody Else"
        assert "length_matched" not in detail, (
            "this file must be rejected before the duration check runs"
        )

    def test_a_deferral_does_not_leave_tag_residue_on_a_filename_rejection(self, tmp_path):
        """⚠️ A filename decision must not be blamed on a tag.

        When the metadata pass DEFERS, the caller lets the filename decide. The
        deferral has already filled the detail with the file's tags, and those
        describe a check that reached no conclusion at all — so they are cleared
        before the filename verdict is recorded. Leaving them would put the
        file's artist and length on a row whose actual reason is "Filename does
        not match", which is how a stale field becomes a wrong explanation.
        """
        from services.downloads.download_completion_service import _file_matches_queue_item

        # No artist tag → the metadata pass cannot conclude anything.
        # A name that matches neither the queue title nor its tokens → the
        # filename pass refuses it too.
        path = _flac(tmp_path / "zzz_totally_unrelated_name.flac",
                     {"title": "The Power of Love"}, 234)
        detail: dict = {}
        matched, source = _file_matches_queue_item(path, dict(QUEUE_ITEM), detail=detail)

        assert matched is False
        assert source == "filename"
        assert detail["reason"] == "filename_mismatch"
        assert "found_artist" not in detail, f"tag residue on a filename row: {detail}"
        assert "found_length_seconds" not in detail, f"tag residue on a filename row: {detail}"

    def test_an_accepted_file_carries_no_reason(self, tmp_path):
        """⚠️ CONTROL. A reason on an ACCEPTED file would report a discard that
        never happened — the exact false positive this section must not show."""
        verdict, detail = _match(
            tmp_path, "accepted",
            {"title": "The Power of Love", "artist": "Huey Lewis and the News"},
            seconds=234,
        )
        assert verdict is True
        assert "reason" not in detail

    def test_a_deferred_file_carries_no_reason(self, tmp_path):
        """⚠️ CONTROL. ``None`` means "unclear, let filename matching decide".

        That is a DEFERRAL, not a rejection: the file may still be accepted. A
        reason recorded here would put a non-discard in the discard list.
        """
        verdict, detail = _match(
            tmp_path, "deferred", {"title": "The Power of Love"}, seconds=234
        )
        assert verdict is None
        assert "reason" not in detail

    def test_the_detail_is_optional(self, tmp_path):
        """CONTROL: omitting it must leave the old signature and behaviour alone."""
        from services.queue.queue_metadata_matcher import _metadata_matches_queue_item

        path = _flac(tmp_path / "no_detail.flac",
                     {"title": "The Power of Love", "artist": "Huey Lewis and the News"}, 234)
        assert _metadata_matches_queue_item(path, dict(QUEUE_ITEM)) is True


# ---------------------------------------------------------------------------
# 2. The sentence the operator reads
# ---------------------------------------------------------------------------


class TestTheDiscardSentence:
    def _sentence(self, detail: dict, source: str = "metadata") -> str:
        from services.downloads.download_completion_service import _discard_detail_sentence

        return _discard_detail_sentence(detail, source)

    def test_it_names_the_found_artist(self):
        sentence = self._sentence({"reason": "wrong_artist", "found_artist": "Somebody Else"})
        assert "Somebody Else" in sentence

    def test_it_names_the_lengths(self):
        sentence = self._sentence({
            "reason": "wrong_length",
            "found_length_seconds": 294.0,
            "expected_length_seconds": 234.0,
        })
        assert "4:54" in sentence, sentence
        assert "3:54" in sentence, sentence

    def test_it_falls_back_to_the_one_length_it_has(self):
        sentence = self._sentence({"reason": "wrong_length", "found_length_seconds": 294.0})
        assert "4:54" in sentence

    def test_an_empty_artist_is_stated_rather_than_left_blank(self):
        sentence = self._sentence({"reason": "wrong_artist", "found_artist": ""})
        assert "no usable artist tag" in sentence

    def test_an_unknown_reason_still_produces_a_sentence(self):
        assert self._sentence({}, "filename")

    def test_the_reason_is_derived_by_ONE_function(self):
        """⚠️ The recorder and the sentence builder must agree.

        They each derived the fallback separately at first, so a rejection with
        no reason from the matcher was STORED as ``filename_mismatch`` while its
        sentence printed the generic "did not match queue item" — a label and a
        description of different things, on the same row.
        """
        from services.downloads.download_completion_service import (
            _discard_detail_sentence,
            _discard_reason,
        )

        for detail, source in (
            ({}, "filename"),
            ({}, "unsupported_format"),
            ({"reason": "wrong_length"}, "metadata"),
        ):
            assert _discard_reason(detail, source)
            assert _discard_detail_sentence(detail, source)

    def test_every_reason_code_has_a_label(self):
        """A new code must not be added with only half the presentation.

        ⚠️ REGEX, not a line-prefix scan: several ``_reject(`` calls put the code
        on the following line, so a startswith scan silently missed
        ``wrong_length`` and reported a gap that was not there.
        """
        import inspect

        from services.downloads.download_completion_service import DISCARD_REASON_LABELS
        from services.queue import queue_metadata_matcher as qmm

        codes = set(re.findall(r'_reject\(\s*"([a-z_]+)"', inspect.getsource(qmm)))
        codes |= {"unsupported_format", "filename_mismatch"}
        assert not sorted(c for c in codes if c not in DISCARD_REASON_LABELS), (
            "a rejection code has no user-facing label"
        )


# ---------------------------------------------------------------------------
# 3. The store
# ---------------------------------------------------------------------------


class TestTheRejectionStore:
    DDL = """
    CREATE TABLE queue_rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        queue_id INTEGER, artist TEXT, title TEXT, album TEXT,
        reason TEXT NOT NULL, detail TEXT,
        found_artist TEXT, found_title TEXT,
        found_length_seconds REAL, expected_length_seconds REAL,
        filename TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """

    @pytest.fixture()
    def store(self, monkeypatch):
        """Point the repository at an in-memory DB.

        ⚠️ Patches the REPOSITORY's own ``db_session``: the module does
        ``from db.engine import db_session``, so rebinding the engine's attribute
        has no effect on it at all.
        """
        from contextlib import contextmanager

        from db.repositories import queue_rejections as repo

        lite = create_engine("sqlite://")
        with lite.begin() as conn:
            conn.execute(text(self.DDL))

        @contextmanager
        def _session():
            with lite.begin() as conn:
                yield conn

        monkeypatch.setattr(repo, "db_session", _session)
        return repo

    def test_a_rejection_round_trips(self, store):
        assert store.record_rejection(
            reason="wrong_length",
            queue_id=41,
            artist="Huey Lewis and the News",
            title="The Power of Love",
            detail="Wrong length — the file is 4:54 but the queue item is 3:54",
            found_length_seconds=294.0,
            expected_length_seconds=234.0,
            filename="wrong.flac",
        )
        rows = store.get_recent_rejections()
        assert len(rows) == 1
        assert rows[0]["reason"] == "wrong_length"
        assert rows[0]["found_length_seconds"] == 294.0
        assert rows[0]["expected_length_seconds"] == 234.0

    def test_a_blank_reason_is_refused(self, store):
        """The reason is the column the UI groups on, so it cannot be empty."""
        assert store.record_rejection(reason="") is False

    def test_counts_group_by_reason(self, store):
        store.record_rejection(reason="wrong_artist")
        store.record_rejection(reason="wrong_artist")
        store.record_rejection(reason="wrong_length")
        assert store.rejection_counts() == {"wrong_artist": 2, "wrong_length": 1}

    def test_newest_first(self, store):
        for index in range(4):
            store.record_rejection(reason="wrong_length", title=f"T{index}")
        assert [r["title"] for r in store.get_recent_rejections()][0] == "T3"

    def test_pruning_keeps_the_newest(self, store):
        for index in range(12):
            store.record_rejection(reason="wrong_length", title=f"T{index}")
        assert store.prune_rejections(keep=5) == 7
        assert [r["title"] for r in store.get_recent_rejections(limit=100)] == [
            "T11", "T10", "T9", "T8", "T7"
        ]

    def test_clear_empties_it(self, store):
        store.record_rejection(reason="wrong_artist")
        assert store.clear_rejections() == 1
        assert store.get_recent_rejections() == []

    def test_every_call_survives_a_dead_database(self, store):
        """⚠️ This runs on a DESTRUCTIVE path — the file is already deleted and
        the item rescheduled, so a bookkeeping failure must never propagate."""
        def _boom():
            raise RuntimeError("db gone")

        store.db_session = _boom
        assert store.record_rejection(reason="wrong_artist") is False
        assert store.get_recent_rejections() == []
        assert store.rejection_counts() == {}
        assert store.prune_rejections() == 0
        assert store.clear_rejections() == 0


# ---------------------------------------------------------------------------
# 4. The wiring
# ---------------------------------------------------------------------------


class TestTheApiIsServed:
    def test_the_route_exists(self):
        """The endpoint the page calls must exist in the real url_map."""
        import app as app_mod

        rules = {str(rule) for rule in app_mod.app.url_map.iter_rules()}
        assert "/api/queue/rejections" in rules

    def test_the_section_reads_that_exact_path(self):
        """⚠️ A section that requests a path the app does not serve renders as
        permanently empty — indistinguishable from "nothing was discarded",
        which is the failure the section exists to prevent. Both trees.
        """
        for rel in ("static/js/discarded-downloads.js",
                    "test_site/static/js/pages/discarded-downloads.js"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "'/api/queue/rejections'" in source, rel


class TestBothTreesRenderTheSection:
    #: (page template, component, script)
    TREES = (
        (
            "templates/pages/downloads/queue.html",
            "templates/components/_discarded_downloads.html",
            "js/discarded-downloads.js",
        ),
        (
            "test_site/templates/Pages/downloads/queue.html",
            "test_site/templates/components/_discarded_downloads.html",
            "js/pages/discarded-downloads.js",
        ),
    )

    @pytest.mark.parametrize("page,component,script", TREES, ids=["live", "rebuilt"])
    def test_the_page_includes_the_component(self, page, component, script):
        source = (REPO_ROOT / page).read_text(encoding="utf-8")
        assert "_discarded_downloads.html" in source, f"{page} does not include the section"
        assert script in source, f"{page} does not load {script}"

    @pytest.mark.parametrize("page,component,script", TREES, ids=["live", "rebuilt"])
    def test_the_section_sits_above_the_tabs(self, page, component, script):
        """⭐ "at the top" is the request. Above the tab strip means it is visible
        whichever tab is open; below a tab pane it would be hidden on most."""
        source = (REPO_ROOT / page).read_text(encoding="utf-8")
        section_at = source.index("_discarded_downloads.html")
        tabs_at = source.index("searchTabs")
        assert section_at < tabs_at, f"{page}: the section renders BELOW the tabs"

    @pytest.mark.parametrize("page,component,script", TREES, ids=["live", "rebuilt"])
    def test_the_section_starts_hidden(self, page, component, script):
        """⚠️ In the COMPONENT, not the page — a permanent empty box on a healthy
        queue is noise, and noise is what makes a real discard go unnoticed."""
        source = (REPO_ROOT / component).read_text(encoding="utf-8")
        card = re.search(r'<div[^>]*id="discardedDownloadsCard"[^>]*>', source)
        assert card, f"{component}: no card element"
        assert "d-none" in card.group(0), (
            f"{component}: the card must start hidden and be revealed by the script"
        )

    @pytest.mark.parametrize("page,component,script", TREES, ids=["live", "rebuilt"])
    def test_the_component_exposes_the_ids_the_script_drives(self, page, component, script):
        source = (REPO_ROOT / component).read_text(encoding="utf-8")
        for element_id in ("discardedDownloadsCard", "discardedList", "discardedCount"):
            assert f'id="{element_id}"' in source, f"{component}: missing #{element_id}"


class TestTheRendererEscapes:
    """⚠️ Every value comes from an ID3 tag or a filename, so all of it is
    untrusted. A tag is attacker-influenced input on a file the app downloaded."""

    def _row_html(self, item: dict) -> str:
        """Call the rebuilt tree's REAL renderer.

        ⚠️ ``esc`` prefers ``global.escapeHtml`` and falls back to its own
        escaper — the fallback is what runs here, and it is the one that matters
        on a page where downloads.js is not loaded.
        """
        import json
        import subprocess
        import shutil
        import tempfile

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is required to drive the row renderer")

        source = (REPO_ROOT / "test_site/static/js/pages/discarded-downloads.js").read_text(
            encoding="utf-8"
        )
        program = """
const fs = require('fs');
const documentStub = {
  readyState: 'complete',
  addEventListener() {},
  getElementById() { return null; },
};
const global_ = { addEventListener() {} };
new Function('window', 'document', %s)(global_, documentStub);
console.log(JSON.stringify({ html: global_.DiscardedDownloads.rowHtml(%s) }));
""" % (json.dumps(source), json.dumps(item))

        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "h.js"
            harness.write_text(program, encoding="utf-8")
            proc = subprocess.run([node, str(harness)], capture_output=True, text=True,
                                  encoding="utf-8", timeout=30)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])["html"]

    def test_a_hostile_tag_cannot_inject_markup(self):
        html = self._row_html({
            "artist": '<img src=x onerror="alert(1)">',
            "title": "Song",
            "reason": "wrong_artist",
            "detail": '<script>alert(2)</script>',
            "filename": '</div><b>bold</b>',
        })
        assert "<script>" not in html
        assert "<img" not in html
        assert "<b>bold</b>" not in html
        assert "&lt;img" in html

    def test_the_artist_and_title_are_shown(self):
        html = self._row_html({
            "artist": "Alestorm", "title": "Mexico",
            "reason": "wrong_artist", "detail": "Wrong artist — the file is credited to 'Somebody Else'",
        })
        assert "Alestorm" in html
        assert "Mexico" in html
        assert "Somebody Else" in html
