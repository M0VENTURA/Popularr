"""Album Title vs Release Name, and edition-aware recording matching.

Three defects, all reported:

1. **The album name was never cleaned.** ``strip_album_edition_marker`` pinned
   the WHOLE parenthetical in every alternative, so markers that put words
   BEFORE the keyword never matched:

       "American Idiot (Holiday Edition Deluxe)"       -> unchanged
       "American Idiot (Holiday Edition)"              -> unchanged
       "Some Album (20th Anniversary Deluxe Edition)"  -> unchanged

   The edition survived into the album name (Album Title) *and* into every
   lookup key built from it.

2. **Release Name was an arbitrary edition.** ``_select_primary_release``
   matched on release-GROUP identity — identical for every edition of an album
   — and then tie-broke on the EARLIEST date, so ``release_title`` became the
   original pressing rather than the edition in the collection. With nothing
   matching at all it fell through to the earliest release of whatever release
   list the recording had, which is how an unrelated compilation (the reported
   "Punk") became an album's Release Name.

3. **A plainly tagged track on a versioned album resolved to the studio
   recording** ("Helden X Hymnen", some titles "Song (Unplugged Version)" and
   some a bare "Song"). For the bare ones
   ``edition_annotations_compatible("Song", "Song (Unplugged Version)")`` is
   False while ``("Song", "Song")`` is True, so the search skipped the unplugged
   candidate and took the identically titled studio one — and the popularity
   figures read through that MBID were the studio version's.

Each guard asserts on the FAILURE it prevents, so a regression names itself.
"""

from __future__ import annotations

import pytest

from helpers.normalization_service import (
    extract_edition_annotation,
    strip_album_edition_marker,
)
from services.enrichment.musicbrainz_service import (
    MusicBrainzService,
    _select_primary_release,
)


def _album_version_annotation(*args, **kwargs):
    """Resolve ``album_version_annotation`` lazily.

    Imported inside the call rather than at module scope so this file still
    COLLECTS on a tree without the helper — an ImportError here would abort the
    whole module and hide every other guard's verdict behind one collection
    error instead of reporting each failure on its own.
    """
    from helpers.normalization_service import album_version_annotation

    return album_version_annotation(*args, **kwargs)


# ===========================================================================
# 1. Album-name cleaning: modifier-prefixed edition markers
# ===========================================================================

class TestEditionMarkersAreStripped:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # The reported case.
            ("American Idiot (Holiday Edition Deluxe)", "American Idiot"),
            ("American Idiot (Holiday Edition)", "American Idiot"),
            ("Some Album (20th Anniversary Deluxe Edition)", "Some Album"),
            ("Some Album (25th Anniversary Remastered Edition)", "Some Album"),
            ("Some Album (Limited Edition Bonus Disc)", "Some Album"),
            ("Some Album (Special Tour Edition)", "Some Album"),
            # Unchanged behaviour for the forms that already worked.
            ("American Idiot (Deluxe Edition)", "American Idiot"),
            ("American Idiot (Deluxe Version)", "American Idiot"),
            ("Some Album (Japanese Edition)", "Some Album"),
            ("Some Album (tour edition) (tour edition)", "Some Album"),
        ],
    )
    def test_modifier_prefixed_markers_are_stripped(self, raw: str, expected: str) -> None:
        assert strip_album_edition_marker(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            # FORM markers name a different RECORDING, not a different pressing,
            # so they must survive the stripper — including through the new
            # modifier-prefixed rule.
            "Helden X Hymnen (Unplugged)",
            "Helden X Hymnen (Unplugged Version)",
            "Some Album (Live)",
            "Some Album (Live at Wembley)",
            "Some Album (Acoustic Version)",
            "Some Album (Remix)",
            "Some Album (Instrumental)",
            "Some Album (Demo)",
        ],
    )
    def test_form_markers_are_preserved(self, raw: str) -> None:
        assert strip_album_edition_marker(raw) == raw, (
            f"{raw!r} was stripped — a version/format marker distinguishes two "
            "different recordings and must never be removed as an edition marker"
        )

    @pytest.mark.parametrize("raw", ["Some Album (Boogie Version)", "Some Album (Album)"])
    def test_a_bare_version_word_is_not_an_edition_marker(self, raw: str) -> None:
        """Only the SAFE keywords count; a bare "Version" is not one."""
        assert strip_album_edition_marker(raw) == raw


# ===========================================================================
# 2. Which release supplies ``release_title``
# ===========================================================================

def _release(rid, title, date, group=None, primary="Album", secondary=None, rg=True):
    rel = {"id": rid, "title": title, "date": date}
    if rg:
        rel["release-group"] = {
            "id": f"rg-{rid}",
            "title": group if group is not None else title,
            "primary-type": primary,
        }
        if secondary:
            rel["release-group"]["secondary-types"] = secondary
    return rel


AMERICAN_IDIOT = [
    _release("r1", "American Idiot", "2004-09-21", group="American Idiot"),
    _release("r2", "American Idiot (Holiday Edition Deluxe)", "2004-11-01", group="American Idiot"),
    # A compilation that ALSO lists the recording. It is the earliest release,
    # so the old earliest-date tie-break could hand its title back as "the
    # specific release" for the album — the reported Release Name of "Punk".
    _release("r3", "Punk", "1999-01-01", group="Punk", secondary=["Compilation"]),
    _release("r4", "Punk", "1999-01-01", group="Punk"),
]


class TestReleaseSelectionPrefersTheEditionHeld:
    def test_the_edition_in_the_collection_wins(self) -> None:
        """The reported case: Release Name must be the edition, not the original.

        The release GROUP is the same for every edition, so group-level
        matching plus an earliest-date tie-break returned the 2004 pressing and
        ``release_title`` came out as the plain album name.
        """
        chosen = _select_primary_release(AMERICAN_IDIOT, "American Idiot (Holiday Edition Deluxe)")
        assert chosen.get("title") == "American Idiot (Holiday Edition Deluxe)"

    def test_an_unrelated_release_is_never_chosen(self) -> None:
        """The reported "Punk": an unrelated compilation must not supply a name.

        Nothing in this release list resembles the album being scanned, which
        means the recording itself is probably the wrong one. No release is
        better than a wrong one — the caller keeps the library's own album name.
        """
        chosen = _select_primary_release(
            [r for r in AMERICAN_IDIOT if r["id"] in ("r3", "r4")],
            "American Idiot (Holiday Edition Deluxe)",
        )
        assert chosen == {}

    def test_group_identity_still_decides_without_an_edition(self) -> None:
        """A plain library album name falls back to the release group, as before."""
        chosen = _select_primary_release(AMERICAN_IDIOT, "American Idiot")
        assert chosen.get("title") == "American Idiot"

    def test_no_anchor_keeps_the_deterministic_fallback(self) -> None:
        """With no album context there is nothing to compare against."""
        chosen = _select_primary_release(AMERICAN_IDIOT, None)
        assert chosen.get("title") == "Punk"

    def test_a_different_edition_is_not_promoted(self) -> None:
        """Edition annotations must be COMPATIBLE, not merely similar."""
        chosen = _select_primary_release(
            AMERICAN_IDIOT, "American Idiot (Deluxe Edition)"
        )
        # The deluxe edition is not in the list, so the group fallback applies —
        # it must never be reported as one of the releases it does not match.
        assert chosen.get("title") in ("American Idiot", "American Idiot (Holiday Edition Deluxe)")


# ===========================================================================
# 3. The album's own version annotation
# ===========================================================================

class TestAlbumVersionAnnotation:
    def test_the_album_name_wins(self) -> None:
        assert _album_version_annotation(
            "Helden X Hymnen (Unplugged Version)",
            ["Song", "Song"],
        ) == "unplugged version"

    def test_majority_of_track_titles(self) -> None:
        """Only SOME tracks carry the marker — the reported shaped release."""
        assert _album_version_annotation(
            "Helden X Hymnen",
            ["A (Unplugged Version)", "B (Unplugged Version)", "C (Unplugged Version)", "D"],
        ) == "unplugged version"

    def test_one_stray_marker_does_not_redefine_the_album(self) -> None:
        """A single annotated track is not the album's version."""
        assert _album_version_annotation(
            "Album", ["A", "B", "C", "D (Unplugged Version)"],
        ) is None

    def test_a_minority_marker_is_not_the_album_version(self) -> None:
        assert _album_version_annotation(
            "Album",
            ["A (Unplugged Version)", "B", "C", "D", "E"],
        ) is None

    def test_nothing_annotated_returns_none(self) -> None:
        assert _album_version_annotation("Album", ["A", "B"]) is None
        assert _album_version_annotation("") is None

    def test_a_form_annotation_is_usable(self) -> None:
        """ "(Unplugged Version)" is an edition annotation via its "version"."""
        assert extract_edition_annotation("Song (Unplugged Version)") == "unplugged version"


# ===========================================================================
# 4. The recording search uses it
# ===========================================================================

class _FakeHttp:
    """Returns a fixed candidate list; the query is not under test."""

    def __init__(self, recordings):
        self._recordings = recordings
        self.calls = 0

    def search_recordings(self, query, **kwargs):
        self.calls += 1
        return list(self._recordings)


def _recording(rid: str, title: str, secondary=None, date: str = "2004-01-01") -> dict:
    return {
        "id": rid,
        "title": title,
        "releases": [{
            "id": f"rel-{rid}",
            "title": title,
            "date": date,
            "release-group": {
                "id": f"rg-{rid}",
                "title": title,
                "primary-type": "Album",
                "secondary-types": list(secondary or []),
            },
        }],
    }


UNPLUGGED_PROBE = [
    _recording("rec-studio", "Song"),
    _recording("rec-unplugged", "Song (Unplugged Version)"),
]


def _service(recordings) -> MusicBrainzService:
    return MusicBrainzService(http_client=_FakeHttp(recordings), enabled=True)


class TestRecordingSearchIsEditionAware:
    def test_a_plain_title_on_a_versioned_album_resolves_to_that_version(self) -> None:
        """The reported failure, end to end.

        The track's own title carries no marker, so only the ALBUM's
        annotation can identify the right recording. Without it the studio
        recording wins and the track inherits the studio version's
        ListenBrainz/Last.fm popularity.
        """
        svc = _service(UNPLUGGED_PROBE)
        mbid, _score = svc.get_suggested_mbid(
            "Song", "DArtagnan", edition_annotation="unplugged version"
        )
        assert mbid == "rec-unplugged", (
            "a plainly tagged track on an unplugged album must resolve to the "
            "unplugged recording"
        )

    def test_without_the_album_annotation_the_plain_recording_wins(self) -> None:
        """Documents the bug this change fixes — the pre-fix behaviour."""
        svc = _service(UNPLUGGED_PROBE)
        mbid, _score = svc.get_suggested_mbid("Song", "DArtagnan")
        assert mbid == "rec-studio"

    def test_a_track_that_carries_its_own_marker_is_unaffected(self) -> None:
        svc = _service(UNPLUGGED_PROBE)
        mbid, _score = svc.get_suggested_mbid(
            "Song (Unplugged Version)", "DArtagnan",
            edition_annotation="unplugged version",
        )
        assert mbid == "rec-unplugged"

    def test_falls_back_rather_than_resolving_to_nothing(self) -> None:
        """If no candidate carries the album's annotation, resolve anyway.

        Returning no MBID would cost the track all of its metadata; a plain
        recording is a better answer than none, and this is what keeps the
        change from making things worse when MusicBrainz has no such version.
        """
        svc = _service([_recording("rec-studio", "Song")])
        mbid, _score = svc.get_suggested_mbid(
            "Song", "DArtagnan", edition_annotation="unplugged version"
        )
        assert mbid == "rec-studio"

    def test_an_unrelated_title_is_still_rejected(self) -> None:
        svc = _service([_recording("rec-other", "Completely Different Song")])
        mbid, score = svc.get_suggested_mbid("Song", "DArtagnan")
        assert not mbid or score < 0.6, (
            "the annotation change must not weaken the title check"
        )

    def test_the_cache_key_separates_versions(self) -> None:
        """A studio and an unplugged cut share title+artist AND liveness.

        Without the annotation in the key, whichever was scanned first would
        answer for the other — the same defect the ``::live`` suffix fixed.
        """
        studio = MusicBrainzService._cache_key("Song", "DArtagnan")
        unplugged = MusicBrainzService._cache_key(
            "Song", "DArtagnan", annotation="unplugged version"
        )
        live = MusicBrainzService._cache_key("Song", "DArtagnan", is_live=True)
        assert len({studio, unplugged, live}) == 3

    def test_the_live_path_is_unaffected(self) -> None:
        """Live albums carry no edition annotation, so nothing changes for them.

        "(Live)" is deliberately not an edition annotation — liveness is
        handled by ``_recording_live_affinity``, and conflating the two would
        change the behaviour of every live album.
        """
        assert extract_edition_annotation("Song (Live)") is None
        assert extract_edition_annotation("American Idiot (Live)") is None
