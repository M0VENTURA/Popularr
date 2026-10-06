"""A compilation's ListenBrainz count must not be replaced by a fragment.

Reported plan (analysed, then re-targeted at the code that actually has the bug):

> ListenBrainz is returning the listen count *only* for that specific
> soundtrack release, which is why "Burn" only registered 577 listens instead
> of its true global count.

Where the bug actually is
--------------------------
`services/popularity/popularity_sources.py::get_listenbrainz_album_tracklist_with_release`
fetches the album's release tracklist (that is the
``Preloaded ListenBrainz album tracklist … release_mbid=…`` line), and
``scan_stage_runner`` then wrote it straight over the row:

```python
_cur["listenbrainz_listens"] = int(_entry["listenbrainz_listens"] or 0)
```

An **assignment**, not a reconciliation. ``prefetched_popularity`` may already
hold the recording's GLOBAL count (fetched per-recording MBID), and a
release-bound count is a *subset* of it — for a compilation the two can differ
by orders of magnitude (577 vs ~150k). The larger, already-known number was
being thrown away, which is exactly why compilation tracks scored too low.

The rule now is **the higher of the two wins**, with the ``(listens, users)``
pair kept together from whichever source won — mixing the release's listens
with the global's user count would produce a number that exists in neither
source.

Why the plan's other steps are not here
---------------------------------------
* its Step 1 snippet (``if va_compilation: mode = 'album_only'``) does not
  exist in this codebase — VA scoring goes through
  ``_apply_album_relative_normalization(..., is_compilation=is_compilation or
  is_va_compilation)`` and ``_mark_track_artist_top_band`` instead, and there
  is no ``album_only`` mode;
* Step 3's "validate album_only z-score" therefore has no such mode to
  validate;
* Step 4 needs a deployed container to run a scan against.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER = REPO_ROOT / "services" / "popularity" / "scan_stage_runner.py"


class TestTheReleaseCountNeverReplacesAGlobalOne:
    def test_the_reconciliation_is_a_max_not_an_assignment(self):
        src = SCANNER.read_text(encoding="utf-8")

        assert "_rel_listens >= _known_listens" in src, (
            "the release-bound count is assigned unconditionally — a "
            "fragmented soundtrack count overwrites the recording's global "
            "count and the track scores far too low"
        )
        assert (
            '_cur["listenbrainz_listens"] = int(_entry["listenbrainz_listens"]' not in src
        ), "the raw release count is still written straight through"

    def test_the_pair_of_values_stays_together(self):
        """Neither source's listens may be paired with the other's users."""
        src = SCANNER.read_text(encoding="utf-8")
        idx = src.index("_rel_listens >= _known_listens")
        # Start just before the anchor so the `if` itself is inside the window.
        window = src[idx - 6: idx + 520]

        assert "if _rel_listens >= _known_listens" in window, (
            "the window must start at the condition, or the assertion proves "
            "nothing about which branch writes what"
        )
        assert '_cur["listenbrainz_listens"] = _rel_listens' in window
        assert '_cur["listenbrainz_users"] = _rel_users' in window, (
            "users must be written by the same branch as listens"
        )
        assert '_lb_won = "recording-global"' in window, (
            "the other branch must exist — otherwise the global value is "
            "unreachable and the max() is not a max"
        )

    def test_the_log_says_which_source_won(self):
        """The reported expectation is verifiable from the log."""
        src = SCANNER.read_text(encoding="utf-8")
        assert "kept={_lb_won}" in src, (
            "the log must say whether the release or the global count won, or "
            "'Burn → 150k' cannot be confirmed from a scan tail"
        )

    def test_the_identity_resolution_is_untouched(self):
        """CONTROL — recording_mbid / release_track_title still come first."""
        src = SCANNER.read_text(encoding="utf-8")
        assert '_cur["recording_mbid"] = _entry.get("recording_mbid")' in src
        assert '_cur["release_track_title"] = _entry.get("release_track_title")' in src
