"""infoarena.ro: no API, so solved status is marked by hand and titles are entered by hand."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import ParseResult, urlparse

from app.platforms.base import ParsedLink, Platform

INFOARENA = "infoarena"

# A problem's page is /problema/<id>, where the id is its short name ("adunare", "bfs", "cmlsc").
_NAME = r"[A-Za-z0-9_-]{1,100}"
_PROBLEM_PATH = re.compile(rf"^/problema/({_NAME})(?:/|$)")
_BARE_NAME = re.compile(rf"^{_NAME}$")


def is_url(url: ParseResult) -> bool:
    host = (url.hostname or "").lower()
    return host == "infoarena.ro" or host.endswith(".infoarena.ro")


def _problem_link(name: str) -> ParsedLink:
    name = name.lower()
    return ParsedLink("infoarena", "problem", name, f"infoarena problem {name}")


class Infoarena(Platform):
    key = "infoarena"
    label = "infoarena"
    icon = "infoarena-icon.png"

    def parse_url(self, source: str, url: ParseResult) -> Optional[ParsedLink]:
        m = _PROBLEM_PATH.match(url.path)
        return _problem_link(m.group(1)) if m else None

    def parse_form(self, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
        if item_type == "contest":
            return None, "infoarena rounds aren't tracked; add its problems one by one as problems."
        raw = raw.strip()
        if _BARE_NAME.match(raw):
            return _problem_link(raw), None
        if re.match(r"^(https?://|www\.|infoarena\.ro/)", raw, re.I):
            url = urlparse(raw if re.match(r"^https?://", raw, re.I) else "https://" + raw)
            link = self.parse_url(INFOARENA, url) if is_url(url) else None
            if link:
                return link, None
        return None, "Invalid infoarena problem URL or name (e.g. adunare or https://www.infoarena.ro/problema/adunare)."

    def item_url(self, item) -> str:
        return f"https://www.infoarena.ro/problema/{item.external_id}"

    def manual_status(self, item) -> bool:
        return True

    def default_title(self, item) -> str:
        return item.external_id


PLATFORM = Infoarena()
