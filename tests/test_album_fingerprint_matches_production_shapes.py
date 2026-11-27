"""The album fingerprint must actually match on unchanged data.

REPORTED (thousands of lines, every album, every scan):

    Re-importing album — song IDs match but metadata changed or cannot be proven
    unchanged album='Roots' fingerprint_equal=False dirty=False

``dirty=False`` on every line was the tell: nothing had been edited, the
comparison itself could never succeed. The consequences in PostgreSQL were
severe — constant full re-imports meant constant writes, which produced
multi-minute checkpoints, a 40-minute autovacuum of ``tracks``, and a deadlock
between two writers of the same rows.

Two asymmetries made the hash permanently unequal:

1. **`year`** — Navidrome's ``Child.year`` is the EDITION year and
   ``payload_builder`` stores it in ``release_year``, putting the ORIGINAL year
   in ``year``. The fingerprint compared the incoming edition year against the
   stored original year.
2. **`artist`/`album_artist`** — the incoming side applied
   ``clean_artist_name_for_storage``; the stored side read the column raw, so a
   case-only divergence ("Soil" vs "SOiL") never settled.
"""

from __future__ import annotations

from services.scanning import reconciliation as rec


def _nav(**overrides):
    track = {
        "id": "id-1",
        "path": "/music/Artist/Album/01 - Song.flac",
        "title": "Song",
        "artist": "Artist",
        "albumArtist": "Artist",
        "year": "2013",          # the EDITION year, as Navidrome reports it
        "trackNumber": 1,
        "discNumber": 1,
        "duration": 245.0,
        "musicBrainzId": "rec-1",
        "genre": "Rock",
    }
    track.update(overrides)
    return track


def _db(**overrides):
    """A row exactly as the import leaves it for a 1987 album reissued in 2013."""
    row = {
        "id": "id-1",
        "file_path": "/music/Artist/Album/01 - Song.flac",
        "title": "Song",
        "artist": "Artist",
        "album_artist": "Artist",
        "album": "Album",
        "year": "1987",          # ORIGINAL year
        "release_year": "2013",  # EDITION year (what Navidrome reports)
        "track_number": "1",
        "disc_number": "1",
        "duration": 245.0,
        "recording_mbid": "rec-1",
        "navidrome_genres": '["rock"]',
    }
    row.update(overrides)
    return row


def _fp_nav(tracks, album="Album"):
    return rec.navidrome_album_fingerprint(tracks, album_name=album, album_artist="Artist")


class TestARemastersEditionYearDoesNotBreakTheMatch:
    def test_original_year_1987_vs_edition_2013_matches(self):
        """THE REPORTED LOOP: the stored original year is not what Navidrome sends."""
        assert _fp_nav([_nav()]) == rec.db_album_fingerprint([_db()])

    def test_a_genuinely_changed_edition_year_still_differs(self):
        """CONTROL: the fingerprint must still notice an edition change."""
        base = rec.db_album_fingerprint([_db()])
        assert rec.db_album_fingerprint([_db(release_year="1990")]) != base

    def test_a_row_with_no_edition_year_falls_back_to_year(self):
        assert rec.db_album_fingerprint([_db(release_year=None, year="2013")]) == _fp_nav(
            [_nav()]
        )


class TestACaseOnlyArtistDifferenceDoesNotBreakTheMatch:
    def test_the_stored_column_is_cleaned_like_the_incoming_name(self):
        """ "SOiL" is a mixed-case act; neither side may mangle it."""
        assert _fp_nav([_nav(artist="SOiL")]) == rec.db_album_fingerprint(
            [_db(artist="SOiL")]
        )

    def test_a_payload_cleaned_value_agrees_with_the_incoming_one(self):
        # ``payload_builder`` writes the cleaned name; the DB column holds it.
        assert _fp_nav([_nav(artist="nirvana")]) == rec.db_album_fingerprint(
            [_db(artist="Nirvana")]
        )

    def test_a_genuinely_different_artist_still_differs(self):
        """CONTROL: cleaning must not erase a real artist change."""
        assert _fp_nav([_nav(artist="Artist")]) != rec.db_album_fingerprint(
            [_db(artist="Other Artist")]
        )


class TestTheSkipGateActuallySkips:
    def test_unchanged_album_is_skipped(self):
        from services.scanning.filters import should_skip_cached_album

        nav = [_nav()]
        equal = _fp_nav(nav) == rec.db_album_fingerprint([_db()])
        assert equal, "precondition: the fingerprints must match"
        assert should_skip_cached_album(
            artist_name="Artist",
            album_name="Album",
            tracks=nav,
            cached_ids_for_album={"id-1"},
            force=False,
            album_needs_reimport=False,
            verbose=False,
            metadata_fingerprint_equal=True,
            album_metadata_dirty=False,
        ) is True
