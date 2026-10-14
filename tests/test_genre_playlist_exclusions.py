"""Genre playlists must use a track's OWN genres, not its album's blend.

Reported
--------
> **Alternative Rock - Top Tracks** — exclude tracks that ALSO carry any
> ``*metal*`` tag (alternative metal, nu metal, heavy metal, industrial metal,
> progressive metal, metalcore, sludge metal, death metal). Keeps Alternative
> Rock purely rock/indie/grunge/post-grunge (Foo Fighters, Muse, Smashing
> Pumpkins, Nirvana, Incubus) and stops Tool, Manson, Slipknot and Deftones
> from taking over.
>
> **Alternative Metal - Top Tracks** — allow ``alternative rock`` co-tags ONLY
> if ``alternative metal`` (or ``nu metal``) is present on the track/album —
> not just inherited from artist fallback when the track itself is only tagged
> ``alternative rock`` / ``pop rock`` / ``electronic``. Keeps Tool (*Schism*),
> Deftones, SOAD and Manson while dropping Linkin Park's *Iridescent*.

Why this needed more than a filter
----------------------------------
``tracks.genres`` is NOT a per-track value on an ordinary album.
``sync_album_file_tags`` writes ONE blended list to every row:

    UPDATE tracks SET genres = :g WHERE album_artist = :a AND album = :alb

So every track of a Linkin Park album carries the same ``genres`` string, and
the playlist builder could only ever answer "is anything on this album metal?",
never "is THIS track metal?". Measured: with a single blended value,
*Iridescent* (electronic/pop rock in its own right) joined the metal playlists
and the band's nu-metal tracks joined the rock one.

The per-track truth is in the SOURCE columns (``lastfm_tags``,
``musicbrainz_genres``, ``discogs_genres``, …), which the track stage writes per
track and the album sync never touches.

What was built
--------------
* ``services.catalog.album_classification_service`` gained ONE genre parser
  (``iter_genre_tokens``) and the metal-family test (``is_metal_genre``) beside
  the existing Christmas test — a substring test, because the subgenre
  vocabulary is unbounded and every spelling contains the word.
* ``finalise_stage`` reads the track's OWN tags when it has any
  (``own_tags_win``), falling back to the album blend only for a sparse track,
  and keeps a metal track out of the configured playlists
  (``metal_excluded_from``).
* Both rules live on the Config page, per the Config-page contract.

⚠️ Both rules are independent, and each is pinned by its own control:
``metal_excluded_from`` decides rule 1, ``own_tags_win`` decides rule 2.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# ⚠️ LAZY imports of the symbols this change ADDS.
#
# A module-scope ``from ... import is_metal_genre`` turns the whole file into a
# COLLECTION ERROR on the unpatched tree, which hides every individual verdict
# behind one "1 error" line. Resolving them at call time keeps the module
# importable so each test reports its own reason.
# ---------------------------------------------------------------------------


def _vocab():
    """``(iter_genre_tokens, is_metal_genre, genre_tokens_contain, is_christmas_genre)``."""
    from services.catalog.album_classification_service import (
        genre_tokens_contain,
        is_christmas_genre,
        is_metal_genre,
        iter_genre_tokens,
    )
    return iter_genre_tokens, is_metal_genre, genre_tokens_contain, is_christmas_genre

# ---------------------------------------------------------------------------
# The reported roster
# ---------------------------------------------------------------------------

#: The album-wide blend ``sync_album_file_tags`` fans out to EVERY track of a
#: Linkin Park album. One string, so it cannot separate the band's metal tracks
#: from its electronic ones.
LP_BLEND = "nu metal, alternative rock"

TOOL = {
    "id": "t-schism", "title": "Schism", "album": "Lateralus", "artist": "Tool",
    "album_artist": "Tool", "stars": 5, "popularity_score": 90.0,
    "is_live": 0, "is_compilation": 0, "duration": 400,
    "lastfm_listeners": 900_000, "listenbrainz_listens": 500_000,
    # Carries alternative rock TOO — that is what makes rule 1 a real test.
    "genres": "progressive metal, alternative metal, alternative rock",
    "musicbrainz_genres": '["progressive metal", "alternative metal", "alternative rock"]',
    "lastfm_tags": '["progressive metal"]',
}

#: Linkin Park's metal side. Its own tags carry nu metal.
LP_METAL = {
    "id": "lp-papercut", "title": "Papercut", "album": "Hybrid Theory",
    "artist": "Linkin Park", "album_artist": "Linkin Park", "stars": 5,
    "popularity_score": 88.0, "is_live": 0, "is_compilation": 0, "duration": 200,
    "lastfm_listeners": 800_000, "listenbrainz_listens": 400_000,
    "genres": LP_BLEND,
    "musicbrainz_genres": '["nu metal", "alternative metal"]',
    "lastfm_tags": '["nu metal"]',
}

#: ⭐ THE REPORTED TRACK. Same album blend as the row above, but its OWN tags are
#: electronic/pop rock, so it must not reach the metal playlists.
LP_SOFT = {
    "id": "lp-iridescent", "title": "Iridescent", "album": "A Thousand Suns",
    "artist": "Linkin Park", "album_artist": "Linkin Park", "stars": 4,
    "popularity_score": 80.0, "is_live": 0, "is_compilation": 0, "duration": 250,
    "lastfm_listeners": 700_000, "listenbrainz_listens": 300_000,
    "genres": LP_BLEND,
    "musicbrainz_genres": '["alternative rock", "electronic", "pop rock"]',
    "lastfm_tags": '["electronic"]',
}

#: The act the rock playlist exists for.
FOO_FIGHTERS = {
    "id": "ff-everlong", "title": "Everlong", "album": "The Colour and the Shape",
    "artist": "Foo Fighters", "album_artist": "Foo Fighters", "stars": 5,
    "popularity_score": 92.0, "is_live": 0, "is_compilation": 0, "duration": 250,
    "lastfm_listeners": 2_000_000, "listenbrainz_listens": 900_000,
    "genres": "alternative rock, post-grunge",
    "musicbrainz_genres": '["alternative rock", "post-grunge"]',
    "lastfm_tags": '["alternative rock"]',
}

#: A track with NO source tags at all: the album blend is the only signal, which
#: is the case ``own_tags_win`` deliberately still allows.
SPARSE = {
    "id": "sparse-1", "title": "Untagged Metal Cut", "album": "Metal Album",
    "artist": "Some Band", "album_artist": "Some Band", "stars": 4,
    "popularity_score": 70.0, "is_live": 0, "is_compilation": 0, "duration": 200,
    "lastfm_listeners": 10_000, "listenbrainz_listens": 5_000,
    "genres": "alternative metal",
    "musicbrainz_genres": None, "lastfm_tags": None,
}

ROWS = [TOOL, LP_METAL, LP_SOFT, FOO_FIGHTERS, SPARSE]
TITLES = {row["id"]: row["title"] for row in ROWS}


# ---------------------------------------------------------------------------
# The builder, driven for real
# ---------------------------------------------------------------------------


@pytest.fixture
def build(monkeypatch):
    """Run the REAL playlist builder over a fixed row set.

    Returns ``{playlist_name: [track ids]}``.

    ⚠️ ``_create_genre_top_track_playlists`` does
    ``from helpers.config_helpers import get_config`` INSIDE the function, so
    the patch has to land on that module rather than on ``finalise_stage`` —
    the same trap as ``finalise_stage.db_session``.
    """
    import helpers.config_helpers as config_helpers
    from services.popularity.stages import finalise_stage as fs

    def _run(*, own_tags_win=True, excluded=("alternative rock",), enabled=True):
        captured: dict[str, list[str]] = {}

        monkeypatch.setattr(fs, "_fetch_genre_playlist_rows", lambda min_stars: ROWS)
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [])
        monkeypatch.setattr(fs, "_genre_playlists_enabled", lambda: True)
        monkeypatch.setattr(fs, "_genre_playlists_delete_enabled", lambda: False)
        monkeypatch.setattr(fs, "_genre_playlists_active", lambda: True)
        monkeypatch.setattr(
            fs, "_sync_playlist_to_navidrome",
            lambda name, song_ids, rows=None: (
                captured.__setitem__(name, list(song_ids))
                or {"ok": True, "failed": [], "updated": 0}
            ),
        )
        monkeypatch.setattr(config_helpers, "get_config", lambda: {
            "playlists": {
                "genre_playlists_create_threshold": 1,
                "genre_playlists_min_stars": 1,
                "genre_playlists_max_genres": 3,
                "genre_playlists_max_tracks": 0,
                "genre_playlists_max_per_artist": 0,
                "playlist_interleave_artists": False,
                "genre_exclusions": {
                    "enabled": enabled,
                    "own_tags_win": own_tags_win,
                    "metal_excluded_from": list(excluded),
                },
            }
        })

        fs._create_genre_top_track_playlists()
        return captured

    def _titles(captured, name):
        return [TITLES[i] for i in captured.get(name, [])]

    _run.titles = _titles
    return _run


ALT_ROCK = "Alternative Rock - Top Tracks"
ALT_METAL = "Alternative Metal - Top Tracks"
NU_METAL = "Nu Metal - Top Tracks"


# ===========================================================================
# 1. Rule 1 — a metal track stays out of the rock playlist
# ===========================================================================


class TestMetalIsKeptOutOfTheRockPlaylist:
    def test_a_crossover_track_is_excluded(self, build):
        """THE REPORTED TAKEOVER. Schism carries alternative rock AND metal, so
        before the rule it pooled into the rock playlist."""
        captured = build()
        rock = build.titles(captured, ALT_ROCK)

        assert "Schism" not in rock, (
            "Tool is in the Alternative Rock playlist — the rule that keeps "
            "rock/indie/grunge acts in it is not firing"
        )

    def test_the_rock_playlist_keeps_the_acts_it_is_for(self, build):
        captured = build()
        rock = build.titles(captured, ALT_ROCK)

        assert "Everlong" in rock, "Foo Fighters must stay in Alternative Rock"

    def test_a_soft_track_on_a_metal_tagged_album_stays(self, build):
        """Iridescent's ALBUM blend carries nu metal. It is not itself metal, so
        the album tag must not evict it from the rock playlist — the exact
        failure that made the two playlists mirror each other."""
        captured = build()
        rock = build.titles(captured, ALT_ROCK)

        assert "Iridescent" in rock

    def test_the_exclusion_count_is_reported(self, build, monkeypatch):
        """A rule that silently drops tracks reads as a bug. One line, only when
        it fires."""
        logged: list[str] = []
        from services.popularity.stages import finalise_stage as fs

        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(str(msg)))
        build()

        assert any("Genre exclusion" in line for line in logged), (
            "the genre exclusion fired but reported nothing"
        )


# ===========================================================================
# 2. Rule 2 — a metal playlist is built from the track's OWN tags
# ===========================================================================


class TestAMetalPlaylistNeedsTheTrackToBeMetal:
    def test_a_genuinely_metal_track_is_kept(self, build):
        """Tool's Schism is the track the rule exists to KEEP."""
        captured = build()

        assert "Schism" in build.titles(captured, ALT_METAL)

    def test_a_soft_track_from_the_same_album_is_dropped(self, build):
        """THE REPORTED CASE. Iridescent shares its album blend — and therefore
        its metal tag — with the band's nu-metal tracks, yet its own tags are
        alternative rock / electronic / pop rock."""
        captured = build()
        metal = build.titles(captured, ALT_METAL)

        assert "Iridescent" not in metal, (
            "a track whose own tags carry no metal genre reached the metal "
            "playlist — the album blend is still deciding"
        )

    def test_the_bands_metal_track_still_reaches_a_metal_playlist(self, build):
        """CONTROL — dropping Iridescent must not drop the band's metal side.
        Papercut lands in a metal playlist; which one depends on the album's own
        tagging, so the assertion is that it lands in one at all."""
        captured = build()
        metal_playlists = [
            name for name in captured if "metal" in name.lower()
        ]
        assert metal_playlists, "the roster produced no metal playlist at all"

        in_some_metal = any(
            "Papercut" in build.titles(captured, name) for name in metal_playlists
        )
        assert in_some_metal, "Linkin Park's nu-metal track vanished entirely"

    def test_a_rock_act_never_reaches_a_metal_playlist(self, build):
        captured = build()

        assert "Everlong" not in build.titles(captured, ALT_METAL)

    def test_a_sparse_track_inherits_the_album_blend(self, build):
        """CONTROL — the fallback is preserved ON PURPOSE. A track with no tags
        of its own has nothing to be judged on, so the album blend decides."""
        captured = build()

        assert "Untagged Metal Cut" in build.titles(captured, ALT_METAL)


# ===========================================================================
# 3. Controls — each rule is independent, and off means off
# ===========================================================================


class TestTheRulesAreIndependentlySwitchable:
    def test_an_empty_exclusion_list_restores_the_takeover(self, build):
        """CONTROL FOR RULE 1 — proves the exclusion list is what prevents it.
        With no playlists protected, Schism pools into Alternative Rock again."""
        captured = build(excluded=())
        rock = build.titles(captured, ALT_ROCK)

        assert "Schism" in rock, (
            "with no protected playlists the metal crossover should pool "
            "normally — if it does not, the exclusion list is not the mechanism"
        )

    def test_rule_2_survives_an_empty_exclusion_list(self, build):
        """CONTROL — the two rules are SEPARATE. Dropping every protected genre
        must not disable the own-tags rule."""
        captured = build(excluded=())
        metal = build.titles(captured, ALT_METAL)

        assert "Iridescent" not in metal, (
            "rule 2 is riding on the exclusion list rather than on own_tags_win"
        )

    def test_ignoring_own_tags_falls_back_to_the_blend(self, build):
        """CONTROL FOR RULE 2 — the switch has to do something. With own tags
        ignored, Iridescent's album blend counts and it drops out of the rock
        playlist too (the confusing old behaviour)."""
        captured = build(own_tags_win=False)
        rock = build.titles(captured, ALT_ROCK)

        assert "Iridescent" not in rock, (
            "own_tags_win=False did not change the verdict — the switch is dead"
        )

    def test_disabling_the_block_restores_the_takeover(self, build):
        """CONTROL — ``enabled: false`` turns both rules off."""
        captured = build(enabled=False)

        assert "Schism" in build.titles(captured, ALT_ROCK)


# ===========================================================================
# 4. The genre vocabulary
# ===========================================================================


class TestTheGenreVocabulary:
    @pytest.mark.parametrize("raw", [
        "alternative metal", "Metalcore", "nu-metal", "nu metal", "heavy metal",
        "industrial metal", "progressive metal", "sludge metal", "death metal",
        "post-metal", "gothic metal", '["nu metal", "alternative rock"]',
        "heavy metal, hard rock", "metal",
    ])
    def test_every_metal_spelling_is_recognised(self, raw):
        """SUBSTRING, not a closed list: the user's own examples plus the
        spellings a source may return. A fixed set would stop matching the
        moment a new subgenre appears."""
        _iter, is_metal_genre, _contains, _christmas = _vocab()

        assert is_metal_genre(raw) is True

    @pytest.mark.parametrize("raw", [
        "alternative rock", "hard rock", "grunge", "post-grunge", "indie rock",
        "progressive rock", "electronic", "pop rock", "Holiday Rock",
        None, "", [],
    ])
    def test_non_metal_genres_are_not_metal(self, raw):
        _iter, is_metal_genre, _contains, _christmas = _vocab()

        assert is_metal_genre(raw) is False

    def test_the_parser_handles_every_stored_form(self):
        """The DB hands back a list, a JSON string or a delimited string
        depending on the column and the path that wrote it."""
        iter_genre_tokens = _vocab()[0]

        assert iter_genre_tokens('["Goth Metal", "metalcore"]') == ["goth metal", "metalcore"]
        assert iter_genre_tokens("Rock / Alternative; Metal") == ["rock", "alternative", "metal"]
        assert iter_genre_tokens(["Nu Metal"]) == ["nu metal"]
        assert iter_genre_tokens(None) == []

    def test_hyphens_are_part_of_the_word(self):
        """ ``nu-metal`` and ``nu metal`` are one genre. Splitting on the hyphen
        would turn ``progressive-metal`` into two meaningless tokens."""
        iter_genre_tokens, is_metal_genre, _contains, _christmas = _vocab()

        assert iter_genre_tokens("nu-metal") == ["nu-metal"]
        assert is_metal_genre("nu-metal") is True

    def test_the_needle_is_normalised_too(self):
        _iter, _metal, genre_tokens_contain, _christmas = _vocab()

        assert genre_tokens_contain("Alternative Metal", "METAL") is True

    def test_the_christmas_test_is_unchanged_by_the_refactor(self):
        """CONTROL — ``is_christmas_genre`` now shares the parser, so its
        whole-token semantics must survive: ``holiday`` matches, ``Holiday
        Rock`` (a real genre) does not."""
        is_christmas_genre = _vocab()[3]

        assert is_christmas_genre("Holiday") is True
        assert is_christmas_genre('["Christmas"]') is True
        assert is_christmas_genre("Holiday Rock") is False
        assert is_christmas_genre("holiday rock") is False
        assert is_christmas_genre("rock, metal") is False


# ===========================================================================
# 5. The per-track resolver
# ===========================================================================


class TestTheResolverPrefersTheTracksOwnTags:
    def _resolver(self):
        from services.popularity.stages.finalise_stage import (
            _playlist_identity_tokens,
            _track_own_genre_tokens,
        )
        return _playlist_identity_tokens, _track_own_genre_tokens

    def test_own_tags_come_from_the_source_columns(self):
        _, own_tokens = self._resolver()

        tokens = own_tokens(LP_SOFT)

        assert "electronic" in tokens
        assert "alternative rock" in tokens
        # ⚠️ The album blend is NOT consulted here — that is the whole point.
        assert "nu metal" not in tokens, (
            "the album blend leaked into the track's own genre set"
        )

    def test_own_tags_decide_when_present(self):
        identity, _ = self._resolver()

        tokens = identity(LP_SOFT, own_tags_win=True)

        assert "nu metal" not in tokens, (
            "the blend's metal tag reached the identity set — rule 2 cannot work"
        )

    def test_a_sparse_track_falls_back_to_the_blend(self):
        identity, _ = self._resolver()

        tokens = identity(SPARSE, own_tags_win=True)

        assert "alternative metal" in tokens

    def test_ignoring_own_tags_unions_both_surfaces(self):
        identity, _ = self._resolver()

        tokens = identity(LP_SOFT, own_tags_win=False)

        assert "nu metal" in tokens and "electronic" in tokens


# ===========================================================================
# 6. Config plumbing (the Config page is the source of truth)
# ===========================================================================


class TestTheConfigPlumbing:
    def test_defaults_apply_with_no_config(self, monkeypatch):
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {})

        rules = ch.get_genre_exclusion_config()

        assert rules["enabled"] is True
        assert rules["own_tags_win"] is True
        assert rules["metal_excluded_from"] == ["alternative rock"]

    def test_a_partial_block_overrides_only_what_it_names(self, monkeypatch):
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {
            "playlists": {"genre_exclusions": {"own_tags_win": False}}
        })

        rules = ch.get_genre_exclusion_config()

        assert rules["own_tags_win"] is False
        assert rules["enabled"] is True
        assert rules["metal_excluded_from"] == ["alternative rock"]

    def test_a_comma_separated_string_is_accepted(self, monkeypatch):
        """The Config page's text input produces a list, but a hand-edited
        config.yaml may hold a plain string."""
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {
            "playlists": {"genre_exclusions": {
                "metal_excluded_from": "Alternative Rock, Indie Rock",
            }}
        })

        rules = ch.get_genre_exclusion_config()

        assert rules["metal_excluded_from"] == ["alternative rock", "indie rock"]

    def test_an_empty_list_survives_as_empty(self, monkeypatch):
        """An empty list is a real choice ("protect nothing") and must not be
        replaced by the default."""
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {
            "playlists": {"genre_exclusions": {"metal_excluded_from": []}}
        })

        assert ch.get_genre_exclusion_config()["metal_excluded_from"] == []

    @pytest.mark.parametrize("tree,page", [
        ("test_site", "Pages/config.html"),
        ("", "pages/config.html"),
    ])
    def test_the_config_page_renders_the_controls(self, tree, page):
        """Config-page contract: an option that is not on the page is not
        configurable."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        html = (root / tree / "templates" / page).read_text(encoding="utf-8")

        for element_id in (
            "playlists_genre_exclusions_enabled",
            "playlists_genre_own_tags_win",
            "playlists_genre_metal_excluded_from",
        ):
            assert f'id="{element_id}"' in html, f"#{element_id} missing from {page}"

    @pytest.mark.parametrize("tree,js", [
        ("test_site", "static/js/pages/config.js"),
        ("", "static/js/config.js"),
    ])
    def test_the_config_page_saves_the_block(self, tree, js):
        """A control the page cannot SAVE is worse than no control."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        script = (root / tree / js).read_text(encoding="utf-8")

        assert "genre_exclusions:" in script, f"{js} does not collect the block"
        for key in ("enabled:", "own_tags_win:", "metal_excluded_from:"):
            assert key in script, f"{js} does not collect {key}"
        # The text input is comma-separated, so the empty case must not yield
        # a list holding one empty string.
        assert "filter((g) => g.length > 0)" in script

    def test_the_nested_block_is_registered_for_saving(self):
        """``_sanitize_config_sections`` coerces any unregistered child to
        ``{}`` — the documented way a nested block silently stops applying."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        source = (root / "routes" / "ui_routes.py").read_text(encoding="utf-8")

        assert '"playlists": ("genre_exclusions",)' in source, (
            "playlists.genre_exclusions is not registered as a nested dict, so "
            "the Config page save would coerce it to {}"
        )

    def test_the_playlist_query_carries_the_per_track_columns(self):
        """The resolver cannot prefer a track's own tags if the query never
        selects them."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        source = (root / "services" / "popularity" / "stages" / "finalise_stage.py").read_text(
            encoding="utf-8"
        )

        sql = source[source.index("_GENRE_ROWS_SQL"):]
        sql = sql[: sql.index('"""', sql.index('"""') + 3)]

        for column in ("lastfm_tags", "musicbrainz_genres", "discogs_genres"):
            assert column in sql, (
                f"the playlist row query does not select {column}, so a "
                "track's own genres are invisible to the rule"
            )
