<div align="center">
  <img src="static/logo.svg" width="80" alt="Squadforces logo">
  <h1>Squadforces</h1>
  <p>A competitive programming tracker for teams and mentors.</p>
</div>

---

Squadforces lets you create groups of competitive programmers, assign Codeforces and AtCoder contests or standalone problems, and track who solved what — live, virtually, or as upsolving. Built for coaches who want a single view across their entire squad.

## Features

- **Multi-platform tracking** — Codeforces contests, AtCoder contests, and standalone problems from both platforms
- **Solve classification** — distinguishes live, virtual, upsolving, and standalone solves; most-recent AC wins (re-doing a contest virtually updates the status)
- **Assignment matrix** — one table per assignment showing every member × every problem, with expandable contest sub-rows
- **Difficulty ratings** — Codeforces rating per problem; AtCoder difficulty via kenkoooo's display formula
- **30-day leaderboard** — problems solved and contests participated per member, per group
- **Member management** — Codeforces and AtCoder handles, CF handle validated against the API on add/edit
- **Hide ratings toggle** — persistent per-browser preference

## Tech stack

| Layer | Choice |
|---|---|
| Backend | FastAPI + Python 3.11 |
| Templates | Jinja2 (server-rendered, no JS bundler) |
| Database | SQLite via SQLAlchemy ORM |
| CF data | Official Codeforces API (anonymous + signed) |
| AtCoder data | kenkoooo.com AtCoder Problems API |
| Deployment | Railway (SQLite volume at `/data`) |

## Setup

```bash
git clone https://github.com/amcbn06/squadforces
cd squadforces
pip install -r requirements.txt
```

Create a `.env` file:

```
ADMIN_PASSWORD=your_password
DATABASE_URL=sqlite:///./squadforces.db
# Optional — enables signed CF API requests (higher rate limits)
CF_API_KEY=
CF_API_SECRET=
```

Run:

```bash
python run.py
```

App is available at `http://localhost:8000`.

## Deployment

The repo includes `railway.toml` for one-command Railway deployment. Set a persistent volume at `/data` and point `DATABASE_URL` to `sqlite:////data/squadforces.db`. Required environment variables: `ADMIN_PASSWORD`, `SECRET_KEY`.

## How it works

Adding a contest or problem to an assignment triggers a background sync that fetches submissions for all group members and classifies each solve. Syncs are idempotent — re-running is safe and updates existing results.

Solve types:

| Symbol | Meaning |
|---|---|
| ✓ (green) | Solved live during the contest |
| ✓ (blue) | Solved in virtual participation |
| ▲ (orange) | Upsolved after the contest |
| ✓ (grey) | Solved standalone (outside any contest context) |
