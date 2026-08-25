# Token Tracker

<img width="3720" height="1920" alt="image" src="https://github.com/user-attachments/assets/0a8d8aed-4df2-4c2b-b36e-8d20e52da763" />



A zero-configuration local token usage tracker for Claude Code, Cline, and Hermes.

Token Tracker watches Claude Code JSONL session logs, Cline's SQLite message database / `taskHistory.json`, and Hermes' SQLite state database, stores usage events in a local SQLite database, and serves a lightweight analytics dashboard from `localhost`.

## Install

### Development (recommended)

```bash
uv venv
uv sync
source .venv/bin/activate
```

### Production

```bash
pip install -e .
```

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
tracker today
tracker week
tracker projects
tracker models
tracker collect --once
tracker install-service
```

Data is stored at `~/.tokentracker/usage.db`. The default Claude log directory is `~/.claude/projects`; override it with `TOKEN_TRACKER_CLAUDE_DIR`.

## Cline

Cline (VS Code extension or CLI) stores its conversation history in a SQLite database. Token Tracker auto-detects it in the usual install locations and ingests it into the same database. To point at a specific file, set `TOKEN_TRACKER_CLINE_DB` to the path of Cline's `cline.db` (or similar).

Cline 4.x additionally stores per-task token aggregates in `state/taskHistory.json` inside its `globalStorage` directory (VS Code `globalStorage` or VS Code Server `~/.vscode-server/data/User/globalStorage/saoudrizwan.claude-dev`). Each record carries `tokensIn`, `tokensOut`, `cacheReads`, `cacheWrites`, `modelId`, and a millisecond `ts` timestamp. Token Tracker auto-discovers this file too; override with `TOKEN_TRACKER_CLINE_TASK_HISTORY` to point at a specific one.

Both Claude Code and Cline usage is aggregated together and labelled by the `provider` field (`claude` / `cline`).

## Hermes

Hermes stores each conversation and its aggregate token usage in a SQLite state database at `~/.hermes/state.db`. Token Tracker reads the `sessions` table and ingests finished conversations (those with an `ended_at` timestamp); in-progress sessions are picked up once they close. To point at a specific file, set `TOKEN_TRACKER_HERMES_DB` to the path of the state database.

Hermes usage is labelled with `provider = "hermes"` and aggregated alongside Claude Code and Cline.

## Background Collection

On Linux with systemd, install and start a per-user service that runs the collector in the background:

```bash
tracker install-service
```

This writes `~/.config/systemd/user/tokentracker.service` (respecting `XDG_CONFIG_HOME`), runs `systemctl --user daemon-reload`, and enables/starts the unit. The unit execs `python -m tokentracker.collector.service`, which loops over `scan_once()` every `TOKEN_TRACKER_POLL_SECONDS` (default 2 s). Stop and remove it with:

```bash
tracker uninstall-service
```

The dashboard API is started on demand by `tracker dashboard`.