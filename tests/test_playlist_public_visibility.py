"""A playlist created as public must actually BE public.

⚠️ WHAT WENT WRONG.

``NavidromeClient.create_playlist(name, song_ids, public=True)`` sent ``public``
to Subsonic's ``createPlaylist`` — and **Navidrome ignores that parameter
completely**. Its handler reads only ``songId``, ``playlistId`` and ``name``,
then calls ``playlists.Create(ctx, playlistId, name, songIds)``, whose signature
has no visibility argument at all:

    // server/subsonic/playlists.go
    id, err := api.playlists.Create(ctx, playlistId, name, songIds)

So every Essential Collection was created **PRIVATE**, while the code, the
default argument and the log line all said "public". Visibility is settable only
through ``updatePlaylist``, where Navidrome reads ``public`` as a NULLABLE
pointer and applies it only when it is present:

    // server/subsonic/playlists.go
    public := p.BoolPtr("public")
    // core/playlists/playlists.go
    if public != nil { pls.Public = *public }

Navidrome's own Jellyfin adapter states the rule outright: *"Create takes no
visibility, so a requested one costs a second write."*

Until now the only thing that ever made these playlists public was the separate
``sync_playlists_public`` pass — which is opt-in, runs only after a library sync,
and is skipped while a Navidrome import runs. So the playlists were public only
by accident of timing, and stopped being public as soon as that pass was
deferred.

These tests pin the second write, and pin that a DEFERRED visibility update
reports FAILURE rather than the empty body's misleading "success".
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

from api_clients.navidrome import NavidromeClient


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _FakeResponse:
    """A response with the attributes the client actually reads.

    ⚠️ ``content`` and ``text`` are BOTH required — the client treats an empty
    body as the success response for mutation endpoints, so a fake without them
    exercises a different code path than production does. Omitting ``content``
    is a pre-existing flaw in ``test_navidrome_playlist_post_body.py`` that
    makes five of its tests fail on pristine ``develop``.
    """

    def __init__(self, payload: dict[str, Any] | None = None, status_code: int = 200):
        self.status_code = status_code
        self.content = b"x" if payload is not None else b""
        self.text = json.dumps(payload) if payload is not None else ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload

    @property
    def _payload(self) -> dict[str, Any]:
        return json.loads(self.text)


class _FakeSession:
    """Records POSTs and GETs separately so the two writes can be told apart."""

    def __init__(
        self,
        create_payload: dict[str, Any] | None = None,
        update_payload: dict[str, Any] | None = None,
    ):
        self._create_payload = create_payload
        self._update_payload = update_payload
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, data=None, **_kw: Any) -> _FakeResponse:
        self.posts.append((url, dict(data or {})))
        return _FakeResponse(self._create_payload)

    def get(self, url: str, params=None, **_kw: Any) -> _FakeResponse:
        self.gets.append((url, dict(params or {})))
        return _FakeResponse(self._update_payload)


def _created_payload(playlist_id: str = "pl-1", songs: int = 3) -> dict[str, Any]:
    """What Navidrome returns from a real ``createPlaylist``."""
    return {
        "subsonic-response": {
            "status": "ok",
            "playlist": {"id": playlist_id, "songCount": songs},
        }
    }


class _Client(NavidromeClient):
    def __init__(self, session: _FakeSession):
        super().__init__(base_url="http://navidrome:4533", username="u", password="p")
        self.session = session


def _gate(monkeypatch: pytest.MonkeyPatch, running: bool) -> None:
    """Force the import gate, so these tests never depend on the state file."""
    gate = importlib.import_module("services.navidrome.import_gate")
    monkeypatch.setattr(gate, "is_import_running", lambda *a, **k: running)
    monkeypatch.setattr(gate, "should_defer", lambda endpoint: running)


# ---------------------------------------------------------------------------
# create_playlist: the second write
# ---------------------------------------------------------------------------


def test_create_playlist_makes_a_second_write_to_set_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE REGRESSION. ``createPlaylist`` cannot set visibility, so it takes two writes."""
    _gate(monkeypatch, running=False)
    session = _FakeSession(create_payload=_created_payload())
    client = _Client(session)

    data = client.create_playlist("Acme (Essential Playlist)", ["1", "2", "3"])

    assert len(session.posts) == 1, "exactly one create"
    assert "createPlaylist" in session.posts[0][0]

    assert len(session.gets) == 1, (
        "a public create MUST issue a second write: Navidrome's createPlaylist "
        "ignores `public`, so without this the playlist is born private"
    )
    url, params = session.gets[0]
    assert "updatePlaylist" in url
    assert params.get("playlistId") == "pl-1"
    assert params.get("public") == "true"

    assert data.get("public") is True, (
        "the result must report the OUTCOME, so a caller cannot mistake a "
        "private playlist for a public one"
    )


def test_create_playlist_private_does_not_attempt_the_second_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: asking for private must not issue an extra request."""
    _gate(monkeypatch, running=False)
    session = _FakeSession(create_payload=_created_payload())
    client = _Client(session)

    data = client.create_playlist("Private One", ["1"], public=False)

    assert len(session.posts) == 1
    assert session.gets == [], "no visibility write is needed for a private playlist"
    assert data.get("public") is False


def test_create_playlist_without_an_id_performs_no_second_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed/deferred create has no playlist id to write to.

    ⚠️ A deferred write is answered with ``{"status": "ok"}`` and no ``playlist``
    object. Without the id guard the second write would be aimed at ``""`` — and
    an empty ``playlistId`` sent to ``updatePlaylist`` is not a harmless no-op.
    """
    _gate(monkeypatch, running=False)
    session = _FakeSession(create_payload={"subsonic-response": {"status": "ok"}})
    client = _Client(session)

    client.create_playlist("Acme", ["1"])

    assert session.gets == [], "no id means nothing to write to"


def test_create_playlist_reports_false_when_visibility_does_not_stick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create still succeeds, but ``public`` must not claim success."""
    _gate(monkeypatch, running=False)
    session = _FakeSession(
        create_payload=_created_payload(),
        update_payload={
            "subsonic-response": {
                "status": "failed",
                "error": {"code": 50, "message": "not authorized"},
            }
        },
    )
    client = _Client(session)

    data = client.create_playlist("Acme", ["1"])

    assert data.get("status") == "ok", "the playlist WAS created — that part succeeded"
    assert data.get("public") is False, (
        "visibility failed, so the result must not claim the playlist is public"
    )
    assert len(session.gets) == 1


def test_create_playlist_sends_public_on_the_create_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parameter is still sent for other Subsonic servers that honour it."""
    _gate(monkeypatch, running=False)
    session = _FakeSession(create_payload=_created_payload())
    client = _Client(session)

    client.create_playlist("Acme", ["1"])

    _url, body = session.posts[0]
    assert body.get("public") == "true"
    assert body.get("name") == "Acme"


# ---------------------------------------------------------------------------
# update_playlist_public: a deferral must not read as success
# ---------------------------------------------------------------------------


def test_deferred_visibility_update_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ THE SUBTLE ONE. A deferred call returns ``{}``, which means "success".

    ``_get_subsonic_response`` answers a skipped request with ``{}``, and the
    empty-body-success rule turns that into ``True``. Every caller then believes
    a playlist was made public when nothing was sent — worse than a failure,
    because nothing retries it.
    """
    _gate(monkeypatch, running=True)
    session = _FakeSession()
    client = _Client(session)

    ok = client.update_playlist_public("pl-1", True)

    assert ok is False, "a deferred update did not happen and must not report success"
    assert session.gets == [], "the request must not even be attempted"


def test_visibility_update_succeeds_when_not_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: Navidrome answers ``updatePlaylist`` with an empty body on success."""
    _gate(monkeypatch, running=False)
    session = _FakeSession(update_payload=None)  # empty body = success
    client = _Client(session)

    assert client.update_playlist_public("pl-1", True) is True
    assert len(session.gets) == 1


def test_import_client_is_never_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The import's own client must not defer its own visibility write."""
    _gate(monkeypatch, running=True)
    session = _FakeSession()
    client = NavidromeClient(
        base_url="http://navidrome:4533",
        username="u",
        password="p",
        is_import=True,
    )
    client.session = session

    assert client.update_playlist_public("pl-1", True) is True
    assert len(session.gets) == 1, "the import is exempt from its own gate"


# ---------------------------------------------------------------------------
# sync_playlists_public: do not report a clean zero for a skipped pass
# ---------------------------------------------------------------------------


def _patch_config(monkeypatch: pytest.MonkeyPatch, auto_public: bool) -> None:
    import helpers.config_helpers as ch

    nav = {
        "base_url": "http://navidrome:4533",
        "user": "u",
        "pass": "p",
        "auto_public_playlists": auto_public,
    }
    monkeypatch.setattr(ch, "get_config", lambda: {"navidrome": nav, "navidrome_users": [nav]})


class _SpyClient:
    """Stand-in that records construction, so "did it contact Navidrome?" is observable."""

    constructed = 0
    listed = 0

    def __init__(self, *_a: Any, **_k: Any) -> None:
        type(self).constructed += 1

    def fetch_all_playlists(self) -> list[dict[str, Any]]:
        type(self).listed += 1
        return []


def test_public_sync_reports_a_deferral_instead_of_a_clean_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`checked: 0` cannot mean both "skipped" and "all already public"."""
    import services.playlists.playlist_service as svc

    _patch_config(monkeypatch, auto_public=True)
    _gate(monkeypatch, running=True)

    _SpyClient.constructed = 0
    monkeypatch.setattr("api_clients.navidrome.NavidromeClient", _SpyClient)

    result = svc.sync_playlists_public()

    assert result.get("deferred") is True, (
        "a pass that ran while an import held Navidrome must say it was deferred — "
        "otherwise it is indistinguishable from a clean run that found nothing to do"
    )
    assert result["enabled"] is True, "the feature is on; only this pass was deferred"
    assert result["made_public"] == 0
    assert _SpyClient.constructed == 0, (
        "it must not even build a client: getPlaylists/updatePlaylist are both "
        "deferred by the gate, so every request would be a wasted round trip"
    )


def test_public_sync_still_runs_when_no_import_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the deferral guard must be inert outside an import."""
    import services.playlists.playlist_service as svc

    _patch_config(monkeypatch, auto_public=True)
    _gate(monkeypatch, running=False)

    _SpyClient.constructed = 0
    monkeypatch.setattr("api_clients.navidrome.NavidromeClient", _SpyClient)

    result = svc.sync_playlists_public()

    assert not result.get("deferred")
    assert _SpyClient.constructed == 1, "a normal pass must still contact Navidrome"
    assert result["enabled"] is True


def test_public_sync_is_disabled_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the opt-in flag still short-circuits before anything else."""
    import services.playlists.playlist_service as svc

    _patch_config(monkeypatch, auto_public=False)
    _gate(monkeypatch, running=False)

    _SpyClient.constructed = 0
    monkeypatch.setattr("api_clients.navidrome.NavidromeClient", _SpyClient)

    result = svc.sync_playlists_public()

    assert result == {"enabled": False, "checked": 0, "made_public": 0, "failed": 0}
    assert _SpyClient.constructed == 0
