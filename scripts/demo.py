"""Squadforces demo: a fictional team with synthetic submissions, running fully offline.

    python scripts/demo.py            # seed demo.db and serve it on http://localhost:8000
    python scripts/demo.py --seed     # only (re)create demo.db

Sign in as `admin` / `demo` (admin) or as any of the fictional members (`ioana`, `matei`, `sofia`, `radu`) with
password `demo-pass`. Contest and problem names are real public data (scripts/demo_data.json); every submission,
rating change and solve is generated. All judge APIs are replaced by in-memory fakes, so nothing here touches
Codeforces, AtCoder or Kilonova, and the refresh buttons keep working without a network.
"""
import asyncio
import json
import os
import random
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
DATA = json.loads((Path(__file__).parent / "demo_data.json").read_text(encoding="utf-8"))
DAY = 86400

# name -> (strength 0..1, Codeforces handle, AtCoder handle, Kilonova handle)
TEAM = {
    "ioana": (0.85, "demo_ioana", "ioana_ac", "ioana_kn"),
    "matei": (0.70, "demo_matei", "matei_ac", "matei_kn"),
    "sofia": (0.55, "demo_sofia", "sofia_ac", "sofia_kn"),
    "radu": (0.40, "demo_radu", "radu_ac", None),
}
GYM_TEAM = ("900001", "[DEMO] Squadforces")
GYM_TEAM_MEMBERS = ("ioana", "matei", "sofia")
# which of the gym's problems the fictional team solves, and with how many wrong attempts first
GYM_RESULT = {"A": 1, "C": 0, "D": 0, "F": 2, "H": 1, "J": 0, "K": 3}
# contests sat live (days ago); the others are done virtually or not at all
CF_CONTESTS = {"1998": 34, "2000": 22, "1985": 15}
# classic problems used as practice filler: (contest, index, name, rating)
PRACTICE = [(1, "A", "Theatre Square", 1000), (4, "A", "Watermelon", 800), (71, "A", "Way Too Long Words", 800),
            (231, "A", "Team", 800), (158, "A", "Next Round", 800), (50, "A", "Domino piling", 800),
            (282, "A", "Bit++", 800), (112, "A", "Petya and Strings", 800), (263, "A", "Beautiful Matrix", 800),
            (339, "A", "Helpful Maths", 800), (118, "A", "String Task", 1000), (58, "A", "Chat room", 1000)]
KN_PROBLEMS = [4373, 4378, 4379]


class World:
    """The fictional judges' state: every user's submissions, generated once from a fixed seed."""

    def __init__(self, now: float | None = None):
        self.now = int(now or time.time())
        self.rng = random.Random(2026)
        self.cf: dict[str, list[dict]] = {}
        self.cf_ratings: dict[str, list[dict]] = {}
        self.ac: dict[str, list[dict]] = {}
        self.ac_history: dict[str, list[dict]] = {}
        self.kn: dict[int, list[dict]] = {}
        self.kn_ids = {}
        for name, (strength, cf_h, ac_h, kn_h) in TEAM.items():
            self._build_cf(name, strength, cf_h)
            self._build_atcoder(strength, ac_h)
            if kn_h:
                self.kn_ids[kn_h] = len(self.kn_ids) + 101
                self._build_kilonova(strength, self.kn_ids[kn_h])
        self._build_gym()
        for handle, subs in self.cf.items():  # newest first, ids increasing with time
            subs.sort(key=lambda s: (s["creationTimeSeconds"], s["id"]), reverse=True)
            for i, s in enumerate(reversed(subs)):
                s["id"] = 300_000_000 + hash_id(handle) + i

    # ── Codeforces ───────────────────────────────────────────────────────────

    def _cf(self, handle, contest, index, name, verdict, at, ptype, rel=None, rating=None, team=None):
        author = {"contestId": int(contest), "participantType": ptype, "members": [{"handle": handle}]}
        if team:
            author["teamId"], author["teamName"] = int(team[0]), team[1]
        prob = {"contestId": int(contest), "index": index, "name": name}
        if rating:
            prob["rating"] = rating
        self.cf.setdefault(handle, []).append({
            "id": 0, "contestId": int(contest), "creationTimeSeconds": int(at),
            "relativeTimeSeconds": rel if rel is not None else 2147483647,
            "problem": prob, "author": author, "programmingLanguage": "C++20 (GCC 13-64)", "verdict": verdict})

    def _build_cf(self, name, strength, handle):
        rng = self.rng
        rating = 1100 + int(strength * 900)
        history = []
        for cid, days_ago in CF_CONTESTS.items():
            c = DATA["cf_contests"][cid]
            start = self.now - days_ago * DAY - (self.now - days_ago * DAY) % DAY + 9 * 3600
            mode = rng.choices(["live", "virtual", "none"], [0.6 + strength / 3, 0.25, 0.15])[0]
            length = 2 * 3600 if cid != "2000" else 2 * 3600 + 1800
            for i, p in enumerate(c["problems"]):
                p_solve = max(0.05, strength - 0.11 * i + rng.uniform(-0.1, 0.1))
                solved = mode != "none" and rng.random() < p_solve
                wrong = rng.choice([0, 0, 1, 1, 2, 3]) if (solved or rng.random() < 0.5) else 0
                t = int(length * (0.08 + 0.85 * (i + rng.random()) / len(c["problems"])))
                if mode != "none":
                    ptype, base = ("CONTESTANT", start) if mode == "live" else ("VIRTUAL", start + (3 + i % 3) * DAY)
                    for w in range(wrong):
                        self._cf(handle, cid, p["index"], p["name"], rng.choice(["WRONG_ANSWER", "WRONG_ANSWER", "TIME_LIMIT_EXCEEDED"]),
                                 base + max(60, t - (w + 1) * 400), ptype, max(60, t - (w + 1) * 400), p["rating"])
                    if solved:
                        self._cf(handle, cid, p["index"], p["name"], "OK", base + t, ptype, t, p["rating"])
                if (not solved) and rng.random() < 0.35 * strength + 0.1:  # upsolved a few days later
                    for w in range(rng.choice([0, 1, 2])):
                        self._cf(handle, cid, p["index"], p["name"], "WRONG_ANSWER", start + (1 + i % 2) * DAY + w * 900, "PRACTICE", rating=p["rating"])
                    self._cf(handle, cid, p["index"], p["name"], "OK", start + (1 + i % 2) * DAY + 3000, "PRACTICE", rating=p["rating"])
            if mode == "live":
                old = rating
                rating += rng.randint(-45, 70)
                history.append({"contestId": int(cid), "contestName": c["title"], "handle": handle, "rank": rng.randint(900, 6000),
                                "ratingUpdateTimeSeconds": start + length + 3600, "oldRating": old, "newRating": rating})
        self.cf_ratings[handle] = history
        for day in range(1, 420):  # practice filler: this is what the heatmap shows
            if rng.random() < 0.10 + 0.4 * strength * (1.2 if day < 60 else 0.7 if day < 230 else 0.45):
                for _ in range(rng.choice([1, 1, 2, 3])):
                    cid, idx, pname, prating = rng.choice(PRACTICE)
                    at = self.now - day * DAY + rng.randint(9, 22) * 3600
                    self._cf(handle, cid, idx, pname, rng.choices(["OK", "WRONG_ANSWER"], [0.65, 0.35])[0], at, "PRACTICE", rating=prating)

    def _build_gym(self):
        gym = DATA["gym"]["105427"]
        start = self.now - 11 * DAY - (self.now - 11 * DAY) % DAY + 10 * 3600
        for member in GYM_TEAM_MEMBERS:
            handle = TEAM[member][1]
            t = 0
            for p in gym["problems"]:
                if p["index"] in GYM_RESULT:
                    t += 900 + hash_id(p["index"]) % 1500
                    for w in range(GYM_RESULT[p["index"]]):
                        self._cf(handle, "105427", p["index"], p["name"], "WRONG_ANSWER", start + t - (w + 1) * 500, "VIRTUAL", t - (w + 1) * 500, team=GYM_TEAM)
                    self._cf(handle, "105427", p["index"], p["name"], "OK", start + t, "VIRTUAL", t, team=GYM_TEAM)
                elif p["index"] in "EG":
                    for w in range(2 if p["index"] == "E" else 1):
                        self._cf(handle, "105427", p["index"], p["name"], "TIME_LIMIT_EXCEEDED", start + 9000 + w * 700, "VIRTUAL", 9000 + w * 700, team=GYM_TEAM)

    # ── AtCoder ──────────────────────────────────────────────────────────────

    def _build_atcoder(self, strength, handle):
        rng = self.rng
        contest = DATA["atcoder"]["abc343"]
        start = contest["timing"]["start_epoch_second"]
        subs, rated = [], rng.random() < 0.5 + strength / 3
        for i, task in enumerate(contest["tasks"]):
            if rng.random() < strength - 0.09 * i + 0.1:
                for _ in range(rng.choice([0, 0, 1])):
                    subs.append((task["id"], "WA", start + 400 + i * 600))
                subs.append((task["id"], "AC", start + 900 + i * 700 + rng.randint(0, 300) if rated else start + 4 * DAY))
        for day in range(1, 200):
            if rng.random() < 0.06 + 0.12 * strength:
                pid = f"abc{rng.randint(300, 340)}_{rng.choice('abc')}"
                subs.append((pid, rng.choices(["AC", "WA"], [0.7, 0.3])[0], self.now - day * DAY + 15 * 3600))
        subs.sort(key=lambda s: s[2])
        self.ac[handle] = [{"id": 60_000_000 + hash_id(handle) + i, "epoch_second": at, "problem_id": pid,
                            "contest_id": pid.split("_")[0], "user_id": handle, "language": "C++ 20 (gcc 12.2)",
                            "point": 100.0, "length": 500, "result": res, "execution_time": 20}
                           for i, (pid, res, at) in enumerate(subs)]
        self.ac_history[handle] = [{"IsRated": True, "Place": rng.randint(200, 4000), "OldRating": 700 + int(strength * 400),
                                    "NewRating": 720 + int(strength * 400), "Performance": 900 + int(strength * 500),
                                    "ContestScreenName": "abc343.contest.atcoder.jp", "ContestName": "AtCoder Beginner Contest 343",
                                    "EndTime": "2024-03-09T22:40:00+09:00"}] if rated else []

    # ── Kilonova ─────────────────────────────────────────────────────────────

    def _build_kilonova(self, strength, user_id):
        rng = self.rng
        subs = []
        for pid in KN_PROBLEMS + [rng.randint(100, 900) for _ in range(6)]:
            for _ in range(rng.choice([1, 2, 3])):
                score = rng.choice([0, 20, 40, 60, 100]) if rng.random() > strength - 0.2 else 100
                subs.append((pid, score, self.now - rng.randint(1, 60) * DAY + rng.randint(0, 80000)))
        subs.sort(key=lambda s: s[2])
        self.kn[user_id] = [{"id": 1_100_000 + user_id * 1000 + i, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(at)),
                             "user_id": user_id, "problem_id": pid, "language": "cpp17", "status": "finished",
                             "compile_error": False, "contest_id": None, "score": score, "score_scale": 100,
                             "icpc_verdict": None if score == 100 else "test_verdict.wrong", "submission_type": "classic"}
                            for i, (pid, score, at) in enumerate(subs)]
        self.kn[user_id].reverse()  # newest first


def hash_id(text) -> int:
    return sum(ord(c) * (i + 3) for i, c in enumerate(str(text))) % 40_000


# ── the fake judges ─────────────────────────────────────────────────────────

def install_fakes(world: World, stack: ExitStack) -> None:
    """Replace every judge API call the app makes with an answer from `world`."""
    from app.scraper import atcoder as ac_api
    from app.scraper import codeforces as cf_api
    from app.scraper import kilonova as kn_api

    async def status(handle, offset=1, count=100000):
        return world.cf.get(handle, [])[offset - 1: offset - 1 + count]

    async def rating_history(handle):
        return world.cf_ratings.get(handle, [])

    async def contest_info(cid):
        c = DATA["cf_contests"][cid]
        return {"title": c["title"], "problems": c["problems"]}

    async def contest_problems(cid):
        return DATA["cf_contests"][cid]["problems"] if cid in DATA["cf_contests"] else []

    async def gym_meta(cid):
        g = DATA["gym"].get(cid)
        return {"id": int(cid), "name": g["title"], "phase": "FINISHED", "durationSeconds": g["duration"]} if g else None

    async def gym_problems(cid):
        return DATA["gym"][cid]["problems"]

    async def problem_rating(cid, index):
        for p in DATA["cf_contests"].get(str(cid), {}).get("problems", []):
            if p["index"] == index:
                return p["rating"]
        return next((r for c, i, _, r in PRACTICE if str(c) == str(cid) and i == index), None)

    async def validate(handle):
        return {"rating": 1500, "rank": "specialist"} if handle.startswith("demo_") else None

    async def ac_subs(handle, from_epoch=0):
        return [s for s in world.ac.get(handle, []) if s["epoch_second"] >= from_epoch]

    async def ac_history(handle):
        return world.ac_history.get(handle, [])

    async def ac_tasks(cid):
        return [dict(t) for t in DATA["atcoder"][cid]["tasks"]] if cid in DATA["atcoder"] else []

    async def ac_timing(cid):
        return DATA["atcoder"][cid]["timing"] if cid in DATA["atcoder"] else None

    async def ac_title(pid):
        return None

    async def kn_user(name):
        return world.kn_ids.get(name)

    async def kn_page(user_id, offset=0):
        rows = world.kn.get(user_id, [])
        return rows[offset: offset + 50], len(rows)

    async def kn_problem(pid):
        known = DATA["kilonova"].get(str(pid))
        return {"id": pid, "name": known["name"] if known else f"Practice problem {pid}", "score_scale": 100}

    async def kn_list(list_id):
        return {"id": list_id, "title": "Warm-up list", "list": KN_PROBLEMS}

    for module, name, fn in [
        (cf_api, "get_user_status", status), (cf_api, "get_user_rating_history", rating_history),
        (cf_api, "get_contest_info", contest_info), (cf_api, "get_contest_problems", contest_problems),
        (cf_api, "get_gym_metadata", gym_meta), (cf_api, "get_gym_problems", gym_problems),
        (cf_api, "get_problem_rating", problem_rating), (cf_api, "validate_handle", validate),
        (ac_api, "get_user_submissions", ac_subs), (ac_api, "get_rating_history", ac_history),
        (ac_api, "get_contest_tasks", ac_tasks), (ac_api, "get_contest_timing", ac_timing),
        (ac_api, "get_problem_title", ac_title),
        (kn_api, "get_user_id", kn_user), (kn_api, "get_user_submissions", kn_page),
        (kn_api, "get_problem", kn_problem), (kn_api, "get_problem_list", kn_list),
    ]:
        stack.enter_context(mock.patch.object(module, name, fn))
    stack.enter_context(mock.patch("app.scheduler.start"))  # its jobs would call the real judges


# ── seeding ─────────────────────────────────────────────────────────────────

async def seed(db) -> None:
    from app import models, sync
    from app.auth import hash_password

    pw = hash_password("demo-pass")
    users = {}
    for name, (_, cf_h, ac_h, kn_h) in TEAM.items():
        u = models.User(username=name, password_hash=pw, user_type="user", full_name=name.title(),
                        codeforces_handle=cf_h, atcoder_handle=ac_h, kilonova_handle=kn_h, cf_rating=1500, cf_rank="specialist")
        db.add(u)
        users[name] = u
    group = models.Group(name="Squadforces Demo Team", description="A fictional ICPC-style team (synthetic data).", hints_allowed=True)
    db.add(group)
    db.flush()
    for u in users.values():
        db.add(models.GroupMembership(group_id=group.id, user_id=u.id))

    def assignment(title, description):
        a = models.Assignment(group_id=group.id, title=title, description=description)
        db.add(a)
        db.flush()
        return a

    def item(a, platform, kind, ext, source_url=None, title=None):
        it = models.AssignmentItem(assignment_id=a.id, type=kind, platform=platform, external_id=ext, source_url=source_url,
                                   title=title, sync_status="pending", created_by_id=users["ioana"].id)
        db.add(it)
        db.flush()
        return it

    week = assignment("Week 12: ICPC virtual + upsolving", "A gym virtual by the whole team, then the Codeforces rounds it upsolves.")
    gym = item(week, "codeforces", "contest", "105427")
    rounds = [item(week, "codeforces", "contest", c) for c in CF_CONTESTS]
    abc = item(week, "atcoder", "contest", "abc343")
    theatre = item(week, "codeforces", "problem", "1/A")
    mixed = assignment("Kilonova, CSES and other sites", "Judges without an API are ticked off by hand.")
    kn_list = item(mixed, "kilonova", "contest", "77")
    kn_one = item(mixed, "kilonova", "problem", "4373")
    cses = [item(mixed, "cses", "problem", t, title=n) for t, n in (("1068", "Weird Algorithm"), ("1083", "Missing Number"), ("1094", "Increasing Array"))]
    edu = item(mixed, "codeforces", "problem", "274545/A", title="Binary Search: warm-up",
               source_url="https://codeforces.com/edu/course/2/lesson/4/3/practice/contest/274545/problem/A")
    other = item(mixed, "other", "problem", "url:demo", title="Two Sum (LeetCode)", source_url="https://leetcode.com/problems/two-sum/")
    db.commit()

    for it in [gym, *rounds, abc, theatre, kn_list, kn_one, *cses, edu, other]:
        await sync.sync_item(it.id, db)  # (a no-op for the hand-marked ones; it just marks them done)

    for it, who in [(cses[0], ("ioana", "matei", "sofia", "radu")), (cses[1], ("ioana", "matei")), (cses[2], ("ioana",)),
                    (edu, ("ioana", "matei", "sofia")), (other, ("matei", "radu"))]:
        for name in who:
            db.add(models.Result(assignment_item_id=it.id, user_id=users[name].id, solved=True))
    hard = db.query(models.ContestProblem).filter_by(assignment_item_id=gym.id, index="E").first()
    db.add(models.Hint(contest_problem_id=hard.id, kind="hint", text="Think about which components can be merged offline, in reverse order."))
    db.add(models.Hint(contest_problem_id=hard.id, kind="note", author_id=users["matei"].id, time_minutes=95,
                       text="Solved after the contest: reverse the operations and use a DSU with rollback. TLE came from an O(n log^2) merge."))
    db.commit()


def build_database(path: Path) -> None:
    """Create a fresh demo database at `path`."""
    from app.database import Base, SessionLocal, engine
    if path.exists():
        path.unlink()
    Base.metadata.create_all(engine)
    world = World()
    with ExitStack() as stack:
        install_fakes(world, stack)
        db = SessionLocal()
        try:
            asyncio.run(seed(db))
        finally:
            db.close()


def main() -> None:
    db_path = ROOT / "demo.db"
    os.environ["DATABASE_URL"] = "sqlite:///" + db_path.as_posix()
    os.environ.setdefault("ADMIN_PASSWORD", "demo")
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from app.main import app
    from app.database import engine
    build_database(db_path)  # after importing the app, so both use the same engine
    if "--seed" in sys.argv:
        print(f"Seeded {db_path}")
        return
    import uvicorn
    print("\nDemo running on http://localhost:8000  (admin: admin / demo, members: ioana, matei, sofia, radu / demo-pass)\n")
    with ExitStack() as stack:
        install_fakes(World(), stack)  # keep the fakes on so refresh buttons work offline
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
