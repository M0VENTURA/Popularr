"""Findings picked up from a lookup must PERSIST until saved or discarded.

THE REPORT
----------
"When missing fields, missing tracks or items are picked up either from metadata
import or from matching the release from the album page, they should persist in
the database and still show on the page on each view until they are either saved
or discarded."

WHAT WAS ACTUALLY WRONG
-----------------------
The storage layer already existed and worked — `tracks.pending_mb_updates` (via
`pending_update_service`) for recommendations, and the `missing_album_tracks`
table for missing tracks. But the ONLY writer of `pending_mb_updates` was the
SCAN, and only when metadata updating is set to "recommend only".

So a proposal produced **from the album page** lived purely in browser state:

  * `POST /api/album/musicbrainz/propose` (Lookup MBID) is documented as
    "nothing is written here";
  * `POST /api/v1/albums/.../musicbrainz-compare` (Compare) persisted nothing.

Reload the page and the review was gone. The READ path was already wired
(`loadPendingRecommendations` runs on DOMContentLoaded); there was simply never
anything stored for a page-derived proposal to find.

The user's decisions:
  * Discard clears ONLY the recommendations, not the missing-track list (the
    missing list is maintained by the scan and per-row dismissal);
  * Compare should PERSIST (not remain a read-only peek);
  * if the DB write fails, WARN the user rather than fail silently.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALBUMS_API = REPO_ROOT / "routes" / "api_v1" / "albums.py"
ALBUM_ROUTES = REPO_ROOT / "routes" / "album_routes.py"
MISSING_SVC = REPO_ROOT / "services" / "metadata" / "album_missing_service.py"
PROPOSAL_SVC = REPO_ROOT / "services" / "metadata" / "metadata_proposal_service.py"
REVIEW_JS = REPO_ROOT / "test_site" / "static" / "js" / "services" / "metadata-review.js"
ALBUM_JS = REPO_ROOT / "test_site" / "static" / "js" / "pages" / "album.js"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_py_comments(source: str) -> str:
    """Blank comments AND string bodies, preserving newlines.

    Required for the "must NOT appear" assertions below: the shipped code
    documents the behaviour it replaced by NAMING it, so a naive substring check
    matches the explanation rather than the code.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch == "#":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        for quote in ('"""', "'''"):
            if source.startswith(quote, i):
                out.extend([" ", " ", " "])
                i += 3
                while i < n and not source.startswith(quote, i):
                    out.append("\n" if source[i] == "\n" else " ")
                    i += 1
                if i < n:
                    out.extend([" ", " ", " "])
                    i += 3
                break
        else:
            if ch in "\"'":
                out.append(" ")
                i += 1
                while i < n:
                    if source[i] == "\\":
                        out.extend([" ", " "])
                        i += 2
                        continue
                    if source[i] == ch:
                        out.append(" ")
                        i += 1
                        break
                    out.append("\n" if source[i] == "\n" else " ")
                    i += 1
                continue
            out.append(ch)
            i += 1
    return "".join(out)


def code_of(path: Path) -> str:
    """Source with comments and string bodies blanked out.

    Module level (not a test-class staticmethod) so every class can use it. Any
    assertion of the form "this CALL must exist" needs it, because the shipped
    code documents the behaviour by NAMING the functions it calls.
    """
    return strip_py_comments(read(path))


# ---------------------------------------------------------------------------
# 1. The compare-to-storage translation (behavioural)
# ---------------------------------------------------------------------------

class TestTheComparisonIsPersisted:
    @staticmethod
    def _capture(monkeypatch, comparison: dict):
        """Run the real helper, capturing the rows it would insert."""
        from contextlib import contextmanager

        import services.metadata.album_missing_service as ams

        written: list[dict] = []

        @contextmanager
        def _session():
            class _S:
                def execute(self, statement, params=None):
                    if "INSERT INTO missing_album_tracks" in str(statement):
                        written.append(dict(params or {}))

                    class _R:
                        def mappings(self):
                            return self

                        def all(self):
                            return []

                        def fetchall(self):
                            return []

                    return _R()

                def commit(self):
                    pass

            yield _S()

        monkeypatch.setattr(ams, "db_session", _session)
        count = ams.persist_missing_from_comparison("Band", "Album", comparison)
        return count, written

    @staticmethod
    def _comparison() -> dict:
        return {
            "success": True,
            "mb_release_mbid": "rel-123",
            "mb_year": "2007",
            "comparison": [
                {"matched": True, "mb_title": "Owned", "mb_track_number": "1",
                 "mb_disc_number": 1},
                {"matched": False, "mb_title": "Missing One", "mb_track_number": "2",
                 "mb_disc_number": 1, "mb_recording_mbid": "rec-2", "mb_artist": "Band"},
                {"matched": False, "mb_title": "Missing Two", "mb_track_number": "3",
                 "mb_disc_number": 2, "mb_recording_mbid": "rec-3", "mb_artist": "Band"},
            ],
        }

    def test_only_unmatched_entries_are_persisted(self, monkeypatch):
        count, written = self._capture(monkeypatch, self._comparison())
        assert count == 2
        assert {r["title"] for r in written} == {"Missing One", "Missing Two"}, (
            "a MATCHED track is already owned and must not be listed as missing"
        )

    def test_the_mb_prefixed_keys_are_translated(self, monkeypatch):
        """⭐ The comparison shape and the storage shape differ.

        `compare_musicbrainz_release` emits `mb_title` / `mb_track_number` /
        `mb_disc_number` / `mb_recording_mbid`, while `_persist_missing_tracks`
        reads `title` / `track_number` / `disc_number` / `recording_mbid`. A
        missing translation would silently persist NOTHING (every row skipped as
        untitled), which is exactly the "does not persist" symptom.
        """
        _, written = self._capture(monkeypatch, self._comparison())
        row = next(r for r in written if r["title"] == "Missing One")
        assert row["track_number"] == "2"
        assert row["disc_number"] == 1
        assert row["recording_mbid"] == "rec-2"
        assert row["release_id"] == "rel-123"
        assert str(row["year"]) == "2007"

    def test_an_entry_with_no_title_is_skipped(self, monkeypatch):
        comp = self._comparison()
        comp["comparison"].append(
            {"matched": False, "mb_title": "", "mb_track_number": "4", "mb_disc_number": 1}
        )
        count, written = self._capture(monkeypatch, comp)
        assert count == 2, "an untitled entry cannot be stored meaningfully"
        assert all(r["title"] for r in written)

    def test_an_empty_comparison_persists_nothing(self, monkeypatch):
        count, written = self._capture(monkeypatch, {"success": True, "comparison": []})
        assert count == 0 and written == []


# ---------------------------------------------------------------------------
# 2. The endpoints call the writers
# ---------------------------------------------------------------------------

class TestTheEndpointsPersist:
    """⚠️ These assert the CALL SITES execute the writers, not merely that the
    names appear in the file.

    A first version checked `"stash_album_recommendations" in src`, which
    SURVIVED deleting the import and the call — the string still occurred in the
    explanatory comment. Every assertion here is therefore scoped to stripped
    source (comments and docstrings removed) and anchored on the actual call.
    """

    @staticmethod
    def _code(path: Path) -> str:
        """Source with comments and string bodies blanked out."""
        return code_of(path)

    def test_the_compare_endpoint_persists_its_findings(self):
        src = self._code(ALBUMS_API)
        assert "_persist_comparison_findings(" in src, (
            "the Compare endpoint must CALL the persistence helper, not just "
            "mention it"
        )
        # The helper itself must call BOTH writers.
        idx = src.index("def _persist_comparison_findings")
        body = src[idx: src.index("def ", idx + 10)]
        assert "stash_album_recommendations(" in body, (
            "the recommendations must be stashed"
        )
        assert "persist_missing_from_comparison(" in body, (
            "the missing tracks must be persisted"
        )

    def test_the_compare_endpoint_reuses_the_comparison_it_already_has(self):
        """Without this the proposal pays a SECOND compare_musicbrainz_release,
        which for an album with no stored MBID means another release SEARCH —
        on a 1 req/s budget shared with any running scan."""
        src = self._code(ALBUMS_API)
        assert "comparison_result=comparison" in src, (
            "propose_album_metadata must be handed the comparison in hand"
        )

    def test_the_proposal_service_accepts_a_precomputed_comparison(self):
        src = read(PROPOSAL_SVC)
        assert "comparison_result: dict[str, Any] | None = None" in src
        # It must only run its own comparison when it was NOT given one.
        assert "if not comparison_result:" in self._code(PROPOSAL_SVC), (
            "the comparison must be skipped when the caller supplied one"
        )

    def test_the_propose_endpoint_stashes_its_proposal(self):
        # ⚠️ RAW source, not comment-stripped: the stash call and its import are
        # the only places the name occurs in CODE, but the identifier also lives
        # inside a string literal on the `import` line, which `code_of` blanks.
        # The window is bounded to the handler so a mention elsewhere cannot
        # satisfy it.
        src = read(ALBUM_ROUTES)
        idx = src.index("def api_album_musicbrainz_propose")
        body = src[idx: src.index("@album_bp.route", idx)]
        assert "stash_album_recommendations" in body, (
            "the Lookup MBID review must survive a reload, so it must be stashed"
        )
        assert "await asyncio.to_thread(" in body, (
            "and off the event loop"
        )
        # ⚠️ The old docstring said nothing was written, which is what made the
        # missing stash look intentional. It must now say what IS stored.
        assert "THE PROPOSAL IS STASHED" in src

    def test_the_run_off_the_event_loop(self):
        """Both writers hit the DB (and the proposal reads MusicBrainz), so they
        must not run inline on the event loop."""
        assert "await asyncio.to_thread(" in read(ALBUM_ROUTES), (
            "the album propose stash must run in a worker thread"
        )
        assert "asyncio.to_thread(" in read(ALBUMS_API), (
            "the compare persist must run in a worker thread"
        )


# ---------------------------------------------------------------------------
# 3. A failed write must be REPORTED, never swallowed
# ---------------------------------------------------------------------------

class TestAFailedWriteIsReported:
    @staticmethod
    def _code(path: Path) -> str:
        return code_of(path)

    def test_the_compare_endpoint_builds_a_stash_warning(self):
        src = self._code(ALBUMS_API)
        idx = src.index("def _persist_comparison_findings")
        body = src[idx: src.index("def ", idx + 10)]
        assert "problems.append(" in body, (
            "each failed write must be RECORDED (in stripped code, not just in "
            "a comment) so the user can be told"
        )
        # And a warning must actually be returned from what was recorded.
        assert "if not problems:" in body and "return" in body

    def test_the_compare_endpoint_returns_the_warning(self):
        src = self._code(ALBUMS_API)
        assert "stash_warning" in src, (
            "the warning must reach the client under the documented key"
        )

    def test_the_propose_endpoint_sets_a_stash_warning(self):
        # RAW source: `stash_warning` appears as a DICT KEY inside a string
        # literal, which the comment stripper blanks. Bounded to the handler.
        src = read(ALBUM_ROUTES)
        idx = src.index("def api_album_musicbrainz_propose")
        body = src[idx: src.index("@album_bp.route", idx)]
        assert "stash_warning" in body, (
            "a failed stash must be surfaced on the propose response"
        )

    def test_the_propose_endpoint_does_not_fail_the_request(self):
        """A failed STASH must not fail the review the user is looking at.

        Asserted by BOUNDING the stash block (from the stash call to the point
        the response is built) and requiring that it contains no early return.
        A window that drifted into the success `return jsonify(result)` would
        flag the response itself, which is the opposite of the property.
        """
        src = read(ALBUM_ROUTES)
        start = src.index("Could not stash album metadata proposal")
        # The stash block ends where the response is built.
        end = src.index("status_code = 200 if result.get(\"success\")", start)
        block = src[start:end]
        assert "return jsonify" not in block, (
            "a failed stash must not abort the handler — the proposal the user "
            "is reviewing is still valid and must be returned"
        )

    def test_the_review_module_warns_rather_than_claiming_success(self):
        src = read(REVIEW_JS)
        # `toast.warning` must be REACHED for the warning kind — a plain
        # substring hit could come from a comment.
        idx = src.index("function notify(")
        body = src[idx: src.index("function postJson", idx)]
        assert "global.toast.warning" in body, (
            "a failed stash reported via toast.success is how a review silently "
            "disappears"
        )
        assert "kind === 'warning'" in body, (
            "the warning kind must be what routes to toast.warning"
        )
        # And it must actually be invoked from the proposal flow.
        assert "reportStashWarning(" in strip_py_comments(src) or \
            "reportStashWarning(" in src

    def test_report_stash_warning_is_called_from_the_proposal_flow(self):
        """⭐ Deleting the CALL left the previous assertions passing, because the
        function's own definition still contained the name."""
        src = read(REVIEW_JS)
        idx = src.index("async function applyProposal")
        body = src[idx: idx + 2200]
        assert "reportStashWarning(data)" in body, (
            "applyProposal must report the warning for the review the user sees"
        )

    def test_the_compare_flow_warns_too(self):
        src = read(ALBUM_JS)
        idx = src.index("async function compareWithMusicBrainz")
        body = src[idx: src.index("function displayComparison", idx)]
        assert "data.stash_warning" in body, (
            "the Compare flow must surface the same warning"
        )
        assert "notifyError" in body, (
            "and it must be shown to the user, not merely read"
        )


# ---------------------------------------------------------------------------
# 4. Discard clears ONLY the recommendations
# ---------------------------------------------------------------------------

class TestDiscardClearsOnlyRecommendations:
    """The user's explicit choice: the missing-track list is NOT part of the
    recommendations discard — it is maintained by the scan and by per-row
    dismissal, and clearing a review must not erase it."""

    def test_discard_does_not_touch_missing_tracks(self):
        src = read(REPO_ROOT / "services" / "metadata" / "pending_update_service.py")
        idx = src.index("def discard_album_recommendations")
        body = src[idx: idx + 600]
        assert "missing_album_tracks" not in body, (
            "discarding recommendations must not clear the missing-track list"
        )
        assert "_clear_album" in body, "it clears the stashed recommendations"

    def test_clear_album_only_touches_pending_mb_updates(self):
        src = read(REPO_ROOT / "services" / "metadata" / "pending_update_service.py")
        idx = src.index("def _clear_album")
        body = src[idx: idx + 1200]
        assert "pending_mb_updates = NULL" in body
        assert "missing_album_tracks" not in body


# ---------------------------------------------------------------------------
# 5. The response shape the frontend depends on
# ---------------------------------------------------------------------------

class TestTheStashWarningContract:
    def test_the_key_name_matches_on_both_sides(self):
        """A typo on either side makes the warning invisible."""
        assert "stash_warning" in read(ALBUMS_API)
        assert "stash_warning" in read(ALBUM_ROUTES)
        assert "stash_warning" in read(REVIEW_JS)
        assert "stash_warning" in read(ALBUM_JS)
