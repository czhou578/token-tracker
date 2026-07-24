const ids = {
  apiEquivalent: document.querySelector("#apiEquivalent"),
  totalTokens: document.querySelector("#totalTokens"),
  requests: document.querySelector("#requests"),
  threads: document.querySelector("#threads"),
  dailyChart: document.querySelector("#dailyChart"),
  dailyRange: document.querySelector("#dailyRange"),
  modelsChart: document.querySelector("#modelsChart"),
  projectsChart: document.querySelector("#projectsChart"),
  recentRows: document.querySelector("#recentRows"),
  window: document.querySelector("#window"),
  refresh: document.querySelector("#refresh"),
};

const fmt = new Intl.NumberFormat();
const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" });

async function get(path) {
  const windowValue = ids.window.value;
  const suffix = windowValue ? `?window=${encodeURIComponent(windowValue)}` : "";
  const response = await fetch(`/api/${path}${suffix}`);
  if (!response.ok) throw new Error(`Failed to load ${path}`);
  return response.json();
}

async function load() {
  const [stats, daily, models, projects] = await Promise.all([
    get("stats"),
    get("daily"),
    get("models"),
    get("projects"),
  ]);
  renderStats(stats);
  renderDaily(daily);
  renderBars(ids.modelsChart, models, "model");
  renderBars(ids.projectsChart, projects, "project");
  renderRecent(stats.recent || []);
}

function renderStats(stats) {
  ids.apiEquivalent.textContent = fmt.format(stats.api_equivalent_tokens || 0);
  ids.totalTokens.textContent = fmt.format(stats.total_tokens || 0);
  ids.credits.textContent = money.format(stats.claude_credits || 0);
  ids.requests.textContent = fmt.format(stats.requests || 0);
  ids.threads.textContent = fmt.format(stats.threads || 0);
}

function renderDaily(rows) {
  ids.dailyChart.innerHTML = "";
  if (!rows.length) {
    ids.dailyChart.innerHTML = '<div class="empty">No usage events found yet</div>';
    ids.dailyRange.textContent = "";
    return;
  }
  const max = Math.max(...rows.map((row) => row.total_tokens || 0), 1);
  ids.dailyRange.textContent = `${rows.length} day${rows.length === 1 ? "" : "s"}`;
  rows.sort((a, b) => String(a.day).localeCompare(String(b.day))).forEach((row) => {
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.style.height = `${Math.max(3, ((row.total_tokens || 0) / max) * 100)}%`;
    bar.title = `${row.day}: ${fmt.format(row.total_tokens || 0)} tokens`;
    bar.innerHTML = `<span>${String(row.day).slice(5)}</span>`;
    ids.dailyChart.appendChild(bar);
  });
}

function renderBars(target, rows, labelKey) {
  target.innerHTML = "";
  if (!rows.length) {
    target.innerHTML = '<div class="empty">No data</div>';
    return;
  }
  const max = Math.max(...rows.map((row) => row.total_tokens || 0), 1);
  rows.slice(0, 8).forEach((row) => {
    const item = document.createElement("div");
    item.className = "row";
    const pct = Math.max(2, ((row.total_tokens || 0) / max) * 100);
    item.innerHTML = `
      <div>
        <div class="label">${escapeHtml(row[labelKey] || "unknown")}</div>
        <div class="track"><div class="fill" style="width:${pct}%"></div></div>
      </div>
      <strong>${fmt.format(row.total_tokens || 0)}</strong>
    `;
    target.appendChild(item);
  });
}

function renderRecent(rows) {
  ids.recentRows.innerHTML = "";
  if (!rows.length) {
    ids.recentRows.innerHTML = '<tr><td colspan="5">No usage events found yet</td></tr>';
    return;
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const date = row.timestamp ? new Date(row.timestamp).toLocaleString() : "";
    tr.innerHTML = `
      <td>${escapeHtml(date)}</td>
      <td>${escapeHtml(row.project || "unknown")}</td>
      <td>${escapeHtml(row.model || "unknown")}</td>
      <td>${fmt.format(row.total_tokens || 0)}</td>
      <td>${money.format(row.cost_usd || 0)}</td>
    `;
    ids.recentRows.appendChild(tr);
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

ids.window.addEventListener("change", load);
ids.refresh.addEventListener("click", load);
load().catch((error) => {
  ids.dailyChart.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
});
