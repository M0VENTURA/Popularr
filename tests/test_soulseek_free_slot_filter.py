"""Soulseek manual-search results must be FILTERED to free upload slots.

Reported
--------
> Under the active queue, when pressing the search button next to it, a Soulseek
> search modal appears. The search button doesn't do anything. This is supposed
> to wire in a manual search for soulseek, show results and allow the user to
> select a result that starts downloading. **The results are filtered showing
> only items that have a free download slot.**

That last requirement was never implemented — at EITHER layer:

1. ``services/downloads/slskd_service.py::get_search_results`` DOES read
   ``hasFreeUploadSlot`` off the slskd response into
   ``SearchResponse.has_free_upload_slot``.
2. ``routes/download_search_routes.py::slskd_search_results`` then built its
   result dict **without that field**, so it never left the server.
3. Both client filters read ``row.freeUploadSlots`` and default a MISSING value
   to 1 (keep), so they silently degraded to "keep everything":
     * ``renderSoulseekManualSearchResults`` — the manual modal
     * the search-tab renderer in ``pollSlskdSearchResults``, which even printed
       "filtered zero slots" while filtering nothing.

Net effect: the user was offered peers that cannot accept another upload — the
exact thing the filter exists to prevent — and told they had been filtered.

⚠️ WHY THESE TESTS DRIVE THE CODE INSTEAD OF SCANNING IT
--------------------------------------------------------
An earlier version asserted ``"freeUploadSlots" in source``. Mutation testing
showed it SURVIVED a change that renamed the key to ``freeUploadSlots_removed``
— the substring was still present. Reading source cannot tell "the field is
sent" apart from "a string containing the field name appears somewhere". So the
route is CALLED and its JSON inspected, and the client filter is RUN against
real rows in Node.
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from quart import Quart

from routes import download_search_routes as routes_mod
from services.downloads.slskd_service import SearchResponse

REPO_ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS_JS = REPO_ROOT / "static" / "js" / "downloads.js"


def _code_only(source: str) -> str:
    """Strip comments so an assertion cannot be satisfied by the prose."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"#[^\n]*", "", source)


def _function_body(source: str, signature_fragment: str) -> str:
    """Extract ONE function's body by brace matching.

    NOTE: Slicing from the signature to end-of-file is WRONG and was a real bug in
    this file: the slice swallowed every LATER renderer, so a mutation that
    removed the filter from this function still matched the identical filter
    text further down the file, and the guard passed on broken code.
    """
    start = source.index(signature_fragment)
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"unbalanced braces after {signature_fragment!r}")


import re  # noqa: E402  (kept beside _code_only for readability)


# ---------------------------------------------------------------------------
# Fake slskd service objects
# ---------------------------------------------------------------------------

class _FakeFile:
    def __init__(self, filename: str, size: int = 1_000_000, bitrate: int = 320):
        self.filename = filename
        self.size = size
        self.size_mb = size / (1024 * 1024)
        self.bitrate = bitrate
        self.sample_rate = 44100
        self.length = 200
        self.duration_formatted = "3:20"


class _FakeResponse:
    def __init__(self, username: str, has_free_slot: bool, files):
        self.username = username
        self.has_free_upload_slot = has_free_slot
        self.files = files
        self.upload_speed = None
        self.queue_length = 0


def _route_payload(responses, monkeypatch):
    """Call the REAL route through a real Quart app and return (status, body)."""
    app = Quart(__name__)
    app.register_blueprint(routes_mod.slskd_bp)

    monkeypatch.setattr(
        routes_mod, "get_config",
        lambda: {"slskd": {"enabled": True, "web_url": "http://x", "api_key": "k"}},
    )
    monkeypatch.setattr(routes_mod, "SlskdHttpClient", lambda *a, **k: object())

    class _Svc:
        def __init__(self, http_client=None):
            pass

        @staticmethod
        def get_search_results(search_id, timeout=None):
            return responses, "Completed", True

    monkeypatch.setattr(routes_mod, "SlskdService", _Svc)
    monkeypatch.setattr(routes_mod, "_manual_search_state", {}, raising=False)
    monkeypatch.setattr(
        routes_mod, "_log_manual_search_event", lambda **kw: None, raising=False
    )

    async def _get():
        client = app.test_client()
        resp = await client.get("/api/slskd/search/sid-1")
        return resp.status_code, json.loads(await resp.get_data())

    import asyncio

    return asyncio.run(_get())


# ===========================================================================
# 1. The server must TRANSPORT the field, and it must DISCRIMINATE
# ===========================================================================

class TestTheRouteActuallySendsTheFreeSlotField:

    def test_a_slotless_peer_is_reported_as_slotless(self, monkeypatch):
        status, body = _route_payload([
            _FakeResponse("full_peer", False, [_FakeFile("a.flac")]),
            _FakeResponse("open_peer", True, [_FakeFile("b.flac")]),
        ], monkeypatch)
        assert status == 200
        results = body["results"]
        assert len(results) == 2

        by_user = {r["username"]: r for r in results}
        assert "freeUploadSlots" in by_user["full_peer"], (
            "the route does not send freeUploadSlots, so every client filter "
            "defaults it to 1 and keeps peers that cannot accept an upload"
        )
        # The VALUES must differ. A missing key, a renamed key, or a hard-coded
        # constant all fail here.
        assert by_user["full_peer"]["freeUploadSlots"] == 0
        assert by_user["open_peer"]["freeUploadSlots"] == 1

    def test_the_snake_case_alias_agrees_with_the_camel_case_one(self, monkeypatch):
        _, body = _route_payload([
            _FakeResponse("full_peer", False, [_FakeFile("a.flac")]),
        ], monkeypatch)
        row = body["results"][0]
        assert row["has_free_upload_slot"] is False
        assert row["freeUploadSlots"] == 0

    def test_every_file_of_one_peer_shares_its_slot_state(self, monkeypatch):
        """The slot belongs to the PEER, so it must not vary per file."""
        _, body = _route_payload([
            _FakeResponse("full_peer", False, [_FakeFile("a.flac"), _FakeFile("b.flac")]),
        ], monkeypatch)
        assert [r["freeUploadSlots"] for r in body["results"]] == [0, 0]

    def test_the_result_set_is_not_reduced_by_the_slot_state(self, monkeypatch):
        """The SERVER reports; the CLIENT filters.

        Filtering server-side too would hide the reason from the UI, which is
        what prints "no results with free upload slots".
        """
        _, body = _route_payload([
            _FakeResponse("full_peer", False, [_FakeFile("a.flac")]),
            _FakeResponse("open_peer", True, [_FakeFile("b.flac")]),
        ], monkeypatch)
        assert len(body["results"]) == 2

    def test_the_service_still_reads_the_flag_from_slskd(self):
        from services.downloads import slskd_service as svc

        src = _code_only(inspect.getsource(svc.SlskdService.get_search_results))
        assert "hasFreeUploadSlot" in src, (
            "the service stopped reading slskd's hasFreeUploadSlot, so the "
            "dataclass now always reports the default"
        )

    def test_the_dataclass_defaults_to_permissive(self):
        """Default True: an UNKNOWN peer must not be hidden by accident.

        The filter exists to drop a peer known to be full, not to drop one whose
        state could not be read.
        """
        assert SearchResponse(username="u", files=[]).has_free_upload_slot is True


# ===========================================================================
# 2. The CLIENT filter must actually drop a slotless peer
# ===========================================================================
#
# Driven in Node against the REAL renderer, because asserting on source text
# could not tell "filters" from "mentions freeUploadSlots somewhere".

_NODE_STUB = r"""
const elements = {};
function makeEl(tag) {
  const el = {
    tagName: String(tag||'div').toUpperCase(),
    children: [], attributes: {}, style: {}, dataset: {},
    _classes: new Set(), _html: '', _text: '', value: '',
    classList: { add(c){el._classes.add(c);}, remove(c){el._classes.delete(c);},
                 contains(c){return el._classes.has(c);}, toggle(){} },
    setAttribute(k,v){el.attributes[k]=String(v);},
    getAttribute(k){return k in el.attributes?el.attributes[k]:null;},
    removeAttribute(k){delete el.attributes[k];},
    appendChild(c){el.children.push(c); c.parentNode=el; return c;},
    insertAdjacentHTML(_p,h){ el._html += String(h); },
    insertAdjacentElement(_p,c){ (el.parentNode||el).children.push(c); return c; },
    querySelector(s){return null;}, querySelectorAll(){return [];},
    addEventListener(){}, remove(){}, focus(){}, closest(){return null;},
  };
  Object.defineProperty(el,'innerHTML',{get(){return el._html;},set(v){el._html=String(v);}});
  Object.defineProperty(el,'textContent',{get(){return el._text;},set(v){el._text=String(v);}});
  return el;
}
const document = {
  body: makeEl('body'),
  getElementById(id){ if(!(id in elements)){const e=makeEl('div'); e.attributes.id=id; elements[id]=e;} return elements[id]; },
  querySelector(){return null;}, querySelectorAll(){return [];},
  createElement(t){return makeEl(t);}, addEventListener(){},
};
global.window = global;
global.document = document;
global.CSS = { escape: v => String(v) };
global.alert = () => {};
global.confirm = () => true;
global.setTimeout = setTimeout;
global.clearTimeout = clearTimeout;
global.AbortController = class { constructor(){this.signal={};} abort(){} };
global.AbortSignal = { any: () => undefined };
global.bootstrap = { Modal: { getOrCreateInstance: () => ({show(){},hide(){}}) } };
global.fetch = () => Promise.resolve({ ok:true, status:200, text: () => Promise.resolve('{}') });
"""


def _run_client_filter(rows: list[dict]) -> str:
    """Render the manual-search results table for *rows*; return the HTML."""
    program = (
        _NODE_STUB
        + "\nconst src = require('fs').readFileSync("
        + json.dumps(str(DOWNLOADS_JS))
        + ", 'utf8');\n(0, eval)(src);\n"
        + "const container = document.getElementById('soulseekManualResults');\n"
        + "renderSoulseekManualSearchResults(" + json.dumps(rows) + ");\n"
        + "console.log(JSON.stringify({ html: container.innerHTML }));\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "h.js"
        harness.write_text(program, encoding="utf-8")
        proc = subprocess.run(["node", str(harness)], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])["html"]


needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to drive the renderer"
)


@needs_node
class TestTheClientFilterExcludesSlotlessPeers:

    def test_a_slotless_peer_is_not_offered(self):
        html = _run_client_filter([
            {"username": "open_peer", "filename": "good.flac", "size": 100, "freeUploadSlots": 1},
            {"username": "full_peer", "filename": "bad.flac", "size": 100, "freeUploadSlots": 0},
        ])
        assert "open_peer" in html
        assert "full_peer" not in html, (
            "a peer with no free upload slot was still offered — the filter is "
            "not in effect"
        )

    def test_all_slotless_peers_produce_the_empty_message(self):
        html = _run_client_filter([
            {"username": "full_peer", "filename": "bad.flac", "size": 100, "freeUploadSlots": 0},
        ])
        assert "full_peer" not in html
        assert "free upload slot" in html.lower(), (
            "with every result filtered out the UI must say so rather than "
            "render an empty table"
        )

    def test_a_peer_with_no_slot_field_is_still_offered(self):
        """Unknown must not mean hidden (same policy as the dataclass default)."""
        html = _run_client_filter([
            {"username": "unknown_peer", "filename": "x.flac", "size": 100},
        ])
        assert "unknown_peer" in html

    def test_the_offered_count_is_reported(self):
        """A 2 -> 1 drop must be stated, not silent."""
        html = _run_client_filter([
            {"username": "open_peer", "filename": "good.flac", "size": 100, "freeUploadSlots": 1},
            {"username": "full_peer", "filename": "bad.flac", "size": 100, "freeUploadSlots": 0},
        ])
        assert "available result" in html.lower(), (
            "the table should say how many results are actually being offered"
        )


@needs_node
class TestTheSearchTabRendererAlsoFilters:

    def test_the_renderer_body_filters_on_the_slot_count(self):
        """Assert the FILTER EXPRESSION, not merely that the name appears.

        Source text is the only practical handle here because the inner renderer
        is a closure inside a polling function that cannot be invoked in
        isolation — so the assertion targets the COMPARISON, which a
        "return true" mutation cannot satisfy.

        ⚠️ The comparison spans two lines:
            const slots = r.freeUploadSlots !== undefined ? r.freeUploadSlots : 1;
            return slots > 0;
        A regex demanding `freeUploadSlots` and `> 0` on the SAME line matches
        nothing and would fail on correct code.
        """
        src = DOWNLOADS_JS.read_text(encoding="utf-8", errors="replace")
        body = _function_body(src, "async function pollSlskdSearchResults")
        assert "freeUploadSlots" in body, (
            "pollSlskdSearchResults no longer reads the slot count, so its "
            "'filtered zero slots' message is a lie"
        )
        assert re.search(r"slots\s*>\s*0", body), (
            "pollSlskdSearchResults no longer compares the slot count against "
            "zero — the filter degraded to 'keep everything'"
        )

    def test_the_manual_renderer_also_uses_a_real_comparison(self):
        src = DOWNLOADS_JS.read_text(encoding="utf-8", errors="replace")
        body = _function_body(src, "function renderSoulseekManualSearchResults")
        assert re.search(r"freeUploadSlots", body)
        assert re.search(r"slots\s*>\s*0", body), (
            "renderSoulseekManualSearchResults no longer compares slots > 0"
        )
