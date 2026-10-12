"""The scan promotes the release tracklist title onto the track, and the
file tag follows.

Reported: *"It also hadn't updated the title even though it was the title
given from the release ID. All File Metadata has been enabled to update the
tags for the files from the metadata."*

Root cause, three connected gaps:

1. ``_resolve_track_mb_metadata`` learned the album's OWN release tracklist
   title for each position (e.g. "My First and Last (Chinese version)" on an
   album whose local file says the plain "My First and Last") but wrote only
   the review column ``musicbrainz_title`` — never the actual ``title``
   column, so the DB kept the local spelling.
2. ``sync_album_file_tags`` fills TITLE only if missing — a wrong existing
   title was never rewritten, so the FILE kept the local spelling even when
   the DB was corrected.
3. ``apply-mb-field`` (the Compare/Apply UI) wrote the DB but never the file.

Fixes pinned here:

- A batch-supplied (album-release) title that differs from the local one is
  promoted to ``payload[\"title\"]``, gated on: not frozen, not ignore-listed
  (``mb_ignored_fields``), meaningful difference, and
  ``metadata_update.apply_during_scan`` (recommend-only mode does not apply).
- ``sync_album_file_tags(corrected_title_ids=...)`` rewrites the TITLE tag
  for exactly those ids, even when the tag exists.
- ``apply-mb-field`` writes the title to the file too.
"""

from __future__ import annotations

import ast
import io

import pytest


def _batch_key(artist: str, title: str, disc=1, track=1):
    from services.popularity.popularity_sources import album_recording_batch_key

    return album_recording_batch_key(artist, title, disc, track)


def _mbid(seed: int) -> str:
    return f"{seed:08d}-0000-0000-0000-{seed:012d}"


# ---------------------------------------------------------------------------
# 1. The scan promotes the release title to the actual title column
# ---------------------------------------------------------------------------

class TestReleaseTitleIsPromoted:
    def test_batch_title_different_from_local_is_promoted(self, monkeypatch):
        from services.popularity.stages import track_stage

        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(track_stage, "get_shared_mb_service",
                            lambda: pytest.fail("batch hit must not search"))
        monkeypatch.setattr(track_stage, "_metadata_update_allowed", lambda: True)

        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={
                "title": "My First and Last",
                "artist": "NCT DREAM",
                "recording_mbid": "",
                "musicbrainz_genres": '["K-pop"]',
                "disc_number": 1,
                "track_number": 3,
            },
            track_title="My First and Last",
            track_artist="NCT DREAM",
            frozen_track=False,
            force_meta=False,
            options={
                "mb_batch_metadata": {
                    _batch_key("nct dream", "My First and Last", 1, 3): {
                        "recording_mbid": _mbid(3),
                        "title": "My First and Last (Chinese version)",
                        # position (track 3, disc 1) + verified length = confirmed
                        "_title_match_confirmed": True,
                    }
                }
            },
        )

        assert result["payload"]["title"] == "My First and Last (Chinese version)", (
            "the release tracklist title must be promoted to the actual title "
            "column — storing it only as musicbrainz_title leaves the wrong "
            "local title in the DB and the file"
        )
        assert result["payload"]["musicbrainz_title"] == "My First and Last (Chinese version)"

    @pytest.mark.parametrize("gate", ["frozen", "ignored", "recommend_only"])
    def test_promotion_is_gated(self, monkeypatch, gate):
        from services.popularity.stages import track_stage

        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(track_stage, "get_shared_mb_service",
                            lambda: pytest.fail("batch hit must not search"))
        monkeypatch.setattr(track_stage, "_metadata_update_allowed",
                            lambda: gate != "recommend_only")

        track = {
            "title": "My First and Last",
            "artist": "NCT DREAM",
            "recording_mbid": "",
            "musicbrainz_genres": '["K-pop"]',
            "disc_number": 1,
            "track_number": 3,
        }
        if gate == "frozen":
            frozen = True
            ignored = ""
        elif gate == "ignored":
            frozen = False
            ignored = '["title"]'
        else:
            frozen = False
            ignored = ""
        track["mb_ignored_fields"] = ignored

        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track=track,
            track_title="My First and Last",
            track_artist="NCT DREAM",
            frozen_track=frozen,
            force_meta=False,
            options={
                "mb_batch_metadata": {
                    _batch_key("nct dream", "My First and Last", 1, 3): {
                        "recording_mbid": _mbid(3),
                        "title": "My First and Last (Chinese version)",
                        "_title_match_confirmed": True,
                    }
                }
            },
        )

        assert "title" not in (result["payload"] or {}), (
            f"title must NOT be promoted when the gate is {gate}"
        )

    def test_equal_title_is_not_promoted(self, monkeypatch):
        from services.popularity.stages import track_stage

        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(track_stage, "get_shared_mb_service",
                            lambda: pytest.fail("batch hit must not search"))
        monkeypatch.setattr(track_stage, "_metadata_update_allowed", lambda: True)

        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={
                "title": "Same Title",
                "artist": "Artist",
                "recording_mbid": "",
                "musicbrainz_genres": '["x"]',
                "disc_number": 1,
                "track_number": 1,
            },
            track_title="Same Title",
            track_artist="Artist",
            frozen_track=False,
            force_meta=False,
            options={
                "mb_batch_metadata": {
                    _batch_key("artist", "Same Title", 1, 1): {
                        "recording_mbid": _mbid(1),
                        "title": "Same Title",
                        "_title_match_confirmed": True,
                    }
                }
            },
        )
        # Same case-insensitive title: no promotion, but musicbrainz_title set.
        assert result["payload"].get("title") is None
        assert result["payload"]["musicbrainz_title"] == "Same Title"

    def test_search_result_does_not_promote(self, monkeypatch):
        """A recording SEARCH is not the release; only the batch identity may."""
        from services.popularity.stages import track_stage

        monkeypatch.setattr(track_stage, "_has_real_genres", lambda track: True)
        monkeypatch.setattr(track_stage, "_metadata_update_allowed", lambda: True)

        class _Svc:
            def lookup_recording_metadata(self, *a, **k):
                return {"recording_mbid": _mbid(9), "title": "Found (Version)",
                        "genres": ["x"]}

        monkeypatch.setattr(track_stage, "get_shared_mb_service", lambda: _Svc())

        result = track_stage._resolve_track_mb_metadata(
            track_id="t1",
            track={"title": "Found", "artist": "Artist", "recording_mbid": "",
                   "musicbrainz_genres": '["x"]', "disc_number": 1, "track_number": 1},
            track_title="Found",
            track_artist="Artist",
            frozen_track=False,
            force_meta=False,
            options={},
        )
        # _from_batch is False for a search — no promotion.
        assert result["payload"].get("title") is None
        assert result["payload"]["musicbrainz_title"] == "Found (Version)"


# ---------------------------------------------------------------------------
# 2. The file-tag sync overwrites corrected titles (and nothing else)
# ---------------------------------------------------------------------------

class TestTagSyncWritesCorrectedTitles:
    def test_corrected_title_is_written_even_when_tag_exists(self, monkeypatch):
        import services.metadata.album_tag_sync_service as sync_mod

        called: dict = {}

        def _fake_record(track, file_values, db_candidates, **_k):
            called["conflicts"] = db_candidates
            # the service does ``corrections_recorded += result``
            return 1

        monkeypatch.setattr(sync_mod, "_load_fresh_tracks", lambda a, al: [{
            "id": "t1", "title": "New Title", "artist": "Artist", "album": "Album",
            "album_artist": "Artist", "file_path": "/music/Artist/Album/1.flac",
            "year": "2020", "release_year": "2020",
            "track_number": "1", "disc_number": "1", "isrc": "", "writer": "",
            "genres": "", "recording_mbid": "", "musicbrainz_albumid": "",
            "musicbrainz_releasegroupid": "", "musicbrainz_artistid": "",
            "musicbrainz_releasetrackid": "", "musicbrainz_workid": "",
        }])
        monkeypatch.setattr(sync_mod, "_read_file_values", lambda p: {
            "title": "Old Title", "artist": "Artist", "album": "Album",
            "album_artist": "Artist", "year": "2020",
            "track_number": "1", "disc_number": "1",
        })
        monkeypatch.setattr(sync_mod, "_record_corrections", _fake_record)

        written: dict = {}

        def _fake_write(path, tags):
            written.update(tags)
            return True

        # sync_album_file_tags imports these from tag_file_service AT CALL TIME
        monkeypatch.setattr("services.metadata.tag_file_service.resolve_music_file_path", lambda p: p)
        monkeypatch.setattr("services.metadata.tag_file_service.write_tags_to_file", _fake_write)
        monkeypatch.setattr("os.path.exists", lambda p: True)

        result = sync_mod.sync_album_file_tags(
            "Artist", "Album", corrected_title_ids={"t1"},
        )
        assert written.get("title") == "New Title", (
            "a corrected title must be FORCE-written to the file even when the "
            "tag exists — fill-if-missing never corrected 'Old Title'"
        )
        assert result.get("files_updated", 0) >= 1

    def test_uncorrected_track_title_is_not_overwritten(self, monkeypatch):
        """A track NOT in the corrected set keeps fill-if-missing semantics."""
        import services.metadata.album_tag_sync_service as sync_mod

        monkeypatch.setattr(sync_mod, "_load_fresh_tracks", lambda a, al: [{
            "id": "t2", "title": "DB Title", "artist": "Artist", "album": "Album",
            "album_artist": "Artist", "file_path": "/music/Artist/Album/2.flac",
            "year": "2020", "release_year": "2020",
            "track_number": "2", "disc_number": "1", "isrc": "", "writer": "",
            "genres": "", "recording_mbid": "", "musicbrainz_albumid": "",
            "musicbrainz_releasegroupid": "", "musicbrainz_artistid": "",
            "musicbrainz_releasetrackid": "", "musicbrainz_workid": "",
        }])
        monkeypatch.setattr(sync_mod, "_read_file_values", lambda p: {
            "title": "Kept Local", "artist": "Artist", "album": "Album",
            "album_artist": "Artist", "year": "2020",
            "track_number": "2", "disc_number": "1",
        })
        monkeypatch.setattr(sync_mod, "_record_corrections",
                            lambda track, fv, dc, **k: 0)
        monkeypatch.setattr("os.path.exists", lambda p: True)

        written: dict = {}

        def _fake_write(path, tags):
            written.update(tags)
            return True

        monkeypatch.setattr("services.metadata.tag_file_service.resolve_music_file_path", lambda p: p)
        monkeypatch.setattr("services.metadata.tag_file_service.write_tags_to_file", _fake_write)

        sync_mod.sync_album_file_tags("Artist", "Album")  # no corrected set
        assert "title" not in written, (
            "a deliberately chosen local title must NOT be overwritten — only "
            "ids the scan corrected get a forced title write"
        )


# ---------------------------------------------------------------------------
# 3. The runner forwards corrected ids to the tag sync (wiring guard)
# ---------------------------------------------------------------------------

def test_runner_passes_corrected_title_ids_to_tag_sync() -> None:
    """Semantic guard (AST): the runner forwards corrected-title ids to the
    tag sync. A text probe is fragile here — the id key is a STRING literal
    (``_res.get("title_corrected")``), so a string-stripping token probe
    cannot see it.
    """
    import ast

    import services.popularity.scan_stage_runner as runner

    tree = ast.parse(io.open(runner.__file__, encoding="utf-8").read())

    sync_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "sync_album_file_tags"
    ]
    assert sync_calls, "the runner never calls sync_album_file_tags"
    # At least one call passes ``corrected_title_ids=_corrected_title_ids``.
    def _names(node) -> list[str]:
        """Collect identifier names referenced by an expression node."""
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.BoolOp):
            return [n for v in node.values for n in _names(v)]
        if isinstance(node, ast.BinOp):
            return _names(node.left) + _names(node.right)
        if isinstance(node, ast.Tuple):
            return [n for el in node.elts for n in _names(el)]
        return []

    assert any(
        any(
            isinstance(kw.arg, str) and kw.arg == "corrected_title_ids"
            and "_corrected_title_ids" in _names(kw.value)
            for kw in call.keywords
        )
        for call in sync_calls
    ), "the runner must forward _corrected_title_ids to sync_album_file_tags"

    # And the collection loop reads the title_corrected flag off each result.
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "title_corrected"
        for n in ast.walk(tree)
    ) or any(
        isinstance(n, ast.Constant) and n.value == "title_corrected"
        for n in ast.walk(tree)
    ), "the runner must read track_result['title_corrected'] to build the set"


# ---------------------------------------------------------------------------
# 4. apply-mb-field writes the file (Compare/Apply UI)
# ---------------------------------------------------------------------------

def test_apply_route_is_gated_and_awaits() -> None:
    """Semantic guard (AST) for the Compare/Apply title write."""
    import ast

    import routes.api_v1.tracks as tracks_mod

    tree = ast.parse(io.open(tracks_mod.__file__, encoding="utf-8").read())

    # Find the ``if field == "title":`` branch inside the apply handler.
    title_branches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.left, ast.Name)
            and test.left.id == "field"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "title"
        ):
            title_branches.append(node)
    assert title_branches, "apply-mb-field must gate a title write on field == 'title'"

    branch_source = ast.get_source_segment(
        io.open(tracks_mod.__file__, encoding="utf-8").read(),
        title_branches[-1],
    )
    assert "write_tags_to_file" in branch_source, (
        "applying the title must also write the file tag (the DB-only write "
        "left the audio file holding the old title)"
    )