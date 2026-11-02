"""Manual MusicBrainz picker: studio/release/type/length-first ordering."""

from __future__ import annotations

from routes.track_routes import _format_mb_recording, _mb_lookup_sort_key


def _recording(
    mbid: str,
    *,
    title: str = "Every You Every Me",
    length: int = 213000,
    video: bool = False,
    release_title: str = "Without You I'm Nothing",
    primary: str = "Album",
    secondary: list[str] | None = None,
    date: str = "1998-10-26",
    disambiguation: str = "",
) -> dict:
    return {
        "id": mbid,
        "title": title,
        "length": length,
        "video": video,
        "disambiguation": disambiguation,
        "artist-credit": [{"name": "Placebo", "artist": {"id": "a", "name": "Placebo"}}],
        "releases": [
            {
                "id": "rel-" + mbid[:4],
                "title": release_title,
                "date": date,
                "country": "GB",
                "status": "Official",
                "release-group": {
                    "primary-type": primary,
                    "secondary-types": secondary or [],
                },
                "media": [{"format": "CD"}],
            }
        ],
    }


def _row(recording: dict, *, expected_title: str = "Every You Every Me", duration=213.0):
    return _format_mb_recording(
        recording,
        expected_title=expected_title,
        expected_artist="Placebo",
        expected_duration_seconds=duration,
    )


class TestFormat:
    def test_studio_release_metadata(self):
        row = _row(_recording("studio"))
        assert row["is_studio"] is True
        assert row["is_live"] is False
        assert row["video"] is False
        assert row["length_match"] is True
        assert row["release_type"] == "Album"
        assert row["album"] == "Without You I'm Nothing"

    def test_live_release_is_marked(self):
        row = _row(
            _recording(
                "live", release_title="Soulmates Never Die",
                primary="Album", secondary=["Live"], date="2004-03-15",
            )
        )
        assert row["is_live"] is True
        assert row["is_studio"] is False
        assert row["release_type"] == "Album · Live"

    def test_video_is_marked_and_last(self):
        row = _row(
            _recording(
                "video", video=True, release_title="Once More With Feeling",
                primary="", date="2004",
            )
        )
        assert row["video"] is True
        assert row["is_studio"] is False
        assert row["release_type"] == "Video"

    def test_extra_title_words_are_flagged(self):
        row = _row(
            _recording("extra", title="Every You Every Me - Remastered 2011"),
        )
        assert row["title_core_match"] is False or row["unrequested_variants"]

    def test_unrequested_live_variant_is_flagged(self):
        row = _row(_recording("live-title", title="Every You Every Me (Live)"))
        assert row["title_core_match"] is True
        assert "live" in row["unrequested_variants"]

    def test_requested_live_intent(self):
        row = _row(
            _recording("live", title="Every You Every Me (Live)", secondary=["Live"]),
            expected_title="Every You Every Me (Live)",
        )
        assert row["requested_is_live"] is True
        assert row["is_live"] is True


class TestOrdering:
    def test_studio_exact_beats_live_and_video(self):
        studio = _row(_recording("studio"))
        live = _row(
            _recording("live", secondary=["Live"], date="2004-03-15"),
        )
        video = _row(_recording("video", video=True, date="2004"))
        extra = _row(
            _recording("extra", title="Every You Every Me - Remastered 2011")
        )
        ordered = sorted([video, extra, live, studio], key=_mb_lookup_sort_key)
        assert [row["mbid"] for row in ordered] == ["studio", "live", "extra", "video"]

    def test_requested_live_prefers_the_live_recording(self):
        studio = _row(_recording("studio"))
        live = _row(
            _recording(
                "live", title="Every You Every Me (Live)", secondary=["Live"],
            ),
            expected_title="Every You Every Me (Live)",
        )
        ordered = sorted([studio, live], key=_mb_lookup_sort_key)
        assert ordered[0]["mbid"] == "live"

    def test_matching_length_wins_between_same_title_studio_recordings(self):
        exact = _row(_recording("exact", length=213000))
        wrong = _row(_recording("wrong", length=240000))
        ordered = sorted([wrong, exact], key=_mb_lookup_sort_key)
        assert ordered[0]["mbid"] == "exact"
