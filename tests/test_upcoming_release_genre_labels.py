"""Main-genre labels on upcoming releases.

REPORTED: *"On the upcoming releases show the genre its aligned to by using the
main one or two genres from musicbrainz. Maybe using a colour coding system for
different genre labels for main genres and sub genres for labels to easily see
if its a metal, pop, rock, punk, dance, etc."*

Design decisions these tests pin, and why each one would be wrong to undo:

1. **The classification is the EXISTING vocabulary**, not a second taxonomy.
   ``classify_main_genre`` already maps ``nu metal`` -> Metal and ``grunge`` ->
   Rock for the playlist builder. A separate table here would eventually
   disagree with the playlist names about what genre an album is.
2. **A label is never colour-only** — every chip carries its genre name.
3. **Unknown genres fall back to a neutral chip**, never vanish. A missing
   colour is cosmetic; a missing label is data loss.
4. **The two columns are ADDITIVE**: absent on an old database, added by
   migration 039 and by the bootstrap registry, and a re-discovery must not
   overwrite a label the match path already resolved.
"""

from __future__ import annotations

import re
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

LABELS = REPO_ROOT / "services/catalog/upcoming_genre_labels.py"
ROUTES = REPO_ROOT / "routes/upcoming_releases_routes.py"
FETCHER = REPO_ROOT / "services/upcoming_releases/musicbrainz_fetcher_service.py"
SERVICE_JS = REPO_ROOT / "test_site/static/js/services/upcoming-releases.js"
CSS = REPO_ROOT / "test_site/static/css/upcoming-genres.css"
SCHEMA = REPO_ROOT / "db/schema.py"
MIGRATION = REPO_ROOT / "migrations/versions/039_upcoming_release_genres.py"


def _read(path: pathlib.Path) -> str:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


class TestClassifyMainGenres:
    def _describe(self, entries):
        from services.catalog.upcoming_genre_labels import describe_release_genres

        return describe_release_genres(entries)

    def test_a_subgenre_resolves_to_its_root(self):
        """The whole point: ``nu metal`` is Metal, not its own label."""
        out = self._describe([{"name": "nu metal", "count": 5}])
        assert [g["id"] for g in out["main_genres"]] == ["metal"]
        assert out["main_genres"][0]["display"] == "Metal"

    def test_two_roots_are_shown_when_a_release_spans_both(self):
        out = self._describe([
            {"name": "nu metal", "count": 5},
            {"name": "alternative rock", "count": 3},
        ])
        ids = [g["id"] for g in out["main_genres"]]
        assert "metal" in ids and "rock" in ids, ids
        assert len(ids) == 2

    def test_the_strongest_vote_leads(self):
        """``count`` is evidence — the higher count must produce the first chip."""
        out = self._describe([
            {"name": "pop", "count": 14},
            {"name": "rock", "count": 2},
        ])
        assert out["main_genres"][0]["id"] == "pop", out["main_genres"]

    def test_at_most_two_main_genres(self):
        out = self._describe([
            {"name": "metal", "count": 9},
            {"name": "rock", "count": 8},
            {"name": "punk", "count": 7},
            {"name": "electronic", "count": 6},
        ])
        assert len(out["main_genres"]) <= 2

    def test_subgenres_are_listed_separately(self):
        """The sub-genre chip is what makes "Rock" say WHICH rock."""
        out = self._describe([
            {"name": "alternative rock", "count": 4},
            {"name": "grunge", "count": 2},
        ])
        assert out["main_genres"][0]["id"] == "rock"
        assert "alternative rock" in out["sub_genres"]

    def test_a_root_name_is_not_repeated_as_a_subgenre(self):
        """A row labelled "Metal" does not need a "Metal" chip beside it."""
        out = self._describe([{"name": "metal", "count": 3}])
        assert out["main_genres"][0]["id"] == "metal"
        assert "metal" not in [s.casefold() for s in out["sub_genres"]]

    def test_an_unknown_genre_is_not_invented(self):
        """CONTROL: an unrecognisable tag answers NO label, not a guess."""
        out = self._describe(["zydeco-speedcore-nonsense"])
        assert out["main_genres"] == []

    def test_empty_input_is_empty_output(self):
        assert self._describe([]) == {"main_genres": [], "sub_genres": []}

    def test_plain_strings_are_accepted(self):
        """Two call sites store a list of strings rather than dicts."""
        out = self._describe(["punk", "pop punk"])
        assert out["main_genres"][0]["id"] == "punk"

    def test_duplicate_names_vote_once(self):
        out = self._describe([
            {"name": "Rock", "count": 1},
            {"name": "rock", "count": 1},
        ])
        assert [g["id"] for g in out["main_genres"]] == ["rock"]


# ---------------------------------------------------------------------------
# The schema and its migration
# ---------------------------------------------------------------------------


class TestTheColumnsExist:
    def test_the_bootstrap_registry_declares_both(self):
        """A database built by db/schema.py must have the columns."""
        schema = _read(SCHEMA)
        block = schema[schema.index('"upcoming_releases": {'):]
        block = block[: block.index("},")]
        assert "musicbrainz_genres" in block
        assert "main_genres" in block

    def test_the_migration_is_in_the_chain(self):
        migration = _read(MIGRATION)
        assert 'revision: str = "039_upcoming_release_genres"' in migration
        assert 'down_revision: Union[str, None] = "038_tracks_artist_trim_idx"' in migration

    def test_every_fixture_that_runs_the_upsert_has_the_columns(self):
        """⚠️ A hand-written test DDL silently lags the schema.

        Six upcoming-releases suites create ``upcoming_releases`` by hand rather
        than from ``db/schema.py``. The keystroke that adds a column to
        production does not touch them, so a fixture only fails when some test
        happens to exercise the write — which is exactly how this went unnoticed
        until the upsert changed.

        Guarding the ones that run the UPSERT is the point: the read-only
        fixtures never name these columns and do not need them.
        """
        import re as _re

        tests = REPO_ROOT / "tests"
        ddl_re = _re.compile(
            r"CREATE TABLE upcoming_releases \((?P<body>.*?)\n\s*\)",
            _re.DOTALL,
        )
        offenders = []
        for path in sorted(tests.glob("test_upcoming*.py")):
            body = path.read_text(encoding="utf-8")
            match = ddl_re.search(body)
            if not match:
                continue
            columns = match.group("body")
            # Only the fixtures whose suite writes through the discovery upsert.
            if "_persist_releases" not in body and "_INSERT_RELEASE_SQL" not in body:
                if "persist" not in body:
                    continue
            missing = [
                name for name in ("musicbrainz_genres", "main_genres")
                if name not in columns
            ]
            if missing:
                offenders.append(f"{path.name}: missing {missing}")

        assert not offenders, (
            "test fixtures that run the upcoming-releases upsert are missing "
            "columns the SQL now writes:\n  " + "\n  ".join(offenders)
        )

    def test_the_migration_checks_the_table_before_the_column(self):
        """The 033/034 defect: an absent table has no columns, so a column-only
        guard issues an ALTER against a table that is not there."""
        migration = _read(MIGRATION)
        upgrade = migration[migration.index("def upgrade()"):]
        assert "has_table(_TABLE)" in upgrade, (
            "the migration must check the table exists before altering it"
        )
        assert upgrade.index("has_table(_TABLE)") < upgrade.index("add_column"), (
            "the table check must come BEFORE the add_column"
        )

    def test_redisovery_cannot_wipe_a_resolved_label(self):
        """The upsert must COALESCE, not overwrite with NULL.

        A re-discovery carries no genres (the search does not ask for them), so
        a plain ``= EXCLUDED.x`` would strip a label the match path had just
        resolved.
        """
        fetcher = _read(FETCHER)
        for column in ("musicbrainz_genres", "main_genres"):
            assert re.search(
                rf"{column}\s*=\s*COALESCE\(", fetcher
            ), f"{column} is overwritten instead of filled in"


# ---------------------------------------------------------------------------
# The backend produces the data
# ---------------------------------------------------------------------------


class TestTheRouteReturnsLabels:
    def test_the_list_response_carries_both_lists(self):
        routes = _read(ROUTES)
        assert 'release["main_genres"]' in routes
        assert 'release["sub_genres"]' in routes

    def test_the_match_path_fetches_genres(self):
        """A matched row must be labelled without waiting for a later pass."""
        routes = _read(ROUTES)
        assert "get_release_group(" in routes
        assert 'inc="genres+tags"' in routes

    def test_the_match_update_writes_both_columns(self):
        routes = _read(ROUTES)
        matched = routes[routes.index("def _persist_match_result"):]
        matched = matched[: matched.index("elif status ==")]
        assert "musicbrainz_genres" in matched
        assert "main_genres" in matched

    def test_the_genre_fetch_is_best_effort(self):
        """A rate-limited MusicBrainz must not fail the MATCH itself."""
        routes = _read(ROUTES)
        fn = routes[routes.index("def _fetch_release_group_genres"):]
        fn = fn[: fn.index("\ndef ")]
        assert "except Exception" in fn
        assert "return []" in fn

    @pytest.mark.parametrize("shape", ["str", "list", "none"])
    def test_a_row_missing_genres_does_not_break_the_list(self, shape):
        """Old rows (pre-migration) have NULL, SQLite hands back a string."""
        from routes.upcoming_releases_routes import _decode_genre_json

        raw = {
            "str": '[{"name": "metal", "count": 2}]',
            "list": [{"name": "metal", "count": 2}],
            "none": None,
        }[shape]
        out = _decode_genre_json({"musicbrainz_genres": raw})
        if shape == "none":
            assert out == {"main_genres": [], "sub_genres": []}
        else:
            assert out["main_genres"][0]["id"] == "metal"


# ---------------------------------------------------------------------------
# The frontend renders the labels
# ---------------------------------------------------------------------------


class TestTheBadgesRender:
    def test_every_chip_carries_its_genre_name(self):
        """⚠️ A label must never be colour-ONLY.

        Red vs orange is not readable for a colour-blind user, and on a printed
        page or a greyscale screenshot the colour disappears. The name is the
        message; the colour is the accelerator.
        """
        js = _read(SERVICE_JS)
        fn = js[js.index("function genreBadges"):]
        fn = fn[: fn.index("\n  }")]
        assert "g.display" in fn, "the chip no longer renders the genre NAME"

    def test_the_class_is_sanitised_from_the_id(self):
        """The id comes over JSON; a class list is not the place to trust it."""
        js = _read(SERVICE_JS)
        fn = js[js.index("function genreBadges"):]
        fn = fn[: fn.index("\n  }")]
        assert re.search(r"replace\(/\[\^a-z0-9_\]/g", fn), (
            "the genre id is interpolated into a class without being filtered"
        )

    def test_the_badges_reach_both_renderers(self):
        """The table row AND the phone carousel."""
        js = _read(SERVICE_JS)
        assert js.count("genreBadges(release)") >= 3, (
            "genreBadges must be called from the table row and the carousel card"
        )

    @pytest.mark.parametrize("name", ["metal", "pop", "rock", "punk", "electronic"])
    def test_each_named_genre_has_a_colour(self, name: str):
        """The reported examples must be colour-coded, not fall back to grey."""
        css = _read(CSS)
        assert f".ur-genre--{name}" in css, f"{name} has no colour rule"

    def test_there_is_a_neutral_fallback(self):
        """CONTROL: a vocabulary root the palette does not know must still show."""
        css = _read(CSS)
        base = css[css.index(".ur-genre {"):]
        base = base[: base.index("}")]
        assert "background" in base and "color" in base, (
            "`.ur-genre` itself must carry a neutral fill so an unknown root "
            "renders as a grey chip rather than an invisible one"
        )

    def test_the_stylesheet_is_loaded_by_both_rendering_pages(self):
        for rel in (
            "test_site/templates/Pages/downloads/upcoming.html",
            "test_site/templates/Pages/dashboard.html",
        ):
            page = _read(REPO_ROOT / rel)
            assert "css/upcoming-genres.css" in page, (
                f"{rel} renders upcoming rows without the chip palette"
            )

    def test_the_stylesheet_is_scoped(self):
        """CONTROL: it must not restyle a class used elsewhere.

        The invariant is not "the selector starts with .ur-genre" — a descendant
        rule like ``.ur-card .ur-genre`` legitimately starts with the card. It is
        that every rule TARGETS a genre chip, so a broad selector cannot restyle
        unrelated markup.
        """
        css = _read(CSS)
        selectors = re.findall(r"^([.\w][^{@\n]*(?:,\s*\n?[.\w][^{@\n]*)*)\{", css, re.MULTILINE)
        assert selectors, "no selectors found — the regex is wrong"
        for group in selectors:
            for selector in (s.strip() for s in group.split(",")):
                if not selector:
                    continue
                # The SUBJECT of the selector is its last compound.
                subject = selector.split()[-1]
                assert subject.startswith((".ur-genre", ".ur-subgenre")), (
                    f"{selector!r} does not target a genre chip as its subject — "
                    "this file should only style the upcoming-release chips"
                )

    def test_subgenre_chips_hide_on_a_phone(self):
        """Fifty rows of two chip lines is unreadable on a phone; the sub-genre
        is the least important thing on the row."""
        css = _read(CSS)
        assert "@media (max-width: 575.98px)" in css
        phone = css[css.index("@media (max-width: 575.98px)"):]
        assert ".ur-subgenre" in phone and "display: none" in phone
