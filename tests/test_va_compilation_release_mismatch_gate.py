"""A VA compilation with a mismatched release must NOT get positional
identities from a different edition of the album.

REPORTED
--------
An 80-track "Various Artists - Alternative 90s" folder carried a stored
MusicBrainz release MBID for the 46-track rendering of the album. The
album stage rejected the tracklist match (``match_score=-1.2``), but the
position pass still aliased local tracks to the wrong release positions:

* local#8  "Song 2"           (Blur)      -> release "What You Do to Me" (Teenage Fanclub)
* local#10 "Where Is My Mind?" (Pixies)   -> release "Happy"             (Ned's Atomic Dustbin)
* local#18 "Buddy Holly"      (Weezer)    -> release "She Know's She's Wrong" (Jellyfish)

The wrong recording MBID then pulled ListenBrainz counts / artists for a
DIFFERENT song into the scan ("Song 2" scored Teenage Fanclub's 82,928
listens; the artist was overwritten to 'Teenage Fanclub').

The position alias is now gated on the release title at that position
CORROBORATING the local title (feat. credits and version/cover markers
ignored — the dArtagnan unplugged case the alias exists for still passes).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _reset_shared_mb_client(monkeypatch):
    """Tests that fall back to MusicBrainz for the release tracklist fake
    ``MusicBrainzHttpClient`` via monkeypatch, but ``get_shared_mb_client()``
    caches the constructed client in a process-wide singleton — without a
    reset, an EARLIER test file that primed a real client makes the MB
    fallback hit the network (and fail) instead of the fixture."""
    import services.enrichment.musicbrainz_service as _mbs

    monkeypatch.setattr(_mbs, "_SHARED_MB_CLIENT", None)
    monkeypatch.setattr(_mbs, "_shared_mb_service", None)
    monkeypatch.setattr(_mbs, "_service", None)
    yield

# The mismatched "Alternative 90s" release (46-track edition, position titles
# differ from the local folder) — the fixture keeps the COUNT mismatch that
# matters: 8 release tracks vs 5 local rows, exactly like 46 vs 80 in the
# report.
RELEASE_TRACKS = [
    (1, 1, "Live Forever", "ooo-0001-0000-0000-000000000001", 200000),
    (1, 2, "Filler One", "fil-0002-0000-0000-000000000006", 180000),
    (1, 3, "Filler Two", "fil-0003-0000-0000-000000000007", 190000),
    (1, 4, "Filler Three", "fil-0004-0000-0000-000000000008", 210000),
    (1, 8, "What You Do to Me", "tfc-0008-0000-0000-000000000002", 190000),
    (1, 10, "Happy", "ned-0010-0000-0000-000000000003", 235000),
    (1, 18, "She Know's She's Wrong", "jel-0018-0000-0000-000000000004", 240000),
    (1, 22, "Birdhouse in Your Soul", "tmb-0022-0000-0000-000000000005", 210000),
]

# The local folder: same songs, DIFFERENT position numbering.
LOCAL_TRACKS = [
    {"title": "Live Forever", "artist": "Oasis", "disc_number": 1, "track_number": 1, "duration": 235},
    {"title": "Song 2", "artist": "Blur", "disc_number": 1, "track_number": 8, "duration": 121},
    {"title": "Where Is My Mind?", "artist": "Pixies", "disc_number": 1, "track_number": 10, "duration": 235},
    {"title": "Buddy Holly", "artist": "Weezer", "disc_number": 1, "track_number": 18, "duration": 159},
    {"title": "Birdhouse in Your Soul", "artist": "They Might Be Giants", "disc_number": 1, "track_number": 22, "duration": 200},
]


def _media_payload() -> list[dict]:
    return [{
        "position": 1,
        "tracks": [
            {
                "position": pos,
                "number": str(pos),
                "title": title,
                "length": length,
                "recording": {"id": mbid},
            }
            for _d, pos, title, mbid, length in RELEASE_TRACKS
        ],
    }]


@pytest.fixture()
def ps(monkeypatch):
    from services.popularity import popularity_sources as _ps

    monkeypatch.setattr(_ps, "_resolve_release_mbid", lambda artist, album, tracks: "rel-alt90")
    monkeypatch.setattr(
        _ps,
        "lb_get_release_metadata_batch",
        lambda mbids: {"rel-alt90": {"media": _media_payload()}},
    )
    monkeypatch.setattr(
        _ps,
        "lb_get_recording_popularity_batch",
        lambda mbids: {m: {"total_listen_count": 500, "total_user_count": 50} for m in mbids},
    )
    return _ps


def _norm_key(value: str) -> str:
    from services.popularity.popularity_sources import normalize_for_aggregation
    return normalize_for_aggregation(value)


def _identity_key(title: str, disc=None, track_number=None) -> str:
    from services.popularity.popularity_sources import track_identity_key
    return track_identity_key(_norm_key(title), disc, track_number)


class TestMismatchedReleaseGetsNoPositionAlias:

    def test_the_rogue_tracks_get_no_identity_at_all(self, ps):
        out, release_mbid = ps.get_listenbrainz_album_tracklist_with_release(
            "Various Artists", "Alternative 90s", LOCAL_TRACKS
        )
        assert release_mbid == "rel-alt90"

        # "Song 2" is NOT on the release — its title-keyed key must not exist,
        # and its position (8) must not alias to "What You Do to Me".
        assert _norm_key("Song 2") not in out
        assert _identity_key("Song 2", 1, 8) not in out

        assert _norm_key("Where Is My Mind?") not in out
        assert _identity_key("Where Is My Mind?", 1, 10) not in out

        assert _norm_key("Buddy Holly") not in out
        assert _identity_key("Buddy Holly", 1, 18) not in out

    def test_a_title_on_the_release_still_matches(self, ps):
        out, _release = ps.get_listenbrainz_album_tracklist_with_release(
            "Various Artists", "Alternative 90s", LOCAL_TRACKS
        )
        entry = out.get(_norm_key("Live Forever")) or {}
        assert entry.get("recording_mbid") == "ooo-0001-0000-0000-000000000001"

    def test_no_position_alias_points_at_the_wrong_recording(self, ps):
        out, _release = ps.get_listenbrainz_album_tracklist_with_release(
            "Various Artists", "Alternative 90s", LOCAL_TRACKS
        )
        wrong_mbids = {
            "tfc-0008-0000-0000-000000000002",   # What You Do to Me
            "ned-0010-0000-0000-000000000003",   # Happy
            "jel-0018-0000-0000-000000000004",   # She Know's She's Wrong
        }
        # Only keys the LOCAL album reads are authoritative: ``out`` also holds
        # the release's OWN catalogue (its titles keyed verbatim), which the
        # scan never looks up by local title.
        local_keys = {_norm_key(t["title"]) for t in LOCAL_TRACKS}
        for key in local_keys:
            entry = out.get(key) or {}
            assert entry.get("recording_mbid") not in wrong_mbids, (
                f"{key} was aliased to a recording from a DIFFERENT song"
            )


# The version-marker case the alias exists for must still corroborate
# (the local title kept its marker/feat, the release title dropped it).
# The release holds a FILLER track and the local folder has 2 rows — the
# counts DIFFER, so ONLY title corroboration can admit these aliases.
VERSION_MARKER_RELEASE = [
    (1, 2, "Wake Me Up", "s260-1-0000000000000000000001", 199747),
    (1, 16, "Farewell [Unplugged Version]", "s261-1-0000000000000000000002", 180640),
    (1, 3, "Some Filler Song", "s262-1-0000000000000000000003", 200000),
]

VERSION_MARKER_LOCAL = [
    {"title": "Wake Me Up (feat. Piper.Ally)", "artist": "DArtagnan feat. Piper.Ally",
     "disc_number": 1, "track_number": 2, "duration": 200},
    {"title": "Farewell (feat. Patty Gurdy)", "artist": "DArtagnan feat. Patty Gurdy",
     "disc_number": 1, "track_number": 16, "duration": 181},
]


@pytest.fixture()
def ps_marker(monkeypatch):
    from services.popularity import popularity_sources as _ps

    media = [{
        "position": 1,
        "tracks": [
            {"position": pos, "number": str(pos), "title": title, "length": length,
             "recording": {"id": mbid}}
            for _d, pos, title, mbid, length in VERSION_MARKER_RELEASE
        ],
    }]
    monkeypatch.setattr(_ps, "_resolve_release_mbid", lambda artist, album, tracks: "rel-dartagnan")
    monkeypatch.setattr(
        _ps,
        "lb_get_release_metadata_batch",
        lambda mbids: {"rel-dartagnan": {"media": media}},
    )
    monkeypatch.setattr(_ps, "lb_get_recording_popularity_batch", lambda mbids: {})
    return _ps


class TestVersionMarkerAliasStillCorroborates:

    def test_a_feat_local_title_matches_an_unplugged_release_title(self, ps_marker):
        out, _release = ps_marker.get_listenbrainz_album_tracklist_with_release(
            "DArtagnan", "Helden X Hymnen", VERSION_MARKER_LOCAL
        )
        # "Wake Me Up (feat. Piper.Ally)" collapses by NORMALISED title to the
        # release's own "Wake Me Up" key, so it is resolved by the title pass.
        wake = out.get(_norm_key("Wake Me Up (feat. Piper.Ally)")) or {}
        assert wake.get("recording_mbid") == "s260-1-0000000000000000000001"
        # "Farewell (feat. Patty Gurdy)" is NOT on the release under its normalised
        # title — it needs the corroborated position alias.
        farewell = out.get(_identity_key("Farewell (feat. Patty Gurdy)", 1, 16)) or {}
        assert farewell.get("recording_mbid") == "s261-1-0000000000000000000002"
        assert farewell.get("release_track_title") == "Farewell [Unplugged Version]"


class TestPositionTitleCorroborates:

    def _corroborates(self, local, release):
        from services.popularity.popularity_sources import _position_title_corroborates
        return _position_title_corroborates(local, release)

    def test_different_songs_do_not_corroborate(self):
        assert self._corroborates("Where Is My Mind?", "Happy") is False
        assert self._corroborates("Song 2", "What You Do to Me") is False
        assert self._corroborates("Buddy Holly", "She Know's She's Wrong") is False
        assert self._corroborates("The Power of Love", "Love Shack") is False

    def test_marker_and_feat_variants_corroborate(self):
        assert self._corroborates("Farewell (feat. Patty Gurdy)", "Farewell [Unplugged Version]") is True
        assert self._corroborates("Für immer Dein", "Für immer Dein (Unplugged Version)") is True
        assert self._corroborates("Wake Me Up (feat. Piper.Ally)", "Wake Me Up") is True
        assert self._corroborates("Helden X Hymnen", "Helden X Hymnen") is True

    def test_cover_markers_never_corroborate(self):
        # A cover is a DIFFERENT recording — the alias must not point at the
        # original's recording.
        assert self._corroborates("Song (Someone Cover)", "Song") is False
        assert self._corroborates("Song", "Song (Someone Cover)") is False

    def test_empty_sides_never_corroborate(self):
        assert self._corroborates("", "Song") is False
        assert self._corroborates("Song", None) is False


class TestPositionAliasAllowed:
    """Two paths admit the alias: title corroboration, or an EXACT same-album
    track count (the translated-title case)."""

    def _allowed(self, local_title, release_title, local_count, release_count):
        from services.popularity.popularity_sources import _position_alias_allowed
        return _position_alias_allowed(
            local_title, release_title,
            local_track_count=local_count, release_track_count=release_count,
        )

    def test_translated_title_is_allowed_when_the_counts_match(self):
        # Stray Kids: release lists "삐처리", the library stores "BLEEP".
        assert self._allowed("BLEEP", "삐처리", 2, 2) is True

    def test_translated_title_is_rejected_when_the_counts_differ(self):
        # A count-mismatched release is a DIFFERENT edition — its positions
        # are not the local positions.
        assert self._allowed("Where Is My Mind?", "Happy", 80, 46) is False
        assert self._allowed("BLEEP", "삐처리", 80, 46) is False

    def test_corroborating_title_is_allowed_regardless_of_counts(self):
        assert self._allowed("Farewell (feat. Patty Gurdy)", "Farewell [Unplugged Version]", 80, 46) is True

    # ⚠️ Tradeoff, deliberately NOT softened: the count path cannot tell a
    # translated title (Stray Kids "삐처리" ↔ "BLEEP" — the pre-existing design
    # that requires the position fallback) from a coincidentally-numbered
    # DIFFERENT song on a same-count release. Title corroboration is the
    # strong path; the exact-count path is the fallback that keeps
    # translated titles working, and a same-count release with a genuinely
    # unrelated title is the residual hazard it accepts.


TRANSLATED_RELEASE = [
    (1, 1, "CEREMONY", "rec-cer-00000000000000000000001", 180000),
    (1, 2, "삐처리", "rec-bleep-kr-0000000000000000000001", 200000),
]

TRANSLATED_LOCAL = [
    {"title": "CEREMONY", "track_number": "1", "disc_number": "1", "duration": 180.0},
    {"title": "BLEEP", "track_number": "2", "disc_number": "1", "duration": 200.0},
]


class TestTranslatedTitleStillPositionMatches:
    """The designed translated-title path must survive the gate: on a
    SAME-COUNT release, an incomparable title is still matched by
    disc+position+length."""

    def test_a_translated_title_adopts_the_position_recording(self, monkeypatch):
        from services.popularity import popularity_sources as _ps

        media = [{
            "position": 1,
            "tracks": [
                {"position": pos, "number": str(pos), "title": title, "length": length,
                 "recording": {"id": mbid}}
                for _d, pos, title, mbid, length in TRANSLATED_RELEASE
            ],
        }]
        monkeypatch.setattr(_ps, "_resolve_release_mbid", lambda artist, album, tracks: "rel-karma")
        monkeypatch.setattr(_ps, "lb_get_release_metadata_batch", lambda *a, **k: {})
        monkeypatch.setattr(
            "services.enrichment.musicbrainz_service.get_shared_mb_client",
            lambda *a, **k: _FakeMBClient(media),
        )
        monkeypatch.setattr(
            _ps, "lb_get_recording_popularity_batch",
            lambda mbids: {m: {"total_listen_count": 98765, "total_user_count": 42000}
                           for m in mbids},
        )
        out, _release = _ps.get_listenbrainz_album_tracklist_with_release(
            "Stray Kids", "KARMA", TRANSLATED_LOCAL
        )
        # CEREMONY by title; BLEEP by position (counts match -> same album).
        assert (out.get(_norm_key("CEREMONY")) or {}).get("recording_mbid") == "rec-cer-00000000000000000000001"
        bleep = out.get(_norm_key("BLEEP")) or {}
        assert bleep.get("recording_mbid") == "rec-bleep-kr-0000000000000000000001"
        assert bleep.get("listenbrainz_listens") == 98765


class _FakeMBClient:
    def __init__(self, media):
        self._media = media

    def get_release(self, release_mbid, inc="", timeout=10.0):
        return {"id": release_mbid, "media": self._media}



class TestCachedIdentityCorroborates:
    """The runner's skip-guard trusts a cached album-tracklist identity only
    when the persisted release title still corroborates the local title."""

    def _check(self, local_title, entry):
        from services.popularity.scan_stage_runner import _cached_identity_corroborates
        return _cached_identity_corroborates(local_title, entry)

    def test_a_poisoned_cached_row_is_not_trusted(self):
        entry = {"source": "album_tracklist", "recording_mbid": "ned-0010...", "release_track_title": "Happy"}
        assert self._check("Where Is My Mind?", entry) is False

    def test_a_corroborating_cached_row_is_trusted(self):
        entry = {"source": "album_tracklist", "recording_mbid": "rec-1", "release_track_title": "Farewell [Unplugged Version]"}
        assert self._check("Farewell (feat. Patty Gurdy)", entry) is True

    def test_a_legacy_row_without_a_release_title_is_re_resolved(self):
        entry = {"source": "album_tracklist", "recording_mbid": "rec-1"}
        assert self._check("Live Forever", entry) is False


class TestReleaseTrackTitleRoundTripsThroughTheCache:
    """Migration 020: the release title the identity was resolved from is
    persisted so a later scan can corroborate it."""

    def _repo(self, tmp_path, monkeypatch):
        import db.repositories.popularity_cache as repo
        from contextlib import contextmanager
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(f"sqlite:///{tmp_path}/pop.db")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE track_popularity_cache (
                    id INTEGER PRIMARY KEY,
                    artist TEXT NOT NULL,
                    title TEXT NOT NULL,
                    lastfm_listeners INTEGER DEFAULT 0,
                    lastfm_playcount BIGINT DEFAULT 0,
                    listenbrainz_listens INTEGER DEFAULT 0,
                    listenbrainz_users INTEGER DEFAULT 0,
                    lastfm_tags TEXT,
                    source TEXT DEFAULT 'bulk',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    recording_mbid TEXT,
                    release_track_title TEXT,
                    CONSTRAINT uq_track_popularity_artist_title UNIQUE (artist, title)
                )
            """))
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        @contextmanager
        def _session(*args, **kwargs):
            session = Session()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        monkeypatch.setattr(repo, "db_session", _session)
        return repo

    def test_written_then_read_back(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path, monkeypatch)
        repo.upsert_track_popularity_bulk([{
            "artist": "Various Artists", "title": "Where Is My Mind?",
            "listenbrainz_listens": 21409, "source": "album_tracklist",
            "recording_mbid": "rec-1", "release_track_title": "Happy",
        }])
        out = repo.get_cached_popularity_for_titles("Various Artists", ["Where Is My Mind?"])
        row = out.get("where is my mind?")
        assert row, "the row must be returned"
        from services.popularity.popularity_cache_service import _row_to_entry
        mapped = _row_to_entry(dict(row))
        assert mapped.get("release_track_title") == "Happy"
        assert mapped.get("recording_mbid") == "rec-1"

    def test_a_row_without_a_release_title_is_preserved_on_later_writes(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path, monkeypatch)
        repo.upsert_track_popularity_bulk([{
            "artist": "Band", "title": "Song",
            "listenbrainz_listens": 100, "source": "bulk",
            "recording_mbid": "rec-1", "release_track_title": "Song (Version)",
        }])
        # A later artist-prefetch write (listeners only, no identity fields)
        # must NOT clobber the persisted identity evidence.
        repo.upsert_track_popularity_bulk([{
            "artist": "Band", "title": "Song",
            "listenbrainz_listens": 200, "source": "bulk",
        }])
        out = repo.get_cached_popularity_for_titles("Band", ["Song"])
        row = out.get("song") or {}
        assert row.get("recording_mbid") == "rec-1"
        assert row.get("release_track_title") == "Song (Version)"