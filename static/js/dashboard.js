/**
 * Dashboard page JavaScript — scan controls, recent scans, upcoming releases, unified log.
 * Loaded separately from the esbuild bundle because it is page-specific.
 */

// Global error handler for development debugging
window.onerror = function (msg, url, line) {
  console.error(`Error: ${msg} on line ${line} in ${url}`);
};

// ===== Shared helpers =====
async function postJSON(u, b) {
  const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
  return r.json().catch(() => ({}));
}

/**
 * POST to a scan stop endpoint and report what actually happened.
 *
 * The bare `fetch(url, { method: "POST" })` this replaces sent no JSON signal,
 * so the route answered with its flash+redirect HTML page. The stop flag WAS
 * set, but a failure — an expired session redirecting to the login page, or a
 * 500 — was silently indistinguishable from success, and the button appeared
 * to do nothing.
 *
 * Sending `Accept: application/json` makes the route negotiate a JSON reply
 * (see routes/scan_routes/_common.py::stop_response), so the result is
 * checkable. Returns {ok, message}.
 */
async function stopScanRequest(url) {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Accept": "application/json", "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json().catch(() => null);
    if (!response.ok || !data || data.success === false) {
      const reason = (data && data.error) || `HTTP ${response.status}`;
      console.error(`Stop request failed for ${url}: ${reason}`);
      return { ok: false, message: reason };
    }
    return { ok: true, message: data.message || "Stop requested" };
  } catch (error) {
    console.error(`Stop request error for ${url}:`, error);
    return { ok: false, message: error && error.message ? error.message : String(error) };
  }
}

function fE(s) {
  if (!s) return "";
  return ` — ${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
}

function escapeHtml(str) {
  if (!str) return "";
  const d = document.createElement("div");
  d.appendChild(document.createTextNode(str));
  return d.innerHTML;
}

// ===== Scan Card Polling =====
(async function () {
  try {
    const r = await fetch("/api/navidrome/scan/status");
    const d = await r.json();
    const e = document.getElementById("nav-connectivity");
    if (d.success !== false) {
      e.className = "badge bg-success ms-1";
      e.title = "OK";
      e.textContent = "\u2713";
    } else {
      e.className = "badge bg-danger ms-1";
      e.title = "Unreachable";
      e.textContent = "\u2717";
    }
  } catch (_) {}
})();

async function startPopularityScan(m, force, restart, resumeFrom) {
  if (window.ScanPreflight) {
    const scanName = window.ScanPreflight.label(m || 'popularity');
    if (!await window.ScanPreflight.confirmIfRunning({ scanName })) return;
  }
  const body = { mode: m || "popularity", force: !!force, restart: !!restart };
  // Explicit resume point from the picker. The server treats a missing value as
  // "use the stored checkpoint", so it is only sent when the user chose one.
  if (resumeFrom) body.resume_from = resumeFrom;
  await postJSON("/api/popularity/run", body);
}

async function stopPopularityScan() {
  return stopScanRequest("/scan/stop-popularity");
}

// Runs the popularity scan selected in the dashboard selector with the
// current Force / Restart checkbox states.  No scan starts until Run is
// pressed.  Restart clears the resume checkpoint so the scan begins from the
// top (skipping recently-scanned items unless Force is also checked).
//
// With Restart UNCHECKED the user is continuing a previous run, so they are
// asked which artist to resume from first.  Restart already means "from the
// top", so prompting there would be contradictory.
async function runDashboardPopularityScan() {
  const mode = document.getElementById("popScanSelector")?.value || "popularity";
  const force = !!document.getElementById("popScanForce")?.checked;
  const restart = !!document.getElementById("popScanRestart")?.checked;
  const btn = document.getElementById("popScanRunBtn");
  const original = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Starting…';
  }
  try {
    // Resume prompt — only when continuing (Restart unchecked). `resume_from`
    // is honoured by every mode (the load stage skips to that artist).
    let resumeFrom = null;
    if (!restart && window.ScanResumePicker) {
      const choice = await window.ScanResumePicker.chooseResumeArtist();
      if (!choice) return;   // cancelled — do not start
      resumeFrom = choice.resume_from || null;
    }
    await startPopularityScan(mode, force, restart, resumeFrom);
  } catch (e) {
    console.error("Error starting popularity scan:", e);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  }
}

async function pollPopularityStatus() {
  const r = await fetch("/api/popularity/status");
  const d = await r.json();
  if (!d.success) return;
  const e = document.getElementById("pop-status");
  if (!e) return;
  if (d.running) {
    e.innerText = `${d.message || "Running..."}${fE(d.elapsed_seconds)}`;
    e.className = "text-primary small";
  } else {
    e.innerText = d.message || "Idle";
    e.className = "text-muted small";
  }
}

async function startNavidromeImport() {
  // "Forced" = mode:force → serial full re-import of every artist, ignoring
  // the new-items/changed-album skips.  Unchecked = mode:all → normal import
  // that skips unchanged albums.
  const force = !!document.getElementById("navImportForce")?.checked;
  const e = document.getElementById("nav-status");

  if (window.ScanPreflight
      && !await window.ScanPreflight.confirmIfRunning({ scanName: 'Navidrome Import' })) return;

  const d = await postJSON("/api/navidrome/import", { mode: force ? "force" : "all" });
  if (d.success) {
    e.innerText = d.message || "Started";
    e.className = "text-success small";
    e.dataset.started = String(Date.now());
  } else {
    e.innerText = d.error || "Failed";
    e.className = "text-danger small";
  }
}

async function startNavidromeServerScan() {
  if (window.ScanPreflight
      && !await window.ScanPreflight.confirmIfRunning({ scanName: 'Navidrome Server Scan' })) return;
  const d = await postJSON("/api/navidrome/scan/start");
  const e = document.getElementById("nav-status");
  e.innerText = d.success ? "Server scan triggered" : "Failed";
  e.className = d.success ? "text-success small" : "text-danger small";
}

async function stopNavidromeSync() {
  return stopScanRequest("/scan/stop-navidrome");
}

async function pollNavidromeStatus() {
  const e = document.getElementById("nav-status");
  if (!e) return;
  const pd = await (await fetch("/api/scan-progress")).json().catch(() => ({}));
  const ns = (pd.active_scans || []).find(s => s.scan_type === "navidrome_scan");
  
  if (ns && ns.is_running) {
    e.innerText = `${ns.message || "Importing..."}${fE(ns.elapsed_seconds)}`;
    e.className = "text-primary small";
    if (e.dataset.started) delete e.dataset.started;
    return;
  }
  
  const sd = await (await fetch("/api/navidrome/scan/status")).json().catch(() => ({}));
  if (sd.scanning) {
    e.innerText = "Server scanning...";
    e.className = "text-warning small";
    if (e.dataset.started) delete e.dataset.started;
  } else if (e.dataset.started) {
    const elapsed = Date.now() - parseInt(e.dataset.started, 10);
    if (elapsed < 120000) {
      e.innerText = "Importing...";
      e.className = "text-primary small";
    } else {
      delete e.dataset.started;
      e.innerText = "Idle";
      e.className = "text-muted small";
    }
  } else {
    e.innerText = "Idle";
    e.className = "text-muted small";
  }
}

async function startEssentiaScan() {
  if (window.ScanPreflight
      && !await window.ScanPreflight.confirmIfRunning({ scanName: 'Essentia Mood Scan' })) return;
  await fetch("/api/essentia/run", { method: "POST" });
}

async function stopEssentiaScan() {
  return stopScanRequest("/scan/stop-essentia-mood");
}

async function pollEssentiaStatus() {
  const e = document.getElementById("ess-status");
  if (!e) return;
  const pd = await (await fetch("/api/scan-progress")).json().catch(() => ({}));
  const es = (pd.active_scans || []).find(s => s.scan_type === "essentia_mood_scan");
  if (es && es.is_running) {
    e.innerText = `${es.message || "Running..."}${fE(es.elapsed_seconds)}`;
    e.className = "text-primary small";
  } else {
    e.innerText = "Idle";
    e.className = "text-muted small";
  }
}

async function stopAllScans() {
  const result = await stopScanRequest("/scan/stop-all");
  if (!result.ok) {
    // Previously silent: an expired session (redirect to the login page) or a
    // server error left the operator believing the scans had been stopped.
    alert(`Could not stop scans: ${result.message}`);
  }
}

// ===== Recent Scans (Dynamic) =====
const SCAN_TYPE_DISPLAY_NAMES = {
  navidrome: "Navidrome Import",
  metadata: "Metadata Scan",
  popularity: "Popularity Scan",
  singles: "Singles Detection",
  singles_detection: "Singles Detection",
  essentia: "Essentia Mood Scan",
  mood: "Mood Scan",
  combined: "Combined Scan",
  all: "Full Scan (All)",
  navidrome_scan: "Navidrome Import",
  metadata_lookup_scan: "Metadata Scan",
  popularity_scan: "Popularity Scan",
  singles_scan: "Singles Detection",
  mood_scan: "Mood Scan",
  essentia_mood_scan: "Essentia Mood Scan",
  combined_scan: "Combined Scan",
  missing_releases_scan: "Missing Releases Scan",
  artist: "Artist Scan",
  artist_scan: "Artist Scan",
  full: "Full Scan",
  full_scan: "Full Scan",
  force: "Forced Full Scan",
};

function parseScanTimestamp(ts) {
  if (!ts && ts !== 0) return null;
  if (ts instanceof Date) return isNaN(ts.getTime()) ? null : ts;
  if (typeof ts === "number") { const d = new Date(ts); return isNaN(d.getTime()) ? null : d; }
  let text = String(ts).trim();
  if (!text) return null;
  if (/^\d+$/.test(text)) {
    const n = Number(text);
    if (!Number.isNaN(n)) { const e = new Date(text.length <= 10 ? n * 1000 : n); if (!isNaN(e.getTime())) return e; }
  }
  // Try a native parse first (handles RFC 1123 / HTTP-date strings such as
  // "Sun, 02 Aug 2026 10:00:00 GMT" emitted by older JSON serializers).
  const native = new Date(text);
  if (!isNaN(native.getTime())) return native;
  let norm = text.replace(" ", "T").replace(/\.(\d{3})\d+/, ".$1").replace(/([+-]\d{2})(\d{2})$/, "$1:$2");
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?$/.test(norm)) norm += "Z";
  const d2 = new Date(norm);
  return isNaN(d2.getTime()) ? null : d2;
}

function formatScanTimestamp(ts) {
  const d = parseScanTimestamp(ts);
  if (!d) return "—";
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHr = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHr / 24);
  
  let relative;
  if (diffSec < 60) relative = `${diffSec}s ago`;
  else if (diffMin < 60) relative = `${diffMin}m ago`;
  else if (diffHr < 24) relative = `${diffHr}h ago`;
  else if (diffDay < 7) relative = `${diffDay}d ago`;
  else relative = d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  
  return `<span title="${escapeHtml(d.toLocaleString())}">${escapeHtml(relative)}</span>`;
}

function renderRecentScans(scans) {
  const body = document.getElementById("recent-scans-body");
  if (!body) return;
  if (!scans || scans.length === 0) {
    body.innerHTML = '<div class="p-3 text-center text-muted small">No recent scan history.</div>';
    return;
  }

  const grouped = {};
  const inProgress = [];
  
  scans.forEach(scan => {
    const ts = scan.scan_timestamp || scan.started_at || scan.timestamp || null;
    if (scan._inProgress) {
      inProgress.push({
        artist: scan.artist,
        album: scan.album || "…",
        scan_types: [{ type: scan.scan_type, timestamp: ts, _inProgress: true }],
        latest_timestamp: ts,
        latest_timestamp_obj: new Date(),
        _inProgress: true,
      });
      return;
    }
    const key = `${scan.artist}|${scan.album}`;
    if (!grouped[key]) {
      grouped[key] = {
        artist: scan.artist,
        album: scan.album,
        status: scan.status,
        scan_types: [],
        latest_timestamp: ts,
        latest_timestamp_obj: parseScanTimestamp(ts),
      };
    }
    
    if (scan.status === "failed" || scan.status === "error") grouped[key].status = "failed";
    else if (scan.status === "completed" || scan.status === "complete") {
      if (grouped[key].status !== "failed") grouped[key].status = "completed";
    } else if (scan.status === "started" && !grouped[key].status) grouped[key].status = "started";
    else if (scan.status === "stopped" && grouped[key].status !== "failed" && grouped[key].status !== "completed") grouped[key].status = "stopped";
    
    if (!grouped[key].scan_types.find(s => s.type === scan.scan_type)) {
      grouped[key].scan_types.push({ type: scan.scan_type, timestamp: ts });
    }
    
    const scanTime = parseScanTimestamp(ts);
    if (scanTime && (!grouped[key].latest_timestamp_obj || scanTime > grouped[key].latest_timestamp_obj)) {
      grouped[key].latest_timestamp = ts;
      grouped[key].latest_timestamp_obj = scanTime;
    }
  });

  const entries = inProgress.concat(
    Object.values(grouped).sort((a, b) => (b.latest_timestamp_obj?.getTime() || 0) - (a.latest_timestamp_obj?.getTime() || 0))
  );

  body.innerHTML = entries.map(group => {
    if (group.artist === "_SCAN_SESSION_") {
      const typeKey = group.album || group.scan_types?.[0]?.type || "";
      const typeName = SCAN_TYPE_DISPLAY_NAMES[typeKey] || escapeHtml(typeKey);
      const isRunning = group.status === "started";
      const statusIcon = isRunning ? "bi-hourglass-split text-primary" : group.status === "failed" ? "bi-x-circle-fill text-danger" : "bi-check-circle-fill text-success";
      const statusLabel = isRunning ? "running" : group.status === "failed" ? "failed" : "completed";
      const spinnerHtml = isRunning ? '<span class="spinner-border spinner-border-sm me-1" style="width:.75em;height:.75em;"></span>' : '';
      return `<div class="list-group-item bg-transparent p-2">
        <small class="${isRunning ? 'text-primary' : group.status === 'failed' ? 'text-danger' : 'text-success'}">${spinnerHtml}<i class="bi ${statusIcon} me-1"></i><strong>${typeName}</strong> ${statusLabel} — ${formatScanTimestamp(group.latest_timestamp)}</small>
      </div>`;
    }

    const artistUrl = `/artist/${encodeURIComponent(group.artist)}`;
    const albumUrl = group.album && group.album !== "…" ? `/album/${encodeURIComponent(group.artist)}/${encodeURIComponent(group.album)}` : artistUrl;
    
    const badges = group.scan_types.filter(st => st.type !== "covers").map(st => {
      if (st._inProgress) return '<span class="badge bg-primary"><span class="spinner-border spinner-border-sm me-1" style="width:.75em;height:.75em;"></span> Scanning…</span>';
      const typeMap = {
        navidrome: '<i class="bi bi-cloud"></i> Navidrome',
        navidrome_scan: '<i class="bi bi-cloud"></i> Navidrome',
        popularity: '<i class="bi bi-graph-up"></i> Popularity',
        popularity_scan: '<i class="bi bi-graph-up"></i> Popularity',
        metadata: '<i class="bi bi-info-circle"></i> Metadata',
        metadata_lookup_scan: '<i class="bi bi-info-circle"></i> Metadata',
        singles: '<i class="bi bi-star"></i> Singles',
        singles_scan: '<i class="bi bi-star"></i> Singles',
        singles_detection: '<i class="bi bi-star"></i> Singles',
        mood: '<i class="bi bi-emoji-smile"></i> Mood',
        mood_scan: '<i class="bi bi-emoji-smile"></i> Mood',
        "essentia-mood": '<i class="bi bi-cpu"></i> Essentia',
        essentia: '<i class="bi bi-cpu"></i> Essentia',
        essentia_mood_scan: '<i class="bi bi-cpu"></i> Essentia',
        combined: '<i class="bi bi-lightning-fill"></i> Combined',
        combined_scan: '<i class="bi bi-lightning-fill"></i> Combined',
        unified: '<i class="bi bi-layers"></i> Unified',
        artist: '<i class="bi bi-person"></i> Artist',
        artist_scan: '<i class="bi bi-person"></i> Artist',
        full: '<i class="bi bi-collection"></i> Full',
        full_scan: '<i class="bi bi-collection"></i> Full',
        force: '<i class="bi bi-collection"></i> Forced',
        all: '<i class="bi bi-collection"></i> Full',
        missing_releases_scan: '<i class="bi bi-calendar-plus"></i> Missing Releases'
      };
      const badgeClass = { navidrome: 'bg-primary', navidrome_scan: 'bg-primary', popularity: 'bg-success', popularity_scan: 'bg-success', metadata: 'bg-info text-dark', metadata_lookup_scan: 'bg-info text-dark', singles: 'bg-warning text-dark', singles_scan: 'bg-warning text-dark', singles_detection: 'bg-warning text-dark', mood: 'bg-secondary', mood_scan: 'bg-secondary', "essentia-mood": 'bg-purple', essentia: 'bg-purple', essentia_mood_scan: 'bg-purple', combined: 'bg-info text-dark', combined_scan: 'bg-info text-dark', unified: 'bg-secondary', artist: 'bg-primary', artist_scan: 'bg-primary', full: 'bg-secondary', full_scan: 'bg-secondary', force: 'bg-secondary', all: 'bg-secondary', missing_releases_scan: 'bg-info text-dark' }[st.type] || 'bg-secondary';
      return `<span class="badge ${badgeClass}" title="${escapeHtml(st.timestamp || '')}">${typeMap[st.type] || escapeHtml(st.type)}</span>`;
    }).join(" ");

    const albumHtml = group.album && group.album !== "…"
      ? `<span class="text-muted mx-1">•</span><a href='${albumUrl}' class="text-light text-decoration-none">${escapeHtml(group.album)}</a>`
      : '';

    return `<div class="list-group-item bg-transparent p-2 d-flex flex-column gap-1">
      <div class="d-flex align-items-center justify-content-between gap-2">
        <div class="text-truncate small min-w-0 overflow-hidden">
          <a href='${artistUrl}' class="text-success text-decoration-none fw-semibold">${escapeHtml(group.artist)}</a>${albumHtml}
        </div>
        <span class="text-muted small flex-shrink-0 text-end" style="min-width: 3.5rem; white-space: nowrap;">${group._inProgress ? "now" : formatScanTimestamp(group.latest_timestamp)}</span>
      </div>
      <div class="d-flex align-items-center gap-1 flex-wrap" style="min-width: 0;">${badges}</div>
    </div>`;
  }).join("");
}

let _lastRecentScansPayload = null;
let _currentScanningAlbum = null;

function updateRecentScans() {
  fetch(`/api/recent-scans?_ts=${Date.now()}`, { cache: "no-store" })
    .then(r => r.json())
    .then(data => {
      const payload = data.scans || [];
      _lastRecentScansPayload = payload;
      renderRecentScans(_currentScanningAlbum ? [_currentScanningAlbum].concat(payload) : payload);
    })
    .catch(error => console.error("Error fetching recent scans:", error));
}

// ===== Upcoming Releases =====
var dashboardTableFilter = "all";

async function addUpcomingReleaseToQueueDashboard(encodedArtist, encodedAlbum, encodedDate, buttonEl, releaseGroupMbid) {  try {
    const artist = decodeURIComponent(encodedArtist || "").trim();
    const album = decodeURIComponent(encodedAlbum || "").trim();
    const dateText = decodeURIComponent(encodedDate || "").trim();
    if (!artist || !album) return;
    
    if (buttonEl) { buttonEl.disabled = true; buttonEl.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>'; }

    // Matched to a MusicBrainz release-group → let the user pick the exact
    // physical version (CD / deluxe / promo) via the release-picker flyout,
    // then queue the FULL tracklist through the same pipeline as the
    // MusicBrainz modal (per-track queue rows).  Falls back to a direct
    // queue of the release group when the picker is unavailable.
    if (releaseGroupMbid) {
      if (typeof window.openReleasePicker === "function") {
        window.openReleasePicker(decodeURIComponent(releaseGroupMbid), album, artist, function () {
          if (buttonEl) {
            buttonEl.disabled = true;
            buttonEl.classList.replace("btn-outline-primary", "btn-outline-success");
            buttonEl.innerHTML = '<i class="bi bi-check2"></i>';
            buttonEl.title = "Queued — exact version chosen";
          }
          setTimeout(loadUpcomingReleasesTable, 800);
        });
        // The flyout is now in charge — restore the button in case the user
        // closes it without queueing (the onQueued callback re-disables it).
        if (buttonEl) { buttonEl.disabled = false; buttonEl.innerHTML = '<i class="bi bi-plus-circle"></i>'; }
        return;
      }
      const result = await postJSON("/api/musicbrainz/download", {
        release_id: decodeURIComponent(releaseGroupMbid),
        release_title: album,
        artist,
        method: "slskd",
        queue_items_only: true,
      });
      if (result.error) throw new Error(result.error);
      const total = result.total_tracks || result.queued_tracks || 0;
      if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.classList.replace("btn-outline-primary", "btn-outline-success");
        buttonEl.innerHTML = '<i class="bi bi-check2"></i>';
        buttonEl.title = total ? `${total} track${total === 1 ? "" : "s"} queued` : (result.message || "Added to queue");
      }
      return;
    }

    // No MusicBrainz match yet → plain single-item queue add (worker searches
    // the album by artist + title).
    const result = await postJSON("/api/queue/add", {
      artist, title: album, album,
      import_type: "album", source: "soulseek", release_source: "dashboard_upcoming",
      priority: 5, is_album: true, year: /^\d{4}/.test(dateText) ? dateText.slice(0, 4) : null,
    });
    
    if (buttonEl) {
      buttonEl.disabled = true;
      buttonEl.classList.replace("btn-outline-primary", "btn-outline-success");
      buttonEl.innerHTML = '<i class="bi bi-check2"></i>';
      buttonEl.title = (result && result.message) || "Added to queue";
    }
  } catch (error) {
    if (buttonEl) { buttonEl.disabled = false; buttonEl.innerHTML = '<i class="bi bi-plus-circle"></i>'; }
  }
}

function renderUpcomingReleasesTable(releases) {
  const body = document.getElementById("upcoming-releases-body");
  if (!body) return;

  const countEl = document.getElementById("upcomingTableCount");
  if (countEl) countEl.textContent = `${releases.length} release(s)`;

  if (!releases || releases.length === 0) {
    // Compact empty state: hide the fixed-height table box entirely so the
    // card collapses around a one-line confirmation instead of a dark void.
    const wrap = document.getElementById("upcomingTableWrap");
    if (wrap) wrap.classList.add("d-none");
    const empty = document.getElementById("upcomingEmptyState");
    if (empty) {
      empty.classList.remove("d-none");
      empty.innerHTML =
        '<i class="bi bi-check-circle-fill text-success fs-4 d-block mb-2"></i>' +
        "No unowned upcoming releases scheduled. All tracked albums exist in your library!" +
        '<div class="mt-3"><button type="button" class="btn btn-sm btn-outline-secondary" onclick="loadUpcomingReleasesTable()">' +
        '<i class="bi bi-arrow-clockwise"></i> Refresh</button></div>';
    }
    body.innerHTML = "";
    const cardsEl = document.getElementById("upcoming-releases-cards");
    if (cardsEl) cardsEl.innerHTML = "";
    return;
  }

  const wrap = document.getElementById("upcomingTableWrap");
  // NOTE: never remove "d-none" from the wrap — the template pairs it with
  // "d-lg-block" (table visible only at >=lg; the stacked cards are shown
  // below lg via "d-lg-none"). Stripping it made BOTH render on small
  // viewports (legacy table + modern cards = double render).
  const empty = document.getElementById("upcomingEmptyState");
  if (empty) {
    empty.classList.add("d-none");
    empty.innerHTML =
      '<i class="bi bi-check-circle-fill text-success fs-4 d-block mb-2"></i>' +
      "No unowned upcoming releases scheduled. All tracked albums exist in your library!" +
      '<div class="mt-3"><button type="button" class="btn btn-sm btn-outline-secondary" onclick="loadUpcomingReleasesTable()">' +
      '<i class="bi bi-arrow-clockwise"></i> Refresh</button></div>';
  }

  // Build each row defensively: a single malformed record (bad date, missing
  // MBID payload, hostile string) must never abort the whole table render —
  // that previously left the header visible with a dead empty body and no
  // empty state.  Skipped records are logged for diagnosis.
  const rows = [];
  releases.forEach(r => {
    try {
      rows.push(_buildUpcomingReleaseRow(r));
    } catch (err) {
      console.error("Skipping malformed upcoming release:", r && (r.artist_name || r.id), err);
    }
  });

  if (rows.length === 0) {
    // Everything failed to build (or data vanished) — surface a real empty
    // state with a refresh action instead of a silent void.
    if (wrap) wrap.classList.add("d-none");
    if (empty) empty.classList.remove("d-none");
    body.innerHTML = "";
    const cardsEl = document.getElementById("upcoming-releases-cards");
    if (cardsEl) cardsEl.innerHTML = "";
    return;
  }

  body.innerHTML = rows.map(x => x.html).join("");
  const cardsEl = document.getElementById("upcoming-releases-cards");
  if (cardsEl) cardsEl.innerHTML = rows.map(x => x.card).join("");
}

function _buildUpcomingReleaseRow(r) {
    const releaseDate = r.release_date || "TBA";
    // The Wikipedia scraper stores the configured source display names (e.g.
    // "2026 Albums", "Heavy Metal 2026") — they never contain the literal
    // "wiki".  Only MusicBrainz-sourced rows should show the MB chip, so
    // treat anything that isn't explicitly MusicBrainz as Wikipedia.
    // Determine source: explicitly check for the known MusicBrainz source name
    // (stored as "MusicBrainz Daily Collection" in the database). Any other source
    // is treated as Wikipedia (e.g., "2026 Albums", "Heavy Metal 2026").
    const sourceStr = String(r.source || "").trim();
    const sourceKey = String(r.source_key || "").trim();
    const isMusicBrainz = sourceStr.toLowerCase().includes("musicbrainz daily collection");
    const sourceBadge = sourceKey
      ? `<span class="source-key-badge" title="Scraper rule: ${escapeHtml(sourceKey)}"><i class="bi bi-wikipedia"></i> ${escapeHtml(sourceKey)}</span>`
      : (isMusicBrainz
          ? '<span class="upcoming-source-chip upcoming-source-musicbrainz"><i class="bi bi-hexagon-fill"></i> MusicBrainz</span>'
          : '<span class="upcoming-source-chip upcoming-source-wikipedia"><i class="bi bi-wikipedia"></i> Wikipedia</span>');

    const dateBadge = new Date(releaseDate) > new Date() ? '<span class="badge bg-success">Upcoming</span>' : '<span class="badge bg-primary">Recent</span>';
    const colBadge = r.artist_in_collection ? '<span class="badge bg-success ms-1" title="Artist in collection"><i class="bi bi-check"></i></span>' : "";
    
    const eArtist = encodeURIComponent(r.artist_name || "");
    const eAlbum = encodeURIComponent(r.album_name || "");
    const eMbid = r.release_group_mbid ? encodeURIComponent(r.release_group_mbid) : "";
    const sArtist = JSON.stringify(String(r.artist_name || ""));
    const sAlbum = JSON.stringify(String(r.album_name || ""));

    // NEW UNIFIED SEARCH TRIGGER
    const searchBtn = `<button class="btn btn-sm btn-outline-info" title="Search on MusicBrainz" onclick='window.openGlobalMbSearch(${sArtist}, ${sAlbum}, function(selected){ if(typeof downloadMbRelease === "function") downloadMbRelease(selected.id, selected.title, selected.artist, "slskd"); })'><i class="bi bi-search"></i></button>`;
    
    const inQueue = r.in_queue === true || r.queue_status === "queued";
    const queueBtn = inQueue
      ? '<button class="btn btn-sm btn-outline-success" disabled title="Already in queue"><i class="bi bi-check2-circle"></i></button>'
      : `<button class="btn btn-sm btn-outline-primary" title="Add to queue" onclick="addUpcomingReleaseToQueueDashboard('${eArtist}', '${eAlbum}', '${encodeURIComponent(r.release_date || "")}', this, '${eMbid}')"><i class="bi bi-plus-circle"></i></button>`;

    // MusicBrainz match badges: 🟩 linked (has release_group_mbid) / 🟨
    // candidate (scoring pipeline found a >=0.65 match awaiting one click).
    const linkedBadge = r.release_group_mbid
      ? `<span class="badge bg-info-subtle text-info-emphasis ms-1" title="Linked to MusicBrainz${r.mbid_match_score ? ' (score ' + escapeHtml(String(r.mbid_match_score)) + ')' : ''}"><i class="bi bi-hexagon-fill"></i></span>`
      : "";
    const isCandidate = !r.release_group_mbid && r.mbid_match_status === "candidate" && !!r.candidate_release_group_mbid;
    const matchBtn = isCandidate
      ? `<button class="btn btn-sm btn-outline-warning" title="Confirm MusicBrainz match (score ${escapeHtml(String(r.mbid_match_score || ""))})" onclick="confirmUpcomingCandidate(${r.id || 0}, '${encodeURIComponent(r.candidate_release_group_mbid)}', this)"><i class="bi bi-link-45deg"></i></button>`
      : "";

    // One primary action per release — mutually exclusive to avoid clutter:
    //  ➕ Queue     when the release is already linked to a MusicBrainz ID
    //  🔗 Confirm   when the pipeline found a >=0.65 candidate awaiting one click
    //  🔍 Search    when the release has no MusicBrainz identity yet
    const actionBtn = r.release_group_mbid
      ? queueBtn
      : (isCandidate ? matchBtn : searchBtn);

    return {
      html: `<tr>
        <td>${escapeHtml(r.artist_name)}${colBadge}</td>
        <td>${escapeHtml(r.album_name)}${linkedBadge}</td>
        <td>${escapeHtml(releaseDate)} ${dateBadge}</td>
        <td>${sourceBadge}</td>
        <td class="text-center"><div class="d-flex gap-1 justify-content-center">${actionBtn}</div></td>
      </tr>`,
      // Stacked mobile card (shown below lg — the table is hidden on small
      // screens). Compact: single-line meta row (date truncates, badges
      // stay) and exactly one action button.
      card: `<div class="upcoming-card rounded border">
        <div class="d-flex justify-content-between align-items-center gap-2">
          <div class="min-w-0">
            <div class="upcoming-card-title fw-semibold text-truncate">${escapeHtml(r.artist_name)}${colBadge}</div>
            <div class="upcoming-card-album text-muted text-truncate">${escapeHtml(r.album_name)}${linkedBadge}</div>
          </div>
          <div class="d-flex flex-shrink-0">${actionBtn}</div>
        </div>
        <div class="upcoming-card-meta d-flex align-items-center mt-1 text-muted">
          <i class="bi bi-calendar"></i><span class="text-truncate">${escapeHtml(releaseDate)}</span>${dateBadge}${sourceBadge}
        </div>
      </div>`
    };
}

function _renderUpcomingTableFilterButtons() {
  ["all", "collection"].forEach(key => {
    const btn = document.getElementById(`upcomingTableFilter${key.charAt(0).toUpperCase() + key.slice(1)}`);
    if (!btn) return;
    const active = key === dashboardTableFilter;
    btn.classList.toggle("btn-info", active);
    btn.classList.toggle("btn-outline-info", !active);
  });
}

function setUpcomingTableFilter(filter) {
  dashboardTableFilter = ["all", "collection"].includes(filter) ? filter : "all";
  _renderUpcomingTableFilterButtons();
  sessionStorage.setItem("dashboardTableFilter", dashboardTableFilter);
  loadUpcomingReleasesTable();
}

// ===== Upcoming Releases: server-side ±7-day snapshot =====

/** One-click confirm of a pipeline candidate match (yellow 🔗 button). */
async function confirmUpcomingCandidate(releaseId, mbidEnc, buttonEl) {
  const mbid = decodeURIComponent(mbidEnc || "");
  if (!releaseId || !mbid) return;
  if (buttonEl) { buttonEl.disabled = true; buttonEl.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>'; }
  try {
    const result = await postJSON(`/api/upcoming-releases/${releaseId}/match`, {
      release_group_mbid: mbid,
      source: "candidate_confirm",
    });
    if (result.error) throw new Error(result.error);
    buttonEl.classList.replace("btn-outline-warning", "btn-outline-success");
    buttonEl.innerHTML = '<i class="bi bi-check2"></i>';
    buttonEl.title = "Linked to MusicBrainz";
    setTimeout(loadUpcomingReleasesTable, 800);
  } catch (error) {
    if (buttonEl) { buttonEl.disabled = false; buttonEl.innerHTML = '<i class="bi bi-link-45deg"></i>'; }
  }
}

async function loadUpcomingReleasesTable() {
  try {
    const data = await UpcomingReleasesService.fetchReleases({
      filter: dashboardTableFilter === "collection" ? "collection" : undefined,
      include_queue: true,
      window: 7,
    });
    let releases = data.releases || [];

    if (dashboardTableFilter === "collection") releases = releases.filter(r => r.artist_in_collection);

    releases.sort((a, b) => (a.release_date || "9999-12-31").localeCompare(b.release_date || "9999-12-31"));
    renderUpcomingReleasesTable(releases);

    // Footer link to the full manager, labelled with the full-window count.
    const link = document.getElementById("upcomingViewAllLink");
    if (link && data.total) {
      link.textContent = `View All ${data.total} Releases & Management Tools `;
    }
  } catch (error) {
    console.error("Error loading upcoming releases table:", error);
    // Surface the failure instead of leaving a blank fixed-height table box.
    const body = document.getElementById("upcoming-releases-body");
    const empty = document.getElementById("upcomingEmptyState");
    const wrap = document.getElementById("upcomingTableWrap");
    if (body) body.innerHTML = "";
    if (wrap) wrap.classList.add("d-none");
    if (empty) {
      empty.classList.remove("d-none");
      empty.innerHTML =
        '<i class="bi bi-exclamation-triangle-fill text-warning fs-4 d-block mb-2"></i>' +
        "Couldn't load upcoming releases. " +
        '<button type="button" class="btn btn-sm btn-outline-secondary ms-1" onclick="loadUpcomingReleasesTable()">' +
        '<i class="bi bi-arrow-clockwise"></i> Retry</button>';
    }
    const cardsEl = document.getElementById("upcoming-releases-cards");
    if (cardsEl) cardsEl.innerHTML = "";
  }
}

// ===== Active Scans Progress Panel =====
/* One-line summary for the sticky bottom status bar (global bar lives in
   base.html; the global log modal is driven by main.js). */
function updateScanStatusBar(active) {
  const line = document.getElementById("scanStatusLine");
  const icon = document.getElementById("scanStatusIcon");
  if (!line || !icon) return;
  if (!active || active.length === 0) {
    line.textContent = "Idle — no scan running";
    icon.className = "scan-status-idle";
    icon.innerHTML = '<i class="bi bi-circle"></i>';
    return;
  }
  const scan = active[0];
  const name = SCAN_TYPE_DISPLAY_NAMES[scan.scan_type] || scan.scan_type;
  const pct = Math.min(scan.percent_complete ?? scan.progress ?? 0, 100);
  const stage = scan.current_stage ? ` · ${scan.current_stage}` : "";
  line.textContent = `${name} — ${pct}%` + stage + (scan.current_item ? ` · ${scan.current_item}` : "");
  icon.className = "scan-status-active";
  icon.innerHTML = '<i class="bi bi-activity"></i>';
}

function updateActiveScans() {
  fetch(`/api/scan-progress?_ts=${Date.now()}`, { cache: "no-store" })
    .then(r => r.json())
    .then(data => {
      const panel = document.getElementById("activeScansPanel");
      const body = document.getElementById("activeScansBody");
      const badge = document.getElementById("scannerStatusBadge");
      const active = data.active_scans || [];
      updateScanStatusBar(active);
      if (badge) {
        badge.innerHTML = '<i class="bi bi-circle-fill me-1 small"></i> ' + (active.length ? "Active" : "Idle");
        badge.className = "badge " + (active.length ? "bg-success" : "bg-secondary");
        badge.style.fontSize = "0.65rem";
      }
      if (!panel || !body) return;
      if (active.length === 0) {
        panel.style.display = "none";
        return;
      }

      panel.style.display = "";
      body.innerHTML = active.map(scan => {
        const pct = Math.min(scan.percent_complete ?? scan.progress ?? 0, 100);
        // ── Abandoned / failed artists banner ────────────────────────────
        // The full-scan orchestrator records per-artist failures and budget
        // abandons on the progress row (scan.abandoned_artists).  Surface
        // them so the operator can investigate instead of the scan silently
        // moving past a hung artist.
        let bannerHtml = "";
        const abandoned = scan.abandoned_artists || {};
        const abandonedEntries = Object.entries(abandoned);
        if (abandonedEntries.length > 0) {
          const rows = abandonedEntries.map(([artistName, records]) => {
            const recs = Array.isArray(records) ? records : [];
            const reasons = recs.map(r => {
              const reason = r && r.reason ? String(r.reason) : "unknown";
              const tag = r && r.abandoned ? '<span class="badge bg-warning text-dark ms-1">abandoned</span>' : '<span class="badge bg-danger ms-1">failed</span>';
              return `<div class="ms-2 small">${tag} <code>${escapeHtml(reason)}</code></div>`;
            }).join("");
            return `<div class="mb-1">
              <i class="bi bi-exclamation-triangle-fill text-warning me-1"></i><strong>${escapeHtml(artistName)}</strong>
              ${reasons}
            </div>`;
          }).join("");
          bannerHtml = `<div class="alert alert-warning py-2 px-3 mb-2" style="font-size:.85rem;">
            <div class="fw-semibold mb-1"><i class="bi bi-hourglass-split me-1"></i>Artists skipped / abandoned — investigate</div>
            ${rows}
            <div class="text-muted small mt-1">These artists were abandoned after the per-artist time budget or raised an error; their tracks were not fully scanned.</div>
          </div>`;
        }
        return `
          <div class="mb-2">
            ${bannerHtml}
            <div class="d-flex justify-content-between align-items-center mb-1">
              <span><i class="bi bi-activity me-1"></i><strong>${escapeHtml(SCAN_TYPE_DISPLAY_NAMES[scan.scan_type] || scan.scan_type)}</strong>
              ${scan.current_stage ? `<span class="badge bg-info ms-2" style="font-size:0.7rem;">${escapeHtml(scan.current_stage)}</span>` : ''}
              ${scan.message ? `<span class="text-muted small ms-2">${escapeHtml(scan.message)}</span>` : ''}</span>
              <span class="small text-muted">${scan.processed_items ?? 0}/${scan.total_items ?? "?"}</span>
            </div>
            ${scan.current_item ? `<div class="small text-muted mb-1 text-truncate" style="max-width:600px;">${escapeHtml(scan.current_item)}</div>` : ''}
            <div class="progress" style="height:8px;">
              <div class="progress-bar progress-bar-striped progress-bar-animated bg-success" style="width:${pct}%;"></div>
            </div>
          </div>`;
      }).join("");
    }).catch(() => {});
}

// ===== Initialization =====
function updateAll() {
  pollPopularityStatus();
  pollNavidromeStatus();
  pollEssentiaStatus();
  updateActiveScans();
  updateRecentScans();
}

document.addEventListener("DOMContentLoaded", function () {
  renderRecentScans((window._pd && window._pd.recentScans) || []);

  try {
    const storedTableFilter = sessionStorage.getItem("dashboardTableFilter");
    if (storedTableFilter) dashboardTableFilter = storedTableFilter;
  } catch (e) {}
  
  _renderUpcomingTableFilterButtons();
  setTimeout(loadUpcomingReleasesTable, 400);
  setInterval(loadUpcomingReleasesTable, 30 * 60 * 1000);

  updateAll();
  setInterval(updateAll, 5000);

  // Lift the sticky scan status bar above the global player bar when it appears.
  setInterval(function () {
    const playerBar = document.getElementById("globalPlayerBar");
    if (playerBar) {
      document.body.classList.toggle("player-visible", !playerBar.classList.contains("d-none"));
    }
  }, 2000);
});