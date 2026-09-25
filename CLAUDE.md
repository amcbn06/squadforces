# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
pip install -r requirements.txt
python run.py
```

App runs at `http://localhost:8000`. The SQLite database (`squadforces.db`) is created automatically on first startup via SQLAlchemy's `create_all`. To reset the DB, delete the file and restart.

Initial admin password: set in `.env` as `ADMIN_PASSWORD` (default `squadforces2024` locally; a deployed instance, i.e. with `RAILWAY_ENVIRONMENT` set, refuses to start without it, and without `SECRET_KEY`). It only seeds the admin account when the database is first created; after that, change it from the **Password** link in the nav (`/account/password`), and editing `ADMIN_PASSWORD` has no effect.

## Architecture

**Single-process FastAPI app** — no build step, no separate frontend. Templates are server-rendered Jinja2; interactivity comes from HTMX (dropdowns, expand/collapse) and Alpine.js (local toggle state). No React, no JS bundler.

### Request flow

1. Browser hits a FastAPI route in `app/routers/groups.py` or `app/routers/assignments.py`
2. Route checks auth via `Depends(require_auth)` → `app/auth.py` (signed session cookie)
3. Route renders a Jinja2 template from `app/templates/`
4. On item add, a `BackgroundTasks` task calls `app/sync.py::sync_item()`, which refreshes the members' stored submissions and derives the item's results from them

### Key data flow: the submission store

Judges are only ever asked for *a user's submissions*, once, and every contest/problem status is then read from the database:

- `app/submissions.py` — the store. `refresh_user(db, user, platform)` mirrors a user's whole history on one platform into the `submissions` table and afterwards only fetches what is newer than the newest stored submission (plus anything the judge hadn't finished grading). `submission_syncs` records when each user/platform was last refreshed; changing a handle resets that user's copy; rating history lives in `rating_entries`. Reads: `for_contest`, `for_problem`, `for_problems`.
- `app/sync.py` — `sync_item(item_id, db)`: refresh the members' stores (skipped if the copy is < 90 s old), then call the item's platform module to fill in titles / problem lists and derive `Result` / `ProblemResult` rows. `AssignmentItem.sync_status` tracks `pending → syncing → done | error | not_started`. Sync is idempotent.
- The live / virtual / upsolved classification is `classify_contest()` in `app/platforms/cf.py` (participant type from the stored submissions) and `app/platforms/atc.py` (submission time vs the contest window). `tests/test_classify.py` checks both against the pre-store implementation (`tests/legacy_reference.py`).

### Platforms and problem detection

- `app/problems.py` — the universal switch. `detect_source(token)` is a chain of `if`s (CF, CF EDU, CF gym, AtCoder, Kilonova, CSES, default = unknown link); `parse_link` / `parse_links` (bulk paste) / `parse_form` (the add-item form) turn input into a `ParsedLink`.
- `app/platforms/<name>.py` — everything specific to one judge, as a `Platform` subclass (see `app/platforms/base.py`): its links (`parse_url`, `parse_bare`, `parse_form`), how it is displayed (`item_url`, `problem_url`, icon, `manual_status`, `default_title`, and for the profile's recent-submissions list `submission_url`, `submission_problem_url`, `submission_problem_label`, `fetch_problem_names` where submissions carry no title), how its submissions are fetched (`fetch_submissions`, `fetch_rating_history`) and how an item's results are derived (`sync_item`). `registry.py` lists them; templates get `platform_of`, `item_url`, `problem_url` from `app/templating.py`, so there are no per-platform `if`s in templates.
- `app/scraper/` — only the HTTP calls (`codeforces.py`, `atcoder.py` via kenkoooo + atcoder.jp rating history, `kilonova.py`). Codeforces requests are serialized and spaced 2.1 s apart.
- Manual platforms have no API: `cses` and `other` (any http(s) link, shown with a "?" icon) — plus Codeforces EDU problems. Members mark these solved themselves.

### Database

SQLAlchemy ORM with SQLite (switchable to PostgreSQL by changing `DATABASE_URL` in `.env`). All models are in `app/models.py`. No Alembic migrations yet — schema is managed via `Base.metadata.create_all` at startup (fine for SQLite, needs Alembic before a Postgres deploy).

Central entities:
- `AssignmentItem` — one contest or standalone problem attached to an `Assignment`
- `ContestProblem` — problems within a contest, auto-populated on sync
- `Submission` — one row per judge submission per user (a team submission is stored for each member); `SubmissionSync` — per user/platform refresh state; `RatingEntry` — rated-contest history
- `Result` — one row per (user, item): contest rank/rating/score or problem solved/unsolved (derived from the store, or set by hand for manual platforms)
- `ProblemResult` — one row per (user, problem-within-contest): solved bool, solve type, attempts

Existing databases get new columns through `ensure_columns()` in `app/database.py` (additive `ALTER TABLE ADD COLUMN`); new tables come from `create_all`; one-time data steps go in idempotent functions called right after it (`migrate_groups()`).

### Groups, ownership and invites

- A `Group` has an `owner_id` (the admin, id 0, for groups made before ownership existed) and a `max_members`. `can_manage_group(account, group)` in `app/auth.py` (owner or admin) gates removing members, writing hints/solutions, deleting assignments, editing/deleting the group and making invite links. Any signed-in `user`-type account may create groups until it owns `MAX_GROUPS_PER_USER` (5); students may not; the admin is exempt. Limits live in `app/limits.py`.
- Only the admin can add someone by username (`/groups/<id>/members/add`, ignoring the member limit); everyone else joins by accepting an invite link. Members can leave; an owner can't (the admin can transfer ownership).
- `app/invites.py` + `app/routers/invites.py`: one `Invite` shape for platform invites (admin; create an account, optionally joining a group) and group invites (owner or admin; an existing account joins; an admin-made one can also allow sign-up). Only the token's SHA-256 is stored; the link is shown once (session flash). `redeem()` is atomic. Registration (`/register`) needs a valid invite unless `ALLOW_OPEN_REGISTRATION` is set.

### Matrix view

`app/routers/assignments.py::_build_matrix()` constructs a list of `{item, cells: [{user, result, problem_results}]}` dicts passed to `assignments/detail.html`. The template renders the table and handles the Alpine.js expand/collapse for contest sub-rows without a round-trip.

## Environment variables

| Variable | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Initial admin password, used only when the admin account is first created |
| `SECRET_KEY` | Signs the session cookies; set a long random value in production |
| `COOKIE_SECURE` | `1` to mark the session cookie Secure outside Railway (it is on automatically when `RAILWAY_ENVIRONMENT` is set) |
| `ALLOW_OPEN_REGISTRATION` | `1` = registration without an invite (development / demo). Off by default; read on every request |
| `PUBLIC_URL` | Base URL for invite links (else `RAILWAY_PUBLIC_DOMAIN`, else the request's address) |
| `SYNC_INTERVAL_HOURS` | How often stored submissions and stale items refresh (default 2) |
| `DATABASE_URL` | SQLAlchemy URL, defaults to `sqlite:///./squadforces.db` |
| `CF_API_KEY` / `CF_API_SECRET` | Optional; enables signed CF API requests for higher rate limits |

## Extending

**Add a new platform**: (1) create `app/platforms/<name>.py` with a `Platform` subclass (copy the closest existing one: `cses.py` for a manual platform, `kn.py` for one with an API) and a `PLATFORM = ...` instance; (2) register it in `app/platforms/registry.py`; (3) add one `if` for its links to `detect_source()` in `app/problems.py` and one line to `SOURCE_PLATFORM`; (4) if it has submissions, add its handle column to `User` (+ `ensure_columns`) and the HTTP calls to `app/scraper/`. The add-item form's platform dropdown and the matrix icons/links follow from the registry.

**Tests**: `python -m unittest discover -s tests -t .` (standard library only; the network is always faked).

**Switch to PostgreSQL**: set `DATABASE_URL=postgresql://...` and run Alembic migrations (needs `alembic init` + migration scripts — not yet set up).

**Cron refresh**: sync is idempotent; run `python -c "import asyncio; from app.sync import sync_item; ..."` from GitHub Actions or any scheduler targeting active assignments.
