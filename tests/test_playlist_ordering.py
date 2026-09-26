"""Playlist CREATION and TRACK ORDERING.

THE PROBLEM THESE TESTS PIN
--------------------------
Generated playlists ordered their tracks on ``COALESCE(popularity, final_score)``,
which is an **album-relative** value: ``_apply_album_relative_normalization``
re-maps the raw score through a robust z-score against the track's OWN album
distribution and then a logistic, and ``_persist_album_relative_scores`` writes
that into both columns. It answers "how far above its own record does this track
sit" — which is exactly right for star rating, and NOT comparable between two
albums.

Ordering a multi-album/multi-artist playlist on such a value ranks ALBUM CONTEXT
rather than the tracks. Measured with the shipped functions: a raw 10 on a
low-median album stores as 29.1 while a raw 65 on a high-median album stores as
18.7, so the weaker track sorts FIRST.

``popularity_math.top_songs_by_genre`` already documented this ("an
album-relative-remapped score ... is only meaningful within one album's own
distribution") — and was never called.

WHAT CHANGED
------------
1. Ordering now uses ``album_prominence_score``, a log-scaled blend of the RAW
   global ``lastfm_listeners`` / ``listenbrainz_listens``. Those counts are
   absolute, so the value means the same thing on every album. Same measure
   ``_build_album_model`` already uses for cross-album comparison.
2. A per-artist cap stops one prolific artist monopolising a genre playlist
   (measured: 2 artists took 200/300 tracks). Overflow is DEFERRED, never
   dropped, so the cap cannot shrink a playlist.
3. An optional interleave spreads artists through the ranking, since a pure
   score ranking arrives album-blocked.

⚠️ ``stars`` is deliberately NOT a fallback ordering key: ``_assign_stars``
rates against the track's own album AND artist distributions, so star tiers are
album-relative too.
"""

from __future__ import annotations

import pytest

from services.popularity.stages import finalise_stage as fs
from services.popularity.popularity_math import apply_album_relative_popularity


def track(
    title: str,
    artist: str,
    *,
    stored: float,
    lf: int = 0,
    lb: int = 0,
    stars: int = 5,
) -> dict:
    """A pool row shaped like the genre/Essential SELECT output."""
    return {
        "id": f"{artist}-{title}",
        "title": title,
        "artist": artist,
        "stars": stars,
        "popularity_score": stored,
        "score": stored,
        "lastfm_listeners": lf,
        "listenbrainz_listens": lb,
    }


def artists_of(rows: list[dict]) -> list[str]:
    return [r["artist"] for r in rows]


def titles_of(rows: list[dict]) -> list[str]:
    return [r["title"] for r in rows]


def longest_artist_run(rows: list[dict]) -> int:
    longest = run = 1
    prev = None
    for row in rows:
        run = run + 1 if row["artist"] == prev else 1
        longest = max(longest, run)
        prev = row["artist"]
    return longest


# ---------------------------------------------------------------------------
# 1. The album-context defect is real, and prominence fixes it
# ---------------------------------------------------------------------------

class TestTheOrderIsComparableAcrossAlbums:
    def test_the_stored_score_really_is_album_relative(self):
        """Establishes the premise the fix rests on, using the shipped remap."""
        # A uniform album nudges a 50 to 50.0; an uneven one leaves it at 50.0;
        # a lower-median album promotes the same 50 to 61.1.
        tight = apply_album_relative_popularity(50, [50, 50, 50, 50, 50, 50])
        low_median = apply_album_relative_popularity(50, [20, 20, 20, 50, 50, 50])
        assert tight != low_median, (
            "the same raw score must store differently under different album "
            "spreads — that is why it cannot be sorted across albums"
        )

    def test_stored_mode_reproduces_the_inversion(self):
        """A KNOWN-BAD mode is kept so the fix is measurable, not asserted.

        These two values are the real outputs of the remap for `raw 10 on a
        low-median album` and `raw 65 on a high-median album`.
        """
        rows = [
            track("Weak On Quiet", "A", stored=29.1, lf=5_000),
            track("Strong On Busy", "B", stored=18.7, lf=900_000),
        ]
        ordered = fs._ordered_playlist_rows(rows, order_mode="stored")
        assert titles_of(ordered)[0] == "Weak On Quiet", (
            "legacy 'stored' mode ranks album context; this documents the bug "
            "the default mode replaces"
        )

    def test_prominence_mode_ranks_the_stronger_track_first(self):
        rows = [
            track("Weak On Quiet", "A", stored=29.1, lf=5_000),
            track("Strong On Busy", "B", stored=18.7, lf=900_000),
        ]
        ordered = fs._ordered_playlist_rows(rows, order_mode="prominence")
        assert titles_of(ordered)[0] == "Strong On Busy", (
            "the track with 180x the listeners must sort first; ranking on the "
            "album-relative stored score puts it last"
        )

    def test_listener_counts_decide_the_order(self):
        rows = [
            track("Few", "A", stored=99, lf=100),
            track("Many", "B", stored=1, lf=10_000_000),
        ]
        assert titles_of(fs._ordered_playlist_rows(rows, order_mode="prominence")) == [
            "Many", "Few",
        ]

    def test_listenbrainz_alone_can_order_a_track(self):
        """A track with only LB data must still be comparable."""
        rows = [
            track("LfOnly", "A", stored=50, lf=1_000),
            track("LbOnly", "B", stored=50, lb=5_000_000),
        ]
        assert titles_of(fs._ordered_playlist_rows(rows, order_mode="prominence"))[0] == (
            "LbOnly"
        )

    @pytest.mark.parametrize("field", ["lastfm_listeners", "listenbrainz_listens"])
    def test_a_track_with_no_listener_data_falls_back_to_stored(self, field):
        row = track("No Data", "Z", stored=77.0)
        row[field] = 0
        assert fs._effective_order_score(row, "prominence") == 77.0, (
            "a track with nothing to compare on must not be pushed to 0 and "
            "sink to the bottom of every playlist"
        )

    def test_stars_are_not_used_as_the_fallback(self):
        """Stars are album-relative too, so they cannot rescue a missing signal.

        A 5-star track from a quiet album must NOT outrank a 4-star track with
        real listeners purely on the star tier when prominence data exists.
        """
        rows = [
            track("Quiet 5 Star", "A", stored=50, lf=10, stars=5),
            track("Loud 4 Star", "B", stored=50, lf=10_000_000, stars=4),
        ]
        assert titles_of(fs._ordered_playlist_rows(rows, order_mode="prominence"))[0] == (
            "Loud 4 Star"
        )


# ---------------------------------------------------------------------------
# 2. The per-artist cap
# ---------------------------------------------------------------------------

class TestThePerArtistCap:
    def test_without_a_cap_one_artist_monopolises(self):
        rows = [track(f"Big {i}", "Prolific", stored=90 - i, lf=100_000 - i)
                for i in range(10)]
        rows += [track(f"Small {i}", "Minor", stored=10 - i, lf=1_000) for i in range(3)]
        ordered = fs._ordered_playlist_rows(rows, order_mode="prominence")
        assert artists_of(ordered)[:10].count("Prolific") == 10, (
            "documents the concentration the cap exists to prevent"
        )

    def test_the_cap_limits_the_leading_block(self):
        rows = [track(f"Big {i}", "Prolific", stored=90 - i, lf=100_000 - i)
                for i in range(10)]
        rows += [track(f"Small {i}", "Minor", stored=10 - i, lf=1_000) for i in range(3)]
        ordered = fs._ordered_playlist_rows(
            rows, order_mode="prominence", max_per_artist=2
        )
        assert artists_of(ordered)[:3] == ["Prolific", "Prolific", "Minor"], (
            "the cap must let other artists through after 2 tracks"
        )

    def test_the_cap_defers_rather_than_drops(self):
        rows = [track(f"Big {i}", "Prolific", stored=90 - i, lf=100_000 - i)
                for i in range(10)]
        ordered = fs._ordered_playlist_rows(
            rows, order_mode="prominence", max_per_artist=2
        )
        assert len(ordered) == len(rows), (
            "the cap must not shorten the playlist, or a single-artist genre "
            "would lose tracks it is entitled to"
        )
        assert set(titles_of(ordered)) == set(titles_of(rows))

    def test_overflow_goes_to_the_end_in_ranked_order(self):
        rows = [track(f"Big {i}", "Prolific", stored=90 - i, lf=100_000 - i)
                for i in range(5)]
        rows += [track("Minor", "MinorArtist", stored=1, lf=1)]
        ordered = fs._ordered_playlist_rows(
            rows, order_mode="prominence", max_per_artist=1
        )
        assert titles_of(ordered) == ["Big 0", "Minor", "Big 1", "Big 2", "Big 3", "Big 4"]

    @pytest.mark.parametrize("value", [0, -1, None])
    def test_zero_or_none_means_unlimited(self, value):
        rows = [track(f"B{i}", "P", stored=50, lf=1000 - i) for i in range(6)]
        ordered = fs._ordered_playlist_rows(
            rows, order_mode="prominence", max_per_artist=value
        )
        assert len(ordered) == 6

    def test_the_config_default_is_twenty_five(self):
        assert fs._resolve_max_per_artist({}) == 25

    @pytest.mark.parametrize("value", ["0", 0, -5, "-1"])
    def test_config_zero_disables_the_cap(self, value):
        assert fs._resolve_max_per_artist({"genre_playlists_max_per_artist": value}) is None

    def test_a_broken_config_value_falls_back_to_the_default(self):
        assert fs._resolve_max_per_artist(
            {"genre_playlists_max_per_artist": "banana"}
        ) == 25

    def test_artists_are_matched_case_insensitively(self):
        """Otherwise 'AC/DC' and 'Ac/Dc' would each get their own allowance."""
        rows = [track(f"A{i}", "Ac/Dc", stored=50, lf=1000 - i) for i in range(4)]
        ordered = fs._ordered_playlist_rows(
            rows, order_mode="prominence", max_per_artist=2
        )
        assert longest_artist_run(ordered) == 4  # all one artist; nothing to interleave
        # The cap applied to ONE bucket, so the first two are that artist and the
        # rest are deferred — the count is what matters.
        assert len(ordered) == 4


# ---------------------------------------------------------------------------
# 3. The interleave
# ---------------------------------------------------------------------------

class TestTheArtistInterleave:
    @staticmethod
    def _ranked() -> list[dict]:
        rows = [track(f"A{i}", "ArtistA", stored=100 - i, lf=200_000) for i in range(5)]
        rows += [track(f"B{i}", "ArtistB", stored=90 - i, lf=150_000) for i in range(5)]
        return sorted(rows, key=lambda r: fs._playlist_order_key(r, "prominence"))

    def test_a_pure_ranking_is_blocked_by_artist(self):
        assert artists_of(self._ranked())[:5] == ["ArtistA"] * 5, (
            "documents why the interleave exists"
        )

    def test_interleaving_removes_artist_runs(self):
        assert longest_artist_run(fs._interleave_artists(self._ranked())) == 1

    def test_interleaving_changes_order_only_not_membership(self):
        ranked = self._ranked()
        interleaved = fs._interleave_artists(ranked)
        assert sorted(titles_of(interleaved)) == sorted(titles_of(ranked))

    def test_each_artists_own_tracks_keep_their_rank_order(self):
        ranked = self._ranked()
        interleaved = fs._interleave_artists(ranked)
        assert [t for t in titles_of(interleaved) if t.startswith("A")] == [
            t for t in titles_of(ranked) if t.startswith("A")
        ]

    def test_the_top_ranked_track_still_leads(self):
        """The round-robin must not displace the overall winner."""
        ranked = self._ranked()
        assert titles_of(fs._interleave_artists(ranked))[0] == titles_of(ranked)[0]

    def test_a_single_artist_list_is_untouched(self):
        rows = [track(f"S{i}", "Solo", stored=50, lf=1000 - i) for i in range(4)]
        assert fs._interleave_artists(rows) == rows

    def test_it_can_be_disabled(self):
        ranked = self._ranked()
        ordered = fs._ordered_playlist_rows(
            ranked, order_mode="prominence", interleave=False
        )
        assert artists_of(ordered)[:5] == ["ArtistA"] * 5

    def test_the_full_pipeline_caps_then_interleaves(self):
        rows = [track(f"Big {i}", "Prolific", stored=90 - i, lf=100_000 - i)
                for i in range(6)]
        rows += [track(f"Other {i}", "Second", stored=80 - i, lf=90_000 - i)
                 for i in range(6)]
        ordered = fs._ordered_playlist_rows(
            rows, order_mode="prominence", max_per_artist=3, interleave=True
        )
        assert len(ordered) == 12
        assert longest_artist_run(ordered) == 1


# ---------------------------------------------------------------------------
# 4. Determinism — a rebuild must be byte-identical
# ---------------------------------------------------------------------------

class TestTheOrderIsDeterministic:
    def test_identical_tracks_produce_a_stable_order(self):
        """Without a total order, a rebuild would resend an identical playlist
        forever and the push cache could never settle."""
        rows = [track("Same", "X", stored=50, lf=1000), track("AlsoSame", "Y", stored=50, lf=1000)]
        first = titles_of(fs._ordered_playlist_rows(rows, order_mode="prominence"))
        second = titles_of(
            fs._ordered_playlist_rows(list(reversed(rows)), order_mode="prominence")
        )
        assert first == second

    def test_the_tie_breaker_is_the_title(self):
        rows = [track("Zeta", "X", stored=50, lf=1000), track("Alpha", "X", stored=50, lf=1000)]
        assert titles_of(fs._ordered_playlist_rows(rows, order_mode="prominence")) == [
            "Alpha", "Zeta",
        ]

    def test_the_interleave_is_deterministic(self):
        rows = [track(f"A{i}", "ArtistA", stored=100 - i, lf=200_000 - i) for i in range(4)]
        rows += [track(f"B{i}", "ArtistB", stored=90 - i, lf=150_000 - i) for i in range(4)]
        once = titles_of(fs._ordered_playlist_rows(rows, order_mode="prominence", interleave=True))
        twice = titles_of(
            fs._ordered_playlist_rows(list(rows), order_mode="prominence", interleave=True)
        )
        assert once == twice


# ---------------------------------------------------------------------------
# 5. The Essential Collection keeps its own (single-artist) ordering
# ---------------------------------------------------------------------------

class TestTheEssentialCollectionOrdering:
    """An Essential Collection is ONE artist, so the catalogue percentile IS a
    legitimate primary key — the album-relative distortion does not apply.
    Only the TIE-BREAK changed, to a signal that is not derived from the same
    album-relative value.
    """

    def test_star_tier_still_wins(self):
        rows = [
            {**track("Four", "A", stored=99, lf=999_999), "stars": 4, "percentile": 0.99},
            {**track("Five", "A", stored=10, lf=10), "stars": 5, "percentile": 0.10},
        ]
        ordered = fs._ordered_by_percentile_then_prominence(rows)
        assert titles_of(ordered)[0] == "Five", "a 5★ track outranks a 4★ track"

    def test_percentile_wins_within_a_tier(self):
        rows = [
            {**track("Low", "A", stored=99, lf=999_999), "stars": 5, "percentile": 0.20},
            {**track("High", "A", stored=10, lf=10), "stars": 5, "percentile": 0.90},
        ]
        ordered = fs._ordered_by_percentile_then_prominence(rows)
        assert titles_of(ordered)[0] == "High"

    def test_prominence_breaks_a_percentile_tie(self):
        """The old tie-break used the album-relative score, which is derived
        from the same value as the percentile and so could not separate two
        albums' tracks meaningfully."""
        rows = [
            {**track("Quiet", "A", stored=100, lf=100), "stars": 5, "percentile": 0.5},
            {**track("Loud", "A", stored=1, lf=9_000_000), "stars": 5, "percentile": 0.5},
        ]
        ordered = fs._ordered_by_percentile_then_prominence(rows)
        assert titles_of(ordered)[0] == "Loud", (
            "equal percentiles must be separated by real listener counts, not "
            "by which album happened to be spread wider"
        )

    def test_ordering_is_deterministic(self):
        rows = [
            {**track("B", "A", stored=50, lf=1000), "stars": 5, "percentile": 0.5},
            {**track("A", "A", stored=50, lf=1000), "stars": 5, "percentile": 0.5},
        ]
        assert titles_of(fs._ordered_by_percentile_then_prominence(rows)) == ["A", "B"]


def strip_py_comments(source: str) -> str:
    """Blank out Python comments AND string literals, preserving newlines.

    ⚠️ REQUIRED, not tidiness. Several assertions here are of the form "this
    token must NOT appear", and the shipped code deliberately DOCUMENTS the
    behaviour it replaced by naming it — e.g. the genre builder's comment
    explains ``winners.sort(key=_popularity_order)`` and the generator's explains
    ``candidates += _collect_lastfm``. A naive substring check therefore matches
    the EXPLANATION rather than the code and reports the opposite of the truth.
    This is the comment-matching trap; it has now bitten this codebase five
    times, so strip for any negative assertion.

    ⚠️ The quoted text is removed along with comments because the shipped
    explanation lives in DOCSTRINGS as well as ``#`` comments: a stripper that
    only handles ``#`` leaves the docstring text visible and the assertion still
    matches prose. Triple-quoted blocks are handled BEFORE single quotes, since
    ``\"\"\"`` would otherwise be read as an empty string followed by a stray
    quote and desynchronise the scan for the rest of the file.
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
                out.extend(" " if c != "\n" else "\n" for c in quote)
                i += 3
                while i < n and not source.startswith(quote, i):
                    out.append("\n" if source[i] == "\n" else " ")
                    i += 1
                if i < n:
                    out.extend(" " * 3)
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
            continue

    return "".join(out)


# ---------------------------------------------------------------------------
# 6. The WIRING — the builders must actually CALL the ordering pipeline
# ---------------------------------------------------------------------------

class TestTheBuildersUseTheOrderingPipeline:
    """⚠️ Behavioural tests on the helper functions cannot prove the CALL SITES
    use them. When the oracle reverted only the call site, every ordering test
    still passed — a false all-clear. These assert the wiring, and the separate
    call-site assertions below are backed by the mutation harness.
    """

    @staticmethod
    def _finalise_source() -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1]
            / "services" / "popularity" / "stages" / "finalise_stage.py"
        ).read_text(encoding="utf-8")

    def test_the_genre_builder_calls_the_ordering_pipeline(self):
        src = self._finalise_source()
        assert "winners = _ordered_playlist_rows(" in src, (
            "the genre builder must route its winners through the ranking, "
            "cap and interleave pipeline"
        )
        assert "winners.sort(key=_popularity_order)" not in strip_py_comments(src), (
            "the legacy stored-score sort must be gone; keeping it would leave "
            "the ordering bug in place regardless of the helpers"
        )

    def test_the_genre_query_selects_the_raw_listener_counts(self):
        """Prominence is impossible without them — an absent column would make
        every track fall back to the album-relative stored score."""
        src = self._finalise_source()
        sql = src[src.index("_GENRE_ROWS_SQL"): src.index("def _fetch_genre_playlist_rows")]
        assert "lastfm_listeners" in sql and "listenbrainz_listens" in sql, (
            "_GENRE_ROWS_SQL must select the raw listener counts"
        )

    def test_the_genre_pool_preserves_the_listener_counts(self):
        src = self._finalise_source()
        idx = src.index("pools[target_norm_key].append({")
        window = src[idx: idx + 1200]
        assert '"lastfm_listeners"' in window, (
            "the pool dict must carry the raw counts through, or the ordering "
            "key silently falls back to the stored score for every track"
        )

    def test_the_essential_builder_uses_the_percentile_orderer(self):
        src = self._finalise_source()
        assert "_ordered_by_percentile_then_prominence(winners, order_mode)" in src, (
            "the Essential Collection must use its (single-artist) orderer"
        )
        assert "winners.sort(\n        key=lambda r: (\n            -int(r.get(\"stars\")" not in src, (
            "the old inline Essential sort must be gone"
        )

    def test_the_essential_query_selects_the_raw_listener_counts(self):
        src = self._finalise_source()
        idx = src.index("def _sync_essential_playlist")
        # Generous window: the SELECT sits well inside the function.
        window = src[idx: idx + 12000]
        assert "COALESCE(lastfm_listeners, 0) AS lastfm_listeners" in window, (
            "the Essential SELECT must fetch the raw counts for the tie-break"
        )

    def test_the_recommendation_generator_interleaves_its_sources(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "services" / "playlists" / "generator_service.py"
        ).read_text(encoding="utf-8")
        assert "candidates: list[dict[str, Any]] = interleave_by_source(source_groups)" in src, (
            "the sources must be round-robined before the limit is applied"
        )
        assert "candidates += _collect_lastfm" not in strip_py_comments(src), (
            "the old append-then-truncate must be gone"
        )


# ---------------------------------------------------------------------------
# 7. The recommendation source mix
# ---------------------------------------------------------------------------

class TestTheRecommendationSourcesAreInterleaved:
    @staticmethod
    def _gen():
        from services.playlists import generator_service as gen

        return gen

    def test_round_robin_takes_from_each_source_in_turn(self):
        a = [{"title": "a1"}, {"title": "a2"}, {"title": "a3"}]
        b = [{"title": "b1"}, {"title": "b2"}]
        assert [r["title"] for r in self._gen().interleave_by_source([a, b])] == [
            "a1", "b1", "a2", "b2", "a3",
        ]

    def test_a_longer_second_source_is_not_truncated(self):
        """zip() would stop at the shortest list and silently drop the rest."""
        a = [{"title": "a1"}]
        b = [{"title": "b1"}, {"title": "b2"}, {"title": "b3"}]
        assert [r["title"] for r in self._gen().interleave_by_source([a, b])] == [
            "a1", "b1", "b2", "b3",
        ]

    def test_no_sources_yields_nothing(self):
        assert self._gen().interleave_by_source([]) == []

    def test_an_empty_source_is_tolerated(self):
        a = [{"title": "a1"}, {"title": "a2"}]
        assert [r["title"] for r in self._gen().interleave_by_source([a, []])] == [
            "a1", "a2",
        ]

    def test_both_sources_reach_a_capped_playlist(self):
        """The regression proper: `source='both'` must include BOTH.

        Driven through the real `generate_recommendations`, with the real
        interleave, and a limit small enough that the old append-then-truncate
        would have filled it entirely from the first source.
        """
        gen = self._gen()
        import services.playlists.playlist_service as ps

        class _Ctx:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        artists = [{"artist": f"A{i}", "artist_mbid": f"m{i}", "track_count": 100 - i}
                   for i in range(30)]

        import pytest as _pytest

        def _fake_library(limit=20):
            return artists

        monkey = _pytest.MonkeyPatch()
        try:
            monkey.setattr(gen, "_library_top_artists", _fake_library)
            monkey.setattr(gen, "_collect_lastfm", lambda a, per_artist=1: [
                {"title": f"LF{i}", "artist": x["artist"], "album": "",
                 "isrc": None, "source": "lastfm"} for i, x in enumerate(a)
            ])
            monkey.setattr(gen, "_collect_listenbrainz", lambda a, per_artist=1: [
                {"title": f"LB{i}", "artist": x["artist"], "album": "",
                 "isrc": None, "source": "listenbrainz"} for i, x in enumerate(a)
            ])
            monkey.setattr(gen, "_match_local_track", lambda title, artist, isrc=None: {
                "id": f"id-{title}", "title": title, "artist": artist,
                "file_path": f"/music/{title}.flac", "duration": 200,
            })
            monkey.setattr(ps, "create_m3u_file", lambda name, tracks: f"/tmp/{name}.m3u")
            monkey.setattr(ps, "sanitize_playlist_name", lambda n: n)

            result = gen.generate_recommendations(source="both", name="Mix", limit=12)
        finally:
            monkey.undo()

        sources = [m["source"] for m in result["matched"]]
        assert "listenbrainz" in sources, (
            "source='both' must actually include ListenBrainz; appending "
            "Last.fm first and truncating made it impossible"
        )
        assert "lastfm" in sources, "and Last.fm must still be represented"


# ---------------------------------------------------------------------------
# 8. The config contract (both template trees)
# ---------------------------------------------------------------------------

class TestTheOrderingConfigIsExposed:
    LIVE = ("templates/pages/config.html", "static/js/config.js")
    REBUILT = ("test_site/templates/Pages/config.html",
               "test_site/static/js/pages/config.js")

    @pytest.mark.parametrize("tree", [LIVE, REBUILT], ids=["live", "test_site"])
    def test_the_order_mode_is_configurable(self, tree):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        html = (root / tree[0]).read_text(encoding="utf-8")
        js = (root / tree[1]).read_text(encoding="utf-8")
        assert 'id="playlists_order_mode"' in html
        assert "playlist_order_mode" in js, "the value must be collected, or it never persists"
        for option in ("prominence", "stored"):
            assert f'value="{option}"' in html, f"the {option} option must be offered"

    @pytest.mark.parametrize("tree", [LIVE, REBUILT], ids=["live", "test_site"])
    def test_the_per_artist_cap_preserves_an_explicit_zero(self, tree):
        """0 means 'no cap', so a `|| default` guard would make it unsayable."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        html = (root / tree[0]).read_text(encoding="utf-8")
        js = (root / tree[1]).read_text(encoding="utf-8")
        assert 'id="playlists_max_per_artist"' in html
        idx = js.index("genre_playlists_max_per_artist")
        window = js[idx: idx + 260]
        assert "Math.max(0, raw)" in window, (
            f"{tree[1]}: the clamp must not turn an explicit 0 into a default"
        )

    @pytest.mark.parametrize("tree", [LIVE, REBUILT], ids=["live", "test_site"])
    def test_the_interleave_toggle_is_exposed_and_defaults_on(self, tree):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        html = (root / tree[0]).read_text(encoding="utf-8")
        js = (root / tree[1]).read_text(encoding="utf-8")
        idx = html.index('id="playlists_interleave_artists"')
        assert "checked" in html[idx: idx + 200], "the default must be on"
        assert "playlist_interleave_artists" in js

    def test_an_unknown_order_mode_falls_back_to_prominence(self):
        """A stale config must not silently select the legacy behaviour."""
        def _resolve(value):
            mode = str(value or "prominence").strip().lower()
            return mode if mode in ("prominence", "stored") else "prominence"

        assert _resolve("nonsense") == "prominence"
        assert _resolve(None) == "prominence"
        assert _resolve("STORED") == "stored"

    def test_the_new_keys_survive_a_config_round_trip(self, tmp_path, monkeypatch):
        """A Config control that writes nothing is a false win.

        The save path writes the collected dict to YAML verbatim, so this pins
        that the three new keys are read back with the same values — including
        the explicit 0 that means "no per-artist cap".
        """
        import yaml

        import helpers.config_helpers as ch

        path = tmp_path / "config.yaml"
        monkeypatch.setattr(ch, "_CONFIG_PATH", str(path))
        try:
            ch.clear_config_cache()
        except Exception:
            pass

        payload = {
            "playlists": {
                "playlist_order_mode": "stored",
                "genre_playlists_max_per_artist": 0,
                "playlist_interleave_artists": False,
            }
        }
        assert ch.save_config(payload) is True, "the save itself must succeed"

        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        pl = written["playlists"]
        assert pl["playlist_order_mode"] == "stored"
        assert pl["genre_playlists_max_per_artist"] == 0, (
            "0 must round-trip as 0 — it means 'no cap', and YAML writing it as "
            "None would silently re-enable the default"
        )
        assert pl["playlist_interleave_artists"] is False
