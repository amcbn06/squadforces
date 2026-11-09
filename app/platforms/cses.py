"""CSES: no API, so titles are entered by hand and solved status is marked by hand, or comes from the member's own
browser: app/cses_import.py takes the solved list a member's bookmarklet reads off their logged-in CSES page and
stores it as submissions, which this module turns into Result rows for the items that name those tasks."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from urllib.parse import ParseResult, urlparse

from app import models
from app.platforms.base import ParsedLink, Platform

CSES = "cses"

_TASK_PATH = re.compile(r"^/problemset/(?:task|view)/(\d+)(?:/|$)")


def is_url(url: ParseResult) -> bool:
    host = (url.hostname or "").lower()
    return host == "cses.fi" or host.endswith(".cses.fi")


def _task_link(task_id: str) -> ParsedLink:
    return ParsedLink("cses", "problem", task_id, f"CSES problem {task_id}")


def mark_solved_from_submissions(db, item, user_ids) -> None:
    """Mark `item` (a CSES task) solved for every one of `user_ids` whose imported progress includes it. A mark
    made by hand is never taken back, and an import never marks anything unsolved."""
    ids = list(user_ids)
    if item.type != "problem" or item.platform != "cses" or not ids:
        return
    first: dict[int, models.Submission] = {}
    rows = (
        db.query(models.Submission)
        .filter(models.Submission.user_id.in_(ids), models.Submission.platform == "cses",
                models.Submission.problem_key == item.external_id, models.Submission.accepted.is_(True))
        .order_by(models.Submission.submitted_at)
        .all()
    )
    for row in rows:
        first.setdefault(row.user_id, row)
    for user_id, row in first.items():
        result = db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=user_id).first()
        if result is None:
            result = models.Result(assignment_item_id=item.id, user_id=user_id)
            db.add(result)
        result.solved = True
        if result.solve_time is None and not row.backfilled:  # a backfilled row only says "before we started looking"
            result.solve_time = datetime.utcfromtimestamp(row.submitted_at)
        result.last_synced_at = datetime.utcnow()


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

    async def sync_item(self, item, members: list, db) -> None:
        mark_solved_from_submissions(db, item, [m.id for m in members])

    def submission_problem_url(self, sub) -> str:
        return f"https://cses.fi/problemset/task/{sub.problem_key}"

    def submission_problem_label(self, sub) -> str:
        return sub.problem_name or f"CSES {sub.problem_key}"


PLATFORM = Cses()
