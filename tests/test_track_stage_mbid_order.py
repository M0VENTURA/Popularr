"""MBID resolution must precede popularity/LB matching, with a zero-LB fallback.

Reported: tracks with correct artist/title had Last.fm listeners but LB=0. LB is
recording-MBID scoped; LF is title/artist scoped. This pins the stage order and
the title/artist fallback used when an MBID-scoped LB count is zero.
"""

from __future__ import annotations

from pathlib import Path

TRACK_STAGE = (
    Path(__file__).resolve().parents[1]
    / "services/popularity/stages/track_stage.py"
)


def test_mbid_resolution_runs_before_popularity_scoring():
    source = TRACK_STAGE.read_text(encoding="utf-8")
    resolve_at = source.index("_mb_meta = _resolve_track_mb_metadata(")
    score_at = source.index("score_data, lb_percentile = _score_track_popularity(")
    assert resolve_at < score_at


def test_the_lb_block_resolves_an_mbid_before_the_lb_fetch():
    source = TRACK_STAGE.read_text(encoding="utf-8")
    # The LB fallback resolver sits before the recording-popularity call.
    resolve_at = source.index("get_suggested_mbid(")
    fetch_at = source.index("get_recording_popularity(recording_mbid)")
    assert resolve_at < fetch_at


def test_a_zero_mbid_scoped_lb_count_falls_back_to_title_artist():
    source = TRACK_STAGE.read_text(encoding="utf-8")
    assert "_lb_source = \"title_fallback\"" in source
    assert "get_aggregated_listenbrainz_popularity(" in source
    # The fallback must not pass the same ISRC that caused the zero.
    fallback_at = source.index("get_aggregated_listenbrainz_popularity(")
    window = source[fallback_at:fallback_at + 600]
    assert "isrc=None" in window


def test_the_debug_identity_event_exists():
    source = TRACK_STAGE.read_text(encoding="utf-8")
    assert "listenbrainz.identity" in source
    assert "listenbrainz_source=_lb_source" in source


def test_mb_recording_search_excludes_video_recordings():
    source = (
        Path(__file__).resolve().parents[1]
        / "services/enrichment/musicbrainz_service.py"
    ).read_text(encoding="utf-8")
    assert 'if bool(recording.get("video")):' in source
    assert '"video": bool(recording.get("video"))' in source


def test_the_album_batch_skips_video_recordings():
    source = (
        Path(__file__).resolve().parents[1]
        / "services/popularity/scan_stage_runner.py"
    ).read_text(encoding="utf-8")
    assert 'if bool(entry.get("video")):' in source


def test_lb_title_search_excludes_video_recordings():
    source = (
        Path(__file__).resolve().parents[1]
        / "services/popularity/popularity_sources.py"
    ).read_text(encoding="utf-8")
    assert 'if bool(rec.get("video")):' in source


def test_automatic_resolution_uses_length_and_title_intent():
    source = (
        Path(__file__).resolve().parents[1]
        / "services/enrichment/musicbrainz_service.py"
    ).read_text(encoding="utf-8")
    assert "duration_seconds=kwargs.get(\"duration_seconds\")" in source
    assert "_length_rank" in source
    assert "Normalize_core_title" in source
    assert "_unrequested_variants" in source


def test_track_stage_passes_duration_to_the_automatic_resolver():
    source = TRACK_STAGE.read_text(encoding="utf-8")
    assert "duration_seconds=_safe_duration(track.get(\"duration\"))" in source
    assert "duration_seconds=_safe_duration(" in source
    assert "effective_track.get(\"duration\")" in source
