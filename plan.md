# Token Tracker
*A lightweight analytics dashboard for Claude Code & AI coding assistants.*

---

# Goal

Create a zero-configuration token usage tracker that automatically records every Claude Code session and provides a beautiful analytics dashboard.

The primary design goal is:

> Install once → forget it exists → click one button whenever you want analytics.

The user should NEVER have to:

- run `npm start`
- run `uvicorn`
- manually start a local server
- wrap Claude Code commands
- change their workflow

Instead:

- background collector starts automatically
- dashboard opens instantly
- usage is continuously updated

---

# Target Users

- Claude Code users
- Claude VSCode Extension users
- Cursor users (future)
- Gemini CLI users (future)
- Codex CLI users (future)

---

# High Level Architecture

    ┌────────────────────────┐
    │ Claude Code CLI        │
    └──────────┬─────────────┘
                │
        Writes logs/history
                │
                ▼
    ┌────────────────────────┐
    │ Background Collector   │
    │ (always running)       │
    └──────────┬─────────────┘
                │
                ▼
            SQLite Database
                │
        FastAPI (localhost only)
                │
                ▼
    React Dashboard (Browser)

---

# Design Principles

- Zero configuration
- Runs entirely locally
- No cloud backend
- No accounts
- No telemetry
- Minimal RAM usage (<30MB)
- Fast startup
- Beautiful UI

---

# Technology Stack

## Collector

Python

Libraries:

- watchdog
- sqlite3
- pydantic
- typer
- psutil

Purpose:

- monitor Claude log directory
- parse usage
- write SQLite

---

## Backend API

FastAPI

Libraries:

- fastapi
- uvicorn
- sqlalchemy
- pydantic

Purpose:

Serve dashboard data.

---

## Database

SQLite

Single file:

~/.tokentracker/usage.db

---

## Dashboard

Next.js

React

TypeScript

TailwindCSS

shadcn/ui

Recharts

---

## Packaging

Python package

```
pip install tokentracker
```

---

# Automatic Startup

Linux

Install as:

systemd --user service

```
systemctl --user enable tokentracker
```

Automatically starts on login.

---

# CLI

```
tracker
```

Opens browser.

```
tracker dashboard
```

Open dashboard.

```
tracker today
```

Print summary.

```
tracker week
```

Print weekly stats.

```
tracker projects
```

Project breakdown.

```
tracker models
```

Model usage.

---

# Database Schema

## usage_events

id

timestamp

thread_id

conversation_id

project

provider

model

prompt_tokens

completion_tokens

cache_read_tokens

cache_write_tokens

reasoning_tokens

total_tokens

latency_ms

cost_usd

metadata_json

---

# Dashboard Pages

## Home

Cards:

- API Equivalent
- Total Tokens
- Claude Credits
- Requests
- Threads

Charts:

- Tokens / day
- Cost / day
- Requests / day

Tables:

Recent sessions

---

## Projects

Per-project usage

Filters

Sorting

---

## Models

Usage by model

Cost by model

Average latency

---

## Threads

Each Claude conversation

Total tokens

Duration

Cost

Messages

---

## Timeline

Hourly usage

Daily usage

Weekly usage

Heatmap

---

# Filters

Window

- Today
- Yesterday
- 7 days
- 30 days
- 90 days
- Custom

Model

Project

Thread

Provider

---

# Charts

Area Chart

Daily tokens

Line Chart

Daily cost

Bar Chart

Model usage

Pie Chart

Project distribution

Heatmap

Hourly activity

---

# API Endpoints

GET

```
/stats
```

GET

```
/daily
```

GET

```
/models
```

GET

```
/projects
```

GET

```
/threads
```

GET

```
/timeline
```

GET

```
/settings
```

---

# Project Structure

```
tokentracker/

    collector/

        watcher.py

        parser.py

        database.py

        models.py

        service.py

        config.py

    api/

        main.py

        routes.py

        schemas.py

    cli/

        cli.py

    dashboard/

        app/

        components/

        hooks/

        lib/

        public/

    shared/

        pricing.py

        constants.py

        utils.py

    tests/

README.md

LICENSE

pyproject.toml

```

---

# Collector Responsibilities

Watch Claude directory

↓

Detect new messages

↓

Extract usage

↓

Normalize

↓

Insert SQLite

↓

Notify dashboard

---

# Dashboard Features

Cards

Charts

Search

Filters

Dark mode

Responsive

Keyboard shortcuts

---

# Future Features

GitHub integration

Usage by repository

---

Cost projections

Estimated monthly spend

# Nice UX Features

Open dashboard instantly

```
tracker
```

System tray icon

---

# MVP Milestone

✓ Background collector

✓ SQLite database

✓ FastAPI

✓ React dashboard

✓ Daily charts

✓ Cost calculation

✓ Thread table

✓ Filters

✓ CLI

✓ Automatic startup
