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

### Audit log

`app/audit.py::record(db, request, action, actor=..., target_*, group_id, details, ok, commit)` adds an `AuditEvent` to the **same session as the action**, before the action's own `db.commit()`, so they commit or roll back together; pass `commit=True` only for events with no other change (failed sign-in, refusal). It never raises. Any new route that changes users, groups, membership, invites or assignments should record an event and add its code to `ACTION_LABELS` (and to `GROUP_ACTIONS` if a group's owner may see it). Secrets are dropped by `_scrub` (any detail key containing password/token/secret/hash/cookie), so don't name a harmless flag that way. Refused (403) requests are recorded by the exception handler in `app/main.py` through `request.state.audit_db`, set by `require_auth`. Read it at `/admin/audit`; owners see `audit.for_group()` on their group page. Rows aren't linked to users or groups (they outlive them) and are pruned after `RETENTION_DAYS` by the scheduler. `User.last_login_at` is set on sign-in and registration.

### Resources

`Group.resources_allowed` is a third switch beside `hints_allowed` / `notes_allowed` (group form, audited in `group.edit`). With it on, each assignment page shows a collapsed "Resources" box under the bulk add, listing that assignment's `Resource` rows (free text, up to 2000 characters, 50 per assignment; `Assignment.resources`, deleted with it). Writing (`POST /assignments/{id}/resources/add|{rid}/edit|{rid}/delete`) is `can_manage_group` only and refused (400) while the switch is off; members just read, and see no box at all while it's empty. An edit that is blank or unchanged is ignored (removing is the delete button's job). Turning the switch off hides the entries without deleting them. Entries are Markdown, rendered by the `markdown` template filter (`app/templating.py::render_markdown`, markdown-it-py): raw HTML is off so typed markup shows as text, markdown-it refuses `javascript:`/`data:`/`vbscript:` link targets, images are off, links open in a new tab, bare URLs are linkified (linkify-it-py) and a single newline is a line break — so it is safe to render user text without a separate sanitizer, and the `tests/test_resources.py` `Markdown` cases pin that. Changes are audited as `assignment.resource_add` / `resource_edit` / `resource_delete`, visible to the group's owner like the other `assignment.` events.

### Editing an assignment

`GET/POST /assignments/{id}/edit-page` and `/edit` reuse `assignments/form.html` (an optional `assignment` in context switches it between New and Edit, the same pattern as `groups/form.html`); gated by `can_manage_group`, same as Delete. The new-assignment JS that fills today's date only runs when there is no `assignment`, so editing never overwrites a stored (or deliberately empty) date.

### Leaderboards

`app/leaderboard.py`: a problem counts for a user once, if their *first* accepted submission (per platform + problem key) falls in the last 30 days. Computed by query (accepted in the window and no older acceptance, via the `(user, platform, problem_key)` index) rather than a stored flag, so it can't go stale as submissions are added or re-graded; only users whose full history is loaded (`SubmissionSync.full_sync_at`) are counted. The group board intersects that with the group's problems from `Platform.problem_keys(item)` (override it if a platform's contest problems are keyed differently from `ContestProblem.platform_problem_id`); the platform board (home page, right side) is per account type: users are ranked against users, students against students (the admin sees both, and is never ranked). An account created by the admin with an empty password stores `auth.NO_LOGIN` (`"!"`): `verify_password` always refuses it, so it can be a group member and on the boards but never signed in to; setting a password later enables it. Manual platforms (CSES, links) have no submissions and don't count.

### Recommend page

`app/recommend.py` grades and ranks two independent pools, one per tab (`/recommend?platform=cf|atc`), both through
the same `grade_contest()` / `_problem_fit()` — a geometric-weighted fit score with the same ideal (0–300 over the
graded rating) and stretch (300–500) bands for both judges. Both pools are DB-backed the same way —
`CfContest`/`CfContestProblem` and `AtcContest`/`AtcContestProblem` — filled by the scheduler, never queried live
at render time; a request only ever reads the DB. Codeforces refreshes at one contest per minute because its API
is rate-limited; AtCoder has no such limit (`app/scraper/atcoder.py`'s catalog is already cached in-process), so
its cache is normally built in one pass (`bootstrap_atc_cache()` / the daily `_refresh_atc_contest_cache` job) —
that's the whole reason it has its own tables instead of being computed live as it first was. Divisions are CF's
existing `div1`../`combined`/`other` vs. AtCoder's `abc`/`arc`/`agc`/`other` (`detect_atc_division()`, from the
contest id prefix) — two separate checkbox groups and default sets, not merged.

The Member dropdown (`app/routers/recommend.py::recommend_page`) is scoped to the active tab: only accounts of the
viewer's own `user_type` (a user sees users, a student sees students, the admin sees both — same rule as
`leaderboard.for_platform()`) that have a handle linked to that tab's platform. With nobody explicitly chosen (or a
stale id left over from switching tabs), the viewer is auto-selected if they themselves qualify, else the dropdown
stays blank. `recommend.effective_rating(user, platform)` is the number both the dropdown's pre-fill and the
default grading rating use — the cached rating, or the 800 floor if a handle is linked but has never rated a
contest there (e.g. someone unrated on Codeforces). A typed `rating` always overrides whatever a selected member
would imply, and is parsed leniently so a bad value never 422s.

The "submissions" filter (`sub_filter=all|none|attempted|solved`) keeps only contests where the *selected member's*
(not necessarily the viewer's) stored submissions land in that bucket (`_submission_bucket()`) — the member picked
in the dropdown is who gets graded *and* whose history is checked; with nobody picked it falls back to the viewer.
It only applies to contests with a real, cached problem set (`exact` grading) — the typical-distribution fallback
isn't a real problem list to check submissions against — and needs that member to have a handle on the platform.

### Cached rating vs rating_entries

`User.cf_rating` / `cf_rank` (shown wherever a member is listed: matrix, group page, leaderboards, Recommend) are a snapshot, refreshed by `Codeforces.refresh_profile()` on every submission refresh (`app/submissions.py::refresh_user`) — not the same thing as `rating_entries`, which already came from `fetch_rating_history` on every refresh and was never the stale one. Before this, the snapshot was set only when a handle was first saved, so a rating change after a contest didn't reach it until the handle was next edited by hand. `User.atc_rating` is the same idea for AtCoder (`AtCoder.refresh_profile()`): the latest *rated* entry's `NewRating` from the handle's history, skipping unrated contests in between; stays `None` for a handle that has never finished a rated one, same as an unrated CF handle leaves `cf_rating` `None`.

### Activity heatmap

`app/activity.py::user_activity()` returns daily *solved* counts (`submissions.daily_solved_counts()`, accepted submissions only — an all-WA day doesn't light up) from the user's very first stored submission on any of the three judges (`submissions.earliest_submission_at()` per platform, oldest wins, regardless of verdict — only the range needs to reach back that far); `app/templates/users/profile.html`'s script renders the current year from Jan 1 to today, then complete Jan-Dec years below back to the first one with data.

### Kilonova: tried vs untried

`ProblemResult.attempts` (per-problem, in a contest item) and `Result.raw_scrape_data["kn_attempted"]` (a whole contest's total, or a standalone problem) record whether a member submitted anything at all, so the matrix can tell "never touched it" (a dash) from "submitted and scored 0" ("0p"), which used to look identical.

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
