"""Genre FAMILY tie-break — deciding "is this track metal?" from the vocabulary.

THE REPORT
----------
Audioslave was being kept OUT of the Alternative Rock genre playlist:

    Genre exclusion — 1834 metal track(s) kept out of 1 playlist(s): alternative rock: 1834

with the note *"Metal was kept out even though the rest of the sections were
rock based."* The requested rule:

    "Use the third or fourth genre as a tie breaker. Grunge is usually more
     aligned to rock than alt metal. Where Nu Metal is more closer to metal."

WHY THE OLD TEST COULD NOT DO IT
--------------------------------
``is_metal_genre`` asks whether the letters "metal" appear in ANY token. Both
bands carry a metal tag, so both were excluded:

    Audioslave: alternative_rock(rock) hard_rock(rock) grunge(rock) alternative_metal(metal)
    Slipknot:   nu_metal(metal) alternative_metal(metal) heavy_metal(metal) alternative_rock(rock)
    families present: ['metal', 'rock']   ← IDENTICAL for both

No set-membership test can separate those. The ORDER can, which is why the rule
ranks by specificity then position and lets the first deciding genre win.

The vocabulary already encoded the musical judgement — ``grunge`` resolves to
family ``rock`` and ``nu_metal`` to ``metal`` — so this wires up data that
already existed rather than inventing a new taxonomy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.enrichment.genre_vocabulary import get_vocabulary
from services.popularity.stages import finalise_stage as fs

REPO_ROOT = Path(__file__).resolve().parent.parent
TS = REPO_ROOT / "test_site"


# ---------------------------------------------------------------------------
# 1. The vocabulary answers the question
# ---------------------------------------------------------------------------


class TestTheVocabularyEncodesTheRule:
    """The data existed before this change; these pin that it still does."""

    @pytest.mark.parametrize("genre,family", [
        ("grunge", "rock"),
        ("post_grunge", "rock"),
        ("alternative_rock", "rock"),
        ("hard_rock", "rock"),
        ("indie_rock", "rock"),
        ("nu_metal", "metal"),
        ("alternative_metal", "metal"),
        ("heavy_metal", "metal"),
        ("death_metal", "metal"),
        ("sludge_metal", "metal"),
    ])
    def test_the_family_of_each_reported_genre(self, genre: str, family: str) -> None:
        vocab = get_vocabulary()
        entry = vocab.genres.get(genre)
        assert entry is not None, f"{genre} left the vocabulary"
        assert entry.family == family, (
            f"{genre} is filed under {entry.family!r}, not {family!r} — the "
            "tie-break's musical judgement is carried by this data"
        )

    def test_metal_is_its_own_family_despite_descending_from_rock(self) -> None:
        """The reason ``family`` is DECLARED rather than derived from the root.

        metal -> rock, so a root-derived family would file every metal genre as
        rock and the family limit would cap them together.
        """
        vocab = get_vocabulary()
        metal = vocab.genres["metal"]
        assert "rock" in metal.ancestors(vocab.genres), "metal no longer sits under rock"
        assert metal.family == "metal"

    def test_metalcore_is_metal(self) -> None:
        """Re-parented on purpose: it descends from hardcore punk, so its
        family resolved to ``punk`` while the substring test called it metal —
        two systems disagreeing about one genre."""
        vocab = get_vocabulary()
        metalcore = vocab.genres["metalcore"]
        assert metalcore.family == "metal"
        assert "hardcore_punk" in metalcore.parents, (
            "the punk lineage was dropped rather than kept alongside metal"
        )
        assert "metal" in metalcore.parents

    def test_the_metalcore_children_follow(self) -> None:
        """CONTROL: re-parenting must not have orphaned them."""
        vocab = get_vocabulary()
        for child in ("deathcore", "mathcore"):
            assert vocab.genres[child].family == "metal", f"{child} did not follow"


# ---------------------------------------------------------------------------
# 2. family_of() must include the entry's OWN family
# ---------------------------------------------------------------------------


class TestFamilyOfIncludesSelf:
    """A latent bug found while building this.

    ``family_of`` walked only the ANCESTORS, so calling it directly on ``metal``
    returned "rock" — the opposite of the branch it names. Production was
    accidentally safe because the one call site is
    ``entry.family or entry.family_of(...)`` and metal declares its own, so the
    ``or`` short-circuited.
    """

    @pytest.mark.parametrize("genre", ["metal", "rock", "punk", "electronica"] if False else
                             ["metal", "rock", "punk", "pop", "electronic"])
    def test_a_root_returns_its_own_family(self, genre: str) -> None:
        vocab = get_vocabulary()
        entry = vocab.genres.get(genre)
        if entry is None:
            pytest.skip(f"{genre} is not in the vocabulary")
        assert entry.family_of(vocab.genres) == entry.family

    def test_metal_specifically_does_not_report_rock(self) -> None:
        """The exact wrong answer, named."""
        vocab = get_vocabulary()
        assert vocab.genres["metal"].family_of(vocab.genres) == "metal", (
            "family_of() is walking past the entry's own declared family again"
        )

    def test_an_inherited_family_still_works(self) -> None:
        """CONTROL: the self-check must not break inheritance for a genre that
        declares nothing of its own."""
        vocab = get_vocabulary()
        grunge = vocab.genres["grunge"]
        assert grunge.family_of(vocab.genres) == "rock"


# ---------------------------------------------------------------------------
# 3. The decider
# ---------------------------------------------------------------------------


class TestTheDecidingFamily:

    @pytest.mark.parametrize("tokens,expected", [
        # ⭐ THE REPORT: most specific tie at 2, grunge is earlier than
        # alternative metal, so rock wins.
        (["alternative rock", "hard rock", "grunge", "alternative metal"], "rock"),
        # Same shape, opposite order: nu metal and alternative metal are BOTH
        # metal, so the first one decides.
        (["nu metal", "alternative metal", "heavy metal", "alternative rock"], "metal"),
        (["progressive metal", "alternative metal", "alternative rock"], "metal"),
        (["alternative rock", "post-grunge"], "rock"),
        (["alternative rock", "electronic", "pop rock"], "rock"),
        (["nu metal", "alternative metal"], "metal"),
        (["alternative rock", "indie rock"], "rock"),
        # Specificity beats position: "metal" is generic (0) and loses to
        # "hard rock" (1) even though it comes first.
        (["metal", "hard rock"], "rock"),
    ])
    def test_the_decider(self, tokens, expected) -> None:
        assert get_vocabulary().family_decider(tokens) == expected

    def test_specificity_outranks_position(self) -> None:
        """The ordering rule is (specificity, then position) — not position alone."""
        vocab = get_vocabulary()
        assert vocab.genres["metal"].specificity < vocab.genres["hard_rock"].specificity
        assert vocab.family_decider(["metal", "hard rock"]) == "rock"

    def test_order_breaks_a_specificity_tie(self) -> None:
        """The user's own rule, isolated: same specificity, order decides."""
        vocab = get_vocabulary()
        assert vocab.genres["grunge"].specificity == vocab.genres["alternative_metal"].specificity
        assert vocab.family_decider(["grunge", "alternative metal"]) == "rock"
        assert vocab.family_decider(["alternative metal", "grunge"]) == "metal", (
            "reversing the order must reverse the verdict — otherwise position "
            "is not actually the tie-break"
        )

    @pytest.mark.parametrize("tokens", [[], None, ["Holiday Rock"], ["NOT A GENRE AT ALL"]])
    def test_an_unknown_set_decides_nothing(self, tokens) -> None:
        """⚠️ "" and not "rock": the caller must FALL BACK, not be told
        "not metal" by a vocabulary that simply does not know the token."""
        assert get_vocabulary().family_decider(tokens) == ""

    def test_it_reports_the_tokens_it_could_not_judge(self) -> None:
        """⭐ The second half of the answer, and the caller's fallback input.

        ``_track_is_metal_by_identity`` uses the family for what it RECOGNISED
        and only the leftover tokens for the substring test. If the unknowns
        came back empty the fallback would be dead, and a brand-new subgenre
        spelling would silently stop being metal.
        """
        vocab = get_vocabulary()
        family, unknown = vocab.deciding_family(
            ["grunge", "Brand New -Corpsegrinder Metal"]
        )
        assert family == "rock"
        # ⚠️ Compared NORMALISED: `iter_genre_tokens` lower-cases and keeps
        # hyphens, so the reported token is the parser's output, not the input
        # spelling. Asserting the input spelling would be asserting the wrong
        # contract.
        assert unknown == ["brand new -corpsegrinder metal"], (
            "the unrecognised token must be reported, or the fallback has "
            "nothing to test"
        )

        # And a fully-recognised set reports nothing unknown, so the fallback
        # cannot re-litigate genres the vocabulary already judged.
        family, unknown = vocab.deciding_family(
            ["alternative rock", "hard rock", "grunge", "alternative metal"]
        )
        assert family == "rock"
        assert unknown == [], (
            "alternative metal is IN the vocabulary, so it must not be handed "
            "back as an unknown token for the substring test to re-examine — "
            "that is exactly how the reported bug came back"
        )


# ---------------------------------------------------------------------------
# 4. The wiring, honouring the config toggle
# ---------------------------------------------------------------------------


class TestTheToggle:
    """``_track_is_metal_by_identity`` is the seam the builder calls."""

    AUDIOSLAVE = ["alternative rock", "hard rock", "grunge", "alternative metal"]
    SLIPKNOT = ["nu metal", "alternative metal", "heavy metal", "alternative rock"]

    def test_the_family_rule_lets_audioslave_through(self) -> None:
        assert fs._track_is_metal_by_identity(
            self.AUDIOSLAVE, use_family_tiebreak=True
        ) is False, "the reported track is still excluded"

    def test_the_family_rule_still_catches_slipknot(self) -> None:
        """CONTROL: the fix must not be 'stop excluding anything'."""
        assert fs._track_is_metal_by_identity(
            self.SLIPKNOT, use_family_tiebreak=True
        ) is True

    def test_turning_it_off_restores_the_old_answer(self) -> None:
        """⭐ The config toggle's whole purpose: a way back."""
        assert fs._track_is_metal_by_identity(
            self.AUDIOSLAVE, use_family_tiebreak=False
        ) is True, (
            "with the family tie-break off this must reproduce the substring "
            "test's answer, or the toggle does not revert the behaviour"
        )

    def test_both_modes_agree_on_a_plainly_metal_set(self) -> None:
        """CONTROL: the toggle must not change a case nobody disputes."""
        for mode in (True, False):
            assert fs._track_is_metal_by_identity(["death metal"], use_family_tiebreak=mode)

    def test_both_modes_agree_on_a_plainly_rock_set(self) -> None:
        for mode in (True, False):
            assert not fs._track_is_metal_by_identity(
                ["alternative rock", "indie rock"], use_family_tiebreak=mode
            )

    def test_an_unknown_genre_falls_back_to_the_substring_test(self) -> None:
        """The original rationale for a substring test was that the subgenre
        vocabulary is unbounded. A token the vocabulary does not know must
        still be caught, in BOTH modes."""
        for mode in (True, False):
            assert fs._track_is_metal_by_identity(
                ["Somethingcore Metal (New)"], use_family_tiebreak=mode
            ), "a brand-new metal spelling stopped being recognised"

    def test_an_empty_identity_is_never_metal(self) -> None:
        for mode in (True, False):
            assert not fs._track_is_metal_by_identity([], use_family_tiebreak=mode)

    def test_the_family_check_is_used_before_the_substring_test(self) -> None:
        """Order matters: the family answer must be able to say NO.

        If the substring test ran first it would find "metal" in Audioslave's
        tags and never consult the family at all.
        """
        source = Path(fs.__file__).read_text(encoding="utf-8")
        idx = source.index("def _track_is_metal_by_identity(")
        body = source[idx: source.index("\n    return is_metal_genre(tokens)", idx)]
        assert "family_decider" in body
        assert body.index("family_decider") < body.index("METAL_GENRE_FAMILY") + 1


# ---------------------------------------------------------------------------
# 5. End to end — the real playlist builder
# ---------------------------------------------------------------------------

#: The album-blend string ``sync_album_file_tags`` would fan out to every track.
AUDIOSLAVE_BLEND = "alternative rock, hard rock, grunge, alternative metal"

ROWS = [
    {
        "id": "as-like-a-stone", "title": "Like a Stone", "album": "Audioslave",
        "artist": "Audioslave", "album_artist": "Audioslave", "stars": 5,
        "popularity_score": 90.0, "is_live": 0, "is_compilation": 0, "duration": 300,
        "lastfm_listeners": 900_000, "listenbrainz_listens": 400_000,
        "genres": AUDIOSLAVE_BLEND,
        # ⭐ The reported shape: recording tags are rock-leaning, and the
        # artist-scoped columns carry the metal token.
        "musicbrainz_genres": '["alternative rock", "hard rock", "grunge"]',
        "lastfm_tags": '["alternative rock"]',
        "audiodb_genres": '["alternative metal", "hard rock"]',
    },
    {
        "id": "as-cochise", "title": "Cochise", "album": "Audioslave",
        "artist": "Audioslave", "album_artist": "Audioslave", "stars": 5,
        "popularity_score": 88.0, "is_live": 0, "is_compilation": 0, "duration": 220,
        "lastfm_listeners": 800_000, "listenbrainz_listens": 350_000,
        "genres": AUDIOSLAVE_BLEND,
        "musicbrainz_genres": '["hard rock", "alternative rock"]',
        "lastfm_tags": '["hard rock"]',
        # ⭐ Artist-scoped columns carry the ARTIST's whole genre set, so they
        # are the same on every track of the artist — including the grunge that
        # is what makes this a rock act. Modelling them per-track would invent
        # a band whose tracks disagree about the band.
        "audiodb_genres": '["grunge", "alternative metal", "hard rock"]',
    },
    {
        "id": "sk-duality", "title": "Duality", "album": "Vol. 3",
        "artist": "Slipknot", "album_artist": "Slipknot", "stars": 5,
        "popularity_score": 87.0, "is_live": 0, "is_compilation": 0, "duration": 250,
        "lastfm_listeners": 850_000, "listenbrainz_listens": 380_000,
        "genres": "nu metal, alternative metal, alternative rock",
        # ⭐ Led by a metal genre at the SAME specificity as grunge.
        "musicbrainz_genres": '["nu metal", "alternative metal"]',
        "lastfm_tags": '["nu metal"]',
    },
]

TITLES = {row["id"]: row["title"] for row in ROWS}
ALT_ROCK = "Alternative Rock - Top Tracks"
ALT_METAL = "Alternative Metal - Top Tracks"
NU_METAL = "Nu Metal - Top Tracks"


@pytest.fixture
def build(monkeypatch):
    """Run the REAL builder over the reported roster."""
    import helpers.config_helpers as config_helpers

    def _run(*, use_family_tiebreak=True):
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
                    "enabled": True,
                    "own_tags_win": True,
                    "use_family_tiebreak": use_family_tiebreak,
                    "metal_excluded_from": ["alternative rock"],
                },
            }
        })

        fs._create_genre_top_track_playlists()
        return captured

    _run.titles = lambda captured, name: [
        TITLES[i] for i in captured.get(name, [])
    ]
    return _run


class TestTheReportedRoster:
    """The real builder over the real reported shape."""

    def test_audioslave_is_back_in_the_rock_playlist(self, build) -> None:
        rock = build.titles(build(), ALT_ROCK)
        assert "Like a Stone" in rock, (
            "Audioslave is still kept out of Alternative Rock — the reported bug"
        )
        assert "Cochise" in rock

    def test_slipknot_is_still_kept_out(self, build) -> None:
        """CONTROL: the case the exclusion exists for."""
        rock = build.titles(build(), ALT_ROCK)
        assert "Duality" not in rock, (
            "the exclusion stopped working altogether — this is the takeover "
            "the rule was introduced to prevent"
        )

    def test_slipknot_still_reaches_a_metal_playlist(self, build) -> None:
        """CONTROL: excluded from rock must not mean excluded from everywhere."""
        captured = build()
        metal_names = [n for n in captured if "metal" in n.lower()]
        assert metal_names, "no metal playlist was produced at all"
        assert any(
            "Duality" in build.titles(captured, n) for n in metal_names
        ), "Slipknot's metal track vanished entirely"

    def test_turning_the_toggle_off_reproduces_the_old_behaviour(self, build) -> None:
        """⭐ The whole roster, both ways — the strongest statement of the toggle.

        With it OFF the reported bug must come back exactly; if it does not, the
        toggle is not actually selecting between the two rules.
        """
        rock_on = build.titles(build(use_family_tiebreak=True), ALT_ROCK)
        rock_off = build.titles(build(use_family_tiebreak=False), ALT_ROCK)

        assert "Like a Stone" in rock_on
        assert "Like a Stone" not in rock_off, (
            "with the family tie-break off the old substring test must exclude "
            "Audioslave again — otherwise the toggle changes nothing"
        )
        # And the disputed track is unaffected either way.
        assert "Duality" not in rock_on
        assert "Duality" not in rock_off

    def test_the_rule_is_per_track_and_that_is_intended(self) -> None:
        """⚠️ A CONSEQUENCE worth naming: two tracks by one artist can separate.

        The rule is applied to a TRACK's own genre list, so whether it is metal
        depends on the most specific genre THAT TRACK carries. A track whose
        most specific genre is metal is metal even if its band is not — here
        Cochise modelled WITHOUT the artist-scoped grunge, which is the shape a
        sparse per-track column produces.

        This is not a defect to fix: the user's rule is explicitly about the
        order of a track's genres, and the alternative (a per-artist verdict)
        would put the artist-scoped columns in charge of every track — the
        album-blend problem this whole area exists to avoid. In practice the
        artist-scoped columns (audiodb, wikidata) carry the same set for every
        track of an artist, so divergence needs a per-track column to disagree.
        """
        without_artist_genres = ["hard rock", "alternative rock", "alternative metal"]
        with_artist_genres = ["hard rock", "alternative rock", "grunge", "alternative metal"]

        assert fs._track_is_metal_by_identity(
            without_artist_genres, use_family_tiebreak=True
        ) is True, (
            "alternative metal (specificity 2) outranks hard rock (1) when "
            "nothing ties with it"
        )
        assert fs._track_is_metal_by_identity(
            with_artist_genres, use_family_tiebreak=True
        ) is False, (
            "grunge ties at specificity 2 and is earlier, so rock wins — the "
            "artist-scoped genre set is what keeps a band's tracks together"
        )


# ---------------------------------------------------------------------------
# 6. The Config page contract
# ---------------------------------------------------------------------------


class TestTheConfigPageExposesIt:
    """§4.1 of the project rules: every user-editable setting appears on the
    Config page. A behaviour with no control is a behaviour nobody can turn off.
    """

    @pytest.mark.parametrize("rel", [
        "test_site/templates/Pages/config.html",
        "templates/pages/config.html",
    ])
    def test_both_config_pages_render_the_toggle(self, rel: str) -> None:
        html = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert 'id="playlists_genre_use_family_tiebreak"' in html, (
            f"{rel}: the family tie-break has no control"
        )
        # It must be in the Genre Exclusions block, not merely present.
        idx = html.index('id="playlists_genre_use_family_tiebreak"')
        window = html[max(0, idx - 2500): idx]
        assert 'id="playlists_genre_metal_excluded_from"' in window, (
            f"{rel}: the toggle is outside the Genre Exclusions card"
        )

    @pytest.mark.parametrize("rel", [
        "test_site/static/js/pages/config.js",
        "static/js/config.js",
    ])
    def test_both_config_scripts_save_it(self, rel: str) -> None:
        """A rendered control that the save path ignores is worse than none:
        the user toggles it, saves, and nothing changes."""
        js = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "use_family_tiebreak: getChecked('playlists_genre_use_family_tiebreak'" in js, (
            f"{rel}: the toggle is not collected, so saving silently drops it"
        )

    @pytest.mark.parametrize("rel", [
        "test_site/templates/Pages/config.html",
        "templates/pages/config.html",
    ])
    def test_the_help_text_describes_the_real_rule(self, rel: str) -> None:
        """The copy must not promise something the code does not do.

        The previous version of the neighbouring field said a track carrying any
        ``*metal*`` tag is excluded — which stopped being true with this change.
        """
        html = (REPO_ROOT / rel).read_text(encoding="utf-8")
        idx = html.index('id="playlists_genre_metal_excluded_from"')
        window = html[idx: idx + 900]
        assert "*metal*" not in window, (
            f"{rel}: the help text still describes the substring rule"
        )


class TestTheConfigGetter:
    def test_it_defaults_on(self, monkeypatch) -> None:
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {"playlists": {}})
        assert ch.get_genre_exclusion_config()["use_family_tiebreak"] is True

    def test_an_explicit_off_survives(self, monkeypatch) -> None:
        import helpers.config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {
            "playlists": {"genre_exclusions": {"use_family_tiebreak": False}}
        })
        assert ch.get_genre_exclusion_config()["use_family_tiebreak"] is False


# ---------------------------------------------------------------------------
# 7. The refresh/pooling mismatch
# ---------------------------------------------------------------------------


class TestTheRefreshAgreesWithTheBuilder:
    """A second bug found while wiring this up.

    ``refresh_genre_playlists_for_album`` computed its ``affected`` genre set
    with ``_resolve_playlist_genres`` (own-tags-first) while the builder pooled
    by ``_genre_playlist_track_genres`` (the album blend): two different answers
    to "which playlists does this album touch", on the one path whose entire job
    is keeping them in step.

    ⭐ CONTRACT REVERSAL (2026-10-26). Both now resolve from the track's FINAL
    ``genres`` value — the three applied musical genres — so the two answers are
    the same BY CONSTRUCTION rather than by coincidence. The previous control
    here asserted that they DISAGREED on a sample row, which encoded the defect
    as a property; it is inverted below.

    The reason the pooling source had to change: the old resolution read the
    per-PROVIDER columns, which keep whatever the provider returned, so a genre
    removed from a track's metadata survived in ``lastfm_tags`` and kept the
    track in a playlist Navidrome said it was not in ("it was adjusted to remove
    Alternative Metal, but it's still on the Alternative Metal Playlist").
    """

    def test_the_refresh_uses_the_pooling_resolver(self) -> None:
        source = Path(fs.__file__).read_text(encoding="utf-8")
        idx = source.index("def refresh_genre_playlists_for_album(")
        body = source[idx: source.index("\ndef ", idx + 10)]
        assert "_resolve_playlist_genres(row" in body, (
            "the refresh is back on a resolver the builder does not pool by"
        )

    def test_the_builder_pools_through_the_same_resolver(self) -> None:
        """The other half: the builder's ``_track_genres`` must use it too."""
        source = Path(fs.__file__).read_text(encoding="utf-8")
        idx = source.index("    def _track_genres(row")
        body = source[idx: source.index("\n    try:", idx)]
        assert "_resolve_playlist_genres(" in body

    def test_the_two_resolvers_now_agree(self) -> None:
        """⭐ THE INVERTED CONTROL (was ``test_the_two_resolvers_actually_differ``).

        Previously: "if they agreed, the mismatch could not have mattered" —
        and they DID disagree, which is what made the pairing a bug. They now
        agree because both read the final ``genres`` value, so the invariant is
        that no row can produce two different pool sets.
        """
        rows = [
            {
                "genres": "alternative rock, nu metal",
                "musicbrainz_genres": '["nu metal"]',
                "lastfm_tags": '["nu metal"]',
            },
            {
                # The reported case: the providers still say metal, the track no
                # longer does.
                "genres": "pop punk, alternative rock",
                "musicbrainz_genres": '["alternative metal"]',
                "discogs_genres": '["alternative metal"]',
            },
        ]
        for row in rows:
            blended = set(fs._genre_playlist_track_genres(row, max_genres=3))
            resolved, _owned = fs._resolve_playlist_genres(row, max_genres=3)
            assert {g.casefold() for g in blended} == {g.casefold() for g in resolved}, (
                f"the two resolvers disagree on {row!r} — the refresh and the "
                "builder would update different playlists"
            )

    def test_the_blend_helper_is_only_the_no_final_value_case(self) -> None:
        """The one place the two answers legitimately differ, and why.

        ``_genre_playlist_track_genres`` reads ONLY ``genres``; when that is
        empty it returns nothing. ``_resolve_playlist_genres`` falls back to the
        track's own source columns first, so a track that has never been
        aggregated is not silently dropped from every playlist. That is a
        deliberate asymmetry, not drift — the refresh and the builder both call
        ``_resolve_playlist_genres``, so they still agree with each other.
        """
        row = {"genres": "", "musicbrainz_genres": '["nu metal"]'}
        assert fs._genre_playlist_track_genres(row, max_genres=3) == []
        resolved, owned = fs._resolve_playlist_genres(row, max_genres=3)
        assert owned is True
        assert [g.casefold() for g in resolved] == ["nu metal"]

    def test_a_removed_genre_is_not_resurrected_by_a_provider_column(self) -> None:
        """The behaviour the agreement exists to protect."""
        row = {
            "genres": "pop punk, alternative rock",
            "musicbrainz_genres": '["alternative metal"]',
            "lastfm_tags": '["alternative metal"]',
        }
        resolved, _owned = fs._resolve_playlist_genres(row, max_genres=3)
        assert "alternative metal" not in {g.casefold() for g in resolved}
