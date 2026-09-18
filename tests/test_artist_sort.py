"""Tests for artist name filing ("The Offspring" → "Offspring, The").

The load-bearing property here is that the SQL expression and the Python
functions file a name EXACTLY the same way.  They are used side by side — the
``/artists`` route orders in SQL and re-sorts in Python, and the letter-tile
scan matches in SQL while the section letters come from Python — so any
divergence shows up as an artist appearing in one place and not another.

That is not hypothetical: an earlier draft used ``SUBSTR(x, 5)`` without a
``TRIM``, which left a leading space on ``"The  Two Spaces"`` (two spaces after
the article) and filed it ahead of every letter, and used ``\\s+`` in the regex
where SQL's ``LIKE 'the %'`` can only express a space.  ``TestSqlMatchesPython``
and ``TestWhitespaceParity`` exist to keep both classes of bug out.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from helpers.artist_sort import (
    ARTICLES,
    artist_sort_key,
    artist_sort_letter,
    artist_sort_letter_sql,
    artist_sort_name,
    artist_sort_sql,
    artist_starts_with_letter_sql,
    split_leading_article,
)


def _sqlite_engine():
    """In-memory engine — the unit-test target, and what Alembic runs against."""
    return create_engine("sqlite:///:memory:")


def _order_by_artist_sort(names: list[str]) -> list[str]:
    """File *names* using the SQL expression, the way the routes do."""
    engine = _sqlite_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (artist TEXT)"))
        conn.execute(
            text("INSERT INTO t (artist) VALUES (:a)"),
            [{"a": n} for n in names],
        )
        expr = artist_sort_sql("artist")
        rows = conn.execute(text(f"SELECT artist FROM t ORDER BY {expr}")).fetchall()
    return [r[0] for r in rows]


def _sql_letter(name: str) -> str:
    """Section letter as SQL computes it, normalised the way callers do."""
    engine = _sqlite_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (artist TEXT)"))
        conn.execute(text("INSERT INTO t (artist) VALUES (:a)"), {"a": name})
        expr = artist_sort_letter_sql("artist")
        got = conn.execute(text(f"SELECT {expr} FROM t")).scalar()
    return got if got and "A" <= got <= "Z" else "#"


class TestSplitLeadingArticle:
    def test_moves_the_article(self):
        assert split_leading_article("The Offspring") == ("Offspring", "The")

    def test_keeps_original_casing_of_the_core(self):
        assert split_leading_article("The OffSpring") == ("OffSpring", "The")

    def test_strips_surrounding_whitespace(self):
        assert split_leading_article("  The Cure  ") == ("Cure", "The")

    def test_tolerates_repeated_spaces(self):
        # The core must not keep a leading space — that is what filed
        # "The  Two Spaces" ahead of every letter.
        core, article = split_leading_article("The  Two Spaces")
        assert (core, article) == ("Two Spaces", "The")
        assert not core.startswith(" ")

    @pytest.mark.parametrize(
        "name",
        [
            "Theatre of Tragedy",  # 'the' is a prefix but not an article
            "Theodore",
            "The",  # nothing to file under
            "A Perfect Circle",  # A/An are deliberately NOT moved
            "An Ocean",
            "Radiohead",
            "Ac/Dc",
        ],
    )
    def test_leaves_non_article_names_alone(self, name):
        assert split_leading_article(name) == (name, "")

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_handles_empty_input(self, value):
        assert split_leading_article(value) == ("", "")


class TestSortKey:
    def test_offspring_files_under_o(self):
        assert artist_sort_key("The Offspring") == "offspring, the"

    def test_pretty_reckless_files_under_p(self):
        assert artist_sort_key("The Pretty Reckless") == "pretty reckless, the"

    def test_plain_name_is_just_lowercased(self):
        assert artist_sort_key("Radiohead") == "radiohead"

    def test_is_case_insensitive(self):
        assert artist_sort_key("THE OFFSPRING") == artist_sort_key("the offspring")

    def test_sorts_among_its_letter_not_t(self):
        ordered = sorted(["The Offspring", "Radiohead", "Zebrahead"], key=artist_sort_key)
        assert ordered == ["The Offspring", "Radiohead", "Zebrahead"]

    def test_plain_name_precedes_its_the_variant(self):
        # "Offspring" then "Offspring, The" — deterministic, and the plain name
        # is the one a user is more likely to mean.
        ordered = sorted(["The Offspring", "Offspring"], key=artist_sort_key)
        assert ordered == ["Offspring", "The Offspring"]


class TestSortName:
    def test_inverts_the_article(self):
        assert artist_sort_name("The Offspring") == "Offspring, The"

    def test_inverts_the_pretty_reckless(self):
        assert artist_sort_name("The Pretty Reckless") == "Pretty Reckless, The"

    @pytest.mark.parametrize(
        "name", ["Radiohead", "Theatre of Tragedy", "Theodore", "The", "A Perfect Circle"]
    )
    def test_leaves_other_names_unchanged(self, name):
        assert artist_sort_name(name) == name

    def test_repeated_the_collapses_cleanly(self):
        assert artist_sort_name("The The") == "The, The"

    def test_only_the_is_an_article(self):
        assert ARTICLES == ("The",)


class TestSortLetter:
    def test_uses_the_core_name(self):
        assert artist_sort_letter("The Offspring") == "O"
        assert artist_sort_letter("The Pretty Reckless") == "P"
        assert artist_sort_letter("The Cure") == "C"

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Radiohead", "R"),
            ("Theatre of Tragedy", "T"),
            ("Theodore", "T"),
            ("The", "T"),
            ("!!!", "#"),
            ("宇多田ヒカル", "#"),
        ],
    )
    def test_letter_or_hash(self, name, expected):
        assert artist_sort_letter(name) == expected

    def test_is_ascii_only_so_the_hash_bucket_is_reachable(self):
        # Unicode-aware isalpha() would yield "宇", which has no tile in the
        # jump picker and no matching SQL predicate.
        assert artist_sort_letter("宇多田ヒカル") == "#"


class TestSqlMatchesPython:
    """The SQL expression must file names exactly as the Python key does."""

    NAMES = [
        "The Offspring",
        "Radiohead",
        "The Cure",
        "Theatre of Tragedy",
        "The Pretty Reckless",
        "A Perfect Circle",
        "Zebrahead",
        "Theodore",
        "The",
        "Ac/Dc",
        "The Aces",
        "Aces High",
        "!!!",
    ]

    def test_ordering_is_identical(self):
        assert _order_by_artist_sort(self.NAMES) == sorted(
            self.NAMES, key=artist_sort_key
        )

    def test_letter_is_identical(self):
        for name in self.NAMES:
            assert _sql_letter(name) == artist_sort_letter(name), name

    def test_the_offspring_is_ordered_among_the_os(self):
        ordered = _order_by_artist_sort(self.NAMES)
        o_index = ordered.index("The Offspring")
        assert ordered[o_index + 1] == "The Pretty Reckless"
        assert ordered.index("The Cure") < o_index


class TestWhitespaceParity:
    """Whitespace edge cases are where SQL and Python drifted before."""

    CASES = [
        "The  Two Spaces",  # SUBSTR(x, 5) used to leave a leading space
        "  The Cure  ",  # stray outer whitespace
        "The\tTabbed",  # a tab is NOT a separator in either implementation
        "The  ",  # article only, after trimming
    ]

    def test_ordering_matches_python(self):
        assert _order_by_artist_sort(self.CASES) == sorted(
            self.CASES, key=artist_sort_key
        )

    def test_core_never_keeps_a_leading_space(self):
        for name in self.CASES:
            core, _ = split_leading_article(name)
            assert not core.startswith(" "), name

    def test_letter_matches_python(self):
        for name in self.CASES:
            assert _sql_letter(name) == artist_sort_letter(name), name

    def test_repeated_spaces_do_not_sort_ahead_of_everything(self):
        # A leading space would sort before "!" — the bug this guards.
        ordered = _order_by_artist_sort(["The  Two Spaces", "!!!", "Zebrahead"])
        assert ordered[0] == "!!!"


class TestLetterPredicate:
    """The letter scan must agree with the section the artist is rendered in."""

    def _matches(self, letter: str, names: list[str]) -> list[str]:
        engine = _sqlite_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE t (artist TEXT)"))
            conn.execute(
                text("INSERT INTO t (artist) VALUES (:a)"), [{"a": n} for n in names]
            )
            pred = artist_starts_with_letter_sql("artist")
            expr = artist_sort_sql("artist")
            rows = conn.execute(
                text(f"SELECT artist FROM t WHERE {pred} ORDER BY {expr}"),
                {"prefix": f"{letter}%"},
            ).fetchall()
        return [r[0] for r in rows]

    def test_o_finds_the_offspring(self):
        assert self._matches("O", ["The Offspring", "Radiohead"]) == ["The Offspring"]

    def test_t_does_not_find_the_offspring(self):
        # The regression this whole change exists to prevent: the naive
        # `UPPER(artist) LIKE 'T%'` put The Offspring under T.
        assert self._matches("T", ["The Offspring", "Theatre of Tragedy"]) == [
            "Theatre of Tragedy"
        ]

    def test_p_finds_the_pretty_reckless(self):
        assert self._matches("P", ["The Pretty Reckless", "The Cure"]) == [
            "The Pretty Reckless"
        ]

    def test_c_finds_the_cure(self):
        assert self._matches("C", ["The Cure", "Radiohead"]) == ["The Cure"]
