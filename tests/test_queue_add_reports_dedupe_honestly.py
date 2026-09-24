"""Regression tests for the dedupe false success on "add to download queue".

REPORTED: "Files I'm adding to download aren't showing."

ROOT CAUSE: ``add_to_queue`` returned ``{"success": True, "already_queued": True}``
when an existing row blocked the insert — but every caller treated ``success``
as "a row was added":

* ``static/js/downloads.js`` alerted "✅ Added to queue" unconditionally.
* ``static/js/album_detail.js`` set the green ☑ tick.
* ``test_site/static/js/pages/album.js`` called ``buttonState.setDone``.

So the user was told the track was queued while NOTHING was inserted and
nothing appeared. The blocker was frequently invisible too:

* ``matched`` / ``pending_match`` / ``duplicate`` were in
  ``BLOCKING_REQUEUE_STATUSES`` but rendered by NO list at all, and
* ``unmatched`` local-disk rows were deliberately hidden from the queue listing.

That combination — "already queued" + "not visible anywhere" — is the
contradiction this pins. A dedupe must now be REPORTED as a dedupe, and when the
blocking row is one the queue page cannot render, that must be surfaced.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _strip_js_comments(source: str) -> str:
    """Remove JS comments before pattern-matching.

    ⭐ LOAD-BEARING — the fixes' own comments QUOTE ``already_queued``, so a
    naive ``"already_queued" in body`` check is satisfied by the PROSE. Proven by
    mutation: replacing the condition with ``if (false)`` left the guard green
    because the explanatory comment above it still contained the identifier.
    """
    without_blocks = _BLOCK_COMMENT_RE.sub("", source)
    out: list[str] = []
    for line in without_blocks.splitlines():
        quote = None
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "\"'`":
                quote = ch
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                if i == 0 or line[i - 1] != ":":
                    cut = i
                    break
            i += 1
        out.append(line[:cut])
    return "\n".join(out)


def _function_body(rel: str, start_marker: str, end_marker: str) -> str:
    """The comment-stripped source of one function, located by two markers."""
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return _strip_js_comments(source[start:end])


# ---------------------------------------------------------------------------
# 1. The service contract
# ---------------------------------------------------------------------------

class TestAddToQueueReportsADedupe:
    """``already_queued`` must be distinguishable from a real insert."""

    @staticmethod
    def _fake_insert(monkeypatch, item):
        import services.queue.queue_processing_service as qps
        monkeypatch.setattr(qps, "insert_queue_item", lambda *a, **kw: item)
        monkeypatch.setattr(qps, "log_queue_event", lambda *a, **kw: None)
        monkeypatch.setattr(qps, "signal_new_item", lambda: None)
        return qps

    def test_a_dedupe_is_not_reported_as_an_insert(self, monkeypatch):
        qps = self._fake_insert(monkeypatch, {
            "id": 7, "status": "queued", "already_queued": True,
        })
        result = qps.add_to_queue("Artist", "Title")

        assert result["success"] is True, "the request WAS handled correctly"
        assert result["already_queued"] is True
        assert result["inserted"] is False, (
            "a dedupe inserts nothing, so `inserted` must be False or callers "
            "cannot tell it apart from a real add"
        )

    def test_a_real_insert_is_reported_as_an_insert(self, monkeypatch):
        qps = self._fake_insert(monkeypatch, {"id": 9, "status": "queued"})
        result = qps.add_to_queue("Artist", "Title")

        assert result["success"] is True
        assert result.get("inserted") is True
        assert not result.get("already_queued")

    def test_the_dedupe_message_names_the_existing_status(self, monkeypatch):
        """The user must learn WHY, not just "already queued"."""
        qps = self._fake_insert(monkeypatch, {
            "id": 7, "status": "downloading", "already_queued": True,
        })
        result = qps.add_to_queue("Artist", "Title")

        assert result["status"] == "downloading"
        assert "downloading" in result["message"]

    def test_a_displayable_blocker_is_flagged_displayable(self, monkeypatch):
        """A blocker the queue page CAN list is the ordinary case — the user can
        go and look at it."""
        qps = self._fake_insert(monkeypatch, {
            "id": 7, "status": "queued", "already_queued": True,
        })
        assert qps.add_to_queue("Artist", "Title")["displayable"] is True

    def test_an_unrenderable_blocker_is_flagged_not_displayable(self, monkeypatch):
        """⭐ The genuinely confusing case.

        If the blocking row's status is not renderable by the queue page, the
        user is told "already queued" about a row they cannot see, cannot clear,
        and cannot retry — so this must be surfaced rather than hidden.
        """
        qps = self._fake_insert(monkeypatch, {
            "id": 7, "status": "some_unknown_status", "already_queued": True,
        })
        result = qps.add_to_queue("Artist", "Title")

        assert result["displayable"] is False
        assert "does not list" in result["message"] or "not list" in result["message"], (
            "the message must explain that the blocking row is invisible, or the "
            "user has no way to act on the contradiction"
        )

    def test_the_displayable_flag_matches_the_shared_display_set(self, monkeypatch):
        """Pinned to the same source of truth the queue page uses, so a status
        moving in or out of the page's sections cannot leave this stale."""
        from services.queue.queue_constraints import QUEUE_DISPLAY_STATUSES

        for status in sorted(QUEUE_DISPLAY_STATUSES):
            qps = self._fake_insert(monkeypatch, {
                "id": 1, "status": status, "already_queued": True,
            })
            assert qps.add_to_queue("A", "T")["displayable"] is True, status

        qps = self._fake_insert(monkeypatch, {
            "id": 1, "status": "imported", "already_queued": True,
        })
        assert qps.add_to_queue("A", "T")["displayable"] is False


# ---------------------------------------------------------------------------
# 2. Every caller must honour it
# ---------------------------------------------------------------------------

class TestCallersDoNotClaimSuccessForADedupe:
    """A template/JS has no compiler, so this is asserted against the source of
    each caller. The three places that reported the false success are pinned so
    a future edit cannot quietly reinstate one.

    ⚠️ Assertions run on COMMENT-STRIPPED source. Without that they are vacuous:
    the fixes' own comments explain the ``already_queued`` contract, so a
    presence check passes even after the condition is deleted.
    """

    @pytest.mark.parametrize(
        "rel",
        [
            "static/js/downloads.js",
            "static/js/album_detail.js",
            "test_site/static/js/pages/album.js",
        ],
    )
    def test_the_caller_branches_on_already_queued(self, rel):
        code = _strip_js_comments((REPO_ROOT / rel).read_text(encoding="utf-8"))
        assert re.search(r"\balready_queued\b", code), (
            f"{rel} posts to /api/queue/add but never branches on "
            "`already_queued` in actual code, so a dedupe is reported to the "
            "user as a real add"
        )

    def test_the_queue_form_does_not_alert_success_unconditionally(self):
        """``addToQueue`` must BRANCH, not alert the success string directly."""
        body = _function_body(
            "static/js/downloads.js", "async function addToQueue", "function searchMusicBrainzForQueue"
        )

        # The condition must be live code, not a literal that disables the branch.
        assert re.search(r"if\s*\([^)]*already_queued[^)]*\)", body), (
            "the dedupe must be a real `if (... already_queued ...)` condition"
        )
        assert not re.search(r"if\s*\(\s*false\s*\)", body), (
            "the dedupe branch is disabled with `if (false)`"
        )
        assert body.index("already_queued") < body.index("Added to queue"), (
            "the dedupe check must come BEFORE the 'Added to queue' alert"
        )

    def test_the_album_button_does_not_show_the_success_marker_for_a_dedupe(self):
        """The rebuilt tree's queueMissingTrack must not award the SUCCESS tick
        for a dedupe — the tick means "added".

        ⚠️ It MAY still mark the button (with an info icon and the server's own
        message) because the row genuinely does exist. What must not happen is
        the dedupe falling through to the plain success ``setDone``.
        """
        body = _function_body(
            "test_site/static/js/pages/album.js", "function queueMissingTrack", "let matchTarget"
        )

        assert re.search(r"if\s*\(\s*data\.already_queued\s*\)", body), (
            "queueMissingTrack must branch explicitly on a dedupe"
        )
        dedupe_at = body.index("data.already_queued")
        branch = body[dedupe_at:]
        success_at = branch.index("Added to download queue")

        # The dedupe branch must terminate BEFORE the real-insert setDone.
        assert "return" in branch[:success_at], (
            "the dedupe branch must return before the success setDone, or a "
            "dedupe is shown as a successful add"
        )
        # And the success setDone must not be reachable as the dedupe's own arm.
        dedupe_arm = branch[: branch.index("return")]
        assert "Added to download queue" not in dedupe_arm, (
            "the dedupe arm must not use the success title"
        )

    def test_the_live_album_button_does_not_tick_a_dedupe(self):
        """Same contract for the live tree's ``queueMissingTrack``."""
        body = _function_body(
            "static/js/album_detail.js", "window.queueMissingTrack = function", "window.ignoreMissingTrack"
        )

        assert re.search(r"if\s*\(\s*data\.already_queued\s*\)", body), (
            "the live album page must branch explicitly on a dedupe"
        )
        dedupe_at = body.index("data.already_queued")
        branch = body[dedupe_at:]

        # The dedupe must return before the success tick is applied.
        assert "return" in branch, "the dedupe branch must return"
        dedupe_arm = branch[: branch.index("return")]
        assert "btn-success" not in dedupe_arm, (
            "the dedupe arm must not apply the green success class, or a dedupe "
            "is displayed as a successful add"
        )
