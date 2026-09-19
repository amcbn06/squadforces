import re
from urllib.parse import urlparse

MAX_LINKS = 50

_URL_START = re.compile(r"^(https?://|www\.)|^(codeforces\.com|atcoder\.jp|kilonova\.ro)/", re.I)
_CF_BARE_PROBLEM = re.compile(r"^(\d+)/?([A-Za-z]\d?)$")
_AC_BARE_PROBLEM = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+$")

_PLATFORM_NAMES = {"codeforces": "Codeforces", "atcoder": "AtCoder", "kilonova": "Kilonova"}


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

    return None


def classify(token: str) -> tuple[dict | None, str]:
    """Return (item, "") on success or (None, reason) on failure."""
    token = token.strip().strip("()<>[]\"'").rstrip(".")
    if not token:
        return None, "empty"

    if _URL_START.match(token):
        item = _classify_url(token)
        if item:
            return item, ""
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
