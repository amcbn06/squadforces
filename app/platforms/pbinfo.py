"""pbinfo.ro: no API, so solved status is marked by hand and titles are entered by hand."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import ParseResult, parse_qs, urlparse

from app.platforms.base import ParsedLink, Platform

PBINFO = "pbinfo"

_PROBLEM_PATH = re.compile(r"^/probleme/(\d+)(?:/|$)")


def is_url(url: ParseResult) -> bool:
    host = (url.hostname or "").lower()
    return host == "pbinfo.ro" or host.endswith(".pbinfo.ro")


def _problem_link(problem_id: str) -> ParsedLink:
    return ParsedLink("pbinfo", "problem", problem_id, f"pbinfo problem {problem_id}")


class Pbinfo(Platform):
    key = "pbinfo"
    label = "pbinfo"
    icon = "pbinfo-icon.png"

    def parse_url(self, source: str, url: ParseResult) -> Optional[ParsedLink]:
        m = _PROBLEM_PATH.match(url.path)  # https://www.pbinfo.ro/probleme/1/sum
        if m:
            return _problem_link(m.group(1))
        query = parse_qs(url.query)  # https://www.pbinfo.ro/?pagina=probleme&id=1
        if query.get("pagina") == ["probleme"]:
            ids = query.get("id", [])
            if len(ids) == 1 and ids[0].isdigit():
                return _problem_link(ids[0])
        return None

    def parse_form(self, item_type: str, raw: str) -> tuple[Optional[ParsedLink], Optional[str]]:
        if item_type == "contest":
            return None, "pbinfo has no contests here; add its problems one by one as problems."
        raw = raw.strip()
        if raw.isdigit():
            return _problem_link(raw), None
        if re.match(r"^(https?://|www\.|pbinfo\.ro/)", raw, re.I):
            url = urlparse(raw if re.match(r"^https?://", raw, re.I) else "https://" + raw)
            link = self.parse_url(PBINFO, url) if is_url(url) else None
            if link:
                return link, None
        return None, "Invalid pbinfo problem URL or ID (e.g. 1 or https://www.pbinfo.ro/probleme/1/sum)."

    def item_url(self, item) -> str:
        return f"https://www.pbinfo.ro/probleme/{item.external_id}"

    def manual_status(self, item) -> bool:
        return True

    def default_title(self, item) -> str:
        return f"pbinfo {item.external_id}"


PLATFORM = Pbinfo()
