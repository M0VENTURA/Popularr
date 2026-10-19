"""A deferred Navidrome read must not look like a real answer.

⚠️ THE BUG THIS FILE EXISTS TO PREVENT.

``NavidromeClient`` answers a request that was deferred while a Navidrome
import runs with a *plausible success payload* — ``{}`` for a read, ``ok`` for
a write. That is deliberate (nothing upstream should crash on a deferral), but
it creates a nasty trap for any caller that **branches on the emptiness of the
result**, because "the import gate said no" and "the server said nothing" are
the same value:

* ``sync_playlist_by_name`` sees an empty ``getPlaylists`` listing, concludes
  the playlist does not exist yet, and **creates a second copy** of a playlist
  that is sitting right there on the server. The next dedupe pass deletes one
  of them — the same ping-pong shape as the old multi-account duplicate bug.
* ``sync_playlist_index`` sees an empty listing, counts **zero errors**, and
  therefore lets the prune run against a listing that contains only the
  file-backed playlists — **deleting every Navidrome playlist from the index**
  and permanently emptying artist pages.
* A deferral that lands *after* a successful listing makes ``getPlaylist``
  answer ``{}``, and ``replace_playlist`` deletes rows before inserting, so
  indexing that as empty **wipes the playlist's entries**.

All three are silent, all three are destructive, and none is visible in a log
unless the caller knows to look. This module pins the guards that make them
impossible, so a future refactor cannot quietly remove them.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest


def _gate(monkeypatch: pytest.MonkeyPatch, running: bool) -> None:
    """Force the import gate to report ``running``.

    The production code imports ``is_import_running`` *inside* the function, so
    patching the attribute on the module is enough — and it is also the honest
    test shape, because it exercises the real import path rather than a
    function object captured at module import time.
    """
    gate = importlib.import_module("services.navidrome.import_gate")
    monkeypatch.setattr(gate, "is_import_running", lambda *a, **k: running)


# ---------------------------------------------------------------------------
# sync_playlist_by_name: deferral must NEVER mean "create it"
# ---------------------------------------------------------------------------


class _RecordingClient:
    """A client that records which calls it received.

    ⚠️ OBSERVATION, NOT EXCEPTION. An earlier version of these tests made every
    attribute raise, on the assumption that reaching the client would blow up.
    It does not: ``sync_playlist_by_name`` treats a client exception as a
    "playlist lookup failed" warning and *returns normally*, so the raise was
    swallowed and the test would have passed for the wrong reason. Recording the
    calls is the only way to distinguish "the guard fired" from "the call
    failed".
    """

    is_import = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, item: str) -> Any:
        # Only reached for attributes not defined on the class, so the explicit
        # methods below still win.
        def _record(*_a: Any, **_k: Any) -> Any:
            self.calls.append(item)
            return None

        return _record

    def fetch_all_playlists(self) -> list[dict[str, Any]]:
        self.calls.append("fetch_all_playlists")
        return []

    def delete_playlist(self, *_a: Any, **_k: Any) -> bool:
        self.calls.append("delete_playlist")
        return True


def test_deferred_sync_skips_instead_of_creating_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression itself: an empty listing must not create a playlist."""
    from services.playlists import playlist_navidrome_service as svc

    _gate(monkeypatch, running=True)

    client = _RecordingClient()
    result = svc.sync_playlist_by_name(client, "Essential Collections", ["1", "2", "3"])

    assert result["skipped"] is True, "a deferred sync must report skipped"
    assert result["success"] is True, (
        "skipped is 'nothing attempted', not 'it broke' — reporting failure "
        "would make the genre loop count a healthy run as a partial one"
    )
    assert result["created"] is False
    assert result["updated"] is False
    assert result["unchanged"] is False
    assert client.calls == [], (
        "the guard must short-circuit BEFORE the listing is fetched; touching "
        "the client at all is how the duplicate got created in the first place"
    )


def test_deferred_sync_reports_success_so_the_caller_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``skipped`` must satisfy the success contract the sync callers use."""
    from services.playlists import playlist_navidrome_service as svc

    _gate(monkeypatch, running=True)

    result = svc.sync_playlist_by_name(_RecordingClient(), "Genre Top Tracks", ["1"])

    # This is the exact expression the genre/Essential-Collections loops use to
    # decide whether to warn about a partially failed run.
    assert (
        result.get("created")
        or result.get("updated")
        or result.get("unchanged")
        or result.get("skipped")
    ), "a deferred sync must count as a non-failure in the caller's loop"


def test_force_bypasses_the_deferral_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``force=True`` is an explicit user action and must still be honoured.

    A user clicking "sync now" while a scan runs is entitled to have it
    attempted — the guard protects unattended loops, not intent.
    """
    from services.playlists import playlist_navidrome_service as svc

    _gate(monkeypatch, running=True)

    client = _RecordingClient()
    svc.sync_playlist_by_name(client, "Essential Collections", ["1"], force=True)

    assert "fetch_all_playlists" in client.calls, (
        "force=True must reach the client even while an import runs"
    )


def test_sync_proceeds_normally_when_no_import_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must be inert outside an import — no behaviour change."""
    from services.playlists import playlist_navidrome_service as svc

    _gate(monkeypatch, running=False)

    client = _RecordingClient()
    svc.sync_playlist_by_name(client, "Essential Collections", ["1"])

    assert "fetch_all_playlists" in client.calls, (
        "the guard must not fire when no import is running"
    )


def test_result_shape_always_carries_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``skipped`` must exist on every path, not only the deferred one.

    Callers read it with ``.get``, but keeping the key present on the normal
    path stops a future ``result["skipped"]`` from raising KeyError.
    """
    from services.playlists import playlist_navidrome_service as svc

    _gate(monkeypatch, running=False)

    result = svc.sync_playlist_by_name(None, "Anything", ["1"])
    assert result["skipped"] is False


# ---------------------------------------------------------------------------
# playlist_index_service: a deferred listing must refuse the prune
# ---------------------------------------------------------------------------


class _EmptyListingClient:
    """Reports success with no playlists — what a deferral looks like."""

    is_import = False

    def fetch_all_playlists(self) -> list[dict[str, Any]]:
        return []


class _RealListingClient:
    """Reports a genuine playlist, deferral or not."""

    is_import = False

    def fetch_all_playlists(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "nav-1",
                "name": "Road Trip",
                "owner": "aaron",
                "public": True,
                "songCount": 12,
                "entry": [],
            }
        ]


def test_deferred_empty_listing_is_counted_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression itself: an empty listing during an import blocks prune."""
    from services.playlists import playlist_index_service as idx

    _gate(monkeypatch, running=True)

    _entries, errors, listings = idx._enumerate([_EmptyListingClient()])

    assert errors == 1, (
        "a deferred listing must be counted as an error — the prune below only "
        "runs when errors == 0, and trusting a deferred empty listing would "
        "delete every Navidrome playlist from the index"
    )
    assert listings == [[]], "the raw slot is still returned per client"


def test_a_genuinely_empty_library_still_prunes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not fire when no import is running.

    A playlist-free server is a legitimate state, and refusing to prune there
    would leave deleted playlists in the index forever.
    """
    from services.playlists import playlist_index_service as idx

    _gate(monkeypatch, running=False)

    _entries, errors, _listings = idx._enumerate([_EmptyListingClient()])

    assert errors == 0, (
        "an empty listing outside an import is a real answer, not a deferral"
    )


def test_a_non_empty_listing_is_never_treated_as_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the *empty* payload is ambiguous, so only it is guarded.

    This matters: a deferred ``getPlaylists`` returns ``{}`` and never a partial
    list, so a non-empty answer proves the request was really served. Guarding
    it would needlessly block the prune whenever an import overlapped a healthy
    listing.
    """
    from services.playlists import playlist_index_service as idx

    _gate(monkeypatch, running=True)

    entries, errors, listings = idx._enumerate([_RealListingClient()])

    assert errors == 0
    assert [e["name"] for e in entries if e["source"] == "navidrome"] == ["Road Trip"]
    assert len(listings[0]) == 1


def test_one_deferred_client_among_several_still_refuses_the_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing is all-or-nothing — a partial listing cannot be pruned safely."""
    from services.playlists import playlist_index_service as idx

    _gate(monkeypatch, running=True)

    _entries, errors, _listings = idx._enumerate(
        [_RealListingClient(), _EmptyListingClient()]
    )

    assert errors == 1, (
        "the healthy client must not mask the deferred one — pruning against a "
        "listing that is missing one server's playlists deletes that server's "
        "entries"
    )


def test_a_deferred_read_does_not_raise_out_of_enumerate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard degrades, it does not explode.

    ``fetch_all_playlists`` swallows transport errors and returns ``[]``, so the
    deferral arrives here as data rather than as an exception. The enumeration
    must survive that and keep serving the file-backed playlists.
    """
    from services.playlists import playlist_index_service as idx

    _gate(monkeypatch, running=True)

    entries, _errors, _listings = idx._enumerate([_EmptyListingClient()])

    assert isinstance(entries, list), "the index still renders file-backed entries"
    assert not any(e["source"] == "navidrome" for e in entries)


class _DeferredDetailClient:
    """A client whose ``getPlaylist`` was deferred, i.e. answered ``{}``."""

    is_import = False

    def fetch_playlist(self, _playlist_id: str) -> dict[str, Any]:
        return {}


def test_a_deferred_playlist_detail_is_unknown_not_empty() -> None:
    """The third hazard: a deferral that lands AFTER the listing.

    ``_enumerate`` and ``sync_playlist_by_name`` guard the front of the sync, but
    an import can also start in the middle of one — the listing was genuinely
    served, and then ``getPlaylist`` is deferred. The detail then arrives as
    ``{}``, which is indistinguishable from a playlist that really has no
    tracks.

    Writing that would call ``replace_playlist``, which DELETES the existing rows
    first, so the playlist would vanish from the artist page until some later
    sync happened to succeed. Returning ``None`` marks it "unreadable", and the
    caller keeps the previous entries instead of writing an empty set.
    """
    from services.playlists import playlist_index_service as idx

    entry = {
        "playlist_id": "nav-1",
        "source": "navidrome",
        "song_count": 12,
        "name": "Road Trip",
    }

    ids, unresolved = idx._track_ids_for(entry, [_DeferredDetailClient()], detail={})

    assert ids is None, (
        "a deferred/empty payload must resolve to 'unknown', never to an empty "
        "track list — an empty list is a write instruction that wipes the "
        "playlist's indexed rows"
    )
    assert unresolved == 0, "nothing was dropped, so nothing is unresolved"


def test_the_gate_is_consulted_only_for_empty_listings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the call shape so the guard cannot silently widen later.

    If a future change consults the gate for every client, a long import would
    suppress the prune on healthy listings too — a behaviour change nobody
    asked for. Counting calls keeps that visible.
    """
    from services.playlists import playlist_index_service as idx

    calls: list[int] = []
    gate = importlib.import_module("services.navidrome.import_gate")

    def _counting_gate(*_a: Any, **_k: Any) -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr(gate, "is_import_running", _counting_gate)

    idx._enumerate([_RealListingClient(), _EmptyListingClient()])

    assert len(calls) == 1, (
        f"the gate was consulted {len(calls)} times for 2 clients; it must be "
        f"consulted only for the one that returned an empty listing"
    )
