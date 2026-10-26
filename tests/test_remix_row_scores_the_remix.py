"""A remix album's rows are scored as the REMIX, not the studio original.

Reported: the same album's listen counts were "completely different" between
the first pass and a later one, and the rows looked like they matched "the
track release rather than the remix release on this album".

The scan log for Limp Bizkit's *New Old Songs* shows both halves of it:

    Album-tracklist LB match for 'My Way' …: 10758 listens (release=10758, kept=release)
    [TRACK] "My Way" … | Score: 71.0 (LF: 1212.0k, LB: 10.8k)

ListenBrainz is **release-scoped** and reports the remix (~10k). Last.fm was
**title-matched** and reported the studio hit (~1.2M). Same row, two different
recordings, a ~100x spread — because every provider match is a TITLE match, and
the title handed to Last.fm was the retitled FILE name, not the release's.

Two further facts made it flip between scans:

* the "is this an alternate rendition" flag was read from
  ``effective_track["musicbrainz_title"]`` — a DB column written by the
  PREVIOUS scan, so it was empty on a first pass;
* the fallback that runs when the aggregate comes back empty re-queried the
  LOCAL title, putting the studio recording straight back.

The album's own release already names the rendition each row is — the same
identity ``_build_album_recording_batch`` applies to the MusicBrainz metadata.
``resolve_performance_title`` reuses it for the popularity lookups.
"""

from __future__ import annotations

import pytest

from services.popularity.popularity_sources import (
    get_aggregated_lastfm_popularity,
    resolve_performance_title,
)

ALBUM = "New Old Songs"
ARTIST = "Limp Bizkit"

#: The reported album shape: local files lost the remix marker, the release
#: kept it. Straight from the log's tracklist.
REPORTED_ROWS = [
    ("Nookie (For the Nookie)", "Nookie (For the Nookie)"),
    ("My Way", "My Way (DJ Premier Way remix)"),
    ("My Way (William Orbit’s mix)", "My Way (William Orbit’s mix)"),
    ("Break Stuff", "Break Stuff"),
    ("Take a Look Around", "Take a Look Around"),
    ("Rollin’ (DJ Monk‐vs‐The Track Mack remix)", "Rollin’ (DJ Monk‐vs‐The Track Mack remix)"),
]


class FakeLastFm:
    """Stub mirroring Last.fm: the studio hit and the remix are SEPARATE
    tracks with wildly different listener counts.

    Rows are served through ``search_track`` because that is the path whose
    results carry the RAW provider title — the artist-catalogue path keys on a
    normalised name and cannot prove a version match.
    """

    def __init__(self, rows: dict[str, int]):
        self.rows = rows
        self.queried: list[str] = []

    def get_artist_top_tracks(self, artist, limit=200):
        return []

    def search_track(self, artist, title, limit=20):
        self.queried.append(title)
        return [
            {"name": name, "artist": "Limp Bizkit", "listeners": n}
            for name, n in self.rows.items()
        ]

    def get_track_info(self, artist, title, track_mbid=None):
        self.queried.append(title)
        return {"listeners": 0, "track_play": 0}


@pytest.fixture(autouse=True)
def _clear_catalogue_cache():
    """``_artist_catalogue`` caches per process — reset it per test."""
    from services.popularity import popularity_sources as ps

    ps._lastfm_artist_catalog_cache.clear()
    yield
    ps._lastfm_artist_catalog_cache.clear()


def _reported_client() -> FakeLastFm:
    """The reported album: a hugely popular studio hit beside its remixes."""
    return FakeLastFm(
        {
            "My Way": 1_212_000,
            "Break Stuff": 1_988_500,
            "My Way (DJ Premier Way remix)": 6_800,
            "My Way (William Orbit’s mix)": 5_000,
        }
    )


# ---------------------------------------------------------------------------
# 1. The resolver
# ---------------------------------------------------------------------------


class TestResolvePerformanceTitle:
    def test_the_retitled_row_takes_the_release_name(self) -> None:
        """⭐ THE REPORTED CASE. The file says "My Way"; the release says which
        remix it is."""
        assert (
            resolve_performance_title("My Way", "My Way (DJ Premier Way remix)")
            == "My Way (DJ Premier Way remix)"
        )

    def test_a_plain_row_is_untouched(self) -> None:
        """CONTROL: an ordinary album must not have its titles rewritten."""
        assert resolve_performance_title("Break Stuff", "Break Stuff") == "Break Stuff"
        assert (
            resolve_performance_title("Take a Look Around", "Take a Look Around")
            == "Take a Look Around"
        )

    def test_a_row_that_already_carries_the_marker_keeps_its_own(self) -> None:
        """No gratuitous churn when the local title is already specific."""
        title = "Rollin’ (DJ Monk‐vs‐The Track Mack remix)"
        assert resolve_performance_title(title, title) == title

    def test_the_release_must_corroborate_the_local_title(self) -> None:
        """A release title for a DIFFERENT song is not this row's identity."""
        assert resolve_performance_title("My Way", "Something Else Entirely") == "My Way"

    def test_a_missing_release_title_falls_back_to_the_local_one(self) -> None:
        assert resolve_performance_title("My Way", None) == "My Way"
        assert resolve_performance_title("My Way", "  ") == "My Way"

    @pytest.mark.parametrize("local,release", REPORTED_ROWS)
    def test_never_invents_a_rendition(self, local: str, release: str) -> None:
        """Every reported row resolves to one of the two inputs, never a mix."""
        assert resolve_performance_title(local, release) in {local, release}

    def test_a_live_marker_is_honoured_too(self) -> None:
        """The rule is about PERFORMANCE identity, not just remixes."""
        assert (
            resolve_performance_title("Halt mich", "Halt mich (Unplugged Version)")
            == "Halt mich (Unplugged Version)"
        )


# ---------------------------------------------------------------------------
# 2. Why it matters — which recording Last.fm reports
# ---------------------------------------------------------------------------


class TestLastfmScoresTheRemixNotTheStudioHit:
    def test_the_local_title_reproduces_the_reported_contamination(self) -> None:
        """⭐ BASELINE, and the defect in one number.

        Queried with the retitled file name, the aggregate returns the studio
        hit's 1.2M listeners — the exact figure the log printed next to
        ListenBrainz's 10.8k for the remix.
        """
        agg = get_aggregated_lastfm_popularity(
            ARTIST, "My Way", lastfm_client=_reported_client(),
        )
        assert agg["listeners"] == 1_212_000

    def test_the_release_title_scores_the_remix(self) -> None:
        """⭐⭐ THE FIX: same row, the release's name, the remix's own count."""
        agg = get_aggregated_lastfm_popularity(
            ARTIST, "My Way (DJ Premier Way remix)", lastfm_client=_reported_client(),
        )
        assert agg["listeners"] == 6_800

    def test_the_studio_count_is_not_reachable_through_the_release_title(self) -> None:
        """The studio row must not survive the version gate on any path."""
        client = _reported_client()
        agg = get_aggregated_lastfm_popularity(
            ARTIST, "My Way (DJ Premier Way remix)", lastfm_client=client,
        )
        matched = [str(t.get("name") or "") for t in agg.get("matched_tracks") or []]
        assert "My Way" not in matched
        assert all("remix" in name.casefold() for name in matched if name)

    def test_a_plain_row_still_scores_its_plain_recording(self) -> None:
        """CONTROL: the fix must not suppress an ordinary album row."""
        agg = get_aggregated_lastfm_popularity(
            ARTIST, "Break Stuff", lastfm_client=_reported_client(),
        )
        assert agg["listeners"] == 1_988_500

    def test_the_remix_count_survives_the_alternate_rendition_flag(self) -> None:
        """The flag suppresses the studio hit; it must not suppress the remix."""
        agg = get_aggregated_lastfm_popularity(
            ARTIST, "My Way (DJ Premier Way remix)", lastfm_client=_reported_client(),
            target_is_alt_rendition=True,
        )
        assert agg["listeners"] == 6_800


# ---------------------------------------------------------------------------
# 3. The wiring — the identity is taken before any provider is asked
# ---------------------------------------------------------------------------


class TestTheTrackStageResolvesTheIdentityFirst:
    """``track_stage`` is one 1500-line function; these pin the ORDER and the
    call sites, which is what the ordering bug was about."""

    @pytest.fixture(scope="class")
    def source(self) -> str:
        import inspect

        from services.popularity.stages import track_stage

        return inspect.getsource(track_stage)

    def test_the_performance_title_is_resolved_from_the_release(self, source: str) -> None:
        assert "resolve_performance_title(" in source
        assert '(_prefetch_entry or {}).get("release_track_title")' in source

    def test_it_is_resolved_before_the_forced_entry_is_dropped(self, source: str) -> None:
        """⚠️ ``_force`` drops a stale cached COUNT. Dropping the IDENTITY with
        it left a forced scan with neither, which is the first-vs-later flip."""
        resolve_at = source.index("resolve_performance_title(")
        drop_at = source.index("_prefetch_entry = None", source.index("_perf_title = _local_perf_title"))
        assert resolve_at < drop_at

    def test_every_lastfm_call_is_given_the_resolved_title(self, source: str) -> None:
        """All three Last.fm entry points — cohort, search and the per-track
        fallback — must agree on which recording this row is."""
        assert "raw_title or title,\n                                    lastfm_client=lf" not in source
        assert source.count("_perf_title") >= 5
        # The aggregate and the search aggregation both take it.
        agg_call = source[source.index("get_aggregated_lastfm_popularity("):]
        assert "_perf_title" in agg_call[:400]
        search_call = source[source.index("get_search_aggregated_lastfm_popularity("):]
        assert "_perf_title" in search_call[:200]

    def test_the_empty_aggregate_fallback_does_not_requery_the_local_title(self, source: str) -> None:
        """The fallback ran precisely when the aggregate found nothing, and it
        re-queried the retitled name — restoring the studio count."""
        assert "lf.get_track_info(artist, title)" not in source
        assert "lf.get_track_info(artist, _perf_title)" in source


# ---------------------------------------------------------------------------
# 4. The flag no longer depends on a previous scan
# ---------------------------------------------------------------------------


class TestTheAltRenditionFlagIsStableAcrossScans:
    def test_the_release_title_drives_the_flag(self) -> None:
        """``musicbrainz_title`` is only populated by the PREVIOUS scan, so a
        first pass had no marker at all. The release title is available
        in-memory on every pass, so both passes now agree."""
        import inspect

        from services.popularity.stages import track_stage

        source = inspect.getsource(track_stage)
        # The flag is raised from the RESOLVED title, not only from a DB read.
        assert "_perf_title != _local_perf_title" in source
        assert "_alt_rendition = True" in source

    def test_a_first_pass_and_a_later_pass_resolve_the_same_title(self) -> None:
        """The resolver is pure: no DB state, so run order cannot change it."""
        first = resolve_performance_title("My Way", "My Way (DJ Premier Way remix)")
        later = resolve_performance_title("My Way", "My Way (DJ Premier Way remix)")
        assert first == later
