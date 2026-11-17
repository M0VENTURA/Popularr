"""Essentia as corroboration AND as a last-resort fallback.

REQUEST

> "Could I use it as corroborate and fallback? If so, continue. It also needs to
> update the moods for tracks when scanning."

WHAT WAS WRONG

1. **The popularity scan ignored Essentia.** ``track_stage`` — where most final
   genres are computed — did not include ``essentia_genres`` in its per-track
   source map, while the genre RESYNC and the VA/album helpers already did. The
   two paths disagreed about the same track.

2. **Essentia could never place a genre on its own.** Its source weight is 0.45
   against a first-slot floor of 0.60, so an analysed-but-untagged track got NO
   genres at all despite having been scanned and stored.

3. **Essentia wrote ``tracks.genres`` and the file tags** while its own comment
   said it must not, so a scan could overwrite the Final Genres the aggregator
   had just cleaned up.

4. **``essentia_genres`` absorbed the file's GENRE tag**, which is where THIS APP
   writes the Final Genres. Once the column is read as evidence that is a
   feedback loop: our own output returns as "independent acoustic analysis".

5. **Only the first mood was stored** (``raw[0]`` / ``texts[0]``), and
   ``mood_last_updated`` was never written at all.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from services.enrichment.genre_selection import select_album_or_track_genres

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TRACK_STAGE = REPO_ROOT / "services" / "popularity" / "stages" / "track_stage.py"
ESSENTIA = REPO_ROOT / "services" / "scanning" / "pipelines" / "essentia_scanner.py"
SELECTION = REPO_ROOT / "services" / "enrichment" / "genre_selection.py"


def _sel(source_map: dict, max_genres: int = 3) -> list[str]:
    return select_album_or_track_genres(source_map, max_genres=max_genres).get("genres") or []


class TestEssentiaCorroborates:
    def test_it_reinforces_a_matching_provider(self) -> None:
        assert _sel({"musicbrainz@track": ["nu metal"], "essentia@track": ["nu metal"]}) == [
            "Nu Metal"
        ]

    def test_it_cannot_outvote_a_provider(self) -> None:
        """0.45 < 1.00 — acoustic analysis must never overrule MusicBrainz."""
        got = _sel({"musicbrainz@track": ["hard rock"], "essentia@track": ["nu metal"]})
        assert "Hard Rock" in got
        assert not any("nu metal" in g.lower() for g in got)

    def test_an_empty_provider_pool_is_unaffected(self) -> None:
        assert _sel({"musicbrainz@track": ["hard rock"]}) == ["Hard Rock"]


class TestEssentiaIsAFallbackOnlyWhenNothingElseExists:
    def test_a_lone_essentia_tag_now_yields_a_genre(self) -> None:
        assert _sel({"essentia@track": ["nu metal"]}) == ["Nu Metal"], (
            "an analysed track with no provider evidence still produced nothing"
        )

    def test_it_is_capped_like_any_other_result(self) -> None:
        got = _sel({"essentia@track": ["nu metal", "alternative metal"]}, max_genres=1)
        assert len(got) == 1

    def test_the_fallback_can_be_disabled(self) -> None:
        from services.enrichment.genre_selection import SelectionSettings

        settings = SelectionSettings(essentia_fallback=False)
        result = select_album_or_track_genres(
            {"essentia@track": ["nu metal"]}, settings=settings
        )
        assert result.get("genres") == []

    def test_the_fallback_never_rescues_a_provider_source(self) -> None:
        """It is scoped to local analysis, not "any single weak source".

        A lone provider tag is exactly the weak evidence the confidence floors
        exist to reject; rescuing it would disable them library-wide.
        """
        from services.enrichment.genre_selection import SelectionSettings

        settings = SelectionSettings(essentia_fallback=True)
        # A provider with no usable genre: the fallback must not invent one.
        assert select_album_or_track_genres(
            {"spotify@track": ["nu metal"]}, settings=settings
        ).get("genres") == []


class TestTheScanPathReadsEssentia:
    def test_essentia_genres_is_in_the_per_track_source_map(self) -> None:
        src = TRACK_STAGE.read_text(encoding="utf-8")
        start = src.index("for key, source_name in [")
        block = src[start:src.index("]:", start)]
        assert "essentia_genres" in block, (
            "the popularity scan ignored the esssentia column while the resync "
            "used it — the two paths disagreed"
        )

    def test_it_is_declared_at_track_level(self) -> None:
        src = TRACK_STAGE.read_text(encoding="utf-8")
        assert '"essentia@track"' in src


def _strip_comments_and_docstrings(source: str) -> str:
    """Source with docstrings and ``#`` comments removed.

    The blocks below DOCUMENT the bug they fix, so the prose necessarily names
    the very expressions that must be gone (``updates["genres"]``, ``raw[0]``).
    Matching raw text would mean the test only passes while the reason is
    undocumented, so the check runs against code only.
    """
    try:
        import ast

        tree = ast.parse(source)
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                first = node.body[0] if node.body else None
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    docstrings.update(
                        range(first.lineno, (first.end_lineno or first.lineno) + 1)
                    )
    except SyntaxError:
        docstrings = set()

    kept = [
        line
        for number, line in enumerate(source.splitlines(), start=1)
        if number not in docstrings and not line.lstrip().startswith("#")
    ]
    return "\n".join(kept)


def _genre_write_block() -> str:
    """The genre branch, code only."""
    src = ESSENTIA.read_text(encoding="utf-8")
    start = src.index("if genre_str and tag_genres:")
    return _strip_comments_and_docstrings(src[start:start + 3000])


class TestEssentiaNoLongerWritesTheConsensusColumn:
    def test_it_does_not_assign_tracks_genres(self) -> None:
        assert 'updates["genres"]' not in _genre_write_block(), (
            "Essentia is still a second writer of the consensus genres column"
        )

    def test_it_does_not_write_genre_file_tags(self) -> None:
        assert "update_file_tags(" not in _genre_write_block()

    def test_the_essentia_column_only_holds_essentia_output(self) -> None:
        """It must not absorb the file's GENRE tag, which is our own output."""
        assert "_read_existing_tcon_genres" not in _genre_write_block()

    def test_the_baseline_is_snapshotted_before_the_run(self) -> None:
        src = ESSENTIA.read_text(encoding="utf-8")
        assert "_pre_essentia_children" in src
        # The snapshot must precede the subprocess, or it would include
        # Essentia's own writes.
        snap_at = src.index("_pre_essentia_children: set[str] = set()")
        run_at = src.index("result = subprocess.run(")
        assert snap_at < run_at


class TestMoodsAreUpdatedDuringTheScan:
    def test_all_mood_values_are_kept(self) -> None:
        src = ESSENTIA.read_text(encoding="utf-8")
        start = src.index("def _read_essentia_mood_from_file")
        block = src[start:src.index("def _read_essentia_genre_from_file", start)]
        code = _strip_comments_and_docstrings(block)
        assert "raw[0]" not in code, "only the first mood value survived"
        assert "texts[0]" not in code
        assert "_joined(" in code

    def test_the_same_joined_helper_is_used_for_every_container(self) -> None:
        src = ESSENTIA.read_text(encoding="utf-8")
        start = src.index("def _read_essentia_mood_from_file")
        block = src[start:src.index("def _read_essentia_genre_from_file", start)]
        # flac, mp3, ogg, opus, m4a, wma, generic
        assert _strip_comments_and_docstrings(block).count("_joined(") >= 6

    def test_mood_last_updated_is_written(self) -> None:
        src = ESSENTIA.read_text(encoding="utf-8")
        assert 'updates["mood_last_updated"]' in src, (
            "the column exists but nothing ever recorded when a mood was computed"
        )

    def test_mood_is_still_written(self) -> None:
        src = ESSENTIA.read_text(encoding="utf-8")
        assert 'updates["mood"] = mood_str' in src
        assert 'updates["mood_source"] = "essentia"' in src
