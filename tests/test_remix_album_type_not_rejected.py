"""A genuine remix album must stay classified as a remix album.

Reported from a scan log:

    [ENRICH] MusicBrainz secondary type rejected
      reason='album title carries no remix marker' musicbrainz_type='album+remix'
      artist='Limp Bizkit' album='New Old Songs'
    [POPULARITY] Album enriched: Limp Bizkit - New Old Songs (type=album+remix)

Two problems in two lines:

1. **The classification was discarded.** "New Old Songs" is a remix album;
   MusicBrainz said ``album+remix``; the guard threw that away because the
   album TITLE does not contain the word "remix". On a fresh scan (no stored
   type to fall back on) the album was filed as a studio album — measured.
2. **The log contradicted itself** — "rejected" immediately followed by
   ``type=album+remix``, because the STORED type survived while MusicBrainz's
   agreement was discarded.

The guard's real job is preventing a bad match from RENAMING files, and
``+remix`` never renames: it sets ``is_remix`` and injects the "Remix" genre.
``+live`` and ``+acoustic`` do rewrite titles, so their corroboration is
unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.popularity.stages import album_stage

REPORTED_ALBUM = "New Old Songs"
REPORTED_ARTIST = "Limp Bizkit"
REPORTED_RG = "add0c57c-a11d-422d-b26f-dc502e99b2bd"

#: The reported release's shape: remix versions, no "Remix" word in the titles.
REPORTED_TRACKS: list[dict[str, Any]] = [
    {"id": "t1", "title": "Nookie (For the Nookie)"},
    {"id": "t2", "title": "My Way"},
    {"id": "t3", "title": "Take a Look Around"},
    {"id": "t4", "title": "Rollin' (Air Raid Vehicle)"},
    {"id": "t5", "title": "Break Stuff"},
]


@pytest.fixture
def mb_returns_remix(monkeypatch):
    """Make the MusicBrainz type lookup answer ``album+remix`` for the album."""
    monkeypatch.setattr(
        album_stage,
        "_lookup_musicbrainz_album_type",
        lambda *a, **k: ("album+remix", REPORTED_RG),
    )
    monkeypatch.setattr(
        "services.metadata.album_lookup_skip_service.album_lookup_skip",
        lambda *a, **k: False,
    )


# ---------------------------------------------------------------------------
# 1. The reported album
# ---------------------------------------------------------------------------


class TestTheReportedRemixAlbum:
    def test_the_guard_accepts_it(self) -> None:
        assert (
            album_stage._mb_type_is_corroborated(
                "album+remix", REPORTED_ALBUM, REPORTED_TRACKS, {}
            )
            is True
        )

    def test_a_fresh_scan_classifies_it_as_remix(self, mb_returns_remix) -> None:
        """⭐ THE MEASURED DEFECT. With no stored type and no stored MBID, the
        guard downgraded a genuine remix album to a studio album."""
        detected, mb_type, rg, _raw = album_stage._resolve_album_type(
            REPORTED_ARTIST, REPORTED_ALBUM, REPORTED_ARTIST, None, REPORTED_TRACKS
        )
        assert "remix" in detected
        assert "remix" in mb_type
        assert rg == REPORTED_RG

    def test_the_title_really_has_no_marker(self) -> None:
        """Establishes the premise: the old check could never pass this album."""
        assert album_stage._album_title_suggests_remix(REPORTED_ALBUM) is False
        marked, total = album_stage._remix_track_ratio(REPORTED_TRACKS)
        assert (marked, total) == (0, 5)


# ---------------------------------------------------------------------------
# 2. Remix is not a destructive type — that is WHY it is safe to accept
# ---------------------------------------------------------------------------


class TestRemixDoesNotRetitle:
    def test_the_remix_branch_only_sets_the_flag_and_genre(self) -> None:
        """⭐ The whole justification for dropping the title requirement.

        If a future change makes ``+remix`` rewrite titles, this guard must come
        back — so the invariant is pinned here rather than in prose.
        """
        import inspect

        source = inspect.getsource(album_stage._apply_live_remix_album_tagging)
        remix_start = source.index("if is_remix_album:")
        remix_body = source[remix_start:]

        assert "is_remix = 1" in remix_body
        assert "_inject_album_genre" in remix_body
        assert "title = :title" not in remix_body, (
            "+remix now rewrites titles — its corroboration guard must be "
            "restored, because a bad match would rename files"
        )

    def test_the_live_branch_does_rewrite_titles(self) -> None:
        """CONTROL: the type whose guard must stay conservative."""
        import inspect

        source = inspect.getsource(album_stage._apply_live_remix_album_tagging)
        live_part = source[: source.index("if is_remix_album:")]
        assert "title = :title" in live_part
        assert "append_annotation_once" in live_part


# ---------------------------------------------------------------------------
# 3. The corroboration that DOES stay
# ---------------------------------------------------------------------------


class TestLiveAndCompilationAreUnchanged:
    def test_live_still_needs_evidence(self) -> None:
        """+live retitles, so an unbound, unevidenced match is still refused."""
        assert album_stage._mb_type_is_corroborated("album+live", "Some Album", [], {}) is False
        assert (
            album_stage._mb_type_is_corroborated(
                "album+live", "Some Album", [{"title": "Song (Live)"}] * 3, {}
            )
            is True
        )

    def test_an_unexplained_compilation_is_still_refused(self) -> None:
        assert (
            album_stage._mb_type_is_corroborated(
                "album+compilation", "Some Album", [{"title": "a"}, {"title": "b"}], {"artist": "X"}
            )
            is False
        )

    def test_a_plain_type_needs_no_evidence(self) -> None:
        for safe in ("album", "ep", "single"):
            assert album_stage._mb_type_is_corroborated(safe, "Whatever", [], {}) is True


# ---------------------------------------------------------------------------
# 4. MBID-first: a bound album is never second-guessed
# ---------------------------------------------------------------------------


class TestAMbidBoundAlbumIsTrusted:
    @pytest.mark.parametrize(
        "mb_type",
        ["album+remix", "album+live", "album+acoustic", "album+compilation"],
    )
    def test_a_bound_release_group_is_corroboration(self, mb_type: str) -> None:
        """⭐⭐ The album is already bound to that release group by MBID, so
        MusicBrainz's secondary type describes THAT binding — there is no
        text-search risk left to guard against."""
        tracks = [dict(t, musicbrainz_releasegroupid=REPORTED_RG) for t in REPORTED_TRACKS]
        assert album_stage._mb_type_is_corroborated(mb_type, REPORTED_ALBUM, tracks, {}) is True

    def test_an_unbound_album_gets_no_such_exemption(self) -> None:
        tracks = [dict(t, musicbrainz_releasegroupid="") for t in REPORTED_TRACKS]
        assert (
            album_stage._mb_type_is_corroborated("album+live", REPORTED_ALBUM, tracks, {})
            is False
        )

    def test_the_bound_case_keeps_the_album_type_local(self, mb_returns_remix) -> None:
        bound = [dict(t, musicbrainz_releasegroupid=REPORTED_RG) for t in REPORTED_TRACKS]
        detected, mb_type, rg, _raw = album_stage._resolve_album_type(
            REPORTED_ARTIST, REPORTED_ALBUM, REPORTED_ARTIST, None, bound
        )
        assert "remix" in detected
        assert rg == REPORTED_RG


# ---------------------------------------------------------------------------
# 5. The contradictory log line
# ---------------------------------------------------------------------------


class TestAStoredTypeIsNotReportedAsRejected:
    def test_agreement_with_the_stored_type_is_corroboration(self) -> None:
        """⭐⭐ The reported log said "rejected" and then ``type=album+remix``.

        The stored value survived while MusicBrainz's AGREEMENT was thrown away,
        which is what made the two lines contradict each other. An answer that
        matches what the album already carries cannot be a guess.
        """
        tracks = [dict(t, musicbrainz_albumtype="album+remix") for t in REPORTED_TRACKS]
        assert (
            album_stage._mb_type_is_corroborated("album+remix", "Some Album", tracks, {})
            is True
        )

    def test_a_stored_plain_album_grants_no_exemption(self) -> None:
        """CONTROL: only a RICH stored type confirms a rich MB type."""
        tracks = [dict(t, musicbrainz_albumtype="album") for t in REPORTED_TRACKS]
        assert (
            album_stage._mb_type_is_corroborated("album+live", "Some Album", tracks, {})
            is False
        )

    def test_the_resolve_path_never_reports_a_type_it_discarded(self, mb_returns_remix) -> None:
        """detected and mb_type must AGREE about remix — the log reads both."""
        detected, mb_type, _rg, _raw = album_stage._resolve_album_type(
            REPORTED_ARTIST, REPORTED_ALBUM, REPORTED_ARTIST, None, REPORTED_TRACKS
        )
        assert ("remix" in detected) == ("remix" in mb_type)
