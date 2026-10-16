"""Regression tests: album-level MusicBrainz tags must be written to MP3
files (as TXXX frames) when the album is saved or an MBID is applied.

Previously ``write_id3_tags`` silently dropped fields like ``releasetype``,
``releasestatus``, ``releasecountry``, ``originalyear``, ``tracktotal``,
``disctotal``, ``media`` and ``label`` — so a track that never received MB
enrichment stayed missing the album-level tags and Navidrome split the
album even though Popularr showed it merged.

Also pins: ``apply_mbid_to_album`` fans the MB album ID out to the audio
FILES (not just the DB), which is what Navidrome reads.
"""

from __future__ import annotations


class _FakeTXXX:
    def __init__(self, encoding, desc, text):
        self.encoding = encoding
        self.desc = desc
        self.text = text


class _FakeAudio:
    def __init__(self, path, tags=None):
        self.tags = tags if tags is not None else _FakeTags()
        self.saved = False

    def add_tags(self):
        self.tags = _FakeTags()

    def save(self, v2_version=3):
        self.saved = True


class _FakeTags(dict):
    def add(self, frame):
        self[frame.desc] = frame

    def delall(self, key):
        for k in list(self.keys()):
            if k.startswith(str(key)):
                del self[k]


class _RecordingTags(dict):
    """A tag object that records every frame the writer adds.

    ⚠️ ``add`` must cope with BOTH kinds of frame the writer uses: ``TXXX``
    frames carry ``desc`` (the name these tests assert on), while text frames
    like ``TIT2`` carry only ``FrameID``. Keying on ``desc`` alone raised
    AttributeError — and these tests never noticed, because a NameError on the
    line above killed the writer's primary path, so ``add`` was never reached
    with a text frame. Fixing that bug exposed this one.
    """

    def __init__(self, sink: dict):
        super().__init__()
        self._sink = sink

    def add(self, frame):
        key = getattr(frame, "desc", None) or getattr(frame, "FrameID", None)
        self._sink[key] = frame.text

    def delall(self, key):
        target = str(key).lower().replace(" ", "").replace("_", "")
        for existing in list(self._sink.keys()):
            if existing and existing.lower().replace(" ", "").replace("_", "") == target:
                self._sink.pop(existing, None)


class TestTypeAliasesExistAtRuntime:
    """⚠️ A name imported only under TYPE_CHECKING is a NameError at runtime.

    ``typing.cast`` is a real CALL, so its first argument is evaluated — and
    ``from __future__ import annotations`` does not defer it, because it is not
    an annotation. ``MP3Type`` was imported only under TYPE_CHECKING and omitted
    from the runtime ``else`` branch, so every ``write_id3_tags`` call raised
    NameError on its first line. A bare ``except Exception`` swallowed it and the
    fallback masked it by working, so the intended MP3 path was dead and nothing
    anywhere said so. This walks the source and asserts every such name resolves.
    """

    def test_every_name_used_in_a_cast_exists_at_runtime(self):
        import re
        from pathlib import Path

        from services.metadata import tag_file_service as tfs

        source = Path(tfs.__file__).read_text(encoding="utf-8")
        # Only simple identifiers: `cast(dict[str, Any], x)` cannot be checked
        # this way and is not the failure mode.
        names = set(re.findall(r"\bcast\(\s*([A-Za-z_]\w*)\s*,", source))
        assert names, "no cast() calls found — has the extraction broken?"

        missing = sorted(name for name in names if not hasattr(tfs, name))
        assert not missing, (
            "these are used in a runtime cast() but do not exist at runtime "
            f"(TYPE_CHECKING-only imports?): {missing}"
        )

    def test_the_mp3_type_alias_is_the_specific_regression(self):
        """Names the regression directly so its return is unmistakable."""
        from services.metadata import tag_file_service as tfs

        assert hasattr(tfs, "MP3Type")


class TestId3WritesAlbumMbFieldsAsTxxx:
    def test_musicbrainz_albumtype_written_as_txxx(self, monkeypatch):
        from services.metadata import tag_file_service as tfs

        frames = {}
        audio = _FakeAudio("/fake/song.mp3", _RecordingTags(frames))
        monkeypatch.setattr(tfs, "MP3", lambda path, ID3=None: audio)
        monkeypatch.setattr(tfs, "ID3", object)
        monkeypatch.setattr(tfs, "TXXX", _FakeTXXX)

        assert tfs.write_id3_tags(
            "/fake/song.mp3",
            {"title": "Song", "musicbrainz_albumtype": "album"},
        ) is True

        # The album-type must land as a TXXX:RELEASETYPE frame.
        assert frames.get("RELEASETYPE") == ["album"]

    def test_release_country_and_originalyear_written(self, monkeypatch):
        from services.metadata import tag_file_service as tfs

        frames = {}
        audio = _FakeAudio("/fake/song.mp3", _RecordingTags(frames))
        monkeypatch.setattr(tfs, "MP3", lambda path, ID3=None: audio)
        monkeypatch.setattr(tfs, "ID3", object)
        monkeypatch.setattr(tfs, "TXXX", _FakeTXXX)

        assert tfs.write_id3_tags(
            "/fake/song.mp3",
            {
                "title": "Song",
                "releasecountry": "XW",
                "originalyear": "2026",
                "tracktotal": "17",
                "media": "Digital Media",
            },
        ) is True

        assert frames.get("RELEASECOUNTRY") == ["XW"]
        assert frames.get("ORIGINALYEAR") == ["2026"]
        assert frames.get("TRACKTOTAL") == ["17"]
        assert frames.get("MEDIA") == ["Digital Media"]


class TestApplyMbidToAlbumFansOutToFiles:
    def test_file_tags_written_with_album_mbid(self, monkeypatch):
        """apply_mbid_to_album must write the MB album ID to the audio FILES
        (Navidrome reads file tags), not just the DB."""
        from services.metadata import album_service as asvc
        from services.metadata import tag_file_service as tfs
        from db import engine as db_engine

        written = []

        def _fake_update_album_mbid_fields(**kwargs):
            return 2  # 2 tracks updated in DB

        def _fake_update_file_tags(path, tags):
            written.append((path, tags))
            return True

        monkeypatch.setattr(asvc, "update_album_mbid_fields", _fake_update_album_mbid_fields)
        # The service imports these INSIDE the function — patch the source
        # modules so the local imports resolve to the fakes.
        monkeypatch.setattr(tfs, "update_file_tags", _fake_update_file_tags)
        monkeypatch.setattr(tfs, "resolve_music_file_path", lambda p: p if p.startswith("/music/") else f"/music/{p}")

        class _FakeResult:
            def __init__(self):
                self._rows = [("/music/Stray Kids/song1.mp3",), ("/music/Stray Kids/song2.mp3",)]

            def fetchall(self):
                return self._rows

        class _FakeSession:
            def execute(self, *a, **k):
                return _FakeResult()

        import contextlib

        @contextlib.contextmanager
        def _fake_db_session(*a, **k):
            yield _FakeSession()

        monkeypatch.setattr(db_engine, "db_session", _fake_db_session)

        result = asvc.apply_mbid_to_album(
            artist="Stray Kids", album="SKZ-REPLAY 2026 Pt.1",
            mbid="729beb45-1c4c-4da9-816a-fc4007ff7507",
            rg_mbid="f3efc4af-6e09-4ea6-b059-15f3b2852fec",
            cover_url="",
        )

        assert result["success"] is True
        assert result["rows_updated"] == 2
        # Both audio files got the MB album ID + release-group ID written.
        assert len(written) == 2
        for _path, tags in written:
            assert tags.get("musicbrainz_albumid") == "729beb45-1c4c-4da9-816a-fc4007ff7507"
            assert tags.get("musicbrainz_releasegroupid") == "f3efc4af-6e09-4ea6-b059-15f3b2852fec"


class TestCoverConvention:
    """A cover detected via the release picker must be renamed to
    'Title (Original Artist Cover)' and tagged with the 'Cover' genre —
    the SAME convention as the standalone cover-detection area.

    ⚠️ The builder lives in ``db.repositories.cover_detection_repository``.
    This test used to call ``CoverDetector._build_cover_title``, which has never
    existed, so the convention was pinned by NOTHING while three places emit it:
    that helper, plus two inline copies in ``routes/ui_routes.py``.

    ⚠️ The two route copies use a LOOSER guard than this helper: they skip the
    rename when the title CONTAINS ``"cover)"``, whereas the helper skips only
    when it ENDS with ``"(… Cover)"``. So ``"Song (Band Cover) [Remaster]"`` is
    left alone by the routes and would be re-annotated by the helper. The
    difference is deliberate-not-yet-reconciled: unifying them changes behaviour
    on that shape, so pin this helper and leave the copies visible rather than
    silently editing them.
    """

    def test_cover_title_build_convention(self):
        """'{title} ({original_artist} Cover)' is the canonical rename."""
        from db.repositories.cover_detection_repository import _build_cover_title

        assert _build_cover_title("Valhalla Calling", "Miracle of Sound") == (
            "Valhalla Calling (Miracle of Sound Cover)"
        )

    def test_an_already_annotated_title_is_left_alone(self):
        """Re-running must not stack suffixes into '(X Cover) (Y Cover)'."""
        from db.repositories.cover_detection_repository import _build_cover_title

        once = _build_cover_title("Valhalla Calling", "Miracle of Sound")
        assert _build_cover_title(once, "Somebody Else") == once

    def test_without_an_original_artist_the_title_is_unchanged(self):
        from db.repositories.cover_detection_repository import _build_cover_title

        assert _build_cover_title("Valhalla Calling", None) == "Valhalla Calling"
        assert _build_cover_title("Valhalla Calling", "") == "Valhalla Calling"

    def test_update_file_metadata_writes_cover_and_writer(self, monkeypatch):
        """update_file_metadata must write writer / is_cover /
        original_cover_artist / work MBID / MB genres to the file tags."""
        from services.metadata import tag_file_service as tfs

        written = {}

        def _fake_write_tags_to_file(path, tags):
            written.update(tags)
            return True

        monkeypatch.setattr(tfs, "write_tags_to_file", _fake_write_tags_to_file)

        assert tfs.update_file_metadata(
            "/fake/song.mp3",
            {
                "title": "Valhalla Calling",
                "artist": "Feuerschwanz",
                "album": "Warriors",
                "album_artist": "Feuerschwanz",
                "year": "2024",
                "track_number": "12",
                "recording_mbid": "3893fd62-7e54-4532-abb1-4024936418a4",
                "release_mbid": "217ab767-2c30-44dd-9f68-cc44960a8b7d",
                "writer": "Gavin Dunne",
                "is_cover": 1,
                "original_cover_artist": "Miracle of Sound",
                "work_mbid": "bb840885-a39d-4a2a-ac84-76684d5edd89",
                "musicbrainz_genres": "Folk Metal",
            },
        ) is True

        assert written.get("writer") == "Gavin Dunne"
        assert written.get("is_cover") == "1"
        assert written.get("original_cover_artist") == "Miracle of Sound"
        assert written.get("musicbrainz_workid") == "bb840885-a39d-4a2a-ac84-76684d5edd89"
        assert written.get("musicbrainz_genres") == "Folk Metal"
