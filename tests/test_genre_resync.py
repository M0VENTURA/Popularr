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


class TestWiring:
    def test_the_route_exists_and_offloads(self):
        source = (REPO_ROOT / "routes" / "misc_routes.py").read_text(encoding="utf-8")
        assert '@misc_api_bp.route("/genres/resync"' in source
        start = source.index("async def api_resync_genres")
        body = source[start:start + 2000]
        assert "asyncio.to_thread" in body

    @pytest.mark.parametrize(
        "rel",
        ["templates/pages/config.html", "test_site/templates/Pages/config.html"],
    )
    def test_config_page_has_the_controls(self, rel):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "genre_resync_preview_btn" in text
        assert "genre_resync_apply_btn" in text

    @pytest.mark.parametrize(
        "rel",
        ["static/js/config.js", "test_site/static/js/pages/config.js"],
    )
    def test_js_calls_the_endpoint(self, rel):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "runGenreResync" in text
        assert "/api/genres/resync" in text
