# Token Tracker

<img width="3720" height="1920" alt="image" src="https://github.com/user-attachments/assets/0a8d8aed-4df2-4c2b-b36e-8d20e52da763" />



A zero-configuration local token usage tracker for Claude Code, Cline, and Hermes.

Token Tracker watches Claude Code JSONL session logs, Cline's SQLite message database / `taskHistory.json`, and Hermes' SQLite state database, stores usage events in a local SQLite database, and serves a lightweight analytics dashboard from `localhost`.

## Install

### For daily use (recommended)

Install the package as a standalone tool. This gives you a `tracker` command on your `PATH` and a
private environment under `~/.local/share/uv/tools/`. That environment is independent of the project
folder, so the background service keeps running when you delete or recreate the project's `.venv`.

```bash
git clone <this repo> ~/Projects/token-tracker
cd ~/Projects/token-tracker
uv tool install .
tracker install-service
tracker status
```

`pipx install .` works the same way if you do not use `uv`.

After you change the code, refresh the tool environment and restart the service:

```bash
uv tool install --reinstall .
tracker install-service
```

### Development

```bash
uv sync --extra dev
uv run pytest
uv run tracker
```

`uv sync` creates `.venv` inside the project. Do not run `tracker install-service` from this
environment: the service would point at `.venv`, and the next `rm -rf .venv` would stop it. The
command prints a warning when it detects this.

## Use

```bash
tracker
```

This starts the local dashboard server if needed and opens the dashboard in your browser.

The `tracker` command works from **any working directory** (the package is installed in site-packages), and so does the module form — handy when the venv's `bin` isn't on `PATH`:

```bash
python -m tokentracker.cli.cli dashboard
python -m tokentracker dashboard
```

Useful commands:

```bash
tracker dashboard
tracker status
tracker pricing
tracker reprice
tracker install-service
tracker uninstall-service
tracker ingest-token          # main tracker: allow other machines to push events
tracker link <url> --token …  # second machine: push events to the main tracker
```

Data is stored at `~/.tokentracker/usage.db`. The default Claude log directory is `~/.claude/projects`; override it with `TOKEN_TRACKER_CLAUDE_DIR`.

### How Claude Code transcripts are counted

Claude Code writes one transcript line per content block of a response (thinking, text, each tool call), and
every line repeats the response's `usage` object. Token Tracker counts each API response once: it keeps the
first line of each `message.id`. A request in the dashboard is one API call.

Databases written before this rule held one row per line, which inflated Claude token counts about 2.8x.
The first start after the upgrade writes a copy of the database next to it (`usage.db.bak-<timestamp>`) and
collapses those rows. Transcripts that still exist are re-parsed, so the result is exact. For transcripts
Claude Code has already deleted, consecutive rows with identical model and token counts within 30 minutes
are treated as one response. `tracker status` shows what the migration did.

## Cline

Cline (VS Code extension or CLI) stores its conversation history in a SQLite database. Token Tracker auto-detects it in the usual install locations and ingests it into the same database. To point at a specific file, set `TOKEN_TRACKER_CLINE_DB` to the path of Cline's `cline.db` (or similar).

Cline 4.x additionally stores per-task token aggregates in `state/taskHistory.json` inside its `globalStorage` directory (VS Code `globalStorage` or VS Code Server `~/.vscode-server/data/User/globalStorage/saoudrizwan.claude-dev`). Each record carries `tokensIn`, `tokensOut`, `cacheReads`, `cacheWrites`, `modelId`, and a millisecond `ts` timestamp. Token Tracker auto-discovers this file too; override with `TOKEN_TRACKER_CLINE_TASK_HISTORY` to point at a specific one.

Both Claude Code and Cline usage is aggregated together and labelled by the `provider` field (`claude` / `cline`).

## Hermes

Hermes stores each conversation and its aggregate token usage in a SQLite state database at `~/.hermes/state.db`. Token Tracker reads the `sessions` table and ingests every conversation, open or closed. An open session's counts grow while you chat, so the collector refreshes that row on each poll instead of waiting for `ended_at`. Hermes closes a session about a day after it starts, so this keeps the dashboard current. To point at a specific file, set `TOKEN_TRACKER_HERMES_DB` to the path of the state database.

Hermes records whatever model its `config.yaml` names, including a local model behind a vLLM server (for example `deepseek-v4-flash-0731` on `http://127.0.0.1:8888/v1`). The token counts come from the server's `usage` field in each response, so they are the server's own counts.

Hermes usage is labelled with `provider = "hermes"` and aggregated alongside Claude Code and Cline.

## Cost Estimates

Every event carries `cost_usd`, an estimate at Anthropic's published list prices. The dashboard shows the
total and how many requests it covers. The `?` button on the cost card opens an explanation of the formula
with the rate table in use. `tracker pricing` prints the same table and `tracker reprice` recomputes every
stored event.

**What is exact.** For Claude models the token counts come from the API's own `usage` object, which is
what Anthropic bills. The estimate applies the rules on the
[pricing page](https://platform.claude.com/docs/en/about-claude/pricing): per-model input and output
rates, 1.25x input for 5-minute cache writes, 2x for 1-hour writes, the per-model cache-read rate
(0.1x input, 0.025x on Fable 5.1, 0.05x on Opus 5.5 and Sonnet 5.5), fast-mode rates, the Haiku 5.5
over-100K prompt tier, the 1.1x US-only inference multiplier, and $0.01 per web search. Claude Code
transcripts carry all of these fields, so the estimate matches Claude Code's own `/usage` figure.

**What is not.** The figure is a list-price equivalent. On a Pro or Max subscription nothing is billed per
token, and API customers with negotiated rates or Batch discounts pay less. The bundled rate table is a
snapshot (`tracker pricing` prints its date); when Anthropic changes prices, update the table and run
`tracker reprice`. Cline and Hermes report only one cache-write total, so their cache writes are priced at
the 5-minute rate.

**Local models have no price.** Events from a model that is not in the table get `cost_usd = NULL` and
count as unpriced. To see what the same tokens would cost at a hosted provider, add a rate card to
`~/.tokentracker/pricing.json` (or the file named by `TOKEN_TRACKER_PRICING`). The collector reloads it
and reprices history when it changes. Keys are matched as a prefix of the model id. Set a key to `null`
to remove a bundled entry.

```json
{
  "deepseek-v4-flash": {"input": 0.14, "output": 0.28, "cache_read": 0.0028},
  "qwen3.8-27b": {"input": 0.0, "output": 0.0}
}
```

Rates are USD per million tokens. `cache_write_5m` and `cache_write_1h` default to 1.25x and 2x `input`.
When Cline or Hermes report a cost themselves, that figure is used instead of the table.

## Background Collection

Install and start a per-user service that runs the collector in the background:

```bash
tracker install-service
```

Two services are installed: the collector and the dashboard API (`tokentracker-dashboard.service` /
`com.tokentracker.dashboard`), so the dashboard and the ingest endpoint are always up.

- **Linux (systemd):** writes the units to `~/.config/systemd/user/` (respecting `XDG_CONFIG_HOME`), reloads the user manager, enables them, and restarts them.
- **macOS (launchd):** writes the plists to `~/Library/LaunchAgents/` and loads them with `launchctl bootstrap`. The agents start at login and restart if they exit. Logs are `~/.tokentracker/collector.log` and `dashboard.log`.
- **Windows:** not supported. The command prints the exact `python -m tokentracker.collector.service` line to schedule at logon with Task Scheduler.

The service runs `python -m tokentracker.collector.service`, which polls every `TOKEN_TRACKER_POLL_SECONDS` (default 2 s). Each poll re-parses only the files whose size or modification time changed, so an idle poll costs a directory walk and nothing else. On the first poll after a start it reads every transcript once.

The unit records the absolute path of the Python interpreter that ran `tracker install-service`. If that interpreter disappears, the service fails on every start. Check it with:

```bash
tracker status
```

This prints the service state and restart count, the interpreter path and whether it exists, and the newest event per source (`claude`, `cline`, `hermes`). A `MISSING` interpreter means: reinstall the package (`uv tool install --reinstall .`) and run `tracker install-service` again.

Stop and remove the service with:

```bash
tracker uninstall-service
```

The dashboard API is started on demand by `tracker dashboard`.

## Second machine

Every event carries the name of the machine it came from, and the dashboard gets a machine selector when
more than one is present. A second machine (for example a work laptop) runs the same collector and pushes
its events to the main tracker over HTTPS. Only token counts, model names, timestamps, and a salted hash of
each project name leave the machine; paths and titles never do. The laptop keeps its own copy and catches up
after being offline.

### On the main tracker

1. Install and start the services (the dashboard now runs as a service too, so it is always on):

   ```bash
   tracker install-service
   tracker ingest-token
   ```

   `ingest-token` writes a bearer token to `~/.tokentracker/ingest_token` (mode 600) and enables
   `POST /api/events`. Without that file the endpoint returns 404.

2. Make the endpoint reachable from the laptop. The dashboard binds to 127.0.0.1, so nothing is exposed
   until you choose how. With Tailscale on your personal tailnet, Funnel publishes one path on a public
   HTTPS address while the rest of the dashboard stays tailnet-only:

   ```bash
   # once, in the Tailscale admin console: enable HTTPS certificates and add the
   # "funnel" node attribute for this machine (Access controls -> nodeAttrs).
   sudo tailscale funnel --bg --set-path /api/events http://127.0.0.1:8765/api/events
   sudo tailscale serve  --bg --https=8443 http://127.0.0.1:8765
   tailscale funnel status
   ```

   The first line publishes only `/api/events` at `https://<machine>.<tailnet>.ts.net`. The second serves
   the whole dashboard at `https://<machine>.<tailnet>.ts.net:8443` to devices on your tailnet only.
   Funnel works even when the laptop is on a different tailnet, because the laptop only makes an outbound
   HTTPS request.

### Phone

With Tailscale on the phone, the dashboard is at `https://<machine>.<tailnet>.ts.net:8443`. In Safari, use
Share, then "Add to Home Screen", for an app icon.

For a home-screen widget, use the [Scriptable](https://scriptable.app) app with
[`extras/scriptable-widget.js`](extras/scriptable-widget.js). The script calls `/api/stats` for today and the
last seven days, draws cost, tokens and requests, opens the dashboard when tapped, and keeps the last numbers
when the phone is off the tailnet. Edit `BASE_URL` at the top if your address differs, and `HOST` to pin one
machine. iOS refreshes widgets on its own schedule, usually every 15 to 30 minutes.

### On the second machine

```bash
git clone <this repo> ~/Projects/token-tracker
cd ~/Projects/token-tracker
uv tool install .
tracker link https://<machine>.<tailnet>.ts.net --token <token from ingest-token> --host-label work-laptop
tracker install-service
tracker status
```

`link` saves the remote URL, the token, and a random salt for project hashing in `~/.tokentracker/config.json`
(mode 600). Pass `--project-labels clear` to send real project names. `tracker status` shows the pending
count and the last successful push; `tracker unlink` stops forwarding.

Claude Code writes its transcripts to `~/.claude/projects` on Linux and macOS, so no configuration is needed
for Claude Code. If Claude Code uses a different config directory (`CLAUDE_CONFIG_DIR`), set
`TOKEN_TRACKER_CLAUDE_DIR` to `<that dir>/projects` in the service environment.
