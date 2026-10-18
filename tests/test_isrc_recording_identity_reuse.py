"""A duplicate recording is answered from the copy already in the library.

Reported: *"As some albums contain the same track (matched ISRC), if another copy
of the same track is found, can it copy the data from the other track on the
database rather than doing a new lookup for the data? I'm assuming the lookup for
the same track on different albums that match the ISRC would have the same
count."*

THE ASSUMPTION IS SOUND, and the reason is a KEY, not a heuristic:

* an ISRC identifies a specific PERFORMANCE, and ``recording_mbid`` is the same
  identity under another name;
* **ListenBrainz** counts are held against the RECORDING MBID, so every album
  carrying that recording reads the same number;
* **Last.fm** counts are held against one artist+title track page, so the same
  recording reads the same listeners.

WHY A SECOND LOOKUP HAPPENED ANYWAY. Every cache in ``popularity_cache_service``
is keyed ``(artist, title)``, and the per-artist prefetch is skipped for a
compilation artist entirely:

* the same recording titled differently on the two albums ("Song" vs
  "Song (feat. X)") keys two rows;
* a compilation credits "Various Artists" while the artist's own album credits
  the band, and no "Various Artists" catalogue exists to prefetch.

WHAT IS DELIBERATELY NOT COPIED

* ``is_single`` and any score. A single verdict is a fact about a RELEASE — a
  track can be a single off one album and a deep cut on a compilation — and
  ``finalise_stage._sync_isrc_popularity`` already owns the score/is_single side.
* a row that holds no counts. The caller's branches treat a falsy count as "no
  data" and fall through to the lookup, so a zero entry would change nothing
  except to look like an answer.
* the identity match is NOT re-checked against the length. The length guard
  decides identity; re-litigating it here would reject copies of the same
  recording that sit at different positions.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from services.popularity.popularity_cache_service import (
    get_recording_identity_popularity,
)

DDL = """
CREATE TABLE tracks (
    id TEXT PRIMARY KEY,
    title TEXT, artist TEXT, album TEXT, album_artist TEXT,
    isrc TEXT, recording_mbid TEXT, mbid TEXT,
    lastfm_listeners INTEGER DEFAULT 0, lastfm_playcount INTEGER DEFAULT 0,
    listenbrainz_listens INTEGER DEFAULT 0, listenbrainz_users INTEGER DEFAULT 0,
    lastfm_tags TEXT, is_live INTEGER DEFAULT 0
)
"""

#: (id, title, artist, album, album_artist, isrc, recording_mbid, mbid,
#:  lf, lfpc, lb, lbu, tags, is_live)
ROWS = [
    # The artist's own album — scanned, both identities, real counts.
    (
        "t1", "Song", "Band", "Album A", "Band",
        "USRC17607839", "rec-1", "rec-1",
        84200, 500000, 468, 120, '["rock"]', 0,
    ),
    # A COMPILATION copy of the same recording, titled differently, counts unset.
    (
        "t2", "Song (feat. Someone)", "Various Artists", "Comp", "Various Artists",
        "USRC17607839", "", "", 0, 0, 0, 0, None, 0,
    ),
    # A different recording: must never answer for the above.
    (
        "t3", "Different Song", "Other", "Album X", "Other",
        "GBAAA0000001", "rec-2", "rec-2", 999, 10, 5, 1, None, 0,
    ),
    # The same identity text but NO counts — offers nothing.
    (
        "t4", "Blank", "Other", "Album Y", "Other",
        "GBAAA0000002", "", "", 0, 0, 0, 0, None, 0,
    ),
    # A row whose isrc still holds the bracketed LIST form (never scanned).
    (
        "t5", "Legacy Form", "Band", "Album Z", "Band",
        '["USRC17607840"]', "", "", 1500, 900, 20, 3, None, 0,
    ),
]


@pytest.fixture()
def db(monkeypatch):
    """Point ``db_session`` at an in-memory SQLite DB holding the fixture rows."""
    from contextlib import contextmanager

    lite = create_engine("sqlite://")
    with lite.begin() as conn:
        conn.execute(text(DDL))
        for row in ROWS:
            conn.execute(
                text(
                    "INSERT INTO tracks (id, title, artist, album, album_artist, isrc,"
                    " recording_mbid, mbid, lastfm_listeners, lastfm_playcount,"
                    " listenbrainz_listens, listenbrainz_users, lastfm_tags, is_live)"
                    " VALUES (:id,:title,:artist,:album,:album_artist,:isrc,"
                    ":recording_mbid,:mbid,:lf,:lfp,:lb,:lbu,:tags,:live)"
                ),
                {
                    "id": row[0], "title": row[1], "artist": row[2], "album": row[3],
                    "album_artist": row[4], "isrc": row[5], "recording_mbid": row[6],
                    "mbid": row[7], "lf": row[8], "lfp": row[9], "lb": row[10],
                    "lbu": row[11], "tags": row[12], "live": row[13],
                },
            )

    @contextmanager
    def _session():
        with lite.begin() as conn:
            yield conn

    from db import engine as db_engine

    monkeypatch.setattr(db_engine, "db_session", _session)
    return lite


# ---------------------------------------------------------------------------
# The reported case
# ---------------------------------------------------------------------------


class TestTheDuplicateIsAnsweredFromTheCopy:
    def test_a_compilation_copy_gets_the_original_s_counts(self, db):
        """⭐ The reported case: a different artist AND a different title.

        This is the pair that could not be found by ``(artist, title)`` — the
        compilation credits "Various Artists" and the title carries a feat
        credit, so every existing cache key differs while the recording is the
        same one.
        """
        out = get_recording_identity_popularity(
            [{"id": "t2", "title": "Song (feat. Someone)", "isrc": "USRC17607839"}]
        )

        assert len(out) == 1
        entry = out["song"]
        assert entry["lastfm_listeners"] == 84200
        assert entry["lastfm_playcount"] == 500000
        assert entry["listenbrainz_listens"] == 468
        assert entry["listenbrainz_users"] == 120

    def test_it_carries_the_recording_identity(self, db):
        """⭐ Which is what skips the ISRC -> recording MusicBrainz call.

        ``resolve_isrc_recording`` is a shared 1 req/s request. Handing the
        track stage the identity it would have resolved removes it from the
        path entirely, not just the count lookups.
        """
        out = get_recording_identity_popularity(
            [{"id": "t2", "title": "Song (feat. Someone)", "isrc": "USRC17607839"}]
        )
        assert out["song"]["recording_mbid"] == "rec-1"

    def test_it_works_from_a_recording_mbid_alone(self, db):
        """A track with no ISRC still benefits — the identity is the MBID.

        ⚠️ CONTRACT REVERSED (2026-10-18): the identity is no longer SUFFICIENT
        on its own — the title must also corroborate. This test used to pass the
        unrelated label "By Mbid" and expect the copy. Matching on the identity
        alone is what let three bonus tracks inherit the lead single's data on
        Arion — "Life Is Not Beautiful" (they all carried its bad ISRC). The
        asserted PROPERTY is unchanged (an MBID with no ISRC resolves); only the
        title now has to agree. See
        ``test_isrc_reuse_requires_title_corroboration.py``.
        """
        out = get_recording_identity_popularity(
            [{"id": "t9", "title": "Song", "recording_mbid": "rec-1"}]
        )
        assert out["song"]["lastfm_listeners"] == 84200

    def test_it_works_from_an_isrc_alone(self, db):
        """Same reversal as above — the ISRC resolves, and the title
        corroborates."""
        out = get_recording_identity_popularity(
            [{"id": "t9", "title": "Song (feat. Someone)", "isrc": "USRC17607839"}]
        )
        assert out["song"]["listenbrainz_listens"] == 468

    def test_a_different_title_does_not_answer(self, db):
        """⭐ THE FIX, pinned here too: the identity alone is NOT enough.

        This is the reported Arion shape — a different song that merely shares
        the identity must not inherit its data.
        """
        out = get_recording_identity_popularity(
            [{"id": "t9", "title": "By Isrc", "isrc": "USRC17607839"}]
        )
        assert out == {}

    def test_it_reads_a_row_whose_isrc_is_still_a_bracketed_list(self, db):
        """⚠️ An UNSCANNED row keeps the raw tag form.

        The track stage normalises ``["CODE"]`` to ``CODE`` and writes it back,
        so a scanned row holds the bare code — but a row that has never been
        through a scan still holds the list. Equality alone would miss it, which
        is why there is a LIKE arm.

        ⚠️ The title corroborates (CONTRACT REVERSED, above): the row is titled
        "Legacy Form", so the request must say the same song, not the label
        "Legacy".
        """
        out = get_recording_identity_popularity(
            [{"id": "t9", "title": "Legacy Form", "isrc": "USRC17607840"}]
        )
        assert out["legacy form"]["lastfm_listeners"] == 1500

    def test_the_entry_is_prefetch_shaped(self, db):
        """So the per-track paths consume it without a branch of their own.

        The track stage reads ``lastfm_listeners`` (truthy gate),
        ``listenbrainz_listens`` and ``recording_mbid`` from a prefetch entry;
        a differently-shaped dict would be silently ignored.
        """
        out = get_recording_identity_popularity(
            [{"id": "t2", "title": "Song (feat. Someone)", "isrc": "USRC17607839"}]
        )
        assert set(out["song"]) >= {
            "lastfm_listeners", "lastfm_playcount",
            "listenbrainz_listens", "listenbrainz_users", "recording_mbid",
        }


# ---------------------------------------------------------------------------
# What must NOT be reused
# ---------------------------------------------------------------------------


class TestWhatIsNotReused:
    def test_a_track_never_answers_from_itself(self, db):
        """That is a re-read of the row's own counts, which the caller has."""
        assert get_recording_identity_popularity(
            [{"id": "t1", "title": "Song", "isrc": "USRC17607839"}]
        ) == {}

    def test_a_different_recording_is_never_used(self, db):
        assert get_recording_identity_popularity(
            [{"id": "t9", "title": "Never Seen", "isrc": "ZZZZZ0000001"}]
        ) == {}

    def test_a_track_with_no_identity_is_never_answered(self, db):
        assert get_recording_identity_popularity([{"id": "t9", "title": "Nothing"}]) == {}

    def test_a_sibling_holding_no_counts_offers_nothing(self, db):
        """⭐ An all-zero entry would CHANGE behaviour, not be a no-op.

        The caller's branches fall through to the lookup on a falsy count, so
        zeros are neutral there — but anything that checks only whether an entry
        EXISTS would read it as an answer. Offering nothing is the only safe
        shape.
        """
        assert get_recording_identity_popularity(
            [{"id": "t9", "title": "Blank", "isrc": "GBAAA0000002"}]
        ) == {}

    def test_it_does_not_copy_is_single_or_a_score(self, db):
        """⭐ A single verdict belongs to a RELEASE, not to the recording.

        The same recording can be a single off one album and a deep cut on a
        compilation, so copying the flag would manufacture a single.
        ``finalise_stage._sync_isrc_popularity`` owns that decision, with its own
        rules.
        """
        out = get_recording_identity_popularity(
            [{"id": "t2", "title": "Song (feat. Someone)", "isrc": "USRC17607839"}]
        )
        for forbidden in ("is_single", "single_confidence", "popularity", "final_score"):
            assert forbidden not in out["song"], (
                f"{forbidden!r} must not be copied — it is release-scoped"
            )


class TestTheQueryIsPortable:
    """⚠️ Both arms must build valid SQL on their own.

    The WHERE clause is assembled from up to two conditions. An earlier form
    joined an empty list into a bare ``()``, which is a SYNTAX ERROR — reached
    whenever a track has a recording MBID and no ISRC, or the reverse.
    """

    def test_an_mbid_with_no_isrc(self, db):
        """⚠️ CONTRACT REVERSED (2026-10-18): the title must corroborate too —
        see the class note above. The property under test (this arm builds valid
        SQL alone and resolves the row) is unchanged."""
        assert get_recording_identity_popularity(
            [{"id": "t9", "title": "Song", "recording_mbid": "rec-1"}]
        )["song"]["lastfm_listeners"] == 84200

    def test_an_isrc_with_no_mbid(self, db):
        assert get_recording_identity_popularity(
            [{"id": "t9", "title": "Song (feat. Someone)", "isrc": "USRC17607839"}]
        )["song"]["listenbrainz_listens"] == 468

    def test_both_present(self, db):
        assert get_recording_identity_popularity(
            [{"id": "t9", "title": "Song", "isrc": "USRC17607839",
              "recording_mbid": "rec-1"}]
        )["song"]["lastfm_listeners"] == 84200


# ---------------------------------------------------------------------------
# The wiring
# ---------------------------------------------------------------------------


class TestTheScanUsesIt:
    @staticmethod
    def _runner() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "services" / "popularity"
                / "scan_stage_runner.py").read_text(encoding="utf-8")

    def test_the_runner_calls_the_seeder(self):
        assert "get_recording_identity_popularity" in self._runner()

    def test_the_seed_is_album_scoped_not_artist_scoped(self):
        """⚠️ Leaking the seed across albums would answer the WRONG track.

        ``prefetched_popularity`` lives for the whole ARTIST pass and is keyed by
        normalised title. A seed taken from this album's "Song" (ISRC X) would
        answer the NEXT album's unrelated "Song" (ISRC Y), so the merge has to go
        into an album-scoped copy.
        """
        runner = self._runner()
        assert "_album_prefetched" in runner, (
            "the seed is not merged into an album-scoped dict"
        )
        assert '_track_options["prefetched_popularity"] = _album_prefetched' in runner, (
            "the per-track options must carry the album-scoped dict, not the "
            "artist-level one — otherwise the seed is silently discarded"
        )

    def test_the_prefetch_is_reset_for_every_artist(self):
        """⭐ The reset must happen BEFORE the compilation guard can skip it.

        It used to sit INSIDE ``if ... and not _is_compilation_artist``, so
        moving from a normal artist to a compilation left the previous artist's
        entries in place. Those are keyed by normalised title, and a compilation
        is exactly where a collision is most likely — it carries the well-known
        titles — so a compilation track could be answered from another artist's
        numbers and never looked up.
        """
        runner = self._runner()
        reset_at = runner.index(
            "if artist and artist != last_prefetch_artist:\n                    prefetched_popularity = {}"
        )
        guard_at = runner.index(
            "if artist and artist != last_prefetch_artist and not _is_compilation_artist:"
        )
        assert reset_at < guard_at, (
            "the artist-change reset must precede the compilation guard, or a "
            "compilation inherits the previous artist's prefetch"
        )
