"""Codeforces API adapter — wraps the official CF API."""
import asyncio
import os
import time
import hashlib
import random
import string
from typing import Optional
import httpx

CF_BASE = "https://codeforces.com/api"
CF_KEY = os.getenv("CF_API_KEY", "")
CF_SECRET = os.getenv("CF_API_SECRET", "")

_last_request_time: float = 0.0
_MIN_INTERVAL = 2.1  # seconds between requests (CF recommends ≤1 req/2s)


async def _call(method: str, params: dict, *, signed: bool = True) -> dict:
    global _last_request_time
    now = time.monotonic()
    wait = _MIN_INTERVAL - (now - _last_request_time)
    if wait > 0:
        await asyncio.sleep(wait)

    if signed and CF_KEY and CF_SECRET:
        params["apiKey"] = CF_KEY
        params["time"] = str(int(time.time()))
        rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        to_hash = f"{rand}/{method}?{param_str}#{CF_SECRET}"
        params["apiSig"] = rand + hashlib.sha512(to_hash.encode()).hexdigest()

    url = f"{CF_BASE}/{method}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
    _last_request_time = time.monotonic()

    data = resp.json()
    if data.get("status") != "OK":
        raise ValueError(f"CF API error: {data.get('comment', data)}")
    return data["result"]


async def get_user_info(handles: list[str]) -> list[dict]:
    """Return basic info for one or more handles."""
    result = await _call("user.info", {"handles": ";".join(handles)})
    return result


async def validate_handle(handle: str) -> Optional[dict]:
    """Return user info dict or None if handle doesn't exist."""
    try:
        users = await get_user_info([handle])
        return users[0] if users else None
    except Exception:
        return None


async def get_contest_standings(contest_id: str, handles: list[str]) -> dict:
    """Return standings for specific handles in a contest."""
    result = await _call("contest.standings", {
        "contestId": contest_id,
        "handles": ";".join(handles),
        "showUnofficial": "true",
        "from": "1",
        "count": "5",  # just enough to get problems list
    })
    return result


async def get_contest_problems(contest_id: str) -> list[dict]:
    """Return list of problems in a contest."""
    info = await get_contest_info(contest_id)
    return info.get("problems", [])


async def get_contest_info(contest_id: str) -> dict:
    """
    Return {title, problems} for a contest.

    Primary: stream the first 16 KB of anonymous contest.standings to capture
    both the contest name and the full problems array (which appears before the
    potentially huge "rows" section). Falls back to contest.status for problems
    if the stream doesn't contain a parseable problems list.

    CF requirement: no API key, no extra params — only contestId.
    """
    import re as _re
    import json as _json

    global _last_request_time
    now = time.monotonic()
    wait = _MIN_INTERVAL - (now - _last_request_time)
    if wait > 0:
        await asyncio.sleep(wait)

    title = ""
    problems: list[dict] = []

    try:
        url = f"{CF_BASE}/contest.standings"
        async with httpx.AsyncClient(timeout=15) as client:
            async with client.stream("GET", url, params={"contestId": contest_id}) as resp:
                buf = b""
                async for chunk in resp.aiter_bytes(chunk_size=1024):
                    buf += chunk
                    # Stop once we've seen the start of "rows" — problems come before it
                    if b'"rows"' in buf or len(buf) >= 16384:
                        break
        _last_request_time = time.monotonic()

        # Extract title
        m = _re.search(rb'"name"\s*:\s*"([^"]+)"', buf)
        if m:
            title = m.group(1).decode("utf-8", errors="replace")

        # Extract problems array using bracket counting (avoids nested-array regex issues)
        pm = _re.search(rb'"problems"\s*:\s*\[', buf)
        if pm:
            start = pm.end() - 1  # position of the opening '['
            depth = 0
            end = start
            for i in range(start, len(buf)):
                b = buf[i:i+1]
                if b == b'[':
                    depth += 1
                elif b == b']':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > start:
                try:
                    problems = _json.loads(buf[start:end].decode("utf-8", errors="replace"))
                except Exception:
                    pass
    except Exception:
        pass

    # Fallback: discover problems from submitted contest.status
    if not problems:
        problems = await get_contest_problems_from_status(contest_id)

    return {"title": title, "problems": problems}


async def get_contest_problems_from_status(contest_id: str) -> list[dict]:
    """
    Fallback when standings are blocked: sample contest.status submissions
    to discover problem indices.  Returns a partial list — only problems
    that at least one CF user submitted to within the sampled window.
    """
    try:
        subs = await _call("contest.status", {
            "contestId": contest_id,
            "from": "1",
            "count": "200",
        })
    except Exception:
        return []

    seen: dict[str, dict] = {}
    for s in subs:
        prob = s.get("problem", {})
        idx = prob.get("index", "")
        if idx and idx not in seen:
            seen[idx] = {
                "index": idx,
                "name": prob.get("name", ""),
                "rating": prob.get("rating"),
            }
    return sorted(seen.values(), key=lambda p: p["index"])


async def get_contest_results_for_handles(contest_id: str, handles: list[str]) -> dict:
    """
    Returns full standings for given handles.
    Result shape: {contest: {...}, problems: [...], rows: [...]}
    """
    result = await _call("contest.standings", {
        "contestId": contest_id,
        "handles": ";".join(handles),
        "showUnofficial": "true",
    })
    return result


async def get_all_user_submissions(handle: str, max_count: int = 3000) -> list[dict]:
    """Return recent submissions for a user (unsigned, capped at max_count)."""
    try:
        return await _call("user.status", {
            "handle": handle, "from": "1", "count": str(max_count),
        }, signed=False)
    except Exception:
        return []


async def get_user_rating_history(handle: str) -> list[dict]:
    """Return rating change history for a user."""
    result = await _call("user.rating", {"handle": handle})
    return result


async def get_user_submissions_for_contest(handle: str, contest_id: str) -> list[dict]:
    """Return all submissions by handle in a specific contest (via user.status)."""
    try:
        all_subs = await _call("user.status", {"handle": handle, "from": "1", "count": "10000"})
    except Exception:
        return []
    return [
        s for s in all_subs
        if str(s.get("problem", {}).get("contestId")) == str(contest_id)
    ]


async def get_problem_solved(handle: str, contest_id: str, problem_index: str) -> bool:
    """Check if handle solved a specific problem (by contestId + index)."""
    try:
        submissions = await _call("user.status", {
            "handle": handle,
            "from": "1",
            "count": "10000",
        })
    except Exception:
        return False

    for sub in submissions:
        p = sub.get("problem", {})
        if (
            str(p.get("contestId")) == str(contest_id)
            and p.get("index") == problem_index
            and sub.get("verdict") == "OK"
        ):
            return True
    return False


def parse_problem_external_id(url_or_id: str) -> Optional[tuple[str, str]]:
    """
    Parse a Codeforces problem URL or ID into (contest_id, index).
    Supports:
      https://codeforces.com/problemset/problem/1234/A
      https://codeforces.com/contest/1234/problem/A
      1234A  or  1234/A
    Returns None if not parseable.
    """
    import re
    url_or_id = url_or_id.strip()

    # URL patterns
    m = re.search(r"(?:problemset/problem|contest)/(\d+)/(?:problem/)?([A-Z]\d*)", url_or_id, re.I)
    if m:
        return m.group(1), m.group(2).upper()

    # Short forms: "1234A", "1234/A"
    m = re.match(r"^(\d+)[/\s]*([A-Z]\d*)$", url_or_id, re.I)
    if m:
        return m.group(1), m.group(2).upper()

    return None


def parse_contest_id(url_or_id: str) -> Optional[str]:
    """
    Parse a Codeforces contest URL or raw ID.
    Supports:
      https://codeforces.com/contest/1234
      https://codeforces.com/gym/102956
      1234
    """
    import re
    url_or_id = url_or_id.strip()

    m = re.search(r"(?:contest|gym)/(\d+)", url_or_id)
    if m:
        return m.group(1)

    if re.match(r"^\d+$", url_or_id):
        return url_or_id

    return None
