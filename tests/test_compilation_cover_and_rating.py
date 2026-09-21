"""Two reported defects on a Various Artists compilation ("Little Nicky").

REPORT 1 — false "(Cover)" annotations.
The album page listed nine of the twelve tracks as covers:

    1  School of Hard Knocks (P.O.D. Cover)      — P.O.D.
    2  Pardon Me (Incubus Cover)                 — Incubus
    3  Change (In the House of Flies) (Deftones Cover)
    ...
    12 Be Quiet and Drive (Far Away) (acoustic) (Deftones Cover)

The credited performer IS P.O.D. / Incubus / Deftones, so each track was
recorded as a cover of its own artist. ``_detect_via_isrc`` compares the
ISRC's recording credit against ``album_artist`` — but on a compilation the
album artist is the placeholder "Various Artists", which matches no real
credit, so the comparison always passed.

REPORT 2 — compilation popularity rated on the wrong basis.
"Compilations popularity matching is supposed to be based on the track artist
song popularity, not the album itself." The scan log's header was

    📊 SCAN RESULTS: Various Artists — Little Nicky (12 Tracks)

with a plain ``Z-SCORE`` column and no ``[COMPILATION: per-track-artist
rating]`` suffix — so ``is_compilation`` was False and the per-credited-artist
catalogue rating never ran. ``post_album_star_ratings`` re-derived that flag
from ``album_results[0]["album_type"]``, a key ``track_stage`` never writes, so
it always collapsed to "".

Verified by oracle against ``origin/develop`` (ed68930b).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _NoDb:
    """Stand-in for ``db_session`` so the rating path never needs a real DB."""

    def __enter__(self):
        raise RuntimeError("no database in this test")

    def __exit__(self, *exc):
        return False


# The album's real track credits, as the track stage resolved them.
NICKY_TRACKS = [
    ("School of Hard Knocks", "P.O.D."),
    ("Pardon Me", "Incubus"),
    ("Change (In the House of Flies)", "Deftones"),
    ("(Rock) Superstar", "Cypress Hill"),
    ("Natural High", "Insolence"),
    ("Points of Authority", "Linkin Park"),
    ("Stupify (Fu's Forbidden Little Nicky remix)", "Disturbed"),
    ("Nothing", "Ünloco"),
    ("When Worlds Collide", "Powerman 5000"),
    ("Cave", "Muse"),
    ("Take a Picture", "Filter"),
    ("Be Quiet and Drive (Far Away) (acoustic)", "Deftones"),
]


class TestCoverIsNeverTheTracksOwnArtist:
    """A track cannot be a cover OF its own performer."""

    def _detector(self):
        from services.enrichment.cover_detector_impl import CoverDetector
        return CoverDetector(db_connection=None)

    def test_the_isrc_credit_is_compared_against_the_track_artist(self, monkeypatch):
        det = self._detector()

        # The ISRC resolves to a recording credited to the SAME performer.
        monkeypatch.setattr(
            det, "mb",
            type("MB", (), {
                "lookup_by_isrc": lambda self, isrc, inc=None: [
                    {"id": "rec-1", "title": "Cave",
                     "artist-credit": [{"artist": {"name": "Muse"}}],
                     "releases": [{"date": "1999-01-01"}]},
                ],
            })(),
        )

        # album_artist is the compilation placeholder — the bug's trigger.
        result = det._detect_via_isrc(
            "DEN129900321", "Cave",
            album_artist="Various Artists",
            track_artist="Muse",
        )

        assert result is None, (
            "a recording credited to the track's OWN artist must not be "
            f"reported as its original (got {result})"
        )

    def test_a_different_artist_is_still_a_cover(self, monkeypatch):
        """The guard must not disable ISRC detection altogether."""
        det = self._detector()

        monkeypatch.setattr(
            det, "mb",
            type("MB", (), {
                "lookup_by_isrc": lambda self, isrc, inc=None: [
                    {"id": "rec-1", "title": "Cave",
                     "artist-credit": [{"artist": {"name": "Someone Else"}}],
                     "releases": [{"date": "1990-01-01"}]},
                ],
            })(),
        )

        result = det._detect_via_isrc(
            "DEN129900321", "Cave",
            album_artist="Various Artists",
            track_artist="Muse",
        )

        assert result and result["artist"] == "Someone Else"

    def test_the_recorder_discards_a_self_credited_result(self, monkeypatch):
        """The funnel check catches a detour any path takes."""
        from services.enrichment.cover_detector_impl import CoverDetector

        # Reach the private ``_record`` closure by running a full album pass in
        # which the ISRC path is forced to propose the track's own artist.
        det = CoverDetector(db_connection=None)
        monkeypatch.setattr(
            det, "_detect_via_isrc",
            lambda isrc, title, album_artist, track_artist="": {
                "artist": track_artist or "Muse", "year": 1999, "confidence": "medium",
            },
        )
        monkeypatch.setattr(det, "_load_cover_checked_map", lambda ids: {})
        monkeypatch.setattr(det, "_collect_track_writers", lambda *a, **k: {})
        monkeypatch.setattr(det, "_get_track_writers", lambda t: [])

        tracks = [
            {"id": "t1", "title": "Cave", "artist": "Muse", "isrc": "X1",
             "album": "Little Nicky", "album_artist": "Various Artists"},
        ]
        results = det.detect_covers_for_album("Little Nicky", "Various Artists", tracks)

        assert results == [], f"self-credited cover leaked through: {results}"


class TestCompilationIsRatedOnTheTrackArtist:
    """The reported second issue — wrong popularity basis for compilations."""

    def _results(self):
        return [
            {"track_id": f"t{i}", "title": title, "artist": credited,
             "album": "Little Nicky", "album_artist": "Various Artists",
             "popularity_score": 50.0, "final_score": 50.0, "_raw_combined": 50.0,
             "lastfm_listeners": 0, "listenbrainz_listens": 0,
             "is_single": False, "single_confidence": "low",
             "exclude_from_stats": False}
            for i, (title, credited) in enumerate(NICKY_TRACKS)
        ]

    def test_the_scan_runner_passes_the_flags_down(self):
        """The only layer holding the album context must supply the verdict."""
        import inspect

        from services.popularity import scan_stage_runner as runner

        source = inspect.getsource(runner)
        idx = source.index("post_album_star_ratings(")
        call = source[idx: idx + 700]
        assert "is_compilation=" in call, (
            "post_album_star_ratings must be told the compilation verdict "
            "instead of re-deriving it"
        )
        assert "is_va_compilation=" in call

    def test_a_va_folder_is_detected_without_a_type_tag(self):
        """The fallback must see the credited artist, not just title/type.

        Re-derivation read only the type tag and the album TITLE, and
        "Little Nicky" carries no keyword — so a genuine compilation came back
        as a studio album.
        """
        from services.popularity.stages.finalise_stage import _resolve_compilation_flags

        is_comp, is_va = _resolve_compilation_flags(
            album_results=self._results(),
            artist="Various Artists",
            album="Little Nicky",
        )
        assert is_comp is True
        assert is_va is True

    def test_a_normal_album_is_not_a_compilation(self):
        from services.popularity.stages.finalise_stage import _resolve_compilation_flags

        rows = [dict(r, artist="Muse", album_artist="Muse", album="Absolution")
                for r in self._results()]
        is_comp, _is_va = _resolve_compilation_flags(
            album_results=rows, artist="Muse", album="Absolution",
        )
        assert is_comp is False

    def test_the_rating_uses_each_credited_artists_catalogue(self, monkeypatch):
        """Each track is compared against ITS OWN artist, not the album pool."""
        from services.popularity.stages import finalise_stage as fs

        calls: list[str] = []

        def _fake_scores(track_artist, scan_results):
            calls.append(track_artist)
            return [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]

        monkeypatch.setattr(fs, "compute_track_artist_scores", _fake_scores)
        monkeypatch.setattr(fs, "_assign_stars", lambda *a, **k: 3)
        monkeypatch.setattr(fs, "_detect_live_album", lambda rows: False)
        monkeypatch.setattr(fs, "_build_album_model", lambda *a, **k: {})
        monkeypatch.setattr(fs, "_log_scan_weights", lambda *a, **k: None)
        monkeypatch.setattr(fs, "db_session", _NoDb())

        fs.post_album_star_ratings(
            album_results=self._results(),
            artist="Various Artists",
            artist_scores=[],
            options={"sync_navidrome": False, "metadata_only": True},
            is_compilation=True,
            is_va_compilation=True,
        )

        # One lookup per DISTINCT credited artist.
        assert calls, "no per-track-artist catalogue was built"
        assert "Various Artists" not in calls, (
            "the compilation's shared album artist must not be used as the "
            f"rating catalogue (got {calls})"
        )
        assert "P.O.D." in calls and "Muse" in calls

    def test_the_scan_header_declares_the_compilation_basis(self):
        """The log suffix the user showed as missing."""
        import inspect

        from services.popularity.stages import finalise_stage as fs

        source = inspect.getsource(fs.post_album_star_ratings)
        assert "COMPILATION: per-track-artist rating" in source
        assert "CAT-Z" in source
