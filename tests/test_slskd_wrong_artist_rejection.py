"""Regression tests: Soulseek matching must not grab wrong-artist tracks.

Reproduces the Orville Peck miss: the queue item "Orville Peck - The Fall"
was matched against a completely different band's file —

    The Fall of Troy - Mukiltearth - 01 A Tribute to Orville Wilcox.flac

Two weak gates combined to accept it:

1. The artist-evidence gate matched the token "orville" anywhere in the
   remote path — but that token came from the TRACK TITLE ("A Tribute to
   Orville Wilcox"), not from the artist.  A lone shared first name is not
   evidence the target artist is present.
2. The title-substring fallback ("The Fall" ⊂ "The Fall of Troy") awarded
   partial title credit even though the artist is a different band, and the
   score reached the 30-point acceptance threshold.

Fix: the artist must be evidenced in the ARTIST SEGMENT of the candidate
(parsed artist field, or the path with the track-title removed) — either by
parsed-artist similarity, the full artist phrase as a substring, or at least
two significant artist words appearing together.  A single shared first
name in the track title no longer counts.
"""

from __future__ import annotations

from services.downloads.download_pipeline_service import _score_result


def _candidate(filename: str, **overrides):
    """Build a slskd result dict with sensible defaults."""
    result = {
        "filename": filename,
        "bitrate": 320,
        "has_free_upload_slot": True,
        "queue_length": 0,
        "upload_speed": 2_000_000,
    }
    result.update(overrides)
    return result


class TestWrongArtistRejection:
    """A different band whose track title shares a first name must be rejected."""

    def test_orville_peck_not_matched_to_fall_of_troy(self):
        """'Orville Peck - The Fall' must NOT match The Fall of Troy's file.

        The candidate path contains the token 'orville' only inside the track
        title ('A Tribute to Orville Wilcox') — no artist evidence.
        """
        candidate = _candidate(
            "music\\The Fall of Troy (post‐hardcore band)\\[2020] Mukiltearth\\"
            "The Fall of Troy - Mukiltearth - 01 A Tribute to Orville Wilcox.flac"
        )
        assert _score_result(
            candidate, "Orville Peck", "The Fall", expected_year=None,
        ) == 0.0

    def test_title_substring_without_artist_evidence_is_rejected(self):
        """A same-worded title by another band ('The Fall' ⊂ 'The Fall of Troy')"""
        candidate = _candidate(
            "The Fall of Troy - Mukiltearth - 01 A Tribute to Orville Wilcox.flac"
        )
        assert _score_result(
            candidate, "Orville Peck", "The Fall",
        ) == 0.0

    def test_single_shared_first_name_is_not_artist_evidence(self):
        """A lone first-name token shared with a song title is not evidence."""
        candidate = _candidate("Other Artist - Some Song ft. Orville Whatever.flac")
        assert _score_result(
            candidate, "Orville Peck", "The Fall",
        ) == 0.0


class TestLegitimateMatchesStillPass:
    """Real matches must keep scoring above the acceptance threshold."""

    def test_artist_album_title_parse_matches(self):
        candidate = _candidate(
            "Orville Peck - Stampede - 02 The Fall.flac"
        )
        assert _score_result(candidate, "Orville Peck", "The Fall") >= 30.0

    def test_artist_folder_evidence_matches(self):
        """Artist name in a parent folder (not the filename) is evidence."""
        candidate = _candidate(
            "music\\Orville Peck\\[2024] Stampede\\Orville Peck - The Fall.flac"
        )
        assert _score_result(candidate, "Orville Peck", "The Fall") >= 30.0

    def test_full_artist_phrase_anywhere_matches(self):
        candidate = _candidate(
            "Orville Peck - The Fall (Live).flac"
        )
        assert _score_result(candidate, "Orville Peck", "The Fall") >= 30.0


class TestMultiWordArtistEvidence:
    """Two significant artist words together count as evidence."""

    def test_two_word_artist_in_scope_is_evidenced(self):
        candidate = _candidate(
            "Various - The Pretty Reckless - Heaven Knows.flac"
        )
        # 'pretty' + 'reckless' together in the artist-scope tokens.
        assert _score_result(candidate, "The Pretty Reckless", "Heaven Knows") >= 30.0

    def test_one_of_two_artist_words_is_not_evidence(self):
        """Only one of 'pretty'/'reckless' appearing is not evidence."""
        candidate = _candidate(
            "A Pretty Song - Heaven Knows.flac"
        )
        assert _score_result(candidate, "The Pretty Reckless", "Heaven Knows") == 0.0


class TestAlbumArtistsAndEdgeCases:
    """Album-artist credits and generic-artist skips."""

    def test_album_artist_in_path_is_evidenced(self):
        candidate = _candidate(
            "music\\Various Artists\\[2024] Stampede\\Orville Peck - The Fall.flac"
        )
        assert _score_result(candidate, "Orville Peck", "The Fall") >= 30.0

    def test_unknown_artist_skips_gate(self):
        """'Unknown' artist skips the gate (title-based fallback allowed).

        With no artist to verify, the title substring may apply — the gate
        only protects KNOWN artists from wrong-artist grabs.
        """
        candidate = _candidate(
            "The Fall of Troy - Mukiltearth - 01 A Tribute to Orville Wilcox.flac"
        )
        score = _score_result(candidate, "Unknown", "The Fall")
        # Gate skipped → partial title substring credit applies (> 0).
        assert score > 0.0

    def test_generic_various_artist_skips_gate(self):
        candidate = _candidate(
            "Various Artists - The Fall - Compilation.flac"
        )
        assert _score_result(candidate, "Various Artists", "The Fall") >= 30.0


class TestWrongAlbumRejection:
    """A file from the WRONG ALBUM must be rejected even when the artist
    matches — searching "Lament for the Hollow" (from *Obscured Horizons*)
    must never download "07 - Yesterday's Fire" from a different release."""

    def test_same_artist_different_album_rejected(self):
        candidate = _candidate(
            "The Eternal - When The Circle Of Light Begins To Fade - 07 - Yesterday's Fire.flac"
        )
        assert _score_result(
            candidate,
            "The Eternal", "Lament for the Hollow",
            expected_album="Obscured Horizons",
        ) == 0.0

    def test_folder_album_mismatch_rejected(self):
        """Album in the parent folder (basename has only a track number)."""
        candidate = _candidate(
            "music/The Eternal [AUS]/2013 - When The Circle Of Light Begins To Fade/07 - Yesterday's Fire.flac"
        )
        assert _score_result(
            candidate,
            "The Eternal", "Lament for the Hollow",
            expected_album="Obscured Horizons",
        ) == 0.0

    def test_correct_album_still_passes(self):
        candidate = _candidate(
            "The Eternal - Obscured Horizons - 01 - Lament for the Hollow.flac"
        )
        assert _score_result(
            candidate,
            "The Eternal", "Lament for the Hollow",
            expected_album="Obscured Horizons",
        ) >= 30.0

    def test_track_number_only_basename_parses_title(self):
        """'07 - Yesterday's Fire.flac' must parse '07' as the track number,
        NOT the artist (previously the artist gate gave folder-artist credit
        and let wrong files through)."""
        from services.downloads.download_pipeline_service import _parse_filename_parts

        parts = _parse_filename_parts("07 - Yesterday's Fire.flac")
        assert parts["artist"] is None
        assert parts["title"] == "Yesterday's Fire"
        assert parts["has_track_number"] is True


class TestHangulTitleMatching:
    """Pure-Hangul tracks (Stray Kids "일상", "타", "미친 놈 (Ex)") previously
    ended ``no_qualifying_result`` despite 39-101 real Soulseek candidates:
    the ASCII-only ``[a-z0-9]`` token fallback found nothing and Korean
    candidates with annotations ("(Korean Ver.)", "(Ex)") dropped the raw
    similarity below the hard title gate.  The bracket-stripped core title
    comparison (which keeps Hangul intact) must let them match."""

    def test_hangul_exact_title_matches(self):
        candidate = _candidate("Stray Kids - 일상.flac")
        assert _score_result(candidate, "Stray Kids", "일상") >= 30.0

    def test_hangul_single_char_title_matches(self):
        candidate = _candidate("Stray Kids - 타.flac")
        assert _score_result(candidate, "Stray Kids", "타") >= 30.0

    def test_hangul_annotated_candidate_matches(self):
        """'일상 (Korean Ver.)' must still match '일상' (bracket-stripped
        core comparison) instead of falling below the title gate."""
        candidate = _candidate("Stray Kids - 일상 (Korean Ver.).flac")
        assert _score_result(candidate, "Stray Kids", "일상") >= 30.0

    def test_hangul_paren_ex_suffix_matches(self):
        candidate = _candidate("Stray Kids - 미친 놈 (Ex).flac")
        assert _score_result(candidate, "Stray Kids", "미친 놈 (Ex)") >= 30.0

    def test_hangul_wrong_track_rejected(self):
        """A genuinely different Hangul track must still be rejected."""
        candidate = _candidate("Stray Kids - 다른 노래.flac")
        assert _score_result(candidate, "Stray Kids", "일상") == 0.0


class TestHangulArtistScriptMismatch:
    """K-pop peers name the artist in Korean ("스트레이 키즈") while the queue
    item carries the Latin name ("Stray Kids"), or vice versa.  The artist-
    evidence gate used only Latin ``[a-z0-9]`` tokens, so the Korean-named
    candidate produced an EMPTY token set and was rejected as "no artist
    evidence" even though the TITLE matched perfectly — the reported
    "Soulseek searches for Stray Kids fail on Korean tracks like
    토끼와 거북이".  A strong Hangul/CJK title match must satisfy the gate,
    and the artist contributes a modest credit so the total clears the
    accept floor.  Wrong tracks must STILL be rejected."""

    def test_korean_artist_latin_queue_matches(self):
        candidate = _candidate("스트레이 키즈 - 토끼와 거북이.flac")
        assert _score_result(candidate, "Stray Kids", "토끼와 거북이") >= 45.0

    def test_latin_artist_korean_queue_matches(self):
        candidate = _candidate("Stray Kids - 토끼와 거북이.flac")
        assert _score_result(candidate, "스트레이 키즈", "토끼와 거북이") >= 45.0

    def test_korean_artist_annotated_title_matches(self):
        candidate = _candidate("스트레이 키즈 - 토끼와 거북이 (Korean Ver.).flac")
        assert _score_result(candidate, "Stray Kids", "토끼와 거북이") >= 45.0

    def test_both_korean_matches(self):
        candidate = _candidate("스트레이 키즈 - 토끼와 거북이.flac")
        assert _score_result(candidate, "스트레이 키즈", "토끼와 거북이") >= 45.0

    def test_wrong_korean_track_rejected(self):
        """A genuinely different Hangul track must still be rejected even
        when the artist is Korean-named."""
        candidate = _candidate("스트레이 키즈 - 미친 놈 (Ex).flac")
        assert _score_result(candidate, "Stray Kids", "토끼와 거북이") == 0.0

    def test_wrong_latin_track_rejected(self):
        candidate = _candidate("Stray Kids - Another Song.flac")
        assert _score_result(candidate, "Stray Kids", "토끼와 거북이") == 0.0


class TestCompilationAlbumLeniency:
    """A track queued from a Various Artists compilation also lives on the
    artist's OWN release, so peers carry it from there with a DIFFERENT album
    name.  The hard album gate must not reject it when artist + title agree —
    reported as *"it doesn't match due to the album name being different even
    though the track artist matches the artist and song title"*."""

    def test_va_queue_row_matches_the_artists_own_release_file(self):
        """Queue: "A Perfect Circle - Weak and Powerless" on
        "MTV2 Headbangers Ball".  The candidate is from "Mer de Noms" —
        different album, same artist, same title."""

        candidate = _candidate(
            "A Perfect Circle - Mer de Noms - 04 - Weak and Powerless.flac"
        )
        score = _score_result(
            candidate,
            "A Perfect Circle", "Weak and Powerless",
            expected_album="MTV2 Headbangers Ball",
            expected_album_artist="Various Artists",
        )
        assert score > 0.0, (
            "a VA compilation album must not hard-reject the same song from "
            "the artist's own release"
        )
        assert score >= 45.0, (
            "the candidate must clear the accept floor — this is the file a "
            "VA row should download"
        )

    def test_compilation_artist_variant_matches_too(self):
        """'VA' is a placeholder in the same set as 'Various Artists'."""
        candidate = _candidate("A Perfect Circle - Mer de Noms - Weak and Powerless.flac")
        score = _score_result(
            candidate,
            "A Perfect Circle", "Weak and Powerless",
            expected_album="MTV2 Headbangers Ball",
            expected_album_artist="VA",
        )
        assert score >= 45.0

    def test_album_artist_missing_still_relaxes_for_a_va_artist(self):
        """Some VA rows only set the per-track artist; the ALBUM name alone is
        not enough to know it is a compilation, so the artist fallback in the
        placeholder set must participate."""
        candidate = _candidate("Various Artists - Weak and Powerless.flac")
        score = _score_result(
            candidate,
            "Various Artists", "Weak and Powerless",
            expected_album="MTV2 Headbangers Ball",
        )
        assert score >= 45.0


class TestCoreTitleOverridesTheAlbumGate:
    """A version/edition marker drops the RAW title similarity below 0.85
    even though the bracket-stripped CORE is the same song.  The same-song-
    from-another-release carve-out the gate already allows for exact titles
    must also apply to a core match."""

    def test_annotated_title_from_another_release_is_accepted(self):
        """The filename carries album + track number, so the parser DOES see
        the mismatched album ("Mer de Noms" ≠ "MTV2 Headbangers Ball") and the
        raw title score for "Weak and Powerless (Album Version)" is below the
        0.85 override — only the bracket-stripped CORE match saves it."""
        candidate = _candidate(
            "A Perfect Circle - Mer de Noms - 04 - Weak and Powerless (Album Version).flac"
        )
        score = _score_result(
            candidate,
            "A Perfect Circle", "Weak and Powerless",
            expected_album="MTV2 Headbangers Ball",
        )
        assert score > 0.0, (
            "core titles match, so the different album is another release of "
            "the SAME song — not evidence of a wrong file"
        )

    def test_genuinely_different_title_from_another_album_still_rejected(self):
        """CONTROL — the album gate must keep its teeth when the title is a
        different song entirely (the pinned wrong-album case)."""
        candidate = _candidate(
            "The Eternal - When The Circle Of Light Begins To Fade - 07 - Yesterday's Fire.flac"
        )
        assert _score_result(
            candidate,
            "The Eternal", "Lament for the Hollow",
            expected_album="Obscured Horizons",
        ) == 0.0


class TestQueueItemIsCompilation:
    def test_recognises_the_placeholders(self):
        from services.downloads.download_pipeline_service import _queue_item_is_compilation

        assert _queue_item_is_compilation(
            expected_artist="A Perfect Circle",
            expected_album_artist="Various Artists",
        ) is True
        assert _queue_item_is_compilation(
            expected_artist="A Perfect Circle",
            expected_album_artist="various",
        ) is True
        assert _queue_item_is_compilation(
            expected_artist="Various Artists",
            expected_album_artist=None,
        ) is True
        assert _queue_item_is_compilation(
            expected_artist="A Perfect Circle",
            expected_album_artist="A Perfect Circle",
        ) is False
        assert _queue_item_is_compilation(
            expected_artist="A Perfect Circle",
            expected_album_artist=None,
        ) is False
