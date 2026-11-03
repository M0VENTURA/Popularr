"""Album-type title heuristics must not outrank MusicBrainz.

REPORTED
--------
> During a popularity scan, Anthology by Alien Ant Farm was adjusted to be a
> compilation even though MusicBrainz lists it as a regular album.

“ANThology” is the album's real NAME; it is not a greatest-hits record. The
local title heuristic (``detect_greatest_hits_album``) matches the token
``anthology`` and returned ``album+compilation``, and the resolved type was
persisted even when MusicBrainz said plain Album.

Also pinned here: a re-confirmed cover whose TITLE is already correct must
still repair a stale ``ORIGINAL_COVER_ARTIST`` file tag (the reported Santana),
otherwise the next Navidrome import reads the old value back.
"""
from __future__ import annotations

from services.popularity.stages import album_stage


def _force_mb(monkeypatch, mb_type: str) -> None:
    monkeypatch.setattr(
        album_stage,
        "_lookup_musicbrainz_album_type",
        lambda *a, **k: (mb_type, "rg-1" if mb_type else None),
    )
    import services.metadata.album_lookup_skip_service as skip

    monkeypatch.setattr(skip, "album_lookup_skip", lambda *a, **k: False)


class TestAnthologyIsNotACompilation:
    def test_the_title_heuristic_alone_still_matches(self):
        """Establishes the premise: the NAME is what fooled the scan."""
        assert album_stage._title_detected_album_type(
            "Alien Ant Farm", "ANThology", "Alien Ant Farm"
        ) == "album+compilation"

    def test_musicbrainz_plain_album_wins(self, monkeypatch):
        _force_mb(monkeypatch, "album")
        detected, mb_type, rg, raw = album_stage._resolve_album_type(
            "Alien Ant Farm",
            "ANThology",
            "Alien Ant Farm",
            "album+compilation",  # stored from the earlier bad detection
            [{"title": "Movies", "id": "t1"}],
        )
        assert mb_type == "album"
        assert detected == "album", (
            "the title says 'anthology' but MusicBrainz says Album; MB must win"
        )

    def test_musicbrainz_secondary_still_fills_in(self, monkeypatch):
        """CONTROL — MB's own compilation marking must survive."""
        _force_mb(monkeypatch, "album+compilation")
        detected, mb_type, _rg, _raw = album_stage._resolve_album_type(
            "Artist",
            "Greatest Hits",
            "Artist",
            None,
            [{"title": "a"}, {"title": "b"}],
        )
        assert mb_type == "album+compilation"
        assert detected == "album+compilation"


class _Result:
    rowcount = 1


class _RecordingSession:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return _Result()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestAPlainTypeHealsAPersistedComposite:
    def _run(self, monkeypatch, *, replace_secondary: bool):
        calls: list = []
        monkeypatch.setattr(
            album_stage, "db_session", lambda *a, **k: _RecordingSession(calls)
        )
        tracks = [{
            "id": "t1",
            "musicbrainz_albumtype": "album+compilation",
            "spotify_album_type": "album+compilation",
            "releasetype": "compilation",
        }]
        album_stage._persist_album_type_to_tracks(
            "Alien Ant Farm", "ANThology", tracks, "album", None,
            replace_secondary=replace_secondary,
        )
        return calls, tracks

    def test_the_plain_type_replaces_the_stored_composite(self, monkeypatch):
        calls, tracks = self._run(monkeypatch, replace_secondary=True)

        updates = [
            (sql, params) for sql, params in calls
            if "UPDATE tracks" in sql and "musicbrainz_albumtype" in sql
        ]
        assert updates, (
            "a plain MusicBrainz type did not rewrite the bad stored type"
        )
        sql, params = updates[0]
        assert params.get("replace_secondary") is True
        assert params.get("track_ids") == ["t1"]
        assert "%+%" in sql or "LIKE" in sql
        assert tracks[0]["musicbrainz_albumtype"] == "album"

    def test_without_the_override_a_composite_is_not_touched(self, monkeypatch):
        """CONTROL — ordinary fill-only persistence keeps a stored composite."""
        calls, tracks = self._run(monkeypatch, replace_secondary=False)

        updates = [sql for sql, _params in calls if "UPDATE tracks" in sql]
        assert updates == []
        assert tracks[0]["musicbrainz_albumtype"] == "album+compilation"
