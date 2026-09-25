"""Kilonova scraper — uses the public Kilonova REST API (kilonova.ro/api/...)."""
from urllib.parse import quote

import httpx

BASE = "https://kilonova.ro/api"
HEADERS = {"Authorization": "guest", "User-Agent": "Squadforces/1.0"}


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
        resp = await client.get(f"{BASE}{path}", params=params)
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != "success":
        raise ValueError(f"Kilonova API error: {body.get('data')}")
    return body["data"]


# ---------------------------------------------------------------------------
# Validation / parsing
# ---------------------------------------------------------------------------

def parse_problem_id(raw: str) -> int | None:
    """Accept kilonova.ro/problems/2460 or bare '2460'. Returns int id or None."""
    raw = raw.strip().rstrip("/")
    if "/problems/" in raw:
        part = raw.split("/problems/")[-1].split("/")[0]
    else:
        part = raw
    try:
        return int(part)
    except ValueError:
        return None


def parse_contest_id(raw: str) -> int | None:
    """Accept kilonova.ro/problem_lists/1572 or bare '1572'. Returns int id or None."""
    raw = raw.strip().rstrip("/")
    if "/problem_lists/" in raw:
        part = raw.split("/problem_lists/")[-1].split("/")[0]
    else:
        part = raw
    try:
        return int(part)
    except ValueError:
        return None


async def validate_handle(username: str) -> dict | None:
    """Return user info dict if handle exists, None otherwise."""
    try:
        return await _get(f"/user/byName/{quote(username, safe='')}")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Problem / contest metadata
# ---------------------------------------------------------------------------

async def get_problem(problem_id: int) -> dict:
    """Return problem info: {id, name, score_scale, ...}"""
    return await _get(f"/problem/{problem_id}")


async def get_problem_list(list_id: int) -> dict:
    """Return problem list info: {id, title, list: [problem_id, ...], ...}"""
    return await _get(f"/problemList/{list_id}")


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------

KN_PAGE_SIZE = 50  # the API's maximum


async def get_user_submissions(user_id: int, offset: int = 0) -> tuple[list[dict], int]:
    """One page of a user's submissions, newest first, and the user's total submission count."""
    data = await _get("/submissions/get", params={"user_id": user_id, "limit": KN_PAGE_SIZE, "offset": offset})
    return data.get("submissions", []), data.get("count", 0)


async def get_user_id(username: str) -> int | None:
    """Resolve a Kilonova username to a numeric user_id."""
    info = await validate_handle(username)
    return info["id"] if info else None
