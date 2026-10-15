"""A large album save's disk phase runs in the background.

The reported problem
--------------------
> When saving an album that has a lot of tracks, it times out due to cloudflare
> time out, can this be added to a background service so the app can continue and
> not wait for the save to complete

Cloudflare gives the origin **100 seconds** and then returns **524**. An album
save is a synchronous form POST, so the whole save must fit in that window — and
the disk phase touches every audio file.

What these tests exist to pin
-----------------------------
1. **The database half stays in the request.** That is what makes a backgrounded
   save a *durable* save: the edit is committed before the redirect, so a later
   failure costs the file tags, not the edit.
2. **⚠️ THE RESCAN MOVES WITH THE FILE WRITES.** This is the one that would
   silently corrupt behaviour. Navidrome serves its own database and only
   refreshes on a re-scan; a rescan that ran before the tags were on disk would
   import the PRE-save values and the next import would put the old metadata back
   — the exact bug ``navidrome_rescan_service`` exists to prevent. So the rescan
   must be the disk phase's LAST step, and a backgrounded save must not rescan
   from the request.
3. **One job per album, newest payload wins.** Two threads writing the same files
   from two different payloads is the failure mode coalescing prevents.
4. **A failure is visible.** The user has navigated away, so a swallowed error
   would be a silently untagged album.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from routes import ui_routes as ui
from services.metadata import album_file_sync_service as afs

REPO_ROOT = Path(__file__).resolve().parents[1]

ROUTE_SOURCE = ui.__file__ and Path(ui.__file__).read_text(encoding="utf-8")

CONFIG_PAGES = (
    REPO_ROOT / "templates" / "pages" / "config.html",
    REPO_ROOT / "test_site" / "templates" / "Pages" / "config.html",
)

CONFIG_JS = (
    REPO_ROOT / "static" / "js" / "config.js",
    REPO_ROOT / "test_site" / "static" / "js" / "pages" / "config.js",
)


@pytest.fixture(autouse=True)
def _clean_service():
    afs.reset_album_file_sync_state()
    yield
    afs.reset_album_file_sync_state()


@pytest.fixture
def recorded(monkeypatch):
    """Record the ORDER of the disk phase's three steps."""
    order: list[str] = []
    calls: dict[str, object] = {}

    def _fake_write(track_id, track, payload, *, strip_disc_numbers, disc_staged):
        order.append(f"tag:{track_id}")
        return (True, f"/music/{track_id}.mp3")

    def _fake_embed(artist, album, image, mime="image/jpeg"):
        order.append("cover")
        calls["cover_args"] = (artist, album)
        return 2

    def _fake_rescan(reason="metadata save"):
        order.append("rescan")
        calls["rescan_reason"] = reason
        return True

    monkeypatch.setattr(ui, "_write_album_track_file_tags", _fake_write)
    monkeypatch.setattr(
        "services.enrichment.album_art_service.apply_album_art_to_tracks", _fake_embed
    )
    monkeypatch.setattr(
        "services.scanning.navidrome_rescan_service.request_rescan", _fake_rescan
    )
    return order, calls


def _payload(*, tracks: int = 3, cover: bool = False, rescan: bool = True, runner=None):
    return {
        "runner": runner if runner is not None else ui._run_album_file_sync_phases,
        "file_jobs": [
            (f"t{i}", {"id": f"t{i}"}, {"id": f"t{i}"}, False, False)
            for i in range(tracks)
        ],
        "album_artist": "Portishead",
        "album": "Dummy",
        "cover_bytes": b"\x89PNG\r\n\x1a\nfake" if cover else None,
        "cover_mime": "image/png",
        "should_rescan": rescan,
    }


# ---------------------------------------------------------------------------
# The disk phase itself
# ---------------------------------------------------------------------------

def test_the_disk_phase_writes_every_track(recorded) -> None:
    order, _ = recorded

    result = ui._run_album_file_sync_phases(_payload(tracks=5))

    assert result["file_failures"] == 0
    assert result["tracks"] == 5
    assert sorted(o for o in order if o.startswith("tag:")) == [
        f"tag:t{i}" for i in range(5)
    ]


def test_the_rescan_runs_AFTER_the_file_writes(recorded) -> None:
    """⚠️ THE INVARIANT THAT MAKES BACKGROUNDING SAFE.

    A rescan before the tags are on disk makes Navidrome import the pre-save
    values, and the next Popularr import then writes those back over the edit.
    """
    order, _ = recorded

    ui._run_album_file_sync_phases(_payload(tracks=4))

    assert order[-1] == "rescan", f"the rescan must be LAST, got {order}"
    assert all(o.startswith("tag:") for o in order[:-1]), order


def test_the_rescan_runs_after_the_cover_embed(recorded) -> None:
    order, _ = recorded

    ui._run_album_file_sync_phases(_payload(tracks=2, cover=True))

    assert order.index("cover") < order.index("rescan")


def test_the_cover_embed_uses_the_TARGET_names(recorded) -> None:
    """The album may be being renamed, and the embed looks its rows up by name."""
    _, calls = recorded

    ui._run_album_file_sync_phases(_payload(tracks=1, cover=True))

    assert calls["cover_args"] == ("Portishead", "Dummy")


def test_no_rescan_when_the_payload_says_so(recorded) -> None:
    """``should_rescan`` is decided from the DATABASE half's counters."""
    order, _ = recorded

    ui._run_album_file_sync_phases(_payload(tracks=2, rescan=False))

    assert "rescan" not in order


def test_a_failed_tag_write_is_counted(monkeypatch) -> None:
    def _fails(track_id, track, payload, *, strip_disc_numbers, disc_staged):
        return (False, "")

    monkeypatch.setattr(ui, "_write_album_track_file_tags", _fails)

    result = ui._run_album_file_sync_phases(_payload(tracks=3, rescan=False))

    assert result["file_failures"] == 3


def test_a_cover_embed_failure_does_not_lose_the_tag_writes(recorded, monkeypatch) -> None:
    """Art is enrichment; it must never be a reason the tags did not land."""
    order, _ = recorded

    def _boom(*a, **kw):
        raise RuntimeError("image host exploded")

    monkeypatch.setattr(
        "services.enrichment.album_art_service.apply_album_art_to_tracks", _boom
    )

    result = ui._run_album_file_sync_phases(_payload(tracks=3, cover=True))

    assert result["file_failures"] == 0
    assert result["cover_embedded"] == 0
    assert order.count("rescan") == 1


# ---------------------------------------------------------------------------
# The service: enqueue, coalesce, report
# ---------------------------------------------------------------------------

def test_enqueue_runs_the_runner() -> None:
    seen: list[dict] = []

    def _runner(payload):
        seen.append(payload)
        return {"file_failures": 0}

    afs.enqueue_album_file_sync(
        artist="Portishead", album="Dummy", payload=_payload(runner=_runner)
    )

    assert afs.wait_for_album_file_sync("Portishead", "Dummy", timeout=5)
    assert len(seen) == 1
    assert len(seen[0]["file_jobs"]) == 3


def test_a_second_save_coalesces_onto_the_running_job() -> None:
    """⚠️ One thread per album — never two writing the same files.

    Two threads with two payloads could interleave writes to the same file; the
    coalesced form guarantees the LAST save is the one that lands.
    """
    import threading

    release = threading.Event()
    ran: list[int] = []

    def _runner(payload):
        ran.append(len(payload["file_jobs"]))
        if len(ran) == 1:
            release.wait(timeout=5)
        return {}

    afs.enqueue_album_file_sync(
        artist="Portishead", album="Dummy", payload=_payload(tracks=1, runner=_runner)
    )
    # Wait for the first run to be in flight, then enqueue a newer save.
    for _ in range(100):
        if ran:
            break
        threading.Event().wait(0.02)
    assert ran, "the first job never started"

    second = afs.enqueue_album_file_sync(
        artist="Portishead", album="Dummy", payload=_payload(tracks=7, runner=_runner)
    )

    assert second["coalesced"] is True, "the second save started a second thread"
    release.set()
    assert afs.wait_for_album_file_sync("Portishead", "Dummy", timeout=5)
    assert ran == [1, 7], (
        f"the running thread must pick up the NEWER payload, got {ran}"
    )


def test_the_album_key_is_case_insensitive() -> None:
    """The same album reached with different casing must not start two jobs."""
    assert afs._key("Portishead", "Dummy") == afs._key("portishead", "DUMMY")


def test_a_runner_failure_is_recorded_and_reported() -> None:
    def _boom(payload):
        raise RuntimeError("disk went away")

    afs.enqueue_album_file_sync(
        artist="Portishead", album="Dummy", payload=_payload(runner=_boom)
    )
    afs.wait_for_album_file_sync("Portishead", "Dummy", timeout=5)

    state = afs.get_album_file_sync_state()
    assert state["recent"], "the run was not recorded"
    entry = state["recent"][0]
    assert entry["success"] is False
    assert "disk went away" in entry["error"], (
        "the failure reason must survive — the user has already navigated away"
    )
    assert "duration_ms" in entry


def test_a_payload_without_a_runner_is_an_error_not_a_silent_no_op() -> None:
    """A missing runner is a programming error; silence would hide it."""
    afs.enqueue_album_file_sync(
        artist="Portishead", album="Dummy", payload={"file_jobs": []}
    )
    afs.wait_for_album_file_sync("Portishead", "Dummy", timeout=5)

    entry = afs.get_album_file_sync_state()["recent"][0]
    assert entry["success"] is False
    assert "runner" in entry["error"]


def test_status_reports_running_and_recent() -> None:
    afs.enqueue_album_file_sync(
        artist="Portishead", album="Dummy", payload=_payload(runner=lambda p: {})
    )
    afs.wait_for_album_file_sync("Portishead", "Dummy", timeout=5)

    state = afs.get_album_file_sync_state()
    assert state["running_count"] == 0
    assert state["pending_count"] == 0
    assert state["recent"][0]["album"] == "Dummy"


def test_the_recent_tail_is_bounded() -> None:
    """It is a diagnostic tail, not a history — it must not grow without bound."""
    for i in range(afs._RECENT_LIMIT + 5):
        afs.enqueue_album_file_sync(
            artist="A", album=f"Album {i}", payload=_payload(runner=lambda p: {})
        )
        afs.wait_for_album_file_sync("A", f"Album {i}", timeout=5)

    assert len(afs.get_album_file_sync_state()["recent"]) == afs._RECENT_LIMIT


def test_enqueued_reports_a_pending_album() -> None:
    assert afs.album_file_sync_enqueued("Portishead", "Dummy") is False
    afs.enqueue_album_file_sync(
        artist="Portishead", album="Dummy", payload=_payload(runner=lambda p: {})
    )
    afs.wait_for_album_file_sync("Portishead", "Dummy", timeout=5)
    # Cleared once the work is done, so a later save is not reported as coalesced.
    assert afs.album_file_sync_enqueued("Portishead", "Dummy") is False


def test_a_dead_worker_slot_is_restarted() -> None:
    """A slot whose thread died must not make the album permanently unprocessable."""
    import threading

    key = afs._key("Portishead", "Dummy")
    with afs._state_lock:
        afs._jobs[key] = afs._AlbumJob("Portishead", "Dummy", {}, "test")
        afs._threads[key] = threading.Thread(target=lambda: None)  # never started

    ran: list[bool] = []
    result = afs.enqueue_album_file_sync(
        artist="Portishead",
        album="Dummy",
        payload=_payload(runner=lambda p: (ran.append(True) or {})),
    )

    assert result["coalesced"] is False
    assert afs.wait_for_album_file_sync("Portishead", "Dummy", timeout=5)
    assert ran == [True]


# ---------------------------------------------------------------------------
# The route's decision
# ---------------------------------------------------------------------------

class TestTheSaveDecidesInlineOrBackground:
    """The threshold, read from the route's own window."""

    @staticmethod
    def _save() -> str:
        source = ROUTE_SOURCE
        return source[source.index("async def album_detail"):]

    def test_the_threshold_gates_the_enqueue(self) -> None:
        save = self._save()
        assert "get_album_save_background_config()" in save
        assert "enqueue_album_file_sync(" in save, (
            "the save never hands the disk phase to the background service"
        )
        assert "len(_file_jobs) >= int(" in save, (
            "the branch must count the FILES that will be written — that is what "
            "costs the wall clock — not the album's row count"
        )

    def test_the_inline_path_is_unchanged(self) -> None:
        """A small album must still finish its tags inside the request."""
        save = self._save()
        assert "await asyncio.to_thread(_run_album_file_sync_phases, _file_payload)" in save, (
            "the inline path must still run the SAME disk phase, off the event loop"
        )
        assert "if not _file_backgrounded:" in save

    def test_a_failed_enqueue_falls_back_to_INLINE(self) -> None:
        """The safe direction: the DB half is durable, and inline is what the
        user asked for — it just risks the timeout it always did."""
        save = self._save()
        assert "_file_backgrounded = False" in save
        assert "running it inline" in save

    def test_should_rescan_comes_from_the_database_half(self) -> None:
        """The background thread cannot see the handler's counters."""
        save = self._save()
        assert '"should_rescan"' in save
        assert "updated_count > 0 or reverted_live_count > 0 or _cover_bytes" in save, (
            "should_rescan must be derived from values only the DB half knows"
        )

    def test_the_route_no_longer_rescans_by_itself(self) -> None:
        """⚠️ A rescan here would fire BEFORE a backgrounded save's tags land.

        That is the "the next import puts the old data back" bug: Navidrome would
        scan the pre-save files and Popularr's next import would read those rows
        back over the edit.
        """
        save = self._save()
        assert "request_rescan(" not in save, (
            "the rescan belongs to the disk phase, which runs it after the writes"
        )
        assert "THE RESCAN MOVED, IT WAS NOT DELETED" in save, (
            "keep the explanation: the next reader will otherwise re-add it here"
        )

    def test_the_cover_is_stored_before_the_branch(self) -> None:
        """Art is stored in the DB (visible immediately); only the EMBED is disk.

        ⚠️ Both halves matter, and the second is the one that is easy to get
        wrong. It is not enough for the store to appear earlier in the file — it
        must not be GATED on the background decision either. A store guarded by
        ``and not _file_backgrounded`` still sits before the branch textually
        while doing nothing for exactly the saves that need it, so the album page
        would show no art until the job happened to run.
        """
        save = self._save()
        store_at = save.index("save_album_art_to_db(")
        branch_at = save.index("get_album_save_background_config()")
        assert store_at < branch_at, (
            "the album_art row must be written in the request — it is what makes "
            "the new art visible on the page"
        )

        # The condition guarding the store must not mention the branch flag.
        lines = save[:store_at].splitlines()
        guard = ""
        for line in reversed(lines):
            stripped = line.strip()
            if stripped.startswith("if "):
                guard = stripped
                break
        assert guard, "could not find the condition guarding save_album_art_to_db"
        assert "_file_backgrounded" not in guard, (
            f"the cover store is gated on the background decision ({guard!r}) — a "
            "backgrounded save would show no art until the job ran"
        )

    def test_the_response_says_the_tagging_is_in_the_background(self) -> None:
        save = self._save()
        assert "Tagging {len(_file_jobs)} audio file(s) in the background" in save, (
            "a backgrounded save must SAY so — otherwise the user cannot tell "
            "whether the file tags were written"
        )

    def test_the_phase_log_still_reports_every_phase(self) -> None:
        """Without numbers the next timeout report is a guess."""
        flat = " ".join(ROUTE_SOURCE.split())
        for field in (
            '"Album save phases"', "mb_backfill_ms=", "write_loop_ms=",
            "file_tags_ms=", "total_ms=",
        ):
            assert field in flat, f"the phase log is missing {field}"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_defaults_keep_small_albums_inline() -> None:
    from helpers.config_helpers import get_album_save_background_config

    cfg = get_album_save_background_config()
    assert cfg["enabled"] is True
    assert cfg["track_threshold"] == 40, (
        "the default must be well clear of the albums that already fit inside "
        "the origin timeout"
    )


def test_config_reads_the_saved_block(monkeypatch) -> None:
    monkeypatch.setattr(
        "helpers.config_helpers.get_config",
        lambda: {"album_save": {"background": {"enabled": False, "track_threshold": 5}}},
    )
    from helpers.config_helpers import get_album_save_background_config

    cfg = get_album_save_background_config()
    assert cfg["enabled"] is False
    assert cfg["track_threshold"] == 5


def test_config_tolerates_junk(monkeypatch) -> None:
    monkeypatch.setattr(
        "helpers.config_helpers.get_config",
        lambda: {"album_save": {"background": {"track_threshold": "lots"}}},
    )
    from helpers.config_helpers import get_album_save_background_config

    assert get_album_save_background_config()["track_threshold"] == 40


def test_config_clamps_a_negative_threshold(monkeypatch) -> None:
    """A negative threshold must not make the comparison meaningless."""
    monkeypatch.setattr(
        "helpers.config_helpers.get_config",
        lambda: {"album_save": {"background": {"track_threshold": -5}}},
    )
    from helpers.config_helpers import get_album_save_background_config

    assert get_album_save_background_config()["track_threshold"] == 0


def test_sanitizer_keeps_the_nested_block_a_dict() -> None:
    from routes.ui_routes import _sanitize_config_sections

    cleaned = _sanitize_config_sections(
        {"album_save": {"background": {"enabled": True, "track_threshold": 20}}}
    )
    assert cleaned["album_save"]["background"] == {"enabled": True, "track_threshold": 20}


def test_sanitizer_coerces_a_scalar_block() -> None:
    from routes.ui_routes import _sanitize_config_sections

    cleaned = _sanitize_config_sections({"album_save": {"background": "yes"}})
    assert cleaned["album_save"]["background"] == {}


# ---------------------------------------------------------------------------
# UI parity — both trees
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("page", CONFIG_PAGES, ids=lambda p: p.parts[-3])
def test_config_page_offers_the_settings(page: Path) -> None:
    html = page.read_text(encoding="utf-8")
    for field in ("album_save_background_enabled", "album_save_background_threshold"):
        assert field in html, f"{page.name} is missing {field}"
    assert "get('album_save', {}).get('background'" in html


@pytest.mark.parametrize("js", CONFIG_JS, ids=lambda p: p.parent.name)
def test_config_js_collects_the_block(js: Path) -> None:
    source = js.read_text(encoding="utf-8")
    assert "album_save: {" in source
    assert "background: {" in source
    assert "album_save_background_threshold" in source


# ---------------------------------------------------------------------------
# The status endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_endpoint_reports_the_jobs(client) -> None:
    afs.enqueue_album_file_sync(
        artist="Portishead", album="Dummy", payload=_payload(runner=lambda p: {})
    )
    afs.wait_for_album_file_sync("Portishead", "Dummy", timeout=5)

    response = await client.get("/api/maintenance/album-file-sync")

    assert response.status_code == 200
    body = await response.get_json()
    assert body["recent"][0]["album"] == "Dummy"


@pytest.mark.asyncio
async def test_drain_waits_for_one_album(client) -> None:
    afs.enqueue_album_file_sync(
        artist="Portishead", album="Dummy", payload=_payload(runner=lambda p: {})
    )

    response = await client.post(
        "/api/maintenance/album-file-sync/drain",
        json={"artist": "Portishead", "album": "Dummy", "timeout": 10},
    )

    assert response.status_code == 200
    assert (await response.get_json())["drained"] is True


@pytest.mark.asyncio
async def test_drain_requires_both_names(client) -> None:
    response = await client.post(
        "/api/maintenance/album-file-sync/drain", json={"artist": "Portishead"}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_drain_rejects_a_bad_timeout(client) -> None:
    response = await client.post(
        "/api/maintenance/album-file-sync/drain",
        json={"artist": "A", "album": "B", "timeout": "soon"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_a_drain_of_an_unknown_album_is_a_no_op(client) -> None:
    """Nothing queued for that album means there is nothing to wait for."""
    response = await client.post(
        "/api/maintenance/album-file-sync/drain",
        json={"artist": "Nobody", "album": "Nothing", "timeout": 1},
    )
    assert response.status_code == 200
    assert (await response.get_json())["drained"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/maintenance/album-file-sync"),
        ("post", "/api/maintenance/album-file-sync/drain"),
    ],
)
async def test_the_routes_require_a_session(
    unauthed_client, method: str, path: str
) -> None:
    response = await getattr(unauthed_client, method)(path, json={})
    assert response.status_code in (401, 403, 302), (
        f"{method.upper()} {path} was reachable without a session"
    )
