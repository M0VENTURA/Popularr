"""Startup JSONB/TEXT drift check-repair.

The guard rails matter more than the happy path here, because this pass issues
DDL (``ALTER COLUMN … TYPE JSONB``) against a live database at boot. The
assertions below therefore focus on:

1. **It refuses to run unless test-site mode is on.** This is the whole reason
   the pass exists in this form — a type conversion rewrites every row of the
   column, so it must be proven on the rebuilt UI first.
2. **It is idempotent.** A column that is already JSONB is skipped, never
   rewritten. A second boot must be a no-op.
3. **It only touches the known drifted columns**, and leaves anything with an
   unexpected type alone rather than guessing.
4. **It converts the way the schema bootstrap does**, so the two cannot
   converge on different JSON shapes.
5. **It never raises.** A repair pass must not be able to stop a boot.
6. **It invents no schema.** In particular it must not create GIN indexes —
   verified by inspection that NONE exist on the drifted columns.

The drift is established by cross-referencing
``migrations/versions/001_initial_schema.py`` (which created these columns as
``sa.Text()``) with the ALTER blocks in ``db/schema.py`` (which never mention
them). ``test_drift_list_matches_the_two_sources_of_truth`` re-derives that
from the files so the literal list cannot silently go stale.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "db" / "jsonb_drift_repair.py"
SCHEMA_PATH = REPO_ROOT / "db" / "schema.py"
MIGRATION_PATH = REPO_ROOT / "migrations" / "versions" / "001_initial_schema.py"


@pytest.fixture(scope="module")
def repair_module():
    import db.jsonb_drift_repair as mod

    return mod


@pytest.fixture(scope="module")
def module_source() -> str:
    return MODULE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The test-site gate
# ---------------------------------------------------------------------------


class TestTestSiteGate:
    def test_it_declines_when_test_site_is_off(self, repair_module, monkeypatch):
        """The load-bearing safety property: no test-site, no DDL."""
        monkeypatch.setattr(repair_module, "_enabled", lambda: False)

        executed: list[str] = []

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *args, **kwargs):
                executed.append(str(args[0]) if args else "")
                raise AssertionError("no SQL may run when the gate is closed")

        monkeypatch.setattr(repair_module, "db_session", lambda: _Session())

        result = repair_module.run_startup_check_repair()

        assert result["ran"] is False
        assert result["reason"] == "test-site mode is off"
        assert executed == []
        assert result["converted"] == []

    def test_it_reads_the_same_flag_as_the_ui_cutover(self, module_source: str):
        """One definition of "test site" in the codebase.

        The pass must not read ``features.use_test_site`` itself — if it did,
        the flag could mean one thing to the cutover and another here.
        """
        assert "from helpers.test_site_mode import config_enables_test_site" in module_source
        assert "config_enables_test_site()" in module_source

    def test_a_config_failure_disables_the_pass(self, repair_module, monkeypatch):
        """A config read that raises must not default to running DDL."""
        import helpers.test_site_mode as tsm

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(tsm, "config_enables_test_site", _boom)
        assert repair_module._enabled() is False


# ---------------------------------------------------------------------------
# 2. Idempotency + selective conversion
# ---------------------------------------------------------------------------


class _FakeSession:
    """Records SQL and answers the type probe from a supplied mapping."""

    def __init__(self, types: dict[str, dict[str, str]], log: list[str]):
        self.types = types
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.log.append(sql)
        if "pg_attribute" in sql:
            table = (params or {}).get("name")
            found = self.types.get(table, {})
            return _Result([[col, typ] for col, typ in found.items()])
        return _Result([])


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


def _run(repair_module, monkeypatch, types):
    log: list[str] = []
    monkeypatch.setattr(repair_module, "_enabled", lambda: True)
    monkeypatch.setattr(repair_module, "db_session", lambda: _FakeSession(types, log))
    monkeypatch.setattr(
        repair_module, "_invalidate_cache", lambda: None, raising=False
    )
    return repair_module.run_startup_check_repair(), log


class TestIdempotency:
    def test_already_jsonb_columns_are_not_rewritten(self, repair_module, monkeypatch):
        types = {
            "tracks": {
                "manual_genres": "jsonb",
                "navidrome_genres": "jsonb",
                "spotify_genres": "jsonb",
                "listenbrainz_genres": "jsonb",
                "essentia_genres": "jsonb",
            },
            "missing_releases": {"lastfm_tags": "jsonb"},
        }
        result, log = _run(repair_module, monkeypatch, types)

        assert result["ran"] is True
        assert result["converted"] == []
        alters = [s for s in log if "ALTER COLUMN" in s]
        assert alters == [], f"idempotency broken — issued DDL: {alters}"
        # ⚠️ Asserted SEPARATELY from `converted`. Without this, dropping the
        # `_is_jsonb` early-continue still leaves `converted` empty — the
        # column would fall through to the unexpected-type branch instead — so
        # the test would pass while every correct column was reported as an
        # error on every boot. Mutation testing caught exactly that hole.
        assert result["errors"] == [], (
            f"correctly-typed columns were not recognised: {result['errors']}"
        )
        # And all six must have been *seen*, or an empty probe would pass.
        assert len(result["checked"]) == 6, result["checked"]

    def test_only_text_columns_are_converted(self, repair_module, monkeypatch):
        types = {
            "tracks": {
                "manual_genres": "text",
                "navidrome_genres": "jsonb",
                "spotify_genres": "jsonb",
                "listenbrainz_genres": "jsonb",
                "essentia_genres": "jsonb",
            },
            "missing_releases": {"lastfm_tags": "jsonb"},
        }
        result, log = _run(repair_module, monkeypatch, types)

        assert result["converted"] == ["tracks.manual_genres"]
        alters = [s for s in log if "ALTER COLUMN" in s]
        assert len(alters) == 1
        assert "manual_genres" in alters[0]

    def test_varchar_is_treated_as_the_drift(self, repair_module, monkeypatch):
        """Migration 001 used Text(), which maps to either text or varchar."""
        types = {
            "tracks": {"manual_genres": "character varying"},
            "missing_releases": {},
        }
        result, _log = _run(repair_module, monkeypatch, types)
        assert result["converted"] == ["tracks.manual_genres"]

    def test_unexpected_type_is_reported_not_converted(self, repair_module, monkeypatch):
        """An unanticipated type must be left alone, not guessed at."""
        types = {
            "tracks": {"manual_genres": "integer"},
            "missing_releases": {},
        }
        result, log = _run(repair_module, monkeypatch, types)

        assert result["converted"] == []
        assert any("unexpected type" in e for e in result["errors"])
        assert [s for s in log if "ALTER COLUMN" in s] == []

    def test_missing_table_is_not_an_error(self, repair_module, monkeypatch):
        """A fresh install may not have every table yet."""
        result, log = _run(repair_module, monkeypatch, {"tracks": {}, "missing_releases": {}})
        assert result["ran"] is True
        assert result["errors"] == []
        assert result["converted"] == []


# ---------------------------------------------------------------------------
# 3. The conversion expression
# ---------------------------------------------------------------------------


class TestConversionExpression:
    def test_empty_becomes_an_empty_array_not_null(self, repair_module):
        expr = repair_module._convert_using("manual_genres")
        assert "'[]'::jsonb" in expr
        # NULL must not survive as NULL — the readers treat [] as "no genres",
        # but a NULL jsonb would break array operations elsewhere.
        assert "IS NULL OR trim(manual_genres) = ''" in expr

    def test_already_json_is_cast_not_resplit(self, repair_module):
        """The '[{' guard is what stops a JSON literal being shredded.

        ``['rock','metal']`` split on commas would become
        ``['["rock"', '"metal"]']`` — the exact corruption this guard prevents.
        """
        expr = repair_module._convert_using("spotify_genres")
        assert "left(trim(spotify_genres), 1) IN ('[', '{')" in expr
        assert "spotify_genres::jsonb" in expr

    def test_csv_becomes_an_array(self, repair_module):
        expr = repair_module._convert_using("essentia_genres")
        assert "to_jsonb(string_to_array(essentia_genres, ','))" in expr

    def test_it_matches_the_existing_schema_alter(self, repair_module):
        """Must not diverge from the proven ALTER block in db/schema.py.

        The bootstrap already converts musicbrainz_genres this way; if this
        pass used a different shape, the same column could end up different
        depending on which path ran first.
        """
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        # The three behaviours must appear in the schema's own USING clause.
        assert "left(trim(musicbrainz_genres), 1) IN ('[', '{')" in schema
        assert "to_jsonb(string_to_array(musicbrainz_genres, ','))" in schema
        expr = repair_module._convert_using("musicbrainz_genres")
        assert "left(trim(musicbrainz_genres), 1) IN ('[', '{')" in expr
        assert "to_jsonb(string_to_array(musicbrainz_genres, ','))" in expr


# ---------------------------------------------------------------------------
# 4. It must never raise
# ---------------------------------------------------------------------------


class TestNeverRaises:
    def test_a_probe_failure_is_captured(self, repair_module, monkeypatch):
        monkeypatch.setattr(repair_module, "_enabled", lambda: True)

        class _Boom:
            def __enter__(self):
                raise RuntimeError("connection lost")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(repair_module, "db_session", lambda: _Boom())
        result = repair_module.run_startup_check_repair()

        assert result["ran"] is True
        assert result["errors"], "a probe failure must be reported"
        assert result["converted"] == []

    def test_an_alter_failure_does_not_abort_the_rest(self, repair_module, monkeypatch):
        """One stubborn column must not block the others."""
        log: list[str] = []

        class _Sess:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, statement, params=None):
                sql = str(statement)
                log.append(sql)
                if "pg_attribute" in sql:
                    return _Result([
                        ["manual_genres", "text"],
                        ["navidrome_genres", "text"],
                    ])
                if "manual_genres" in sql:
                    raise RuntimeError("column is locked")
                return _Result([])

        monkeypatch.setattr(repair_module, "_enabled", lambda: True)
        monkeypatch.setattr(repair_module, "db_session", lambda: _Sess())

        result = repair_module.run_startup_check_repair()

        assert result["converted"] == ["tracks.navidrome_genres"], (
            "the second column must still be attempted after the first fails"
        )
        assert any("manual_genres" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# 5. No invented schema
# ---------------------------------------------------------------------------


class TestNoInventedSchema:
    def test_it_creates_no_indexes(self, module_source: str):
        """Verified: NO GIN index exists on any drifted column.

        The JSONB GIN indexes in INDEXES_TO_ENSURE cover musicbrainz_genres /
        discogs_genres / lastfm_tags / audiodb_genres / wikidata_genres — none
        of which are drifted, because those columns DID get their ALTER.
        Creating indexes here would invent schema the codebase never had.
        """
        assert "CREATE INDEX" not in module_source.upper()

    def test_no_gin_index_targets_a_drifted_column(self):
        """Re-derive the claim above from the schema itself.

        ⚠️ Must be TABLE-AWARE. ``lastfm_tags`` is a GIN index target on
        ``tracks`` — where it DID get its ALTER, so it is not drifted — while
        the drifted ``lastfm_tags`` lives on ``missing_releases``. Matching on
        the column name alone conflates two different columns.
        """
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        drifted = {
            ("tracks", "manual_genres"),
            ("tracks", "navidrome_genres"),
            ("tracks", "spotify_genres"),
            ("tracks", "listenbrainz_genres"),
            ("tracks", "essentia_genres"),
            ("missing_releases", "lastfm_tags"),
        }
        gin_targets: set[tuple[str, str]] = set()
        for m in re.finditer(
            r"USING gin \(([^)]*)\)", schema
        ):
            for token in m.group(1).split(","):
                gin_targets.add(("tracks", token.strip()))
        clash = gin_targets & drifted
        assert not clash, (
            f"a GIN index now targets {clash} — re-check whether the repair "
            "pass should recreate it"
        )

    def test_it_touches_only_two_tables(self, repair_module):
        assert set(repair_module._DRIFTED) == {"tracks", "missing_releases"}


# ---------------------------------------------------------------------------
# 6. The drift list matches the two sources of truth
# ---------------------------------------------------------------------------


class TestDriftListIsStillTrue:
    def test_drift_list_matches_the_two_sources_of_truth(self, repair_module):
        """Re-derive which columns are drifted, and compare to the literal.

        Drift = declared JSONB in COLUMN_REGISTRY['tracks'] AND created as
        sa.Text() by migration 001 AND never ALTERed to JSONB in schema.py.

        This is the assertion that stops the hard-coded list going stale: if
        someone adds the missing ALTER, this test fails and tells them the
        list can shrink.
        """
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        migration = MIGRATION_PATH.read_text(encoding="utf-8")

        registry = re.search(r'"tracks": \{(.*?)\n    \},', schema, re.S)
        assert registry, "COLUMN_REGISTRY['tracks'] not found"
        declared_jsonb = {
            name for name, typ in re.findall(r'"(\w+)": "(\w+)"', registry.group(1))
            if typ == "JSONB"
        }
        assert declared_jsonb, "no JSONB columns parsed — the regex is wrong"

        created_text = set(re.findall(r'sa\.Column\("(\w+)",\s*sa\.Text\(\)', migration))
        altered = set(re.findall(r"ALTER COLUMN (\w+) TYPE JSONB", schema))

        derived = declared_jsonb & created_text - altered
        asserted = set(repair_module._DRIFTED["tracks"])

        assert derived == asserted, (
            f"drift list is stale.\n"
            f"  derived from the files: {sorted(derived)}\n"
            f"  hard-coded in the module: {sorted(asserted)}\n"
            f"  (directly ALTERed, so no longer drifted: {sorted(altered & declared_jsonb)})"
        )

    def test_the_columns_we_know_got_an_alter_are_not_listed(self, repair_module):
        """Spot-check the two columns the migration block did convert."""
        listed = set(repair_module._DRIFTED["tracks"])
        for column in ("musicbrainz_genres", "discogs_genres", "lastfm_tags", "single_sources"):
            assert column not in listed, f"{column} IS altered — it must not be listed"

    def test_missing_releases_drift_is_real(self, repair_module):
        """The second table's entry must also be justified."""
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        migration = MIGRATION_PATH.read_text(encoding="utf-8")
        assert '"missing_releases"' in schema
        assert 'sa.Column("lastfm_tags", sa.Text()' in migration
        # The ONLY lastfm_tags ALTER proves the tracks one, not the
        # missing_releases one.
        assert "table_name='missing_releases' AND column_name='lastfm_tags'" not in schema
        assert repair_module._DRIFTED["missing_releases"] == ("lastfm_tags",)


# ---------------------------------------------------------------------------
# 7. Wiring
# ---------------------------------------------------------------------------


class TestBootWiring:
    def test_it_is_started_at_boot(self):
        src = (REPO_ROOT / "db" / "bootstrap.py").read_text(encoding="utf-8")
        assert "_repair_jsonb_drift_at_boot" in src
        assert "from db.jsonb_drift_repair import run_startup_check_repair" in src

    def test_it_runs_on_both_startup_paths(self):
        """Immediate boot AND the deferred retry path.

        On a cold start PostgreSQL may not be ready, so schema work is
        deferred. A repair that only ran on the immediate path would silently
        never execute on the very boots that need it most.
        """
        src = (REPO_ROOT / "db" / "bootstrap.py").read_text(encoding="utf-8")
        calls = len(re.findall(r"^\s+_repair_jsonb_drift_at_boot\(\)$", src, re.M))
        assert calls == 2, f"expected the repair on both startup paths, found {calls}"

    def test_it_runs_off_the_main_thread(self):
        """It issues DDL — it must not delay startup."""
        src = (REPO_ROOT / "db" / "bootstrap.py").read_text(encoding="utf-8")
        start = src.index("def _repair_jsonb_drift_at_boot")
        # To the NEXT top-level def, so the window cannot clip the thread call.
        end = src.find("\ndef ", start + 1)
        body = src[start:end if end != -1 else len(src)]
        assert "threading.Thread" in body
        assert "daemon=True" in body

    def test_a_repair_failure_cannot_break_the_boot(self):
        """The boot helper must swallow everything.

        Asserted on the AST rather than the text: the docstring legitimately
        discusses the bootstrap's hard-``raise`` behaviour, so a substring
        check matches prose rather than code.
        """
        import ast

        src = (REPO_ROOT / "db" / "bootstrap.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        helper = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_repair_jsonb_drift_at_boot"
        )
        # No `raise` statement anywhere inside the helper or its nested _run.
        raises = [n for n in ast.walk(helper) if isinstance(n, ast.Raise)]
        assert not raises, "the boot helper must not raise — it would break startup"
        # And it must actually catch broadly.
        handlers = [
            h for n in ast.walk(helper) if isinstance(n, ast.Try) for h in n.handlers
        ]
        assert handlers, "no try/except: a failure would propagate out of the thread"
        assert any(
            isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in handlers
        ), "must catch Exception, not a narrower type"
