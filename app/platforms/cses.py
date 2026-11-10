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


def sync_results_from_import(db, item, users) -> None:
    """Make `item`'s (a CSES task's) results of the members among `users` who imported their CSES progress match
    that import: solved where it has the task, no mark where it doesn't (CSES is the source of truth for them, so
    a stale mark made by hand is replaced by what CSES says). Members who never imported keep whatever they marked
    by hand."""
    if item.type != "problem" or item.platform != "cses":
        return
    linked = [u.id for u in users if u.cses_id]
    if not linked:
        return
    first: dict[int, models.Submission] = {}
    rows = (
        db.query(models.Submission)
        .filter(models.Submission.user_id.in_(linked), models.Submission.platform == "cses",
                models.Submission.problem_key == item.external_id, models.Submission.accepted.is_(True))
        .order_by(models.Submission.submitted_at)
        .all()
    )
    for row in rows:
        first.setdefault(row.user_id, row)
    for user_id in linked:
        result = db.query(models.Result).filter_by(assignment_item_id=item.id, user_id=user_id).first()
        row = first.get(user_id)
        if row is None:
            if result is not None:
                db.delete(result)  # no row means "not marked", which the matrix shows as not solved here
            continue
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
        sync_results_from_import(db, item, members)

    def locked_status(self, item, user, solved: bool) -> Optional[str]:
        if not user.cses_id:
            return None
        return ("Solved on CSES (from the last import)" if solved else
                "Not solved on CSES at the last import. Import again to update it.")

    def manual_hint(self, item) -> str:
        return " It doesn't count in the rankings; importing your CSES progress (CSES in the menu) does."

    def submission_problem_url(self, sub) -> str:
        return f"https://cses.fi/problemset/task/{sub.problem_key}"

    def submission_problem_label(self, sub) -> str:
        return sub.problem_name or f"CSES {sub.problem_key}"


PLATFORM = Cses()
