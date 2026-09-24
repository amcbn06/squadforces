"""Anything that isn't a supported judge: kept as a plain link (its URL is the title until renamed) and marked
solved by hand. It is also what the registry falls back to for an unrecognised platform key."""
from __future__ import annotations

import hashlib
from typing import Optional
from urllib.parse import urlparse

from app.platforms.base import ParsedLink, Platform

MAX_URL_LENGTH = 500  # AssignmentItem.source_url


def normalize_url(raw: str) -> Optional[str]:
    """The link if it is a usable http(s) URL, else None. Other schemes (javascript:, data:, file:) are refused,
    since the link is rendered as a clickable anchor."""
    raw = raw.strip()
    if not raw or any(c.isspace() for c in raw):
        return None
    if raw.lower().startswith("www."):
        raw = "https://" + raw
    if len(raw) > MAX_URL_LENGTH:
        return None
    url = urlparse(raw)
    if url.scheme.lower() not in ("http", "https") or not url.hostname or "." not in url.hostname:
        return None
    return raw


def link_for(url: str) -> ParsedLink:
    ext = "url:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    shown = url if len(url) <= 60 else url[:57] + "..."
    return ParsedLink("other", "problem", ext, f"link {shown}", url)


class Other(Platform):
    key = "other"
    label = "Other"
    icon = None  # the UI shows a "?" placeholder

    def parse_form(self, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
        if item_type == "contest":
            return None, "Only problems can be added from other sites; paste the link to a single problem."
        url = normalize_url(raw)
        if not url:
            return None, "Enter a full http(s) link to the problem."
        return link_for(url), None

    def item_url(self, item) -> str:
        return item.source_url or ""

    def manual_status(self, item) -> bool:
        return True

    def default_title(self, item) -> str:
        return item.source_url or item.external_id


PLATFORM = Other()
