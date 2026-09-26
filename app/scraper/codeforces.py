"""Codeforces API adapter — wraps the official CF API."""
import asyncio
import os
import time
import hashlib
import random
import string
import weakref
from contextlib import asynccontextmanager
from typing import Optional
import httpx

CF_BASE = "https://codeforces.com/api"
CF_KEY = os.getenv("CF_API_KEY", "")
CF_SECRET = os.getenv("CF_API_SECRET", "")


class ContestNotStartedError(Exception):
    """Raised when a CF contest exists but has not started yet."""

_last_request_time: float = 0.0
_MIN_INTERVAL = 2.1  # seconds between requests (CF recommends ≤1 req/2s)
_LIMIT_RETRIES = 2   # extra attempts when CF answers "call limit exceeded"

# One lock per event loop: requests are made one at a time so concurrent sync tasks can't both slip through the
# 2.1 s spacing. (A lock is bound to the loop it first waits on, hence the per-loop table.)
_rate_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


@asynccontextmanager
async def _throttle():
    """Hold the request slot: wait out the spacing since the previous request, then stamp the time once done."""
    global _last_request_time
    loop = asyncio.get_running_loop()
    lock = _rate_locks.get(loop)
    if lock is None:
        lock = _rate_locks[loop] = asyncio.Lock()
    async with lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request_time)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            yield
        finally:
            _last_request_time = time.monotonic()


# Codeforces names problems in the language the request asks for, and answers in Russian when it doesn't say
# (from some servers). Ask for English on every call: it is a header, so the anonymous standings call that
# forbids extra parameters still accepts it.
HEADERS = {"Accept-Language": "en"}


async def _call(method: str, params: dict, *, signed: bool = True, timeout: float = 15) -> dict:
    for attempt in range(_LIMIT_RETRIES + 1):
        call_params = dict(params)
        if signed and CF_KEY and CF_SECRET:
            call_params["apiKey"] = CF_KEY
            call_params["time"] = str(int(time.time()))
            rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
            param_str = "&".join(f"{k}={v}" for k, v in sorted(call_params.items()))
            to_hash = f"{rand}/{method}?{param_str}#{CF_SECRET}"
            call_params["apiSig"] = rand + hashlib.sha512(to_hash.encode()).hexdigest()

        url = f"{CF_BASE}/{method}"
        async with _throttle():
            async with httpx.AsyncClient(timeout=timeout, headers=HEADERS) as client:
                resp = await client.get(url, params=call_params)

        try:
            data = resp.json()
        except ValueError:
            raise ValueError(f"CF API returned an unreadable response (HTTP {resp.status_code})") from None
        if data.get("status") == "OK":
            return data["result"]
        comment = str(data.get("comment", data))
        if "limit exceeded" in comment.lower() and attempt < _LIMIT_RETRIES:
            await asyncio.sleep(3)
            continue
        raise ValueError(f"CF API error: {comment}")


async def get_contest_list() -> list[dict]:
    """Return metadata for all CF contests (single call, no auth needed)."""
    return await _call("contest.list", {"gym": "false"}, signed=False)


_GYM_LIST_TTL = 12 * 3600
_gym_list: Optional[tuple[float, dict[int, dict]]] = None


async def get_gym_metadata(contest_id: str) -> Optional[dict]:
    """{id, name, durationSeconds, startTimeSeconds, ...} of a gym, or None if it isn't in the public gym list.
    The list is ~2600 entries in one call, so it is cached for all lookups."""
    global _gym_list
    if _gym_list is None or time.monotonic() - _gym_list[0] > _GYM_LIST_TTL:
        gyms = await _call("contest.list", {"gym": "true"}, signed=False, timeout=60)
        _gym_list = (time.monotonic(), {g["id"]: g for g in gyms})
    return _gym_list[1].get(int(contest_id))


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


_PROBLEMSET_TTL = 12 * 3600
_problemset_ratings: Optional[tuple[float, dict[tuple[str, str], int]]] = None


async def get_problem_rating(contest_id: str, index: str) -> Optional[int]:
    """Rating of a problem, or None if unrated. Uses one cached problemset.problems call for all lookups."""
    global _problemset_ratings
    now = time.monotonic()
    if _problemset_ratings is None or now - _problemset_ratings[0] > _PROBLEMSET_TTL:
        result = await _call("problemset.problems", {}, signed=False)
        ratings = {
            (str(p["contestId"]), p["index"]): p["rating"]
            for p in result.get("problems", [])
            if "contestId" in p and "rating" in p
        }
        _problemset_ratings = (now, ratings)
    return _problemset_ratings[1].get((str(contest_id), index.upper()))


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

    title = ""
    problems: list[dict] = []

    stream_err: Exception | None = None
    buf = b""
    try:
        url = f"{CF_BASE}/contest.standings"
        async with _throttle():
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
                async with client.stream("GET", url, params={"contestId": contest_id}) as resp:
                    async for chunk in resp.aiter_bytes(chunk_size=1024):
                        buf += chunk
                        # Stop once we've seen the start of "rows" — problems come before it
                        if b'"rows"' in buf or len(buf) >= 16384:
                            break
    except Exception as e:
        stream_err = e
        import logging as _log
        _log.getLogger(__name__).warning("Contest %s stream error: %s", contest_id, e)

    # Detect explicit FAILED response — contest not started yet
    if buf and b'"FAILED"' in buf and b'not started' in buf.lower():
        raise ContestNotStartedError(f"Contest {contest_id} has not started yet")

    try:
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
                except Exception as e:
                    import logging as _log
                    _log.getLogger(__name__).warning("Contest %s: JSON parse error: %s", contest_id, e)
        else:
            import logging as _log
            _log.getLogger(__name__).warning(
                "Contest %s: 'problems' key not found in first %d bytes (buf snippet: %s)",
                contest_id, len(buf), buf[:200]
            )
    except Exception as e:
        import logging as _log
        _log.getLogger(__name__).warning("Contest %s parse error: %s", contest_id, e)

    # Fallback: discover problems from submitted contest.status
    if not problems:
        problems = await get_contest_problems_from_status(contest_id)

    return {"title": title, "problems": problems}


async def get_contest_metadata(contest_id: str) -> dict | None:
    """
    Return {id, name, phase, startTimeSeconds, ...} for a single contest
    by searching contest.list. Used to get the name of not-yet-started contests
    since contest.standings is unavailable for them.
    """
    try:
        all_contests = await _call("contest.list", {"gym": "false"}, signed=False)
        cid = int(contest_id)
        for c in all_contests:
            if c.get("id") == cid:
                return c
    except Exception:
        pass
    return None


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


async def get_user_status(handle: str, offset: int = 1, count: int = 100000) -> list[dict]:
    """A page of a user's submissions, newest first (offset is 1-based). Includes gym and team submissions,
    but not Codeforces EDU practice ones. Raises on failure, so callers can tell "nothing yet" from "no answer"."""
    return await _call(
        "user.status",
        {"handle": handle, "from": str(offset), "count": str(count)},
        signed=False,
        timeout=60,
    )


async def get_user_rating_history(handle: str) -> list[dict]:
    """Return rating change history for a user."""
    result = await _call("user.rating", {"handle": handle})
    return result


async def get_gym_problems(contest_id: str) -> list[dict]:
    """Full problem list of a gym. Gym standings need a login, so the problems are collected from the contest's
    whole submission list, which names every problem someone has attempted (one large call, made once per item)."""
    subs = await _call(
        "contest.status", {"contestId": contest_id, "from": "1", "count": "100000"}, signed=False, timeout=90
    )
    seen: dict[str, dict] = {}
    for s in subs:
        prob = s.get("problem", {})
        idx = prob.get("index", "")
        if idx and idx not in seen:
            seen[idx] = {"index": idx, "name": prob.get("name", ""), "rating": prob.get("rating")}
    return sorted(seen.values(), key=lambda p: (len(p["index"]), p["index"]))
