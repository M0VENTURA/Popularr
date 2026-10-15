"""One Navidrome client per SERVER, not per configured user account.

THE REPORT
----------
A genre playlist was deleted and recreated in the SAME run — `7fNI…` removed as a
duplicate, a fresh `1xYY…` created moments later — and the two accounts repeated
that every scan.

WHY IT HAPPENED
---------------
``navidrome_users`` is a list of ACCOUNTS. Two accounts routinely point at the
SAME server (a personal login and an admin one). ``_navidrome_clients()`` built
one client per account, and every playlist caller then ran the whole sync once
per client — against one server, twice.

That is destructive, not merely redundant. A Navidrome playlist belongs to the
SERVER and is owned by the account that created it, so:

  * client A creates "Alternative Rock - Top Tracks", owned by A;
  * client B — which cannot see A's playlist — finds nothing by that name and
    creates its OWN copy;
  * each client's next sync sees two playlists with the name, keeps the one it
    can own, and DELETES the other. That delete is ``sync_playlist_by_name``'s
    own dedupe pass, working exactly as designed.

They then recreate what the other deleted, forever. Playlists are public, so one
sync per server is sufficient — and the first configured account owns it.

WHAT IS PINNED HERE
-------------------
The dedupe, the URL equivalence rules it rests on, and — crucially — that the
per-user consumers were NOT swept up with it. Star ratings and favourites are
per-account data; two accounts on one server legitimately differ there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.popularity.stages import finalise_stage as fs

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# A fake client, so the REAL factory can be driven
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records what it was built with; the factory does nothing else.

    ⚠️ ``built`` records CONSTRUCTION order, which is NOT the same as the order
    of the list the factory RETURNS. A mutation that reorders the returned list
    but still constructs both clients would pass an assertion on ``built``
    alone — and the returned order is what decides who owns the playlist, since
    the first client to run the sync is the one that creates it. So the
    identity of the returned clients is asserted through ``.user``.
    """

    built: list[tuple[str, str, str]] = []

    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base_url = base_url
        self.user = user
        _FakeClient.built.append((base_url, user, password))


@pytest.fixture
def clients(monkeypatch):
    """Drive the REAL ``_navidrome_clients()`` over a given user list.

    Only the two inputs are patched — the config getter and the client class —
    so the dedupe being tested is the shipped one, not a re-implementation.
    """
    import api_clients.navidrome as nav
    import helpers.config_helpers as ch

    def _run(users):
        _FakeClient.built = []
        fs.reset_playlist_push_cache()   # clears the 300s client cache
        monkeypatch.setattr(ch, "get_navidrome_users_normalized", lambda: list(users))
        monkeypatch.setattr(nav, "NavidromeClient", _FakeClient)
        result = fs._navidrome_clients()
        return result, list(_FakeClient.built)

    return _run


def _returned_users(result) -> list[str]:
    return [getattr(c, "user", "?") for c in result]


def _user(base_url: str, name: str, password: str = "pw") -> dict[str, str]:
    return {"base_url": base_url, "user": name, "pass": password}


# ---------------------------------------------------------------------------
# 1. The fix
# ---------------------------------------------------------------------------


class TestOneClientPerServer:

    def test_two_accounts_on_one_server_produce_one_client(self, clients) -> None:
        """⭐ THE REPORTED CASE."""
        result, built = clients([
            _user("https://nav.example.com", "personal"),
            _user("https://nav.example.com", "admin"),
        ])
        assert len(result) == 1, (
            "two clients on one server is what makes each delete the playlist "
            "the other just created"
        )
        assert _returned_users(result) == ["personal"], (
            "the FIRST configured account must own the playlist"
        )
        # And `built` agrees — the skipped account is never constructed at all,
        # which is the stronger statement of the same thing.
        assert [b[1] for b in built] == ["personal"]

    def test_the_first_account_is_the_owner(self, clients) -> None:
        """Whatever the order, the earlier entry owns the playlist — otherwise
        which account creates it would depend on dict ordering elsewhere.

        ⚠️ Asserted on the RETURNED list, not on construction order. The
        returned order is what decides the owner: `_sync_playlist_to_navidrome`
        iterates it, and the first client to run creates the playlist. A
        mutation that constructs both and then reorders the result passes any
        assertion made on `built` alone.
        """
        result, _built = clients([
            _user("https://nav.example.com", "admin"),
            _user("https://nav.example.com", "personal"),
        ])
        assert _returned_users(result) == ["admin"], (
            "the returned list does not lead with the first configured account"
        )

    def test_two_DIFFERENT_servers_still_produce_two_clients(self, clients) -> None:
        """⚠️ CONTROL — the dedupe must be by SERVER, not by "keep the first".

        Two genuinely different servers each need their own playlist sync; a
        fix that returned one client unconditionally would leave the second
        server silently unsynced, which is worse than the bug.
        """
        result, built = clients([
            _user("https://nav.example.com", "personal"),
            _user("https://other.example.com", "personal"),
        ])
        assert len(result) == 2
        assert [b[0] for b in built] == [
            "https://nav.example.com", "https://other.example.com",
        ]

    def test_a_third_account_on_a_known_server_is_also_skipped(self, clients) -> None:
        result, built = clients([
            _user("https://nav.example.com", "a"),
            _user("https://other.example.com", "b"),
            _user("https://nav.example.com", "c"),
        ])
        assert _returned_users(result) == ["a", "b"], (
            "the third entry must be dropped, and the surviving order preserved"
        )
        assert [b[1] for b in built] == ["a", "b"]


# ---------------------------------------------------------------------------
# 2. What counts as "the same server"
# ---------------------------------------------------------------------------


class TestServerEquivalence:
    """The dedupe is only as good as its idea of "same server"."""

    @pytest.mark.parametrize("a,b,same", [
        # Same server, cosmetic differences.
        ("https://nav.example.com", "https://nav.example.com/", True),
        ("https://nav.example.com", "https://NAV.EXAMPLE.COM", True),
        ("https://Nav.Example.com/", "https://nav.example.com", True),
        # Different servers.
        ("https://nav.example.com", "https://other.example.com", False),
        ("http://nav.example.com", "https://nav.example.com", False),
        # ⚠️ A PATH is case-SENSITIVE: a proxy mounted at /Sub/Navidrome is not
        # the same endpoint as /sub/navidrome.
        ("https://nav.example.com/Sub", "https://nav.example.com/sub", False),
        ("https://nav.example.com/nav", "https://nav.example.com", False),
    ])
    def test_the_server_key(self, a: str, b: str, same: bool) -> None:
        assert (fs._navidrome_server_key(a) == fs._navidrome_server_key(b)) is same

    def test_host_case_and_trailing_slash_collapse_to_one_client(self, clients) -> None:
        """The realistic misconfiguration: someone re-enters the same host with
        a capital letter or a trailing slash."""
        result, _built = clients([
            _user("https://nav.example.com", "one"),
            _user("https://NAV.example.com/", "two"),
        ])
        assert len(result) == 1

    def test_a_path_difference_keeps_both(self, clients) -> None:
        """CONTROL for the case-sensitivity above, at the factory level."""
        result, _built = clients([
            _user("https://nav.example.com/music", "one"),
            _user("https://nav.example.com/Music", "two"),
        ])
        assert len(result) == 2

    def test_an_unparseable_url_does_not_raise(self) -> None:
        """A malformed entry must not take the whole client list down."""
        assert fs._navidrome_server_key("not a url") == "not a url"
        assert fs._navidrome_server_key("") == ""


# ---------------------------------------------------------------------------
# 3. Incomplete entries must not reserve a server
# ---------------------------------------------------------------------------


class TestIncompleteEntries:

    def test_an_entry_missing_a_password_does_not_reserve_the_server(self, clients) -> None:
        """⭐ Otherwise a half-filled row would silently block the working one.

        The setup wizard writes stub rows with an empty base_url; an account
        saved before its password is typed is the same hazard.
        """
        result, built = clients([
            _user("https://nav.example.com", "stub", password=""),
            _user("https://nav.example.com", "real"),
        ])
        assert len(result) == 1
        assert built[0][1] == "real", (
            "the usable account was skipped because an unusable one came first"
        )

    def test_an_entry_with_no_base_url_is_ignored(self, clients) -> None:
        result, built = clients([
            _user("", "stub"),
            _user("https://nav.example.com", "real"),
        ])
        assert len(result) == 1
        assert built[0][1] == "real"


# ---------------------------------------------------------------------------
# 4. The per-USER consumers must NOT be deduped
# ---------------------------------------------------------------------------


class TestThePerUserConsumersAreUntouched:
    """⭐ The scope guard on this fix.

    Ratings and favourites are per-ACCOUNT: two accounts on one server can
    legitimately have different star ratings and different favourites. Only
    playlist operations are server-scoped, so only they may collapse.
    """

    @pytest.mark.parametrize("rel", [
        "services/navidrome/rating_sync_service.py",
        "services/favourites_service.py",
    ])
    def test_they_read_the_config_getter_not_the_client_factory(self, rel: str) -> None:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "get_navidrome_users_normalized" in source, (
            f"{rel} no longer reads the per-user list"
        )
        assert "_navidrome_clients" not in source, (
            f"{rel} now uses the playlist client factory — which is deduped to "
            "one per server, so a second account's ratings/favourites would "
            "silently stop syncing"
        )

    def test_the_client_factory_is_playlist_only(self) -> None:
        """CONTROL: the factory must not have become a general-purpose one.

        If a per-user consumer ever imports it, this fails and the scope guard
        above is what needs revisiting.
        """
        users = []
        for path in (REPO_ROOT / "services").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r"_navidrome_clients\s*\(", text):
                users.append(path.name)
        assert set(users) == {"finalise_stage.py"}, (
            f"_navidrome_clients is now used by {sorted(set(users))} — the "
            "server-scoped dedupe assumes playlist-only callers"
        )


# ---------------------------------------------------------------------------
# 5. End to end — the sync runs once per server
# ---------------------------------------------------------------------------


class TestTheSyncRunsOncePerServer:
    """The factory returning one client is only useful if the callers iterate
    it. This drives the real sync entry point and counts."""

    def test_one_sync_per_server_not_per_account(self, clients, monkeypatch) -> None:
        import services.playlists.playlist_navidrome_service as pns

        calls: list[str] = []

        _clients_list, built = clients([
            _user("https://nav.example.com", "personal"),
            _user("https://nav.example.com", "admin"),
            _user("https://other.example.com", "second-server"),
        ])

        # The factory is the REAL one (via the fixture); stub only what the
        # sync does with each client.
        monkeypatch.setattr(
            fs, "_navidrome_clients", lambda: list(_clients_list)
        )
        monkeypatch.setattr(pns, "sync_playlist_by_name",
                            lambda client, name, song_ids, **kw: calls.append(name) or {
                                "success": True, "updated": True, "created": False,
                                "unchanged": False, "deduped": 0,
                            })
        monkeypatch.setattr(fs, "_playlist_already_pushed", lambda *a, **k: False)
        monkeypatch.setattr(fs, "_record_playlist_push", lambda *a, **k: None)

        synced = fs._sync_playlist_to_navidrome("Alternative Rock - Top Tracks", ["t1"])

        assert len(calls) == 2, (
            f"expected one sync per SERVER (2), got {len(calls)} — three "
            "accounts across two servers"
        )
        assert synced["clients"] == 2

    def test_the_reported_ping_pong_cannot_start(self, clients, monkeypatch) -> None:
        """⭐ The narrow statement of the bug: one server must never have two
        clients running the dedupe pass, because each deletes the other's
        playlist."""
        clients_list, _built = clients([
            _user("https://nav.example.com", "personal"),
            _user("https://nav.example.com", "admin"),
        ])
        seen_owners = {id(c) for c in clients_list}
        assert len(seen_owners) == 1, (
            "two distinct clients on one server — the dedupe pass in each will "
            "delete the playlist the other created"
        )


# ---------------------------------------------------------------------------
# 6. The skip is visible
# ---------------------------------------------------------------------------


class TestTheSkipIsLogged:
    def test_a_skipped_account_is_reported(self, clients, monkeypatch) -> None:
        """"My second account is being ignored" must be distinguishable from a
        broken config."""
        logged: list[str] = []

        class _Logger:
            def info(self, event, **kw):
                logged.append(str(event))

            def warning(self, event, **kw):
                logged.append(str(event))

        monkeypatch.setattr(fs, "logger", _Logger())
        clients([
            _user("https://nav.example.com", "personal"),
            _user("https://nav.example.com", "admin"),
        ])
        assert any("skipped" in line.lower() for line in logged), (
            "the second account was dropped without saying so"
        )


# ---------------------------------------------------------------------------
# 7. A config change must take effect NOW, not in five minutes
# ---------------------------------------------------------------------------


class TestTheCacheIsClearedOnConfigSave:
    """⭐ The client list and the push signatures are both cached, so without an
    invalidation a `navidrome_users` edit does nothing until the TTL expires.

    That matters more than usual here: the fix for this bug IS a config change
    (removing the duplicate account), and the user would watch it "not work"
    for five minutes and reasonably conclude the fix was wrong.
    """

    def test_the_save_route_resets_the_playlist_caches(self) -> None:
        source = (REPO_ROOT / "routes" / "ui_routes.py").read_text(encoding="utf-8")
        idx = source.index("async def config_save_json")
        body = source[idx: source.index("\n@ui_bp.route", idx + 10)]
        # ⚠️ The CALL, not the NAME. `"reset_playlist_push_cache" in body` is
        # satisfied by the IMPORT line alone, so deleting the call still passed —
        # the assertion was reading the wrong thing. The escaped mutation is what
        # surfaced that; the parens are what make this a call.
        assert "reset_playlist_push_cache()" in body, (
            "a config save leaves the 300s Navidrome client cache (and the "
            "already-pushed signatures) in place"
        )
        assert "_sched_stats" in body, (
            "the reset should sit with the other post-save re-applies"
        )

    def test_the_reset_clears_BOTH_caches(self) -> None:
        """The client list AND the push signatures, via the one function.

        Clearing only the clients would leave the signature cache skipping a
        push to a just-added server, because the same playlist name had been
        pushed elsewhere moments earlier.
        """
        fs.reset_playlist_push_cache()
        fs._record_playlist_push("Alternative Rock - Top Tracks", "sig")
        assert fs._playlist_already_pushed("Alternative Rock - Top Tracks", "sig") is True

        fs.reset_playlist_push_cache()
        assert fs._playlist_already_pushed("Alternative Rock - Top Tracks", "sig") is False, (
            "the push signatures survived the reset"
        )
