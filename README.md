<div align="center">
  <img src="static/logo.svg" width="90" alt="Squadforces logo"><br><br>
  <h1>Squadforces</h1>
  <p>Competitive programming tracker for teams and mentors.</p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white" alt="FastAPI">
    <img src="https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white" alt="SQLite">
    <img src="https://img.shields.io/badge/Railway-deployed-0B0D0E?logo=railway&logoColor=white" alt="Railway">
    <img src="https://img.shields.io/github/stars/amcbn06/squadforces?style=social&cacheSeconds=1" alt="GitHub stars">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  </p>

  <p>
    <a href="https://squadforces.up.railway.app"><strong>Live demo →</strong></a>
  </p>
</div>

---

Squadforces helps coaches track competitive programming progress across their entire squad. Create a group, assign Codeforces and AtCoder contests or standalone problems, and get a unified view of who solved what — live, virtually, or as upsolving.

## Features

- **Multi-platform** — Codeforces and AtCoder contests and standalone problems in a single assignment
- **Smart solve classification** — live, virtual, upsolving, and standalone; the most recent AC determines the type, so re-doing a contest virtually correctly updates its status
- **Assignment matrix** — per-assignment table with every member × every problem; contest rows expand to show per-problem results
- **Difficulty ratings** — CF rating shown per problem; AtCoder difficulty from kenkoooo's display formula
- **30-day leaderboard** — problems solved and contests completed per member per group, updated on every sync
- **Member profiles** — per-member submission heatmap (CF + AtCoder combined), current streak, longest streak
- **Member management** — add members with Codeforces and AtCoder handles; CF handle is validated against the API; handles editable at any time
- **Dark mode** — full dark/light theme toggle, persisted per browser

## Architecture

### System overview

```mermaid
graph TB
    subgraph Browser
        UI["Browser"]
    end

    subgraph App["FastAPI app (single process)"]
        Routes["Routes\ngroups · assignments · members"]
        Auth["Auth\ncookie session"]
        Sync["sync.py\nsync_item()"]
        Scheduler["APScheduler\nevery 6 h"]
    end

    subgraph External["External APIs"]
        CF["Codeforces API\ncontest.standings · contest.status\nuser.rating · user.status"]
        AC["kenkoooo.com\nAtCoder Problems API\nproblems · submissions · contest history"]
    end

    DB[("SQLite\nvia SQLAlchemy")]

    UI -->|HTTP| Routes
    Routes --> Auth
    Routes -->|BackgroundTask on item add| Sync
    Scheduler -->|periodic re-sync| Sync
    Sync -->|≥2.1 s between calls| CF
    Sync --> AC
    Routes --> DB
    CF --> DB
    AC --> DB
```

### Codeforces contest sync

```mermaid
sequenceDiagram
    participant S as sync_item()
    participant CF as Codeforces API
    participant DB as Database

    S->>CF: contest.standings (stream first 16 KB)
    CF-->>S: contest title + problem list
    S->>DB: upsert ContestProblem rows

    loop for each group member  [rate-limited: 2.1 s between calls]
        S->>CF: contest.status?contestId=X&handle=Y
        CF-->>S: member's submissions for this contest
        S->>DB: upsert Result + ProblemResult rows
        S->>CF: user.rating?handle=Y
        CF-->>S: full rating history
        S->>DB: write old rating / new rating / rank
    end

    Note over S: 5-minute hard timeout (asyncio.wait_for)
```

### Solve classification

```mermaid
flowchart TD
    A([submissions for contest]) --> B{any AC?}
    B -- no --> C[solved = false\ncollect wrong verdicts]
    B -- yes --> D{most recent AC\nparticipant type}
    D -- CONTESTANT --> E["✓ live (green)"]
    D -- VIRTUAL --> F["✓ virtual (blue)"]
    D -- PRACTICE --> G{ever entered\ncontest or virtual?}
    G -- yes --> H["▲ upsolving (orange)"]
    G -- no --> I["✓ standalone (grey)"]
```

## How syncing works

When a contest or problem is added to an assignment, a background task fetches every group member's submission history and classifies each result. Syncs are **idempotent** — re-running is safe and updates existing records. Sync status per item is tracked (`pending → syncing → done / error`). Auto-sync runs every 6 hours via APScheduler.

Solve types:

| Symbol | Meaning |
|---|---|
| ✓ green | Solved live during the contest |
| ✓ blue | Solved in virtual participation |
| ▲ orange | Upsolved after the contest ended |
| ✓ grey | Solved standalone (no associated contest) |

## Tech stack

| Layer | Choice |
|---|---|
| Backend | FastAPI + Python 3.11, async throughout |
| Templates | Jinja2, server-rendered — no JS build step |
| Interactivity | Inline JS for expand/collapse, heatmap, rating toggle |
| Database | SQLite via SQLAlchemy ORM (drop-in PostgreSQL support) |
| CF data | Official Codeforces API — anonymous streaming for problem lists, signed requests for user data |
| AtCoder data | [kenkoooo.com](https://kenkoooo.com/atcoder/) AtCoder Problems API, paginated submission fetch |
| Deployment | Railway with persistent SQLite volume at `/data` |

## Setup

```bash
git clone https://github.com/amcbn06/squadforces
cd squadforces
pip install -r requirements.txt
```

Create `.env`:

```env
ADMIN_PASSWORD=your_password
SECRET_KEY=your_secret_key
DATABASE_URL=sqlite:///./squadforces.db

# Optional — enables signed CF API requests (higher rate limits)
CF_API_KEY=
CF_API_SECRET=
SYNC_INTERVAL_HOURS=6          # auto-sync cadence (default 6h)
```

```bash
python run.py
# → http://localhost:8000
```

---

> Built with [Claude Code](https://claude.ai/code)

## Deployment on Railway

The repo ships with `railway.toml`. Steps:

1. Push to GitHub and connect the repo to Railway
2. Add a volume mounted at `/data`
3. Set `DATABASE_URL=sqlite:////data/squadforces.db`, `ADMIN_PASSWORD`, and `SECRET_KEY` as environment variables
4. Deploy — Railway picks up the start command from `railway.toml` automatically
