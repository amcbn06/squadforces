"""CSES: no API, so solved status is marked by hand and titles are entered by hand."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import ParseResult, urlparse

from app.platforms.base import ParsedLink, Platform

CSES = "cses"

_TASK_PATH = re.compile(r"^/problemset/(?:task|view)/(\d+)(?:/|$)")


def is_url(url: ParseResult) -> bool:
    host = (url.hostname or "").lower()
    return host == "cses.fi" or host.endswith(".cses.fi")


def _task_link(task_id: str) -> ParsedLink:
    return ParsedLink("cses", "problem", task_id, f"CSES problem {task_id}")


class Cses(Platform):
    key = "cses"
    label = "CSES"
    icon = "cses-icon.png"

    def parse_url(self, source: str, url: ParseResult) -> Optional[ParsedLink]:
        m = _TASK_PATH.match(url.path)
        return _task_link(m.group(1)) if m else None

    def parse_form(self, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
        if item_type == "contest":
            return None, "CSES has no contests; add its tasks one by one as problems."
        raw = raw.strip()
        if raw.isdigit():
            return _task_link(raw), None
        if re.match(r"^(https?://|www\.|cses\.fi/)", raw, re.I):
            url = urlparse(raw if re.match(r"^https?://", raw, re.I) else "https://" + raw)
            link = self.parse_url(CSES, url) if is_url(url) else None
            if link:
                return link, None
        return None, "Invalid CSES task URL or ID (e.g. 1068 or https://cses.fi/problemset/task/1068)."

    def item_url(self, item) -> str:
        return f"https://cses.fi/problemset/task/{item.external_id}"

    def manual_status(self, item) -> bool:
        return True

    def default_title(self, item) -> str:
        return f"CSES {item.external_id}"


PLATFORM = Cses()
