# Token Tracker

A zero-configuration local token usage tracker for Claude Code.

Token Tracker watches Claude Code JSONL session logs, stores usage events in a local SQLite database, and serves a lightweight analytics dashboard from `localhost`.

## Install

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

## Background Collection

On Linux, install a user service:

```bash
tracker install-service
systemctl --user enable --now tokentracker
```

The service runs the collector in the background. The dashboard API is started on demand by `tracker dashboard`.
