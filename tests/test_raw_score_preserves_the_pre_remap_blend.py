"""``tracks.raw_score`` — the pre-album-relative-remap popularity blend.

REPORTED
--------
> When just running a finalize scan from the dashboard, no tracks are getting
> 5 stars.

ROOT CAUSE
----------
``_apply_album_relative_normalization`` rewrites ``popularity_score`` /
``final_score`` from ``_raw_combined`` as ``sigmoid((x - median) / mad)`` —
monotonic but **saturating**, so applying it to an already-remapped value pulls
the album's extremes back toward the middle.

``_raw_combined`` was never persisted (an underscore-prefixed key that
``_execute_save`` drops), so the next run rebuilt it from ``final_score`` — the
*remapped* value — remapped it again, and wrote the result back. Measured with
the shipped functions, an 8-track album's top ``album_z`` falls monotonically
from 1.422 and crosses below ``star5_album_z`` (1.0) after 12 further passes,
after which the 5-star gate can never clear again while 4/3/2/1 keep working.
A repeated Finalise pass is exactly that loop.

``raw_score`` keeps the blend the remap is defined *against*, which makes the
whole operation idempotent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# A plausible raw blend: one clear standout, the rest trailing.
RAW = [88.0, 71.0, 64.0, 55.0, 47.0, 39.0, 31.0, 24.0]


def _remap_all(values):
    from services.popularity.popularity_math import apply_album_relative_popularity

    return [apply_album_relative_popularity(v, values) for v in values]


def _strip_python(src: str) -> str:
    """Remove docstrings and ``#`` comments so a guard cannot match its own
    explanation.

    Hit immediately: the migration's docstring *names* ``ADD COLUMN IF NOT
    EXISTS`` to say why it is PostgreSQL-only, and a substring check found that
    and reported the file as using it. Same class as a fix's comment quoting
    the broken pattern — strip before matching, always.
    """
    # re.S so the pattern needs no \n — writing \n inside a raw string that is
    # itself in a JSON-escaped edit produced a literal newline and a syntax
    # error last time.
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


# ---------------------------------------------------------------------------
# 1. The column exists wherever columns are declared
# ---------------------------------------------------------------------------


class TestTheColumnIsDeclared:
    def test_schema_declares_it(self):
        schema = (REPO_ROOT / "db" / "schema.py").read_text(encoding="utf-8")
        assert '"raw_score": "DOUBLE PRECISION"' in schema, (
            "db/schema.py is the DDL registry — without it the runtime "
            "bootstrap never adds the column and every write is silently dropped"
        )

    def test_the_orm_model_declares_it(self):
        models = (REPO_ROOT / "db" / "models.py").read_text(encoding="utf-8")
        assert re.search(r"^\s*raw_score: Mapped\[", models, re.M), (
            "the ORM model must expose raw_score or ORM reads miss it"
        )

    def test_a_navidrome_sync_cannot_overwrite_it(self):
        repo = (REPO_ROOT / "db" / "repositories" / "popularity_repository.py").read_text(
            encoding="utf-8"
        )
        protected = repo.split("_POPULARITY_PROTECTED_COLUMNS")[1].split("})", 1)[0]
        assert '"raw_score"' in protected, (
            "raw_score is owned by the scoring pipeline — a Navidrome sync must "
            "never replace it with a tag-derived value"
        )


# ---------------------------------------------------------------------------
# 2. The wiring: written when raw, read when stored
# ---------------------------------------------------------------------------


class _TrackStageSource:
    @classmethod
    def text(cls) -> str:
        path = REPO_ROOT / "services" / "popularity" / "stages" / "track_stage.py"
        return path.read_text(encoding="utf-8")


class TestTheRawBlendIsPersisted:
    def test_the_fresh_scoring_path_writes_it(self):
        src = _TrackStageSource.text()
        assert 'update_payload["raw_score"] = _fresh_raw' in src, (
            "a fresh score must persist its pre-remap blend, or the only copy "
            "dies with the process and the next run falls back to final_score"
        )
        # ...and only when the score was NOT taken from storage, or it would
        # overwrite a good raw_score with an already-remapped value.
        idx = src.index('update_payload["raw_score"] = _fresh_raw')
        guard = src.rfind('if not update_payload.get("_cached")', 0, idx)
        assert guard != -1 and idx - guard < 900, (
            "raw_score must be written under the not-_cached guard, or a cached "
            "track would overwrite a good raw_score with an already-remapped "
            "final_score. Window is 900 chars because the explanatory comment "
            "between them is deliberately long."
        )

    def test_both_reblend_paths_write_it(self):
        src = _TrackStageSource.text()
        assert 'update_payload["raw_score"] = float(_audited_final or 0)' in src, (
            "the log-ratio audit recomputes from raw components — persist it"
        )
        assert 'update_payload["raw_score"] = float(score_data["combined_score"])' in src, (
            "the interlude re-blend recomputes from the raw Last.fm component"
        )


class TestTheStoredReadPrefersRawScore:
    def test_the_singles_pass_reads_it_first(self):
        src = _TrackStageSource.text()
        idx = src.index('_stored_raw = float(track.get("raw_score") or 0)')
        window = src[idx: idx + 900]
        assert "if _stored_raw > 0:" in window, (
            "the stored branch must take the PRE-remap blend when present"
        )
        # Since 2026-10-11 the pre-016 fallback is a RECONSTRUCTION of the
        # true blend from stored listeners — never the remapped final_score
        # (feeding it back was the erosion loop behind \"previously-scanned
        # albums reset to 3★\"). See test_pre016_rows_repair_not_erode.py.
        assert "_reconstruct_raw_blend(" in window, (
            "pre-016 rows must rebuild their raw blend from stored data"
        )
        assert 'else float(score_data["combined_score"])' not in src, (
            "final_score is a REMAPPED value — it may never be the raw input"
        )

    def test_the_cached_branch_reads_it_first(self):
        src = _TrackStageSource.text()
        idx = src.index('update_payload["_cached"] = True')
        window = src[idx: idx + 1200]
        assert 'float(effective_track.get("raw_score") or 0)' in window, (
            "the cached branch is the one a Finalise pass takes for every track"
        )
        assert "_reconstruct_raw_blend(" in window, (
            "the cached branch must repair pre-016 rows, not re-remap them"
        )


# ---------------------------------------------------------------------------
# 3. WHY: the remap is only stable when fed the raw blend
# ---------------------------------------------------------------------------


class TestTheRemapIsIdempotentOnlyWithTheRawBlend:
    def test_a_genuinely_raw_blend_produces_the_stored_value(self, monkeypatch):
        """THE CONTRACT: with raw_score preserved, a re-run changes nothing."""
        from services.popularity import scan_stage_runner as runner

        expected = _remap_all(RAW)
        tracks = [
            {"track_id": f"t{i}", "_raw_combined": raw, "popularity_score": stored,
             "final_score": stored}
            for i, (raw, stored) in enumerate(zip(RAW, expected))
        ]

        persisted: list = []
        monkeypatch.setattr(runner, "_persist_album_relative_scores", persisted.append)

        changed = runner._apply_album_relative_normalization(tracks)

        assert changed == 0, (
            "a second pass over an album whose _raw_combined is the RAW blend "
            f"moved {changed} score(s) — the operation is not idempotent, so "
            "every Finalise run would keep eroding album_z"
        )
        assert persisted == []
        assert tracks[0]["popularity_score"] == pytest.approx(expected[0], abs=1e-3)

    def test_the_old_behaviour_remaps_and_moves_the_score(self, monkeypatch):
        """CONTROL — proves the test above can fail, and names the defect.

        Before ``raw_score``, ``_raw_combined`` was rebuilt from the ALREADY
        REMAPPED ``final_score``. Feed that in and the score moves — which is
        what ``_persist_album_relative_scores`` then wrote back, permanently.
        """
        from services.popularity import scan_stage_runner as runner

        remapped_once = _remap_all(RAW)
        tracks = [
            {"track_id": f"t{i}", "_raw_combined": stored, "popularity_score": stored,
             "final_score": stored}
            for i, stored in enumerate(remapped_once)
        ]
        before = [t["popularity_score"] for t in tracks]

        monkeypatch.setattr(runner, "_persist_album_relative_scores", lambda rows: None)
        changed = runner._apply_album_relative_normalization(tracks)

        assert changed > 0, (
            "feeding an already-remapped value back in must move it — if it "
            "does not, the erosion the fix targets no longer exists and this "
            "control is vacuous"
        )
        assert tracks[0]["popularity_score"] != before[0]

    def test_the_top_track_loses_star_z_without_the_raw_blend(self):
        """The measured consequence, so the arithmetic is pinned too."""
        from services.popularity.popularity_math import calculate_robust_zscore
        from services.popularity.stages.finalise_stage import _live_star_thresholds

        need = _live_star_thresholds()["star5_album_z"]

        once = _remap_all(RAW)
        z_after_one, _ = calculate_robust_zscore(once[0], once)
        assert z_after_one >= need, "control: a single pass still clears 5★"

        # What the old code did on every subsequent Finalise pass.
        cur = list(once)
        eroded_to = z_after_one
        for _ in range(12):
            cur = _remap_all(cur)
            eroded_to, _ = calculate_robust_zscore(cur[0], cur)
        assert eroded_to < need, (
            f"12 remap passes took album_z to {eroded_to:.3f} — under "
            f"star5_album_z={need}, so 5★ became unreachable while 4/3/2/1 kept "
            "working"
        )


# ---------------------------------------------------------------------------
# 4. The migration must run on both engines
# ---------------------------------------------------------------------------


class TestTheMigrationIsPortable:
    MIGRATION = REPO_ROOT / "migrations" / "versions" / "016_add_tracks_raw_score.py"

    def test_it_chains_from_the_previous_head(self):
        src = self.MIGRATION.read_text(encoding="utf-8")
        assert 'down_revision: Union[str, None] = "015_drop_download_queue_max_retries"' in src

    def test_it_uses_an_inspector_guard_not_pg_only_sql(self):
        """``ADD COLUMN IF NOT EXISTS`` is PostgreSQL-only and the chain also
        runs against SQLite — the exact failure this avoids."""
        src = _strip_python(self.MIGRATION.read_text(encoding="utf-8"))
        assert "ADD COLUMN IF NOT EXISTS" not in src, (
            "raw SQL with ADD COLUMN IF NOT EXISTS breaks the SQLite leg of the "
            "migration chain"
        )
        assert "sa.inspect" in src and "op.add_column" in src
