"""An EP MusicBrainz classified as one must not be demoted to an album.

REPORTED
--------
> During the metadata scan, albums that are EPs are not always matching
> correctly and leaving them as albums.

ROOT CAUSE
----------
``_resolve_album_type`` re-bucketed MusicBrainz's release-group
``primary_type`` by **track count**:

    if mb_type in {"single", "ep"} and track_count > 6:
        mb_type = "album"

Carried over from ``old_system`` (``if track_count > 6:  # Standard EP
threshold is 3-6 tracks``), and doubly wrong here:

* ``track_count`` is the **local** tracklist (bonus tracks, live bonus cuts,
  flattened multi-disc folders) — not MusicBrainz's own release tracklist; and
* an EP with 7-8 tracks is entirely normal.

So an EP survived only while the folder held 6 or fewer tracks and silently
became an ``album`` above that — which is why it looked intermittent rather
than systematically broken.

MusicBrainz's release-group ``primary_type`` is authoritative for an EP. Only a
*single* with an implausible count is genuinely ambiguous, and that half of the
rule is kept.
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _strip_python(source: str) -> str:
    """Remove comments and docstrings so a probe cannot match its own prose.

    The fix's comment quoting the removed condition ("track_count > 6")
    would otherwise satisfy a naive substring check.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return source
    kept = [
        tok.string
        for tok in tokens
        if tok.type not in (
            tokenize.COMMENT,
            tokenize.STRING,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENCODING,
        )
    ]
    return " ".join(kept)


def _resolve(monkeypatch, mb_type, track_count, *, spotify_type=None, title="Untitled"):
    """Run ``_resolve_album_type`` with a forced MusicBrainz answer."""
    from services.popularity.stages import album_stage

    tracks = [{"title": f"{title} {i}"} for i in range(track_count)]

    monkeypatch.setattr(
        album_stage,
        "_lookup_musicbrainz_album_type",
        lambda *a, **k: (mb_type, "rg-1" if mb_type else None),
    )

    detected, resolved_mb, rg, mb_raw = album_stage._resolve_album_type(
        "Artist", title, "Artist", spotify_type, tracks
    )
    return detected, resolved_mb, rg, mb_raw


class TestMusicBrainzEpsSurviveTheTrackCountRule:
    """THE DEFECT: the local count used to demote a MusicBrainz EP."""

    @pytest.mark.parametrize("track_count", [1, 4, 6, 7, 8, 12, 20])
    def test_an_ep_stays_an_ep_at_every_track_count(self, monkeypatch, track_count):
        detected, mb, _rg, _raw = _resolve(monkeypatch, "ep", track_count)

        assert mb == "ep", (
            f"a MusicBrainz EP with {track_count} local track(s) was demoted to "
            f"{mb!r} — the count is the LOCAL folder, not the release tracklist"
        )
        assert detected == "ep", (
            f"the persisted type came out {detected!r} for a {track_count}-track "
            "release MusicBrainz calls an EP"
        )

    def test_the_local_generic_album_is_overridden_by_the_ep(self, monkeypatch):
        """The normal case: the local heuristic knows nothing about EPs."""
        from services.popularity.stages import album_stage

        assert album_stage._detect_album_type(
            "Artist", "Untitled", "Artist", None
        ) == "album", "control: a plain title detects as album locally"

        detected, mb, _rg, _raw = _resolve(monkeypatch, "ep", 9)
        assert mb == "ep"
        assert detected == "ep", (
            "MusicBrainz's EP must fill in when the local heuristic only "
            "says 'album'"
        )

    def test_no_track_count_rebucketing_remains(self):
        """The count rule must stay gone for BOTH single and ep.

        The diagnostic log it emitted ("adjusted by track count") is the
        tell that the rule is back; a source probe keeps it from being
        reintroduced unnoticed. Comments are STRIPPED first — the fix's own
        comment quotes the removed condition, and a naive substring match
        would be satisfied by that quotation.
        """
        source = _strip_python(
            (REPO_ROOT / "services" / "popularity" / "stages" / "album_stage.py")
            .read_text(encoding="utf-8")
        )
        assert "adjusted by track count" not in source, (
            "the local-track-count re-bucketing of a MusicBrainz single/ep "
            "has been reintroduced"
        )
        assert "track_count > 3" not in source
        assert "track_count > 6" not in source


class TestTheSingleHeuristicIsGoneToo:
    """The single half of the count rule was removed on 2026-10-12.

    The 2026-10-09 fix kept ``elif mb_type == "single" and track_count > 3:
    mb_type = "ep"`` on the assumption that "a single with an implausible
    count is genuinely ambiguous". A live report disproved it: a release
    MusicBrainz correctly matched to a SINGLE (with its release-group and
    release id) stayed an EP because the local folder held 4-6 tracks —
    exactly the "LOCAL folder, not the release tracklist" error the EP fix
    removed. MusicBrainz's primary_type is authoritative for single AND ep.
    """

    @pytest.mark.parametrize("track_count", [1, 2, 3, 4, 6, 7, 10, 20])
    def test_a_single_stays_a_single_at_every_track_count(self, monkeypatch, track_count):
        _mb, resolved_mb, _rg, _raw = _resolve(monkeypatch, "single", track_count)
        assert resolved_mb == "single", (
            f"a {track_count}-track release MusicBrainz calls a SINGLE resolved "
            f"to {resolved_mb!r} — the count is the LOCAL folder, not the "
            "release tracklist"
        )

    @pytest.mark.parametrize("track_count", [4, 6])
    def test_the_reported_ep_case_is_fixed(self, monkeypatch, track_count):
        """REPORTED 2026-10-12: a matched Single 'stayed as EP'."""
        detected, resolved_mb, _rg, _raw = _resolve(monkeypatch, "single", track_count)
        assert resolved_mb == "single"
        assert detected == "single", (
            "a release MusicBrainz matched to a Single must not present as an EP"
        )

    def test_a_plain_album_is_untouched(self, monkeypatch):
        detected, mb, _rg, _raw = _resolve(monkeypatch, "album", 14)
        assert mb == "album"
        assert detected == "album"


class TestStoredRichTypesStillWin:
    """CONTROL — the EP must not clobber a stored composite type."""

    @pytest.mark.parametrize(
        "stored",
        ["album+soundtrack", "album+live", "album+remix", "album+compilation"],
    )
    def test_a_stored_rich_type_is_not_replaced_by_the_ep(self, monkeypatch, stored):
        detected, mb, _rg, _raw = _resolve(
            monkeypatch, "ep", 8, spotify_type=stored, title="Some Title"
        )
        assert mb == "ep", "MusicBrainz still reports the release type"
        assert detected == stored, (
            f"a stored {stored!r} must not be overwritten by the EP — "
            "'+' in detected blocks the override on purpose"
        )
