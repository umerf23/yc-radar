const state = { dashboard: null, slack: null, slackChannels: null };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const sourceLabels = {
  yc_directory: "YC Directory",
  yc_speedrun: "a16z Speedrun",
  x_twitter: "X / Twitter",
  linkedin: "LinkedIn",
};

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function relativeTime(value) {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const ranges = [[31536000, "year"], [2592000, "month"], [86400, "day"], [3600, "hour"], [60, "minute"]];
  for (const [size, unit] of ranges) {
    if (Math.abs(seconds) >= size) {
      return new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(Math.round(seconds / size), unit);
    }
  }
  return "just now";
}

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 3500);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = "YC Radar could not complete this request.";
    try {
      const payload = await response.json();
      message = payload.error?.message || payload.detail?.message || message;
    } catch {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function renderSources(sources) {
  const expected = ["yc_directory", "yc_speedrun", "x_twitter", "linkedin"];
  const healthy = expected.filter((name) => sources[name]?.status === "ok").length;
  $("#sourceScore").textContent = `${healthy}/${expected.length}`;
  $("#sourceScore").style.color = healthy === expected.length ? "var(--green)" : "var(--yellow)";
  $("#sourceList").innerHTML = expected.map((name) => {
    const source = sources[name] || {};
    const ok = source.status === "ok";
    return `<div class="source-row" title="${escapeHtml(source.error || "Healthy")}"><span class="status-dot ${ok ? "ok" : source.status ? "bad" : ""}"></span><strong>${sourceLabels[name]}</strong><span>${source.status ? `${source.items_found ?? 0} found` : "Awaiting run"}</span></div>`;
  }).join("");
}

function renderDashboard(data) {
  const totals = data.totals || {};
  const lastRun = data.last_run || {};
  $("#earlyTotal").textContent = Number(totals.early_signals || 0).toLocaleString();
  $("#alertedTotal").textContent = Number(totals.alerted || 0).toLocaleString();
  $("#candidateTotal").textContent = Number(totals.total_candidates || 0).toLocaleString();
  $("#officialTotal").textContent = Number(totals.official_known || 0).toLocaleString();
  $("#runExamined").textContent = Number(lastRun.examined || 0).toLocaleString();
  $("#runNew").textContent = Number(lastRun.new || 0).toLocaleString();
  $("#runClassified").textContent = Number(lastRun.classified || 0).toLocaleString();
  $("#runAlerted").textContent = Number(lastRun.alerted || 0).toLocaleString();
  $("#lastSync").textContent = relativeTime(lastRun.finished_at);
  const online = data.status === "ok";
  $("#sidebarStatus").textContent = online ? "Agent online" : "Agent starting";
  $("#sidebarStatusDot").className = `status-dot ${online ? "ok" : ""}`;
  renderSources(data.sources || {});
  $("#manifestUrl").textContent = `${location.origin}/manifest`;
  $("#cycleSummary").innerHTML = `<span>Latest cycle</span><p>${lastRun.examined ?? 0} examined · ${lastRun.new ?? 0} new · ${lastRun.alerted ?? 0} alerts delivered</p>`;
}

function renderSlack(status) {
  const addButton = $("#addSlackButton");
  const workspaceCard = $("#workspaceCard");
  const topAction = $("#slackTopAction");
  if (!status.available) {
    addButton.hidden = true;
    workspaceCard.hidden = true;
    $("#slackHeading").textContent = "Slack installation setup required";
    $("#slackDescription").textContent = "The app owner needs to add the Slack OAuth settings on Railway before other workspaces can connect.";
    topAction.textContent = "Slack setup required";
    topAction.href = "#slack";
    topAction.dataset.viewTarget = "slack";
    return;
  }
  if (!status.connected) {
    addButton.hidden = false;
    workspaceCard.hidden = true;
    topAction.innerHTML = "Add to Slack <span>→</span>";
    topAction.href = "/slack/install";
    return;
  }
  addButton.hidden = true;
  workspaceCard.hidden = false;
  $("#slackHeading").textContent = "Workspace connected";
  $("#slackDescription").textContent = status.channel_configured
    ? "Choose where leads should be delivered, then run a fresh scan whenever you want."
    : "Authentication succeeded. Choose the channel where YC Radar should deliver leads.";
  $("#workspaceName").textContent = status.workspace;
  $("#workspaceChannel").textContent = status.channel_configured ? `#${status.channel}` : "Channel not selected";
  $("#runWorkspaceButton").disabled = !status.channel_configured;
  topAction.innerHTML = `${escapeHtml(status.workspace)} <span>→</span>`;
  topAction.href = "#slack";
  topAction.dataset.viewTarget = "slack";
  if (!state.slackChannels) loadSlackChannels(status);
}

async function loadSlackChannels(status) {
  const select = $("#slackChannelSelect");
  const saveButton = $("#saveSlackChannelButton");
  select.disabled = true;
  saveButton.disabled = true;
  try {
    const result = await api("/api/slack/channels");
    state.slackChannels = result.channels || [];
    if (!state.slackChannels.length) {
      select.innerHTML = '<option value="">No accessible channels found</option>';
      return;
    }
    select.innerHTML = [
      '<option value="">Choose a channel…</option>',
      ...state.slackChannels.map((channel) =>
        `<option value="${escapeHtml(channel.id)}">#${escapeHtml(channel.name)}</option>`
      ),
    ].join("");
    select.value = status.channel_id || "";
    select.disabled = false;
    saveButton.disabled = false;
  } catch (error) {
    select.innerHTML = '<option value="">Could not load channels</option>';
    showToast(error.message, true);
  }
}

async function loadAll(quiet = false) {
  $("#refreshButton").classList.add("spinning");
  try {
    const [dashboard, slack] = await Promise.all([api("/api/dashboard"), api("/api/slack/status")]);
    state.dashboard = dashboard;
    state.slack = slack;
    renderDashboard(dashboard);
    renderSlack(slack);
    if (!quiet) showToast("Dashboard is up to date.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    $("#refreshButton").classList.remove("spinning");
  }
}

function selectView(name) {
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `${name}View`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  const labels = {
    overview: ["LIVE OPERATIONS", "Signal overview"],
    slack: ["PRIVATE DELIVERY", "Slack workspace"],
    connection: ["AGENT SETUP", "Pond connection"],
  };
  $("#pageEyebrow").textContent = labels[name][0];
  $("#pageTitle").textContent = labels[name][1];
  $(".sidebar").classList.remove("open");
  $("#mobileMenu").setAttribute("aria-expanded", "false");
  history.replaceState(null, "", `#${name}`);
}

$("#refreshButton").addEventListener("click", () => loadAll());
$("#mobileMenu").addEventListener("click", () => {
  const sidebar = $(".sidebar");
  const open = sidebar.classList.toggle("open");
  $("#mobileMenu").setAttribute("aria-expanded", String(open));
});
$$('[data-view]').forEach((button) => button.addEventListener("click", () => selectView(button.dataset.view)));
document.addEventListener("click", async (event) => {
  const viewTarget = event.target.closest("[data-view-target]");
  if (viewTarget?.dataset.viewTarget) {
    event.preventDefault();
    selectView(viewTarget.dataset.viewTarget);
  }
  if (event.target.closest('[data-copy="manifest"]')) {
    await navigator.clipboard.writeText(`${location.origin}/manifest`);
    showToast("Manifest URL copied.");
  }
});

$("#runWorkspaceButton").addEventListener("click", async () => {
  const button = $("#runWorkspaceButton");
  const feedback = $("#runFeedback");
  button.disabled = true;
  button.textContent = "Scanning four sources…";
  feedback.textContent = "This can take several minutes. Keep this page open.";
  try {
    const result = await api("/api/slack/run", { method: "POST", headers: { "X-YC-Radar-Action": "run" } });
    feedback.textContent = result.delivered
      ? `${result.delivered} new qualified lead${result.delivered === 1 ? " was" : "s were"} sent to Slack.`
      : "Scan complete. No new qualified leads; a summary was sent to Slack.";
    showToast("Slack delivery completed.");
    await loadAll(true);
  } catch (error) {
    feedback.textContent = error.message;
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Run scan and send leads";
  }
});

$("#saveSlackChannelButton").addEventListener("click", async () => {
  const select = $("#slackChannelSelect");
  const button = $("#saveSlackChannelButton");
  if (!select.value) {
    showToast("Choose a Slack channel first.", true);
    return;
  }
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const result = await api("/api/slack/channel", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-YC-Radar-Action": "select-channel",
      },
      body: JSON.stringify({ channel_id: select.value }),
    });
    showToast(`#${result.channel} will receive YC Radar alerts.`);
    await loadAll(true);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Save channel";
  }
});

const requestedView = location.hash.slice(1);
if (["overview", "slack", "connection"].includes(requestedView)) selectView(requestedView);
const slackResult = new URLSearchParams(location.search).get("slack");
if (slackResult === "choose_channel") showToast("Slack connected. Choose a destination channel.");
if (slackResult && slackResult !== "choose_channel") showToast("Slack connection was not completed. Please try again.", true);
loadAll(true);
