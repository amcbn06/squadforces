"""CodeChef: no API here, so solved status is marked by hand and titles are entered by hand."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import ParseResult, urlparse

from app.platforms.base import ParsedLink, Platform

CODECHEF = "codechef"

# /problems/<CODE> (FLOW001), or the same problem inside a contest: /<CONTEST>/problems/<CODE>. Codes are uppercase,
# which keeps list pages (/problems/school) out.
_CODE = r"[A-Z0-9_]{1,40}"
_PROBLEM_PATH = re.compile(rf"^/(?:[A-Za-z0-9_]+/)?problems/({_CODE})/?$")
_BARE_CODE = re.compile(r"^[A-Za-z0-9_]{1,40}$")


def is_url(url: ParseResult) -> bool:
    host = (url.hostname or "").lower()
    return host == "codechef.com" or host.endswith(".codechef.com")


def _problem_link(code: str) -> ParsedLink:
    code = code.upper()
    return ParsedLink("codechef", "problem", code, f"CodeChef problem {code}")


class CodeChef(Platform):
    key = "codechef"
    label = "CodeChef"
    icon = "codechef-icon.png"

    def parse_url(self, source: str, url: ParseResult) -> Optional[ParsedLink]:
        m = _PROBLEM_PATH.match(url.path)
        return _problem_link(m.group(1)) if m else None

    def parse_form(self, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
        if item_type == "contest":
            return None, "CodeChef contests aren't tracked; add its problems one by one as problems."
        raw = raw.strip()
        if _BARE_CODE.match(raw):
            return _problem_link(raw), None
        if re.match(r"^(https?://|www\.|codechef\.com/)", raw, re.I):
            url = urlparse(raw if re.match(r"^https?://", raw, re.I) else "https://" + raw)
            link = self.parse_url(CODECHEF, url) if is_url(url) else None
            if link:
                return link, None
        return None, "Invalid CodeChef problem URL or code (e.g. FLOW001 or https://www.codechef.com/problems/FLOW001)."

    def item_url(self, item) -> str:
        return f"https://www.codechef.com/problems/{item.external_id}"

    def manual_status(self, item) -> bool:
        return True

    def default_title(self, item) -> str:
        return item.external_id


PLATFORM = CodeChef()
