"""Various Artists tracks get THEIR genres from MusicBrainz/Last.fm/…, not Navidrome.

Request
-------
> I want the tracks on Various Artists collections to get the genres from
> Musicbrainz, Last.fm, etc for the tracks rather than using the Navidrome
> genres.

What was actually happening, for a VA album:

* the Navidrome import writes ``genres`` from the FILE's own tag
  (``payload_builder.py``: ``genres = navidrome_genres``);
* the album-level overwrite in ``sync_album_file_tags`` is deliberately
  **skipped** for VA albums — correct, because it writes ONE blended value to
  every track of the album and destroys each performer's genre;
* and no per-track writer existed to replace it. Every
  ``UPDATE tracks SET genres`` in the repo is either the album-wide one, a
  manual artist-page action, the album-save path, or one of two functions
  (``sync_confident_genres`` / ``enrich_genres_aggressively``) with **zero
  callers**.

So a VA track's ``genres`` was frozen at whatever Navidrome had, while its
MusicBrainz / Last.fm / Discogs / ListenBrainz columns sat right next to it,
populated by the scan.

These tests pin the fix: **per track** (never album-wide — that is the whole
point of a compilation), from the track's own source columns, with Navidrome
excluded from both the votes and the tie-break it normally provides.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from services.enrichment import genre_aggregation_service as gas

REPO_ROOT = Path(__file__).resolve().parents[1]
TAG_SYNC = REPO_ROOT / "services" / "metadata" / "album_tag_sync_service.py"

# Every source the aggregation service weighs, except the one we are removing.
ONLINE_SOURCES = {
    "musicbrainz_genres": "musicbrainz",
    "discogs_genres": "discogs",
    "audiodb_genres": "audiodb",
    "essentia_genres": "essentia",
    "listenbrainz_genres": "listenbrainz",
    "lastfm_genres": "lastfm",
    "spotify_genres": "spotify",
    "wikidata_genres": "wikidata",
    "manual_genres": "manual",
}


def _track(**overrides):
    row = {
        "id": "t1",
        "title": "Beds Are Burning",
        "genres": "rock",
        "navidrome_genres": "dance, pop",
    }
    row.update(overrides)
    return row


# ===========================================================================
# 1. What goes into the vote
# ===========================================================================
class TestTheSourceMap:
    def test_navidrome_is_excluded(self):
        """The whole point: Navidrome must not contribute."""
        source_map = gas.va_track_source_map(
            _track(navidrome_genres="dance, pop", lastfm_genres="rock")
        )
        assert "navidrome" not in source_map, (
            "the request is explicitly for MusicBrainz/Last.fm 'rather than "
            "the Navidrome genres'"
        )

    @pytest.mark.parametrize("column,source", sorted(ONLINE_SOURCES.items()))
    def test_each_online_source_is_carried(self, column, source):
        source_map = gas.va_track_source_map(_track(**{column: "rock"}))
        assert source in source_map, f"{column} was dropped from the vote"

    def test_blank_columns_contribute_nothing(self):
        source_map = gas.va_track_source_map(
            _track(lastfm_genres="", musicbrainz_genres=None, discogs_genres=[])
        )
        assert source_map == {}

    def test_a_track_with_only_navidrome_has_no_vote_at_all(self):
        """It must fall through to 'leave the row alone', not 'write nothing'."""
        assert gas.va_track_source_map(_track()) == {}

    def test_values_are_parsed_not_passed_raw(self):
        """JSONB columns arrive as strings and lists; both must parse."""
        source_map = gas.va_track_source_map(
            _track(musicbrainz_genres='["Rock", "Metal"]', lastfm_genres="rock, metal")
        )
        assert source_map["musicbrainz"] == ["Rock", "Metal"]
        assert source_map["lastfm"] == ["rock", "metal"]


# ===========================================================================
# 2. What gets written
# ===========================================================================
class TestWhatIsWritten:
    def test_the_value_comes_from_the_aggregator_not_navidrome(self, monkeypatch):
        """Capture what the aggregator was asked for."""
        seen = {}

        def _fake_aggregate(source_map, **kwargs):
            seen["source_map"] = source_map
            seen["kwargs"] = kwargs
            return ["Rock", "Metal"]

        _patch_writer(monkeypatch, aggregate=_fake_aggregate)
        tracks = [_track(id="t1", lastfm_genres="rock, metal")]

        gas.sync_various_artists_track_genres(tracks, album="The Power and the Passion")

        assert "navidrome" not in seen["source_map"]
        # Navidrome normally acts as the tie-breaker; it must not here either.
        assert seen["kwargs"].get("nav_genres") is None, (
            "passing nav_genres would let Navidrome decide a tie on an album "
            "the request asked to exclude it from"
        )
        assert seen["kwargs"].get("context_title") == "Beds Are Burning"

    def test_the_write_is_per_track_not_album_wide(self, monkeypatch):
        writes = _patch_writer(monkeypatch, aggregate=lambda *a, **k: ["Rock"])
        tracks = [
            # genres differs from what the aggregator returns, so it must write
            # (a matching value is skipped — see the test below).
            _track(id="t1", lastfm_genres="rock", genres="pop"),
            _track(id="t2", lastfm_genres="rock", genres="pop"),
        ]

        gas.sync_various_artists_track_genres(tracks, album="X")

        assert writes == [
            {"genres": "Rock", "track_id": "t1"},
            {"genres": "Rock", "track_id": "t2"},
        ], (
            "each track must be written by its OWN id — the album-wide UPDATE "
            "is exactly what a compilation must not do"
        )
        sql = _SQL[0]
        assert "WHERE id = :track_id" in sql
        assert "album = :alb" not in sql

    def test_a_track_with_no_online_sources_is_left_alone(self, monkeypatch):
        """Never wipe a track down to nothing."""
        writes = _patch_writer(monkeypatch, aggregate=lambda *a, **k: ["Rock"])
        gas.sync_various_artists_track_genres([_track()], album="X")
        assert writes == [], "Navidrome-only tracks must keep their genres"

    def test_a_track_that_already_matches_is_not_rewritten(self, monkeypatch):
        writes = _patch_writer(monkeypatch, aggregate=lambda *a, **k: ["Rock"])
        gas.sync_various_artists_track_genres(
            [_track(genres="Rock", lastfm_genres="rock")], album="X"
        )
        assert writes == [], "an unchanged row must not be written (file churn)"

    def test_a_spelling_variant_of_the_same_genre_is_not_rewritten(self, monkeypatch):
        """MusicBrainz's ``hip-hop`` against a stored ``Hip Hop`` is not a change.

        ``_genre_sets_equal`` normalises case and whitespace but not
        punctuation, so on its own this rewrote the row — and, because a track
        write fans out to the file tags, the physical file too — for a
        difference nobody can see.
        """
        writes = _patch_writer(monkeypatch, aggregate=lambda *a, **k: ["hip-hop"])
        gas.sync_various_artists_track_genres(
            [_track(genres="Hip Hop", lastfm_genres="hip-hop")], album="X"
        )
        assert writes == [], "a spelling variant of the same genre must not write"

    def test_a_genuinely_different_genre_still_writes(self, monkeypatch):
        """CONTROL — the spelling tolerance must not hide a real change."""
        writes = _patch_writer(monkeypatch, aggregate=lambda *a, **k: ["Jazz"])
        gas.sync_various_artists_track_genres(
            [_track(genres="Hip Hop", lastfm_genres="hip-hop")], album="X"
        )
        assert writes == [{"genres": "Jazz", "track_id": "t1"}], (
            "tolerating 'hip hop'/'hip-hop' must not make the sync refuse to "
            "replace a track's genre with a different one"
        )

    def test_the_aggregator_may_return_nothing_and_nothing_is_written(self, monkeypatch):
        writes = _patch_writer(monkeypatch, aggregate=lambda *a, **k: [])
        gas.sync_various_artists_track_genres(
            [_track(lastfm_genres="rock")], album="X"
        )
        assert writes == []

    def test_a_single_weak_source_cannot_define_a_track_by_itself(self, monkeypatch):
        """Documented limit of the LEGACY engine, not an accident.

        ``aggregate_genres`` used to reject anything below ``genres.min_weight``
        (0.25), and Last.fm weighed 0.10 / ListenBrainz 0.15 / Spotify 0.05 — so
        a track whose ONLY online source was one of those kept its existing
        genres rather than being rewritten from a single weak signal.
        MusicBrainz (0.40), Discogs (0.25) and Manual (0.30) each cleared it
        alone, which is the common case: the scan writes ``musicbrainz_genres``
        for every recording.

        ⚠️ The threshold is a LEGACY mechanism, so the legacy engine is forced
        here. The canonical engine replaces it with per-position confidence
        floors AND re-weights Last.fm to 0.70 (the recommended provider trust for
        broad coverage), so a track's own Last.fm tag now IS accepted — see
        ``test_a_lone_track_tag_is_accepted_by_the_canonical_engine``.
        """
        from services.enrichment import genre_aggregation_service as gas_mod

        monkeypatch.setattr(gas_mod, "_selection_engine_v2_enabled", lambda: False)
        assert gas.aggregate_genres(
            {"lastfm": ["rock"]}, max_genres=2, nav_genres=None
        ) == []
        assert gas.aggregate_genres(
            {"musicbrainz": ["rock"]}, max_genres=2, nav_genres=None
        ) == ["Rock"]

    def test_a_lone_tag_survives_only_when_the_only_voice(self):
        """⚠️ CHANGED 2026-11-20 — the corroboration rule, stated precisely.

        Corroboration needs a SECOND VOICE to exist:

            ONE provider speaking             -> the tag stands
                (nothing to corroborate against; gating it would blank the
                 genre field of every track whose providers only partly cover it)
            TWO providers, one naming it      -> the tag is rejected
                (a genuine disagreement)

        The ARTIST-level protection is unchanged and still asserted last.
        """
        # One voice: stands.
        assert gas.aggregate_genres(
            {"lastfm": ["rock"]}, max_genres=2, nav_genres=None
        ) == ["Rock"]
        # Two voices, one naming it: rejected.
        assert gas.aggregate_genres(
            {"lastfm": ["rock"], "musicbrainz": ["pop"]}, max_genres=2,
            nav_genres=None,
        ) == ["Pop"]
        # Two voices, both agreeing: accepted.
        assert gas.aggregate_genres(
            {"lastfm": ["rock"], "musicbrainz": ["rock"]},
            max_genres=2, nav_genres=None,
        ) == ["Rock"]
        # Artist-level evidence still cannot define a track on its own.
        assert gas.aggregate_genres(
            {"lastfm@artist": ["rock"]}, max_genres=2, nav_genres=None
        ) == []


# Every statement the writer executed, so the per-track assertion can read the
# actual SQL without threading it back through each test.
_SQL: list[str] = []


def _patch_writer(monkeypatch, *, aggregate):
    """Patch the aggregator and the DB write; return the recorded writes."""
    monkeypatch.setattr(gas, "aggregate_genres", aggregate)

    recorded: list[dict] = []
    _SQL.clear()

    class _Result:
        rowcount = 1

    class _Session:
        def execute(self, statement, params=None):
            # The writer batches the album as executemany (a LIST of param
            # dicts), so record each row rather than treating the list as one
            # mapping — ``dict([{...}])`` reads its keys and produced a bogus
            # ``{"genres": "track_id"}`` row.
            if isinstance(params, list):
                for one in params:
                    recorded.append(dict(one))
            else:
                recorded.append(dict(params or {}))
            _SQL.append(str(statement))
            return _Result()

        def commit(self):
            pass

        def rollback(self):
            pass

    @contextmanager
    def _fake_db_session():
        yield _Session()

    monkeypatch.setattr(gas, "db_session", _fake_db_session)
    return recorded


# ===========================================================================
# 3. It is actually wired into the scan
# ===========================================================================
class TestTheScanCallsIt:
    def test_the_various_artists_branch_uses_it(self):
        source = TAG_SYNC.read_text(encoding="utf-8")
        # Just the VA branch — everything up to the `else:` that handles a
        # normal, single-artist album.
        branch = source.split("if is_various_artists_album(", 1)[1].split("else:", 1)[0]
        assert "sync_various_artists_track_genres" in branch, (
            "the VA branch still only logs 'Skipped', so VA genres stay frozen "
            "at whatever Navidrome imported"
        )

    def test_the_non_va_branch_prefers_PER_TRACK_genres(self):
        """⚠️ CHANGED 2026-11-20 — this used to be a CONTROL pinning the exact
        opposite ("a single-artist album still shares one genre list").

        Sharing one list is precisely the reported bug: "genres are being
        assigned per album, so every track on an album gets that album's tags".
        One odd tag on one release became the genre of the whole album —
        Green Day's Demolicious reading "Rock; Jazz; Trance", Lacuna Coil's
        In a Reverie "Gothic Metal; Blues", and Evanescence's piano ballad
        "My Immortal" inheriting Nu Metal.

        The album blend is now a FALLBACK. A track whose own provider columns
        resolve keeps that answer; only a track that resolves NOTHING inherits
        the album list.
        """
        source = TAG_SYNC.read_text(encoding="utf-8")
        assert "get_track_recommendations" in source, (
            "the album blend is still needed as the FALLBACK"
        )
        assert "_select_track_own_genres" in source, (
            "each track must be asked for its OWN genres before the album blend"
        )
        # The album list is still written, but no longer unconditionally to
        # every id: it is one of several per-value UPDATE groups.
        assert "UPDATE tracks SET genres = :g" in source
        assert "_final_genres_for" in source, (
            "the stored value must be computed per track (own evidence, else "
            "the album blend), not once for the album"
        )
