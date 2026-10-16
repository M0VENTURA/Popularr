"""Novelty re-recordings must not absorb the original recording's popularity.

The report: *"Alestorm had an album that had a second CD where it has the same
song, but it listed as 'for dogs'. Something in the popularity search is finding
these as the original songs and getting scored as 4 and 5 stars."*

Alestorm's *No Grave but the Sea* deluxe edition ships a second disc in which
every vocal is replaced with dog barks, each track retitled ``"<original> for
Dogs"`` (the German-language track became ``"Bar ünd Imbiss für Hunde"``).  The
novelty takes were treated as ordinary studio tracks and out-scored the real
recordings, taking a GLOBAL 5★ slot.

Three independent defects combined, and each is pinned separately below:

1. **The Last.fm version aggregator summed the original INTO the novelty take.**
   Its guard compared titles with ``fuzzy_match_score``, which is
   ``rapidfuzz.token_set_ratio`` — a metric that returns **1.0 for any strict
   token subset** by design.  So ``"fucked with an anchor"`` scored a PERFECT
   1.0 against ``"fucked with an anchor for dogs"`` and the guard's ``>= 0.90``
   threshold accepted it.  The threshold could never reject the case it was
   written to catch.  84,200 + ~6,000 = the 90,242 the log reported.

2. **The novelty suffix was not a recognised version marker**, so nothing
   downstream knew these were alternate takes at all.

3. **``detect_alternate_takes`` grouped on the bracket-stripped title only**,
   and ``normalize_title_for_lookup`` strips BRACKETED annotations but not
   unbracketed ones — so ``"Mexico (Live)"`` folded into ``"Mexico"`` while
   ``"Mexico for Dogs"`` did not.  The deluxe disc produced the reported
   ``groups_seen=0`` and none of the ten takes was ever linked to its parent.
"""

from __future__ import annotations

import pytest

from services.popularity.popularity_matching import (
    aggregation_keys_compatible,
    normalize_for_aggregation,
)
from services.catalog.album_classification_service import (
    alternate_take_group_key,
    detect_alternate_takes,
    is_alternate_or_bonus_title,
    is_bonus_track_title,
)

MAIN = "Fucked With an Anchor"
DOG = "Fucked With an Anchor for Dogs"
GER_MAIN = "Bar ünd Imbiss"
GER_DOG = "Bar ünd Imbiss für Hunde"


class _FakeLastFm:
    """Returns a fixed search result set, like the real client's search arm."""

    def __init__(self, results):
        self._results = results

    def search_track(self, artist, title, limit=20):
        return list(self._results)

    def get_track_info(self, artist, title, track_mbid=None):
        return {"listeners": 0, "track_play": 0}


# ---------------------------------------------------------------------------
# 1. The aggregator: the base recording must not be summed in
# ---------------------------------------------------------------------------


class TestTheNoveltyTakeDoesNotAbsorbTheOriginal:
    def _aggregate(self, target, results):
        from services.popularity.popularity_sources import (
            get_search_aggregated_lastfm_popularity,
        )

        return get_search_aggregated_lastfm_popularity(
            "Alestorm", target, lastfm_client=_FakeLastFm(results)
        )

    def test_the_base_recording_is_not_summed_in(self):
        """⭐ THE REPORTED BUG, end to end.

        The log line was ``track='Fucked With an Anchor for Dogs'
        listeners=90242`` — 84,200 for the studio original plus ~6,000 for the
        dog take. The novelty take's own listeners are the only ones that
        belong to it.
        """
        agg = self._aggregate(
            DOG,
            [
                {"name": MAIN, "artist": "Alestorm", "listeners": 84200, "url": "u1"},
                {"name": DOG, "artist": "Alestorm", "listeners": 6000, "url": "u2"},
            ],
        )
        assert agg["listeners"] == 6000, (
            f"the original's listeners were summed into the novelty take: {agg['listeners']}"
        )

    def test_the_bleed_is_blocked_in_both_directions(self):
        """The metric is symmetric, so the original absorbed the take too."""
        agg = self._aggregate(
            MAIN,
            [
                {"name": MAIN, "artist": "Alestorm", "listeners": 84200, "url": "u1"},
                {"name": DOG, "artist": "Alestorm", "listeners": 6000, "url": "u2"},
            ],
        )
        assert agg["listeners"] == 84200

    def test_the_german_novelty_title_is_also_blocked(self):
        agg = self._aggregate(
            GER_DOG,
            [
                {"name": GER_MAIN, "artist": "Alestorm", "listeners": 22000, "url": "u1"},
                {"name": GER_DOG, "artist": "Alestorm", "listeners": 3700, "url": "u2"},
            ],
        )
        assert agg["listeners"] == 3700

    def test_its_own_other_spellings_still_aggregate(self):
        """CONTROL: blocking the base must not stop the take summing with itself."""
        agg = self._aggregate(
            DOG,
            [
                {"name": DOG, "artist": "Alestorm", "listeners": 6000, "url": "u1"},
                {"name": DOG, "artist": "Alestorm", "listeners": 500, "url": "u2"},
            ],
        )
        assert agg["listeners"] == 6500


# ---------------------------------------------------------------------------
# 1b. The gate itself
# ---------------------------------------------------------------------------


class TestTheAggregationGate:
    """``aggregation_keys_compatible`` — the rule that replaced the fuzzy test."""

    @staticmethod
    def _same(a: str, b: str) -> bool:
        return aggregation_keys_compatible(
            normalize_for_aggregation(a), normalize_for_aggregation(b)
        )

    @pytest.mark.parametrize(
        "novelty",
        [DOG, GER_DOG, "Mexico for Dogs", "No Grave but the Sea for Dogs"],
    )
    def test_a_novelty_suffix_is_never_the_same_recording(self, novelty):
        assert not self._same(MAIN, novelty)
        assert not self._same(novelty, MAIN)

    def test_a_novelty_take_matches_itself(self):
        assert self._same(DOG, DOG)

    def test_the_fuzzy_metric_alone_would_have_accepted_it(self):
        """⭐ Proof the old guard was structurally unable to catch this.

        ``fuzzy_match_score`` is ``token_set_ratio``, which scores a strict
        token subset at a perfect 1.0.  Any ``>= 0.90`` threshold therefore
        accepts it — the guard was not merely too lax, it could not fire.
        """
        from services.popularity.popularity_math import fuzzy_match_score

        assert fuzzy_match_score(
            normalize_for_aggregation(MAIN), normalize_for_aggregation(DOG)
        ) == 1.0

    @pytest.mark.parametrize(
        "candidate",
        [
            "Herzblut",                                  # identical
            "Herzblut (feat. Melissa Bonny)",             # bracketed feat.
            "Herzblut featuring Melissa Bonny",           # unparenthesised feat.
            "Herzblut feat. Melissa Bonny",
            "Hoppipolla",                                 # diacritic folding
            "Herzblutt",                                  # typo tolerance
        ],
    )
    def test_real_variants_still_aggregate(self, candidate):
        """CONTROL: the gate must not be so strict that it stops summing."""
        base = "Hoppípolla" if candidate == "Hoppipolla" else "Herzblut"
        assert self._same(base, candidate), f"{candidate!r} stopped aggregating"

    def test_a_session_provenance_annotation_still_aggregates(self):
        """⭐ The case a stricter rule broke.

        Last.fm files "100 Words (from Sing the Sorrow sessions 2003)" as a
        separate row from "100 Words". That is the SAME recording with a
        provenance note, and the aggregator exists to sum them — so "the
        candidate has extra words" cannot be the discriminator. Only the KIND
        of extra words is.
        """
        assert self._same("100 Words", "100 Words (from Sing the Sorrow sessions 2003)")

    def test_marker_agreement_is_left_to_the_context_aware_upstream(self):
        """CONTROL: this gate must stay out of the marker question.

        ``title_variants_compatible`` / ``_alt_status_matches_ctx`` decide it
        WITH release-level context — a plainly titled track on an unplugged
        release still matches its unplugged counterpart. A context-free
        duplicate of that logic here rejected exactly that case when tried.
        """
        assert self._same("Farewell", "Farewell [Unplugged Version]")


# ---------------------------------------------------------------------------
# 2. The suffix is a recognised alternate/novelty marker
# ---------------------------------------------------------------------------


class TestTheNoveltySuffixIsRecognised:
    @pytest.mark.parametrize("title", [DOG, GER_DOG, "Mexico for Dogs", "No Grave but the Sea for Dogs"])
    def test_it_is_a_bonus_track(self, title):
        assert is_bonus_track_title(title), f"{title!r} is still an ordinary studio track"

    @pytest.mark.parametrize("title", [DOG, GER_DOG])
    def test_it_is_an_alternate_or_bonus_title(self, title):
        assert is_alternate_or_bonus_title(title)

    @pytest.mark.parametrize("title", [MAIN, GER_MAIN, "Mexico", "No Grave but the Sea"])
    def test_the_originals_are_not_marked(self, title):
        """CONTROL: the originals must stay canonical.

        If the base titles were marked too, ``_persist_alternate_takes`` could
        find no canonical base and the real recordings would be stamped as the
        alternates.
        """
        assert not is_bonus_track_title(title)
        assert not is_alternate_or_bonus_title(title)

    @pytest.mark.parametrize(
        "title",
        ["Song for the Dead", "Waiting for the End", "For Sinners", "Dead to the World"],
    )
    def test_ordinary_prepositional_titles_are_not_caught(self, title):
        """⭐ Why the pattern is the phrase and not a bare ``\\bfor\\b``.

        "for <noun>" is an ordinary song-title shape. Widening the pattern
        would misclassify real album tracks as novelty takes — a worse error
        than the one being fixed, because it would cap and exclude genuine
        recordings.
        """
        assert not is_bonus_track_title(title)


# ---------------------------------------------------------------------------
# 3. The takes are linked to the track they re-record
# ---------------------------------------------------------------------------


class TestTheTakesAreGroupedWithTheirParent:
    def test_the_group_key_folds_the_novelty_suffix(self):
        assert alternate_take_group_key(DOG) == alternate_take_group_key(MAIN)
        assert alternate_take_group_key(GER_DOG) == alternate_take_group_key(GER_MAIN)

    def test_a_title_that_strips_to_nothing_does_not_collapse_the_group(self):
        """⚠️ The guard is load-bearing, and it needs a title that ACTUALLY empties.

        Stripping the whole title leaves ``""`` as the key, and an empty key is
        shared: every track that stripped to nothing would land in ONE group, and
        then be linked as alternate takes of each other and of whichever one
        became the base. The first version of this test used the title "Live",
        which does NOT empty — the trailing patterns all require a leading
        space or dash — so it passed with the guard removed and pinned nothing.

        " - Live" and " - Acoustic" are the shapes that do empty.
        """
        a = alternate_take_group_key(" - Live")
        b = alternate_take_group_key(" - Acoustic")
        assert a and b, (
            f"an emptied key would merge every such track into one group: {a!r}, {b!r}"
        )
        assert a != b, f"two different titles collapsed onto one key: {a!r}"

        # And behaviourally: they must not be reported as alternate takes of
        # one another, which is what a shared empty key would do.
        assert detect_alternate_takes(
            [{"id": 1, "title": " - Live"}, {"id": 2, "title": " - Acoustic"}]
        ) == {}

    def test_the_deluxe_disc_groups_against_disc_one(self):
        """⭐ The reported ``groups_seen=0``."""
        groups = detect_alternate_takes(
            [
                {"id": 1, "title": MAIN},
                {"id": 2, "title": "Mexico"},
                {"id": 3, "title": "To the End of the World"},
                {"id": 11, "title": DOG},
                {"id": 12, "title": "Mexico for Dogs"},
                {"id": 13, "title": "To the End of the World for Dogs"},
            ]
        )
        assert len(groups) == 3, f"expected 3 parent/novelty groups, got {len(groups)}"
        for key, variants in groups.items():
            titles = [v["title"] for v in variants]
            assert len(titles) == 2, f"{key}: {titles}"

    def test_the_canonical_take_sorts_first(self):
        """⚠️ Order decides which track becomes ``base_track_id``.

        ``_persist_alternate_takes`` stamps everything AFTER the base as
        ``alternate_take = 1``, so a novelty take sorted first would mark the
        real studio recording as the alternate — inverting the damage.
        """
        groups = detect_alternate_takes(
            [
                {"id": 11, "title": DOG},
                {"id": 1, "title": MAIN},
            ]
        )
        (variants,) = groups.values()
        assert variants[0]["title"] == MAIN, (
            "the novelty take sorted first, so it would become the base"
        )

    def test_bracketed_variants_still_group(self):
        """CONTROL: the pre-existing behaviour must survive.

        ``normalize_title_for_lookup`` already strips BRACKETS, so these
        grouped before this change and must keep grouping.
        """
        for variant in ("Mexico (Live)", "Mexico (Acoustic Version)", "Mexico - Remastered"):
            groups = detect_alternate_takes(
                [{"id": 1, "title": "Mexico"}, {"id": 2, "title": variant}]
            )
            assert len(groups) == 1, f"{variant!r} stopped grouping"

    def test_unrelated_tracks_do_not_group(self):
        groups = detect_alternate_takes(
            [{"id": 1, "title": "Mexico"}, {"id": 2, "title": "Something Else Entirely"}]
        )
        assert groups == {}


# ---------------------------------------------------------------------------
# 3b. The persistence picks the canonical base
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Captures the UPDATE parameters instead of touching a database."""

    def __init__(self):
        self.updates: list[tuple[str, str]] = []

    def execute(self, statement, params):
        self.updates.append((params["base_id"], params["alternate_id"]))

        class _Result:
            rowcount = 1

        return _Result()

    def begin_nested(self):
        """``_row_savepoint`` wraps each UPDATE in a SAVEPOINT."""
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestThePersistedBaseIsTheCanonicalTake:
    """⚠️ Must be the UNMARKED variant, not simply the first one.

    ``detect_alternate_takes`` does sort the canonical take first, but relying
    on that silently is a cross-module contract: get it wrong and the novelty
    take becomes the base while the real studio recording is stamped
    ``alternate_take = 1`` — excluded from the album's stats baseline and
    inheriting the novelty take's (inflated) popularity.
    """

    def _run(self, monkeypatch, variants):
        from services.popularity.stages import album_stage

        session = _RecordingSession()
        monkeypatch.setattr(album_stage, "db_session", lambda: session)
        album_stage._persist_alternate_takes({"alternate_takes": {"x": variants}})
        return session.updates

    def test_the_base_is_the_take_without_a_marker(self, monkeypatch):
        # Deliberately NOT canonical-first, so a `variants[0]` implementation
        # would pick the novelty take and this test would fail.
        updates = self._run(
            monkeypatch,
            [{"id": 11, "title": DOG}, {"id": 1, "title": MAIN}],
        )
        assert updates == [("1", "11")], (
            f"expected base=1 (the studio original), got {updates}"
        )

    def test_the_german_novelty_take_is_also_an_alternate(self, monkeypatch):
        updates = self._run(
            monkeypatch,
            [{"id": 21, "title": GER_DOG}, {"id": 2, "title": GER_MAIN}],
        )
        assert updates == [("2", "21")]

    def test_a_bracketed_alternate_still_uses_the_plain_base(self, monkeypatch):
        updates = self._run(
            monkeypatch,
            [{"id": 31, "title": "Mexico (Live)"}, {"id": 3, "title": "Mexico"}],
        )
        assert updates == [("3", "31")]
