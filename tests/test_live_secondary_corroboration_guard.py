"""Regression tests for the MusicBrainz live-type corroboration guard.

The bug
-------
An album genuinely titled "...Death of a Bachelor Tour Live" was reported by
MusicBrainz as ``album+live``, and the local detector agreed (``album+live``,
honoured from the album's stored rich type) — yet the scan logged::

    [ENRICH] MusicBrainz secondary type rejected
        reason='neither the album title nor the track titles corroborate a live
                or acoustic release; refusing to retitle tracks'
        musicbrainz_type='album+live' live_track_count=0 track_count=21

    [ENRICH] live/remix tagging evaluated is_live_album=False

so no track was ever flagged ``is_live`` for an unambiguously live release.

Root cause
----------
``_album_title_suggests_live`` (the title half of the guard) matched a PRIVATE
copy of the live-album patterns, ``album_stage._LIVE_ALBUM_PATTERNS``. That
copy is a NARROW list tuned for classifying a title with no other evidence and
is missing the bare trailing-`` Live `` form (``\\s+live\\s*$``) that the
canonical list in ``services/catalog/album_classification_service.py`` has. The
album title ends in exactly "…Tour Live", so the guard saw "no corroboration",
rejected MusicBrainz's own answer, and downgraded ``album+live`` to ``album``.

Because the guard runs in BOTH ``_resolve_album_type`` and
``enrich_album_extras``, the downgrade reached ``_apply_live_remix_album_tagging``
and left ``is_live_album=False``.

The fix is to delegate to the canonical ``is_live_or_alternate_album`` rather
than maintain a second pattern list that can drift. These tests pin both the
specific case and the no-regression property that made the delegation safe.
"""

from __future__ import annotations

import pytest

from services.catalog.album_classification_service import (
    is_live_album_enhanced,
    is_live_or_alternate_album,
)
from services.popularity.stages import album_stage

# The album from the bug report. Note the typographic apostrophe (U+2019) —
# it arrives that way from the tags, and the guard must cope with it.
BUG_ALBUM = "All My Friends, We\u2019re Glorious: Death of a Bachelor Tour Live"

# 21 real track titles from that release. NONE carries a live marker, which is
# why the track-title half of the guard could never corroborate it either.
BUG_ALBUM_TRACKS = [
    {"title": t}
    for t in (
        "Don\u2019t Threaten Me With a Good Time", "This Is Gospel",
        "Death of a Bachelor", "The Ballad of Mona Lisa",
        "Movin\u2019 Out (Anthony\u2019s Song) (Billy Joel Cover)",
        "Emperor\u2019s New Clothes", "Nicotine", "Crazy=Genius",
        "Let\u2019s Kill Tonight", "Girls/Girls/Boys",
        "Bohemian Rhapsody (Queen Cover)", "LA Devotee",
        "I Write Sins Not Tragedies", "Victorious",
        "Ready to Go (Get Me Out of My Mind)", "Golden Days", "Vegas Lights",
        "A Fever You Can\u2019t Sweat Out Medley", "Hallelujah",
        "Nine in the Afternoon", "Miss Jackson",
    )
]


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

def test_title_corroborates_live_for_trailing_live_album():
    """The bug album's title ends in " Live" — that IS a live marker."""
    assert album_stage._album_title_suggests_live(BUG_ALBUM) is True


def test_guard_accepts_album_plus_live_for_the_bug_album():
    """The full guard must accept MusicBrainz's album+live for this album.

    This is the assertion that would have failed before the fix: the guard
    returned False, downgrading the type and leaving is_live_album=False.
    """
    assert (
        album_stage._mb_type_is_corroborated(
            "album+live", BUG_ALBUM, BUG_ALBUM_TRACKS, {"artist": "Panic! at the Disco"}
        )
        is True
    )


# NOTE: ``test_bug_album_corroborates_through_the_canonical_detector`` used to
# be defined BOTH here and again after ``test_is_live_or_alternate_album_is_the
# _correct_delegation_target``. Identical bodies, so Python silently kept only
# the second and the first could never run — a test that cannot fail protects
# nothing. The surviving copy sits with the delegation tests, where it belongs.


def test_acoustic_secondary_still_corroborates():
    """+acoustic must remain corroborated — it is a destructive type too."""
    assert album_stage._album_title_suggests_live("The Acoustic Album") is True
    assert album_stage._album_title_suggests_live("MTV Unplugged") is True
    assert (
        album_stage._mb_type_is_corroborated(
            "album+acoustic", "The Acoustic Album", [], {"artist": "x"}
        )
        is True
    )


# ---------------------------------------------------------------------------
# The false-positive exemption must survive the delegation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    ["(how to live) as ghosts", "How to Live", "How to Live (Deluxe)"],
)
def test_how_to_live_is_not_treated_as_live(title: str):
    """"How to Live" is a proper name, not a format tag.

    The canonical detector carries this exemption; the point of delegating is
    that the guard inherits it instead of needing its own copy.
    """
    assert album_stage._album_title_suggests_live(title) is False


@pytest.mark.parametrize(
    "title",
    ["Greatest Hits", "Death of a Bachelor", "The Wall", "Random Album Title"],
)
def test_studio_titles_are_not_corroborated(title: str):
    """The guard must not start accepting ordinary studio albums."""
    assert album_stage._album_title_suggests_live(title) is False


# ---------------------------------------------------------------------------
# The property that made the delegation safe
# ---------------------------------------------------------------------------

#: The narrow list the guard used to match, copied verbatim from the pre-fix
#: implementation. Kept here ONLY so the superset property can be asserted;
#: production code must not reference it.
_LEGACY_NARROW_PATTERNS = (
    r"\blive\s+at\b", r"\blive\s+in\b", r"\blive\s+from\b",
    r"\blive\s+session\b", r"[\(\[]live[\)\]]\s*$",
    r"-\s*live\s*$", r",\s*live\s*$", r"\+\s*live\s*$",
    r"live\s+recording\b", r"live\s+tour\b", r"\bin\s+concert\b",
    r"\bunplugged\b", r"\bacoustic\b",
)


def _legacy_narrow(title: str) -> bool:
    import re

    return any(re.search(p, (title or "").casefold()) for p in _LEGACY_NARROW_PATTERNS)


@pytest.mark.parametrize(
    "title",
    [
        "Live at Wembley", "Live in Tokyo", "Live from Abbey Road",
        "Live Session", "The Wall (Live)", "Chaos and Disorder - Live",
        "Something, Live", "Greatest Hits + Live", "Live Recording",
        "The Live Tour", "In Concert", "MTV Unplugged",
        "Unplugged in New York", "The Acoustic Album", "Acoustic",
        BUG_ALBUM,
        "Greatest Hits", "Death of a Bachelor", "The Wall", "(how to live) as ghosts",
    ],
)
def test_delegation_never_rejects_a_previously_accepted_title(title: str):
    """Delegation must be a strict SUPERSET of the old narrow matching.

    Every title the old guard corroborated must still be corroborated — the fix
    may only ever ACCEPT more, never less. Without this, the change could have
    silently stopped corroborating acoustic/unplugged releases (which
    ``_DESTRUCTIVE_SECONDARY_TYPES`` also guards).
    """
    if _legacy_narrow(title):
        assert album_stage._album_title_suggests_live(title) is True, (
            f"{title!r} was corroborated before the delegation and must remain so"
        )


def test_is_live_or_alternate_album_is_the_correct_delegation_target():
    """Pin WHY we delegate to ``is_live_or_alternate_album``, not ``is_live_album_enhanced``.

    ``is_live_album_enhanced`` deliberately matches only unambiguous ``live``
    format tags, so it drops unplugged/acoustic coverage. Delegating to it
    would have regressed those, so the guard must use the wider detector.
    """
    acoustic_titles = ["MTV Unplugged", "Unplugged in New York", "The Acoustic Album", "Acoustic"]
    for title in acoustic_titles:
        assert is_live_or_alternate_album(title) is True
    # If this ever stops being true the comment in the guard is stale.
    assert any(is_live_or_alternate_album(t) and not is_live_album_enhanced(t) for t in acoustic_titles)


def test_bug_album_corroborates_through_the_canonical_detector():
    """The delegation target itself must accept the bug album."""
    assert is_live_or_alternate_album(BUG_ALBUM) is True


# ---------------------------------------------------------------------------
# The remix branch is unaffected
# ---------------------------------------------------------------------------

def test_remix_is_applied_without_a_title_marker():
    """⭐ CONTRACT REVERSAL (2026-10-26) — this test used to assert the OPPOSITE.

    It read: *"+remix has its own branch (album title must say remix) —
    unchanged"* and pinned

        _mb_type_is_corroborated("album+remix", "Some Album", [], {}) is False

    That requirement was the defect. This guard exists to stop a bad match
    RENAMING files, and ``+remix`` never renames: for ``+remix``
    ``_apply_live_remix_album_tagging`` only sets ``is_remix = 1`` and injects
    the "Remix" genre — the title is untouched (unlike ``+live``/``+acoustic``,
    which do rewrite it).

    The reported case was Limp Bizkit's "New Old Songs" — a genuine remix album
    whose title contains no remix word. MusicBrainz said ``album+remix``, the
    title check rejected it, and a fresh scan filed a remix album as a studio
    album. Losing a real remix classification is worse than one track carrying
    an ``is_remix`` flag it did not earn.
    """
    # Applied regardless of the title, because it cannot retitle anything.
    assert album_stage._mb_type_is_corroborated("album+remix", "Some Album", [], {}) is True
    assert album_stage._mb_type_is_corroborated("album+remix", "New Old Songs", [], {}) is True

    # And the evidence is still RECOGNISED when it exists (it drives the log
    # message, so a classification can be audited).
    assert album_stage._album_title_suggests_remix("Remixes") is True
    assert album_stage._album_title_suggests_remix("Re-Works") is True
    assert album_stage._album_title_suggests_remix("New Old Songs") is False


def test_the_remix_evidence_recognises_the_spellings_albums_use():
    """A remix collection does not always say the word "remix"."""
    for title in ("Remixes", "Remixed", "Re-Mix", "Reworked", "The Re-Works", "Refixed", "Dub Mix"):
        assert album_stage._album_title_suggests_remix(title) is True, title
    for title in ("New Old Songs", "Greatest Hits", "Version 2.0"):
        assert album_stage._album_title_suggests_remix(title) is False, title


def test_track_titles_are_remix_evidence_too():
    """Mirrors the +live branch: the tracklist can carry the evidence."""
    tracks = [{"title": f"Song {i} (Remix)"} for i in range(4)] + [{"title": "Plain"}]
    marked, total = album_stage._remix_track_ratio(tracks)
    assert (marked, total) == (4, 5)

    tracks = [{"title": "Plain A"}, {"title": "Plain B"}]
    assert album_stage._remix_track_ratio(tracks) == (0, 2)


def test_plain_types_always_pass_the_guard():
    """A type with no destructive marker needs no corroboration at all.

    ⚠️ ``album+compilation`` used to be listed here and the assertion FAILED —
    at baseline, before any 2026-10-26 change — because a compilation IS a
    guarded type with its own evidence branch (greatest-hits detection, title
    markers, various-artists detection). The list was describing the test
    author's intent, not the code. A compilation needs evidence; a plain
    album/single/ep does not.
    """
    for safe_type in ("album", "single", "ep"):
        assert album_stage._mb_type_is_corroborated(safe_type, "Whatever", [], {}) is True


def test_the_compilation_branch_is_guarded_not_plain():
    """CONTROL for the correction above — this is why it was never a "plain" type."""
    assert (
        album_stage._mb_type_is_corroborated(
            "album+compilation", "Some Album", [{"title": "a"}], {"artist": "X"}
        )
        is False
    )
    assert (
        album_stage._mb_type_is_corroborated(
            "album+compilation", "Greatest Hits", [{"title": "a"}], {"artist": "X"}
        )
        is True
    )


def test_guard_falls_back_to_local_patterns_if_helper_raises(monkeypatch):
    """A classification-helper failure must not reject MusicBrainz's answer.

    The guard wraps the delegation in try/except precisely so a helper bug
    cannot silently downgrade a live release; the fallback keeps the old
    narrow behaviour rather than returning False.
    """
    monkeypatch.setattr(
        album_stage,
        "is_live_or_alternate_album",
        lambda _album: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # "Live at Wembley" matches the local narrow list, so the fallback says yes.
    assert album_stage._album_title_suggests_live("Live at Wembley") is True
    # A studio album matches neither, so the fallback still says no.
    assert album_stage._album_title_suggests_live("Greatest Hits") is False
