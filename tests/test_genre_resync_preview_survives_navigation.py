"""A finished resync PREVIEW must survive navigating away from the page.

Reported: *"When running the genre playlist resync, if I navigate away from the
page it forgets the preview, can this be kept until the system is reset so I
don't need to keep running preview if I navigate away?"*

The result was never actually lost — it is stored server-side on the preview's
progress row (``scan_states.extra_data.resync_result``). The client discarded it:
on returning, the page polls with ``silent=true``, and the poller returned early
with the comment "a stale finished row: leave the card untouched".

Two things had to be true for a safe restore:

* Apply must RECOMPUTE server-side, so a restored (possibly old) preview can only
  ever describe the current state — it cannot cause an outdated write. It does:
  ``apply_genre_playlist_resync`` re-runs ``_genre_changes(dry_run=False)`` and
  rebuilds the playlists; the client never sends the preview payload back.
* The preview and the apply SHARE one card, and on page load both progress rows
  are polled. Without an ordering rule the card would flicker between
  "Preview ready" and "Resync complete" depending on which fetch lands last.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "config.js"
SERVICE = REPO_ROOT / "services" / "enrichment" / "genre_playlist_resync_service.py"


def _src(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestTheRestoreIsSafeBecauseApplyRecomputes:
    def test_apply_does_not_take_a_preview_payload(self):
        """If apply needed the client's preview, caching it would be unsafe."""
        source = _src(REPO_ROOT / "routes" / "misc_routes.py")
        start = source.index('route("/genres/playlist-resync/apply"')
        body = source[start:start + 2000]
        assert "resync_result" not in body, (
            "apply must recompute, not consume a client-supplied preview"
        )

    def test_apply_recomputes_the_genres(self):
        body = _src(SERVICE)
        assert "_genre_changes(dry_run=False" in body

    def test_the_preview_result_is_persisted_on_the_progress_row(self):
        body = _src(SERVICE)
        assert "result=result" in body, (
            "the result rides on the progress row — that is what makes a "
            "restore possible at all"
        )


class TestThePreviewIsRestoredOnReturn:
    def _poller(self) -> str:
        source = _src(CONFIG_JS)
        start = source.index("async function pollGenrePlaylistPreview(")
        end = source.index("function _resyncWhen", start)
        return source[start:end]

    def test_the_silent_path_no_longer_returns_before_rendering(self):
        body = self._poller()
        assert "if (silent) return;" not in body, (
            "that early return is exactly the line that threw the preview away"
        )

    def test_a_completed_preview_is_rendered_on_return(self):
        body = self._poller()
        assert "if (silent) {" in body
        silent_block = body[body.index("if (silent) {"):]
        silent_block = silent_block[:silent_block.index("return;")]
        assert "data.status === 'complete'" in silent_block
        assert "renderGenrePlaylistPreview(" in silent_block

    def test_a_restored_preview_enables_apply(self):
        body = self._poller()
        silent_block = body[body.index("if (silent) {"):]
        silent_block = silent_block[:silent_block.index("return;")]
        assert "applyBtn.disabled = false" in silent_block

    def test_an_idle_row_leaves_the_card_alone(self):
        body = self._poller()
        silent_block = body[body.index("if (silent) {"):]
        silent_block = silent_block[:silent_block.index("return;")]
        assert "'idle'" not in silent_block, (
            "an idle/never-run row must not repaint anything"
        )


class TestTheNewestResultWins:
    def test_an_ordering_guard_exists(self):
        source = _src(CONFIG_JS)
        assert "_claimResyncCard" in source
        assert "let _resyncCardStamp" in source

    def test_both_pollers_use_the_guard(self):
        source = _src(CONFIG_JS)
        preview = source[source.index("async function pollGenrePlaylistPreview("):]
        preview = preview[:preview.index("function _resyncWhen")]
        apply_poll = source[source.index("async function pollGenrePlaylistResync("):]
        apply_poll = apply_poll[:apply_poll.index("// ── Excluded playlist artists")]
        assert "_claimResyncCard(" in preview
        assert "_claimResyncCard(" in apply_poll

    def test_the_guard_rejects_an_older_result(self):
        """The comparison itself: older loses, newer and equal win."""
        source = _src(CONFIG_JS)
        start = source.index("function _claimResyncCard(")
        body = source[start:start + 400]
        assert "if (stamp < _resyncCardStamp) return false;" in body
        assert "_resyncCardStamp = stamp;" in body


class TestThePreviewAgeIsShown:
    def test_the_renderer_accepts_a_completion_time(self):
        source = _src(CONFIG_JS)
        assert "function renderGenrePlaylistPreview(data, seconds, completedAt)" in source

    def test_the_summary_carries_the_computed_time(self):
        source = _src(CONFIG_JS)
        start = source.index("function renderGenrePlaylistPreview(")
        body = source[start:start + 3000]
        assert "_resyncWhen(completedAt)" in body, (
            "a restored preview may be old; its age is the honest signal that "
            "it can predate the current library state"
        )

    def test_the_apply_result_shows_its_time_too(self):
        source = _src(CONFIG_JS)
        assert "_resyncWhen(data.last_updated)" in source
