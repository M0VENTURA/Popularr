"""Advanced full-scan tickboxes select which stages are FORCED.

A forced scan must still fill a stage that has no data even when the stage is
unticked; only ticked stages ignore their stored values.
"""

from __future__ import annotations


class TestStageForcing:
    def test_ticked_stage_is_forced(self):
        from services.popularity.stages.track_stage import _stage_is_forced

        options = {
            "force": True,
            "stage_selection": True,
            "force_singles_detection": True,
            "force_popularity_scoring": False,
        }

        assert _stage_is_forced(options, "force_singles_detection") is True
        assert _stage_is_forced(options, "force_popularity_scoring") is False

    def test_unticked_stage_is_fill_only(self):
        from services.popularity.stages.track_stage import _stage_is_forced

        options = {"force": True, "stage_selection": True}

        assert _stage_is_forced(options, "force_singles_detection") is False
        assert _stage_is_forced(options, "force_cover_detection") is False
        assert _stage_is_forced(options, "force_genre_detection") is False

    def test_old_callers_without_stage_selection_keep_force_all(self):
        from services.popularity.stages.track_stage import _stage_is_forced

        options = {"force": True}

        assert _stage_is_forced(options, "force_singles_detection") is True

    def test_non_forced_scan_never_forces_a_stage(self):
        from services.popularity.stages.track_stage import _stage_is_forced

        options = {
            "force": False,
            "stage_selection": True,
            "force_singles_detection": True,
        }

        assert _stage_is_forced(options, "force_singles_detection") is False


class TestStageOptionsFromForm:
    def test_ticked_box_becomes_force_and_unticked_is_absent(self):
        from routes.scan_routes.artist_album import _stage_options_from_form

        class Form(dict):
            pass

        options = _stage_options_from_form(Form({
            "scan_type": "full",
            "stage_selection": "1",
            "force_singles": "1",
        }))

        assert options["stage_selection"] is True
        assert options["force_singles_detection"] is True
        assert options["force_popularity_scoring"] is False
