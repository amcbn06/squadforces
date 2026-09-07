# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
pip install -r requirements.txt
python run.py
```

App runs at `http://localhost:8000`. The SQLite database (`squadforces.db`) is created automatically on first startup via SQLAlchemy's `create_all`. To reset the DB, delete the file and restart.

Default admin password: set in `.env` as `ADMIN_PASSWORD` (default `squadforces2024`).

## Architecture

**Single-process FastAPI app** — no build step, no separate frontend. Templates are server-rendered Jinja2; interactivity comes from HTMX (dropdowns, expand/collapse) and Alpine.js (local toggle state). No React, no JS bundler.

### Request flow

1. Browser hits a FastAPI route in `app/routers/groups.py` or `app/routers/assignments.py`
2. Route checks auth via `Depends(require_auth)` → `app/auth.py` (cookie-based, single shared password)
3. Route renders a Jinja2 template from `app/templates/`
4. On item add, a `BackgroundTasks` task calls `app/sync.py::sync_item()` (async scrape, writes Results to DB)

### Key data flow: scraping

`app/sync.py` is the core sync engine. `sync_item(item_id, db)` dispatches to `_sync_cf_item` or `_sync_ac_item` based on `AssignmentItem.platform`. Those call the scrapers in `app/scraper/`:

- `codeforces.py` — wraps the official CF API (`contest.standings`, `user.rating`, `user.status`, `user.info`). Rate-limited to one request per 2.1s via `asyncio.sleep`. Supports optional API key/secret signing.
- `atcoder.py` — uses the kenkoooo.com AtCoder Problems API (no official API exists). Never scrapes HTML.

Sync is **idempotent** — it upserts `Result` and `ProblemResult` rows, so re-running is safe. `AssignmentItem.sync_status` tracks `pending → syncing → done | error`.

### Database

SQLAlchemy ORM with SQLite (switchable to PostgreSQL by changing `DATABASE_URL` in `.env`). All models are in `app/models.py`. No Alembic migrations yet — schema is managed via `Base.metadata.create_all` at startup (fine for SQLite, needs Alembic before a Postgres deploy).

Central entities:
- `AssignmentItem` — one contest or standalone problem attached to an `Assignment`
- `ContestProblem` — problems within a contest, auto-populated on sync
- `Result` — one row per (user, item): contest rank/rating/score or problem solved/unsolved
- `ProblemResult` — one row per (user, problem-within-contest): solved bool

### Matrix view

`app/routers/assignments.py::_build_matrix()` constructs a list of `{item, cells: [{user, result, problem_results}]}` dicts passed to `assignments/detail.html`. The template renders the table and handles the Alpine.js expand/collapse for contest sub-rows without a round-trip.

## Environment variables

| Variable | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Login password (shared for all mentors) |
| `SECRET_KEY` | Unused currently; reserved for future session signing |
| `DATABASE_URL` | SQLAlchemy URL, defaults to `sqlite:///./squadforces.db` |
| `CF_API_KEY` / `CF_API_SECRET` | Optional; enables signed CF API requests for higher rate limits |

## Extending

**Add a new platform**: create `app/scraper/newplatform.py` with `validate_handle`, `get_contest_problems`, `get_contest_results_for_handles`, `get_problem_solved`, `parse_contest_id`, `parse_problem_external_id`. Wire it into `app/sync.py` and the platform dropdown in `assignments/detail.html`.

**Switch to PostgreSQL**: set `DATABASE_URL=postgresql://...` and run Alembic migrations (needs `alembic init` + migration scripts — not yet set up).

**Cron refresh**: the scraper is stateless; run `python -c "import asyncio; from app.sync import sync_item; ..."` from GitHub Actions or any scheduler targeting active assignments.
