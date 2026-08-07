# Token Tracker

<img width="3720" height="1920" alt="image" src="https://github.com/user-attachments/assets/0a8d8aed-4df2-4c2b-b36e-8d20e52da763" />



A zero-configuration local token usage tracker for Claude Code and Cline.

Token Tracker watches Claude Code JSONL session logs and Cline's SQLite message database / `taskHistory.json`, stores usage events in a local SQLite database, and serves a lightweight analytics dashboard from `localhost`.

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

## Background Collection

On Linux, install a user service:

```bash
tracker install-service
systemctl --user enable --now tokentracker
```

The service runs the collector in the background. The dashboard API is started on demand by `tracker dashboard`.