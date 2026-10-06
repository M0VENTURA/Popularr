"""The "Length differs" row can now ask Soulseek for the correct version.

Reported
--------
> When a track has **"Length differs:** *3:53* → **4:00** — this is probably a
> different version of the recording. Nothing to save: a file's length cannot
> be changed by a metadata update." Can this track be given an option to add to
> Soulseek to redownload the correct version?

The row was deliberately *informational* — there is nothing to stage, because
a metadata import cannot rewrite audio. But the underlying problem (the file
IS a different version) is only fixable by a different **file**, so the row
now carries **"Redownload correct version"**.

Design notes
------------
* It goes through the **existing** queue path — live's
  `window.queueMissingTrack(btn)` / test_site's
  `global.albumDetail.queueMissingTrack(payload, btn)` — rather than posting
  `/api/queue/add` itself, so the dedupe handling, the busy popup and the
  "a dedupe is NOT an insert" rule stay in one place instead of three.
  That is why `queueMissingTrack` had to be **exported** on test_site's
  `global.albumDetail`: it was module-private.
* The button is built with `createElement`/`setAttribute`, never string
  concatenation — a track title must not be able to break out of an HTML
  attribute.
* The server-side payload (`disc_number`, `recording_mbid`, raw `duration`) is
  covered in `tests/test_metadata_lookup_duration_check.py`, because that is
  where the duration check is already driven.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_REVIEW = REPO_ROOT / "static" / "js" / "metadata-review.js"
REBUILT_REVIEW = REPO_ROOT / "test_site" / "static" / "js" / "services" / "metadata-review.js"
REBUILT_ALBUM = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestBothTreesOfferTheButton:
    def test_they_build_and_append_it(self):
        for path in (LIVE_REVIEW, REBUILT_REVIEW):
            src = _src(path)
            assert "function buildRedownloadButton(check)" in src, (
                f"{path.name} does not build the redownload button"
            )
            assert "holder.appendChild(buildRedownloadButton(check))" in src, (
                f"{path.name} builds the button but never puts it in the row"
            )

    def test_it_is_built_without_string_concatenation(self):
        """A title must not be able to break out of an HTML attribute.

        The two trees pass the payload differently (live reads ``data-*`` off
        the button, the rebuilt tree builds an object for its ``(payload,
        btn)`` signature) — what matters is that the title is assigned as a
        VALUE, never interpolated into markup.
        """
        for path in (LIVE_REVIEW, REBUILT_REVIEW):
            src = _src(path)
            start = src.index("function buildRedownloadButton(check)")
            body = src[start: start + 2600]

            assigned = (
                "btn.dataset.title = check.title" in body
                or "title: check.title" in body
            )
            assert assigned, (
                f"{path.name} never passes the title to the queue as a value"
            )
            assert "data-title=\"' + " not in body, (
                f"{path.name} builds an attribute by concatenating the title"
            )


class TestItUsesTheExistingQueuePath:
    def test_live_calls_the_window_implementation(self):
        src = _src(LIVE_REVIEW)
        assert "window.queueMissingTrack(btn)" in src, (
            "the live tree must reuse queueMissingTrack — it owns the linked-"
            "release fallback, the busy popup and the already_queued rule"
        )

    def test_rebuilt_calls_the_exported_implementation_with_a_payload(self):
        src = _src(REBUILT_REVIEW)
        assert "global.albumDetail.queueMissingTrack(payload, btn)" in src, (
            "the rebuilt tree must go through the same queue function, not "
            "post /api/queue/add itself"
        )

    def test_rebuilt_exports_that_function(self):
        src = _src(REBUILT_ALBUM)
        assert "queueMissingTrack," in src, (
            "queueMissingTrack is module-private — without exporting it the "
            "button's handler would throw on click"
        )

    def test_the_payload_carries_the_identity_fields(self):
        src = _src(REBUILT_REVIEW)
        start = src.index("function buildRedownloadButton(check)")
        body = src[start: start + 2400]

        for field in ("recording_mbid: check.recording_mbid",
                      "duration: check.duration",
                      "disc_number: check.disc_number",
                      "title: check.title"):
            assert field in body, f"the queue payload is missing {field}"
