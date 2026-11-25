"""Genre re-evaluation from stored evidence, and the vocabulary/marker fixes."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from services.enrichment import genre_resync_service as svc
from services.enrichment.genre_aggregation_service import _append_extra_genres
from services.enrichment.genre_vocabulary import (
    KIND_GENRE,
    KIND_MOOD,
    get_vocabulary,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestAcousticMarker:
    def test_acoustic_title_is_acoustic_not_live(self):
        assert _append_extra_genres(["Punk"], "Baby Come On (acoustic)", "") == [
            "Punk", "Acoustic",
        ]

    def test_unplugged_title_is_unplugged(self):
        assert _append_extra_genres(["Rock"], "Song (unplugged)", "") == [
            "Rock", "Unplugged",
        ]

    def test_live_title_is_live(self):
        assert _append_extra_genres(["Rock"], "Song (live)", "") == ["Rock", "Live"]


class TestVocabularyAdditions:
    @pytest.mark.parametrize(
        "value,canonical",
        [
            ("pop rock", "pop_rock"),
            ("classic rock", "classic_rock"),
            ("dance-pop", "dance_pop"),
            ("disco", "disco"),
            ("gangsta", "gangsta_rap"),
            ("britpop", "britpop"),
            ("psychobilly", "psychobilly"),
            ("pop rap", "pop_rap"),
            ("funk metal", "funk_metal"),
            ("euro house", "euro_house"),
            ("bubblegum pop", "bubblegum_pop"),
            ("teen pop", "teen_pop"),
            ("new jack swing", "new_jack_swing"),
            ("west coast swing", "west_coast_swing"),
            ("arena rock", "arena_rock"),
            ("rock & roll", "rock_and_roll"),
            ("acoustic rock", "acoustic_rock"),
            ("symphonic rock", "symphonic_rock"),
            ("industrial rock", "industrial_rock"),
            ("sea shanties", "sea_shanties"),
        ],
    )
    def test_value_is_a_genre(self, value, canonical):
        vocab = get_vocabulary()
        assert not vocab.errors
        classification = vocab.classify(value)
        assert classification.kind == KIND_GENRE
        assert classification.canonical == canonical

    @pytest.mark.parametrize("value", ["melodic", "heavy"])
    def test_texture_words_are_not_genres(self, value):
        assert get_vocabulary().classify(value).kind == KIND_MOOD


# ---------------------------------------------------------------------------
# resync service
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Session:
    def __init__(self, rows, updates):
        self.rows = rows
        self.updates = updates

    def execute(self, statement, params=None):
        if str(statement).lstrip().upper().startswith("SELECT"):
            return _Result(self.rows)
        self.updates.append((str(statement), params))
        return _Result([])


@contextmanager
def _session_cm(rows, updates):
    yield _Session(rows, updates)


def _row(**overrides):
    row = {
        "id": "1", "title": "Song", "album": "Album", "artist": "Artist",
        "album_artist": "Artist", "genres": "Rock",
        "musicbrainz_genres": None, "listenbrainz_genres": None,
        "lastfm_tags": None, "discogs_genres": None, "spotify_genres": None,
        "navidrome_genres": None, "audiodb_genres": None,
        "essentia_genres": None, "wikidata_genres": None, "manual_genres": None,
    }
    row.update(overrides)
    return row


def _run(monkeypatch, rows, **kwargs):
    updates: list = []
    monkeypatch.setattr(
        svc, "db_session", lambda: _session_cm(rows, updates)
    )
    result = svc.resync_genres(**kwargs)
    return result, updates


def test_dry_run_reports_changes_and_writes_nothing(monkeypatch):
    rows = [
        _row(id="1", musicbrainz_genres='["Punk"]'),
        _row(id="2"),  # no stored evidence
    ]
    result, updates = _run(monkeypatch, rows, dry_run=True)

    assert result["tracks"] == 2
    assert result["with_evidence"] == 1
    assert result["skipped_no_evidence"] == 1
    assert result["changed"] == 1
    assert updates == []
    assert result["changed_tracks"][0]["after"]


def test_apply_writes_the_selected_genres(monkeypatch):
    rows = [_row(id="1", musicbrainz_genres='["Punk"]')]
    result, updates = _run(monkeypatch, rows, dry_run=False)

    assert result["changed"] == 1
    assert len(updates) == 1
    _sql, params = updates[0]
    assert params[0]["id"] == "1"
    assert "Punk" in params[0]["genres"]


def test_manual_genres_are_used(monkeypatch):
    rows = [_row(id="1", genres="", manual_genres='["Shoegaze"]')]
    result, updates = _run(monkeypatch, rows, dry_run=True)

    assert result["changed"] == 1
    assert result["changed_tracks"][0]["after"] == "Shoegaze"


def test_a_row_with_no_evidence_is_left_alone(monkeypatch):
    rows = [_row(id="1", genres="Legacy Rock")]
    result, updates = _run(monkeypatch, rows, dry_run=False)

    assert result["with_evidence"] == 0
    assert result["changed"] == 0
    assert updates == []


class TestCommaSeparatedEvidenceIsRead:
    """A comma-joined genre list must yield candidates, not one unknown token.

    REPORTED

    > "I ran the genre Re-evaluation, but it didn't seem to do what I expected."
    > ... 11,842 tracks whose Final Genres still violate the cleanup rules.

    ``tracks.genres`` and every provider column are written with
    ``", ".join(genres)``, but ``tokenize_values`` only split on ``;``, ``|``,
    ``\\``, ``/`` and ``&``. A comma-joined value therefore stayed ONE token,
    was classified ``unknown``, and contributed NO candidate — so the selector
    returned an empty list and the resync skipped the track entirely. Stale
    genres survived a "successful" re-evaluation, which is exactly the report.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "Industrial Metal, Rock, Nu Metal",
            "Industrial Metal; Rock; Nu Metal",
            '["Industrial Metal", "Rock", "Nu Metal"]',
            ["Industrial Metal", "Rock", "Nu Metal"],
        ],
        ids=["comma", "semicolon", "json", "list"],
    )
    def test_every_stored_shape_yields_the_same_clean_set(self, raw):
        from services.enrichment.genre_aggregation_service import aggregate_genres

        out = aggregate_genres({"musicbrainz@track": raw}, max_genres=4)
        assert out == ["Industrial Metal", "Nu Metal"], (
            f"{raw!r} did not parse into candidates; a comma-joined list is "
            "silently skipped by the resync"
        )

    def test_a_comma_does_not_break_a_single_genre_name(self):
        """A name with a comma inside would be split only as a last resort."""
        from services.enrichment.genre_vocabulary import tokenize_values

        vocab = get_vocabulary()
        # ``K-pop`` style values and multi-word names must survive whole.
        assert tokenize_values("Melodic Death Metal", vocab) == ["Melodic Death Metal"]
        assert tokenize_values("Rock, Pop", vocab) == ["Rock", "Pop"]

    def test_the_resync_actually_rewrites_comma_joined_sources(self, monkeypatch):
        """End-to-end: the stale set is replaced, not left alone."""
        rows = [
            _row(
                id="1",
                genres="industrial metal; rock; metal; hard rock; punk",
                musicbrainz_genres="Industrial Metal, Nu Metal, Heavy Metal, Rock",
                listenbrainz_genres="Industrial Metal, Nu Metal",
                lastfm_tags="Industrial Metal, Nu Metal, Rock",
            )
        ]
        result, updates = _run(monkeypatch, rows, dry_run=False)

        assert result["changed"] == 1, (
            "comma-joined provider columns produced no candidates, so the "
            "resync skipped the track and left its stale genres in place"
        )
        _statement, params = updates[0]
        # ``_persist`` batches the rewrites, so the payload is a LIST of dicts.
        batch = params if isinstance(params, list) else [params]
        after_text = ", ".join(str((p or {}).get("genres") or "") for p in batch)
        assert "Industrial Metal" in after_text
        assert "rock" not in after_text.lower(), (
            f"the rock family should have been condensed away, got {after_text!r}"
        )


class TestApplyProgressAndSpeed:
    """The apply must not look hung between the walk and the file writes.

    Reported: "Seems to be stuck here — genre_playlist_resync 25% · Writing
    genres · Recomputing genres (67000/67039 tracks)…". The walk reports every
    500 tracks, so the last frame can be one short of the total; the save was a
    single silent 70k-row UPDATE; and the file loop waited for file 100 before
    its first frame. On a slow mount that whole stretch looked frozen.
    """

    def _rows(self, count=2):
        return [
            _row(id=str(i), musicbrainz_genres='["Punk"]', file_path=f"/music/{i}.mp3")
            for i in range(1, count + 1)
        ]

    def test_the_walk_end_is_reported_and_each_stage_is_named(self, monkeypatch):
        updates: list = []
        rows = self._rows()
        monkeypatch.setattr(svc, "db_session", lambda: _session_cm(rows, updates))

        from services.metadata import tag_file_service as tfs
        monkeypatch.setattr(tfs, "update_file_tags", lambda path, tags: True)
        monkeypatch.setattr(tfs, "_resolve_music_file_path", lambda path: path)

        frames: list[tuple] = []
        svc.resync_genres(
            dry_run=False,
            write_file_tags=True,
            sync_navidrome=False,
            on_progress=lambda *args: frames.append(args),
        )

        assert (2, 2, 0, "aggregate") in frames, frames
        assert any(f[3] == "persist" for f in frames), frames
        assert any(f[3] == "files" for f in frames), frames

    def test_the_column_save_is_chunked_and_reports(self, monkeypatch):
        calls: list = []

        @contextmanager
        def _db():
            class _S:
                def execute(self, statement, params=None):
                    calls.append(params)
            yield _S()

        monkeypatch.setattr(svc, "db_session", _db)

        frames: list[tuple[int, int]] = []
        changes = [(str(i), "Punk") for i in range(2500)]
        svc._persist(changes, on_progress=lambda done, total: frames.append((done, total)))

        assert len(calls) == 3, f"expected three chunks, got {len(calls)}"
        assert frames == [
            (0, 2500), (1000, 2500), (2000, 2500), (2500, 2500),
        ], frames

    def test_the_file_write_uses_the_carried_path(self, monkeypatch):
        """No per-track DB lookup on the slowest phase of the job."""
        from services.metadata import tag_file_service as tfs

        # The real signature now takes ``defer_stamp`` (the local-authority mark
        # is written once per batch, not per file).
        seen: list[bool] = []

        def _fake_update(path, tags, *, defer_stamp=False):
            seen.append(defer_stamp)
            return True

        monkeypatch.setattr(tfs, "update_file_tags", _fake_update)
        monkeypatch.setattr(tfs, "_resolve_music_file_path", lambda path: path)
        monkeypatch.setattr(
            svc,
            "_resolve_track_file_path",
            lambda _id: (_ for _ in ()).throw(AssertionError("DB lookup per file")),
        )

        out = svc._push_to_files(
            [{"track_id": "1", "file_path": "/music/one.mp3", "after": "Punk"}],
            write_file_tags=True,
            sync_navidrome=False,
        )
        assert out["files_written"] == 1, out
        assert seen == [True], "the per-file stamp must be deferred to the batch"

    def test_apply_does_not_recompute_selector_explanations(self, monkeypatch):
        """``removed`` is preview-only; computing it re-ran the selector 70k times."""
        updates: list = []
        monkeypatch.setattr(
            svc, "db_session", lambda: _session_cm(self._rows(1), updates)
        )
        monkeypatch.setattr(
            svc,
            "_explain",
            lambda *args: (_ for _ in ()).throw(AssertionError("selector re-run")),
        )

        result = svc.resync_genres(
            dry_run=False, write_file_tags=False, sync_navidrome=False
        )
        assert result["changed"] == 1, result


class TestTheSaveSurvivesADatabaseRestart:
    """PostgreSQL crashed mid-save; the chunk must wait and retry, not abort."""

    @staticmethod
    @contextmanager
    def _db_that_fails_once(counter):
        class _S:
            def execute(self, _statement, _params=None):
                counter["attempts"] += 1
                if counter["attempts"] == 1:
                    raise RuntimeError(
                        "terminating connection because of crash of another "
                        "server process"
                    )
        yield _S()

    def test_the_chunk_is_retried_after_recovery(self, monkeypatch):
        counter = {"attempts": 0}
        monkeypatch.setattr(
            svc, "db_session", lambda: self._db_that_fails_once(counter)
        )
        monkeypatch.setattr(svc, "_db_is_recovering", lambda: True)
        monkeypatch.setattr(svc, "_wait_for_db_recovery", lambda *_a, **_k: True)

        svc._persist([("1", "Punk")])

        assert counter["attempts"] == 2, "the chunk was not retried after the restart"

    def test_it_aborts_with_a_clear_error_if_the_database_stays_down(self, monkeypatch):
        counter = {"attempts": 0}
        monkeypatch.setattr(
            svc, "db_session", lambda: self._db_that_fails_once(counter)
        )
        monkeypatch.setattr(svc, "_db_is_recovering", lambda: True)
        monkeypatch.setattr(svc, "_wait_for_db_recovery", lambda *_a, **_k: False)

        with pytest.raises(RuntimeError) as caught:
            svc._persist([("1", "Punk")])

        assert "database is restarting" in str(caught.value)


class TestStaleProgressRows:
    """A dead daemon thread must not block retries forever."""

    @staticmethod
    def _patch_state(monkeypatch, state):
        from services.scanning import scan_state

        monkeypatch.setattr(scan_state, "read_progress_file", lambda _path: state)

    def test_a_fresh_running_row_is_running(self, monkeypatch):
        from datetime import datetime

        from services.enrichment import genre_playlist_resync_service as gpr

        self._patch_state(
            monkeypatch,
            {"is_running": True, "last_updated": datetime.now().isoformat()},
        )
        assert gpr.is_resync_running() is True

    def test_an_old_running_row_is_not_running(self, monkeypatch):
        from datetime import datetime, timedelta

        from services.enrichment import genre_playlist_resync_service as gpr

        old = (datetime.now() - timedelta(minutes=30)).isoformat()
        self._patch_state(monkeypatch, {"is_running": True, "last_updated": old})
        assert gpr.is_resync_running() is False, (
            "a restarted app could never start a new resync"
        )

    def test_a_finished_row_is_not_running(self, monkeypatch):
        from services.enrichment import genre_playlist_resync_service as gpr

        self._patch_state(monkeypatch, {"is_running": False})
        assert gpr.is_resync_running() is False


class TestWiring:
    def test_the_route_exists_and_offloads(self):
        source = (REPO_ROOT / "routes" / "misc_routes.py").read_text(encoding="utf-8")
        assert '@misc_api_bp.route("/genres/resync"' in source
        start = source.index("async def api_resync_genres")
        body = source[start:start + 2000]
        assert "asyncio.to_thread" in body

    @pytest.mark.parametrize(
        "rel",
        ["test_site/templates/Pages/config.html", "test_site/templates/Pages/config.html"],
    )
    def test_config_page_has_the_controls(self, rel):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "genre_resync_preview_btn" in text
        assert "genre_resync_apply_btn" in text

    @pytest.mark.parametrize(
        "rel",
        ["test_site/static/js/pages/config.js", "test_site/static/js/pages/config.js"],
    )
    def test_js_calls_the_endpoint(self, rel):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "runGenreResync" in text
        assert "/api/genres/resync" in text
