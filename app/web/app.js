const state = { accessKey: sessionStorage.getItem("ycRadarAccessKey") || "", data: null, candidates: [] };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const els = {
  shell: $("#appShell"), gate: $("#accessGate"), form: $("#accessForm"), key: $("#accessKey"), error: $("#accessError"),
  refresh: $("#refreshButton"), run: $("#runButton"), toast: $("#toast"), rows: $("#candidateRows"), empty: $("#emptyState"),
  search: $("#searchInput"), status: $("#statusFilter"), source: $("#sourceFilter"), priority: $("#prioritySignals"),
};

const sourceLabels = { yc_directory: "YC Directory", yc_speedrun: "a16z Speedrun", x_twitter: "X / Twitter", linkedin: "LinkedIn" };
const statusLabels = { EARLY_SIGNAL: "Early signal", CONFIRMED_YC: "Confirmed YC", CONFIRMED_SPEEDRUN: "Speedrun", LINKEDIN_COMPANY_SIGNAL: "LinkedIn signal" };

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function safeUrl(value) {
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : ""; } catch { return ""; }
}

function badgeClass(status) {
  if (status === "EARLY_SIGNAL") return "early";
  if (status === "LINKEDIN_COMPANY_SIGNAL") return "linkedin";
  return "confirmed";
}

function relativeTime(value) {
  if (!value) return "Never";
  const date = new Date(value); if (Number.isNaN(date.getTime())) return "Unknown";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const ranges = [[31536000,"year"],[2592000,"month"],[86400,"day"],[3600,"hour"],[60,"minute"]];
  for (const [size, unit] of ranges) if (Math.abs(seconds) >= size) return new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(Math.round(seconds / size), unit);
  return "just now";
}

function showToast(message, isError = false) {
  els.toast.textContent = message; els.toast.classList.toggle("error", isError); els.toast.classList.add("show");
  clearTimeout(showToast.timer); showToast.timer = setTimeout(() => els.toast.classList.remove("show"), 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { Authorization: `Bearer ${state.accessKey}`, "X-Agent-Protocol-Version": "1.0", ...(options.headers || {}) } });
  if (!response.ok) {
    let message = "The dashboard could not reach YC Radar.";
    try { const payload = await response.json(); message = payload.error?.message || payload.detail?.message || message; } catch {}
    const error = new Error(message); error.status = response.status; throw error;
  }
  return response.json();
}

async function loadDashboard({ quiet = false } = {}) {
  els.refresh.classList.add("spinning");
  try {
    const data = await api("/api/dashboard?limit=250");
    state.data = data; state.candidates = data.candidates || [];
    sessionStorage.setItem("ycRadarAccessKey", state.accessKey);
    els.gate.hidden = true; els.shell.setAttribute("aria-hidden", "false");
    renderDashboard(); if (!quiet) showToast("Dashboard is up to date.");
  } catch (error) {
    if (error.status === 401) { sessionStorage.removeItem("ycRadarAccessKey"); state.accessKey = ""; els.gate.hidden = false; els.shell.setAttribute("aria-hidden", "true"); els.error.textContent = "That access key is not valid."; }
    else { els.error.textContent = error.message; if (!quiet) showToast(error.message, true); }
    throw error;
  } finally { els.refresh.classList.remove("spinning"); }
}

function renderDashboard() {
  const { totals = {}, sources = {}, last_run: lastRun = {} } = state.data;
  $("#earlyTotal").textContent = Number(totals.early_signals || 0).toLocaleString();
  $("#alertedTotal").textContent = Number(totals.alerted || 0).toLocaleString();
  $("#candidateTotal").textContent = Number(totals.total_candidates || 0).toLocaleString();
  $("#officialTotal").textContent = Number(totals.official_known || 0).toLocaleString();
  $("#navSignalCount").textContent = totals.early_signals || 0;
  const serviceOk = state.data.status === "ok";
  $("#sidebarStatus").textContent = serviceOk ? "Agent online" : "Agent starting";
  $("#sidebarStatusDot").className = `status-dot ${serviceOk ? "ok" : ""}`;
  $("#lastSync").textContent = relativeTime(lastRun.finished_at);
  renderSources(sources); renderPriority(); renderCandidates();
  $("#manifestUrl").textContent = `${location.origin}/manifest`;
  const examined = lastRun.examined ?? 0, found = lastRun.new ?? 0, alerted = lastRun.alerted ?? 0;
  $("#cycleSummary").innerHTML = `<span>Latest cycle</span><p>${examined} examined · ${found} new · ${alerted} alerts delivered</p>`;
}

function renderSources(sources) {
  const expected = ["yc_directory", "yc_speedrun", "x_twitter", "linkedin"];
  const healthy = expected.filter((name) => sources[name]?.status === "ok").length;
  $("#sourceScore").textContent = `${healthy}/${expected.length}`;
  $("#sourceScore").style.color = healthy === expected.length ? "var(--green)" : "var(--yellow)";
  $("#sourceList").innerHTML = expected.map((name) => {
    const source = sources[name] || {}, ok = source.status === "ok";
    const count = source.items_found ?? 0;
    return `<div class="source-row" title="${escapeHtml(source.error || "Healthy")}"><span class="status-dot ${ok ? "ok" : source.status ? "bad" : ""}"></span><strong>${sourceLabels[name]}</strong><span>${source.status ? `${count} found` : "Awaiting run"}</span></div>`;
  }).join("");
}

function renderPriority() {
  const items = [...state.candidates].sort((a,b) => (a.status === "EARLY_SIGNAL" ? -1 : 1) - (b.status === "EARLY_SIGNAL" ? -1 : 1)).slice(0, 3);
  if (!items.length) { els.priority.innerHTML = `<div class="empty-state"><span class="empty-radar"></span><h3>No signals recorded yet</h3><p>Run a scan to start building the queue.</p></div>`; return; }
  els.priority.innerHTML = items.map((item) => `<article class="signal-item"><div><div class="signal-meta"><span class="badge ${badgeClass(item.status)}">${escapeHtml(statusLabels[item.status] || item.status)}</span><span class="badge">${escapeHtml(item.batch || sourceLabels[item.source] || "Unknown")}</span></div><h3>${escapeHtml(item.company_name || "Company not stated")}</h3><p>${escapeHtml(item.founder_handle ? `Founder ${item.founder_handle}` : sourceLabels[item.source] || item.source)}</p></div><time>${relativeTime(item.first_seen_at)}</time></article>`).join("");
}

function filteredCandidates() {
  const query = els.search.value.trim().toLowerCase();
  return state.candidates.filter((item) => {
    const haystack = [item.company_name, item.founder_handle, item.batch, item.source].join(" ").toLowerCase();
    return (!query || haystack.includes(query)) && (els.status.value === "all" || item.status === els.status.value) && (els.source.value === "all" || item.source === els.source.value);
  });
}

function renderCandidates() {
  const items = filteredCandidates(); $("#resultCount").textContent = `${items.length} candidate${items.length === 1 ? "" : "s"}`; els.empty.hidden = items.length > 0;
  els.rows.innerHTML = items.map((item) => {
    const url = safeUrl(item.url), confidence = Math.round(Number(item.confidence || 0) * 100), company = item.company_name || "Company not stated";
    return `<tr><td><div class="company-cell"><strong>${escapeHtml(company)}</strong><span>${escapeHtml(item.founder_handle || sourceLabels[item.source] || item.source)}</span></div></td><td><span class="badge ${badgeClass(item.status)}">${escapeHtml(statusLabels[item.status] || item.status)}</span></td><td>${escapeHtml(item.batch || "Unknown")}</td><td><div class="confidence"><span>${confidence}%</span><span class="confidence-track"><span style="width:${Math.max(0, Math.min(confidence, 100))}%"></span></span></div></td><td>${relativeTime(item.first_seen_at)}</td><td><div class="row-actions"><button class="row-action copy" data-copy-brief="${escapeHtml(item.dedup_key)}">Copy brief</button>${url ? `<a class="row-action" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">Open ↗</a>` : ""}</div></td></tr>`;
  }).join("");
}

function selectView(name) {
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `${name}View`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  const labels = { overview: ["LIVE OPERATIONS", "Signal overview"], signals: ["OUTREACH PIPELINE", "Signal inbox"], connection: ["AGENT SETUP", "Pond connection"] };
  $("#pageEyebrow").textContent = labels[name][0]; $("#pageTitle").textContent = labels[name][1];
  $(".sidebar").classList.remove("open"); $("#mobileMenu").setAttribute("aria-expanded", "false");
  history.replaceState(null, "", `#${name}`);
}

els.form.addEventListener("submit", async (event) => {
  event.preventDefault(); els.error.textContent = ""; const button = els.form.querySelector("button[type=submit]"); button.disabled = true;
  state.accessKey = els.key.value.trim();
  try { await loadDashboard({ quiet: true }); } catch {} finally { button.disabled = false; }
});

$("#toggleKey").addEventListener("click", () => { const show = els.key.type === "password"; els.key.type = show ? "text" : "password"; $("#toggleKey").textContent = show ? "Hide" : "Show"; });
els.refresh.addEventListener("click", () => loadDashboard().catch(() => {}));
els.run.addEventListener("click", async () => {
  els.run.disabled = true; const original = els.run.innerHTML; els.run.textContent = "Scanning all sources…";
  try { const result = await api("/api/run", { method: "POST" }); showToast(`Scan complete: ${result.new || 0} new, ${result.alerted || 0} alerts.`); await loadDashboard({ quiet: true }); }
  catch (error) { showToast(error.message, true); } finally { els.run.disabled = false; els.run.innerHTML = original; }
});

$("#disconnectButton").addEventListener("click", () => { sessionStorage.removeItem("ycRadarAccessKey"); state.accessKey = ""; els.key.value = ""; els.gate.hidden = false; els.shell.setAttribute("aria-hidden", "true"); });
$("#mobileMenu").addEventListener("click", () => { const sidebar = $(".sidebar"), open = sidebar.classList.toggle("open"); $("#mobileMenu").setAttribute("aria-expanded", String(open)); });
$$('[data-view]').forEach((button) => button.addEventListener("click", () => selectView(button.dataset.view)));
$$('[data-view-target]').forEach((button) => button.addEventListener("click", () => selectView(button.dataset.viewTarget)));
[els.search, els.status, els.source].forEach((input) => input.addEventListener(input === els.search ? "input" : "change", renderCandidates));

document.addEventListener("click", async (event) => {
  const briefButton = event.target.closest("[data-copy-brief]");
  if (briefButton) {
    const item = state.candidates.find((candidate) => candidate.dedup_key === briefButton.dataset.copyBrief); if (!item) return;
    const brief = `${item.company_name || "Unnamed company"} — ${statusLabels[item.status] || item.status}${item.batch ? ` (${item.batch})` : ""}. Source: ${sourceLabels[item.source] || item.source}.${item.founder_handle ? ` Founder: ${item.founder_handle}.` : ""}${item.url ? ` ${item.url}` : ""}`;
    await navigator.clipboard.writeText(brief); showToast("Outreach brief copied.");
  }
  if (event.target.closest('[data-copy="manifest"]')) { await navigator.clipboard.writeText(`${location.origin}/manifest`); showToast("Manifest URL copied."); }
});

const initialView = location.hash.slice(1); if (["overview", "signals", "connection"].includes(initialView)) selectView(initialView);
if (state.accessKey) { state.accessKey = sessionStorage.getItem("ycRadarAccessKey"); loadDashboard({ quiet: true }).catch(() => {}); } else { els.gate.hidden = false; }
