const ids = {
  cost: document.querySelector("#cost"),
  costNote: document.querySelector("#costNote"),
  totalTokens: document.querySelector("#totalTokens"),
  requests: document.querySelector("#requests"),
  threads: document.querySelector("#threads"),
  dailyChart: document.querySelector("#dailyChart"),
  seriesTitle: document.querySelector("#seriesTitle"),
  dailyRange: document.querySelector("#dailyRange"),
  modelsChart: document.querySelector("#modelsChart"),
  projectsChart: document.querySelector("#projectsChart"),
  window: document.querySelector("#window"),
  refresh: document.querySelector("#refresh"),
  costHelp: document.querySelector("#costHelp"),
  costDialog: document.querySelector("#costDialog"),
  costClose: document.querySelector("#costClose"),
  pricingMeta: document.querySelector("#pricingMeta"),
  rateRows: document.querySelector("#rateTable tbody"),
  unpricedNote: document.querySelector("#unpricedNote"),
  longPromptThreshold: document.querySelector("#longPromptThreshold"),
  usMultiplier: document.querySelector("#usMultiplier"),
  webSearchFee: document.querySelector("#webSearchFee"),
};

const fmt = new Intl.NumberFormat();
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

// Whole numbers with separators up to 999,999,999; from one billion, "1.23B".
// Visible values use this so a card or row never grows wider than its box.
function count(value) {
  const n = Math.round(Number(value) || 0);
  if (Math.abs(n) < 1e9) return fmt.format(n);
  const billions = n / 1e9;
  const digits = Math.abs(billions) >= 100 ? 0 : Math.abs(billions) >= 10 ? 1 : 2;
  return `${billions.toFixed(digits)}B`;
}

function money(value) {
  return value == null ? "—" : usd.format(value);
}

async function get(path) {
  const windowValue = ids.window.value;
  const suffix = windowValue ? `?window=${encodeURIComponent(windowValue)}` : "";
  const response = await fetch(`/api/${path}${suffix}`);
  if (!response.ok) throw new Error(`Failed to load ${path}`);
  return response.json();
}

async function load() {
  const [stats, series, models, projects] = await Promise.all([
    get("stats"),
    get("series"),
    get("models"),
    get("projects"),
  ]);
  renderStats(stats);
  renderSeries(series);
  renderBars(ids.modelsChart, models, "model");
  renderBars(ids.projectsChart, projects, "project");
}

function renderStats(stats) {
  ids.cost.textContent = money(stats.cost_usd || 0);
  const requests = stats.requests || 0;
  const priced = stats.priced_requests || 0;
  ids.costNote.textContent = requests
    ? priced === requests
      ? "every request priced"
      : `${fmt.format(requests - priced)} of ${fmt.format(requests)} requests unpriced (local models)`
    : "";
  ids.totalTokens.textContent = count(stats.total_tokens);
  ids.totalTokens.title = `${fmt.format(stats.total_tokens || 0)} tokens`;
  ids.requests.textContent = count(stats.requests);
  ids.threads.textContent = count(stats.threads);
}

const UNIT_LABEL = { hour: "Hour", day: "Day", week: "Week", month: "Month" };
const MAX_AXIS_LABELS = 12;

function periodLabel(period, unit) {
  const text = String(period);
  if (unit === "hour") return text.slice(11, 16);
  if (unit === "month") return text.slice(0, 7);
  return text.slice(5, 10);
}

function periodTitle(period, unit) {
  const text = String(period);
  if (unit === "hour") return `${text.slice(0, 10)} ${text.slice(11, 16)} UTC`;
  if (unit === "week") return `week of ${text.slice(0, 10)}`;
  if (unit === "month") return text.slice(0, 7);
  return text.slice(0, 10);
}

function renderSeries(series) {
  const unit = series.unit || "day";
  const rows = series.rows || [];
  ids.seriesTitle.textContent = `Tokens / ${UNIT_LABEL[unit] || "Day"}`;
  ids.dailyChart.innerHTML = "";
  if (!rows.length) {
    ids.dailyChart.innerHTML = '<div class="empty">No usage events found yet</div>';
    ids.dailyRange.textContent = "";
    return;
  }
  const max = Math.max(...rows.map((row) => row.total_tokens || 0), 1);
  const unitName = (UNIT_LABEL[unit] || "Day").toLowerCase();
  ids.dailyRange.textContent = `${rows.length} ${unitName}${rows.length === 1 ? "" : "s"}`;
  // Label at most MAX_AXIS_LABELS bars so labels never collide, whatever the bar count.
  const step = Math.max(1, Math.ceil(rows.length / MAX_AXIS_LABELS));
  rows.forEach((row, index) => {
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.style.height = `${Math.max(2, ((row.total_tokens || 0) / max) * 100)}%`;
    bar.title = `${periodTitle(row.period, unit)}: ${fmt.format(row.total_tokens || 0)} tokens, ${money(row.cost_usd)}, ${fmt.format(row.requests || 0)} requests`;
    if (index % step === 0) {
      bar.innerHTML = `<span>${periodLabel(row.period, unit)}</span>`;
    }
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
      <strong title="${fmt.format(row.total_tokens || 0)} tokens">${count(row.total_tokens)}<small>${row.cost_usd == null ? "unpriced" : money(row.cost_usd)}</small></strong>
    `;
    target.appendChild(item);
  });
}

async function openCostHelp() {
  ids.costDialog.showModal();
  try {
    const response = await fetch("/api/pricing");
    if (!response.ok) throw new Error("Failed to load pricing");
    renderPricing(await response.json());
  } catch (error) {
    ids.pricingMeta.textContent = error.message;
  }
}

function renderPricing(info) {
  ids.longPromptThreshold.textContent = fmt.format(info.long_prompt_threshold || 100000);
  ids.usMultiplier.textContent = String(info.us_inference_multiplier || 1.1);
  ids.webSearchFee.textContent = money(info.web_search_usd_per_request || 0.01);
  const override = info.override_active
    ? `Overrides from ${info.override_file} are applied.`
    : `No override file found at ${info.override_file}.`;
  ids.pricingMeta.textContent = `Rates copied from Anthropic's pricing page on ${info.source_date}. ${override} Run "tracker reprice" after changing rates.`;
  const rate = (value) => (value == null ? "—" : `$${Number(value).toLocaleString("en-US", { maximumFractionDigits: 4 })}`);
  ids.rateRows.innerHTML = Object.entries(info.rates || {})
    .map(
      ([model, rates]) => `<tr>
        <td>${escapeHtml(model)}${rates.fast ? ' <span class="tag">fast</span>' : ""}${rates.over_100k ? ' <span class="tag">tiered</span>' : ""}</td>
        <td>${rate(rates.input)}</td><td>${rate(rates.output)}</td><td>${rate(rates.cache_read)}</td>
        <td>${rate(rates.cache_write_5m)}</td><td>${rate(rates.cache_write_1h)}</td>
      </tr>`
    )
    .join("");
  const unpriced = info.unpriced_models || [];
  ids.unpricedNote.textContent = unpriced.length
    ? `Unpriced models in this database: ${unpriced
        .map((row) => `${row.model} (${count(row.total_tokens)} tokens)`)
        .join(", ")}. Add a rate card to the override file to estimate them.`
    : "Every model in this database has a rate card.";
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
ids.costHelp.addEventListener("click", () => openCostHelp().catch(() => {}));
ids.costClose.addEventListener("click", () => ids.costDialog.close());
ids.costDialog.addEventListener("click", (event) => {
  if (event.target === ids.costDialog) ids.costDialog.close();
});
load().catch((error) => {
  ids.dailyChart.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
});
