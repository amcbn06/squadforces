"""AtCoder adapter — uses the AtCoder Problems API (kenkoooo.com)."""
import asyncio
import re
import time
from collections import defaultdict
from typing import Optional
import httpx

AC_PROBLEMS_BASE = "https://kenkoooo.com/atcoder"

_CATALOG_TTL = 12 * 3600
# (fetched_at, problems by id, [(contest_id, problem_index)] by problem id, problem ids by contest)
_catalog: Optional[tuple[float, dict[str, dict], dict[str, list[tuple[str, str]]], dict[str, list[str]]]] = None
_models: Optional[tuple[float, dict[str, dict]]] = None  # (fetched_at, difficulty models by problem id)


async def _get_catalog() -> tuple[dict[str, dict], dict[str, list[tuple[str, str]]], dict[str, list[str]]]:
    """Every AtCoder problem, every contest it appeared in, and each contest's problems.
    ~4 MB of JSON, so cached for all lookups."""
    global _catalog
    if _catalog is None or time.monotonic() - _catalog[0] > _CATALOG_TTL:
        async with httpx.AsyncClient(timeout=30) as client:
            problems_resp, mapping_resp = await asyncio.gather(
                client.get(f"{AC_PROBLEMS_BASE}/resources/problems.json"),
                client.get(f"{AC_PROBLEMS_BASE}/resources/contest-problem.json"),
            )
        problems_resp.raise_for_status()
        mapping_resp.raise_for_status()
        appearances: dict[str, list[tuple[str, str]]] = defaultdict(list)
        by_contest: dict[str, list[str]] = defaultdict(list)
        for row in mapping_resp.json():
            appearances[row["problem_id"]].append((row["contest_id"], row["problem_index"]))
            by_contest[row["contest_id"]].append(row["problem_id"])
        _catalog = (time.monotonic(), {p["id"]: p for p in problems_resp.json()}, appearances, by_contest)
    return _catalog[1], _catalog[2], _catalog[3]


async def _get_models() -> dict[str, dict]:
    """Kenkoooo's per-problem difficulty estimates. Optional: a failed fetch just means no ratings."""
    global _models
    if _models is None or time.monotonic() - _models[0] > _CATALOG_TTL:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{AC_PROBLEMS_BASE}/resources/problem-models.json")
            if resp.status_code != 200:
                return {}
            _models = (time.monotonic(), resp.json())
        except Exception:
            return {}
    return _models[1]


async def get_problem_title(problem_id: str) -> Optional[str]:
    """AtCoder's own task title, e.g. 'F - Second Largest Query'; None if the problem isn't indexed.

    The letter comes from the problem's home contest (the ID prefix, e.g. abc343 for abc343_f). Kenkoooo's
    own problem_index is the position in the latest contest that reused the problem (an ADT), so it's wrong here.
    """
    problems, appearances, _ = await _get_catalog()
    problem = problems.get(problem_id)
    if not problem or not problem.get("name"):
        return None

    home_contest, _, suffix = problem_id.rpartition("_")
    index = next((idx for cid, idx in appearances.get(problem_id, []) if cid == home_contest), None)
    if index is None and suffix.isalpha():
        index = suffix.upper()
    if index is None and appearances.get(problem_id):
        index = appearances[problem_id][0][1]
    return f"{index} - {problem['name']}" if index else problem["name"]


async def get_user_submissions(handle: str, from_epoch: int = 0) -> list[dict]:
    """A user's submissions from `from_epoch` (inclusive) on, oldest first. Kenkoooo returns at most 500 per call,
    so this pages on; each page resumes at the last second seen (inclusive: a page can end mid-second) and the
    caller de-duplicates by submission id."""
    url = f"{AC_PROBLEMS_BASE}/atcoder-api/v3/user/submissions"
    all_subs: list[dict] = []
    seen: set[int] = set()
    epoch = from_epoch
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(url, params={"user": handle, "from_second": epoch})
            resp.raise_for_status()
            batch = resp.json()
            fresh = [s for s in batch if s["id"] not in seen]
            for s in fresh:
                seen.add(s["id"])
            all_subs.extend(fresh)
            if len(batch) < 500 or not fresh:
                break  # short page = the end; a page with nothing new means 500+ sharing one second (can't advance)
            epoch = max(s.get("epoch_second", 0) for s in batch)
    return all_subs


async def get_rating_history(handle: str) -> list[dict]:
    """Rated contests of a user, from AtCoder's own public history JSON (kenkoooo has no such endpoint).
    Each entry has ContestScreenName ("abc300.contest.atcoder.jp"), Place, OldRating, NewRating, Performance, EndTime."""
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Squadforces/1.0"}) as client:
        resp = await client.get(f"https://atcoder.jp/users/{handle}/history/json")
    resp.raise_for_status()
    return resp.json()


async def get_contest_tasks(contest_id: str) -> list[dict]:
    """Tasks of a contest, each with kenkoooo's difficulty estimate when it has one.

    Membership comes from contest-problem.json, not from problems.json: that file lists each problem under
    a single contest, the latest one to reuse it (usually an AtCoder Daily Training), so filtering it by
    contest dropped most of an ABC's tasks (abc343 came back with 1 of its 7).
    """
    problems, appearances, by_contest = await _get_catalog()
    ids = set(by_contest.get(contest_id, ()))
    ids |= {pid for pid, p in problems.items() if p.get("contest_id") == contest_id}  # too new for the mapping
    tasks = [dict(problems[pid]) for pid in sorted(ids) if pid in problems]  # copies: the catalog is shared

    models = await _get_models()
    for t in tasks:
        # The label AtCoder gives the task in *this* contest (A..G, Ex); differs from the ID suffix for reused tasks.
        t["contest_index"] = next((idx for cid, idx in appearances.get(t["id"], ()) if cid == contest_id), None)
        diff = models.get(t.get("id", ""), {}).get("difficulty")
        if diff is not None:
            if diff >= 400:
                t["difficulty"] = int(round(diff))
            else:
                t["difficulty"] = max(1, int(round(400 / (2 ** ((400 - diff) / 278)))))

    return tasks


_contests: Optional[tuple[float, dict[str, dict]]] = None


async def get_contest_timing(contest_id: str) -> dict | None:
    """Return {start_epoch_second, duration_second} for a contest, or None if not found.
    contests.json is ~1 MB, so it is cached for all lookups."""
    global _contests
    if _contests is None or time.monotonic() - _contests[0] > _CATALOG_TTL:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{AC_PROBLEMS_BASE}/resources/contests.json")
        if resp.status_code != 200:
            return None
        _contests = (time.monotonic(), {c["id"]: c for c in resp.json()})
    c = _contests[1].get(contest_id)
    if not c:
        return None
    return {"start_epoch_second": c["start_epoch_second"], "duration_second": c["duration_second"]}


async def validate_handle(handle: str) -> bool:
    """Check if an AtCoder handle exists by fetching their submission history."""
    try:
        url = f"{AC_PROBLEMS_BASE}/atcoder-api/v3/user/submissions"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params={"user": handle, "from_second": 0})
        # A 200 with any array (even empty) means the handle is valid
        return resp.status_code == 200
    except Exception:
        return False


def parse_problem_id(url_or_id: str) -> Optional[tuple[str, str]]:
    """
    Parse an AtCoder problem URL into (contest_id, problem_id).
    Supports: https://atcoder.jp/contests/abc123/tasks/abc123_a
              abc123_a
    """
    url_or_id = url_or_id.strip()
    m = re.search(r"contests/([^/]+)/tasks/([^/?#]+)", url_or_id)
    if m:
        return m.group(1), m.group(2)
    # bare problem_id like "abc123_a"
    if re.match(r"^[a-z0-9]+_[a-z0-9]+$", url_or_id, re.I):
        parts = url_or_id.rsplit("_", 1)
        return parts[0], url_or_id
    return None


def parse_contest_id(url_or_id: str) -> Optional[str]:
    """
    Parse an AtCoder contest URL or slug.
    Supports: https://atcoder.jp/contests/abc123  → "abc123"
    Slugs may contain "_" and "-" (e.g. adt_hard_20240502_2).
    """
    url_or_id = url_or_id.strip()
    m = re.search(r"contests/([^/?#]+)", url_or_id)
    if m:
        return m.group(1)
    if re.match(r"^[a-z0-9_-]+$", url_or_id, re.I):
        return url_or_id
    return None
