"""Recognising where a problem or contest comes from.

`detect_source()` is the one switch: an `if` per source, first match wins, with a default case at the end. Once the
source is known, everything specific to that judge (what its links look like, how to fetch, how to display) is in
its own module under app/platforms/. To support a new judge: add the module, register it in
app/platforms/registry.py, add one `if` to `detect_source()` and one line to SOURCE_PLATFORM.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import ParseResult, urlparse

from app.platforms import atc, cf, cses, kn, other, registry
from app.platforms.base import ParsedLink

MAX_LINKS = 50

# Sources. Several can belong to one platform (Codeforces has regular contests, gyms and EDU).
CF = cf.CF
CF_EDU = cf.CF_EDU
CF_GYM = cf.CF_GYM
ATC = atc.ATC
KN = kn.KN
CSES = cses.CSES
UNKNOWN = "unknown"

# Source -> key of the platform that handles it.
SOURCE_PLATFORM = {
    CF: "codeforces",
    CF_EDU: "codeforces",
    CF_GYM: "codeforces",
    ATC: "atcoder",
    KN: "kilonova",
    CSES: "cses",
    UNKNOWN: "other",
}

_SCHEME = re.compile(r"^https?://", re.I)
# something that is clearly a link even without a scheme: "www.x.y", or "host.tld/path"
_LINK_LIKE = re.compile(r"^(?:https?://|www\.|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::\d+)?/)", re.I)


def as_url(token: str) -> Optional[ParseResult]:
    """`token` as a parsed URL if it looks like a link, else None."""
    token = token.strip()
    if not _LINK_LIKE.match(token):
        return None
    return urlparse(token if _SCHEME.match(token) else "https://" + token)


def detect_source(token: str) -> str:
    """Which source a link or bare ID belongs to: one of CF, CF_EDU, CF_GYM, ATC, KN, CSES or UNKNOWN."""
    token = token.strip()
    url = as_url(token)
    if url is not None:
        if cf.is_edu_url(url):
            return CF_EDU
        if cf.is_gym_url(url):
            return CF_GYM
        if cf.is_url(url):
            return CF
        if atc.is_url(url):
            return ATC
        if kn.is_url(url):
            return KN
        if cses.is_url(url):
            return CSES
        return UNKNOWN
    if cf.is_bare_id(token):
        return CF
    if atc.is_bare_id(token):
        return ATC
    return UNKNOWN


def _unparsed_reason(source: str) -> str:
    """Why a link to a known judge couldn't become an item."""
    if source == CF_EDU:
        return "Codeforces EDU page - paste a link to a single problem (.../practice/contest/<id>/problem/A)"
    if source == CSES:
        return "CSES page - paste a link to a single task (https://cses.fi/problemset/task/<id>)"
    return "unsupported link (expected a contest, problem or problem list URL)"


_WRAPPING = "()<>[]\"'.,;"  # what surrounds a link pasted from prose or markdown


def _clean(token: str) -> str:
    """Strip whitespace and wrapping punctuation, repeatedly: "(https://x.y/z)." -> "https://x.y/z"."""
    previous = None
    while previous != token:
        previous = token
        token = token.strip().strip(_WRAPPING)
    return token


def parse_link(token: str) -> tuple[Optional[ParsedLink], str]:
    """Return (link, "") on success or (None, reason) on failure."""
    token = _clean(token)
    if not token:
        return None, "empty"

    source = detect_source(token)
    platform = registry.get(SOURCE_PLATFORM[source])
    url = as_url(token)

    if url is not None:
        if source == UNKNOWN:  # default case: not a judge we know, so keep it as a plain link
            usable = other.normalize_url(url.geturl())
            return (other.link_for(usable), "") if usable else (None, "not a usable link")
        link = platform.parse_url(source, url)
        return (link, "") if link else (None, _unparsed_reason(source))

    if token.isdigit():
        return None, "ambiguous number - paste the full URL so the platform is known"
    link = platform.parse_bare(token) if source != UNKNOWN else None
    return (link, "") if link else (None, "not recognized as a link or problem ID")


def parse_links(text: str) -> tuple[list[ParsedLink], list[tuple[str, str]]]:
    """Split free text into tokens and classify each. Returns (unique links, [(token, reason)])."""
    links: list[ParsedLink] = []
    errors: list[tuple[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for token in re.split(r"[\s,;]+", text):
        if not token.strip():
            continue
        link, reason = parse_link(token)
        if link is None:
            errors.append((token, reason))
            continue
        key = (link.platform, link.type, link.external_id)
        if key not in seen:
            seen.add(key)
            links.append(link)
    return links, errors


def parse_form(platform_key: str, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
    """The "add an item" form: the platform was chosen explicitly, so hand `raw` straight to it."""
    platform = registry.PLATFORMS.get(platform_key)
    if platform is None or item_type not in ("contest", "problem"):
        return None, "Unknown platform or item type."
    return platform.parse_form(item_type, raw)
