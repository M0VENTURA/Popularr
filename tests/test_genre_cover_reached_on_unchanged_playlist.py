"""The genre-cover pass must run on the UNCHANGED path too.

Reported: the Gemini key was in ``config.yaml``, yet no AI cover art ever
appeared and the scan log said nothing about covers.

Root cause: ``_create_genre_top_track_playlists`` did a bare ``continue`` when
``_sync_playlist_to_navidrome`` reported the playlist's content signature as
already pushed. That ``continue`` sat ABOVE the AI-cover block, so the block was
skipped for every playlist that had ever synced — even though the block's own
comment promises it "runs for EVERY playlist this pass synced — created,
updated OR merely unchanged — because the point is to ensure the playlist HAS
its cover".

Consequence: enabling the feature, correcting a bad key, or healing a cover
removed by hand could not take effect until the playlist's CONTENT changed. And
because the toggle being OFF produced no log line at all, that pass was
indistinguishable from one that looked and found nothing to do.

These are structural tests over the real source. The behaviour they pin is a
control-flow property of a single function that is otherwise only reachable
end-to-end via a full scan.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_FINALISE = REPO_ROOT / "services" / "popularity" / "stages" / "finalise_stage.py"
FINALISE_SRC = _FINALISE.read_text(encoding="utf-8")

#: The sync-result branch in the genre loop. Anchored on both lines because
#: ``keep_playlist_names.add(playlist_name)`` appears twice in the function.
_SKIP_ANCHOR = (
    'keep_playlist_names.add(playlist_name)\n'
    '            if sync_res.get("skipped"):'
)

_COVER_HEADER = "# ── AI cover art for the TOP GENRE playlists"


def test_no_continue_between_the_skip_branch_and_the_cover_block():
    """A bare ``continue`` here is the bug: it skips the cover check entirely.

    Checked per LINE, so the explanatory comment that mentions ``continue``
    does not satisfy (or break) the assertion — only a real statement counts.
    """
    start = FINALISE_SRC.index(_SKIP_ANCHOR)
    end = FINALISE_SRC.index(_COVER_HEADER, start)
    between = FINALISE_SRC[start:end]

    offenders = [line for line in between.splitlines() if line.strip() == "continue"]

    assert not offenders, (
        "the skipped branch jumps over the AI-cover block again — a playlist "
        "that already synced would never have its cover checked"
    )


def test_the_cover_block_still_follows_the_sync_result():
    """Guards the anchor above: the cover block must remain AFTER the skip."""
    assert FINALISE_SRC.index(_COVER_HEADER) > FINALISE_SRC.index(_SKIP_ANCHOR)


def test_the_off_toggle_is_distinguished_in_the_summary():
    """Covers being OFF must not be silent when the Gemini key IS configured.

    ``_cover_checked`` only increments inside the enabled branch, so before this
    the summary line was simply absent and "disabled" looked exactly like
    "enabled but nothing to do".
    """
    assert "_cover_flag_off = True" in FINALISE_SRC, (
        "the pass no longer records why nothing was checked"
    )
    assert "elif _cover_flag_off:" in FINALISE_SRC, (
        "the summary no longer reports the disabled case"
    )
