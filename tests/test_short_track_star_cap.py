"""Regression tests for the short-track star cap.

A 50-second track was landing 4★/5★.  Skits, interludes and hidden-track
jokes are counted as full "listens" by Last.fm and ListenBrainz, so on a
popular album a very short track's raw play counts can out-rank the album's
real songs and earn a rating it cannot support.

``statistics.short_track_star_cap`` (default: enabled, 70s, 2★) is a FINAL
clamp applied after every other star path — era slot caps and the live-album
caps included — because those passes write 4★ to a demoted 5★ and would undo
a clamp applied earlier.

These tests cover:
- the config helper (defaults, partial overrides, ``0`` disables);
- the clamp itself (lowering, threshold semantics, ms normalisation);
- exemptions (user override, global 5★ lock, hearted track);
- ordering — the clamp survives the slot caps that run before it;
- the Config-page contract (both UI trees).
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest


# ---------------------------------------------------------------------------
# Config helper
# ---------------------------------------------------------------------------

class TestShortTrackStarCapConfig:
    """``get_short_track_star_cap_config`` defaults and partial overrides."""

    def test_defaults_when_config_empty(self):
        from helpers.config_helpers import get_short_track_star_cap_config

        cfg = get_short_track_star_cap_config({})
        assert cfg["enabled"] == 1
        assert cfg["max_duration_seconds"] == 70.0
        assert cfg["max_stars"] == 2

    def test_partial_block_overrides_only_named_keys(self):
        from helpers.config_helpers import get_short_track_star_cap_config

        cfg = get_short_track_star_cap_config(
            {"statistics": {"short_track_star_cap": {"max_duration_seconds": 90}}}
        )
        # Only the named key moves; the rest keep their defaults.
        assert cfg["max_duration_seconds"] == 90.0
        assert cfg["max_stars"] == 2
        assert cfg["enabled"] == 1

    def test_disabled_flag_accepted_as_bool_int_or_string(self):
        from helpers.config_helpers import get_short_track_star_cap_config

        for raw in (False, 0, "false", "0", "no", "off", ""):
            cfg = get_short_track_star_cap_config(
                {"statistics": {"short_track_star_cap": {"enabled": raw}}}
            )
            assert cfg["enabled"] == 0, f"{raw!r} should disable the cap"

    def test_malformed_values_fall_back_to_defaults(self):
        from helpers.config_helpers import get_short_track_star_cap_config

        cfg = get_short_track_star_cap_config(
            {
                "statistics": {
                    "short_track_star_cap": {
                        "max_duration_seconds": "not-a-number",
                        "max_stars": "also-not-a-number",
                    }
                }
            }
        )
        assert cfg["max_duration_seconds"] == 70.0
        assert cfg["max_stars"] == 2


# ---------------------------------------------------------------------------
# The clamp
# ---------------------------------------------------------------------------

def _track(track_id: str, title: str, stars: int, duration=None, **extra) -> dict:
    track = {
        "track_id": track_id,
        "title": title,
        "album": "Test Album",
        "popularity_score": 80.0,
        "final_score": 80.0,
        "stars": stars,
        "single_confidence": "low",
    }
    if duration is not None:
        track["duration"] = duration
    track.update(extra)
    return track


class TestShortTrackCapApplied:
    """Tracks under the threshold are held at or below ``max_stars``."""

    def test_fifty_second_track_capped_to_two_stars(self):
        from services.popularity.stages import finalise_stage as fs

        tracks = [_track("t1", "Hidden Noise", 5, duration=50.0)]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 2
        assert changed == 1

    def test_four_star_short_track_lowered_too(self):
        from services.popularity.stages import finalise_stage as fs

        tracks = [_track("t1", "Skit", 4, duration=42.0)]
        fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 2

    def test_exactly_the_threshold_is_not_capped(self):
        """The rule is "less than", so 70s itself is a real (if short) song."""
        from services.popularity.stages import finalise_stage as fs

        tracks = [_track("t1", "Just Long Enough", 5, duration=70.0)]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0

    def test_normal_length_track_untouched(self):
        from services.popularity.stages import finalise_stage as fs

        tracks = [_track("t1", "Real Song", 5, duration=245.0)]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0

    def test_tracks_already_at_or_below_the_cap_are_skipped(self):
        from services.popularity.stages import finalise_stage as fs

        tracks = [
            _track("t1", "Short One Star", 1, duration=20.0),
            _track("t2", "Short Two Star", 2, duration=30.0),
        ]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert [t["stars"] for t in tracks] == [1, 2]
        assert changed == 0

    def test_duration_from_db_lookup_when_row_lacks_it(self):
        """``track_stage`` result dicts do not carry duration."""
        from services.popularity.stages import finalise_stage as fs

        tracks = [_track("t1", "No Duration On Row", 5)]
        changed = fs._apply_short_track_star_cap(
            tracks, durations={"t1": 55.0}, artist="A", album="B"
        )

        assert tracks[0]["stars"] == 2
        assert changed == 1

    def test_missing_duration_is_left_alone(self):
        """Unknown length is not evidence of a short track."""
        from services.popularity.stages import finalise_stage as fs

        tracks = [_track("t1", "Unknown Length", 5)]
        changed = fs._apply_short_track_star_cap(tracks, durations={}, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0

    def test_zero_duration_is_left_alone(self):
        from services.popularity.stages import finalise_stage as fs

        tracks = [_track("t1", "Zero Duration", 5, duration=0)]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0

    def test_millisecond_duration_normalised(self):
        """A 50,000 ms value must not read as 50,000 seconds (i.e. no cap)."""
        from services.popularity.stages import finalise_stage as fs

        tracks = [_track("t1", "Ms Duration", 5, duration=50_000.0)]
        fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 2

    def test_eleven_minute_epic_not_misread_as_milliseconds(self):
        """A 700s track is long music, not 0.7s of micro-padding."""
        from services.popularity.stages import finalise_stage as fs

        tracks = [_track("t1", "Epic", 5, duration=700.0)]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0


class TestShortTrackCapExemptions:
    """A user override or a heart is an instruction, not a guess."""

    def test_user_single_override_never_lowered(self):
        from services.popularity.stages import finalise_stage as fs

        tracks = [
            _track("t1", "User Rated", 5, duration=40.0, single_confidence="user")
        ]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0

    def test_global_five_star_lock_never_lowered(self):
        from services.popularity.stages import finalise_stage as fs

        tracks = [
            _track("t1", "Locked", 5, duration=40.0, _global_5star_locked=True)
        ]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0

    def test_hearted_track_never_lowered(self):
        from services.popularity.stages import finalise_stage as fs

        tracks = [_track("t1", "Hearted", 5, duration=40.0, is_favourite=True)]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0

    def test_cap_clears_stale_five_star_flags(self):
        """A demoted track must not keep claiming a 5★ award."""
        from services.popularity.stages import finalise_stage as fs

        tracks = [
            _track(
                "t1", "Stale Flag", 5, duration=40.0,
                _era_5star=True, _force_floor=5,
            )
        ]
        fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 2
        assert tracks[0]["_era_5star"] is False
        assert tracks[0]["_force_floor"] <= 2


class TestShortTrackCapDisabled:
    """``enabled: false`` and defensive zero values disable the clamp."""

    def test_disabled_leaves_ratings_untouched(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch,
            "get_short_track_star_cap_config",
            lambda config=None: {
                "enabled": 0, "max_duration_seconds": 70.0, "max_stars": 2,
            },
        )
        tracks = [_track("t1", "Short Banger", 5, duration=50.0)]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0

    def test_zero_duration_threshold_disables(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs
        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch,
            "get_short_track_star_cap_config",
            lambda config=None: {
                "enabled": 1, "max_duration_seconds": 0, "max_stars": 2,
            },
        )
        tracks = [_track("t1", "Any Length", 5, duration=50.0)]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0

    def test_cap_of_five_or_more_disables(self, monkeypatch):
        """A 5★ ceiling caps nothing, so the pass is skipped entirely."""
        from services.popularity.stages import finalise_stage as fs
        import helpers.config_helpers as ch

        monkeypatch.setattr(
            ch,
            "get_short_track_star_cap_config",
            lambda config=None: {
                "enabled": 1, "max_duration_seconds": 70.0, "max_stars": 5,
            },
        )
        tracks = [_track("t1", "Short", 5, duration=50.0)]
        changed = fs._apply_short_track_star_cap(tracks, artist="A", album="B")

        assert tracks[0]["stars"] == 5
        assert changed == 0


# ---------------------------------------------------------------------------
# Ordering inside post_album_star_ratings
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RecordingSession:
    def __init__(self, rows_by_execute=None):
        self.executed: list[tuple] = []
        self._results = list(rows_by_execute or [])
        self._index = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        rows = self._results[self._index] if self._index < len(self._results) else []
        self._index += 1
        return _FakeResult(rows)

    def commit(self):
        pass

    def rollback(self):
        pass

    def begin_nested(self):
        return self

    def is_active(self):
        return True


def _session_factory(session):
    @contextmanager
    def _cm():
        yield session
    return _cm


class TestClampRunsLast:
    """The clamp must survive the slot caps that run before persistence."""

    def test_duration_is_loaded_alongside_stars(self, monkeypatch):
        """The batch query must SELECT duration, or the cap can never see it."""
        from services.popularity.stages import finalise_stage as fs

        session = _RecordingSession()
        # ``finalise_stage`` binds ``db_session`` at import time (line 93), so
        # patching ``db.engine`` would not reach it.
        monkeypatch.setattr(fs, "db_session", _session_factory(session))
        monkeypatch.setattr(
            fs, "_assign_stars", lambda track, *args, **kwargs: 5
        )
        monkeypatch.setattr(fs, "_sync_rating_to_navidrome", lambda *a, **k: True)

        results = [
            {
                "track_id": "t1",
                "artist": "Muse",
                "album": "Absolution",
                "title": "Short One",
                "popularity_score": 80.0,
                "final_score": 80.0,
                "single_confidence": "low",
            }
        ]
        fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={},
        )

        rating_loads = [
            str(sql) for sql, _ in session.executed
            if "FROM tracks WHERE CAST(id AS TEXT) IN" in str(sql)
        ]
        assert rating_loads, "expected the batch rating/path load to run"
        assert "duration" in rating_loads[0], (
            "batch load must fetch duration or the short-track cap is blind"
        )

    def test_post_album_caps_a_short_five_star_track(self, monkeypatch):
        """End-to-end: a 50s track that scored 5★ is persisted at 2★."""
        from services.popularity.stages import finalise_stage as fs

        session = _RecordingSession()
        monkeypatch.setattr(fs, "db_session", _session_factory(session))
        monkeypatch.setattr(fs, "_assign_stars", lambda track, *args, **kwargs: 5)
        monkeypatch.setattr(fs, "_sync_rating_to_navidrome", lambda *a, **k: True)
        monkeypatch.setattr(fs, "_log_scan_weights", lambda *a, **k: None)
        monkeypatch.setattr(fs, "log_unified", lambda *a, **k: None)
        # The DB lookup is the duration source; patch it to report 50s.
        monkeypatch.setattr(
            fs, "_track_duration_seconds",
            lambda track, durations: 50.0,
        )

        results = [
            {
                "track_id": "t1",
                "artist": "Muse",
                "album": "Absolution",
                "title": "Hidden Noise",
                "popularity_score": 80.0,
                "final_score": 80.0,
                "single_confidence": "low",
            }
        ]
        fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={},
        )

        star_updates = [
            params for sql, params in session.executed
            if "UPDATE tracks SET stars" in str(sql)
        ]
        assert star_updates, "expected a star rating to be persisted"
        assert star_updates[0]["stars"] == 2


# ---------------------------------------------------------------------------
# Config-page contract (both UI trees)
# ---------------------------------------------------------------------------

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Every configurable option must be surfaced on the Config page in BOTH
#: trees — ``test_site/Pages/config.html`` is served first when the cutover
#: is active, so a change to the live tree alone would be invisible.
_TREES = [
    ("templates/pages/config.html", "static/js/config.js"),
    ("test_site/templates/Pages/config.html", "test_site/static/js/pages/config.js"),
]

_FIELD_IDS = (
    "short_track_star_cap_enabled",
    "short_track_star_cap_max_duration_seconds",
    "short_track_star_cap_max_stars",
)


class TestShortTrackCapConfigPage:
    """Both UI trees render the inputs and collect them into the save payload."""

    async def test_rendered_config_page_carries_the_cap_inputs(self, client):
        """The Jinja must actually render — a template check alone would pass
        on a block the engine never emits."""
        response = await client.get("/config")
        assert response.status_code == 200
        body = await response.get_data(as_text=True)

        for field_id in _FIELD_IDS:
            assert field_id in body, f"rendered /config missing {field_id}"

        assert 'id="short_track_star_cap_max_duration_seconds" placeholder="70"' in body
        assert 'id="short_track_star_cap_max_stars" placeholder="2"' in body

    @pytest.mark.parametrize("html_rel,js_rel", _TREES)
    def test_html_renders_the_cap_inputs(self, html_rel, js_rel):
        with open(os.path.join(_ROOT, html_rel), encoding="utf-8") as f:
            body = f.read()

        for field_id in _FIELD_IDS:
            assert field_id in body, f"{html_rel} missing input {field_id}"

        # Defaults baked into the template match the helper defaults.
        assert 'id="short_track_star_cap_max_duration_seconds" placeholder="70"' in body
        assert 'id="short_track_star_cap_max_stars" placeholder="2"' in body

    @pytest.mark.parametrize("html_rel,js_rel", _TREES)
    def test_js_collects_the_cap_keys(self, html_rel, js_rel):
        with open(os.path.join(_ROOT, js_rel), encoding="utf-8") as f:
            js = f.read()

        expected = {
            "short_track_star_cap_enabled":
                "getChecked('short_track_star_cap_enabled', true)",
            "short_track_star_cap_max_duration_seconds":
                "getValue('short_track_star_cap_max_duration_seconds', '70')",
            "short_track_star_cap_max_stars":
                "getValue('short_track_star_cap_max_stars', '2')",
        }
        for key, fragment in expected.items():
            assert key in js, f"{js_rel} missing key {key}"
            assert fragment in js, f"{js_rel} does not collect {key} via {fragment}"

        # The block must nest under statistics, not float at the top level.
        assert "short_track_star_cap: {" in js
