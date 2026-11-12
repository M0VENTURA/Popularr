"""A song's WRITER is not its "original performer".

REPORTED
--------
Every track of Flaw's own 2019 album *Vol. IV: Because of the Brave* was listed
as a cover, all naming the same artist:

    Persistence (Janet Gardner Cover)
    Walk the Line (Janet Gardner Cover)
    Because of the Brave (Janet Gardner Cover)
    ... all ten tracks

Those are Flaw's own recordings. Janet Gardner is credited as a WRITER on the
works — MusicBrainz's work ``artist-credit`` carries the COMPOSER(s) of the
composition.

ROOT CAUSE
----------
``_flatten_release`` compared the performing artist with the work's
``artist-credit`` and set ``is_cover`` + ``original_cover_artist`` whenever they
differed. That question is "did the performer WRITE this song?", not "is this a
cover", so EVERY track whose songwriter is not the performer was mislabelled —
an entire band's album became "covers of their songwriter".

The authoritative signal is MusicBrainz's ``cover`` ATTRIBUTE on the recording's
performance relation (``cover recording of``), which the same function already
reads into ``cover_work_mbid`` / ``is_cover_relation``.
"""

from __future__ import annotations

from services.enrichment.musicbrainz_service import _flatten_release


def _work(title: str, work_artist: str) -> dict:
    return {
        "id": "work-" + title.replace(" ", "-"),
        "title": title,
        "artist-credit": [
            {"name": work_artist, "artist": {"id": "writer-1", "name": work_artist}}
        ],
    }


def _release(tracks: list[dict]) -> dict:
    media_tracks = []
    for index, spec in enumerate(tracks, start=1):
        media_tracks.append({
            "position": str(index),
            "title": spec["title"],
            "recording": {
                "id": f"rec-{index}",
                "title": spec["title"],
                "artist-credit": [
                    {"name": spec.get("performer", "Flaw"),
                     "artist": {"id": "performer-1",
                                "name": spec.get("performer", "Flaw")}}
                ],
                "relations": spec.get("relations", []),
            },
        })
    return {
        "id": "rel-1",
        "title": "Vol IV Because of the Brave",
        "artist-credit": [{"name": "Flaw", "artist": {"id": "performer-1", "name": "Flaw"}}],
        "release-group": {"id": "rg-1", "title": "Vol IV Because of the Brave"},
        "media": [{"position": "1", "tracks": media_tracks}],
    }


def _performance(work: dict, *, cover: bool = False) -> dict:
    relation = {"type": "performance", "work": work}
    if cover:
        relation["attributes"] = ["cover"]
    return relation


class TestASongwriterIsNotAnOriginalPerformer:
    def test_the_flaw_album_is_not_a_set_of_janet_gardner_covers(self):
        """The exact reported case: a work credited to the songwriter."""
        release = _release([
            {"title": "Persistence", "relations": [_performance(_work("Persistence", "Janet Gardner"))]},
            {"title": "Walk the Line", "relations": [_performance(_work("Walk the Line", "Janet Gardner"))]},
            {"title": "Because of the Brave", "relations": [_performance(_work("Because of the Brave", "Janet Gardner"))]},
        ])

        flat = _flatten_release(release, "rel-1")

        for track in flat["tracks"]:
            assert not track.get("is_cover"), (
                f"{track['title']!r} was marked a cover because its WRITER "
                f"({track.get('work_artist')!r}) differs from the performer — "
                "that is not what 'cover' means"
            )
            assert not track.get("original_cover_artist")

    def test_the_work_credit_is_still_published_for_the_explanation(self):
        """The review's title bar still needs the work credit to explain itself."""
        release = _release([
            {"title": "Persistence",
             "relations": [_performance(_work("Persistence", "Janet Gardner"))]},
        ])
        track = _flatten_release(release, "rel-1")["tracks"][0]
        assert track["work_artist"] == "Janet Gardner"
        assert not track.get("work_artist_is_performer")

    def test_a_work_credited_to_the_performer_is_flagged_as_self_written(self):
        """Positive evidence the performer wrote it — used to explain a verdict."""
        release = _release([
            {"title": "Own Song",
             "relations": [_performance(_work("Own Song", "Flaw"))]},
        ])
        track = _flatten_release(release, "rel-1")["tracks"][0]
        assert track.get("work_artist_is_performer") is True
        assert not track.get("is_cover")


class TestTheAuthoritativeCoverSignalStillWorks:
    def test_the_cover_attribute_still_marks_a_cover(self):
        """A real cover keeps working — ``cover recording of`` is the signal."""
        release = _release([
            {"title": "Hurt",
             "relations": [_performance(_work("Hurt", "Trent Reznor"), cover=True)]},
        ])
        track = _flatten_release(release, "rel-1")["tracks"][0]
        assert track.get("is_cover") is True

    def test_a_cover_attribute_is_scoped_to_its_own_track(self):
        """One genuine cover must not mark its album-mates."""
        release = _release([
            {"title": "Own Song",
             "relations": [_performance(_work("Own Song", "Flaw"))]},
            {"title": "Hurt",
             "relations": [_performance(_work("Hurt", "Trent Reznor"), cover=True)]},
        ])
        tracks = _flatten_release(release, "rel-1")["tracks"]
        assert not tracks[0].get("is_cover")
        assert tracks[1].get("is_cover") is True

    def test_no_work_relation_means_no_verdict_either_way(self):
        release = _release([{"title": "Instrumental", "relations": []}])
        track = _flatten_release(release, "rel-1")["tracks"][0]
        assert not track.get("is_cover")
        assert "work_artist" not in track
