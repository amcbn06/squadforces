"""The list of platforms. Adding one = a new module here + one line in PLATFORMS + one `if` in
app/problems.py::detect_source."""
from __future__ import annotations

from app.platforms import atc, cf, cses, kn, other
from app.platforms.base import Platform

PLATFORMS: dict[str, Platform] = {
    p.key: p for p in (cf.PLATFORM, atc.PLATFORM, kn.PLATFORM, cses.PLATFORM, other.PLATFORM)
}

OTHER = other.PLATFORM


def get(key: str) -> Platform:
    """The platform for a stored key; anything unrecognised is treated as "other" so old rows never break a page."""
    return PLATFORMS.get(key, OTHER)


def for_item(item) -> Platform:
    return get(item.platform)


def all_platforms() -> list[Platform]:
    return list(PLATFORMS.values())
