"""AtCoder adapter — uses the AtCoder Problems API (kenkoooo.com)."""
import asyncio
import re
import time
from collections import defaultdict
from typing import Optional
import httpx

AC_PROBLEMS_BASE = "https://kenkoooo.com/atcoder"

_CATALOG_TTL = 12 * 3600
# (fetched_at, problems by id, [(contest_id, problem_index)] by problem id)
_catalog: Optional[tuple[float, dict[str, dict], dict[str, list[tuple[str, str]]]]] = None


async def _get_catalog() -> tuple[dict[str, dict], dict[str, list[tuple[str, str]]]]:
    """Every AtCoder problem and every contest it appeared in. ~4 MB of JSON, so cached for all lookups."""
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
        for row in mapping_resp.json():
            appearances[row["problem_id"]].append((row["contest_id"], row["problem_index"]))
        _catalog = (time.monotonic(), {p["id"]: p for p in problems_resp.json()}, appearances)
    return _catalog[1], _catalog[2]


async def get_problem_title(problem_id: str) -> Optional[str]:
    """AtCoder's own task title, e.g. 'F - Second Largest Query'; None if the problem isn't indexed.

    The letter comes from the problem's home contest (the ID prefix, e.g. abc343 for abc343_f). Kenkoooo's
    own problem_index is the position in the latest contest that reused the problem (an ADT), so it's wrong here.
    """
    problems, appearances = await _get_catalog()
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
    """Return all submissions for a user. Paginates automatically (kenkoooo returns max 500/page)."""
    url = f"{AC_PROBLEMS_BASE}/atcoder-api/v3/user/submissions"
    all_subs: list[dict] = []
    epoch = from_epoch
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            resp = await client.get(url, params={"user": handle, "from_second": epoch})
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_subs.extend(batch)
            if len(batch) < 500:
                break
            epoch = max(s.get("epoch_second", 0) for s in batch) + 1
    return all_subs


async def get_contest_results(contest_id: str, handle: str) -> dict:
    """
    Return contest info for a user.
    Uses the results API: /atcoder-api/v3/user/contest_history
    """
    url = f"{AC_PROBLEMS_BASE}/atcoder-api/v3/user/contest_history"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, params={"user": handle})
    resp.raise_for_status()
    history = resp.json()
    for entry in history:
        if entry.get("ContestScreenName") == contest_id or entry.get("ContestSlug") == contest_id:
            return entry
    return {}


async def get_contest_tasks(contest_id: str) -> list[dict]:
    """Return list of tasks in a contest with difficulty ratings from kenkoooo."""
    async with httpx.AsyncClient(timeout=20) as client:
        problems_resp = await client.get(f"{AC_PROBLEMS_BASE}/resources/problems.json")
        models_resp = await client.get(f"{AC_PROBLEMS_BASE}/resources/problem-models.json")
    problems_resp.raise_for_status()
    all_problems = problems_resp.json()
    tasks = [p for p in all_problems if p.get("contest_id") == contest_id]
    tasks.sort(key=lambda p: p.get("id", ""))

    if models_resp.status_code == 200:
        models = models_resp.json()
        for t in tasks:
            model = models.get(t.get("id", ""), {})
            diff = model.get("difficulty")
            if diff is not None:
                if diff >= 400:
                    t["difficulty"] = int(round(diff))
                else:
                    t["difficulty"] = max(1, int(round(400 / (2 ** ((400 - diff) / 278)))))

    return tasks


async def get_contest_timing(contest_id: str) -> dict | None:
    """Return {start_epoch_second, duration_second} for a contest, or None if not found."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{AC_PROBLEMS_BASE}/resources/contests.json")
    if resp.status_code != 200:
        return None
    for c in resp.json():
        if c.get("id") == contest_id:
            return {
                "start_epoch_second": c["start_epoch_second"],
                "duration_second": c["duration_second"],
            }
    return None


async def check_problem_solved(handle: str, problem_id: str) -> bool:
    """Return True if the user has an AC submission for problem_id."""
    subs = await get_user_submissions(handle)
    for sub in subs:
        if sub.get("problem_id") == problem_id and sub.get("result") == "AC":
            return True
    return False


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
    """
    url_or_id = url_or_id.strip()
    m = re.search(r"contests/([^/?#]+)", url_or_id)
    if m:
        return m.group(1)
    if re.match(r"^[a-z0-9]+$", url_or_id, re.I):
        return url_or_id
    return None
