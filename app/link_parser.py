import re
from urllib.parse import urlparse

MAX_LINKS = 50

_URL_START = re.compile(r"^(https?://|www\.)|^(codeforces\.com|atcoder\.jp|kilonova\.ro|cses\.fi)/", re.I)
_CSES_TASK = re.compile(r"^/problemset/(?:task|view)/(\d+)(?:/|$)")
_CF_BARE_PROBLEM = re.compile(r"^(\d+)/?([A-Za-z]\d?)$")
_AC_BARE_PROBLEM = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+$")
# Codeforces EDU practice problem, e.g. /edu/course/2/lesson/4/3/practice/contest/274545/problem/A
_CF_EDU_PROBLEM = re.compile(r"^/edu/course/\d+/lesson/\d+(?:/\d+)*/practice/contest/(\d+)/problem/([A-Za-z]\d?)/?$")

_PLATFORM_NAMES = {"codeforces": "Codeforces", "atcoder": "AtCoder", "kilonova": "Kilonova", "cses": "CSES"}


def _item(platform: str, item_type: str, external_id: str) -> dict:
    kind = "contest" if item_type == "contest" else "problem"
    return {
        "platform": platform,
        "type": item_type,
        "external_id": external_id,
        "label": f"{_PLATFORM_NAMES[platform]} {kind} {external_id}",
    }


def _classify_url(token: str) -> dict | None:
    if not re.match(r"^https?://", token, re.I):
        token = "https://" + token
    parsed = urlparse(token)
    host = (parsed.hostname or "").lower()
    path = parsed.path

    if host.endswith("codeforces.com"):
        m = _CF_EDU_PROBLEM.match(path)
        if m:
            item = _item("codeforces", "problem", f"{m.group(1)}/{m.group(2).upper()}")
            canonical = path.rstrip("/")[: -len(m.group(2))] + m.group(2).upper()
            item["source_url"] = f"https://codeforces.com{canonical}"
            return item
        m = re.match(r"^/(?:problemset/problem|contest)/(\d+)/(?:problem/)?([A-Za-z]\d?)/?$", path)
        if m:
            return _item("codeforces", "problem", f"{m.group(1)}/{m.group(2).upper()}")
        m = re.match(r"^/(?:contest|gym)/(\d+)(?:/|$)", path)
        if m:
            return _item("codeforces", "contest", m.group(1))
        return None

    if host.endswith("atcoder.jp"):
        m = re.match(r"^/contests/([^/]+)/tasks/([^/]+)/?$", path)
        if m:
            return _item("atcoder", "problem", m.group(2))
        m = re.match(r"^/contests/([^/]+)(?:/|$)", path)
        if m and m.group(1) != "archive":
            return _item("atcoder", "contest", m.group(1))
        return None

    if host.endswith("kilonova.ro"):
        m = re.match(r"^/problems/(\d+)(?:/|$)", path)
        if m:
            return _item("kilonova", "problem", m.group(1))
        m = re.match(r"^/problem_lists/(\d+)(?:/|$)", path)
        if m:
            return _item("kilonova", "contest", m.group(1))
        return None

    if host.endswith("cses.fi"):
        m = _CSES_TASK.match(path)
        if m:
            return _item("cses", "problem", m.group(1))
        return None

    return None


def cses_task_id(raw: str) -> str | None:
    """Task id from a CSES task URL, or from a bare number."""
    raw = raw.strip()
    if raw.isdigit():
        return raw
    item = _classify_url(raw)
    return item["external_id"] if item and item["platform"] == "cses" else None


def _host_is(token: str, suffix: str) -> bool:
    if not re.match(r"^https?://", token, re.I):
        token = "https://" + token
    return (urlparse(token).hostname or "").lower().endswith(suffix)


def edu_problem_url(token: str) -> str | None:
    """Canonical link if `token` is a Codeforces EDU practice problem URL, else None."""
    item = _classify_url(token.strip())
    return item.get("source_url") if item else None


def _is_cf_edu_page(token: str) -> bool:
    if not re.match(r"^https?://", token, re.I):
        token = "https://" + token
    parsed = urlparse(token)
    return (parsed.hostname or "").lower().endswith("codeforces.com") and parsed.path.startswith("/edu/")


def classify(token: str) -> tuple[dict | None, str]:
    """Return (item, "") on success or (None, reason) on failure."""
    token = token.strip().strip("()<>[]\"'").rstrip(".")
    if not token:
        return None, "empty"

    if _URL_START.match(token):
        item = _classify_url(token)
        if item:
            return item, ""
        if _is_cf_edu_page(token):
            return None, "Codeforces EDU page - paste a link to a single problem (.../practice/contest/<id>/problem/A)"
        if _host_is(token, "cses.fi"):
            return None, "CSES page - paste a link to a single task (https://cses.fi/problemset/task/<id>)"
        return None, "unsupported link (expected a contest, problem or problem list URL)"

    if token.isdigit():
        return None, "ambiguous number - paste the full URL so the platform is known"

    m = _CF_BARE_PROBLEM.match(token)
    if m:
        return _item("codeforces", "problem", f"{m.group(1)}/{m.group(2).upper()}"), ""

    if _AC_BARE_PROBLEM.match(token):
        return _item("atcoder", "problem", token), ""

    return None, "not recognized as a link or problem ID"


def parse_links(text: str) -> tuple[list[dict], list[tuple[str, str]]]:
    """Split free text into tokens and classify each. Returns (unique items, [(token, reason)])."""
    items: list[dict] = []
    errors: list[tuple[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for token in re.split(r"[\s,;]+", text):
        if not token.strip():
            continue
        item, reason = classify(token)
        if item is None:
            errors.append((token, reason))
            continue
        key = (item["platform"], item["type"], item["external_id"])
        if key not in seen:
            seen.add(key)
            items.append(item)
    return items, errors
