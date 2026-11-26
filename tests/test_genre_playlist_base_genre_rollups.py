"""Genre playlists: base-genre rollups, and Christmas by base genre.

REQUESTED

1. *"Metal genre should include all metal and metalcore based genres. Punk should
   include all punk based genres such as emo, ska, etc. Rock should include all
   rock based genres such as grunge."*
   → Each track is ALSO pooled into its vocabulary ROOT genre's playlist, in
   addition to the leaf list it already joined. Nothing is removed.

2. *"Christmas playlists should follow the same rules as the covers playlist,
   where they are just created by base genre."*
   → "Christmas Metal - Top Tracks", not "Christmas Nu Metal - Top Tracks".

3. *"All of these playlists should have tracks that are 4* or higher ... Currently
   some 2 and 3 star tracks are being added to them."*
   → The floor is (and stays) 4★; every route into a playlist is floored.

Also fixed while here: ``emo`` and ``ska`` were re-parented under ``punk`` in the
vocabulary (they were ``alternative_rock`` and ``reggae``), and ``iter_genre_tokens``
no longer leaves a slashed compound like "Emo / Screamo" as one unresolvable token.
"""

from __future__ import annotations

import importlib

import pytest

import helpers.config_helpers as config_helpers
from services.playlists import main_genres


@pytest.fixture()
def fs():
    return importlib.import_module("services.popularity.stages.finalise_stage")


def _row(track_id, stars, genres, *, title=None):
    return {
        "id": track_id,
        "title": title or f"Song {track_id}",
        "album": "Album",
        "artist": "A",
        "album_artist": "A",
        "file_path": f"/music/{track_id}.mp3",
        "duration": 180,
        "stars": stars,
        "popularity_score": 50.0,
        "final_score": 50.0,
        "is_live": 0,
        "is_compilation": 0,
        "lastfm_listeners": 1_000_000,
        "listenbrainz_listens": 1_000,
        "genres": genres,
    }


@pytest.fixture()
def builder(fs, monkeypatch):
    """The REAL builder with a stubbed Navidrome, returning what it pushed."""
    state = {"rows": [], "pushed": [], "rollups": True, "min_stars": 4}

    def _cfg():
        return {"playlists": {
            "genre_playlists_enabled": True,
            "genre_playlists_delete_enabled": False,
            "genre_playlists_create_threshold": 1,
            "genre_playlists_delete_threshold": 1,
            "genre_playlists_min_stars": state["min_stars"],
            "genre_playlists_max_genres": 3,
            "genre_playlists_max_tracks": 0,
            "genre_playlists_max_per_artist": 0,
            "playlist_order_mode": "prominence",
            "playlist_interleave_artists": False,
            "exclude_christmas_from_playlists": True,
            "genre_playlists_parent_rollups": state["rollups"],
        }}

    monkeypatch.setattr(config_helpers, "get_config", _cfg)
    monkeypatch.setattr(fs, "_genre_playlists_enabled", lambda: True)
    monkeypatch.setattr(fs, "_genre_playlists_delete_enabled", lambda: False)
    monkeypatch.setattr(fs, "_genre_playlists_active", lambda: True)

    class _Client:
        def fetch_all_playlists(self):
            return []

    monkeypatch.setattr(fs, "_navidrome_clients", lambda: [_Client()])
    monkeypatch.setattr(fs, "_playlist_already_pushed", lambda *a, **k: False)
    monkeypatch.setattr(fs, "_record_playlist_push", lambda *a, **k: None)

    def _sync(name, ids, rows=None, **kw):
        state["pushed"].append((name, list(ids)))
        return {"updated": 1, "created": 0, "unchanged": 0, "failed": 0,
                "skipped": 0, "success": True}

    monkeypatch.setattr(fs, "_sync_playlist_to_navidrome", _sync)
    return state


def _run(fs, builder, monkeypatch, rows):
    builder["rows"] = rows
    monkeypatch.setattr(
        fs, "_fetch_genre_playlist_rows",
        lambda min_stars, genres=None: [dict(r) for r in builder["rows"]],
    )
    fs._create_genre_top_track_playlists()
    return {name: ids for name, ids in builder["pushed"]}


# ---------------------------------------------------------------------------
# The taxonomy itself
# ---------------------------------------------------------------------------

class TestTheVocabularyFoldsSubgenres:
    @pytest.mark.parametrize("genre", [
        "Metal", "Heavy Metal", "Metalcore", "Death Metal", "Nu Metal",
        "Deathcore", "Thrash Metal", "Groove Metal", "Symphonic Metal",
    ])
    def test_metal_based_genres_resolve_to_metal(self, genre):
        assert main_genres.classify_main_genre([genre])["id"] == "metal"

    @pytest.mark.parametrize("genre", [
        "Punk", "Pop Punk", "Hardcore Punk", "Post-Hardcore", "Screamo",
        # ⭐ RE-PARENTED: these were alternative_rock and reggae.
        "Emo", "Ska",
    ])
    def test_punk_based_genres_resolve_to_punk(self, genre):
        assert main_genres.classify_main_genre([genre])["id"] == "punk"

    @pytest.mark.parametrize("genre", [
        "Rock", "Grunge", "Alternative Rock", "Indie Rock", "Hard Rock",
        "Stoner Rock", "Psychedelic Rock", "Shoegaze",
    ])
    def test_rock_based_genres_resolve_to_rock(self, genre):
        assert main_genres.classify_main_genre([genre])["id"] == "rock"

    def test_non_genres_still_do_not_vote(self):
        for token in ("Cover", "Live", "Acoustic", "Male Vocalists"):
            assert main_genres.classify_main_genre([token]) is None


class TestCompoundGenreTokens:
    @pytest.mark.parametrize("value, expected", [
        ("Emo / Screamo", "punk"),
        ("Metalcore / Post-Hardcore", "metal"),
        ("Crust Punk", "punk"),
    ])
    def test_a_slashed_compound_resolves(self, value, expected):
        """A list entry used to stay ONE token and match no vocabulary entry."""
        assert main_genres.classify_main_genre([value])["id"] == expected

    @pytest.mark.parametrize("value, expected", [
        ("death/doom", "metal"),       # a REAL single genre
        ("Drum & Bass", "electronic"),  # ditto
        ("R&B", "r_and_b"),             # ditto
        ("Folk, World, & Country", "folk"),
    ])
    def test_real_single_genres_are_not_torn_apart(self, value, expected):
        """CONTROL: a blind split would destroy these."""
        assert main_genres.classify_main_genre([value])["id"] == expected


# ---------------------------------------------------------------------------
# Parent rollups
# ---------------------------------------------------------------------------

class TestParentRollupPlaylists:
    def test_metalcore_tracks_also_reach_metal(self, fs, builder, monkeypatch):
        lists = _run(fs, builder, monkeypatch, [
            _row("mc4", 4, "Metalcore"),
            _row("nu4", 4, "Nu Metal"),
            _row("dm4", 4, "Death Metal"),
        ])
        assert {"mc4", "nu4", "dm4"} <= set(lists.get("Metal - Top Tracks", [])), lists

    def test_emo_and_ska_reach_punk(self, fs, builder, monkeypatch):
        lists = _run(fs, builder, monkeypatch, [
            _row("emo4", 4, "Emo"),
            _row("ska4", 4, "Ska"),
            _row("pp4", 4, "Pop Punk"),
        ])
        assert {"emo4", "ska4", "pp4"} <= set(lists.get("Punk - Top Tracks", [])), lists

    def test_grunge_reaches_rock(self, fs, builder, monkeypatch):
        lists = _run(fs, builder, monkeypatch, [
            _row("gr4", 4, "Grunge"),
            _row("ar4", 4, "Alternative Rock"),
        ])
        assert {"gr4", "ar4"} <= set(lists.get("Rock - Top Tracks", [])), lists

    def test_the_leaf_playlists_are_still_created(self, fs, builder, monkeypatch):
        """⭐ THE ROLLUP IS ADDITIVE — no leaf list is replaced."""
        lists = _run(fs, builder, monkeypatch, [
            _row("mc4", 4, "Metalcore"),
            _row("gr4", 4, "Grunge"),
        ])
        assert lists.get("Metalcore - Top Tracks") == ["mc4"]
        assert lists.get("Grunge - Top Tracks") == ["gr4"]
        assert "mc4" in lists.get("Metal - Top Tracks", [])

    def test_the_switch_restores_leaf_only_behaviour(self, fs, builder, monkeypatch):
        """CONTROL: ``genre_playlists_parent_rollups=false`` is the old behaviour."""
        builder["rollups"] = False
        lists = _run(fs, builder, monkeypatch, [_row("mc4", 4, "Metalcore")])
        assert "Metalcore - Top Tracks" in lists
        assert "Metal - Top Tracks" not in lists

    @pytest.mark.parametrize("genre", ["Hip-Hop", "R&B"])
    def test_a_multi_word_root_does_not_create_a_duplicate(
        self, fs, builder, monkeypatch, genre
    ):
        """⚠️ ``normalize_main_genre_key`` gives "hip_hop" while a pool key is
        "hip hop"; keying the rollup through the wrong normaliser wrote a SECOND
        pool and therefore a duplicate playlist.

        Inspected on the RAW push list, because ``_run`` returns a dict that would
        silently collapse two writes with the same name.
        """
        _run(fs, builder, monkeypatch, [_row("h1", 4, genre)])
        names = [name for name, _ids in builder["pushed"]]
        assert len(names) == len(set(names)), (
            f"a duplicate playlist was written for {genre!r}: {names}"
        )
        pushes_with_track = [ids for _n, ids in builder["pushed"] if "h1" in ids]
        assert len(pushes_with_track) == 1, (
            f"{genre!r} track reached {len(pushes_with_track)} lists"
        )

    def test_a_root_leaf_pool_is_not_doubled(self, fs, builder, monkeypatch):
        """A track tagged literally "Metal" must land in Metal exactly once."""
        lists = _run(fs, builder, monkeypatch, [
            _row("m1", 4, "Metal"),
            _row("mc1", 4, "Metalcore"),
        ])
        metal = lists.get("Metal - Top Tracks", [])
        assert metal.count("m1") == 1, f"'Metal' track duplicated in the rollup: {metal}"
        assert {"m1", "mc1"} <= set(metal)


# ---------------------------------------------------------------------------
# The 4★ floor
# ---------------------------------------------------------------------------

class TestTheStarFloorHoldsOnEveryRoute:
    def test_three_and_two_star_tracks_reach_no_playlist(
        self, fs, builder, monkeypatch
    ):
        lists = _run(fs, builder, monkeypatch, [
            _row("mc5", 5, "Metalcore"),
            _row("mc4", 4, "Metalcore"),
            _row("mc3", 3, "Metalcore"),
            _row("nu2", 2, "Nu Metal"),
        ])
        pushed = {tid for ids in lists.values() for tid in ids}
        assert "mc3" not in pushed, "a 3★ track reached a playlist"
        assert "nu2" not in pushed, "a 2★ track reached a playlist"
        assert {"mc4", "mc5"} <= pushed

    def test_the_rollup_is_floored_too(self, fs, builder, monkeypatch):
        """The rollup copies pool entries, so it must inherit the floor."""
        lists = _run(fs, builder, monkeypatch, [
            _row("mc4", 4, "Metalcore"),
            _row("mc3", 3, "Metalcore"),
        ])
        assert "mc3" not in set(lists.get("Metal - Top Tracks", []))
        assert "mc4" in set(lists.get("Metal - Top Tracks", []))


# ---------------------------------------------------------------------------
# Christmas by base genre
# ---------------------------------------------------------------------------

class TestChristmasFollowsTheCoversRule:
    def test_seasonal_tracks_group_by_base_genre(self, fs, builder, monkeypatch):
        lists = _run(fs, builder, monkeypatch, [
            _row("xmc", 4, "Christmas, Metalcore", title="Deck the Halls"),
            _row("xnu", 4, "Holiday, Nu Metal", title="Jingle Bells"),
            _row("xgr", 4, "Christmas, Grunge", title="Silent Night"),
        ])
        xmas = {n: ids for n, ids in lists.items() if "hristmas" in n}
        assert xmas.get("Christmas Metal - Top Tracks") is not None, xmas
        assert {"xmc", "xnu"} <= set(xmas["Christmas Metal - Top Tracks"])
        assert xmas.get("Christmas Rock - Top Tracks") is not None
        assert "xgr" in xmas["Christmas Rock - Top Tracks"]

    def test_no_leaf_named_christmas_playlist(self, fs, builder, monkeypatch):
        """The old code produced "Christmas Metalcore - Top Tracks"."""
        lists = _run(fs, builder, monkeypatch, [
            _row("xmc", 4, "Christmas, Metalcore", title="Deck the Halls"),
        ])
        assert not any(
            "hristmas" in n and any(leaf in n for leaf in ("Metalcore", "Nu Metal", "Grunge"))
            for n in lists
        ), lists

    def test_seasonal_tracks_stay_out_of_the_ordinary_rollups(
        self, fs, builder, monkeypatch
    ):
        """⚠️ The Christmas exclusion policy applies to rollups too."""
        lists = _run(fs, builder, monkeypatch, [
            _row("xmc", 4, "Christmas, Metalcore", title="Deck the Halls"),
            _row("plain", 4, "Metalcore", title="Regular Song"),
        ])
        metal = set(lists.get("Metal - Top Tracks", []))
        assert "plain" in metal
        assert "xmc" not in metal, (
            "a Christmas track leaked into the ordinary Metal rollup"
        )

    def test_one_catch_all_list_not_two(self, fs, builder, monkeypatch):
        """ "Holiday - Top Tracks" and "Christmas - Top Tracks" were duplicates."""
        lists = _run(fs, builder, monkeypatch, [
            _row("xbare", 4, "Christmas", title="Little Drummer Boy"),
        ])
        catch_all = [
            n for n in lists
            if n in ("Holiday - Top Tracks", "Christmas - Top Tracks")
        ]
        assert len(catch_all) == 1, f"two catch-all Christmas lists: {catch_all}"


# ---------------------------------------------------------------------------
# The self-check understands rollups (and premium seasonal base pools)
# ---------------------------------------------------------------------------

class TestTheSelfCheckAcceptsARollup:
    def _verify(self, fs, genre, name, row_genres):
        return fs._verify_genre_playlist(
            genre=genre,
            playlist_name=name,
            winners=[{"id": "x", "title": "Song", "stars": 4, "genres": row_genres}],
            max_tracks=1,
            max_per_artist=4,
            eligible_pool_size=1,
            min_stars=4,
        )

    def test_a_subgenre_is_accepted_in_its_root(self, fs):
        assert self._verify(fs, "Metal", "Metal - Top Tracks", ["Metalcore"]) == []

    def test_a_subgenre_is_accepted_in_a_christmas_root(self, fs):
        """ "Christmas Metal" is a base-genre pool, so Metalcore belongs."""
        assert self._verify(
            fs, "Christmas Metal", "Christmas Metal - Top Tracks", ["Metalcore"]
        ) == []

    def test_a_leaf_pool_still_demands_an_exact_match(self, fs):
        """CONTROL: "Metalcore" must NOT accept a "Nu Metal" track."""
        problems = self._verify(
            fs, "Metalcore", "Metalcore - Top Tracks", ["Nu Metal"]
        )
        assert any(p.startswith("GENRE_MISMATCH") for p in problems), problems

    def test_a_christmas_leaf_pool_still_demands_an_exact_match(self, fs):
        """CONTROL: "Christmas Metalcore" must NOT accept a "Nu Metal" track."""
        problems = self._verify(
            fs, "Christmas Metalcore", "Christmas Metalcore - Top Tracks", ["Nu Metal"]
        )
        assert any(p.startswith("GENRE_MISMATCH") for p in problems), problems


# ---------------------------------------------------------------------------
# The base-genre memo must follow the vocabulary
# ---------------------------------------------------------------------------

class TestTheMainGenreMemoTracksTheVocabulary:
    def test_a_vocabulary_reload_drops_the_memo(self, fs):
        """A Config-page vocabulary save must not serve stale answers.

        ``_classify_main`` memoises per VOCABULARY OBJECT, so clearing the
        vocabulary cache (which rebuilds a new object) must discard the memo
        rather than keep answering from the old one.
        """
        from services.enrichment import genre_vocabulary as gv

        fs._MAIN_GENRE_CACHE["nu metal"] = "WRONG"  # poison
        gv.clear_vocabulary_cache()                  # force a new object

        assert fs._classify_main("nu metal", "Nu Metal") == "Metal", (
            "the memo survived a vocabulary reload"
        )

    def test_the_memo_is_reused_within_one_vocabulary(self, fs):
        """CONTROL: a genuine cache hit still returns the cached value."""
        from services.enrichment import genre_vocabulary as gv

        gv.get_vocabulary()  # pin the current object
        fs._classify_main("death metal", "Death Metal")  # warm the memo
        fs._MAIN_GENRE_CACHE["death metal"] = "CACHED"
        assert fs._classify_main("death metal", "Death Metal") == "CACHED"
