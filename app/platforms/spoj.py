"""SPOJ: no API, so solved status is marked by hand and titles are entered by hand."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import ParseResult, urlparse

from app.platforms.base import ParsedLink, Platform

SPOJ = "spoj"

# A problem's page is /problems/<CODE>/ (TEST, PRIME1, ADDREV). Codes are uppercase, which is what tells them apart
# from the lowercase list pages (/problems/classical/).
_CODE = r"[A-Z0-9_]{1,40}"
_PROBLEM_PATH = re.compile(rf"^/problems/({_CODE})/?$")
_BARE_CODE = re.compile(r"^[A-Za-z0-9_]{1,40}$")


def is_url(url: ParseResult) -> bool:
    host = (url.hostname or "").lower()
    return host == "spoj.com" or host.endswith(".spoj.com")


def _problem_link(code: str) -> ParsedLink:
    code = code.upper()
    return ParsedLink("spoj", "problem", code, f"SPOJ problem {code}")


class Spoj(Platform):
    key = "spoj"
    label = "SPOJ"
    icon = "spoj-icon.png"

    def parse_url(self, source: str, url: ParseResult) -> Optional[ParsedLink]:
        m = _PROBLEM_PATH.match(url.path)
        return _problem_link(m.group(1)) if m else None

    def parse_form(self, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
        if item_type == "contest":
            return None, "SPOJ contests aren't tracked; add its problems one by one as problems."
        raw = raw.strip()
        if _BARE_CODE.match(raw):
            return _problem_link(raw), None
        if re.match(r"^(https?://|www\.|spoj\.com/)", raw, re.I):
            url = urlparse(raw if re.match(r"^https?://", raw, re.I) else "https://" + raw)
            link = self.parse_url(SPOJ, url) if is_url(url) else None
            if link:
                return link, None
        return None, "Invalid SPOJ problem URL or code (e.g. TEST or https://www.spoj.com/problems/TEST/)."

    def item_url(self, item) -> str:
        return f"https://www.spoj.com/problems/{item.external_id}/"

    def manual_status(self, item) -> bool:
        return True

    def default_title(self, item) -> str:
        return item.external_id


PLATFORM = Spoj()
