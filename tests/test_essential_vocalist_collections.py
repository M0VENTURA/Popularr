"""Essential VOCALIST collections: cross-project thresholds + vocal confirmation.

Requirements:

* 15 distinct 4–5★ recordings
* 4 distinct release groups (compilations do not add breadth)
* 2 projects with ≥5 qualifying tracks each, OR 3 artist projects
* ≥5 tracks and ≥25% outside the dominant project
* confirmed recording-level vocals (MusicBrainz performer/vocal relations)
"""

from __future__ import annotations

from services.playlists import playlist_taxonomy as taxonomy
from services.popularity.stages import finalise_stage as fs


def _row(
    *,
    project: str,
    group: str,
    title: str,
    artist: str | None = None,
    vocalists: list[str] | None = None,
    stars: int = 5,
    compilation: int = 0,
    album_artist: str | None = None,
) -> dict:
    return {
        "id": f"{project}:{title}",
        "title": title,
        "album": f"{project} Album",
        "artist": artist or project,
        "album_artist": album_artist or project,
        "stars": stars,
        "is_live": 0,
        "is_compilation": compilation,
        "musicbrainz_releasegroupid": group,
        "popularity_score": 50.0,
        "final_score": 50.0,
        "vocalists": __import__("json").dumps(vocalists or [project]),
    }


def _meets(rows: list[dict]) -> bool:
    cfg = {
        "essential_vocalists_min_tracks": 15,
        "essential_vocalists_min_release_groups": 4,
        "essential_vocalists_min_projects_with_five": 2,
        "essential_vocalists_min_project_tracks": 5,
        "essential_vocalists_min_projects": 3,
        "essential_vocalists_min_outside_dominant": 5,
        "essential_vocalists_min_outside_ratio": 0.25,
    }
    return fs._essential_vocalist_meets_thresholds(rows, cfg)


def _pool(
    counts: dict[str, int],
    *,
    groups: int = 4,
    vocalist: str = "Vocalist",
) -> list[dict]:
    rows: list[dict] = []
    index = 0
    for project, count in counts.items():
        for number in range(count):
            rows.append(
                _row(
                    project=project,
                    group=f"rg-{index % groups}",
                    title=f"{project} Song {number}",
                    vocalists=[project, vocalist],
                )
            )
            index += 1
    return rows


class TestTheCrossProjectThresholds:
    def test_fifteen_tracks_across_three_projects_qualify(self) -> None:
        assert _meets(_pool({"A": 5, "B": 5, "C": 5}))

    def test_two_projects_with_five_each_qualify(self) -> None:
        # 10 in two deep projects + 5 one-offs = 15; the alternative branch
        # (3 projects) is not satisfied, the depth branch is.
        assert _meets(_pool({"A": 5, "B": 5, "C": 2, "D": 2, "E": 1}))

    def test_fourteen_tracks_fail_the_track_floor(self) -> None:
        assert not _meets(_pool({"A": 5, "B": 5, "C": 4}))

    def test_three_release_groups_fail_breadth(self) -> None:
        assert not _meets(_pool({"A": 5, "B": 5, "C": 5}, groups=3))

    def test_compilations_do_not_add_release_breadth(self) -> None:
        rows = _pool({"A": 5, "B": 5, "C": 5}, groups=4)
        for index, row in enumerate(rows):
            if index < 5:
                # Only THREE non-compilation release groups.
                row["musicbrainz_releasegroupid"] = f"rg-{index % 3}"
            else:
                row["is_compilation"] = 1
                row["musicbrainz_releasegroupid"] = f"comp-{index}"
        assert not _meets(rows)

    def test_one_dominant_project_fails_cross_project_depth(self) -> None:
        assert not _meets(_pool({"A": 14, "B": 1}))

    def test_fewer_than_five_outside_the_dominant_project_fails(self) -> None:
        # Three projects (cross-project passes) but only 2+2 outside the
        # dominant 11-track project.
        assert not _meets(_pool({"A": 11, "B": 2, "C": 2}))


class TestVocalParticipation:
    def test_confirmed_vocalists_are_authoritative(self) -> None:
        row = _row(project="Host", group="rg", title="Song", vocalists=["Melissa Bonny"])
        assert fs._row_is_vocalist(row, "Melissa Bonny")
        assert not fs._row_is_vocalist(row, "Someone Else")

    def test_a_guest_credit_is_used_when_the_row_was_never_scanned(self) -> None:
        row = _row(
            project="Host",
            group="rg",
            title="Song",
            artist="Host feat. Melissa Bonny",
            vocalists=[],
        )
        row["vocalists"] = ""
        assert fs._row_is_vocalist(row, "Melissa Bonny")

    def test_confirmed_relations_override_the_credit_string(self) -> None:
        """A title credit cannot invent a vocalist when MB says who sang."""
        row = _row(
            project="Host",
            group="rg",
            title="Song (feat. Melissa Bonny)",
            artist="Host",
            vocalists=["Host"],
        )
        assert not fs._row_is_vocalist(row, "Melissa Bonny")

    def test_the_vocalists_own_album_artist_counts(self) -> None:
        row = _row(project="Melissa Bonny", group="rg", title="Solo", vocalists=["Melissa Bonny"])
        assert fs._row_is_vocalist(row, "Melissa Bonny")

    def test_confirmed_relations_override_the_album_artist(self) -> None:
        """A band album is not a vocalist credit.

        Confirmed relations list the singer; the album artist (the band) must
        not therefore create a "Lacuna Coil - Essential Vocalist Collection".
        """
        row = _row(
            project="Lacuna Coil",
            group="rg",
            title="Song",
            vocalists=["Cristina Scabbia"],
        )
        assert fs._row_is_vocalist(row, "Cristina Scabbia")
        assert not fs._row_is_vocalist(row, "Lacuna Coil")

    def test_unconfirmed_credits_do_not_create_vocalist_candidates(self) -> None:
        rows = [
            _row(project="Pantera", group="rg", title="Song", vocalists=[]),
        ]
        rows[0]["vocalists"] = ""
        assert fs._vocalist_candidates_from_rows(rows) == {}

    def test_a_band_name_is_not_a_candidate_from_its_own_credit(self) -> None:
        rows = [
            _row(project="Pantera", group="rg", title="Song", vocalists=["Pantera"]),
        ]
        assert fs._vocalist_candidates_from_rows(rows) == {}

    def test_a_singer_on_a_band_track_is_a_candidate(self) -> None:
        rows = [
            _row(
                project="Lacuna Coil",
                group="rg",
                title="Song",
                vocalists=["Cristina Scabbia"],
            ),
        ]
        assert fs._vocalist_candidates_from_rows(rows) == {
            "cristina scabbia": "Cristina Scabbia"
        }

    def test_instrumental_titles_are_not_vocal_tracks(self) -> None:
        row = _row(project="Melissa Bonny", group="rg", title="Intro (Instrumental)")
        assert not fs._row_is_vocalist(row, "Melissa Bonny")


class TestMusicBrainzVocalRelations:
    def test_a_vocal_performer_relation_confirms_the_name(self) -> None:
        from services.enrichment.musicbrainz_service import _vocalists_from_recording

        got = _vocalists_from_recording(
            {
                "relations": [
                    {
                        "type": "performer",
                        "attributes": ["lead vocals"],
                        "artist": {"name": "Melissa Bonny"},
                    }
                ]
            }
        )
        assert got == ["Melissa Bonny"]

    def test_an_instrumental_performer_relation_is_not_a_vocalist(self) -> None:
        from services.enrichment.musicbrainz_service import _vocalists_from_recording

        got = _vocalists_from_recording(
            {
                "relations": [
                    {
                        "type": "performer",
                        "attributes": ["electric guitar"],
                        "artist": {"name": "Guitarist"},
                    }
                ],
                "artist-credit": [
                    {"artist": {"name": "Host"}, "name": "Host"}
                ],
            }
        )
        assert got == []

    def test_without_relations_only_a_person_credit_is_a_fallback(self) -> None:
        from services.enrichment.musicbrainz_service import _vocalists_from_recording

        got = _vocalists_from_recording(
            {
                "relations": [],
                "artist-credit": [
                    {
                        "artist": {"name": "Host", "type": "Group"},
                        "name": "Host",
                    },
                    {
                        "artist": {"name": "Melissa Bonny", "type": "Person"},
                        "name": "Melissa Bonny",
                    },
                ],
            }
        )
        assert got == ["Melissa Bonny"]

    def test_a_group_credit_is_not_a_vocalist_without_relations(self) -> None:
        from services.enrichment.musicbrainz_service import _vocalists_from_recording

        got = _vocalists_from_recording(
            {
                "relations": [],
                "artist-credit": [
                    {"artist": {"name": "Pantera", "type": "Group"}, "name": "Pantera"}
                ],
            }
        )
        assert got == []


class TestThePlaylistNameIsManaged:
    def test_the_vocalist_template_is_recognised(self) -> None:
        assert (
            taxonomy.classify_managed_playlist(
                "Melissa Bonny - Essential Vocalist Collection"
            )
            == taxonomy.ESSENTIAL_VOCALIST
        )

    def test_the_per_artist_template_is_still_recognised(self) -> None:
        assert (
            taxonomy.classify_managed_playlist("Powerwolf - Essential Collection")
            == taxonomy.ESSENTIAL
        )

    def test_the_vocalist_name_reverses_to_the_name(self) -> None:
        assert (
            taxonomy.managed_name_value(
                "Melissa Bonny - Essential Vocalist Collection",
                taxonomy.ESSENTIAL_VOCALIST,
            )
            == "Melissa Bonny"
        )


class TestConfigDefaults:
    def test_the_requirement_defaults_are_present(self, monkeypatch) -> None:
        from helpers import config_helpers as ch

        monkeypatch.setattr(ch, "get_config", lambda: {"playlists": {}})
        cfg = ch.get_playlists_config()
        assert cfg["essential_vocalists_enabled"] is True
        assert cfg["essential_vocalists_min_tracks"] == 15
        assert cfg["essential_vocalists_min_release_groups"] == 4
        assert cfg["essential_vocalists_min_projects_with_five"] == 2
        assert cfg["essential_vocalists_min_project_tracks"] == 5
        assert cfg["essential_vocalists_min_projects"] == 3
        assert cfg["essential_vocalists_min_outside_dominant"] == 5
        assert cfg["essential_vocalists_min_outside_ratio"] == 0.25
        assert cfg["essential_vocalists_max_tracks"] == 50
        assert (
            cfg["essential_vocalists_name_template"]
            == "{artist} - Essential Vocalist Collection"
        )

    def test_a_partial_override_keeps_the_other_defaults(self, monkeypatch) -> None:
        from helpers import config_helpers as ch

        monkeypatch.setattr(
            ch,
            "get_config",
            lambda: {
                "playlists": {
                    "essential_vocalists_min_tracks": 20,
                    "essential_vocalists_enabled": "false",
                }
            },
        )
        cfg = ch.get_playlists_config()
        assert cfg["essential_vocalists_min_tracks"] == 20
        assert cfg["essential_vocalists_enabled"] is False
        assert cfg["essential_vocalists_min_release_groups"] == 4


class TestTheConfigPageExposesTheBlock:
    def test_both_trees_render_and_collect_every_key(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        keys = (
            "essential_vocalists_enabled",
            "essential_vocalists_include_featured",
            "essential_vocalists_name_template",
            "essential_vocalists_min_tracks",
            "essential_vocalists_min_release_groups",
            "essential_vocalists_min_projects_with_five",
            "essential_vocalists_min_project_tracks",
            "essential_vocalists_min_projects",
            "essential_vocalists_min_outside_dominant",
            "essential_vocalists_min_outside_ratio",
            "essential_vocalists_max_tracks",
        )
        for template_name, js_name in (
            ("templates/pages/config.html", "static/js/config.js"),
            (
                "test_site/templates/Pages/config.html",
                "test_site/static/js/pages/config.js",
            ),
        ):
            html = (root / template_name).read_text(encoding="utf-8")
            js = (root / js_name).read_text(encoding="utf-8")
            for key in keys:
                assert f'id="playlists_{key}"' in html, (template_name, key)
                assert f"playlists_{key}" in js, (js_name, key)
