# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ResyGrabber is a Python-based restaurant reservation bot for Resy.com. It runs locally with a two-component architecture: a FastAPI proxy server and an interactive terminal UI client.

## Running the Project

```bash
# Start both server and client together (recommended)
python start.py

# Or manually in separate terminals:
cd server && python server.py      # Terminal 1 (port 8000)
cd client && python entry.py       # Terminal 2
```

## Installation

```bash
pip install -r client/requirements.txt
pip install -r server/requirements.txt
```

No build step required — pure Python.

## Architecture

**`client/`** — Interactive terminal UI (Click + Inquirer):
- `resygrabber.py` — Main application: menu system, task/account/proxy/schedule management, all user-facing logic
- `task_executor.py` — Core booking loop: polls Resy calendar API, calls the local server to get booking tokens, completes reservations, sends Discord notifications
- `entry.py` — Thin entry point calling `resygrabber.menu()`

**`server/`** — FastAPI proxy (port 8000):
- `server.py` — Two endpoints that forward requests to Resy's API (with optional proxy support):
  - `POST /api/get-details` → fetches book_token from Resy venue calendar
  - `POST /api/book-reservation` → completes the reservation

**Booking flow**: Task executor polls Resy → calls `/api/get-details` → gets book_token → calls `/api/book-reservation` → Discord notification on result.

## Persistent State (JSON files in `client/`)

| File | Purpose |
|------|---------|
| `tasks.json` | Configured reservation tasks (restaurant, dates, times, party size, accounts) |
| `accounts.json` | Resy auth tokens and payment IDs |
| `info.json` | CAPSolver/CapMonster API keys, Discord webhook URL |
| `restaurant_cache.json` | Cached restaurant names (reduces API calls) |
| `scheduled_tasks.json` | APScheduler task metadata for scheduled runs |

These files are user-managed and excluded from git. File I/O uses locks and atomic writes to prevent race conditions from concurrent task execution.

## Key Implementation Notes

- The server has CORS enabled for all origins (client communicates over localhost)
- SSL verification is disabled in server-side HTTP calls to Resy
- Proxies are formatted as `ip:port:username:password` and randomly selected per request
- APScheduler runs tasks in background threads; task_executor uses a thread pool for concurrent booking attempts
- The server exists as a proxy layer so proxies can be applied server-side without exposing them in client HTTP calls
