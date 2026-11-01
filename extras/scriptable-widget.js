// Token Tracker home-screen widget for iOS, run by the Scriptable app.
//
// Shows today's and the last seven days' estimated cost, tokens and requests,
// read from the dashboard's JSON API over Tailscale. Tapping the widget opens
// the dashboard. When the phone cannot reach the Spark (Tailscale off), the
// widget keeps showing the last numbers it fetched and says how old they are.
//
// Setup:
//   1. Install Scriptable from the App Store and open it once.
//   2. Create a new script, paste this file, name it "Token Tracker".
//   3. Edit BASE_URL below if your dashboard address differs.
//   4. Long-press the home screen, add a Scriptable widget (small or medium),
//      tap it and choose the "Token Tracker" script.
//
// Refresh: iOS decides when widgets reload. The script asks for a reload every
// REFRESH_MINUTES, which iOS honours only while the phone is awake and has
// budget (in practice every few minutes in use, 15 to 60 minutes otherwise).
// The footer shows how old the numbers are. To refresh on demand, add a second
// widget whose Parameter field is "refresh" and set "When Interacting" to
// "Run Script": tapping that widget re-fetches immediately instead of opening
// the dashboard.

const BASE_URL = "https://spark-44de.tailfbc629.ts.net:8443";
const HOST = ""; // "" = all machines, or e.g. "spark-44de" / "work-laptop"
const TIME_ZONE = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch (e) {
    return "";
  }
})();
const REFRESH_MINUTES = 5; // earliest next reload; Apple suggests no less than 5
const TAP_REFRESHES = (args.widgetParameter || "").trim().toLowerCase() === "refresh";

const fm = FileManager.local();
const cachePath = fm.joinPath(fm.documentsDirectory(), "tokentracker-widget.json");

async function fetchStats(window) {
  const params = [`window=${encodeURIComponent(window)}`];
  if (HOST) params.push(`host=${encodeURIComponent(HOST)}`);
  if (TIME_ZONE) params.push(`tz=${encodeURIComponent(TIME_ZONE)}`);
  const request = new Request(`${BASE_URL}/api/stats?${params.join("&")}`);
  request.timeoutInterval = 8;
  return request.loadJSON();
}

async function loadData() {
  try {
    const [today, week] = await Promise.all([fetchStats("today"), fetchStats("7d")]);
    const data = { today, week, fetchedAt: Date.now(), live: true };
    fm.writeString(cachePath, JSON.stringify(data));
    return data;
  } catch (error) {
    if (fm.fileExists(cachePath)) {
      const cached = JSON.parse(fm.readString(cachePath));
      return { ...cached, live: false, error: String(error) };
    }
    return { live: false, error: String(error) };
  }
}

// Same rule as the dashboard: whole digits below one billion, "1.23B" above.
function count(value) {
  const n = Math.round(Number(value) || 0);
  if (Math.abs(n) < 1e9) return n.toLocaleString("en-US");
  const billions = n / 1e9;
  const digits = Math.abs(billions) >= 100 ? 0 : Math.abs(billions) >= 10 ? 1 : 2;
  return `${billions.toFixed(digits)}B`;
}

function money(value) {
  return `$${(Number(value) || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function age(timestamp) {
  const minutes = Math.round((Date.now() - timestamp) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 90) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 36 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`;
}

function addLine(stack, text, size, color, weight) {
  const line = stack.addText(text);
  line.font = weight === "bold" ? Font.boldSystemFont(size) : Font.systemFont(size);
  line.textColor = color;
  line.lineLimit = 1;
  line.minimumScaleFactor = 0.5; // shrink long numbers instead of clipping them
  return line;
}

function addBlock(stack, label, stats, large) {
  addLine(stack, label, 11, Color.gray());
  addLine(stack, stats ? money(stats.cost_usd) : "—", large ? 26 : 20, Color.white(), "bold");
  addLine(stack, stats ? `${count(stats.total_tokens)} tokens · ${count(stats.requests)} req` : "no data", 11, Color.lightGray());
}

function buildWidget(data) {
  const widget = new ListWidget();
  widget.backgroundColor = new Color("#1a1d21");
  if (!TAP_REFRESHES) {
    widget.url = BASE_URL; // a tap opens the dashboard (via a brief hop through Scriptable, an iOS rule)
  }
  widget.setPadding(14, 14, 12, 14);
  widget.refreshAfterDate = new Date(Date.now() + REFRESH_MINUTES * 60 * 1000);

  const title = widget.addText("TOKEN TRACKER");
  title.font = Font.boldSystemFont(10);
  title.textColor = new Color("#64d2a6");
  widget.addSpacer(6);

  const medium = config.widgetFamily === "medium" || config.widgetFamily === "large";
  if (medium) {
    const row = widget.addStack();
    row.layoutHorizontally();
    const left = row.addStack();
    left.layoutVertically();
    addBlock(left, "Today", data.today, true);
    row.addSpacer();
    const right = row.addStack();
    right.layoutVertically();
    addBlock(right, "Last 7 days", data.week, true);
  } else {
    addBlock(widget, "Today", data.today, false);
    widget.addSpacer(4);
    addBlock(widget, "7 days", data.week, false);
  }

  widget.addSpacer();
  const footer = data.live
    ? `updated ${age(data.fetchedAt)}${TAP_REFRESHES ? " · tap to refresh" : ""}`
    : data.fetchedAt
      ? `offline · last ${age(data.fetchedAt)}`
      : "offline · turn on Tailscale";
  const note = widget.addText(footer);
  note.font = Font.systemFont(9);
  note.textColor = data.live ? Color.gray() : new Color("#f2bd5e");
  note.lineLimit = 1;
  return widget;
}

const widget = buildWidget(await loadData());
if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  await widget.presentMedium();
}
Script.complete();
